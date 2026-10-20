"""Confirmed upstream intake (2026-10-12) plus preserved historical inbox.

Current intake is receive_confirmed: automatically create, never start.
The old pending/confirm/reject/link functions below are historical compatibility,
not the current product workflow or HTTP routes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

from .fire_events import EventRegistry, FireEventRecord, utc_now_iso

#: 收件箱状态机：pending 为唯一可动作态，其余为终态（不可逆，重开须新报警）。
ALARM_STATUS_WORDS = {
    "created": "已自动建事件",
    "pending": "待确认",
    "confirmed": "已确认建档",
    "rejected": "已拒绝",
    "duplicate": "重复/已关联",
}

#: 原始报文摘要截断长度（完整原文以 SHA-256 + 来源系统留存为准）。
_SUMMARY_LIMIT = 240


@dataclass
class ExternalAlarmRecord:
    """一条外部报警（01 号 §3.2 字段同构映射 + 接入元数据）。"""

    alarm_id: str             # alm-{n}，收件箱内编列
    source_system: str        # 外部系统名称/编码
    external_no: str          # 外部报警编号（sourceRecordId）
    received_at: str          # 本系统接收时刻（ISO）
    reported_at: str          # 原始报警时间（eventTime）
    name: str                 # 火情名称
    location: str             # 位置描述
    level: str = ""           # 等级/告警级别（原文）
    source_status: str = ""   # 外部状态枚举原文：只存不解释
    initial_report: str = ""  # 初报内容
    key_targets: tuple[str, ...] = ()
    raw_digest: str = ""      # 原始报文规范化 JSON 的 SHA-256
    raw_summary: str = ""     # 原始报文摘要（截断）
    remark: str = ""          # 如「演示数据」标注
    suggested_event_id: str = ""  # 外部 eventId 仅作参考显示，不用作内部编号
    status: str = "pending"
    event_id: str = ""        # confirmed/duplicate 后的关联正式事件
    reject_reason: str = ""
    history: list[dict] = field(default_factory=list)

    @property
    def index_key(self) -> str:
        return f"{self.source_system}|{self.external_no}"

    def status_word(self) -> str:
        return ALARM_STATUS_WORDS.get(self.status, self.status)

    def to_dict(self) -> dict:
        payload = vars(self).copy()
        payload["key_targets"] = list(self.key_targets)
        return payload

    @classmethod
    def from_dict(cls, payload: dict) -> "ExternalAlarmRecord":
        data = dict(payload)
        data["key_targets"] = tuple(data.get("key_targets") or ())
        return cls(**data)


def _digest_and_summary(message: dict) -> tuple[str, str]:
    """规范化 JSON 的 SHA-256 + 截断摘要；同一报文必得同一指纹。"""
    canonical = json.dumps(message, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    flat = ";".join(f"{k}={message.get(k)}" for k in sorted(message) if k != "data")
    data_bits = ";".join(f"{k}={v}" for k, v in sorted((message.get("data") or {}).items()))
    summary = f"{flat};data:{data_bits}"[:_SUMMARY_LIMIT]
    return digest, summary


def _reject(code: str, message: str, errs: list[dict] | None = None) -> dict:
    return {"code": code, "message": message, "errors": errs or []}


class AlarmInbox:
    """外部报警收件箱：接入、核对、三选一处置；JSON 持久化。"""

    def __init__(self) -> None:
        self.alarms: dict[str, ExternalAlarmRecord] = {}
        #: index_key → 最近受理的 sourceVersion（UPDATE 幂等/冲突判定）。
        self.external_versions: dict[str, int] = {}

    def receive_confirmed(self, message: dict, *, registry: EventRegistry,
                          actor: str = "upstream", received_at: str | None = None) -> dict:
        """Current intake contract: upstream has confirmed the fire; create, never start.

        Called by the trusted upstream adapter, not an unauthenticated HTTP route.
        Legacy inbox/decision methods remain solely for historical data compatibility.
        New reception does not reinterpret or migrate historical pending/rejected rows.
        """
        from copy import deepcopy
        import math
        if not isinstance(message, dict) or message.get("operation") != "CREATE":
            return _reject("INVALID_ARGUMENT", "本建事件入口只接收已确认火情的 CREATE 消息")
        staged = deepcopy(self)
        try:
            result = staged.ingest_message(message, actor=actor, received_at=received_at)
        except (AttributeError, TypeError, ValueError):
            return _reject("INVALID_ARGUMENT", "火情推送字段类型不正确")
        if result["code"] == "DUPLICATE":
            record = self.alarms[result["alarm_id"]]
            if record.event_id and registry.external_index.get(record.index_key) == record.event_id and record.event_id in registry.events:
                return dict(result, event_id=record.event_id)
            return _reject("HISTORICAL_RECORD", "该来源存在历史记录，保留原记录，不自动重新建事件")
        if result["code"] != "PENDING_CONFIRM":
            return result
        record = staged.alarms[result["alarm_id"]]
        if record.index_key in registry.external_index:
            return _reject("SOURCE_CONFLICT", "来源关联记录不完整，保留现有事件，不重复建事件")
        data = message["data"]
        try:
            coordinates = [data.get("lat"), data.get("lon")]
            if (coordinates[0] is None) != (coordinates[1] is None):
                raise ValueError("经纬度必须同时提供")
            if coordinates[0] is not None:
                coordinates = [float(v) for v in coordinates]
                if not all(math.isfinite(v) for v in coordinates) or not (-90 <= coordinates[0] <= 90 and -180 <= coordinates[1] <= 180):
                    raise ValueError("经纬度超出范围")
            candidate = deepcopy(registry)
            from .fire_demo import fire_source_label
            event = candidate.create_event(
                name=record.name, report_no=data.get("reportNo") or record.external_no,
                discovered_at=record.reported_at, source=fire_source_label(record.source_system),
                location=record.location, level=record.level,
                admin_region=data.get("adminRegion") or "", initial_report=record.initial_report,
                key_targets=record.key_targets, created_by=actor,
                lat=coordinates[0], lon=coordinates[1],
            )
        except (ValueError, TypeError, AttributeError) as exc:
            return _reject("INVALID_ARGUMENT", str(exc))
        candidate.external_index[record.index_key] = event.event_id
        record.status = "created"
        record.event_id = event.event_id
        record.history.append(dict(at=record.received_at, action="auto_created", operator=actor,
                                   detail=f"upstream_confirmed event={event.event_id}"))
        registry.__dict__.update(candidate.__dict__)
        self.__dict__.update(staged.__dict__)
        return dict(code="CREATED", message="已确认火情已自动建事件，等待值班人员启动处置",
                    alarm_id=record.alarm_id, event_id=event.event_id)

    # -- 编列 ---------------------------------------------------------------

    def next_alarm_id(self) -> str:
        highest = 0
        for alarm_id in self.alarms:
            if alarm_id.startswith("alm-"):
                try:
                    highest = max(highest, int(alarm_id[4:]))
                except ValueError:
                    continue
        return f"alm-{highest + 1:03d}"

    # -- 接入（FIRE_EVENT 信封口径；端点待契约评审） -------------------------

    def ingest_message(
        self,
        message: dict,
        *,
        actor: str = "external",
        received_at: str | None = None,
    ) -> dict:
        """接入一条 FIRE_EVENT 报警消息 → 一律「待确认」，绝不直接建档。

        CREATE：信封+数据校验通过后落收件箱（PENDING_CONFIRM）；幂等键
        ``sourceSystem|sourceRecordId`` 重复时返回 DUPLICATE 并指向原报警，
        不产生重复对象。UPDATE：仅对待确认报警刷新字段并记版本；对已人工
        处置的报警显式返回 MANUAL_DECIDED——外部更新不再自动生效。
        """
        errors: list[dict] = []
        message_id = (message.get("messageId") or "").strip()
        message_type = (message.get("messageType") or "").strip()
        source_system = (message.get("sourceSystem") or "").strip()
        source_record_id = (message.get("sourceRecordId") or "").strip()
        operation = (message.get("operation") or "").strip().upper()
        data = message.get("data")

        if not message_id:
            errors.append({"field": "messageId", "message": "缺少消息唯一标识"})
        if message_type != "FIRE_EVENT":
            errors.append({"field": "messageType", "message": "本端点只受理 FIRE_EVENT"})
        if not source_system:
            errors.append({"field": "sourceSystem", "message": "缺少来源系统编码"})
        if not source_record_id:
            errors.append({"field": "sourceRecordId", "message": "缺少来源记录标识"})
        if operation not in {"CREATE", "UPDATE"}:
            errors.append({"field": "operation", "message": "只支持 CREATE / UPDATE"})
        if not isinstance(data, dict):
            errors.append({"field": "data", "message": "缺少业务数据对象"})
        if errors:
            return _reject("INVALID_ARGUMENT", "信封校验未通过", errors)

        index_key = f"{source_system}|{source_record_id}"
        now = received_at or utc_now_iso()

        if operation == "CREATE":
            existing = self._by_index(index_key)
            if existing is not None:
                return {
                    "code": "DUPLICATE",
                    "message": "重复消息，已存在处理结果",
                    "alarm_id": existing.alarm_id,
                }
            name = (data.get("name") or "").strip()
            reported_at = (
                data.get("discoveredAt") or message.get("eventTime") or ""
            ).strip()
            location = (data.get("location") or "").strip()
            if not name:
                errors.append({"field": "data.name", "message": "缺少火情名称"})
            if not reported_at:
                errors.append({"field": "data.discoveredAt", "message": "缺少原始报警时间"})
            if not location:
                errors.append({"field": "data.location", "message": "缺少位置或位置未知说明"})
            if errors:
                return _reject("INVALID_ARGUMENT", "FIRE_EVENT 字段校验未通过", errors)

            digest, summary = _digest_and_summary(message)
            record = ExternalAlarmRecord(
                alarm_id=self.next_alarm_id(),
                source_system=source_system,
                external_no=source_record_id,
                received_at=now,
                reported_at=reported_at,
                name=name,
                location=location,
                level=(data.get("level") or data.get("alarmLevel") or "").strip(),
                source_status=str(data.get("status") or ""),
                initial_report=(data.get("initialReport") or "").strip(),
                key_targets=tuple(data.get("keyTargets") or ()),
                raw_digest=digest,
                raw_summary=summary,
                suggested_event_id=(data.get("eventId") or "").strip(),
                remark=(data.get("remark") or "").strip(),
            )
            record.history.append(
                {"at": now, "action": "received", "operator": actor, "detail": ""}
            )
            self.alarms[record.alarm_id] = record
            try:
                version = int(message.get("sourceVersion") or 1)
            except (TypeError, ValueError):
                version = 1
            self.external_versions[index_key] = version
            return {
                "code": "PENDING_CONFIRM",
                "message": "报警已进入待确认收件箱，等待人工核对建档",
                "alarm_id": record.alarm_id,
            }

        # -- UPDATE ---------------------------------------------------------
        record = self._by_index(index_key)
        if record is None:
            return _reject(
                "INVALID_ARGUMENT",
                "更新目标报警不存在",
                [{"field": "sourceRecordId", "message": "NOT_FOUND"}],
            )
        try:
            incoming_version = int(message.get("sourceVersion") or 0)
        except (TypeError, ValueError):
            incoming_version = 0
        stored_version = self.external_versions.get(index_key)
        if stored_version is not None and incoming_version <= stored_version:
            return _reject(
                "VERSION_CONFLICT",
                "基础版本已变化，请重新查询合并",
                [{"field": "sourceVersion", "message": f"当前已受理 v{stored_version}"}],
            )
        if record.status != "pending":
            return {
                "code": "MANUAL_DECIDED",
                "message": "该报警已人工处理，外部更新不再自动生效，请人工核对",
                "alarm_id": record.alarm_id,
                "event_id": record.event_id,
            }
        # 只刷新描述性字段；状态原文只存不解释，绝不改写收件箱状态机。
        record.name = (data.get("name") or "").strip() or record.name
        record.location = (data.get("location") or "").strip() or record.location
        record.level = (data.get("level") or data.get("alarmLevel") or "").strip() or record.level
        record.source_status = str(data.get("status") or "") or record.source_status
        record.initial_report = (data.get("initialReport") or "").strip() or record.initial_report
        if data.get("keyTargets"):
            record.key_targets = tuple(data.get("keyTargets"))
        record.history.append(
            {"at": now, "action": "external_update", "operator": actor,
             "detail": f"v{incoming_version}"}
        )
        self.external_versions[index_key] = incoming_version
        return {
            "code": "PENDING_CONFIRM",
            "message": "报警字段已刷新，仍处待确认状态",
            "alarm_id": record.alarm_id,
        }

    def _by_index(self, index_key: str) -> ExternalAlarmRecord | None:
        for record in self.alarms.values():
            if record.index_key == index_key:
                return record
        return None

    # -- 三选一处置（面板层调用；审计链由面板写入） ---------------------------

    def confirm(
        self,
        alarm_id: str,
        *,
        registry: EventRegistry,
        report_no: str,
        name: str = "",
        location: str = "",
        operator: str = "",
    ) -> FireEventRecord:
        """确认建档：复用手动登记同一 ``registry.create_event``（同构事件）。"""
        record = self._require(alarm_id)
        if record.status != "pending":
            raise ValueError(f"报警状态为「{record.status_word()}」，不能确认建档。")
        report_no = (report_no or "").strip()
        if not report_no:
            # fail-closed：报告编号是人工登记责任字段，外部报警不自动编造。
            raise ValueError("报告编号不能为空：外部报警不自动生成编号，请人工填写。")
        event = registry.create_event(
            name=(name or "").strip() or record.name,
            report_no=report_no,
            discovered_at=record.reported_at or record.received_at,
            source=f"外部报警·{record.source_system}",
            location=(location or "").strip() or record.location,
            level=record.level,
            initial_report=record.initial_report,
            key_targets=record.key_targets,
            created_by=operator or "external-alarm",
        )
        # 关联双写：注册表幂等索引指向正式事件（后续 UPDATE 可定位），报警
        # 记录保留事件编号；事件编号始终由本系统 fire-{n} 编列。
        registry.external_index[record.index_key] = event.event_id
        record.status = "confirmed"
        record.event_id = event.event_id
        record.history.append(
            {"at": utc_now_iso(), "action": "confirmed", "operator": operator,
             "detail": f"event={event.event_id} report_no={report_no}"}
        )
        return event

    def reject(self, alarm_id: str, *, reason: str, operator: str = "") -> ExternalAlarmRecord:
        """拒绝报警：原因必填；不建任何事件。"""
        record = self._require(alarm_id)
        if record.status != "pending":
            raise ValueError(f"报警状态为「{record.status_word()}」，不能拒绝。")
        reason = (reason or "").strip()
        if not reason:
            raise ValueError("拒绝必须填写原因。")
        record.status = "rejected"
        record.reject_reason = reason
        record.history.append(
            {"at": utc_now_iso(), "action": "rejected", "operator": operator,
             "detail": reason}
        )
        return record

    def link_duplicate(
        self, alarm_id: str, *, registry: EventRegistry, event_id: str, operator: str = ""
    ) -> ExternalAlarmRecord:
        """重复报警：关联到已有正式事件，不重复建档。"""
        record = self._require(alarm_id)
        if record.status != "pending":
            raise ValueError(f"报警状态为「{record.status_word()}」，不能执行关联。")
        event_id = (event_id or "").strip()
        if event_id not in registry.events:
            raise ValueError(f"要关联的事件不存在：{event_id or '（空）'}")
        record.status = "duplicate"
        record.event_id = event_id
        record.history.append(
            {"at": utc_now_iso(), "action": "linked", "operator": operator,
             "detail": f"event={event_id}"}
        )
        return record

    def _require(self, alarm_id: str) -> ExternalAlarmRecord:
        record = self.alarms.get((alarm_id or "").strip())
        if record is None:
            raise ValueError(f"未知报警编号：{alarm_id or '（空）'}")
        return record

    # -- 持久化 -------------------------------------------------------------

    def save(self, path: str | Path) -> Path:
        from .durable_storage import before_write
        before_write(path)
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "alarms": [r.to_dict() for r in self.alarms.values()],
            "external_versions": dict(self.external_versions),
        }
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)
        return path

    @classmethod
    def load(cls, path: str | Path) -> "AlarmInbox":
        path = Path(path)
        inbox = cls()
        if not path.exists():
            return inbox
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("alarms") or []:
            record = ExternalAlarmRecord.from_dict(item)
            inbox.alarms[record.alarm_id] = record
        inbox.external_versions = dict(payload.get("external_versions") or {})
        return inbox
