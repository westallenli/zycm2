"""Fire-event domain layer (P16 T1) — 多火情管理 · 成员账号 · 事件二维码 · 刷码匹配.

对齐口径（实现逐条锚定，不得偏离）：

- 页面设计定版 ``01_登录页_V0.3.md``：账号体系（§2）、火情事件管理与生命周期（§3）、
  扫码入岗流程与幂等（§4）、二维码规则（§5）、身份匹配结果表（§6）；
- 《增强版PRD》§13.3 固定人员扫码自动入岗六步：提前配置名册 → 个人账号登录 →
  扫码只识别事件 → 提示「工作组—岗位」本人确认 → 写入岗记录 → 重复扫描幂等、
  异常转人工且不允许自行改岗；
- 《对外数据接口规范》V0.1：FIRE_EVENT 消息最低字段与创建/升级/关闭操作（§4），
  ORG_MEMBER_ONBOARDING 的业务错误语义（§11：不在名册/码不属于事件/冲突事件
  返回业务错误，重复消息按原幂等键返回原结果）。

纪律（本模块是纯领域层，无 HTTP、无 HTML）：

- 一切无法验证的状态一律 fail-closed：转人工或显式业务错误，绝不静默放行；
- 入岗落账仍走工作区唯一写入口 ``scan_check_in``（由调用方以回调注入），
  本模块只做授权判定与匹配，不绕过任何既有权力门；
- 长期人员库在本注册表层维护（跨事件底账）；事件任务空间的 workspace 库
  在确认入岗时由桥接函数补齐，保持「长期档案 ≠ 事件快照」分离。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timedelta
from pathlib import Path

from .models import PersonRecord
from .org_model import (
    NODE_INDEX,
    POSITION_SLOTS,
    SLOT_INDEX,
    normalize_phone,
    slot_domain_parent,
)

# ---------------------------------------------------------------------------
# 定版常量（界面必须照实标注，不得伪装）


#: 成员初始口令（01 号 §2：统一 123456，界面标注「演示初始口令」，正式版接统一认证替换）。
DEMO_INITIAL_PASSWORD = "123456"
#: 口令哈希迭代次数（pbkdf2_hmac-sha256）。
PBKDF2_ITERATIONS = 100_000
#: 连续失败锁定阈值（01 号 §8「账号锁定」状态）。
LOCK_THRESHOLD = 5
#: 二维码默认有效期（01 号 §5：默认 72 小时，到期重签，不自动续期）。
DEFAULT_QR_VALID_HOURS = 72

#: 事件生命周期（01 号 §3.3）：登记中 → 进行中 → 已升级 → 已关闭（登记中可直接关闭）。
STATUS_WORDS = {
    "registered": "登记中",
    "active": "进行中",
    "upgraded": "已升级",
    "closed": "已关闭",
}
#: 允许刷码入岗 / 签发二维码的状态（仅 进行中／已升级）。
ONBOARDABLE_STATUSES = frozenset({"active", "upgraded"})


# ---------------------------------------------------------------------------
# 时间工具（存 ISO 字符串保留来源，比较时解析；解析失败显式报错）


def parse_time(raw: str) -> datetime:
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"时间格式不合法：{raw!r}") from exc


def utc_now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# 领域记录（frozen：变更一律整体替换，保持可审计）


@dataclass(frozen=True)
class FireEventRecord:
    """火情事件（01 号 §3.2 字段表；外部接入字段同构）。"""

    event_id: str            # fire-{n}（页面录入）或外部系统 eventId
    name: str
    report_no: str
    discovered_at: str       # ISO 本地时间
    source: str              # 页面录入 / 接入系统名称
    location: str            # 行政位置描述（接口允许「位置未知」说明）
    status: str = "registered"
    level: str = ""          # 选填：等级
    admin_region: str = ""   # 选填：行政区
    initial_report: str = ""  # 选填：初报
    key_targets: tuple[str, ...] = ()  # 选填：重点目标
    mission_space_id: str = ""  # 前指任务空间标识 ms-{eventId}
    snapshot_path: str = ""     # 事件任务空间快照文件（启动处置后落地）
    created_by: str = ""
    created_at: str = ""
    updated_at: str = ""
    lat: float | None = None    # 事件坐标（03 号 §6 真实底图定位；选填）
    lon: float | None = None

    def status_word(self) -> str:
        return STATUS_WORDS.get(self.status, self.status)


@dataclass(frozen=True)
class QRCodeObject:
    """事件二维码（01 号 §5.1）：一枚码只绑一个 eventId，不含岗位不含身份。"""

    qr_id: str               # qr-{n}，按事件空间编列
    event_id: str
    issued_by: str
    issued_at: str
    valid_hours: int = DEFAULT_QR_VALID_HOURS
    revoked_by: str = ""
    revoked_at: str = ""


@dataclass(frozen=True)
class RosterEntry:
    """本事件名册条目（01 号 §4.1）：提前维护的授权清单，区别于花名册。"""

    phone: str
    pos_code: str | None     # None ＝ 本职未能解析到编制槽位，待指挥员指定
    resolved_from_home: bool  # 是否由本职（home_position）自动解析的默认建议
    added_by: str = ""
    added_at: str = ""


@dataclass
class EventRoster:
    """本事件名册；每次变更 roster_version 递增（ORG_MEMBER_ONBOARDING 携带版本）。"""

    event_id: str
    entries: dict[str, RosterEntry] = field(default_factory=dict)
    roster_version: int = 1


@dataclass
class MemberCredential:
    """成员凭证（01 号 §2）：账号＝长期人员库手机号；口令只存哈希，永不留明文。"""

    phone: str
    password_hash: str = ""
    failed_count: int = 0
    locked: bool = False


@dataclass(frozen=True)
class CheckinOutcome:
    """刷码匹配结果（01 号 §6 身份匹配结果表的程序化表达）。"""

    kind: str
    message: str
    event_id: str = ""
    group_name: str = ""     # 「即将进入：某工作组—某岗位」的工作组
    position_name: str = ""
    pos_code: str = ""


#: 结果种类常量（测试与面板路由共用，避免散落字符串）。
OUTCOME_CONFIRM = "confirm_pending"        # 匹配成功，待本人确认
OUTCOME_IDEMPOTENT = "already_onboarded"   # 已在岗（含会话丢失）：幂等回空间
OUTCOME_NOT_IN_LIBRARY = "account_unknown"
OUTCOME_EVENT_NOT_OPEN = "event_not_open"
OUTCOME_CROSS_CONFLICT = "cross_event_conflict"
OUTCOME_NOT_ON_ROSTER = "not_on_roster"
OUTCOME_POSITION_UNRESOLVED = "position_unresolvable"
OUTCOME_QR_UNKNOWN = "qr_invalid"
OUTCOME_QR_REVOKED = "qr_revoked"
OUTCOME_QR_EXPIRED = "qr_expired"


# ---------------------------------------------------------------------------
# 本职岗位 → 编制槽位的确定性解析（默认建议，可人工改）


def resolve_home_slot(home_position: str) -> str | None:
    """按本职名称解析编制槽位：精确命中优先，其后 ≥2 字词重叠，均未中返回 None。

    解析只产出「默认建议」；名册条目一旦保存 pos_code 即以保存值为准。
    """
    text = (home_position or "").strip()
    if not text:
        return None
    if text in SLOT_INDEX:  # 直接给了 pos_code
        return text
    for slot in POSITION_SLOTS:  # 岗位全名精确命中
        if slot.name == text:
            return slot.pos_code
    tokens = [text[i:i + 2] for i in range(len(text) - 1)] or ([text] if text else [])
    best: tuple[int, str] | None = None
    for slot in POSITION_SLOTS:
        overlap = sum(1 for token in tokens if token in slot.name)
        if overlap >= 2 and (best is None or overlap > best[0]):
            best = (overlap, slot.pos_code)
    return best[1] if best else None


def group_of_slot(pos_code: str) -> str:
    """岗位槽位所属工作组名（「即将进入：某工作组—某岗位」前半句）。"""
    return NODE_INDEX[slot_domain_parent(pos_code)].name


# ---------------------------------------------------------------------------
# 口令哈希（fail-closed：比较用 hmac.compare_digest，失败不泄露原因细节）


def _hash_password(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt_hex, digest_hex = stored.split("$")
    except ValueError:
        return False
    if scheme != "pbkdf2_sha256":
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations)
    )
    return hmac.compare_digest(digest.hex(), digest_hex)


# ---------------------------------------------------------------------------
# 注册表：多事件 + 名册 + 二维码 + 成员凭证 + 长期人员库


def _empty_roster_factory(event_id: str) -> EventRoster:
    return EventRoster(event_id=event_id)


class EventRegistry:
    """多火情注册表（JSON 持久化）：每事件一个独立任务空间，一人同一时间只绑一个事件。

    - ``events``：eventId → FireEventRecord；
    - ``rosters``：eventId → EventRoster（预配置授权清单）；
    - ``qrs``：qr_id → QRCodeObject（过期在查询时现算）；
    - ``credentials``：phone → MemberCredential（口令哈希 + 锁定态）；
    - ``library``：长期人员库（跨事件底账，phone → PersonRecord）。
    """

    def __init__(self) -> None:
        self.events: dict[str, FireEventRecord] = {}
        # Removed demonstration cases remain evidence for historical ledger reads.
        # They are never selectable events or sources of current appointments.
        self.archived_test_events: dict[str, FireEventRecord] = {}
        self.rosters: dict[str, EventRoster] = {}
        self.qrs: dict[str, QRCodeObject] = {}
        self.credentials: dict[str, MemberCredential] = {}
        self.library: dict[str, PersonRecord] = {}
        #: 授权维护人员（doc 05 §9）：长期库手机号集合，复盘页查看/问询/建议。
        self.maintenance_phones: set[str] = set()
        #: (sourceSystem, sourceRecordId) → eventId：外部消息幂等索引。
        self.external_index: dict[str, str] = {}
        #: (sourceSystem, sourceRecordId) → 最近受理的 sourceVersion。
        self.external_versions: dict[str, int] = {}
        self._event_seq = 0
        self._qr_seq = 0

    # -- 事件 ---------------------------------------------------------------

    def next_event_id(self) -> str:
        self._event_seq += 1
        return f"fire-{self._event_seq:03d}"

    def archive_test_event(self, event_id: str) -> FireEventRecord:
        """Narrow maintenance operation for the explicitly retired transfer fixture."""
        record = self.events.get(event_id)
        if record is None:
            if event_id in self.archived_test_events:
                return self.archived_test_events[event_id]
            raise ValueError('未知测试事件。')
        if (record.name, record.report_no, record.source) != (
                '模拟调拨事件', 'P08-TRANSFER', '人工模拟登记'):
            raise ValueError('只能移除已明确指定的旧调拨测试样例。')
        self.archived_test_events[event_id] = self.events.pop(event_id)
        return record

    def next_qr_id(self) -> str:
        self._qr_seq += 1
        return f"qr-{self._qr_seq:03d}"

    def create_event(
        self,
        *,
        name: str,
        report_no: str,
        discovered_at: str,
        source: str,
        location: str,
        level: str = "",
        admin_region: str = "",
        initial_report: str = "",
        key_targets: tuple[str, ...] = (),
        event_id: str | None = None,
        created_by: str = "",
        created_at: str | None = None,
        lat: float | None = None,
        lon: float | None = None,
    ) -> FireEventRecord:
        """录入新火情，初始态固定为「登记中」（01 号 §3.3）。"""
        name = (name or "").strip()
        report_no = (report_no or "").strip()
        location = (location or "").strip()
        if not name:
            raise ValueError("火情名称不能为空。")
        if not report_no:
            raise ValueError("报告编号不能为空。")
        if not location:
            raise ValueError("行政位置描述不能为空。")
        parse_time(discovered_at)  # 显式校验，坏时间不入库
        if event_id is None:
            event_id = self.next_event_id()
        if event_id in self.events or event_id in self.archived_test_events:
            raise ValueError(f"事件编号已存在：{event_id}")
        now = created_at or utc_now_iso()
        record = FireEventRecord(
            event_id=event_id,
            name=name,
            report_no=report_no,
            discovered_at=str(discovered_at),
            source=(source or "").strip() or "页面录入",
            location=location,
            status="registered",
            level=(level or "").strip(),
            admin_region=(admin_region or "").strip(),
            initial_report=(initial_report or "").strip(),
            key_targets=tuple(key_targets),
            mission_space_id=f"ms-{event_id}",
            created_by=created_by,
            created_at=now,
            updated_at=now,
            lat=lat,
            lon=lon,
        )
        self.events[event_id] = record
        self.rosters[event_id] = _empty_roster_factory(event_id)
        return record

    def transition(self, event_id: str, action: str, *, operator: str = "") -> FireEventRecord:
        """生命周期流转：start（启动处置）/ upgrade（升级）/ close（关闭归档）。"""
        record = self._require_event(event_id)
        now = utc_now_iso()
        if action == "start":
            if record.status != "registered":
                raise ValueError(
                    f"只有「登记中」事件可启动处置（当前：{record.status_word()}）。"
                )
            new_status = "active"
        elif action == "upgrade":
            if record.status != "active":
                raise ValueError(
                    f"只有「进行中」事件可标记升级（当前：{record.status_word()}）。"
                )
            new_status = "upgraded"
        elif action == "close":
            if record.status == "closed":
                raise ValueError("事件已关闭归档，为终态不可再变更。")
            new_status = "closed"
        else:
            raise ValueError(f"未知生命周期动作：{action}")
        updated = FireEventRecord(
            **{
                **asdict(record),
                "status": new_status,
                "updated_at": now,
            }
        )
        self.events[event_id] = updated
        return updated

    def set_snapshot_path(self, event_id: str, snapshot_path: str) -> FireEventRecord:
        """登记事件任务空间快照文件（启动处置建空间后调用）。"""
        record = self._require_event(event_id)
        updated = FireEventRecord(**{**asdict(record), "snapshot_path": str(snapshot_path)})
        self.events[event_id] = updated
        return updated

    def _require_event(self, event_id: str) -> FireEventRecord:
        record = self.events.get(event_id)
        if record is None:
            raise KeyError(f"未知事件：{event_id}")
        return record

    # -- 二维码（01 号 §5）---------------------------------------------------

    def issue_qr(
        self,
        event_id: str,
        *,
        issued_by: str,
        issued_at: str | None = None,
        valid_hours: int = DEFAULT_QR_VALID_HOURS,
    ) -> QRCodeObject:
        """签发事件二维码：仅「进行中／已升级」可签发；签发人/时间落审计。"""
        record = self._require_event(event_id)
        if record.status not in ONBOARDABLE_STATUSES:
            raise ValueError(
                f"只有「进行中／已升级」事件可签发二维码（当前：{record.status_word()}）。"
            )
        if valid_hours <= 0:
            raise ValueError("二维码有效期必须为正小时数。")
        stamp = issued_at or utc_now_iso()
        parse_time(stamp)
        qr = QRCodeObject(
            qr_id=self.next_qr_id(),
            event_id=event_id,
            issued_by=issued_by,
            issued_at=stamp,
            valid_hours=int(valid_hours),
        )
        self.qrs[qr.qr_id] = qr
        return qr

    def revoke_qr(self, qr_id: str, *, revoked_by: str) -> QRCodeObject:
        qr = self.qrs.get(qr_id)
        if qr is None:
            raise KeyError(f"未知二维码：{qr_id}")
        if qr.revoked_at:
            raise ValueError(f"二维码 {qr_id} 已撤销，不可重复撤销。")
        revoked = QRCodeObject(
            **{**asdict(qr), "revoked_by": revoked_by, "revoked_at": utc_now_iso()}
        )
        self.qrs[qr_id] = revoked
        return revoked

    @staticmethod
    def qr_status(qr: QRCodeObject, *, now: str | None = None) -> str:
        """二维码状态现算（01 号 §5.1）：有效 / 已过期 / 已撤销。"""
        if qr.revoked_at:
            return "已撤销"
        moment = parse_time(now) if now else datetime.now().astimezone()
        expiry = parse_time(qr.issued_at) + timedelta(hours=qr.valid_hours)
        if moment >= expiry:
            return "已过期"
        return "有效"

    @staticmethod
    def qr_expiry_iso(qr: QRCodeObject) -> str:
        """二维码有效期止（ISO 字符串，展示用；P16 T2 面板层禁止直接依赖 datetime）。"""
        return (
            parse_time(qr.issued_at) + timedelta(hours=qr.valid_hours)
        ).isoformat(timespec="seconds")

    # -- 长期人员库（跨事件底账，规则与 workspace.import_person_library 同构）--

    def import_person_library(self, records) -> int:
        normalized: dict[str, PersonRecord] = {}
        for record in records:
            phone = normalize_phone(record.phone)
            if not record.name.strip():
                raise ValueError(f"人员姓名不能为空（phone={phone}）")
            normalized[phone] = PersonRecord(
                name=record.name.strip(),
                phone=phone,
                unit=record.unit.strip(),
                home_position=record.home_position.strip(),
                specialty=record.specialty.strip(),
                is_leader=bool(record.is_leader),
            )
        if not normalized:
            raise ValueError("人员库导入批次为空。")
        self.library.update(normalized)
        return len(normalized)

    # -- 本事件名册（01 号 §4.1）---------------------------------------------

    def _require_open_for_config(self, event_id: str) -> EventRoster:
        self._require_event(event_id)
        return self.rosters.setdefault(event_id, _empty_roster_factory(event_id))

    def add_roster_entry(
        self,
        event_id: str,
        phone: str,
        *,
        pos_code: str | None = None,
        added_by: str = "",
    ) -> RosterEntry:
        """把长期人员库成员加入本事件名册；缺省岗位用本职解析建议，可传 pos_code 改派。"""
        roster = self._require_open_for_config(event_id)
        phone_n = normalize_phone(phone)
        person = self.library.get(phone_n)
        if person is None:
            raise KeyError("该手机号不在长期人员库，请先补录人员档案。")
        if pos_code is not None and pos_code not in SLOT_INDEX:
            raise KeyError(f"未知岗位槽位：{pos_code}")
        resolved_from_home = False
        if pos_code is None:
            pos_code = resolve_home_slot(person.home_position)
            resolved_from_home = pos_code is not None
        entry = RosterEntry(
            phone=phone_n,
            pos_code=pos_code,
            resolved_from_home=resolved_from_home,
            added_by=added_by,
            added_at=utc_now_iso(),
        )
        roster.entries[phone_n] = entry
        roster.roster_version += 1
        return entry

    def remove_roster_entry(self, event_id: str, phone: str) -> None:
        roster = self._require_open_for_config(event_id)
        phone_n = normalize_phone(phone)
        if phone_n not in roster.entries:
            raise KeyError("该人员不在本事件名册。")
        del roster.entries[phone_n]
        roster.roster_version += 1

    def populate_roster_all(self, event_id: str, *, added_by: str = "") -> int:
        """「全部加入」：长期人员库全员按本职建议加入名册，返回名册人数。"""
        roster = self._require_open_for_config(event_id)
        for phone in sorted(self.library):
            person = self.library[phone]
            pos_code = resolve_home_slot(person.home_position)
            roster.entries[phone] = RosterEntry(
                phone=phone,
                pos_code=pos_code,
                resolved_from_home=pos_code is not None,
                added_by=added_by,
                added_at=utc_now_iso(),
            )
        roster.roster_version += 1
        return len(roster.entries)

    def assign_roster_position(self, event_id: str, phone: str, pos_code: str) -> RosterEntry:
        """指挥员为本职未解析的名册条目人工指派槽位（不允许成员自行改岗）。"""
        roster = self._require_open_for_config(event_id)
        phone_n = normalize_phone(phone)
        entry = roster.entries.get(phone_n)
        if entry is None:
            raise KeyError("该人员不在本事件名册。")
        if pos_code not in SLOT_INDEX:
            raise KeyError(f"未知岗位槽位：{pos_code}")
        updated = RosterEntry(
            **{**asdict(entry), "pos_code": pos_code, "resolved_from_home": False}
        )
        roster.entries[phone_n] = updated
        roster.roster_version += 1
        return updated

    # -- 成员账号（01 号 §2）--------------------------------------------------

    def ensure_account(self, phone: str, *, initial_password: str = DEMO_INITIAL_PASSWORD) -> MemberCredential:
        """为长期人员库成员开通个人账号（初始口令统一 123456，界面须标注演示性质）。"""
        phone_n = normalize_phone(phone)
        if phone_n not in self.library:
            raise KeyError("账号不在长期人员库，转人工补录，不静默放行。")
        credential = self.credentials.get(phone_n)
        if credential is None:
            credential = MemberCredential(
                phone=phone_n, password_hash=_hash_password(initial_password)
            )
            self.credentials[phone_n] = credential
        return credential

    def verify_member_login(self, raw_phone: str, password: str) -> str:
        """成员登录核验（fail-closed）：返回规范化手机号；一切异常显式拒绝。"""
        try:
            phone_n = normalize_phone(raw_phone)
        except ValueError as exc:
            raise PermissionError(f"账号格式不合法：{exc}") from exc
        if phone_n not in self.library:
            raise PermissionError("账号不在长期人员库，转人工补录。")
        credential = self.credentials.get(phone_n)
        if credential is None:
            credential = self.ensure_account(phone_n)
        if credential.locked:
            raise PermissionError("账号已锁定，请联系指挥部解除后再试。")
        if not _verify_password(password or "", credential.password_hash):
            credential.failed_count += 1
            if credential.failed_count >= LOCK_THRESHOLD:
                credential.locked = True
            remaining = max(LOCK_THRESHOLD - credential.failed_count, 0)
            detail = "，账号已锁定。" if credential.locked else f"，剩余 {remaining} 次机会。"
            raise PermissionError("账号或口令不正确" + detail)
        credential.failed_count = 0
        return phone_n

    def unlock_account(self, raw_phone: str, *, operator: str = "") -> MemberCredential:
        """指挥员解除账号锁定（人工动作，落调用方审计）。"""
        phone_n = normalize_phone(raw_phone)
        credential = self.credentials.get(phone_n)
        if credential is None:
            raise KeyError("该账号尚未开通。")
        credential.locked = False
        credential.failed_count = 0
        return credential

    # -- 持久化（JSON 原子写）--------------------------------------------------

    def to_dict(self) -> dict:
        payload = {
            "version": 1,
            "counters": {"event_seq": self._event_seq, "qr_seq": self._qr_seq},
            "events": {eid: asdict(rec) for eid, rec in self.events.items()},
            "rosters": {
                eid: {
                    "event_id": r.event_id,
                    "roster_version": r.roster_version,
                    "entries": {p: asdict(e) for p, e in r.entries.items()},
                }
                for eid, r in self.rosters.items()
            },
            "qrs": {qid: asdict(qr) for qid, qr in self.qrs.items()},
            "credentials": {p: asdict(c) for p, c in self.credentials.items()},
            "library": {p: asdict(rec) for p, rec in self.library.items()},
            "external_index": dict(self.external_index),
            "external_versions": dict(self.external_versions),
            "maintenance_phones": sorted(self.maintenance_phones),
        }
        if self.archived_test_events:
            payload['archived_test_events'] = {
                eid: asdict(record) for eid, record in self.archived_test_events.items()}
        return payload

    def save(self, path: str | Path) -> Path:
        from .durable_storage import before_write
        before_write(path)
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path: str | Path) -> "EventRegistry":
        target = Path(path)
        registry = cls()
        if not target.exists():
            return registry
        payload = json.loads(target.read_text(encoding="utf-8"))
        registry._event_seq = payload["counters"]["event_seq"]
        registry._qr_seq = payload["counters"]["qr_seq"]
        for eid, raw in payload["events"].items():
            raw["key_targets"] = tuple(raw.get("key_targets") or ())
            registry.events[eid] = FireEventRecord(**{
                f.name: raw[f.name] for f in fields(FireEventRecord) if f.name in raw
            })
        for eid, raw in payload.get('archived_test_events', {}).items():
            if eid in registry.events or raw.get('event_id') != eid:
                raise ValueError('测试归档与运行事件归属冲突。')
            raw['key_targets'] = tuple(raw.get('key_targets') or ())
            registry.archived_test_events[eid] = FireEventRecord(**{
                f.name: raw[f.name] for f in fields(FireEventRecord) if f.name in raw})
        for eid, raw in payload["rosters"].items():
            roster = EventRoster(
                event_id=raw["event_id"], roster_version=raw["roster_version"]
            )
            for phone, entry_raw in raw["entries"].items():
                roster.entries[phone] = RosterEntry(**entry_raw)
            registry.rosters[eid] = roster
        for qid, raw in payload["qrs"].items():
            registry.qrs[qid] = QRCodeObject(**raw)
        for phone, raw in payload["credentials"].items():
            registry.credentials[phone] = MemberCredential(**raw)
        for phone, raw in payload["library"].items():
            registry.library[phone] = PersonRecord(**raw)
        registry.external_index = dict(payload.get("external_index", {}))
        registry.external_versions = dict(payload.get("external_versions", {}))
        registry.maintenance_phones = set(payload.get("maintenance_phones") or ())
        return registry


# ---------------------------------------------------------------------------
# FIRE_EVENT 外部接入（接口规范 §2/§4/§5；HTTP 端点本期标注「待对接」）
# P17-P1（2026-08-25）口径变更：外部报警一律先进「待确认」收件箱，由指挥员
# 人工核对后确认建档／拒绝／重复关联——外部消息不再直通建档或状态流转
# （DEVELOPMENT_PLAN_PAGES_V1 §4.1.B）。旧直通实现移入 alarm_inbox 域。


def ingest_fire_event(inbox, registry, message, *, actor="external"):
    """按 FIRE_EVENT 契约接入一条外部报警消息 → 一律「待确认」，绝不直接建档。

    兼容门面：真实逻辑在 :class:`panda_brain.alarm_inbox.AlarmInbox`。
    ``inbox`` 为收件箱实例；响应码语义：

    - ``PENDING_CONFIRM``：已入收件箱等待人工核对（CREATE 成功 / UPDATE 刷新）；
    - ``DUPLICATE``：同一 ``sourceSystem|sourceRecordId`` 重复消息，指向原报警；
    - ``INVALID_ARGUMENT``：信封或必填字段校验未通过，逐条列出；
    - ``VERSION_CONFLICT``：UPDATE 版本落后；
    - ``MANUAL_DECIDED``：报警已被人工处置，外部更新不再自动生效。
    """
    return inbox.ingest_message(message, actor=actor)




# ---------------------------------------------------------------------------
# 刷码匹配与确认入岗（01 号 §4 流程 + §6 结果表；PRD §13.3 第 3~6 步）


def resolve_checkin(
    registry: EventRegistry,
    *,
    qr_input: str,
    phone: str,
    session_bound_event_id: str = "",
    now: str | None = None,
) -> CheckinOutcome:
    """刷码匹配：纯判定函数，产出「即将进入：某工作组—某岗位」前的全部分支结论。

    判定次序：二维码有效性 → 账号存在性 → 事件开放性 → 幂等/跨事件冲突 →
    名册 → 岗位解析。任何分支都不产生副作用；真正写记录发生在
    :func:`confirm_checkin`（本人确认之后）。
    """
    qr_id = (qr_input or "").strip()
    qr = registry.qrs.get(qr_id)
    if qr is None:
        return CheckinOutcome(
            OUTCOME_QR_UNKNOWN, "二维码无效：请核对编号或向指挥部获取新码。"
        )
    status_word = EventRegistry.qr_status(qr, now=now)
    if status_word == "已撤销":
        return CheckinOutcome(
            OUTCOME_QR_REVOKED, "二维码已撤销：请向指挥部获取新码。", event_id=qr.event_id
        )
    if status_word == "已过期":
        return CheckinOutcome(
            OUTCOME_QR_EXPIRED, "二维码已过期：请向指挥部获取重签新码。", event_id=qr.event_id
        )
    event = registry.events[qr.event_id]

    try:
        phone_n = normalize_phone(phone)
    except ValueError as exc:
        raise PermissionError(f"会话身份异常：{exc}") from exc
    if phone_n not in registry.library:
        return CheckinOutcome(
            OUTCOME_NOT_IN_LIBRARY,
            "账号不在长期人员库：转人工补录，不允许自行放行。",
            event_id=event.event_id,
        )

    if event.status not in ONBOARDABLE_STATUSES:
        word = event.status_word()
        reason = (
            "事件尚在登记中，未启动处置，暂不可刷码入岗。"
            if event.status == "registered"
            else "事件已关闭归档，历史只读，不再接受入岗。"
        )
        return CheckinOutcome(
            OUTCOME_EVENT_NOT_OPEN, f"{word}：{reason}", event_id=event.event_id
        )

    if session_bound_event_id == event.event_id:
        return CheckinOutcome(
            OUTCOME_IDEMPOTENT,
            "你已在岗：直接返回本事件工作空间（幂等，不重复记录到场）。",
            event_id=event.event_id,
        )
    if session_bound_event_id:
        bound = registry.events.get(session_bound_event_id)
        bound_word = bound.status_word() if bound else "其他事件"
        return CheckinOutcome(
            OUTCOME_CROSS_CONFLICT,
            f"你已绑定其他事件（{bound_word}）：跨事件冲突转人工处理，不允许自行改岗。",
            event_id=event.event_id,
        )

    roster = registry.rosters.get(event.event_id)
    entry = roster.entries.get(phone_n) if roster else None
    if entry is None:
        return CheckinOutcome(
            OUTCOME_NOT_ON_ROSTER,
            "你不在此事件名册：身份异常待处理，转人工核实。",
            event_id=event.event_id,
        )
    if entry.pos_code is None or entry.pos_code not in SLOT_INDEX:
        return CheckinOutcome(
            OUTCOME_POSITION_UNRESOLVED,
            "本职岗位无法解析到编制槽位：请指挥部在名册中为你指定岗位。",
            event_id=event.event_id,
        )
    slot = SLOT_INDEX[entry.pos_code]
    return CheckinOutcome(
        OUTCOME_CONFIRM,
        f"即将进入：{group_of_slot(entry.pos_code)}—{slot.name}",
        event_id=event.event_id,
        group_name=group_of_slot(entry.pos_code),
        position_name=slot.name,
        pos_code=entry.pos_code,
    )


def confirm_checkin(
    registry: EventRegistry,
    outcome: CheckinOutcome,
    *,
    phone: str,
    bind_fn,
    operator: str = "",
) -> dict:
    """本人确认后的入岗落账：经注入回调走工作区唯一门 ``scan_check_in``。

    fail-closed 复核：只接受 ``confirm_pending`` 结论；名册岗位与结论不一致、
    事件已不开放一律拒绝。返回到场信息；会话绑定（bound event + 到场时间）
    由调用方的会话层完成，本模块不持有会话。
    """
    if outcome.kind != OUTCOME_CONFIRM:
        raise PermissionError("只有「匹配成功待确认」的结论可以确认入岗。")
    event = registry._require_event(outcome.event_id)
    if event.status not in ONBOARDABLE_STATUSES:
        raise PermissionError(f"事件当前为「{event.status_word()}」，不可入岗。")
    phone_n = normalize_phone(phone)
    roster = registry.rosters[outcome.event_id]
    entry = roster.entries.get(phone_n)
    if entry is None or entry.pos_code != outcome.pos_code:
        raise PermissionError("名册已变更，与确认内容不一致：请重新刷码核验。")
    person = registry.library.get(phone_n)
    if person is None:
        raise PermissionError("账号已不在长期人员库：转人工处理。")
    binding = bind_fn(pos_code=outcome.pos_code, phone=phone_n, operator=operator or "member-onboarding")
    return {
        "event_id": outcome.event_id,
        "mission_space_id": event.mission_space_id,
        "binding_id": binding.id,
        "position": outcome.position_name,
        "group": outcome.group_name,
        "arrived_at": utc_now_iso(),
        "roster_version": roster.roster_version,
    }


def ensure_person_in_workspace(registry: EventRegistry, workspace, phone: str) -> None:
    """把长期库成员同步进事件任务空间的 workspace 人员库（确认入岗前的桥接）。

    workspace 库是事件空间内的副本；长期档案在本注册表，二者分离（doc 04 §9），
    此处只做「补齐缺失」，不反向覆盖长期档案。
    """
    phone_n = normalize_phone(phone)
    person = registry.library.get(phone_n)
    if person is None:
        raise PermissionError("账号不在长期人员库：转人工补录。")
    if getattr(workspace, "person_library", {}).get(phone_n) is None:
        workspace.import_person_library([person])
