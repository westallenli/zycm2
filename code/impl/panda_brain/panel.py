"""Local control panel (doc 11 §7 P10) — the commander's web surface.

Zero third-party dependencies: Python stdlib ``http.server`` plus
server-rendered HTML. Binds to 127.0.0.1 only (single-user local premise,
doc 11 §3.5). The panel is a NEW ENTRANCE, never a new power layer: every
command act flows through the same authenticated workspace gates and the
same audit chain, and DB-backed panels write the snapshot back after each
successful act (same persistence model, no side channel).

    python -m panda_brain.panel [--db PATH] [--demo] [--port N] [--secret S]

Command surface (deliberately minimal, P10 decision point 2):
- answer an open general question (routed, targeted re-staffing; an anomaly
  answer also resumes the paused order);
- confirm or reject a memory candidate.
Order drafting / approval / activation stay OUT of the web surface.

Sessions live in memory only — restarting the panel always resets authority,
matching the kernel's "authority never persists" rule.

P16 T2 wires 定版 01《登录页功能定版 V0.3》 onto this surface:
- /login becomes a three-zone light page (doc 00 V0.2 palette): commander
  login (legacy POST /login semantics untouched), member login
  (POST /login/member through the real registry gate), and the QR check-in
  zone shown only with a live member session;
- /events (+ /events/{id}/roster, /events/{id}/close-confirm) gives the
  commander multi-fire-event management behind the SAME command_mode gate
  (invisible without it, like /drill);
- members get an idempotent scan→confirm→onboard loop whose only write path
  is still workspace.scan_check_in — command power never loosens.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from contextlib import contextmanager
from dataclasses import replace
import html as _html
import json
import os
import time
import secrets as _secrets
import sqlite3
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote

from .advisor_core import StaffCouncil
from .after_action_report import render_after_action_report
from .auth import GeneralEntryAuth
from .authorization import Action, AuthorizationPolicy, Role, RoleGrant
from . import callouts as _callout
from . import captain_arrivals as _captain_arrival
from . import temporary_personnel as _temp
from . import personnel_groups as _groups
from . import business_modules as _business
from . import unified_ui as _unified
from . import local_notifications as _local_notice
from . import squad_readiness as _readiness
from . import temporary_group_readiness as _temp_readiness
from . import force_needs as _force_needs
from .event_authorization import EventAuthorizationService, KIND as APPOINTMENT_KIND, event_grants
from .sand_drafts import FEEDBACK_ACTIONS, draft_context
from .task_drafts import COMMON_LABELS, TASK_LABELS, TaskCommonContent, TaskDraftSpec
from . import task_review as _task_review
from . import offline_tasks as _offline_tasks
from . import task_recipients as _task_recipients
from .personnel_states import (
    STATE_COLORS,
    derive_personnel_states,
    mark_person_state,
)
from .chief_advisor import ChiefAdvisor
from .data_imports import (
    DATA_TYPES,
    STATUS_FAILED,
    STATUS_PREVIEW,
    ImportLedger,
    TEXT_SUFFIXES,
    parse_and_map,
    publish as publish_import,
    rollback as rollback_import,
    sha256_text,
    validate_draft,
)
from .fire_events import (
    OUTCOME_CONFIRM,
    OUTCOME_IDEMPOTENT,
    CheckinOutcome,
    EventRegistry,
    confirm_checkin,
    ensure_person_in_workspace,
    resolve_checkin,
    group_of_slot,
    resolve_home_slot,
    utc_now_iso,
)
from .alarm_inbox import AlarmInbox, ALARM_STATUS_WORDS
from .forest_fire_playbook import FOREST_FIRE_PLAYBOOK
from . import homepage as _home
from . import materials as _mat
from .material_gap_evidence import assess_gap_inputs
from .material_supply_evidence import review_supply_leads
from . import material_supply_contacts as _material_supply_contacts
from . import material_supply_drafts as _material_supply_drafts
from . import material_supply_draft_page as _material_supply_draft_page
from . import material_supply_page as _material_supply_page
from . import manual_material_reports as _manual_material_reports
from . import manual_material_transfers as _manual_material_transfers
from . import material_withdrawals as _material_withdrawals
from . import material_custody_receipts as _material_custody_receipts
from . import material_custody_settlements as _material_custody_settlements
from . import material_custody_page as _material_custody_page
from . import grounded_inquiry as _grounded_inquiry
from .observation_context import is_observation_question
from . import deepseek_qa as _deepseek_qa
from . import model_connections as _models
from . import local_method_candidates as _local_methods
from .material_transfers import (_history as _transfer_history, loan_status,
                                 transfer_balance)
from . import sandtable
from . import tactical_map
from . import tileserve as _tiles
from . import imagery as _img
from . import intent_router
from .imagery import ImageryLedger
from .map_annotations import (
    AnnotationKind,
    AnnotationSource,
    AnnotationStatus,
    MapAnnotation,
    TACTICAL_KINDS,
    kind_label,
    status_word as annotation_status_word,
)
from .materials import MaterialsLedger
from . import review_domain as _rvd
from .review_domain import ReviewLedger
from .reviewview import render_review_page, export_review_markdown
from .sandview import render_sand_page
from .models import (
    DecisionActor,
    DecisionType,
    GENERAL_ENTRY_ID,
    InformationGapStatus,
    Option,
    OrderStatus,
    Reversibility,
    RiskLevel,
)
from .org_model import POSITION_SLOTS, SLOT_INDEX, mask_phone, normalize_phone
from . import workbench as wb
from .invariants import check_campaign_invariants
from .situation_score import default_scorer
from .workspace import ScenarioWorkspace, WorkspaceConflictError

#: Routes of the read-only panel (T4 appends /timeline; P15 T4 appends the
#: map-first workbench surfaces /map and /org).
ROUTES: tuple[tuple[str, str], ...] = (
    ("/", "首页"),
    ("/map", "态势一张图"),
    ("/overview", "态势总览"),
    ("/org", "编成"),
    ("/brief", "决策简报"),
    ("/orders", "在途命令"),
    ("/questions", "待你回答"),
    ("/reviews", "复盘与记忆"),
    ("/timeline", "时间线审计"),
)

_SESSION_COOKIE = "panel_session"
_ACTION_ROUTES = {"/answer", "/memory", "/pause", "/confirm-goal", "/propose-option", "/restaff"}
#: Drill-console acts (P15 T4): same permission pre-gate, same status-code
#: style as _ACTION_ROUTES (403 locked / 409 refused), routed separately so
#: the console stays invisible until the general entry is authenticated.
_DRILL_ACTION_ROUTES = {
    "/drill/checkin",
    "/drill/checkout",
    "/drill/import-roster",
    "/drill/save",
    "/drill/load",
    "/drill/reset",
}

_TERMINAL_ORDER_VALUES = {"completed", "failed", "cancelled", "expired", "superseded"}

#: 首页静态资源白名单（拒绝任意路径读取）。
#: P17-P2：组织架构改为数据驱动自绘，org_chart.png 不再提供静态路由——
#: 运行时零图片依赖（docs/页面设计定版/02_首页组织架构自绘_V0.3.md §1）。
STATIC_DIR = Path(__file__).resolve().parents[1] / "static"
_STATIC_WHITELIST = {
    'captain-mobile.css': (STATIC_DIR / 'captain-mobile.css','text/css; charset=utf-8'),
    'captain-mobile.js': (STATIC_DIR / 'captain-mobile.js','text/javascript; charset=utf-8'),
    "resource-adviser.js": (STATIC_DIR / "resource-adviser.js", "text/javascript; charset=utf-8"),
    "resource-directory.js": (STATIC_DIR / "resource-directory.js", "text/javascript; charset=utf-8"),
    "sand-command.css": (STATIC_DIR / "sand-command.css", "text/css; charset=utf-8"),
    "sand-command.js": (STATIC_DIR / "sand-command.js", "text/javascript; charset=utf-8"),
    "map-brief.js": (STATIC_DIR / "map-brief.js", "text/javascript; charset=utf-8"),
    "command-sop.js": (STATIC_DIR / "command-sop.js", "text/javascript; charset=utf-8"),
    "map-demo.js": (STATIC_DIR / "map-demo.js", "text/javascript; charset=utf-8"),
    "map-brief.css": (STATIC_DIR / "map-brief.css", "text/css; charset=utf-8"),
    "wide-arrow.js": (STATIC_DIR / "wide-arrow.js", "text/javascript; charset=utf-8"),
    "manual-tools.js": (STATIC_DIR / "manual-tools.js", "text/javascript; charset=utf-8"),
    "manual-tools.css": (STATIC_DIR / "manual-tools.css", "text/css; charset=utf-8"),
    "offline-map.js": (STATIC_DIR / "offline-map.js", "text/javascript; charset=utf-8"),
    "model-test.js": (STATIC_DIR / "model-test.js", "text/javascript; charset=utf-8"),
    "org-float.css": (STATIC_DIR / "org-float.css", "text/css; charset=utf-8"),
    "org-float.js": (STATIC_DIR / "org-float.js", "text/javascript; charset=utf-8"),
    "login.css": (STATIC_DIR / "login.css", "text/css; charset=utf-8"),
    "login.js": (STATIC_DIR / "login.js", "text/javascript; charset=utf-8"),
    "login-landscape.png": (STATIC_DIR / "login-landscape.png", "image/png"),
    "offline.css": (STATIC_DIR / "offline.css", "text/css; charset=utf-8"),
    "offline.js": (STATIC_DIR / "offline.js", "text/javascript; charset=utf-8"),
    "org_chart.png": None,  # 已下线：保留键位以显式返回 404，防旧客户端回退
    "workbench-theme.css": (STATIC_DIR / "workbench-theme.css", "text/css; charset=utf-8"),
    "sand-shell.css": (STATIC_DIR / "sand-shell.css", "text/css; charset=utf-8"),
    "sand-shell.js": (STATIC_DIR / "sand-shell.js", "text/javascript; charset=utf-8"),
    "emergency-ui.js": (STATIC_DIR / "emergency-ui.js", "text/javascript; charset=utf-8"),
    "emergency-ui.css": (STATIC_DIR / "emergency-ui.css", "text/css; charset=utf-8"),
    "sand-drafts.js": (STATIC_DIR / "sand-drafts.js", "text/javascript; charset=utf-8"),
    "sand-runtime.js": (STATIC_DIR / "sand-runtime.js", "text/javascript; charset=utf-8"),
    "source-review.js": (STATIC_DIR / "source-review.js", "text/javascript; charset=utf-8"),
    "material-custody.js": (STATIC_DIR / "material-custody.js", "text/javascript; charset=utf-8"),
    "material-withdrawal.js": (STATIC_DIR / "material-withdrawal.js", "text/javascript; charset=utf-8"),
    "material-supply.js": (STATIC_DIR / "material-supply.js", "text/javascript; charset=utf-8"),
    "supply-draft.js": (STATIC_DIR / "supply-draft.js", "text/javascript; charset=utf-8"),
    "live-context.js": (STATIC_DIR / "live-context.js", "text/javascript; charset=utf-8"),
    "intelligence.css": (STATIC_DIR / "intelligence.css", "text/css; charset=utf-8"),
    "workflow.css": (STATIC_DIR / "workflow.css", "text/css; charset=utf-8"),
    "workflow.js": (STATIC_DIR / "workflow.js", "text/javascript; charset=utf-8"),
    "sop-sand.css": (STATIC_DIR / "sop-sand.css", "text/css; charset=utf-8"),
    "sop-sand.js": (STATIC_DIR / "sop-sand.js", "text/javascript; charset=utf-8"),
    "field-sop.css": (STATIC_DIR / "field-sop.css", "text/css; charset=utf-8"),
    "field-sop.js": (STATIC_DIR / "field-sop.js", "text/javascript; charset=utf-8"),
    "nine-todo.css": (STATIC_DIR / "nine-todo.css", "text/css; charset=utf-8"),
    "review-replay.js": (STATIC_DIR / "review-replay.js", "text/javascript; charset=utf-8"),
    "review-flow.css": (STATIC_DIR / "review-flow.css", "text/css; charset=utf-8"),
    "nine-flow.css": (STATIC_DIR / "nine-flow.css", "text/css; charset=utf-8"),
    "leaflet/leaflet.js": (STATIC_DIR / "leaflet/leaflet.js", "text/javascript; charset=utf-8"),
    "leaflet/leaflet.css": (STATIC_DIR / "leaflet/leaflet.css", "text/css; charset=utf-8"),
    **{f"leaflet/images/{name}.png": (STATIC_DIR / f"leaflet/images/{name}.png", "image/png")
       for name in ('layers', 'layers-2x', 'marker-icon', 'marker-icon-2x', 'marker-shadow')},
}

#: 物资底账持久化位置（P16 T5）：首次变更时惰性创建；文件不存在即空底账。
DEFAULT_MATERIALS_PATH = Path(__file__).resolve().parents[1] / "data" / "materials.json"

#: 复盘底账持久化位置（P16 T7）：问题/整改/经验，独立 JSON 分库。
DEFAULT_REVIEWS_PATH = Path(__file__).resolve().parents[1] / "data" / "reviews.json"

#: 外部报警收件箱持久化位置（P17-P1）：待确认→确认建档/拒绝/重复关联，
#: 独立 JSON 分库；建档前报警以此为本业务事实源（与名册 start 前登记同例）。
DEFAULT_ALARM_INBOX_PATH = Path(__file__).resolve().parents[1] / "data" / "alarm_inbox.json"

#: 无人机影像底账持久化位置（P16 T8）。
DEFAULT_IMAGERY_PATH = Path(__file__).resolve().parents[1] / "data" / "imagery.json"
#: 上传原图落盘目录：静态目录下的 uploads/{event_id}/，仅指挥员可读。
UPLOAD_DIR = STATIC_DIR / "uploads"

#: P16 T2 member sessions ride a SEPARATE cookie namespace so one browser can
#: hold commander + member identities at once without privilege mixing; member
#: tokens never enter ``state.sessions``, so command_mode stays fail-closed.
_MEMBER_COOKIE = "member_session"
#: Multi-fire-event registry persistence location (P16 T2): created lazily on
#: the first registry mutation; a missing file simply means an empty registry.
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "data" / "event_registry.json"

# ---------------------------------------------------------------------------
# Panel state


class PanelActionError(Exception):
    """A command act was refused; message is safe to show the commander."""


class MemberSession:
    """现场人员会话（01 号 §2.1）：身份来自长期人员库；绑定事件与到场时间在
    刷码确认成功后写入。仅存内存，进程重启即清空（与指挥员会话同纪律）。"""

    def __init__(self, phone: str, name: str) -> None:
        self.phone = phone
        self.name = name
        self.bound_event_id = ""  # 空 ＝ 尚未入岗
        self.selected_event_id = ""  # 浏览上下文；不代表刷码、到场或指挥员权限
        self.workflow_preferences = {}  # Per-event navigation only; cleared with this login.
        self.arrived_at = ""      # 刷码确认成功自动写入
        self.custody_csrf = _secrets.token_urlsafe(32)

    @property
    def onboarded(self) -> bool:
        return bool(self.bound_event_id)


def _event_slug(event_id: str) -> str:
    """Filename-safe slug, same discipline as the report export route."""
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in event_id)


def _event_label(registry: EventRegistry, event_id: str) -> str:
    """「fire-001 汉江上游山火」式标签；未知编号照实回显编号本身。"""
    record = registry.events.get(event_id)
    return f"{event_id} {record.name}" if record is not None else str(event_id)


class PanelState:
    """Everything the renderer needs; swapped atomically on reload."""

    def __init__(
        self,
        workspace: ScenarioWorkspace,
        *,
        db_path: str | Path | None = None,
        auth: GeneralEntryAuth | None = None,
        registry: EventRegistry | None = None,
        registry_path: str | Path | None = None,
        ledger: MaterialsLedger | None = None,
        materials_path: str | Path | None = None,
        review_ledger: ReviewLedger | None = None,
        reviews_path: str | Path | None = None,
        imagery: ImageryLedger | None = None,
        imagery_path: str | Path | None = None,
        upload_dir: str | Path | None = None,
        imports_ledger: ImportLedger | None = None,
        imports_path: str | Path | None = None,
        alarm_inbox: AlarmInbox | None = None,
        alarm_inbox_path: str | Path | None = None,
        duty_subjects: tuple[str, ...] = (),
    ) -> None:
        self.workspace = workspace
        self._simulation_lock = threading.RLock()
        self.simulation_host = None  # Explicit isolated launcher only; normal panels stay unchanged.
        self.db_path = Path(db_path) if db_path is not None else None
        self.auth = auth if auth is not None else GeneralEntryAuth()
        #: In-memory session tokens; empty after every process restart.
        self.sessions: set[str] = set()
        #: 会话签发时刻（monotonic），支撑 01 号 §8「会话过期」TTL。
        self.session_issued_at: dict[str, float] = {}
        #: 路由层为复盘负责人本人会话设置的临时授权上下文（单线程 HTTP）。
        self._pending_review_actor = None
        self._review_form_secret = _secrets.token_bytes(32)
        #: LLM 增强层装配失败时的用户可见说明（P16-T22）。
        self.llm_notice: str = ""
        #: LLM 连通自检最近一次结果（P16-T23；仅内存，重启即清）。
        self.llm_selfcheck: dict | None = None
        #: Proof-of-possession credentials of the LIVE login; used only to
        #: re-arm swapped workspaces (snapshot load / drill reset). Never
        #: persisted, never leaves the process.
        self._credentials = None
        self._chief: ChiefAdvisor | None = None
        #: P16 T2：多火情注册表（事件/名册/二维码/成员凭证/长期人员库）。
        self.registry_path = (
            Path(registry_path) if registry_path is not None else DEFAULT_REGISTRY_PATH
        )
        from .durable_storage import recover, recovery_snapshot_paths
        recovered = recover(self.registry_path.parent / '.state-transactions', files=(
            self.registry_path, self.db_path,
            materials_path or DEFAULT_MATERIALS_PATH, reviews_path or DEFAULT_REVIEWS_PATH,
            imagery_path or DEFAULT_IMAGERY_PATH, imports_path,
            alarm_inbox_path or DEFAULT_ALARM_INBOX_PATH,
            *recovery_snapshot_paths(self.registry_path, self.db_path)),
            directories=(wb.SNAPSHOT_DIR,))
        if recovered and self.db_path and self.db_path.exists():
            from .persistence import load_workspace
            self.workspace = load_workspace(self.db_path, general_auth=self.auth)
        self.inquiry_journal = _grounded_inquiry.InquiryJournal(
            self.registry_path.with_name('inquiries.json'))
        #: One-time request previews, bound to a live commander session.
        self.deepseek_previews: dict[str, dict] = {}
        self.model_connections: dict[str, _models.Session] = {}
        # Event-scoped delegation references an administrator session, never a copied Key.
        self.sand_model_bindings: dict[str, dict] = {}
        self.sand_deputy_models: dict[tuple[str, str], _models.Session] = {}
        self._model_connections_lock = threading.RLock()
        self.registry = (
            registry if registry is not None and not recovered else EventRegistry.load(self.registry_path)
        )
        #: P16 T5：物资与中队长期底账（doc 04；独立 JSON，与注册表分库）。
        self.materials_path = (
            Path(materials_path) if materials_path is not None else DEFAULT_MATERIALS_PATH
        )
        self.ledger = (
            ledger if ledger is not None else MaterialsLedger.load(self.materials_path)
        )
        #: 成员会话（仅内存）；指挥员会话机制（self.sessions）保持原样。
        self.member_sessions: dict[str, MemberSession] = {}
        self._event_authority_lock = threading.RLock()
        # Trusted application configuration, never an HTTP form field. A
        # general-entry session is not a named duty officer by itself.
        self.configure_duty_subjects(duty_subjects)
        #: 当前已进入的事件上下文；非空时保证 self.workspace 就是该事件的
        #: 任务空间（任何工作区替换都必须同步清空，见 handle_reset / load）。 
        self.active_event_id = ""
        #: P16 T6：沙盘问询留痕（仅内存会话内，重启即清）。
        self.sand_qa_log: list = []
        self.voice_drafts: dict[str, dict] = {}
        self.intent_notice: str = ""
        #: P18 指挥助手系统通知留痕（仅内存会话内，重启即清；业务事实仍在审计链）。
        self.advisor_log: list[dict] = []
        #: P16 T10：复盘问询留痕（只读；仅内存会话内，重启即清）。
        self.review_qa_log: list = []
        #: P16 T7：复盘底账（问题/整改/经验；独立 JSON，与注册表/物资分库）。
        self.reviews_path = (
            Path(reviews_path) if reviews_path is not None
            else DEFAULT_REVIEWS_PATH
        )
        self.review_ledger = (
            review_ledger if review_ledger is not None
            else ReviewLedger.load(self.reviews_path)
        )
        #: P16 T8：无人机影像底账（独立 JSON；原图落盘 uploads/{event}/）。
        self.imagery_path = (
            Path(imagery_path) if imagery_path is not None else DEFAULT_IMAGERY_PATH
        )
        self.imagery = (
            imagery if imagery is not None else ImageryLedger.load(self.imagery_path)
        )
        #: 原图落盘根目录（可注入以便测试隔离）。
        self.upload_dir = Path(upload_dir) if upload_dir is not None else UPLOAD_DIR
        #: 文件导入批次台账（doc 04 §8；JSON 持久化与其余底账同构）。
        self.imports_path = (
            Path(imports_path) if imports_path is not None else None
        )
        self.imports = (
            imports_ledger if imports_ledger is not None
            else (ImportLedger.load(self.imports_path)
                  if self.imports_path and self.imports_path.exists()
                  else ImportLedger())
        )
        #: P17-P1：外部报警收件箱（待确认→确认建档/拒绝/重复关联；独立 JSON）。
        self.alarm_inbox_path = (
            Path(alarm_inbox_path) if alarm_inbox_path is not None
            else DEFAULT_ALARM_INBOX_PATH
        )
        self.alarm_inbox = (
            alarm_inbox if alarm_inbox is not None
            else AlarmInbox.load(self.alarm_inbox_path)
        )

    @property
    def entry_unlocked(self) -> bool:
        return bool(self.workspace._general_entry_unlocked or self.named_sand_authorized())

    def chain_broken(self) -> tuple[int, ...]:
        return self.workspace.verify_audit_chain()

    @property
    def chief(self) -> ChiefAdvisor:
        if self._chief is None:
            self._chief = ChiefAdvisor(
                StaffCouncil(FOREST_FIRE_PLAYBOOK, enhancer=self._build_enhancer()),
                situation_scorer=default_scorer(),
            )
        return self._chief

    @staticmethod
    def _llm_slot_settings(settings) -> dict:
        """settings → provider 槽位映射。凭证只从环境变量读，绝不落盘：
        openai_compatible/local_nim 统一约定 LLM_API_KEY；nvidia 另认 NVIDIA_API_KEY。
        """
        import os as _os

        return {
            "provider": settings.llm_provider,
            "model": settings.llm_model,
            "base_url": settings.llm_base_url,
            "api_key": _os.environ.get("LLM_API_KEY", ""),
            "timeout_seconds": settings.llm_timeout_seconds,
        }

    def _build_enhancer(self):
        """按 config/settings.yaml 装配 LLM 增强器（P16-T22）。

        纪律（doc 11 §6.6 / P8-T5）：联网开关关闭、无凭证或装配失败时，
        一律返回 None——规则参谋部原样运行，LLM 只是可选增强层。
        """
        try:
            from .settings import load_settings

            settings = load_settings()
            if settings.llm_provider == "null" or not settings.network_enabled:
                return None
            from .providers import create_provider

            provider = create_provider(
                self._llm_slot_settings(settings), system_settings=settings)
        except Exception as exc:   # 配置损坏/未知槽位/缺凭证等：安全回落
            self.llm_notice = f"LLM 增强层未启用：{exc}"
            return None
        from .llm_enhance import LLMTextEnhancer

        return LLMTextEnhancer(provider)

    def _build_llm_provider(self) -> tuple[object, str]:
        try:
            from .settings import load_settings

            settings = load_settings()
            if settings.llm_provider == "null":
                return None, "llm_provider=null（未配置联网槽位）"
            if not settings.network_enabled:
                return None, "联网开关关闭（network_enabled=false）"
            from .providers import create_provider

            return (
                create_provider(
                    self._llm_slot_settings(settings), system_settings=settings),
                "",
            )
        except Exception as exc:
            return None, str(exc)

    #: 连通自检最小调用文案：固定探针，不携带任何业务内容。
    LLM_SELFCHECK_SYSTEM = (
        "Connectivity probe inside a command-staff enhancement layer."
    )
    LLM_SELFCHECK_PROMPT = "Reply with the single word OK and nothing else."

    def handle_llm_selfcheck(self, *, provider=None) -> dict:
        """LLM 连通自检（后台运维诊断，P16-T23；前台出口已按五页口径移除）。

        参谋/LLM 工作一律后台运行（DEVELOPMENT_PLAN_PAGES_V1 §5.3）：本方法
        不再有 HTTP 路由或页面卡片，仅供运维控制台/测试直接调用取证。provider
        最小调用，成败都入审计链；审计载荷只含 provider 名/model/时延/截断
        回执或失败原因，绝不含凭证，也不改写任何评估、不发号施令。
        ``provider`` 参数仅供测试注入离线桩，生产路径一律按配置现建。
        """
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        import time as _time

        started = _time.monotonic()
        pname = pmodel = ""
        ok, detail = False, ""
        try:
            if provider is None:
                provider, reason = self._build_llm_provider()
                if provider is None:
                    raise RuntimeError(f"LLM 未装配：{reason}")
            pname = str(getattr(provider, "name", "unknown"))
            pmodel = str(getattr(provider, "model", "") or "")
            reply = provider.complete(
                system=self.LLM_SELFCHECK_SYSTEM,
                prompt=self.LLM_SELFCHECK_PROMPT,
            )
            text = " ".join(str(reply).split())
            if not text:
                raise RuntimeError("空回执")
            ok, detail = True, text[:120]
        except Exception as exc:
            detail = f"{type(exc).__name__}: {exc}"[:300]
        latency_ms = int((_time.monotonic() - started) * 1000)
        result = {
            "at": utc_now_iso(),
            "ok": ok,
            "provider": pname or "(未装配)",
            "model": pmodel,
            "latency_ms": latency_ms,
            "detail": detail,
        }
        self.workspace.append_event(   # 唯一写入口：成功与失败均留痕
            "LLMSelfCheck",
            at=result["at"], ok=ok, provider=result["provider"],
            model=pmodel, latency_ms=latency_ms, detail=detail,
        )
        self.persist_if_db()
        self.llm_selfcheck = result
        return result

    # -- session lifecycle (T2) ---------------------------------------------

    def login(self, candidate: str) -> str | None:
        """Authenticate against the real general-entry gate; issue one session."""
        try:
            credentials = self.auth.authenticate(candidate)
        except PermissionError:
            return None
        self.workspace.unlock_general_entry(credentials)
        self._credentials = credentials
        token = _secrets.token_urlsafe(24)
        self.sessions.add(token)
        import time as _time
        self.session_issued_at[token] = _time.monotonic()
        try:
            local = _models.load_local(self.registry_path.with_name('model-connections.json'))
            self.model_connections[token] = _models.Session(local=local)
        except (ValueError, OSError):
            self.model_connections[token] = _models.Session(local_status='已保存的本地配置无法读取，请重新配置。')
        return token

    def logout(self, token: str | None) -> None:
        with self._model_connections_lock:
            self.sessions.discard(token or "")
            self.session_issued_at.pop(token or "", None)
            self.model_connections.pop(token or '', None)
            self.deepseek_previews = {k: v for k, v in self.deepseek_previews.items() if v['session'] != token}
            from .sand_command import revoke_model_bindings
            revoke_model_bindings(self, token)

    #: 指挥员会话有效期（01 号 §8「会话过期」）：8 小时，超时需重新登录。
    SESSION_TTL_SECONDS = 8 * 3600

    def session_valid(self, token: str | None) -> bool:
        if not token or token not in self.sessions:
            return False
        import time as _time
        issued = self.session_issued_at.get(token)
        if issued is not None and (_time.monotonic() - issued) > self.SESSION_TTL_SECONDS:
            self.logout(token)                   # Expiry also removes session credentials.
            return False
        return True

    def named_sand_authorized(self) -> bool:
        token = getattr(self, '_named_sand_token', None)
        return bool(token and _unified.deputy_command(self, token))

    def command_mode(self, token: str | None) -> bool:
        """Command acts need BOTH a valid session and an unlocked workspace."""
        return self.entry_unlocked and self.session_valid(token)

    # -- command acts (T3) ----------------------------------------------------

    def persist_if_db(self) -> None:
        """Write the snapshot back after a successful act (same audit chain)."""
        if self.db_path is not None:
            from .persistence import save_workspace

            save_workspace(self.workspace, self.db_path)
        self._persist_active_task_snapshot()

    def _persist_active_task_snapshot(self) -> None:
        """Keep the event snapshot used by named roles aligned with draft edits."""
        event_id = self.active_event_id
        record = self.registry.events.get(event_id) if event_id else None
        target = Path(record.snapshot_path) if record and record.snapshot_path else None
        if (target is not None and target.is_file()
                and self.workspace.event_id == event_id):
            from .persistence import save_workspace
            save_workspace(self.workspace, target)

    def handle_answer(self, question_id: str, answer: str) -> dict:
        """Answer one open question through the classified routing.

        Routable kinds get targeted re-staffing (P7-T4); an execution-anomaly
        answer additionally resumes the paused order (doc 11 §3.5 rule 5).
        """
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        question = self.workspace.general_questions.get(question_id)
        if question is None:
            raise PanelActionError(f"未知请示：{question_id}")
        if question.status != "open":
            raise PanelActionError(f"请示 {question_id} 已答复归档。")
        answer = (answer or "").strip()
        if not answer:
            raise PanelActionError("答复内容不能为空。")

        result: dict = {"question_id": question_id, "resumed_order": None}
        if question.kind == "execution_anomaly":
            self.workspace.answer_general_question(
                question_id,
                answer,
                actor_role=DecisionActor.COMMANDER,
                answered_by=GENERAL_ENTRY_ID,
            )
            order = self.workspace.orders.get(question.subject or "")
            if order is not None and order.status == OrderStatus.PAUSED:
                self.workspace.resume_order(order.id, actor_role=DecisionActor.COMMANDER)
                result["resumed_order"] = order.id
        else:
            outcome = self.chief.handle_question_answer(
                self.workspace,
                question_id,
                answer,
                actor_role=DecisionActor.COMMANDER,
                answered_by=GENERAL_ENTRY_ID,
            )
            result.update(
                plan_id=outcome.get("plan_id"),
                task_id=outcome.get("task_id"),
                assessment_id=outcome.get("assessment_id"),
            )
        self.persist_if_db()
        return result

    def handle_memory(self, candidate_id: str, decision: str) -> dict:
        """Confirm or reject one proposed memory candidate (a general act)."""
        if decision not in {"confirm", "reject"}:
            raise PanelActionError("记忆处置只能是 confirm 或 reject。")
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        candidate = self.workspace.memory_candidates.get(candidate_id)
        if candidate is None:
            raise PanelActionError(f"未知记忆候选：{candidate_id}")
        if candidate.status.value != "proposed":
            raise PanelActionError(f"候选 {candidate_id} 已处置（{candidate.status.value}）。")
        updated = self.workspace.confirm_memory_candidate(
            candidate_id,
            actor_role=DecisionActor.COMMANDER,
            confirmed_by=GENERAL_ENTRY_ID,
            reject=(decision == "reject"),
        )
        self.persist_if_db()
        return {"candidate_id": candidate_id, "status": updated.status.value}

    def handle_pause(self, order_id: str, reason: str) -> dict:
        """Pause one active execution order — a general act (docs/16 v2 batch).

        The reason is mandatory: OrderPaused carries it into the audit chain.
        """
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        reason = (reason or "").strip()
        if not reason:
            raise PanelActionError("暂停原因不能为空。")
        order = self.workspace.orders.get(order_id)
        if order is None:
            raise PanelActionError(f"未知命令：{order_id}")
        if order.status != OrderStatus.ACTIVE:
            raise PanelActionError(
                f"命令 {order_id} 当前为 {order.status.value}，只有执行中的命令可暂停。"
            )
        self.workspace.pause_order(order_id, reason, actor_role=DecisionActor.COMMANDER)
        self.persist_if_db()
        return {"order_id": order_id, "status": "paused"}

    def handle_confirm_goal(self, goal_frame_id: str) -> dict:
        """Confirm one proposed goal frame — a general act (docs/16 v2 batch)."""
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        frame = self.workspace.goal_frames.get(goal_frame_id)
        if frame is None:
            raise PanelActionError(f"未知目标框：{goal_frame_id}")
        if frame.confirmed_by is not None:
            raise PanelActionError(
                f"目标框 {goal_frame_id} 已由 {frame.confirmed_by} 确认。"
            )
        confirmed = self.chief.confirm_goal_frame(self.workspace, goal_frame_id)
        self.persist_if_db()
        return {"goal_frame_id": confirmed.id, "confirmed_by": confirmed.confirmed_by}

    def handle_propose_option(
        self,
        *,
        title: str,
        summary: str,
        claim_subjects: str = "",
        risks: str = "",
        no_go_conditions: str = "",
        preconditions: str = "",
        reversibility: str = "reversible",
    ) -> dict:
        """Register an option proposed by the general himself (docs/16 v2 batch).

        Identity attribution is structural: add_commander_option forces
        proposed_by_commander=True and proposer_role=None on the audit event.
        """
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        title = (title or "").strip()
        summary = (summary or "").strip()
        if not title:
            raise PanelActionError("方案标题不能为空。")
        if not summary:
            raise PanelActionError("方案概述不能为空。")
        rev = _REVERSIBILITY_CHOICES.get((reversibility or "").strip())
        if rev is None:
            raise PanelActionError(f"可逆性取值不认识：{reversibility!r}")

        def csv(raw: str) -> tuple[str, ...]:
            return tuple(part.strip() for part in (raw or "").split(",") if part.strip())

        option_id = f"opt-cmd-v{self.workspace.version + 1:03d}"
        option = Option(
            id=option_id,
            title=title,
            summary=summary,
            evidence_ids=(),
            claim_subjects=csv(claim_subjects),
            risks=csv(risks),
            reversibility=rev,
            no_go_conditions=csv(no_go_conditions),
            preconditions=csv(preconditions),
        )
        registered = self.workspace.add_commander_option(
            option, actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID
        )
        self.persist_if_db()
        return {"option_id": registered.id}

    def handle_restaff(self, question: str | None = None, *, session_token: str | None = None) -> dict:
        """Trigger one fresh staff pass (docs/16 v2 companion).

        Orchestration and brief composition are staff WORK, not general power:
        neither sits behind the workspace gate. The panel still requires a live
        session because a pass consumes real staff effort.
        """
        if not (self.session_valid(session_token) or self.named_sand_authorized()):
            raise PermissionError("重新参谋需要面板会话。")
        text = (question or "").strip() or "Panel-triggered re-staffing pass."
        topic = self.chief.orchestrate(self.workspace, text)
        brief = self.chief.compose_decision_brief(self.workspace, topic)
        return {"topic_id": topic.id, "brief_id": brief.id}

    # -- workbench drill acts (P15 T4) ---------------------------------------

    def review_operator(self) -> str:
        """当前复盘作业动作的操作者署名（doc 05 §9 权限矩阵）。"""
        actor = getattr(self, "_pending_review_actor", None)
        if actor and actor[0] == "owner":
            return f"复盘负责人·{actor[1]}"
        return "指挥员"

    def _require_review_actor(self) -> None:
        """复盘作业授权（doc 05 §9）：指挥员秘钥会话，或路由层核验过的
        复盘负责人本人会话（_pending_review_actor 由 /review/ 门设置）。"""
        actor = getattr(self, "_pending_review_actor", None)
        if self.workspace.event_id:
            from .review_meeting import is_owner
            if actor and actor[0] == 'owner' and len(actor) > 3 and actor[2] == self.active_event_id and is_owner(self,actor[3]):
                return
            raise PermissionError('仅本事件复盘负责人本人可执行。')
        if self.entry_unlocked:
            return
        actor = getattr(self, "_pending_review_actor", None)
        if actor and actor[0] == "owner":
            return
        raise PermissionError("指挥动作需要指挥员入口认证。")

    def _require_command(self, *, allow_closed=False) -> None:
        if not self.entry_unlocked:
            raise PermissionError("指挥动作需要指挥员入口认证。")

        if not allow_closed:
            from .fire_events import ONBOARDABLE_STATUSES
            eid = self.workspace.event_id or self.active_event_id
            if eid:
                event = self.registry.events.get(eid)
                if event is None or event.status not in ONBOARDABLE_STATUSES:
                    raise PanelActionError('当前事件不接受作战或人员写入；关闭事件请使用批准的物资收尾或复盘入口。')

    def _require_legacy_personnel(self):
        """日常人员名录维护：指挥员/值班员负责，事件进行中也允许维护名册。

        以前这里只要存在事件就一律拒绝，导致"日常维护"在演示或正式事件期间
        完全做不了。人员名册是基础台账，不是作战命令，不该被事件锁死。
        """
        self._require_command()

    def handle_offline_map_package(self, event_id, *, member_token, **values):
        from .offline_resources import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            result = record(candidate, actor, **values)
            if candidate.events != space.events:
                from .persistence import save_workspace
                save_workspace(candidate, target)
                if active:
                    self.workspace = candidate
            return result

    def handle_terminal_report(self, event_id, *, member_token, envelope):
        from .terminal_reports import submit
        with self._event_authority_lock:
            return submit(self, event_id, member_token, envelope)

    def handle_personnel_management(self, event_id, *, member_token, **values):
        from .personnel_management import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate, registry, ledger = deepcopy(space), deepcopy(self.registry), deepcopy(self.ledger)
            result = record(candidate, registry, actor, ledger=ledger, spaces=self.supply_event_spaces(override=candidate), **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            registry.save(self.registry_path)
            if values.get("operation") == "squad":
                ledger.save(self.materials_path)
            save_workspace(candidate, target)
            self.registry, self.ledger = registry, ledger
            if active:
                self.workspace = candidate
            return result

    def handle_checkin(self, pos_code: str, phone: str) -> dict:
        """Simulated QR check-in — flows through the workspace roster gates."""
        self._require_legacy_personnel()
        pos_code, phone = (pos_code or "").strip(), (phone or "").strip()
        if not pos_code or not phone:
            raise PanelActionError("请选择人员与岗位槽位后再签到。")
        binding = self.workspace.scan_check_in(pos_code, phone=phone, operator="workbench")
        self.persist_if_db()
        return {"binding_id": binding.id}

    def handle_checkout(self, binding_value: str) -> dict:
        """Release one member from a slot (value ``<pos_code>|<phone>``)."""
        self._require_legacy_personnel()
        pos_code, _, phone = (binding_value or "").partition("|")
        if not pos_code or not phone:
            raise PanelActionError("请选择要离岗的在岗人员。")
        departed = self.workspace.check_out(pos_code, phone=phone)
        self.persist_if_db()
        return {"binding_id": departed.id, "status": departed.status}

    def handle_person_state_mark(
        self, event_id: str, action: str, *, phone: str,
        order_id: str = "", note: str = "",
    ) -> dict:
        """指挥员八态人工判定（doc 04 §4.1）：执行中/暂离轮休/失联/归建等。"""
        self._require_legacy_personnel()
        roster = self.registry.rosters.get(event_id)
        known_in_roster = roster is not None and phone in roster.entries
        known_binding = any(
            b.person.phone == phone for b in self.workspace.roster_bindings.values())
        if not (known_in_roster or known_binding):
            raise PanelActionError("该手机号不在本事件名册与签到记录中，拒绝标记。")
        try:
            mark_person_state(
                self.workspace, action, phone=phone,
                operator=GENERAL_ENTRY_ID, order_id=order_id, note=note)
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self.persist_if_db()
        if self.active_event_id:
            record = self.registry.events.get(self.active_event_id)
            snapshot = Path(record.snapshot_path) if record and record.snapshot_path else None
            if snapshot is not None and snapshot.exists():
                from .persistence import save_workspace
                save_workspace(self.workspace, snapshot)
        return {"phone": phone, "action": action}

    def handle_import_roster(self, csv_text: str) -> dict:
        """Standing-library import; any row error blocks the whole batch."""
        self._require_legacy_personnel()
        records, errors = wb.parse_roster_csv(csv_text or "")
        if errors:
            raise PanelActionError("人员库导入未执行，逐行错误：\n" + "\n".join(errors))
        if not records:
            raise PanelActionError("导入批次为空：请粘贴「姓名,手机号,…」CSV 行。")
        count = self.workspace.import_person_library(records)
        self.persist_if_db()
        return {"count": count}

    def handle_save_snapshot(self) -> dict:
        """Full snapshot into impl/snapshots/<slug>-<UTC stamp>.db."""
        self._require_command(allow_closed=True)
        from .persistence import save_workspace

        wb.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        filename = (
            f"{wb.scenario_slug(self.workspace.scenario_id)}-{wb.snapshot_stamp()}.db"
        )
        save_workspace(self.workspace, wb.SNAPSHOT_DIR / filename)
        return {"filename": filename}

    def handle_load_snapshot(self, filename: str) -> dict:
        """Swap the live workspace for a stored snapshot.

        Authority is never restored from disk: the reloaded workspace binds
        THIS panel's auth and starts locked, then is opened with the live
        session's credentials only.
        """
        self._require_command(allow_closed=True)
        from .persistence import load_workspace

        known = {entry["name"] for entry in wb.list_snapshots()}
        if filename not in known:
            raise PanelActionError(f"未知快照文件：{filename}")
        workspace = load_workspace(wb.SNAPSHOT_DIR / filename, general_auth=self.auth)
        workspace.unlock_general_entry(self._credentials)
        # 快照可能是任一历史状态，事件任务空间保证随之失效。
        self.active_event_id = ""
        self.workspace = workspace
        return {"scenario_id": workspace.scenario_id}

    def handle_reset(self) -> dict:
        """Fresh ForestFireScenario demo workspace bound to the live session.

        The scenario builder provisions an ephemeral entry of its own; the
        panel rebinds the SAME fingerprint the commander authenticated
        against and opens it with the live credentials, so command authority
        still originates exclusively from a real general-entry login.
        """
        self._require_command(allow_closed=True)
        from .simulator import ForestFireScenario

        fresh = ForestFireScenario().workspace
        fresh._general_entry_fingerprint = self.auth.fingerprint
        fresh._general_entry_unlocked = False
        fresh.unlock_general_entry(self._credentials)
        # 工作区被替换为演练初始态，事件任务空间保证不再成立。
        self.active_event_id = ""
        self.workspace = fresh
        return {"scenario_id": fresh.scenario_id}

    # -- fire-event management + member check-in (P16 T2) ---------------------

    def _save_registry(self) -> None:
        """Persist the registry JSON right away (persist_if_db pattern)."""
        self.registry.save(self.registry_path)

    def _save_alarm_inbox(self) -> None:
        """外部报警收件箱落盘（P17-P1；同 persist_if_db 模式）。"""
        self.alarm_inbox.save(self.alarm_inbox_path)

    # -- Confirmed upstream fire intake (2026-10-12) -----------------------

    def handle_confirmed_fire_event(self, message: dict) -> dict:
        """Trusted adapter entry: create a registered event without any live login/start.

        Real upstream authentication/transport remains to be connected. This method
        is not exposed as an anonymous HTTP endpoint. Durable state actions protect
        the registry and source record as one recoverable bundle.
        """
        source = message.get("sourceSystem", "") if isinstance(message, dict) else ""
        result = self.alarm_inbox.receive_confirmed(
            message, registry=self.registry, actor=f"upstream:{source}")
        if result["code"] == "CREATED":
            self._save_registry()
            self._save_alarm_inbox()
        return result

    def handle_alarm_demo_ingest(self) -> str:
        """Explicit simulation of one upstream-confirmed push, never real transport."""
        self._require_command(allow_closed=True)
        from uuid import uuid4
        stamp = uuid4().hex
        now = utc_now_iso()
        from .fire_demo import confirmed_demo_message
        message = confirmed_demo_message(source_record_id=f"demo-{stamp}", timestamp=now)
        result = self.handle_confirmed_fire_event(message)
        if result["code"] not in {"CREATED", "DUPLICATE"}:
            raise PanelActionError(f"模拟推送失败：{result['message']}")
        return result["alarm_id"]

    def handle_alarm_confirm(
        self,
        alarm_id: str,
        *,
        report_no: str,
        name: str = "",
        location: str = "",
    ) -> dict:
        """确认建档：复用手动登记同一 create_event 逻辑（同构事件）+ 审计镜像。"""
        self._require_command(allow_closed=True)
        try:
            alarm = self.alarm_inbox.alarms.get(alarm_id)
            event = self.alarm_inbox.confirm(
                alarm_id,
                registry=self.registry,
                report_no=report_no,
                name=name,
                location=location,
                operator=GENERAL_ENTRY_ID,
            )
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        self._save_alarm_inbox()
        self._mirror_to_event_space(   # 关联事实进事件任务空间哈希链
            event.event_id,
            "ExternalAlarmConfirmed",
            alarm_id=alarm_id,
            source_system=getattr(alarm, "source_system", "") if alarm else "",
            external_no=getattr(alarm, "external_no", "") if alarm else "",
            raw_digest=getattr(alarm, "raw_digest", "") if alarm else "",
            report_no=(report_no or "").strip(),
            operator=GENERAL_ENTRY_ID,
        )
        return {"event_id": event.event_id}

    def handle_alarm_reject(self, alarm_id: str, *, reason: str) -> dict:
        """拒绝报警：原因必填；不建任何事件。留痕在收件箱 JSON（历史+原因）。"""
        self._require_command(allow_closed=True)
        try:
            record = self.alarm_inbox.reject(
                alarm_id, reason=reason, operator=GENERAL_ENTRY_ID)
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self._save_alarm_inbox()
        return {"alarm_id": record.alarm_id}

    def handle_alarm_link(self, alarm_id: str, *, event_id: str) -> dict:
        """重复报警关联已有事件：不重复建档；关联事实镜像进该事件哈希链。"""
        self._require_command(allow_closed=True)
        try:
            record = self.alarm_inbox.link_duplicate(
                alarm_id, registry=self.registry, event_id=event_id,
                operator=GENERAL_ENTRY_ID)
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self._save_alarm_inbox()
        self._mirror_to_event_space(
            record.event_id,
            "ExternalAlarmLinked",
            alarm_id=record.alarm_id,
            source_system=record.source_system,
            external_no=record.external_no,
            raw_digest=record.raw_digest,
            operator=GENERAL_ENTRY_ID,
        )
        return {"alarm_id": record.alarm_id, "event_id": record.event_id}

    def _save_ledger(self) -> None:
        """物资底账落盘（同 persist_if_db 模式）。"""
        self.ledger.save(self.materials_path)

    def member_session(self, token: str | None) -> MemberSession | None:
        return self.member_sessions.get(token or "")

    def authorization_subject(self, token: str | None) -> str:
        """Resolve a V2 subject from a live member token, never from form identity."""
        session = self.member_session(token)
        if session is None or session.phone not in self.registry.library:
            raise PermissionError('A live named member session is required')
        credential = self.registry.credentials.get(session.phone)
        if credential is None or credential.locked:
            raise PermissionError('Member account is unavailable or locked')
        from .resource_directory import enabled
        if not enabled(self.ledger,'people',session.phone): raise PermissionError('人员已停用。')
        return session.phone

    def configure_duty_subjects(self, subjects: tuple[str, ...]) -> None:
        """Install trusted server-side duty grants; never expose this on HTTP."""
        grants = tuple(RoleGrant(phone, Role.DUTY) for phone in subjects)
        self._duty_policy = AuthorizationPolicy(grants)
        self._duty_grants = grants

    def duty_subject(self, token: str | None) -> str:
        """Resolve a live named session with a trusted global duty grant."""
        subject = self.authorization_subject(token)
        if not any(grant.subject_id == subject for grant in self._duty_grants):
            raise PermissionError("事件启动需要已授权值班人员会话。")
        return subject

    def require_duty_start(self, token: str | None, event_id: str) -> str:
        subject = self.duty_subject(token)
        self._duty_policy.require(subject, Action.START_EVENT, event_id=event_id)
        return subject

    def _appointment_space(self, event_id: str, *, for_write: bool = False, history_only: bool = False):
        """Load the event's own snapshot; bind older snapshots only with audit proof."""
        record = self.registry.events.get(event_id)
        if record is None and history_only and not for_write:
            record = self.registry.archived_test_events.get(event_id)
        if record is None:
            raise PanelActionError(f"未知事件：{event_id}")
        if record.status == "registered":
            raise PanelActionError("事件尚未启动，不能登记副总指挥任命。")
        if for_write and record.status == "closed":
            raise PanelActionError("事件已关闭，不能新增任命登记。")
        target = Path(record.snapshot_path) if record.snapshot_path else None
        if target is None or not target.is_file():
            raise PanelActionError("事件任务空间不存在，不能登记任命。")
        from .persistence import load_workspace
        active = (for_write and self.active_event_id == event_id
                  and any(e.kind == "FireEventStarted"
                          and e.payload.get("event_id") == event_id
                          for e in self.workspace.events))
        space = self.workspace if active else load_workspace(target, general_auth=self.auth)
        if space.event_id and space.event_id != event_id:
            raise PanelActionError("事件任务空间归属不符，拒绝登记任命。")
        if not space.event_id:
            if space.verify_audit_chain():
                raise PanelActionError("事件任务空间审计链校验失败。")
            starts = [e for e in space.events if e.kind == "FireEventStarted"]
            if len(starts) != 1 or starts[0].payload.get("event_id") != event_id:
                raise PanelActionError("旧事件任务空间缺少可核验的启动归属。")
            space.event_id = event_id
        return space, target, active

    def _material_closeout_space(self, event_id, actor):
        """Narrow D4 entry for existing receipts/decisions, with event leader proof."""
        _material_custody_receipts.require_closeout_event(self.registry, event_id)
        closed = self.registry.events[event_id].status == 'closed'
        space, target, active = self._appointment_space(event_id, for_write=not closed)
        _material_custody_receipts.require_receiver(
            space, self.ledger, event_id=event_id, actor=actor)
        if closed:
            # Read the durable closed snapshot, then refresh the matching in-memory
            # context after saving. Never switch the selected event implicitly.
            active = (self.active_event_id == event_id and self.workspace.event_id == event_id
                      and any(e.kind == 'FireEventStarted' and e.payload.get('event_id') == event_id
                              for e in self.workspace.events))
        return space, target, active

    def deputy_appointment(self, event_id: str):
        """Read the validated initial appointment, if one exists."""
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            event_grants(space)
            return next((r for r in space.object_revisions.values()
                         if r.reference.object_kind == APPOINTMENT_KIND
                         and r.reference.object_id == Role.DEPUTY.value), None)

    def handle_duty_demo(self, *, duty_token, request_id):
        from .duty_intake import simulate
        return simulate(self,duty_token,request_id)

    def handle_duty_deputy_selection(self,event_id,*,duty_token,subject_id,request_id):
        from .duty_intake import select_and_issue
        return select_and_issue(self,event_id,duty_token,subject_id,request_id)

    def handle_deputy_appointment(self, event_id: str, *, duty_token: str | None,
                                  subject_id: str, evidence: str, request_id: str,
                                  occurred_at: str = ""):
        """Record an already-made offline appointment; this act cannot appoint."""
        self.duty_subject(duty_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = service.record_initial_appointment(
                duty_token, event_id=event_id, role=Role.DEPUTY,
                subject_id=subject_id, evidence=evidence,
                request_id=request_id, occurred_at=occurred_at,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def deputy_events(self, token: str | None) -> list[str]:
        """Event IDs where this named session holds the initial deputy grant."""
        self.authorization_subject(token)
        allowed = []
        with self._event_authority_lock:
            for event_id, record in sorted(self.registry.events.items()):
                if record.status not in {"active", "upgraded"}:
                    continue
                try:
                    space, _, _ = self._appointment_space(event_id)
                    service = EventAuthorizationService(
                        space, resolve_subject=self.authorization_subject,
                        global_grants=self._duty_grants,
                        subject_exists=lambda subject: subject in self.registry.library,
                    )
                    service.require(token, Action.ASSIGN_PERSONNEL, event_id=event_id)
                except (PanelActionError, PermissionError, ValueError):
                    continue
                allowed.append(event_id)
        return allowed

    def professional_duties(self, event_id: str, *, deputy_token: str | None):
        from .professional_assignments import history
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            service = EventAuthorizationService(space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants, subject_exists=lambda subject: subject in self.registry.library)
            service.require(deputy_token, Action.ASSESS_TASK_REVIEW, event_id=event_id)
            return history(space)

    def handle_professional_duty(self, event_id: str, *, deputy_token: str | None, **values):
        from .professional_assignments import record
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            service = EventAuthorizationService(candidate, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants, subject_exists=lambda subject: subject in self.registry.library)
            result = record(candidate, service, self.registry, deputy_token, event_id=event_id, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def task_review_bundles(self, event_id: str, *, deputy_token: str | None):
        """Read audited drafts and current/old classification reports for one event."""
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            service.require(deputy_token, Action.ASSESS_TASK_REVIEW, event_id=event_id)
            result = []
            for saved in space.task_drafts.values():
                bundle = space.task_drafts_for_decision(saved.decision_id, event_id=event_id)
                if bundle is None:
                    continue
                reports = {task.id: _task_review.assessment_for(space, bundle, task.id)
                           for task in bundle.tasks}
                result.append((bundle, reports))
            return result

    def task_release_views(self, member_token):
        from .professional_assignments import grants
        from .task_release import history, releases
        actor = self.authorization_subject(member_token)
        rows = []
        with self._event_authority_lock:
            for event_id in sorted(self.registry.events):
                if self.registry.events[event_id].status == 'registered':
                    continue
                try:
                    space, _, _ = self._appointment_space(event_id)
                    duties = event_grants(space) + grants(space)
                except (ValueError, PanelActionError, OSError):
                    # An unrelated damaged event never supplies authority or
                    # blocks a person's ordinary login and other event work.
                    continue
                if not any(g.subject_id == actor and g.role in {Role.DEPUTY, Role.PROFESSIONAL} for g in duties):
                    continue
                rows.append((event_id, space, history(space), releases(space)))
        return rows

    def supply_event_spaces(self, *, override=None):
        spaces = {}
        for event_id, event in self.registry.events.items():
            if event.status == 'registered':
                continue
            spaces[event_id] = override if override is not None and override.event_id == event_id else self._appointment_space(event_id)[0]
        for event_id in self.registry.archived_test_events:
            spaces[event_id] = self._appointment_space(event_id, history_only=True)[0]
        return spaces

    def material_supply_execution_view(self, event_id, *, member_token):
        from .material_supply_contacts import require_leader
        from .material_supply_execution import inspect
        actor = self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            deputy = event_id in self.deputy_events(member_token)
            if not deputy:
                require_leader(space, self.ledger, event_id=event_id, actor=actor)
            return inspect(self.ledger, self.supply_event_spaces())

    def handle_material_supply_execution(self, event_id, *, member_token, action, **values):
        from . import material_supply_execution as supply
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            ledger = deepcopy(self.ledger)
            spaces = self.supply_event_spaces(override=candidate)
            if action == 'verify_stock':
                result = supply.verify_stock(candidate, ledger, spaces, actor, **values)
            else:
                result = supply.act(candidate, ledger, spaces, actor, registry=self.registry, action=action, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            ledger.save(self.materials_path)
            save_workspace(candidate, target)
            self.ledger = ledger
            if active:
                self.workspace = candidate
            return result

    def handle_deployment_recommendation(self, event_id, *, member_token, **values):
        from . import deployment_recommendations as recommendations
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            service = EventAuthorizationService(candidate, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants, subject_exists=lambda subject: subject in self.registry.library)
            result = recommendations.record(self, candidate, service, member_token, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def handle_task_release_action(self, event_id, *, member_token, action, **values):
        from . import task_release
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            service = EventAuthorizationService(candidate, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants, subject_exists=lambda subject: subject in self.registry.library)
            if action == 'evidence':
                result = task_release.record_evidence(candidate, service, self.registry, member_token, **values)
            elif action == 'publish':
                ledger = deepcopy(self.ledger)
                result = task_release.publish(candidate, service, self.registry, ledger, member_token, **values)
                if candidate.events != space.events:
                    from .material_supply_execution import reserve, publication_plan
                    bundle = task_release._bundle(candidate, values['decision_id'], values['draft_version'])
                    plans, _ = publication_plan(candidate, ledger, bundle)
                    if plans:
                        reserve(candidate, ledger, self.supply_event_spaces(override=candidate), result,
                                self.authorization_subject(member_token))
            else:
                raise ValueError('未知正式任务操作。')
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            if action == 'publish' and ledger.supply_records != self.ledger.supply_records:
                ledger.save(self.materials_path)
            save_workspace(candidate, target)
            if action == 'publish':
                self.ledger = ledger
            if active:
                self.workspace = candidate
            return result

    def handle_material_supply_receipt(self, event_id, *, member_token, **values):
        from .material_supply_receipts import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._material_closeout_space(event_id, actor)
            candidate = deepcopy(space)
            ledger = deepcopy(self.ledger)
            result = record(candidate, ledger, self.supply_event_spaces(override=candidate), actor, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            ledger.save(self.materials_path)
            save_workspace(candidate, target)
            self.ledger = ledger
            if active:
                self.workspace = candidate
            return result

    def handle_task_relations(self, event_id, *, member_token, action, **values):
        from . import task_relations
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            if action == 'relations':
                service = EventAuthorizationService(candidate, resolve_subject=self.authorization_subject,
                    global_grants=self._duty_grants, subject_exists=lambda person: person in self.registry.library)
                result = task_relations.record(candidate, service, member_token, **values)
            else:
                result = task_relations.control(candidate, self.registry, self.ledger, actor, action=action, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def require_material_maintenance(self, member_token):
        from .material_maintenance_authority import require
        actor = self.authorization_subject(member_token)
        return require(self.ledger, self.supply_event_spaces(), self.registry, actor)

    def handle_daily_resources(self, *, member_token, **values):
        from .resource_maintenance import record
        return record(self, member_token, **values)

    def handle_resource_planning(self, event_id, *, member_token, **values):
        from .resource_advisor import record_context
        return record_context(self, member_token, event_id, **values)

    def handle_material_maintenance_authority(self, event_id, *, member_token, **values):
        from .material_maintenance_authority import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate, ledger = deepcopy(space), deepcopy(self.ledger)
            spaces = self.supply_event_spaces(override=candidate)
            result = record(candidate, ledger, spaces, self.registry, actor, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            ledger.save(self.materials_path)
            save_workspace(candidate, target)
            self.ledger = ledger
            if active:
                self.workspace = candidate
            return result

    def member_material_maintenance(self, token):
        try:
            self.require_material_maintenance(token)
            return True
        except PermissionError:
            return False

    def handle_material_closeout_account(self, event_id, *, member_token, **values):
        from .material_closeout_accounts import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._material_closeout_space(event_id, actor)
            candidate = deepcopy(space)
            result = record(candidate, self.ledger, self.supply_event_spaces(override=candidate), self.registry, actor, **values)
            if result.id in space.object_revisions:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def handle_material_catalog(self, *, member_token, **values):
        from .material_catalog import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            grant = self.require_material_maintenance(member_token)
            space, target, active = self._appointment_space(grant.reference.event_id)
            active = self.active_event_id == grant.reference.event_id and self.workspace.event_id == grant.reference.event_id
            candidate, ledger = deepcopy(space), deepcopy(self.ledger)
            result = record(candidate, ledger, self.supply_event_spaces(override=candidate), self.registry, actor, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            ledger.save(self.materials_path)
            save_workspace(candidate, target)
            self.ledger = ledger
            if active:
                self.workspace = candidate
            return result

    def handle_formal_task_feedback(self, event_id, *, member_token, **values):
        from .formal_task_feedback import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            result = record(candidate, self.registry, self.ledger, actor, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def handle_formal_task_receipt(self, event_id, *, member_token, **values):
        from .formal_task_receipts import record
        with self._event_authority_lock:
            actor = self.authorization_subject(member_token)
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            result = record(candidate, self.registry, self.ledger, actor, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def handle_task_review_assessment(self, event_id: str, *, deputy_token: str | None,
                                      decision_id: str, task_id: str,
                                      expected_draft_version: int,
                                      expected_review_version: int,
                                      category: str, reason: str, basis_ref: str,
                                      request_id: str):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _task_review.record_assessment(
                space, service, deputy_token, event_id=event_id,
                decision_id=decision_id, task_id=task_id,
                expected_draft_version=expected_draft_version,
                expected_review_version=expected_review_version,
                category=category, reason=reason, basis_ref=basis_ref,
                request_id=request_id)
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def offline_task_views(self, token: str | None):
        """Show only a named participant's instructions in each event."""
        actor = self.authorization_subject(token)
        result = []
        with self._event_authority_lock:
            for event_id, record in sorted(self.registry.events.items()):
                if record.status == 'registered':
                    continue
                try:
                    space, _, _ = self._appointment_space(event_id)
                    tasks = _offline_tasks.task_state(space, event_id)
                    deputy = any(grant.role == Role.DEPUTY and grant.subject_id == actor
                                 for grant in event_grants(space))
                except (PanelActionError, ValueError):
                    continue
                visible = [task for task in tasks.values()
                           if deputy or actor in {task.recipient_id, task.verifier_id}]
                if visible or (deputy and record.status != 'closed'):
                    eligible = {}
                    for task in visible:
                        if actor == task.recipient_id:
                            try:
                                eligible[task.id] = self._offline_leader(
                                    space, event_id, task.group_id, actor)
                            except ValueError:
                                eligible[task.id] = False
                    reminders = set(_offline_tasks.overdue_reminders(
                        space, event_id, utc_now_iso()))
                    result.append((record, sorted(visible, key=lambda task: task.id),
                                   deputy, eligible, reminders))
        return result

    def _offline_leader(self, space, event_id: str, group_id: str, actor: str) -> bool:
        route = _task_recipients.recipient_route(
            space, event_id, group_id, registry=self.registry, ledger=self.ledger)
        return bool(route.leader_id == actor and not route.issue)

    def _save_offline_space(self, space, target, active):
        from .persistence import save_workspace
        save_workspace(space, target)
        if active:
            self.persist_if_db()

    def handle_offline_task_record(self, event_id: str, *, deputy_token: str | None,
                                   content: str, group_id: str, issued_by: str,
                                   recipient_id: str, verifier_id: str,
                                   occurred_at: str, evidence: str,
                                   known_progress: str, parent_id: str,
                                   request_id: str, due_at: str = ''):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library)
            result = _offline_tasks.record_offline(
                space, service, deputy_token, event_id=event_id,
                content=content, group_id=group_id, issued_by=issued_by,
                recipient_id=recipient_id, verifier_id=verifier_id,
                occurred_at=occurred_at, evidence=evidence,
                known_progress=known_progress, parent_id=parent_id,
                request_id=request_id, due_at=due_at)
            self._save_offline_space(space, target, active)
            return result

    def handle_offline_task_amend(self, event_id: str, *, deputy_token: str | None,
                                  task_id: str, expected_version: int, content: str,
                                  group_id: str, recipient_id: str, verifier_id: str,
                                  occurred_at: str, reason: str, request_id: str,
                                  due_at: str = ''):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library)
            result = _offline_tasks.amend_offline(
                space, service, deputy_token, event_id=event_id,
                task_id=task_id, expected_version=expected_version,
                content=content, group_id=group_id, recipient_id=recipient_id,
                verifier_id=verifier_id, occurred_at=occurred_at,
                reason=reason, request_id=request_id, due_at=due_at)
            self._save_offline_space(space, target, active)
            return result

    def handle_offline_task_control(self, event_id: str, *, deputy_token: str | None,
                                    task_id: str, expected_version: int,
                                    action: str, occurred_at: str, reason: str,
                                    successor_id: str, request_id: str):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library)
            result = _offline_tasks.record_control(
                space, service, deputy_token, event_id=event_id,
                task_id=task_id, expected_version=expected_version,
                action=action, occurred_at=occurred_at, reason=reason,
                successor_id=successor_id, request_id=request_id)
            self._save_offline_space(space, target, active)
            return result

    def handle_offline_task_feedback(self, event_id: str, *, member_token: str | None,
                                     task_id: str, expected_version: int,
                                     action: str, basis: str, request_id: str):
        self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            result = _offline_tasks.record_feedback(
                space, self.authorization_subject, member_token,
                event_id=event_id, task_id=task_id,
                expected_version=expected_version, action=action,
                basis=basis, request_id=request_id,
                is_current_leader=lambda group, actor: self._offline_leader(
                    space, event_id, group, actor))
            self._save_offline_space(space, target, active)
            return result

    def handle_offline_task_close(self, event_id: str, *, member_token: str | None,
                                  task_id: str, expected_version: int,
                                  evidence: str, overall_goal_evidence: str,
                                  request_id: str):
        self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            result = _offline_tasks.verify_close(
                space, self.authorization_subject, member_token,
                event_id=event_id, task_id=task_id,
                expected_version=expected_version, evidence=evidence,
                overall_goal_evidence=overall_goal_evidence,
                request_id=request_id,
                subject_exists=lambda subject: subject in self.registry.library)
            self._save_offline_space(space, target, active)
            return result

    def event_callouts(self, event_id: str) -> dict[str, _callout.Callout]:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return _callout.callout_state(space, event_id)

    def local_notices(self, event_id: str) -> dict[tuple[str, str], _local_notice.LocalNotice]:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return _local_notice.notice_state(space, event_id)

    def member_local_notices(self, token: str | None) -> dict[str, list[_local_notice.LocalNotice]]:
        phone = self.authorization_subject(token)
        mine = {}
        for event_id, record in sorted(self.registry.events.items()):
            if record.status not in {"active", "upgraded"}:
                continue
            found = [notice for notice in self.local_notices(event_id).values()
                     if notice.phone == phone]
            if found:
                mine[event_id] = found
        return mine

    def handle_prepare_local_notice(self, event_id: str, *, duty_token: str | None,
                                    phone: str, local_role: str, channel: str,
                                    request_id: str):
        self.duty_subject(duty_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _local_notice.prepare(
                space, token=duty_token, event_id=event_id, phone=phone,
                local_role=local_role, channel=channel, request_id=request_id,
                registry=self.registry, ledger=self.ledger,
                authorization=service,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def captain_callouts(self, token: str | None) -> list[_callout.Callout]:
        phone = self.authorization_subject(token)
        mine = []
        with self._event_authority_lock:
            for event_id, record in sorted(self.registry.events.items()):
                if record.status not in {"active", "upgraded"}:
                    continue
                try:
                    space, _, _ = self._appointment_space(event_id)
                    callouts = _callout.callout_state(space, event_id)
                except (PanelActionError, ValueError):
                    continue
                mine.extend(c for c in callouts.values() if c.leader_phone == phone)
        return mine

    def handle_select_squads(self, event_id: str, *, deputy_token: str | None,
                             squad_ids, channel: str, request_id: str) -> dict:
        self.authorization_subject(deputy_token)
        from .resource_directory import enabled
        if any(not enabled(self.ledger,'squads',sid) or any(not enabled(self.ledger,'people',p) for p in self.ledger.squads[sid].member_phones) for sid in squad_ids if sid in self.ledger.squads):
            raise ValueError('所选中队或成员已停用，请先核对日常编制。')
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _callout.select_squads(
                space, token=deputy_token, event_id=event_id, squad_ids=squad_ids,
                channel=channel, request_id=request_id, authorization=service,
                squads=self.ledger.squads, library=self.registry.library,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def handle_captain_reply(self, event_id: str, *, captain_token: str | None,
                             squad_id: str, can_depart: str, expected_people: str,
                             eta: str, reason: str, request_id: str) -> dict:
        self.authorization_subject(captain_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            result = _callout.captain_reply(
                space, token=captain_token, event_id=event_id, squad_id=squad_id,
                can_depart=can_depart, expected_people=expected_people,
                eta=eta, reason=reason, request_id=request_id,
                resolve_subject=self.authorization_subject, squads=self.ledger.squads,
                library=self.registry.library,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def handle_callout_simulation_failure(self, event_id: str, *,
                                          deputy_token: str | None, squad_id: str,
                                          reason: str, request_id: str) -> dict:
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _callout.record_simulated_failure(
                space, token=deputy_token, event_id=event_id,
                squad_id=squad_id, reason=reason, request_id=request_id,
                authorization=service,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def captain_loaded_roster(self, event_id: str, squad_id: str):
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return _captain_arrival.loaded_rosters(space, event_id).get(squad_id)

    def handle_captain_scan(self, event_id: str, *, captain_token: str | None,
                            squad_id: str, qr_id: str):
        self.authorization_subject(captain_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            loaded = _captain_arrival.scan_roster(
                space, token=captain_token, event_id=event_id, squad_id=squad_id,
                qr_id=qr_id, registry=self.registry, ledger=self.ledger,
                resolve_subject=self.authorization_subject,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return loaded

    def handle_captain_arrival(self, event_id: str, *, captain_token: str | None,
                               squad_id: str, mode: str, phones: list[str],
                               request_id: str):
        self.authorization_subject(captain_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            flow = _captain_arrival.report_arrival(
                space, token=captain_token, event_id=event_id, squad_id=squad_id,
                mode=mode, phones=phones, request_id=request_id,
                registry=self.registry, ledger=self.ledger,
                resolve_subject=self.authorization_subject,
            )
            self._save_ledger()
            from .persistence import save_workspace
            save_workspace(space, target)
            session = self.member_session(captain_token)
            session.bound_event_id = event_id
            initial = next((r for r in self.ledger.flow_records.values()
                            if r.reference.event_id == event_id
                            and r.reference.object_kind == "team_arrival"
                            and r.reference.object_id == squad_id
                            and r.reference.version == 1), flow)
            session.arrived_at = initial.times.occurred_at or initial.times.recorded_at
            if active:
                self.persist_if_db()
            return flow

    def event_readiness(self, event_id: str) -> dict:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            fixed = _readiness.readiness_state(space, event_id, ledger=self.ledger)
            temporary = _temp_readiness.readiness_state(
                space, event_id, ledger=self.ledger
            )
            if fixed.keys() & temporary.keys():
                raise ValueError("固定中队与临时组状态人员重叠，须人工核对。")
            return fixed | temporary

    def event_temporary_group_leaders(self, event_id: str) -> dict[str, str]:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return _temp_readiness.leader_state(space, event_id, ledger=self.ledger)

    def member_temporary_leadership(self, token: str | None) -> list[tuple[str, str]]:
        phone = self.authorization_subject(token)
        result = []
        for event_id, record in sorted(self.registry.events.items()):
            if record.status not in {"active", "upgraded"}:
                continue
            result.extend((event_id, code) for code, leader in
                          self.event_temporary_group_leaders(event_id).items()
                          if leader == phone)
        return result

    def member_material_closeout_events(self, token: str | None) -> list[str]:
        """Closed-event navigation uses the same current named leader as closeout writes."""
        with self._event_authority_lock:
            phone = self.authorization_subject(token)
            return [event_id for event_id, record in sorted(self.registry.events.items())
                    if record.status == "closed"
                    and self.event_temporary_group_leaders(event_id).get("log-material") == phone]

    def handle_temporary_group_leader(self, event_id: str, *,
                                      deputy_token: str | None, pos_code: str,
                                      phone: str, evidence: str, request_id: str):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _temp_readiness.appoint(
                space, token=deputy_token, event_id=event_id,
                pos_code=pos_code, phone=phone, evidence=evidence,
                request_id=request_id, registry=self.registry,
                ledger=self.ledger, authorization=service,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def handle_temporary_group_readiness(self, event_id: str, *,
                                         leader_token: str | None, pos_code: str,
                                         phones: list[str], status: str, basis: str,
                                         order_id: str, request_id: str):
        self.authorization_subject(leader_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            result = _temp_readiness.report(
                space, token=leader_token, event_id=event_id,
                pos_code=pos_code, phones=phones, status=status, basis=basis,
                order_id=order_id, request_id=request_id,
                registry=self.registry, ledger=self.ledger,
                resolve_subject=self.authorization_subject,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def event_readiness_orders(self, event_id: str) -> list[str]:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return sorted(o.id for o in space.orders.values()
                          if o.status.value in _readiness.ACTIVE_ORDER_STATUSES)

    def handle_captain_readiness(self, event_id: str, *, captain_token: str | None,
                                 squad_id: str, phones: list[str], status: str,
                                 basis: str, order_id: str, request_id: str):
        self.authorization_subject(captain_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            result = _readiness.report(
                space, token=captain_token, event_id=event_id, squad_id=squad_id,
                phones=phones, status=status, basis=basis, order_id=order_id,
                request_id=request_id, registry=self.registry, ledger=self.ledger,
                resolve_subject=self.authorization_subject,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def temporary_people(self, event_id: str) -> dict[str, _temp.TemporaryPerson]:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return _temp.temporary_state(space, event_id)

    def temporary_events(self, token: str | None) -> list[str]:
        phone = self.authorization_subject(token)
        return [event_id for event_id, record in sorted(self.registry.events.items())
                if record.status in {"active", "upgraded"}
                and phone in self.temporary_people(event_id)]

    def handle_temporary_scan(self, event_id: str, *, member_token: str | None,
                              qr_id: str):
        phone = self.authorization_subject(member_token)
        with self._event_authority_lock:
            bound = self._effective_bound_event(self.member_session(member_token))
            if bound and bound != event_id:
                raise PanelActionError("已绑定另一进行中事件，不能跨事件登记临时人员。")
            if any(other != event_id for other in self.temporary_events(member_token)):
                raise PanelActionError("已登记另一进行中事件的临时人员区。")
            space, target, active = self._appointment_space(event_id, for_write=True)
            person = _temp.register(
                space, token=member_token, event_id=event_id, qr_id=qr_id,
                registry=self.registry, ledger=self.ledger,
                resolve_subject=self.authorization_subject,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return person

    def handle_temporary_arrival(self, event_id: str, *, member_token: str | None):
        self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            person = _temp.report_arrival(
                space, token=member_token, event_id=event_id,
                registry=self.registry, resolve_subject=self.authorization_subject,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return person

    def handle_temporary_assign(self, event_id: str, *, deputy_token: str | None,
                                phones: list[str], pos_code: str, request_id: str):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _temp.assign(
                space, token=deputy_token, event_id=event_id, phones=phones,
                pos_code=pos_code, request_id=request_id, registry=self.registry,
                authorization=service,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def event_group_people(self, event_id: str) -> dict[str, _groups.GroupPerson]:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            return _groups.group_people(space, event_id, ledger=self.ledger)

    def event_force_needs(self, event_id: str) -> _force_needs.NeedView:
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            people = _groups.group_people(space, event_id, ledger=self.ledger)
            readiness = (_readiness.readiness_state(space, event_id, ledger=self.ledger)
                         | _temp_readiness.readiness_state(
                             space, event_id, ledger=self.ledger))
            return _force_needs.need_view(
                space, event_id, record=self.registry.events.get(event_id),
                people=people, readiness=readiness,
            )

    def handle_force_needs_review(self, event_id: str, *, deputy_token: str | None,
                                  note: str, expected_fingerprint: str,
                                  request_id: str):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            people = _groups.group_people(space, event_id, ledger=self.ledger)
            readiness = (_readiness.readiness_state(space, event_id, ledger=self.ledger)
                         | _temp_readiness.readiness_state(
                             space, event_id, ledger=self.ledger))
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _force_needs.review(
                space, token=deputy_token, event_id=event_id, note=note,
                expected_fingerprint=expected_fingerprint, request_id=request_id,
                registry=self.registry, people=people, readiness=readiness,
                authorization=service,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def handle_person_group_transfer(self, event_id: str, *,
                                     deputy_token: str | None, phone: str,
                                     to_pos_code: str, request_id: str):
        self.authorization_subject(deputy_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            service = EventAuthorizationService(
                space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants,
                subject_exists=lambda subject: subject in self.registry.library,
            )
            result = _groups.transfer(
                space, token=deputy_token, event_id=event_id, phone=phone,
                to_pos_code=to_pos_code, request_id=request_id,
                registry=self.registry, ledger=self.ledger,
                authorization=service,
            )
            from .persistence import save_workspace
            save_workspace(space, target)
            if active:
                self.persist_if_db()
            return result

    def member_login(self, phone: str, password: str) -> tuple[str | None, str]:
        """Authenticate a field member through the real registry gate.

        fail-closed: every refusal reason from ``verify_member_login`` is shown
        verbatim (01 号 §8 清单)，成功才签发内存会话令牌。首次登录会补开账号
        （写入口令哈希），因此成功后必须落盘注册表。
        """
        try:
            phone_n = normalize_phone(phone)
        except ValueError:
            return None, '账号格式不合法，请输入手机号或 admin。'
        try:
            from .resource_directory import enabled
            if not enabled(self.ledger,'people',phone_n): raise PermissionError('该人员已停用，请联系值班员。')
            phone_n = self.registry.verify_member_login(phone, password)
        except PermissionError as exc:
            return None, str(exc)
        self._save_registry()
        token = _secrets.token_urlsafe(24)
        self.member_sessions[token] = MemberSession(
            phone=phone_n, name=self.registry.library[phone_n].name
        )
        return token, ""

    def member_logout(self, token: str | None) -> None:
        self.member_sessions.pop(token or "", None)
        from .deputy_models import forget
        forget(self, token)

    def _workspace_has_active_binding(self, phone: str) -> bool:
        return any(
            binding.status == "active" and binding.person.phone == phone
            for binding in self.workspace.roster_bindings.values()
        )

    def _effective_bound_event(self, session: MemberSession) -> str:
        """会话绑定优先；会话丢失（重启）时从当前事件空间花名册恢复判定，
        使重新登录后的刷码仍走幂等分支、不重复写记录（01 号 §4.2）。"""
        if session.bound_event_id:
            return session.bound_event_id
        if self.active_event_id and self._workspace_has_active_binding(session.phone):
            return self.active_event_id
        reported = [event_id for event_id, snapshot in self.ledger.snapshots.items()
                    if self.registry.events.get(event_id) is not None
                    and self.registry.events[event_id].status in {"active", "upgraded"}
                    and any(session.phone in squad.arrived_phones
                            for squad in snapshot.squads.values())]
        if len(reported) == 1:
            return reported[0]
        return ""

    def member_can_view_current_event(self, token: str | None) -> bool:
        session = self.member_session(token)
        if session is None or not self.active_event_id:
            return False
        try:
            self.authorization_subject(token)
            return (self._effective_bound_event(session) == self.active_event_id
                    or self.active_event_id in _business.events(self, token))
        except PermissionError:
            return False

    def handle_create_event(
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
        key_targets: str = "",
        lat: str = "",
        lon: str = "",
    ) -> dict:
        """页面录入新火情（初始态固定「登记中」，01 号 §3.1/§3.3）。"""
        self._require_command(allow_closed=True)
        targets = tuple(
            part.strip() for part in (key_targets or "").split(",") if part.strip()
        )
        lat_f, lon_f = parse_coords(lat, lon)
        try:
            record = self.registry.create_event(
                name=name,
                report_no=report_no,
                discovered_at=(discovered_at or "").strip() or utc_now_iso(),
                source=source,
                location=location,
                level=level,
                admin_region=admin_region,
                initial_report=initial_report,
                key_targets=targets,
                created_by=GENERAL_ENTRY_ID,
                lat=lat_f,
                lon=lon_f,
            )
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        return {"event_id": record.event_id}

    def _build_event_space(self, event_id: str) -> ScenarioWorkspace:
        """新建 ForestFireScenario 任务空间并落地 impl/snapshots/{event_id}.db。

        与 handle_reset 同一构造纪律：快照绑定本面板认证指纹后用活会话凭证
        打开，指挥权力仍然只来自真实指挥员入口登录。"""
        from .persistence import save_workspace
        fresh = ScenarioWorkspace('event-' + event_id, event_id=event_id, general_auth=self.auth)
        fresh.append_event('EventWorkspaceCreated', event_id=event_id, source='registered-event')
        fresh._general_entry_fingerprint = self.auth.fingerprint
        fresh._general_entry_unlocked = False
        if self._credentials is not None:
            fresh.unlock_general_entry(self._credentials)
        wb.SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
        target = wb.SNAPSHOT_DIR / f"{_event_slug(event_id)}.db"
        save_workspace(fresh, target)
        self.registry.set_snapshot_path(event_id, str(target))
        return fresh

    def handle_enter_event(self, event_id: str) -> dict:
        """「进入事件」：切换工作区上下文到该事件的独立任务空间。"""
        self._require_command(allow_closed=True)
        record = self.registry.events.get(event_id)
        if record is None:
            raise PanelActionError(f"未知事件：{event_id}")
        if self._credentials is None:
            raise PermissionError("指挥动作需要指挥员入口认证。")
        snapshot = Path(record.snapshot_path) if record.snapshot_path else None
        if snapshot is not None and snapshot.exists():
            from .persistence import load_workspace

            workspace = load_workspace(snapshot, general_auth=self.auth)
            workspace.unlock_general_entry(self._credentials)
        else:
            if record.status != 'registered':
                raise PanelActionError('已有事件任务空间缺失，不能新建替代历史或重开作战，请保留失败来源核对恢复。')
            workspace = self._build_event_space(event_id)
        self.workspace = workspace
        self.active_event_id = event_id
        self._save_registry()
        return {"event_id": event_id, "scenario_id": workspace.scenario_id}

    def handle_event_action(self, event_id: str, action: str, *, qr_id: str = "",
                            duty_token: str | None = None, defer_initial_qr: bool = False) -> dict:
        """生命周期与二维码动作；每个分支都立即落盘注册表 JSON。

        自 P16-T14 起，关键状态流转同步镜像进该事件自己的任务空间哈希链
        （fail-closed：空间不存在则跳过镜像，以注册表 JSON 为准，不伪造历史）。
        """
        actor = (self.require_duty_start(duty_token, event_id)
                 if action == "start" else "")
        if action != "start":
            self._require_command(allow_closed=True)
        try:
            if action == "start":
                pending = self.registry.events.get(event_id)
                if pending is None:
                    raise PanelActionError(f"未知事件：{event_id}")
                if pending.status != "registered":
                    raise PanelActionError(
                        f"只有「登记中」事件可启动处置（当前：{pending.status_word()}）。"
                    )
                # Prepare the task space before changing the event lifecycle.
                # A snapshot build failure must not leave an active event without its task space.
                fresh = self._build_event_space(event_id)  # 启动处置即落地独立任务空间
                confirmed_alarms = []
                for alarm in self.alarm_inbox.alarms.values():
                    if alarm.status not in {"confirmed", "created"} or alarm.event_id != event_id:
                        continue
                    confirmation = next((entry for entry in reversed(alarm.history)
                                         if entry.get("action") in {"confirmed", "auto_created"}), None)
                    if (confirmation is None or not confirmation.get("at")
                            or not confirmation.get("operator")):
                        raise PanelActionError("火情来源历史不完整，不能启动并建立事件审计基线。")
                    confirmed_alarms.append((alarm, confirmation))
                record = self.registry.transition(
                    event_id, "start", operator=actor
                )
                qr = None if defer_initial_qr else self.registry.issue_qr(event_id, issued_by=actor)
                roster = self.registry.rosters.get(event_id)
                fresh.append_event(
                    "FireEventStarted",
                    event_id=event_id,
                    operator=actor,
                    roster_count=len(roster.entries) if roster else 0,
                    at=utc_now_iso(),  # P16-T15：到场时长基准时间
                )
                if qr is not None:
                    fresh.append_event("EventQRIssued", qr_id=qr.qr_id, operator=actor)
                for alarm, confirmation in confirmed_alarms:
                    fresh.append_event(
                        "ExternalFireEventReceived" if alarm.status == "created" else "ExternalAlarmConfirmed", event_id=event_id,
                        alarm_id=alarm.alarm_id, source_system=alarm.source_system,
                        external_no=alarm.external_no, raw_digest=alarm.raw_digest,
                        report_no=record.report_no,
                        operator=confirmation["operator"],
                        confirmed_at=confirmation["at"],
                        source="pre_start_inbox_baseline",
                        data_kind="simulated" if alarm.source_system == "DEMO_CONSOLE"
                        else "reported",
                    )
                from .persistence import save_workspace as _save_ws
                _save_ws(fresh, wb.SNAPSHOT_DIR / f"{_event_slug(event_id)}.db")
                self._save_registry()
                return {"event_id": event_id, "status": record.status, "qr_id": qr.qr_id if qr else ""}
            if action == "upgrade":
                record = self.registry.transition(
                    event_id, "upgrade", operator=GENERAL_ENTRY_ID
                )
                self._mirror_to_event_space(event_id, "FireEventUpgraded")
                self._save_registry()
                return {"event_id": event_id, "status": record.status}
            if action == "close":
                record = self.registry.transition(
                    event_id, "close", operator=GENERAL_ENTRY_ID
                )
                self._mirror_to_event_space(event_id, "FireEventClosed")
                self._save_registry()
                return {"event_id": event_id, "status": record.status}
            if action == "enter":
                self.handle_enter_event(event_id)
                return {"event_id": event_id, "status": "entered"}
            if action == "issue-qr":
                qr = self.registry.issue_qr(event_id, issued_by=GENERAL_ENTRY_ID)
                self._mirror_to_event_space(
                    event_id, "EventQRIssued", qr_id=qr.qr_id)
                self._save_registry()
                return {"qr_id": qr.qr_id, "event_id": event_id}
            if action == "revoke-qr":
                revoked = self.registry.revoke_qr(qr_id, revoked_by=GENERAL_ENTRY_ID)
                self._mirror_to_event_space(
                    getattr(revoked, "event_id", "") or event_id,
                    "EventQRRevoked", qr_id=revoked.qr_id)
                self._save_registry()
                return {"qr_id": revoked.qr_id}
        except (ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        raise PanelActionError(f"未知事件动作：{action}")

    def _mirror_to_event_space(self, event_id: str, kind: str, **payload) -> bool:
        """把注册表侧动作镜像进该事件自己的任务空间哈希链。

        - 该事件正是当前活跃空间 → 直接写内存副本（避免副本分叉）；
        - 否则从快照载入→追加→存回；
        - 快照不存在（如 start 前的名册登记）→ 跳过并以注册表 JSON 为准，
          不为审计而凭空造任务空间；返回是否已镜像。
        """
        record = self.registry.events.get(event_id)
        if record is None:
            return False
        payload = {"event_id": event_id, **payload}
        if self.active_event_id == event_id:
            self.workspace.append_event(kind, **payload)
            self.persist_if_db()
            return True
        snapshot = Path(record.snapshot_path) if record.snapshot_path else None
        if snapshot is None or not snapshot.exists():
            return False
        from .persistence import load_workspace, save_workspace
        space = load_workspace(snapshot, general_auth=self.auth)
        space.append_event(kind, **payload)
        save_workspace(space, snapshot)
        return True

    def handle_roster_add(self, event_id: str, phones) -> int:
        """把勾选的长期人员库成员加入本事件名册（先全量校验再落账）。"""
        self._require_legacy_personnel()
        wanted: list[str] = []
        for raw in phones or []:
            text = (raw or "").strip()
            if text and text not in wanted:
                wanted.append(text)
        if not wanted:
            raise PanelActionError("请先勾选要加入名册的人员。")
        normalized: list[str] = []
        try:
            for phone in wanted:
                phone_n = normalize_phone(phone)
                if phone_n not in self.registry.library:
                    raise PanelActionError(f"{mask_phone(phone_n)} 不在长期人员库，请先补录人员档案。")
                if phone_n not in normalized:
                    normalized.append(phone_n)
        except ValueError as exc:
            raise PanelActionError(str(exc))
        try:
            for phone_n in normalized:
                self.registry.add_roster_entry(event_id, phone_n, added_by=GENERAL_ENTRY_ID)
        except (ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        self._mirror_to_event_space(
            event_id, "RosterEntriesAdded", phones=normalized)
        return len(normalized)

    def handle_roster_all_add(self, event_id: str) -> int:
        self._require_legacy_personnel()
        try:
            count = self.registry.populate_roster_all(event_id, added_by=GENERAL_ENTRY_ID)
        except (ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        if count:
            self._mirror_to_event_space(
                event_id, "RosterEntriesAdded", count=count)
        return count

    def handle_roster_assign(self, event_id: str, phone: str, pos_code: str) -> dict:
        """人工指派岗位（本职未解析时必用；不允许成员自行改岗）。"""
        self._require_legacy_personnel()
        try:
            entry = self.registry.assign_roster_position(event_id, phone, pos_code)
        except (ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        self._mirror_to_event_space(
            event_id, "RosterPositionAssigned",
            phone=entry.phone, pos_code=entry.pos_code)
        return {"phone": entry.phone, "pos_code": entry.pos_code}

    def handle_import_member_library(self, csv_text: str) -> int:
        """长期人员库导入（注册表层跨事件底账；CSV 口径与演练控制台一致）。"""
        self._require_legacy_personnel()
        records, errors = wb.parse_roster_csv(csv_text or "")
        if errors:
            raise PanelActionError("人员库导入未执行，逐行错误：\n" + "\n".join(errors))
        if not records:
            raise PanelActionError("导入批次为空：请粘贴「姓名,手机号,…」CSV 行。")
        try:
            count = self.registry.import_person_library(records)
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        return count

    # -- P16 T5：物资底账维护（doc 04 §10：指挥员/授权维护承担，成员只读）--------

    def handle_material_item(
        self, *, name: str, category: str = "", spec: str = "", unit: str = "",
        default_carry_qty: str = "",
        member_token=None,
    ) -> dict:
        self.require_material_maintenance(member_token)
        raise PermissionError("旧资料写口不保留完整修改版本，请使用长期物资资料维护入口。")

    def handle_material_squad(
        self, *, name: str, member_phones: str = "", headcount_planned: str = "",
        leader_phone: str = "",
        member_token=None,
    ) -> dict:
        self.require_material_maintenance(member_token)
        raise PermissionError("旧资料写口不保留完整修改版本，请使用长期物资资料维护入口。")

    def handle_material_squad_leader(self, squad_id: str, phone: str, *, member_token=None) -> dict:
        raise PermissionError('物资维护权不包含队长指定，请使用具名人员管理入口。')

    def handle_material_carry(
        self, *, squad_id: str, item_id: str, quantity: str = "", member_token=None
    ) -> dict:
        self.require_material_maintenance(member_token)
        raise PermissionError("旧资料写口不保留完整修改版本，请使用长期物资资料维护入口。")

    def handle_material_point(
        self, *, name: str, location: str = "", contact_name: str = "",
        contact_phone: str = "", item_id: str = "", stock: str = "",
        member_token=None,
    ) -> dict:
        self.require_material_maintenance(member_token)
        raise PermissionError("旧资料写口不保留完整修改版本，请使用长期物资资料维护入口。")

    def handle_material_check(self, *, event_id: str, squad_id: str, item_id: str, qty_usable: str, reason: str = "") -> dict:
        raise PermissionError('旧现场核对写口停用，请由现任队长使用具名报告及核验链。')

    def handle_material_consume(self, *, event_id: str, squad_id: str, item_id: str, amount: str) -> dict:
        raise PermissionError('旧现场消耗写口停用，请由现任队长使用具名耗损报告链。')

    def task_material_inputs(self, event_id: str, *, member_token, order_id, item_id):
        from .task_material_inputs import review
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            service = EventAuthorizationService(space, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants, subject_exists=lambda s: s in self.registry.library)
            service.require(member_token, Action.PUBLISH_TASK, event_id=event_id)
            return review(space, self.ledger, event_id=event_id, order_id=order_id, item_id=item_id)

    def handle_task_material_inputs(self, event_id: str, *, member_token, **values):
        from .task_material_inputs import record
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            candidate = deepcopy(space)
            service = EventAuthorizationService(candidate, resolve_subject=self.authorization_subject,
                global_grants=self._duty_grants, subject_exists=lambda s: s in self.registry.library)
            result = record(candidate, self.ledger, self.registry, authorization=service,
                            token=member_token, event_id=event_id, **values)
            if candidate.events == space.events:
                return result
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def handle_radio_material_report(self, capture, *, identity_resolver=None):
        """Internal adapter entry only; HTTP cannot assert trusted radio identity."""
        from .radio_material_reports import ingest, MaterialCapture
        if not isinstance(capture, MaterialCapture):
            raise ValueError('需要明确的对讲物资采集契约。')
        with self._event_authority_lock:
            space, target, active = self._appointment_space(capture.event_id, for_write=True)
            candidate = deepcopy(space)
            ledger_copy = deepcopy(self.ledger)
            result = ingest(candidate, ledger_copy, self.registry, capture,
                            identity_resolver=identity_resolver)
            if ledger_copy.to_dict() == self.ledger.to_dict() and candidate.events == space.events:
                return result
            if ledger_copy.to_dict() != self.ledger.to_dict():
                ledger_copy.save(self.materials_path)
                self.ledger = ledger_copy
            from .persistence import save_workspace
            save_workspace(candidate, target)
            if active:
                self.workspace = candidate
            return result

    def handle_manual_material_loss(
        self, event_id: str, *, captain_token: str | None, squad_id: str,
        item_id: str, loss_id: str, kind: str, amount: str,
        expected_version: str, evidence: str, occurred_at: str,
        reason: str, request_id: str,
    ):
        """A captain's reported loss; ledger and event mirror can repair on retry."""
        self.authorization_subject(captain_token)
        if (not expected_version.isascii() or not expected_version.isdigit()
                or len(expected_version) > 9):
            raise ValueError('物资报告版本不正确，请刷新后重试。')
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            ledger_copy = deepcopy(self.ledger)
            space_copy = deepcopy(space)
            revision = _manual_material_reports.report(
                space_copy, ledger_copy, self.registry, token=captain_token,
                resolve_subject=self.authorization_subject,
                event_id=event_id, squad_id=squad_id, item_id=item_id,
                loss_id=loss_id, kind=kind, amount=float(amount),
                expected_version=int(expected_version), evidence=evidence,
                occurred_at=occurred_at, reason=reason, request_id=request_id,
            )
            if ledger_copy.to_dict() == self.ledger.to_dict() and space_copy.events == space.events:
                return revision
            ledger_copy.save(self.materials_path)
            self.ledger = ledger_copy
            from .persistence import save_workspace
            save_workspace(space_copy, target)
            if active:
                self.workspace = space_copy
                self.persist_if_db()
            return revision

    def handle_manual_material_transfer(
        self, event_id: str, *, member_token: str | None, stage: str,
        transfer_id: str, request_id: str, evidence: str,
        item_id: str = '', from_squad: str = '', to_squad: str = '',
        quantity: str = '', expected_version: str = '', occurred_at: str = '',
        purpose: str = 'transfer', return_for: str = '',
        damaged_quantity: str = '0',
    ):
        """Trusted named-session entry; same request repairs a missing event mirror."""
        self.authorization_subject(member_token)
        if (stage in {'sent', 'received'} and
                (not expected_version.isascii() or not expected_version.isdigit()
                 or len(expected_version) > 9)):
            raise ValueError('调拨版本不正确，请刷新后重试。')
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            ledger_copy = deepcopy(self.ledger)
            space_copy = deepcopy(space)
            revision = _manual_material_transfers.record(
                space_copy, ledger_copy, self.registry,
                token=member_token, resolve_subject=self.authorization_subject,
                event_id=event_id, stage=stage, transfer_id=transfer_id,
                request_id=request_id, evidence=evidence, item_id=item_id,
                from_squad=from_squad, to_squad=to_squad,
                quantity=(float(quantity) if quantity else None),
                expected_version=(int(expected_version) if expected_version else None),
                occurred_at=occurred_at, purpose=purpose, return_for=return_for,
                damaged_quantity=float(damaged_quantity or '0'),
            )
            ledger_copy.save(self.materials_path)
            self.ledger = ledger_copy
            from .persistence import save_workspace

            save_workspace(space_copy, target)
            if active:
                self.workspace = space_copy
                self.persist_if_db()
            return revision

    def material_custody_receipts(self, event_id: str, *, member_token: str | None,
                                 loan_transfer_id: str):
        actor = self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            _material_custody_receipts.require_receiver(
                space, self.ledger, event_id=event_id, actor=actor)
            return _material_custody_receipts.summary(
                space, self.ledger, event_id=event_id, loan_transfer_id=loan_transfer_id)

    def handle_material_custody_receipt(
        self, event_id: str, *, member_token: str | None, loan_transfer_id: str,
        quantity: float, damaged_quantity: float, location: str, custodian: str,
        evidence: str, request_id: str, expected_version: int, occurred_at: str = '',
        corrects_id: str = '', correction_reason: str = '',
    ):
        """Named service only; receipt leaves inventory and liability unchanged."""
        actor = self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, target, active = self._material_closeout_space(event_id, actor)
            space_copy = deepcopy(space)
            result = _material_custody_receipts.record(
                space_copy, self.ledger, self.registry,
                event_id=event_id, token=member_token,
                resolve_subject=self.authorization_subject,
                loan_transfer_id=loan_transfer_id, quantity=quantity,
                damaged_quantity=damaged_quantity, location=location, custodian=custodian,
                evidence=evidence, request_id=request_id, expected_version=expected_version,
                occurred_at=occurred_at, corrects_id=corrects_id, correction_reason=correction_reason)
            if result.id in space.object_revisions:
                return result  # Validated replay must not rewrite persisted snapshots.
            from .persistence import save_workspace
            save_workspace(space_copy, target)
            if active:
                self.workspace = space_copy
                self.persist_if_db()
            return result

    def material_supply_contacts(self, event_id: str, *, member_token: str | None,
                                 item_id: str, source_kind: str, source_id: str):
        actor = self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            _material_supply_contacts.require_leader(
                space, self.ledger, event_id=event_id, actor=actor)
            return _material_supply_contacts.review(space, self.ledger, event_id=event_id,
                item_id=item_id, source_kind=source_kind, source_id=source_id)

    def handle_material_supply_contact(
        self, event_id: str, *, member_token: str | None, item_id: str,
        source_kind: str, source_id: str, expected_source_digest: str,
        available_quantity: float | None, promised_delivery_at: str,
        evidence: str, request_id: str, expected_version: int, occurred_at: str = '',
    ):
        """Append a named reported contact; no task, stock or catalog mutation."""
        self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            space_copy = deepcopy(space)
            result = _material_supply_contacts.record(space_copy, self.ledger, self.registry,
                event_id=event_id, token=member_token, resolve_subject=self.authorization_subject,
                item_id=item_id, source_kind=source_kind, source_id=source_id,
                expected_source_digest=expected_source_digest, available_quantity=available_quantity,
                promised_delivery_at=promised_delivery_at, evidence=evidence, request_id=request_id,
                expected_version=expected_version, occurred_at=occurred_at)
            if result.id in space.object_revisions:
                return result
            from .persistence import save_workspace
            save_workspace(space_copy, target)
            if active:
                self.workspace = space_copy
                self.persist_if_db()
            return result

    def material_supply_draft(self, event_id: str, *, session_token: str | None,
                              decision_id: str, task_id: str, item_id: str):
        with self._event_authority_lock:
            if not self.command_mode(session_token):
                raise PermissionError('补给任务草案核对需要有效的指挥入口会话。')
            space, _, _ = self._appointment_space(event_id)
            return _material_supply_drafts.review(space, self.ledger, event_id=event_id,
                decision_id=decision_id, task_id=task_id, item_id=item_id)

    def handle_material_supply_draft(self, event_id: str, *, session_token: str | None,
                                    decision_id: str, task_id: str, item_id: str,
                                    expected_bundle_version: int, expected_bundle_digest: str,
                                    expected_item_digest: str, expected_version: int,
                                    requested_quantity: float | None, quantity_basis: str,
                                    needed_by: str, evidence: str, request_id: str,
                                    contact_id: str = '', internal_contact_ids: tuple[str, ...] = (),
                                    internal_priority_reason: str = '', destination_hint: str = ''):
        """Couple reported supply evidence to a task version under existing draft authority."""
        with self._event_authority_lock:
            if not self.command_mode(session_token):
                raise PermissionError('补给任务草案编制需要有效的指挥入口会话。')
            if self.active_event_id != event_id or self.workspace.event_id != event_id:
                raise ValueError('请先进入本事件的任务工作区。')
            space, target, active = self._appointment_space(event_id, for_write=True)
            if not active:
                raise ValueError('当前任务工作区与事件不一致。')
            space_copy = deepcopy(space)
            result = _material_supply_drafts.record(space_copy, self.ledger, self.registry,
                event_id=event_id, decision_id=decision_id, task_id=task_id, item_id=item_id,
                expected_bundle_version=expected_bundle_version,
                expected_bundle_digest=expected_bundle_digest, expected_item_digest=expected_item_digest,
                expected_version=expected_version, requested_quantity=requested_quantity,
                quantity_basis=quantity_basis, needed_by=needed_by, evidence=evidence,
                request_id=request_id, contact_id=contact_id, internal_contact_ids=internal_contact_ids,
                internal_priority_reason=internal_priority_reason, destination_hint=destination_hint)
            if result.id in space.object_revisions:
                return result
            from .persistence import save_workspace
            save_workspace(space_copy, target)
            self.workspace = space_copy
            self.persist_if_db()
            return result

    def material_custody_settlement(self, event_id: str, *, member_token: str | None,
                                   loan_transfer_id: str):
        actor = self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            _material_custody_receipts.require_receiver(
                space, self.ledger, event_id=event_id, actor=actor)
            return _material_custody_settlements.summary(
                space, self.ledger, event_id=event_id, loan_transfer_id=loan_transfer_id)

    def _record_material_custody_decision(self, event_id, member_token, operation, **values):
        actor = self.authorization_subject(member_token)
        with self._event_authority_lock:
            space, target, active = self._material_closeout_space(event_id, actor)
            space_copy = deepcopy(space)
            result = operation(space_copy, self.ledger, self.registry, event_id=event_id,
                token=member_token, resolve_subject=self.authorization_subject, **values)
            if result.id in space.object_revisions:
                return result
            from .persistence import save_workspace
            save_workspace(space_copy, target)
            if active:
                self.workspace = space_copy
                self.persist_if_db()
            return result

    def handle_material_custody_verification(
        self, event_id: str, *, member_token: str | None, loan_transfer_id: str,
        receipt_id: str, result: str, conclusion: str, evidence: str, request_id: str,
        expected_version: int, expected_receipt_version: int, occurred_at: str = '',
        corrects_id: str = '', correction_reason: str = '',
    ):
        return self._record_material_custody_decision(event_id, member_token,
            _material_custody_settlements.verify, loan_transfer_id=loan_transfer_id,
            receipt_id=receipt_id, result=result, conclusion=conclusion, evidence=evidence,
            request_id=request_id, expected_version=expected_version,
            expected_receipt_version=expected_receipt_version, occurred_at=occurred_at,
            corrects_id=corrects_id, correction_reason=correction_reason)

    def handle_material_custody_settlement(
        self, event_id: str, *, member_token: str | None, loan_transfer_id: str,
        evidence: str, request_id: str, expected_version: int, expected_receipt_version: int,
        expected_verification_version: int, handling_evidence: str = '',
        handling_conclusion: str = '', occurred_at: str = '',
    ):
        return self._record_material_custody_decision(event_id, member_token,
            _material_custody_settlements.settle, loan_transfer_id=loan_transfer_id,
            evidence=evidence, request_id=request_id, expected_version=expected_version,
            expected_receipt_version=expected_receipt_version,
            expected_verification_version=expected_verification_version,
            handling_evidence=handling_evidence, handling_conclusion=handling_conclusion,
            occurred_at=occurred_at)

    def handle_material_custody_invalidation(self, event_id, *, member_token, **values):
        return self._record_material_custody_decision(event_id, member_token,
            _material_custody_settlements.invalidate, **values)

    def material_withdrawal_review(self, event_id: str, *, captain_token: str | None, squad_id: str):
        actor = self.authorization_subject(captain_token)
        with self._event_authority_lock:
            space, _, _ = self._appointment_space(event_id)
            _material_withdrawals.require_captain(space, self.ledger, self.registry,
                event_id=event_id, squad_id=squad_id, actor=actor)
            return _material_withdrawals.review_withdrawal(
                space, self.ledger, event_id=event_id, squad_id=squad_id)

    def handle_team_material_withdrawal(
        self, event_id: str, *, captain_token: str | None, squad_id: str,
        exceptions: tuple[_material_withdrawals.WithdrawalException, ...] = (),
        borrowed_custody: tuple[_material_withdrawals.BorrowedCustody, ...] = (),
        evidence: str, request_id: str, occurred_at: str = '',
    ):
        """Named team departure; same request repairs its missing event mirror."""
        self.authorization_subject(captain_token)
        with self._event_authority_lock:
            space, target, active = self._appointment_space(event_id, for_write=True)
            ledger_copy = deepcopy(self.ledger)
            space_copy = deepcopy(space)
            revision = _material_withdrawals.report_withdrawal(
                space_copy, ledger_copy, self.registry,
                token=captain_token, resolve_subject=self.authorization_subject,
                event_id=event_id, squad_id=squad_id, exceptions=exceptions,
                borrowed_custody=borrowed_custody,
                evidence=evidence, request_id=request_id, occurred_at=occurred_at,
            )
            if (revision.id in self.ledger.withdrawal_records
                    and space_copy.events == space.events):
                return revision  # Valid original replay must not rewrite any store.
            ledger_copy.save(self.materials_path)
            self.ledger = ledger_copy
            from .persistence import save_workspace

            save_workspace(space_copy, target)
            if active:
                self.workspace = space_copy
                self.persist_if_db()
            return revision

    # -- P16 T18：文件导入流水线（doc 04 §8） ---------------------------

    def _save_imports(self) -> None:
        if self.imports_path is not None:
            self.imports.save(self.imports_path)

    def _require_import_authority(self, data_type, member_token):
        if data_type in {'personnel', 'squad'}:
            self._require_legacy_personnel()
        else:
            self.require_material_maintenance(member_token)
            raise PermissionError('物资导入须使用保留原文件、具名版本和安全回滚的长期物资资料入口。')

    def handle_import_upload(
        self, *, data_type: str, filename: str, content: str, member_token=None
    ) -> dict:
        """上传即完成解析+自动映射：有错→失败（带逐行清单），无错→预览。"""
        self._require_import_authority(data_type, member_token)
        if data_type not in DATA_TYPES:
            raise PanelActionError(f"未知数据类型：{data_type}")
        suffix = (".") + filename.rsplit(".", 1)[1].lower() if "." in filename else ""
        if suffix and suffix not in TEXT_SUFFIXES:
            raise PanelActionError(
                f"{suffix} 需要外部解析引擎（Word/Excel/PDF/图片表格与 OCR 待接入），"
                "当前支持 TXT / CSV 粘贴导入。")
        mapping, rows, errors = parse_and_map(data_type, content)
        batch = self.imports.new_batch(
            data_type=data_type, filename=filename or "pasted.csv",
            file_kind=suffix.lstrip(".") or "csv",
            content_sha256=sha256_text(content),
            uploaded_by="指挥员", uploaded_at=utc_now_iso(),
            status=STATUS_FAILED if errors else STATUS_PREVIEW,
            errors=errors, mapping=mapping, rows=rows)
        self._save_imports()
        return {"batch_id": batch.batch_id, "status": batch.status}

    def handle_import_draft(self, batch_id: str, *, member_token=None) -> dict:
        """生成草稿：对照当前长期库查重/查错，通过才进入待发布。"""
        batch = self.imports.get(batch_id)
        self._require_import_authority(batch.data_type if batch else '', member_token)
        if batch is None or batch.status != STATUS_PREVIEW:
            raise PanelActionError("只有「预览」状态的批次可生成草稿。")
        errs = validate_draft(batch.data_type, batch.rows, self.registry, self.ledger)
        batch.errors = errs
        if not errs:
            batch.status = "已生成草稿"
        else:
            batch.status = "失败"
        self._save_imports()
        return {"status": batch.status, "errors": errs}

    def handle_import_publish(self, batch_id: str, *, member_token=None) -> dict:
        """授权发布：写入长期库 + 留存回滚快照；有活跃事件时镜像进哈希链。"""
        batch = self.imports.get(batch_id)
        self._require_import_authority(batch.data_type if batch else '', member_token)
        if batch is None or batch.status != "已生成草稿":
            raise PanelActionError("只有「已生成草稿」的批次可授权发布。")
        try:
            publish_import(batch, self.registry, self.ledger)
        except (ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        batch.status = "已发布"
        batch.published_by = "指挥员"
        batch.published_at = utc_now_iso()
        batch.rolled_back = False
        self._save_registry()
        self._save_ledger()
        self._save_imports()
        if self.active_event_id:
            self._mirror_to_event_space(
                self.active_event_id, "DataImportPublished",
                batch_id=batch.batch_id, data_type=batch.data_type,
                rows=len(batch.rows), version=batch.version, digest=batch.post_digest)
            # P20：授权发布的数据进入事件空间后跑一次确定性态势扫描（幂等）。
            self.workspace.situation_scan(event_id=self.active_event_id)
            self.persist_if_db()
        return {"version": batch.version}

    def handle_import_rollback(self, batch_id: str, *, member_token=None) -> dict:
        """回滚：恢复发布前快照，批次回到草稿态可修正重发。"""
        batch = self.imports.get(batch_id)
        self._require_import_authority(batch.data_type if batch else '', member_token)
        if batch is None or batch.status != "已发布":
            raise PanelActionError("只有「已发布」批次可回滚。")
        try:
            rollback_import(batch, self.registry, self.ledger)
        except ValueError as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        self._save_ledger()
        self._save_imports()
        if self.active_event_id:
            self._mirror_to_event_space(
                self.active_event_id, "DataImportRolledBack",
                batch_id=batch.batch_id, version=batch.version)
        return {"status": batch.status}

    # -- P16 T6：沙盘（doc 03） ------------------------------------------

    def handle_sand_annotate(
        self,
        *,
        kind: str,
        title: str,
        points: str,
        certainty: str = "",
        note: str = "",
        geo_text: str = "",
        source: AnnotationSource = AnnotationSource.MANUAL,
        details: dict[str, str] | None = None,
        action: str = "draft",
        responsible_unit: str = "",
        responsible_person: str = "",
        source_ref: tuple[str, ...] = (),
        basis: tuple[str, ...] = (),
        route_status: str = "",
        positioning_status: str = "",
        is_demo: bool = False,
        demo_notice: str = "",
        event_id: str = "",
        related_order_id: str = "",
    ) -> str:
        """新增标绘（人工作业）；导入/绘制都不是核验。

        P19：`action=verify` 先过提交核验闸门再写入（闸门不过则一条也不写）；
        战术类型的专属字段进 `details`，不得塞进备注绕过校验。
        """
        self._require_command()
        try:
            kind_enum = AnnotationKind(kind)
        except ValueError:
            raise PanelActionError(f"未知标绘类型：{kind}") from None
        pts_text = (points or "").strip()
        try:
            geo_pts = parse_geo_points(geo_text)
            if pts_text:
                pts = sandtable.parse_points(pts_text)
            elif geo_pts:
                # P18 图上直接绘制：只录地理坐标，不做示意投影。
                pts = ()
            else:
                raise PanelActionError(
                    "请提供坐标：优先用左侧工具图上绘制（自动回填地理坐标），"
                    "或手填示意/地理坐标。"
                )
            clean_details = {}
            for key, value in (details or {}).items():
                text = str(value).strip()
                if not text:
                    continue
                clean_details[str(key)] = text[:500]
            self._check_detail_keys(kind_enum, clean_details)
            from .manual_shapes import validate as validate_manual_style
            validate_manual_style(kind_enum.value, clean_details)
            mark = MapAnnotation(
                id=f"ann-{len(self.workspace.annotations) + 1:03d}",
                kind=kind_enum,
                points=pts,
                title=title.strip() or kind_label(kind_enum),
                status=AnnotationStatus.DRAFT,
                certainty=(certainty or None) or None,
                note=note.strip(),
                source=source,
                geo_points=geo_pts,
                details=clean_details,
                responsible_unit=responsible_unit.strip(),
                responsible_person=responsible_person.strip(),
                source_ref=tuple(s for s in source_ref if s),
                basis=tuple(b for b in basis if b),
                route_status=route_status.strip(),
                positioning_status=positioning_status.strip(),
                is_demo=bool(is_demo),
                demo_notice=demo_notice.strip(),
                event_id=event_id or self.workspace.event_id or self.active_event_id or "",
                related_order_id=related_order_id.strip(),
                created_at=self.workspace._audit_now(),
                updated_at=self.workspace._audit_now(),
            )
            if action == "verify":
                errors = tactical_map.verify_gate(mark)
                if errors:
                    raise PanelActionError("；".join(errors))
            self.workspace.record_annotation(mark)
            if action == "verify":
                self.workspace.set_annotation_status(
                    mark.id, AnnotationStatus.PENDING_REVIEW
                )
        except (ValueError, TypeError) as exc:
            raise PanelActionError(str(exc))
        return mark.id

    def handle_sand_annotation_edit(
        self,
        annotation_id: str,
        *,
        title: str = "",
        note: str = "",
        details: dict[str, str] | None = None,
        responsible_unit: str = "",
        route_status: str = "",
        positioning_status: str = "",
        base_version: str = "",
        as_revision: bool = False,
    ) -> str:
        """标绘修订：已发布对象形成本地修订草稿；版本不一致则拒收。"""
        self._require_command()
        clean = {}
        for key, value in (details or {}).items():
            text = str(value).strip()
            if text:
                clean[str(key)] = text[:500]
        current = self.workspace.annotations.get(annotation_id)
        if current is not None:
            self._check_detail_keys(current.kind, clean)
        try:
            updated = self.workspace.update_annotation(
                annotation_id,
                title=title,
                note=note,
                details=clean,
                responsible_unit=responsible_unit or None,
                route_status=route_status or None,
                positioning_status=positioning_status or None,
                base_version=int(base_version) if str(base_version).strip() else None,
                as_revision=bool(as_revision),
            )
        except (KeyError, ValueError, WorkspaceConflictError) as exc:
            raise PanelActionError(str(exc))
        return updated.id

    @staticmethod
    def _check_detail_keys(kind_enum: AnnotationKind, details: dict[str, str]) -> None:
        """专属字段必须属于本类型：其他类型的字段一律拒收（不吃表单残留）。"""
        if kind_enum not in TACTICAL_KINDS:
            return
        allowed = tactical_map.allowed_detail_keys(kind_enum.value)
        wrong = sorted(k for k in details if k not in allowed)
        if wrong:
            raise PanelActionError(
                f"{tactical_map.kind_word(kind_enum.value)}不适用字段："
                + "、".join(tactical_map.detail_word(w) for w in wrong)
                + "；请确认对象类型后重新填写。"
            )

    def handle_sand_conflict_resolve(
        self, conflict_id: str, *, resolution: str, reason: str
    ) -> str:
        """冲突只能由明确业务结果关闭（定稿 §8.3）。"""
        self._require_command()
        try:
            rec = self.workspace.resolve_conflict(
                conflict_id, by="local-general-entry",
                resolution=resolution, reason=reason,
            )
        except (KeyError, ValueError) as exc:
            raise PanelActionError(str(exc))
        return rec.conflict_id


    def handle_sand_review_current(self) -> str:
        """复核当前态势（docs/20）：指挥员动作；同一数据截止点幂等。

        复核只形成带数据截止点的研判批次与建议，不发布态势、不改命令。
        """
        self._require_command()
        before = self.workspace.version
        batch, created = self.workspace.review_current_situation(
            requested_by=GENERAL_ENTRY_ID,
            event_id=self.workspace.event_id or self.active_event_id or "",
        )
        if created:
            self.persist_if_db()
            head = ("本轮纳入资料未发现需复核的已记录变化；未覆盖信息仍需人工核对。"
                    if batch.outcome == "no_material_change"
                    else f"已纳入 {len(batch.source_change_ids)} 项待复核变化。")
            self._note_advisor(
                "态势复核",
                f"复核批次 {batch.review_batch_id} 已完成（数据截至 "
                f"{self._short_time(batch.cutoff_at)}）；{head}",
            )
        else:
            self._note_advisor(
                "态势复核",
                f"没有新数据：仍为复核批次 {batch.review_batch_id}（"
                f"{self._short_time(batch.requested_at)}），未重复生成结论。",
            )
        return batch.review_batch_id

    @staticmethod
    def _short_time(text: str) -> str:
        return str(text or "").replace("T", " ").split("+")[0][:16]

    def handle_sand_observe(
        self,
        *,
        kind: str,
        title: str,
        geo_text: str,
        observed_at: str,
        source_ref: str,
        positioning_status: str = "",
        details: dict[str, str] | None = None,
        note: str = "",
        is_demo: bool = False,
        reporter: str = "",
    ) -> str:
        """现场观测进入事件空间（docs/20 §9.2 统一数据入口）。

        身份由路由层保证：指挥员会话用本机指挥端署名；成员会话用本人姓名。
        自动发现只产候选与台账，绝不会直接发布。
        """
        reporter = str(reporter or "").strip()
        if not reporter:
            raise PanelActionError("观测必须署名：请确认登录会话后重试。")
        try:
            geo_pts = parse_geo_points(geo_text)
            clean_details = {
                str(k): str(v).strip()[:500]
                for k, v in (details or {}).items() if str(v).strip()
            }
            result = self.workspace.register_observation(
                kind=str(kind or "").strip(),
                title=title.strip(),
                geo_points=geo_pts,
                observed_at=(observed_at or "").strip(),
                source_ref=(source_ref or "").strip(),
                details=clean_details,
                positioning_status=(positioning_status or "").strip(),
                note=note.strip(),
                is_demo=bool(is_demo),
                created_by=reporter,
                event_id=self.workspace.event_id or self.active_event_id or "",
            )
        except (ValueError, TypeError) as exc:
            raise PanelActionError(str(exc))
        self.persist_if_db()
        word = ("已进入待核验" if result["status"] == "pending_review"
                else "专属字段不足，保留草稿")
        self._note_advisor(
            "现场观测",
            f"{reporter} 上报的观测已入事件空间：候选 {result['annotation_id']}（{word}）；"
            f"自动发现 {len(result['change_ids'])} 项变化。",
        )
        return result["annotation_id"]

    def handle_sand_change_action(
        self, change_id: str, *, action: str, reason: str = ""
    ) -> str:
        """变化处理（已阅／按现有资料继续）：只改台账状态，不动态势。"""
        self._require_command()
        try:
            change = self.workspace.handle_situation_change(
                change_id, action=(action or "").strip(),
                by=GENERAL_ENTRY_ID, reason=(reason or "").strip(),
            )
        except (KeyError, ValueError) as exc:
            raise PanelActionError(str(exc))
        self.persist_if_db()
        return change.change_id

    def handle_sand_annotation_geo(
        self, annotation_id: str, geo_text: str
    ) -> str:
        """给既有标绘补录/更新地理坐标（指挥员作业，留审计）。"""
        self._require_command()
        try:
            geo_pts = parse_geo_points(geo_text)
            mark = self.workspace.set_annotation_geo(annotation_id, geo_pts)
        except (KeyError, ValueError, TypeError) as exc:
            raise PanelActionError(str(exc))
        return mark.id

    def handle_sand_annotation_status(self, annotation_id: str, target: str) -> str:
        """标绘核验链流转；verify/publish/withdraw 由工作区内部要指挥员入口。"""
        self._require_command()
        try:
            target_enum = AnnotationStatus(target)
        except ValueError:
            raise PanelActionError(f"未知标绘状态：{target}") from None
        try:
            updated = self.workspace.set_annotation_status(annotation_id, target_enum)
        except (KeyError, ValueError) as exc:
            raise PanelActionError(str(exc))
        if target_enum in {
            AnnotationStatus.VERIFIED,
            AnnotationStatus.PUBLISHED,
            AnnotationStatus.WITHDRAWN,
        }:
            # P20：对象重新过核验链后，旧“待复核”提示自动让位（docs/20 §6）。
            self.workspace.supersede_situation_changes(
                annotation_id, by=GENERAL_ENTRY_ID,
                note=f"对象已{annotation_status_word(updated.status)}",
            )
            self.persist_if_db()
        return updated.status.value

    def _next_annotation_seq(self) -> int:
        seq = 0
        for key in self.workspace.annotations:
            try:
                seq = max(seq, int(key.rsplit("-", 1)[-1]))
            except ValueError:
                continue
        return seq + 1

    def handle_sand_ai_candidate(self) -> str:
        """生成一条显式标注的演示智脑候选（pending_review 入库，doc 03 §10）。

        真实影像分析未接入；候选与真实候选走同一核验链，标题带【演示】前缀。
        坐标为示意坐标，非实测。
        """
        self._require_command()
        seq = self._next_annotation_seq()
        mark = MapAnnotation(
            id=f"ann-{seq:03d}",
            kind=AnnotationKind.FIRE_POINT,
            points=((860.0 + seq * 7.0 % 120, 300.0 + seq * 11.0 % 90),),
            title=f"【演示】疑似火点 {seq:03d}",
            status=AnnotationStatus.PENDING_REVIEW,
            certainty="suspected",
            note="演示数据：模拟无人机影像识别结果，非真实分析",
            source=AnnotationSource.AI_SUGGESTION,
            event_id=self.workspace.event_id or self.active_event_id or "",
        )
        self.workspace.record_annotation(mark)
        return mark.id

    def _note_advisor(self, mtype: str, text: str) -> None:
        """指挥助手系统通知（仅内存留痕；业务事实仍以审计链为准）。"""
        self.advisor_log.append({"type": mtype, "text": text})

    def handle_sand_goal(self, statement: str) -> str:
        """司令提出目标：登记目标框（待确认）；确认走既有 handle_confirm_goal。"""
        self._require_command()
        statement = (statement or "").strip()
        if not statement:
            raise PanelActionError("目标内容不能为空。")
        frame = self.chief.open_goal_frame(self.workspace, statement)
        self.persist_if_db()
        self._note_advisor("目标登记", f"已记录目标草案 {frame.id}，等待司令确认后参谋才展开工作。")
        return frame.id

    def handle_sand_gap_defer(self, gap_id: str) -> str:
        """司令选择「按现有资料继续」：显式留痕，缺口保持开放但不再阻塞研判。"""
        self._require_command()
        gap = self.workspace.information_gaps.get(gap_id)
        if gap is None:
            raise PanelActionError(f"未知信息缺口：{gap_id}")
        if gap.status != InformationGapStatus.OPEN:
            raise PanelActionError(f"缺口 {gap_id} 已关闭。")
        if any(
            e.kind == "InformationGapDeferred"
            and (e.payload or {}).get("gap_id") == gap_id
            for e in self.workspace.events
        ):
            raise PanelActionError("该缺口已按现有资料继续过，不重复登记。")
        self.workspace.append_event(
            "InformationGapDeferred",
            gap_id=gap_id,
            reason="指挥员选择按现有资料继续；受影响方案保留未确认前提标注。",
            operator=GENERAL_ENTRY_ID,
        )
        self.persist_if_db()
        self._note_advisor("信息缺口", f"缺口「{gap.subject}」按现有资料继续（已入审计链）。")
        return gap_id

    def handle_plan_decision(
        self,
        option_id: str,
        action: str,
        *,
        reason: str = "",
        accept_risks: bool = False,
    ) -> str:
        """方案区决断：采纳／否决／暂缓，全部经 authenticated general entry 入链。

        采纳登记了风险的方案时必须显式接受全部风险（doc 11 §7 P3 风险门）。
        """
        self._require_command()
        option = self.workspace.options.get(option_id)
        if option is None:
            raise PanelActionError(f"未知方案：{option_id}")
        if any(
            d.decision_type == DecisionType.REJECT and d.option_id == option_id
            for d in self.workspace.decisions.values()
        ):
            raise PanelActionError(
                f"方案 {option_id} 已被否决；如需重启请基于它自提新方案。"
            )
        dtype = {
            "adopt": DecisionType.ADOPT,
            "reject": DecisionType.REJECT,
            "defer": DecisionType.DEFER,
        }.get(action)
        if dtype is None:
            raise PanelActionError(f"未知决断动作：{action}")
        reason = (reason or "").strip()
        default_reason = {
            DecisionType.ADOPT: f"指挥员采纳「{option.title}」",
            DecisionType.REJECT: f"指挥员否决「{option.title}」",
            DecisionType.DEFER: f"指挥员暂缓「{option.title}」",
        }[dtype]
        if action in {"reject", "defer"} and not reason:
            raise PanelActionError("否决／暂缓必须填写原因。")
        accepted_risks: tuple[str, ...] = ()
        if action == "adopt":
            if option.risks and not accept_risks:
                raise PanelActionError(
                    "该方案登记了已知风险，须勾选「知悉并接受全部风险」后方可采纳。"
                )
            accepted_risks = tuple(option.risks) if accept_risks else ()
        scope = "沙盘指挥" + (f"·{self.active_event_id}" if self.active_event_id else "")
        with self._event_authority_lock:
            try:
                decision = self.workspace.record_decision(
                    dtype, option_id, reason=reason or default_reason, scope=scope,
                    actor_role=DecisionActor.COMMANDER, decided_by=GENERAL_ENTRY_ID,
                    accepted_risks=accepted_risks,
                )
            except PermissionError as exc:
                raise PanelActionError(str(exc)) from None
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
            self.persist_if_db()
            if dtype == DecisionType.ADOPT:
                self._persist_active_task_snapshot()
        word = {
            DecisionType.ADOPT: "采纳",
            DecisionType.REJECT: "否决",
            DecisionType.DEFER: "暂缓",
        }[dtype]
        self._note_advisor("决断完成", f"{word}「{option.title}」已入审计链（{decision.id}）。")
        return decision.id

    def handle_task_draft_edit(self, form: dict[str, list[str]]):
        """Save the visible bundle as one checked revision, including add/remove."""
        self._require_command()

        def one(name: str) -> str:
            values = form.get(name)
            if values is None or len(values) != 1:
                raise PanelActionError('草案表单不完整，请刷新后重试。')
            return values[0]

        decision_id = one('decision_id')
        raw_version = one('expected_version')
        if not raw_version.isascii() or not raw_version.isdigit() or len(raw_version) > 9:
            raise PanelActionError('草案版本不正确，请刷新后重试。')
        action = one('action') if 'action' in form else 'remove'
        if action not in {'save', 'add', 'remove'}:
            raise PanelActionError('未知草案操作。')
        remove_id = one('remove_child') if action == 'remove' else ''
        with self._event_authority_lock:
            event_id = self.active_event_id
            if (not event_id or self.workspace.event_id != event_id
                    or event_id not in self.registry.events):
                raise PanelActionError('当前事件任务空间不匹配，不能编辑草案。')
            try:
                bundle = self.workspace.task_drafts_for_decision(decision_id, event_id=event_id)
                if bundle is None:
                    raise ValueError('本事件没有对应的方案任务草案。')
                common = TaskCommonContent(**{
                    name: one(f'common__{name}') for name in COMMON_LABELS})
                specs = {}
                for task in bundle.tasks:
                    prefix = f'task__{task.id}__'
                    content = TaskCommonContent(**{
                        name: one(prefix + name) for name in COMMON_LABELS})
                    refs = tuple(name for name in COMMON_LABELS
                                 if form.get(f'ref__{task.id}__{name}') == ['1'])
                    specs[task.id] = TaskDraftSpec(
                        **{name: one(prefix + name) for name in TASK_LABELS},
                        content=content, common_fields=refs)
                revised = self.workspace.revise_task_drafts(
                    decision_id, event_id=event_id, expected_version=int(raw_version),
                    common=common, specs=specs, add_child=action == 'add',
                    remove_child_id=remove_id)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
            self.persist_if_db()
            self._persist_active_task_snapshot()
            return revised

    def handle_sand_ask(self, question: str, role: str, mask_phone_fn=None):
        """态势问询：只检索、不改状态；问答留痕仅内存会话内。"""
        text = (question or "").strip()
        if not text:
            raise PanelActionError("问题不能为空")
        event = None
        if self.active_event_id:
            event = self.registry.events.get(self.active_event_id)
        answer = sandtable.answer_inquiry(
            text,
            self.workspace,
            event=event,
            ledger=self.ledger,
            role=role,
            mask_phone_fn=mask_phone_fn,
        )
        self.sand_qa_log.append((text, answer))
        if len(self.sand_qa_log) > 20:
            del self.sand_qa_log[:-20]
        return answer

    def handle_grounded_sand_ask(self, question: str, *, role: str,
                                 session_token: str | None = None,
                                 member_token: str | None = None,
                                 mask_phone_fn=None):
        """HTTP inquiry: live viewer, one event, checked citations and durable log."""
        text = (question or '').strip()
        if not text or len(text) > 2000:
            raise PanelActionError('请输入 1–2000 字的问题。')
        event_id = self.active_event_id
        if (not event_id or event_id not in self.registry.events
                or self.workspace.event_id != event_id
                or self.workspace.verify_audit_chain()):
            raise PanelActionError('请先进入审计有效的当前事件任务空间。')
        if role == 'commander' and self.command_mode(session_token):
            actor = GENERAL_ENTRY_ID
        elif role == 'member':
            try:
                actor = self.authorization_subject(member_token)
                member = self.member_session(member_token)
                if (self._effective_bound_event(member) != event_id
                        and event_id not in self.deputy_events(member_token)):
                    raise PermissionError('成员未获当前事件访问权限。')
            except PermissionError as exc:
                raise PanelActionError(str(exc)) from None
        else:
            raise PanelActionError('需要当前有效会话。')
        prior = self.inquiry_journal.latest(event_id, actor)
        routed = text
        context_id = ''
        primary, _ = sandtable.route_question(text)
        if primary is None and prior and any(word in text for word in
                                              ('刚才', '上述', '这些', '它们', '他们', '继续')):
            previous_topics = prior['answer'].get('matched_topics') or []
            if len(previous_topics) == 1:
                routed += ' ' + prior['question']
                context_id = prior['id']
            else:
                raise PanelActionError('追问指代不明确，请说明要查询的具体主题。')
        deputy = role == 'member' and event_id in self.deputy_events(member_token)
        if sandtable.route_question(routed)[0] == 'gaps' and (deputy or role == 'commander'):
            from .intelligence import answer_decision_gaps
            try:
                answer = answer_decision_gaps(self, event_id)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif '联动' in routed or '实时资料' in routed:
            if role != 'commander':
                raise PanelActionError('联动业务资料需要当前指挥员会话。')
            from .live_context import answer_question
            try:
                answer = answer_question(self, event_id, routed)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif '经验' in routed or '案例' in routed:
            answer = _grounded_inquiry.answer_experience_question(
                self.review_ledger, self.registry, routed)
        elif '地图历史' in routed or '标绘历史' in routed or '地图版本' in routed:
            try:
                answer = _grounded_inquiry.answer_map_history_question(
                    self.workspace, event_id, role=role, question=routed)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif '通话' in routed or '对讲' in routed:
            try:
                answer = _grounded_inquiry.answer_radio_question(
                    self.workspace, event_id, role=role,
                    deputy=deputy, question=routed)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif '方案' in routed and any(word in routed for word in
                                      ('比较', '对比', '区别', '差异')):
            try:
                answer = _grounded_inquiry.answer_plan_comparison(
                    self.workspace, event_id, routed)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif any(word in routed for word in ('指南', '规范', '条例', '方法', '标准', '训练大纲', '手册', 'YJ/T', 'YJT')):
            answer = _local_methods.answer_method_question(routed)
        elif any(word in routed for word in ('人员','到场','签到','多少人','几个人')):
            from .personnel_groups import group_people
            from .sandtable import InquiryAnswer
            try:
                people = group_people(self.workspace, event_id, ledger=self.ledger)
                arrived = sum(1 for row in people.values() if getattr(row, 'arrived', False))
                answer = InquiryAnswer(
                    topic='people',
                    conclusion=f'本事件在册 {len(people)} 人，已到场 {arrived} 人，未到场 {len(people) - arrived} 人。',
                    data_time='按当前登记', sources=(), location='本事件',
                    confidence='登记资料汇总', unconfirmed='到场以本人或队长确认为准。')
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif any(word in routed for word in ('物资','库存','缺口','补给','装备')):
            from .resource_advisor import assess
            from .sandtable import InquiryAnswer
            try:
                advice = assess(self, event_id)
                gaps = [row for row in advice['rows']
                        if (row.get('current_gap') or 0) > 0 or (row.get('forecast_gap') or 0) > 0]
                parts = [f"{row['name']}缺 {row.get('current_gap') or row.get('forecast_gap'):g}{row['unit']}"
                         for row in gaps[:5]]
                conclusion = ('物资缺口：' + '；'.join(parts) + '。') if parts else '当前没有算出明确缺口，但依据不全不代表没有缺口。'
                answer = InquiryAnswer(
                    topic='supply', conclusion=conclusion, data_time='按当前台账', sources=(),
                    location='本事件', confidence='台账汇总', unconfirmed='缺口以实收与需求核定为准。')
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif '任务' in routed:
            try:
                answer = _grounded_inquiry.answer_task_question(
                    self.workspace, event_id, role=role, actor_id=actor, deputy=deputy)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        elif sandtable.route_question(routed)[0] is None and is_observation_question(routed):
            try:
                answer = _grounded_inquiry.answer_observation_question(
                    self.workspace, event_id, role=('commander' if deputy else role), question=routed)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
        else:
            event = self.registry.events[event_id]
            answer = sandtable.answer_inquiry(
                routed, self.workspace, event=event, ledger=self.ledger,
                role=role, mask_phone_fn=mask_phone_fn, event_id=event_id)
        try:
            citations = _grounded_inquiry.validate_sources(
                answer, self.workspace, self.ledger, event_id, role,
                actor_id=actor, deputy=deputy,
                review_ledger=self.review_ledger, registry=self.registry, imagery=self.imagery,state=self)
        except ValueError as exc:
            raise PanelActionError(str(exc)) from None
        self.inquiry_journal.append(
            event_id=event_id, actor_id=actor, role=role, question=text,
            answer=answer, citations=citations, context_id=context_id,
            workspace_version=self.workspace.version)
        return answer

    def model_session(self, session_token: str | None) -> _models.Session:
        with self._model_connections_lock:
            if not self.command_mode(session_token) or session_token not in self.model_connections:
                raise PermissionError('模型连接设置需要有效指挥员会话。')
            return self.model_connections[session_token]

    def handle_model_settings(self, fields: dict[str, str], *, session_token: str | None) -> None:
        session = self.model_session(session_token)
        if not _secrets.compare_digest(fields.get('csrf', ''), session.csrf):
            raise PermissionError('模型设置表单已失效，请重新打开设置页。')
        action = fields.get('action', '')
        if action in {'share-sand', 'stop-sand'}:
            from .sand_command import configure_model_binding
            try:
                configure_model_binding(self, session_token, fields)
            except ValueError as exc:
                raise PanelActionError(str(exc)) from None
            return
        try:
            if action in ('test-cloud', 'test-local'):
                mode = action.split('-')[1]
                connection = getattr(session, mode)
                if connection is None:
                    raise ValueError('请先保存本地模型配置。')
                try:
                    raw = _models.SafeProvider(connection).complete(
                        system='Return only JSON. This is an anonymous connectivity test, not an event.',
                        prompt='Return {"ok":true}.')
                    if json.loads(raw).get('ok') is not True:
                        raise ValueError('服务已响应，但未通过固定 JSON 测试。')
                    status = '固定匿名样例连通测试通过；答案事实质量尚未验收。'
                    if mode == 'local':
                        status += ' 实际本地计算及断公网验收仍待核实。'
                except _models.ConnectionUnavailable as exc:
                    status = '连接测试失败：' + str(exc)
                except Exception:
                    status = '连接或固定 JSON 测试失败；请核对调用许可、凭证、模型及服务状态。'
                # A concurrent edit/logout must not restore old credentials or overwrite new settings.
                with self._model_connections_lock:
                    if self.model_connections.get(session_token) is session:
                        self.model_connections[session_token] = replace(session, **{mode + '_status': status})
                return
            with self._model_connections_lock:
                if self.model_connections.get(session_token) is not session or not self.session_valid(session_token):
                    raise PermissionError('会话或模型配置已变化，请重新打开设置页。')
                updated = _models.update(session, fields, self.registry_path.with_name('model-connections.json'))
                if self.model_connections.get(session_token) is not session:
                    raise PermissionError('会话已退出，请重新登录。')
                self.model_connections[session_token] = updated
                from .sand_command import revoke_model_bindings
                revoke_model_bindings(self, session_token)
                self.deepseek_previews = {k: v for k, v in self.deepseek_previews.items() if v['session'] != session_token}
        except (ValueError, OSError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else '本地非敏感配置保存失败，请重试。'
            raise PanelActionError(message) from None

    def _model_profile(self, session_token: str | None, *, snapshot=None) -> _models.Profile:
        session = snapshot or self.model_session(session_token)
        try:
            connection = session.selected
        except ValueError as exc:
            raise PanelActionError(str(exc)) from None
        # Preserve the dedicated environment-only DeepSeek path until the user configures a profile.
        if session.revision == 0 and session.mode == 'cloud':
            from .settings import load_settings
            settings = load_settings()
            connection = replace(connection, api_key=os.environ.get('DEEPSEEK_API_KEY', ''),
                                 enabled=settings.network_enabled, timeout=settings.llm_timeout_seconds)
        return connection

    def _deepseek_context(self, session_token: str | None) -> str:
        event_id = self.active_event_id
        if (not self.command_mode(session_token) or not event_id
                or event_id not in self.registry.events
                or self.workspace.event_id != event_id
                or self.workspace.verify_audit_chain()):
            raise PanelActionError('需要有效指挥员会话及审计有效的当前事件。')
        return event_id

    def prepare_deepseek_qa(self, inquiry_id: str, *, session_token: str | None) -> dict:
        """Preview the exact JSON request body; no network call occurs here."""
        event_id = self._deepseek_context(session_token)
        model_session = self.model_session(session_token)
        connection = self._model_profile(session_token, snapshot=model_session)
        record = next((r for r in self.inquiry_journal.records
                       if r['id'] == inquiry_id and r['event_id'] == event_id
                       and r['actor_id'] == GENERAL_ENTRY_ID
                       and not r.get('model_meta')), None)
        if record is None:
            raise PanelActionError('当前事件不存在这条本地问答。')
        answer = sandtable.InquiryAnswer(**record['answer'])
        try:
            citations = _grounded_inquiry.validate_sources(
                answer, self.workspace, self.ledger, event_id, 'commander',
                actor_id=GENERAL_ENTRY_ID, review_ledger=self.review_ledger,
                registry=self.registry)
            if list(citations) != record['citations']:
                raise ValueError('问答来源记录已变化。')
            names = tuple(p.name for p in self.registry.library.values() if p.name)
            event = self.registry.events[event_id]
            preview = _deepseek_qa.build_preview(
                record, names=names,
                event_tokens=(event_id, event.name, event.report_no), model=connection.model)
        except ValueError as exc:
            raise PanelActionError(str(exc)) from None
        nonce = _secrets.token_urlsafe(24)
        with self._model_connections_lock:
            if self.model_session(session_token) is not model_session:
                raise PanelActionError('模型配置已变化，请重新预览。')
            self.deepseek_previews[nonce] = {
                'session': session_token, 'event_id': event_id,
                'version': self.workspace.version, 'record_id': inquiry_id,
                'body': preview.request_body, 'source_map': preview.source_map,
                'expires': time.monotonic() + 300,
                'connection_revision': model_session.revision,
                'vendor': connection.vendor, 'mode': model_session.mode,
            }
        return {'token': nonce, 'request_body': preview.request_body,
                'endpoint': connection.endpoint, 'model_label': connection.label,
                'mode': model_session.mode}

    def confirm_deepseek_qa(self, preview_token: str, *, session_token: str | None,
                            provider=None) -> sandtable.InquiryAnswer:
        """Consume one preview, then send exactly its frozen prompts."""
        event_id = self._deepseek_context(session_token)
        connection_session = self.model_session(session_token)
        pending = self.deepseek_previews.pop(preview_token, None)
        if (pending is None or pending['session'] != session_token
                or pending['event_id'] != event_id
                or pending['version'] != self.workspace.version
                or pending.get('connection_revision', 0) != connection_session.revision
                or time.monotonic() > pending['expires']):
            raise PanelActionError('预览已过期或事件已变化，请重新预览。')
        source = next((r for r in self.inquiry_journal.records
                       if r['id'] == pending['record_id'] and r['event_id'] == event_id), None)
        if source is None:
            raise PanelActionError('原始问答不存在，请重新预览。')
        local_answer = sandtable.InquiryAnswer(**source['answer'])
        try:
            citations = _grounded_inquiry.validate_sources(
                local_answer, self.workspace, self.ledger, event_id, 'commander',
                actor_id=GENERAL_ENTRY_ID, review_ledger=self.review_ledger,
                registry=self.registry)
        except ValueError as exc:
            raise PanelActionError(str(exc)) from None
        if list(citations) != source['citations'] or set(pending['source_map'].values()) != set(local_answer.sources):
            raise PanelActionError('来源已变化，请重新预览。')
        if provider is None:
            connection = self._model_profile(session_token, snapshot=connection_session)
            if not connection.enabled:
                raise PanelActionError('当前模型未启用联网或本地调用许可；请进入模型连接设置。')
            if connection.vendor != 'local' and not connection.api_key:
                raise PanelActionError('当前云厂商未配置 Key；请进入模型连接设置。')
            provider = _models.SafeProvider(connection)
        body = pending['body']
        digest = sha256_text(json.dumps(body, ensure_ascii=False, sort_keys=True))
        try:
            raw = provider.complete(system=body['messages'][0]['content'],
                                    prompt=body['messages'][1]['content'])
            parsed = _deepseek_qa.parse_reply(raw, pending['source_map'])
        except Exception as exc:
            self.inquiry_journal.append(
                event_id=event_id, actor_id=GENERAL_ENTRY_ID, role='commander',
                question=source['question'],
                answer=sandtable.InquiryAnswer(
                    'model_error', '模型分析失败；原本地回答保留。',
                    local_answer.data_time, (), '', '未生成模型结论',
                    type(exc).__name__),
                citations=(), context_id=source['id'],
                workspace_version=self.workspace.version,
                model_meta={'provider': pending.get('vendor', 'deepseek'), 'model': body['model'],
                            'mode': pending.get('mode', 'cloud'),
                            'connection_revision': pending.get('connection_revision', 0),
                            'status': 'failed', 'error_type': type(exc).__name__,
                            'request_sha256': digest,
                            'source_inquiry_id': source['id']})
            raise PanelActionError('模型分析失败；原问题和本地回答已保留。请检查连接、接口能力或切换已配置的本地模式。') from None
        sources = tuple(pending['source_map'][x] for x in parsed['source_ids'])
        answer = sandtable.InquiryAnswer(
            'model_analysis', '模型分析·待人工核验：' + parsed['answer'],
            local_answer.data_time, sources, '', '待核验', parsed['unknown'])
        try:
            model_citations = _grounded_inquiry.validate_sources(
                answer, self.workspace, self.ledger, event_id, 'commander',
                actor_id=GENERAL_ENTRY_ID, review_ledger=self.review_ledger,
                registry=self.registry)
        except ValueError as exc:
            raise PanelActionError(str(exc)) from None
        self.inquiry_journal.append(
            event_id=event_id, actor_id=GENERAL_ENTRY_ID, role='commander',
            question=source['question'], answer=answer, citations=model_citations,
            context_id=source['id'], workspace_version=self.workspace.version,
            model_meta={'provider': pending.get('vendor', 'deepseek'), 'model': body['model'],
                        'mode': pending.get('mode', 'cloud'),
                        'connection_revision': pending.get('connection_revision', 0),
                        'status': 'succeeded',
                        'request_sha256': digest, 'source_inquiry_id': source['id']})
        return answer

    # -- P16 T13：语音/文字意图分流（doc 03 §13/§13.2） --------------------

    def _known_unit_names(self) -> tuple[str, ...]:
        """从既有台账收集已知单位名，供草稿字段提示匹配；取不到就空表。"""
        names: list[str] = []
        ledger = getattr(self, "ledger", None)
        for squad in (getattr(ledger, "squads", {}) or {}).values():
            name = getattr(squad, "name", "") or (
                squad if isinstance(squad, str) else ""
            )
            if name:
                names.append(name)
        registry = getattr(self, "registry", None)
        for person in (getattr(registry, "library", {}) or {}).values():
            unit = getattr(person, "unit", "")
            if unit:
                names.append(unit)
        return tuple(dict.fromkeys(names))

    def handle_voice_input(self, text: str, role: str, mask_phone_fn=None,
                           *, session_token: str | None = None,
                           member_token: str | None = None) -> dict:
        """意图分流唯一入口：问询只检索；命令仅指挥员可捕获为草稿。

        命令草稿绝不自动执行；确认动作走指挥员自提方案既有认证门与审计，
        从既有链进入 ExecutionOrder 流程（doc 03 §13.2）。
        """
        text = (text or "").strip()
        if not text:
            raise PanelActionError("输入不能为空")
        drawn = self._try_staff_draw(text, role)
        if drawn is not None:
            return drawn
        kind = intent_router.classify_intent(text)
        if kind != "command":
            answer = (self.handle_grounded_sand_ask(
                text, role=role, session_token=session_token,
                member_token=member_token, mask_phone_fn=mask_phone_fn)
                if session_token or member_token else
                self.handle_sand_ask(text, role=role, mask_phone_fn=mask_phone_fn))
            self.intent_notice = "已按问询处理（只检索、不改状态），回答见下方问询记录。"
            return {"kind": "inquiry", "answer": answer}
        if role != "commander":
            self.intent_notice = (
                "命令口令仅指挥员可用：已拒绝执行；如属工作建议请口头报告指挥员。"
            )
            return {"kind": "refused",
                    "message": self.intent_notice}
        fields = intent_router.parse_command_fields(
            text, self._known_unit_names())
        seq = len(self.voice_drafts) + 1
        draft = {
            "id": f"CD-{seq:03d}",
            "raw_text": text,
            "unit_hint": fields["unit"],
            "area_hint": fields["area"],
            "status": "待确认",
            "option_id": "",
        }
        self.voice_drafts[draft["id"]] = draft
        # 捕获即入哈希链：谁在何时输入了什么指令原文，永久留痕、未执行。
        self.workspace.append_event(
            "CommandDraftCaptured",
            draft_id=draft["id"],
            raw_text=text,
            unit_hint=fields["unit"],
            area_hint=fields["area"],
            captured_by="指挥员",
        )
        self.persist_if_db()
        self.intent_notice = (
            f"命令意图已捕获为草稿 {draft['id']}（未执行）；"
            "请核对执行单位与区域后点「确认转方案」进入指挥链。"
        )
        return {"kind": "command", "draft": draft}

    # 参谋代绘：识别画图意图，但只有事件中心或含糊方位时先要求核查位置。
    # 事件中心不能推导火线、隔离带、火点等对象的精确几何。
    _DRAW_KIND_WORDS = (
        ("firebreak", ("隔火带", "隔离带")),
        ("fire_area", ("火场范围", "火场")),
        ("hazard_zone", ("危险区",)),
        ("main_fireline", ("火线",)),
        ("hotspot", ("火点", "热点", "烟点")),
        ("spread_direction", ("蔓延方向", "蔓延")),
        ("team_position", ("小队位置", "队伍位置")),
        ("team_route", ("小队路线", "行进路线")),
        ("evacuation_route", ("撤离路线", "撤离")),
        ("command_post", ("指挥部", "前指")),
        ("water_source", ("水源", "取水点")),
        ("supply_point", ("物资保障点", "物资点", "物资")),
        ("medical_point", ("医疗",)),
        ("drone_position", ("无人机",)),
        ("protected_target", ("保护目标", "重点目标")),
        ("road_block", ("道路阻断", "阻断点")),
        ("staging_point", ("集结点",)),
        ("control_line", ("封控线", "封控")),
        ("route", ("路线",)),
        ("area", ("区域",)),
    )

    def _try_staff_draw(self, text: str, role: str):
        if role != "commander":
            return None
        if not any(w in text for w in ("画", "标绘", "标注", "标出")):
            return None
        kind = word = ""
        for k, words in self._DRAW_KIND_WORDS:
            hit = next((w for w in words if w in text), "")
            if hit:
                kind, word = k, hit
                break
        if not kind:
            return None
        self.intent_notice = (
            f"已识别「{word}」标绘请求，但位置缺少可核来源。"
            "请在图上标绘具体几何，或提供已登记地点／对象供核对；"
            "本次没有生成候选标绘。"
        )
        return {"kind": "clarification", "message": self.intent_notice}

    def handle_voice_draft_confirm(self, draft_id: str) -> dict:
        """指挥员确认草稿 → 转指挥员自提方案（既有认证门+审计），进指挥链。"""
        self._require_command()
        draft = self.voice_drafts.get(draft_id)
        if draft is None:
            raise PanelActionError(f"指令草稿不存在：{draft_id}")
        if draft["status"] != "待确认":
            raise PanelActionError(
                f"草稿 {draft_id} 已处理（{draft['status']}），不重复确认")
        result = self.handle_propose_option(
            title=f"语音/文字指令·{draft['raw_text'][:18]}",
            summary=draft["raw_text"],
        )
        draft["status"] = "已转自提方案"
        draft["option_id"] = result["option_id"]
        self.workspace.append_event(
            "CommandDraftConfirmed",
            draft_id=draft_id,
            option_id=result["option_id"],
            confirmed_by="指挥员",
        )
        self.persist_if_db()
        self.intent_notice = (
            f"草稿 {draft_id} 已确认为自提方案 {result['option_id']}，"
            "进入决断→命令链；未核实前提由后续政策检查把关。"
        )
        return result

    def handle_voice_draft_discard(self, draft_id: str) -> str:
        self._require_command()
        draft = self.voice_drafts.get(draft_id)
        if draft is None:
            raise PanelActionError(f"指令草稿不存在：{draft_id}")
        if draft["status"] != "待确认":
            raise PanelActionError(
                f"草稿 {draft_id} 已处理（{draft['status']}），不可作废")
        draft["status"] = "已作废"
        self.workspace.append_event(
            "CommandDraftDiscarded",
            draft_id=draft_id,
            discarded_by="指挥员",
        )
        self.persist_if_db()
        self.intent_notice = f"草稿 {draft_id} 已作废（留痕）。"
        return draft_id

    # -- P16 T7：复盘（doc 05） ------------------------------------------

    def _save_reviews(self) -> None:
        self.review_ledger.save(self.reviews_path)

    def _mirror_review(self, kind: str, **payload) -> None:
        """P16-T14：复盘台账关键流转镜像进当前工作区哈希链。

        走 append_event 唯一写入口；台账仍是业务事实源，链上事件是
        防篡改审计副本——谁在何时对哪条记录做了什么，永久可验证。
        """
        if self.workspace.event_id:
            payload = {**payload, 'event_id': self.active_event_id}
        self.workspace.append_event(kind, **payload)
        self.persist_if_db()
        if self.workspace.event_id:
            self._persist_active_task_snapshot()

    def _active_event(self):
        if not self.active_event_id:
            return None
        return self.registry.events.get(self.active_event_id)

    def handle_review_owner_assign(self, phone: str, *, deputy_token=None, evidence='', request_id='', occurred_at='') -> dict:
        if self.workspace.event_id:
            from .review_meeting import service
            import copy
            candidate = copy.deepcopy(self.workspace)
            original = self.workspace
            try:
                self.workspace = candidate
                result = service(self).record_initial_appointment(deputy_token,
                    event_id=self.active_event_id, role=Role.REVIEW_OWNER, subject_id=normalize_phone(phone),
                    evidence=evidence, request_id=request_id, occurred_at=occurred_at)
                self.persist_if_db()
                self._persist_active_task_snapshot()
            except Exception:
                self.workspace = original
                raise
            return {'owner': json.loads(result.content_json)['subject_id']}
        """指派复盘负责人（doc 05 §9）：指挥员专属；入哈希链留痕。"""
        self._require_command()
        phone_n = normalize_phone(phone)
        person = self.registry.library.get(phone_n)
        if person is None:
            raise PanelActionError("该手机号不在长期人员库，无法指派为复盘负责人。")
        self.review_ledger.owner_phone = phone_n
        self.review_ledger.owner_name = person.name
        self._save_reviews()
        if self.active_event_id:
            self.workspace.append_event(
                "ReviewOwnerAssigned",
                phone=phone_n, name=person.name,
                operator=GENERAL_ENTRY_ID, at=utc_now_iso())
            self.persist_if_db()
        return {"owner": phone_n}

    def handle_maintenance_grant(self, phone: str, revoke: bool = False) -> dict:
        """授予/撤销授权维护人员（doc 05 §9：查看、问询、建议）。"""
        self._require_legacy_personnel()
        phone_n = normalize_phone(phone)
        if phone_n not in self.registry.library:
            raise PanelActionError("该手机号不在长期人员库。")
        if revoke:
            self.registry.maintenance_phones.discard(phone_n)
        else:
            self.registry.maintenance_phones.add(phone_n)
        self._save_registry()
        return {"phone": phone_n, "granted": not revoke}

    def handle_account_unlock(self, phone: str) -> dict:
        """解锁成员账号（01 号 §8「锁定 → 解除」）；指挥员专属。"""
        self._require_legacy_personnel()
        try:
            cred = self.registry.unlock_account(phone, operator=GENERAL_ENTRY_ID)
        except (ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_registry()
        return {"phone": cred.phone}

    def handle_review_ask(self, question: str, role: str, mask_phone_fn=None, *, viewer_id='', follow_up=False):
        """复盘问询（doc 05 §11）：只读检索，不写台账、不改任何状态。"""
        text = (question or "").strip()
        if not text:
            raise PanelActionError("问题不能为空")
        if self.workspace.event_id:
            from .review_inquiry import answer as answer_current
            answer = answer_current(self, text, role, viewer_id, follow_up=follow_up,
                                    mask_phone_fn=mask_phone_fn)
            self.review_qa_log.append((text, answer))
            if len(self.review_qa_log) > 20:
                del self.review_qa_log[:-20]
            return answer
        from .review_meeting import scoped_state
        answer = _rvd.answer_review_inquiry(
            text,
            ledger=scoped_state(self).review_ledger,
            workspace=self.workspace,
            event_id=self.active_event_id,
            role=role,
            materials_ledger=self.ledger,
            mask_phone_fn=mask_phone_fn,
        )
        answer = replace(answer, context_event_id=self.active_event_id or '', context_role=role)
        self.review_qa_log.append((text, answer))
        if len(self.review_qa_log) > 20:
            del self.review_qa_log[:-20]
        return answer

    def handle_review_generate(self) -> int:
        """规则驱动的候选生成（问题+根因假设）；已存在同编号的幂等跳过。返回新增数。"""
        self._require_review_actor()
        event = self._active_event()
        added = 0
        issue_ids: list[str] = []
        hyp_ids: list[str] = []
        for issue in _rvd.generate_issue_candidates(self.workspace, event):
            if issue.issue_id in self.review_ledger.issues:
                continue
            self.review_ledger.add_issue(issue)
            issue_ids.append(issue.issue_id)
            added += 1
        for hyp in _rvd.generate_hypothesis_candidates(self.workspace, event):
            if hyp.hyp_id in self.review_ledger.hypotheses:
                continue
            self.review_ledger.add_hypothesis(hyp)
            hyp_ids.append(hyp.hyp_id)
            added += 1
        if issue_ids:
            self._mirror_review("ReviewIssuesGenerated", issue_ids=issue_ids)
        if hyp_ids:
            self._mirror_review(
                "ReviewHypothesesGenerated", hyp_ids=hyp_ids)
        if added:
            self._save_reviews()
        return added

    def handle_review_issue_status(self, issue_id: str, target: str,
                                   reason: str = "", assessment_ref: str = "") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            from .review_causality import decision_basis
            if target not in {'confirmed', 'rejected'}:
                raise PanelActionError('本事件问题结论只允许负责人确认或否决；后续流转走整改验收。')
            try:
                basis = decision_basis(self, 'issue', issue_id, assessment_ref,
                                       reason, positive=target == 'confirmed')
                candidate = deepcopy(self.review_ledger)
                updated = candidate.transition_issue(issue_id, target,
                    operator=self.review_operator(), stamp=utc_now_iso(), reason=reason)
                line = f'{utc_now_iso()} {self.review_operator()}：{target}；依据 {basis.id}；{reason.strip()}'
                updated = replace(updated,
                    confirmed_assessment_ref=basis.id,
                    decision_history=updated.decision_history + (line,))
                candidate.issues[issue_id] = updated
                candidate.save(self.reviews_path)
                self.review_ledger = candidate
            except (_rvd.ReviewDomainError, ValueError, KeyError) as exc:
                raise PanelActionError(str(exc)) from exc
            self._mirror_review('ReviewIssueStatusChanged', issue_id=issue_id,
                                to=target, assessment_ref=basis.id, reason=reason.strip())
            return target
        try:
            updated = self.review_ledger.transition_issue(
                issue_id, target, operator=self.review_operator(),
                stamp=utc_now_iso(), reason=reason,
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewIssueStatusChanged",
            issue_id=issue_id, to=updated.review_status,
            reason=(reason or "").strip())
        return updated.review_status

    def handle_review_issue_reopen(self, issue_id: str, reason: str) -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            from .review_causality import assessments
            current = self.review_ledger.issues.get(issue_id)
            latest = assessments(self).get(f'issue:{issue_id}')
            if (current is None or current.event_id != self.active_event_id or latest is None
                    or latest.id == current.confirmed_assessment_ref):
                raise PanelActionError('本事件问题重开须先保存新的证据核查版本。')
        try:
            updated = self.review_ledger.reopen_issue(
                issue_id, operator=self.review_operator(), stamp=utc_now_iso(), reason=reason,
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewIssueStatusChanged",
            issue_id=issue_id, to=updated.review_status,
            reason=f"重开：{(reason or '').strip()}")
        return updated.review_status

    def handle_review_rect_new(
        self, *, related_id: str = "", measures: str = "",
        owner_unit: str = "", owner_person: str = "",
        priority: str = "中", deadline: str = "", verify_method: str = "",
    ) -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            raise PanelActionError('本事件整改须从已确认问题和根因生成草案。')
        seq = sum(1 for k in self.review_ledger.rects
                  if k.startswith("RECT-")) + 1
        event_id = self.active_event_id or "none"
        task = _rvd.RectTask(
            rect_id=f"RECT-{event_id}-{seq:02d}",
            related_ids=(related_id,) if related_id.strip() else (),
            measures=(measures or "").strip(),
            owner_unit=(owner_unit or "").strip(),
            owner_person=(owner_person or "").strip(),
            priority=priority if priority in {"高", "中", "低"} else "中",
            deadline=(deadline or "").strip(),
            verify_method=(verify_method or "").strip(),
            event_id=event_id,
        )
        if not task.measures:
            raise PanelActionError("整改措施不能为空")
        try:
            self.review_ledger.add_rect(task)
        except _rvd.ReviewDomainError as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review("ReviewRectRecorded", rect_id=task.rect_id)
        return task.rect_id

    def handle_review_rect_status(self, rect_id: str, target: str,
                                  reason: str = "", *,
                                  operator: str = "指挥员") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            raise PanelActionError('本事件整改须由发布、责任人反馈和指定验收入口流转。')
        try:
            updated = self.review_ledger.transition_rect(
                rect_id, target, operator=operator,
                stamp=utc_now_iso(), reason=reason,
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewRectStatusChanged",
            rect_id=rect_id, to=updated.status,
            reason=(reason or "").strip())
        return updated.status

    def handle_review_exp_new(self, content: str, applicability: str = "") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            raise PanelActionError('本事件经验候选须关联已核实来源。')
        seq = sum(1 for k in self.review_ledger.experiences
                  if k.startswith("EXP-")) + 1
        exp = _rvd.Experience(
            exp_id=f"EXP-{self.active_event_id or 'none'}-{seq:02d}",
            content=(content or "").strip(),
            applicability=(applicability or "").strip(),
            source_event_id=self.active_event_id or "none",
        )
        if not exp.content:
            raise PanelActionError("经验内容不能为空")
        try:
            self.review_ledger.add_experience(exp)
        except _rvd.ReviewDomainError as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review("ReviewExperienceRecorded", exp_id=exp.exp_id)
        return exp.exp_id

    def handle_review_exp_status(self, exp_id: str, target: str, *,
                                 operator: str = "指挥员") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            raise PanelActionError('本事件经验确认与停用须核对来源、条件及依据。')
        try:
            updated = self.review_ledger.set_experience_status(
                exp_id, target, operator=operator, stamp=utc_now_iso(),
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewExperienceStatusChanged",
            exp_id=exp_id, to=updated.status)
        return updated.status

    # -- 根因假设（doc 05 §5） ------------------------------------------

    def handle_review_hyp_new(self, statement: str, correlation: str = "弱",
                              evidence: str = "", counter_examples: str = "",
                              to_verify: str = "",
                              related_issues: str = "") -> str:
        self._require_review_actor()
        seq = sum(1 for k in self.review_ledger.hypotheses
                  if k.startswith("HYP-")) + 1
        tv_items = tuple(
            ln.strip() for ln in (to_verify or "").splitlines() if ln.strip()
        )
        ev_items = tuple(
            p.strip() for p in (evidence or "").replace("；", ";").split(";")
            if p.strip()
        )
        rel_items = tuple(
            p.strip() for p in (related_issues or "").replace("，", ",").split(",")
            if p.strip()
        )
        hyp = _rvd.RootCauseHypothesis(
            hyp_id=f"HYP-{self.active_event_id or 'none'}-{seq:02d}",
            statement=(statement or "").strip(),
            correlation=(correlation or "弱").strip(),
            evidence_ids=ev_items,
            counter_examples=(counter_examples or "").strip(),
            to_verify=tv_items,
            related_issues=rel_items,
            origin="manual",
            event_id=self.active_event_id or "none",
        )
        try:
            self.review_ledger.add_hypothesis(hyp)
        except _rvd.ReviewDomainError as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review("ReviewHypothesisRecorded", hyp_id=hyp.hyp_id)
        return hyp.hyp_id

    def handle_review_hyp_status(self, hyp_id: str, target: str,
                                 reason: str = "", assessment_ref: str = "", *,
                                 operator: str = "指挥员") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            from .review_causality import decision_basis, assessments
            if target not in {'verified', 'rejected', 'pending'}:
                raise PanelActionError('未知的本事件根因结论状态。')
            try:
                if target == 'pending':
                    current = self.review_ledger.hypotheses.get(hyp_id)
                    basis = assessments(self).get(f'hypothesis:{hyp_id}')
                    if (current is None or current.event_id != self.active_event_id
                            or basis is None or basis.id == current.confirmed_assessment_ref
                            or not reason.strip()):
                        raise ValueError('重开须有新证据核查版本及负责人理由。')
                else:
                    basis = decision_basis(self, 'hypothesis', hyp_id, assessment_ref,
                                           reason, positive=target == 'verified')
                candidate = deepcopy(self.review_ledger)
                updated = candidate.transition_hypothesis(hyp_id, target,
                    operator=operator, stamp=utc_now_iso(), reason=reason)
                updated = replace(updated,
                    confirmed_assessment_ref=basis.id)
                candidate.hypotheses[hyp_id] = updated
                candidate.save(self.reviews_path)
                self.review_ledger = candidate
            except (_rvd.ReviewDomainError, ValueError, KeyError) as exc:
                raise PanelActionError(str(exc)) from exc
            self._mirror_review('ReviewHypothesisStatusChanged', hyp_id=hyp_id,
                                to=target, assessment_ref=basis.id, reason=reason.strip())
            return target
        try:
            updated = self.review_ledger.transition_hypothesis(
                hyp_id, target, operator=operator,
                stamp=utc_now_iso(), reason=reason,
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewHypothesisStatusChanged",
            hyp_id=hyp_id, to=updated.status,
            reason=(reason or "").strip())
        return updated.status

    def handle_review_hyp_resolve(self, hyp_id: str, item: str,
                                  answer: str, *,
                                  operator: str = "指挥员") -> str:
        self._require_review_actor()
        try:
            self.review_ledger.resolve_verification(
                hyp_id, item, answer,
                operator=operator, stamp=utc_now_iso(),
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewHypothesisVerifiedItem", hyp_id=hyp_id, item=item)
        return hyp_id

    # -- 报告版本（doc 05 §12） ------------------------------------------

    def handle_report_generate(self, *, stage: bool = False) -> str:
        """生成新版本 AI 草稿：旧版本保留不动（新证据 → 新版本）。"""
        self._require_review_actor()
        from .reviewview import export_review_markdown

        event_id = self.active_event_id or "none"
        source_json, source_digest = "", ""
        if self.workspace.event_id:
            from .review_reports import source_snapshot
            if self._active_event().status != 'closed' and not stage:
                raise PanelActionError('事件结束后方可生成正式复盘报告草稿。')
            source_json, source_digest = source_snapshot(self)
        version = self.review_ledger.next_report_version(event_id)
        generated_at = utc_now_iso()
        content_md = export_review_markdown(self)
        if stage:
            content_md = ('> 阶段复盘草稿：仅覆盖生成时已记录过程；事件仍可继续，不代表最终结案。\n\n' + content_md)
        if source_digest:
            content_md += (f'\n- 报告版本：v{version}；生成时间：{generated_at}\n'
                           f'- 来源记录截至：第 {self.workspace.version} 条；精确来源快照单独保存。\n')
        report = _rvd.ReviewReport(
            report_id=f"RPT-{event_id}-v{version}",
            event_id=event_id,
            version=version,
            stage_report=stage,
            content_md=content_md,
            created_at=generated_at,
            source_snapshot_json=source_json,
            source_digest=source_digest,
            source_workspace_version=self.workspace.version,
            source_audit_sequence=max((e.sequence for e in self.workspace.events), default=0),
        )
        try:
            self.review_ledger.add_report(report)
        except _rvd.ReviewDomainError as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewReportVersioned",
            report_id=report.report_id, version=report.version)
        return report.report_id

    def handle_report_edit(self, report_id: str, content_md: str, *,
                           operator: str = "指挥员") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            report = self.review_ledger.reports.get(report_id)
            if report is None or report.event_id != self.active_event_id:
                raise PanelActionError('报告不属于当前事件。')
            operator = self.review_operator()
        try:
            updated = self.review_ledger.edit_report(
                report_id, content_md,
                operator=operator, stamp=utc_now_iso(),
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review("ReviewReportEdited", report_id=report_id)
        return updated.status

    def handle_report_status(self, report_id: str, target: str, *,
                             operator: str = "指挥员") -> str:
        self._require_review_actor()
        if self.workspace.event_id:
            report = self.review_ledger.reports.get(report_id)
            if report is None or report.event_id != self.active_event_id:
                raise PanelActionError('报告不属于当前事件。')
        if self.workspace.event_id and target in {'pending_review', 'published'}:
            from .review_reports import freshness_error
            error = freshness_error(self, report)
            if error:
                raise PanelActionError(error)
            if self._active_event().status != 'closed':
                raise PanelActionError('事件尚未结束，不可审核发布复盘报告。')
        if self.workspace.event_id:
            operator = self.review_operator()
        try:
            updated = self.review_ledger.transition_report(
                report_id, target, operator=operator, stamp=utc_now_iso(),
            )
        except (_rvd.ReviewDomainError, KeyError) as exc:
            raise PanelActionError(str(exc))
        self._save_reviews()
        self._mirror_review(
            "ReviewReportStatusChanged", report_id=report_id, to=updated.status)
        return updated.status

    # -- P16 T8：无人机影像（doc 03 §9） ---------------------------------

    def _save_imagery(self) -> None:
        self.imagery.save(self.imagery_path)

    def handle_image_upload(
        self,
        *,
        source_system: str,
        collected_by: str = "",
        collected_at: str,
        has_spatial: bool,
        spatial_range: str = "",
        geo_precision: str = "",
        filename: str = "",
        payload: bytes = b"",
        note: str = "",
    ) -> str:
        """导入无人机图像：落盘原图 + 登记元数据（有/无空间信息分流）。"""
        self._require_command()
        if not payload:
            raise PanelActionError("请选择图像文件")
        if len(payload) > _img.MAX_UPLOAD_BYTES:
            raise PanelActionError("图像超过 8MB 上限，请压缩后重试")
        clean_name = _img.safe_upload_name(filename)
        sniffed = _img.sniff_image_type(payload)
        if sniffed is None:
            raise PanelActionError("文件内容不是 JPG/PNG 图像（魔数校验失败）")
        event_id = self.active_event_id or "none"
        rel_dir = self.upload_dir / event_id
        rel_dir.mkdir(parents=True, exist_ok=True)
        target = rel_dir / f"{utc_now_iso().replace(':', '')}-{clean_name}"
        target.write_bytes(payload)
        try:
            image = self.imagery.add_image(
                event_id=event_id,
                source_system=source_system,
                collected_by=collected_by,
                collected_at=collected_at,
                uploaded_at=utc_now_iso(),
                image_type=sniffed,
                has_spatial_info=has_spatial,
                spatial_range=spatial_range,
                geo_precision=geo_precision,
                file_path=f"uploads/{event_id}/{target.name}",
                note=note,
            )
        except _img.ImageryError as exc:
            target.unlink(missing_ok=True)  # 元数据拒绝则不留孤儿文件
            raise PanelActionError(str(exc))
        self._save_imagery()
        return image.image_id

    def handle_image_status(self, image_id: str, target: str,
                            spatial_range: str = "") -> str:
        self._require_command()
        try:
            updated = self.imagery.transition(
                image_id, target, operator="指挥员", spatial_range=spatial_range,
            )
        except _img.ImageryError as exc:
            raise PanelActionError(str(exc))
        self._save_imagery()
        return updated.status

    def handle_image_analyze(self, image_id: str) -> str:
        """演示智脑分析：未配准硬拦截；产出候选标注走既有核验链。

        真实影像识别未接入；候选标题带【演示】前缀并关联图像编号。
        """
        self._require_command()
        try:
            self.imagery.transition(image_id, "analyzing")
            image = self.imagery.mark_analyzed(image_id)
        except _img.ImageryError as exc:
            self._save_imagery()  # analyzing 若已推进需落盘，失败也如实保存
            raise PanelActionError(str(exc))
        seq = self._next_annotation_seq()
        mark = MapAnnotation(
            id=f"ann-{seq:03d}",
            kind=AnnotationKind.FIRE_POINT,
            points=((880.0 + seq * 5.0 % 140, 320.0 + seq * 7.0 % 100),),
            title=f"【演示】{image_id} 识别疑似火点 {seq:03d}",
            status=AnnotationStatus.PENDING_REVIEW,
            certainty="suspected",
            note=(
                f"演示数据：模拟对 {image_id} 的影像识别结果，非真实分析；"
                f"空间范围：{image.spatial_range or '未登记'}"
            ),
            source=AnnotationSource.AI_SUGGESTION,
            event_id=self.workspace.event_id or self.active_event_id or "",
        )
        self.workspace.record_annotation(mark)
        self.imagery.link_annotation(image_id, mark.id)
        self.imagery.transition(image_id, "candidate_generated")
        self._save_imagery()
        # P20：新影像候选进入事件空间后，同步跑一次确定性态势扫描（幂等）。
        self.workspace.situation_scan(event_id=self.workspace.event_id or self.active_event_id or "")
        self.persist_if_db()
        return mark.id

    def handle_image_decision(self, image_id: str, decision: str) -> str:
        """指挥员对候选的处理：确认 → 标注转已核验；驳回 → 标注撤回。"""
        self._require_command()
        image = self.imagery.get(image_id)
        if image.status != "candidate_generated":
            raise PanelActionError(
                f"{image_id} 当前状态为「{image.status_word()}」，无候选可处理"
            )
        if decision == "confirm":
            if image.related_annotation_id:
                self.workspace.set_annotation_status(
                    image.related_annotation_id, AnnotationStatus.VERIFIED
                )
            new_status = "confirmed"
        elif decision == "reject":
            if image.related_annotation_id:
                self.workspace.set_annotation_status(
                    image.related_annotation_id, AnnotationStatus.WITHDRAWN
                )
            new_status = "rejected"
        else:
            raise PanelActionError(f"未知处理动作：{decision}")
        updated = self.imagery.transition(image_id, new_status)
        self._save_imagery()
        return updated.status

    def member_scan(self, token: str | None, qr_input: str) -> CheckinOutcome | None:
        """刷码匹配判定（无副作用）；会话缺失返回 None，由路由层送回登录页。"""
        session = self.member_session(token)
        if session is None:
            return None
        if self.temporary_events(token):
            return CheckinOutcome(
                "temporary_personnel",
                "已登记本次临时人员区，请在临时人员页查看编组与到场状态；不能走固定岗位入岗入口。",
            )
        outcome = resolve_checkin(
            self.registry,
            qr_input=qr_input,
            phone=session.phone,
            session_bound_event_id=self._effective_bound_event(session),
        )
        if outcome.kind == OUTCOME_CONFIRM and any(
            sq.leader_phone and session.phone in sq.member_phones
            for sq in self.ledger.squads.values()
        ):
            return CheckinOutcome(
                "squad_captain_flow",
                "固定作战中队仅队长扫码。请在「队长召集回执」进入队长到场页，先载入本队名册再确认到场。",
                event_id=outcome.event_id,
            )
        if outcome.kind == OUTCOME_IDEMPOTENT and not session.bound_event_id:
            # 已在岗但会话丢失：补发会话绑定，不重复写任何记录（01 号 §4.2）。
            session.bound_event_id = outcome.event_id
        return outcome

    def member_confirm(self, token: str | None, qr_input: str) -> dict:
        """本人确认后的入岗落账；只写当前已进入的事件任务空间。"""
        session = self.member_session(token)
        if session is None:
            raise PermissionError("成员会话已失效，请重新登录。")
        if self.temporary_events(token):
            raise PanelActionError(
                "已登记本次临时人员区，不能通过固定岗位入口重复入岗。"
            )
        if any(sq.leader_phone and session.phone in sq.member_phones
               for sq in self.ledger.squads.values()):
            raise PanelActionError(
                "固定作战中队仅队长扫码并确认整队到场；不能通过旧个人入岗入口计入整队物资。"
            )
        outcome = resolve_checkin(
            self.registry,
            qr_input=qr_input,
            phone=session.phone,
            session_bound_event_id=self._effective_bound_event(session),
        )
        if outcome.kind == OUTCOME_IDEMPOTENT:
            return {
                "kind": "idempotent",
                "message": outcome.message,
                "event_id": outcome.event_id,
            }
        if outcome.kind != OUTCOME_CONFIRM:
            return {"kind": "error", "message": outcome.message}
        if self.active_event_id != outcome.event_id:
            raise PanelActionError("事件任务空间未就绪，请指挥部先进场配置。")
        try:
            ensure_person_in_workspace(self.registry, self.workspace, session.phone)
            result = confirm_checkin(
                self.registry,
                outcome,
                phone=session.phone,
                bind_fn=self.workspace.scan_check_in,
                operator="member-panel",
            )
        except (PermissionError, ValueError, KeyError) as exc:
            raise PanelActionError(str(exc))
        session.bound_event_id = outcome.event_id
        session.arrived_at = result["arrived_at"]
        # doc 04 §4：人员刷码 → 匹配所属中队 → 自动载入完整携带清单生成到场快照。
        # 无中队归属的人不生成快照（诚实缺省，不伪造清单）。
        squad_id = next(
            (
                sq.squad_id
                for sq in self.ledger.squads.values()
                if session.phone in sq.member_phones
            ),
            "",
        )
        if squad_id:
            try:
                self.ledger.generate_event_snapshot(
                    outcome.event_id, {session.phone: squad_id}
                )
                self._save_ledger()
            except KeyError:
                pass  # 中队档案异常：跳过快照，不阻断入岗
        self.persist_if_db()
        return {"kind": "onboarded", **result}


from .durable_storage import install_state_actions
install_state_actions(PanelState)


def load_state(
    *,
    db_path: str | Path | None = None,
    demo: bool = False,
    auth: GeneralEntryAuth | None = None,
    duty_subjects: tuple[str, ...] = (),
) -> PanelState:
    if db_path:
        from .persistence import load_workspace

        db = Path(db_path)
        # 侧车数据（注册表/物资/复盘/影像/报警收件箱）与 db 同目录：
        # 演示工作副本可整体复制而不串写共享文件（P17 演示场景修复）。
        side = lambda name: db.parent / name  # noqa: E731
        # Recover bundles before parsing a possibly interrupted SQLite file.
        state = PanelState(
            ScenarioWorkspace('startup-recovery', general_auth=auth),
            db_path=db,
            auth=auth,
            registry_path=side(DEFAULT_REGISTRY_PATH.name),
            materials_path=side(DEFAULT_MATERIALS_PATH.name),
            reviews_path=side(DEFAULT_REVIEWS_PATH.name),
            imagery_path=side(DEFAULT_IMAGERY_PATH.name),
            alarm_inbox_path=side(DEFAULT_ALARM_INBOX_PATH.name),
            duty_subjects=duty_subjects,
        )
        state.workspace = load_workspace(db, general_auth=auth)
        return state
    del demo  # demo mode is the only remaining source below
    from .campaign_runner import DRILL_SECRET, run_campaign

    # 演练态默认用 DRILL_SECRET。但调用方若显式给了口令（--secret / 环境变量），
    # 工作区必须用同一口令建，否则 main() 用该口令解锁时指纹不符，启动即崩。
    secret = auth.reveal_secret() if auth is not None else DRILL_SECRET
    result = run_campaign(secret=secret)
    assert result.workspace is not None
    # The drill workspace is bound to the drill secret; the panel must present
    # the SAME fingerprint to earn command authority over it.
    return PanelState(result.workspace, auth=auth or GeneralEntryAuth(secret=DRILL_SECRET),
                      duty_subjects=duty_subjects)


# ---------------------------------------------------------------------------
# Rendering helpers


_REVERSIBILITY_CHOICES = {
    "reversible": Reversibility.REVERSIBLE,
    "partially_reversible": Reversibility.PARTIALLY_REVERSIBLE,
    "irreversible": Reversibility.IRREVERSIBLE,
}


def report_title(workspace: ScenarioWorkspace) -> str:
    """Deterministic export title shared by the HTTP route and tests."""
    return f"{workspace.scenario_id} 战后报告"


def export_report_markdown(workspace: ScenarioWorkspace) -> str:
    """Read-only export: pure renderer over the current workspace state."""
    return render_after_action_report(workspace, title=report_title(workspace))


def esc(value) -> str:
    return _html.escape(str(value), quote=True)


def _status_chip(status: str) -> str:
    tone = {
        "verified": "ok",
        "confirmed": "ok",
        "succeeded": "ok",
        "active": "run",
        "observed": "info",
        "hypothesis": "info",
        "draft": "info",
        "pending_confirm": "info",
        "paused": "warn",
        "unknown": "dim",
        "open": "warn",
    }.get(status, "dim")
    return f'<span class="chip chip-{tone}">{esc(status)}</span>'


_CSS = """
:root{
  --bg:#101012; --bg2:#16161a; --line:#26262c; --line2:#303038;
  --tx:#e4e4e7; --tx2:#a1a1aa; --tx3:#6b6b76;
  --ac:#3fb950; --ac-dim:#23863620; --warn:#d29922; --bad:#f85149;
  --mono:"Cascadia Mono","JetBrains Mono",Consolas,monospace;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--tx);font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;}
a{color:var(--tx2);text-decoration:none}
a:hover{color:var(--tx)}
.shell{display:grid;grid-template-columns:216px 1fr;min-height:100dvh}
.side{border-right:1px solid var(--line);padding:22px 0;background:var(--bg2)}
.brand{padding:0 22px 18px;border-bottom:1px solid var(--line);margin-bottom:14px}
.brand b{font-size:15px;letter-spacing:.02em;display:block;color:var(--tx)}
.brand span{font-size:11px;color:var(--tx3);letter-spacing:.08em;text-transform:uppercase}
.nav a{display:flex;justify-content:space-between;padding:9px 22px;font-size:13px;border-left:2px solid transparent}
.nav a.on{color:var(--tx);border-left-color:var(--ac);background:#ffffff06}
.main{padding:26px 34px 60px;max-width:1060px}
.head{display:flex;align-items:baseline;gap:14px;border-bottom:1px solid var(--line);padding-bottom:14px;margin-bottom:6px}
.head h1{font-size:19px;font-weight:600;letter-spacing:-.01em}
.head .sub{color:var(--tx3);font-size:12px;font-family:var(--mono)}
.strip{display:flex;gap:18px;flex-wrap:wrap;padding:10px 0 18px;color:var(--tx2);font-size:12px;align-items:center}
.strip b{color:var(--tx);font-weight:600}
.dot{display:inline-block;width:7px;height:7px;border-radius:50%;margin-right:6px;vertical-align:1px}
.dot.ok{background:var(--ac)} .dot.bad{background:var(--bad)}
section{border-top:1px solid var(--line);padding:16px 0 6px;margin-top:8px}
section>h2{font-size:11px;color:var(--tx3);letter-spacing:.14em;text-transform:uppercase;margin-bottom:10px}
.row{display:flex;gap:10px;padding:9px 0;border-bottom:1px solid var(--line);align-items:baseline;flex-wrap:wrap}
.row:last-child{border-bottom:none}
.mono{font-family:var(--mono);font-size:12.5px}
.id{color:var(--ac)} .dim{color:var(--tx3)}
.kv{min-width:170px;color:var(--tx2)}
.chip{font-family:var(--mono);font-size:11px;padding:1px 8px;border-radius:99px;border:1px solid var(--line2);color:var(--tx2)}
.chip-ok{color:var(--ac);border-color:#23863666;background:var(--ac-dim)}
.chip-warn{color:var(--warn);border-color:#9e670033}
.chip-bad{color:var(--bad);border-color:#f8514933}
.chip-run{color:#79c0ff;border-color:#388bfd33}
.chip-info{color:var(--tx2)}
.chip-dim{color:var(--tx3)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--tx3);font-weight:500;font-size:11px;letter-spacing:.1em;text-transform:uppercase;padding:6px 10px 6px 0;border-bottom:1px solid var(--line2)}
td{padding:8px 10px 8px 0;border-bottom:1px solid var(--line);vertical-align:baseline}
td.num,th.num{font-family:var(--mono);text-align:right}
.empty{padding:26px 0;color:var(--tx3);font-size:13px}
.empty b{display:block;color:var(--tx2);font-weight:600;margin-bottom:4px}
.note{color:var(--tx3);font-size:12px;padding-top:6px}
.score{font-family:var(--mono);color:var(--tx2);white-space:pre-wrap}
ul.plain{list-style:none}
ul.plain li{padding:8px 0;border-bottom:1px solid var(--line)}
ul.plain li:last-child{border-bottom:none}
form.inline{display:flex;gap:8px;align-items:center;margin-left:auto}
label.fld{display:block;font-size:11px;color:var(--tx3);letter-spacing:.1em;text-transform:uppercase;margin-bottom:6px}
input[type=password],input[type=text]{background:var(--bg2);border:1px solid var(--line2);color:var(--tx);
  padding:7px 10px;font:13px var(--mono);min-width:230px;border-radius:4px}
input:focus{outline:none;border-color:var(--ac)}
button{background:var(--ac-dim);border:1px solid #23863666;color:var(--ac);padding:7px 16px;
  font:600 13px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;border-radius:4px;cursor:pointer}
button:hover{background:#23863633}
button:active{transform:translateY(1px)}
.err{color:var(--bad);font-size:12px;margin-top:8px}
form.stack{display:block;margin-top:6px}
form.stack label{display:block;margin-bottom:10px}
form.stack input,form.stack select{width:100%;max-width:520px}
a.btn{display:inline-block;background:var(--ac-dim);border:1px solid #23863666;color:var(--ac);padding:7px 16px;font-size:13px;border-radius:4px;margin-right:10px}
.loginbox{max-width:380px;margin-top:30px;border:1px solid var(--line);border-radius:6px;padding:26px;background:var(--bg2)}
.loginbox p{color:var(--tx2);font-size:13px;margin-bottom:16px}
select,textarea{background:var(--bg2);border:1px solid var(--line2);color:var(--tx);
  padding:7px 10px;font:13px var(--mono);border-radius:4px;max-width:520px}
textarea{width:100%}
.prewrap{white-space:pre-wrap}
/* --- P15 T4 workbench (direction C tactical-map surface) ----------------- */
.plain-note{color:#8b8672;font-size:12px;padding:2px 0 8px}
.plain-note b{color:#a1a1aa}
.tactical-stage{position:relative;height:74vh;min-height:560px;background:#ddd6c4;
  border:1px solid #b9b29c;border-radius:4px;overflow:hidden;margin-top:8px}
.mapbase{position:absolute;inset:0;width:100%;height:100%}
.wpanel{position:absolute;background:rgba(247,244,235,.94);border:1px solid #b9b29c;
  border-radius:3px;box-shadow:0 2px 10px rgba(47,51,39,.18);color:#2f3327}
.wpanel h3{font-size:12px;letter-spacing:2px;color:#5d6149;font-weight:700;
  padding:8px 14px 6px;border-bottom:1px solid #cdc7b2;margin:0}
.p-sum{top:14px;left:14px;width:360px}
.p-sum .body{padding:10px 14px}
.metrics{display:flex;border-bottom:1px dashed #cdc7b2;padding-bottom:9px;margin-bottom:6px}
.m{flex:1;text-align:center}
.m+.m{border-left:1px dashed #cdc7b2}
.m .v{font-family:var(--mono);font-size:22px;font-weight:700;color:#4a5240}
.m.hot .v{color:#a63c2a}
.m .l{font-size:11px;color:#8b8672}
.claim-digest .tx{font-size:12px;color:#5d5b4e}
.p-todo{top:14px;right:14px;width:300px}
.wtodo{display:flex;align-items:center;gap:10px;padding:9px 14px;
  border-bottom:1px dashed #cdc7b2;font-size:13.5px;color:#2f3327}
.wtodo:hover{background:#efeadb;color:#2f3327}
.wtodo:last-child{border-bottom:none}
a.wtodo{text-decoration:none}
.wdot{width:9px;height:9px;border-radius:50%;flex:none}
.wdot.red{background:#a63c2a}.wdot.amber{background:#c07f28}.wdot.green{background:#5f7346}
.wtodo .go{color:#4a5240;font-size:12px;white-space:nowrap;margin-left:auto}
.wsteps{position:absolute;left:50%;transform:translateX(-50%);bottom:16px;display:flex;
  background:rgba(247,244,235,.94);border:1px solid #b9b29c;border-radius:3px;
  box-shadow:0 2px 10px rgba(47,51,39,.18);max-width:calc(100% - 28px);overflow-x:auto}
.wstep{padding:9px 20px;font-size:13.5px;color:#8b8672;border-right:1px solid #ded8c5;
  white-space:nowrap}
.wstep:last-child{border-right:none}
.wstep.done{color:#5f7346}
.wstep.on{color:#2f3327;font-weight:700;box-shadow:inset 0 -3px 0 #a63c2a;background:#efeadb}
.wbadge{position:absolute;top:12px;left:50%;transform:translateX(-50%);font-size:11px;
  color:#8b8672;background:rgba(247,244,235,.9);border:1px solid #cdc7b2;
  border-radius:2px;padding:1px 8px;white-space:nowrap}
.wlegend{position:absolute;left:14px;bottom:16px;background:rgba(247,244,235,.94);
  border:1px solid #b9b29c;border-radius:3px;padding:8px 12px;font-size:11.5px;color:#5d6149}
.wlegend span{margin-right:12px;white-space:nowrap}
.lg{display:inline-block;width:10px;height:10px;vertical-align:-1px;margin-right:4px}
.orgtree ul{list-style:none;margin:0;padding-left:22px;border-left:1px dashed var(--line2)}
.orgtree>ul{padding-left:0;border-left:none}
.orgtree li{padding:3px 0}
.orgtree .nodename{color:var(--tx);font-size:13.5px}
.slotrow{display:flex;gap:10px;align-items:baseline;flex-wrap:wrap}
.memberline{color:var(--tx2);font-size:12.5px;list-style:none}
/* --- projector mode (?projector=1): bigger type, heavier lines, contrast -- */
body.projector{--tx2:#cacbd2;--tx3:#9fa0ad}
body.projector .head h1{font-size:27px}
body.projector .nav a,body.projector .strip,body.projector td,body.projector th,
body.projector .row,body.projector .note,body.projector .empty,body.projector .plain-note,
body.projector .memberline,body.projector .score{font-size:18px}
body.projector section>h2{font-size:15px}
body.projector .chip{font-size:15px;padding:2px 10px}
body.projector input,body.projector select,body.projector textarea,body.projector button,
body.projector a.btn{font-size:17px;padding:9px 14px}
body.projector .mono{font-size:16px}
body.projector table td,body.projector table th{border-bottom-width:2px}
body.projector section{border-top-width:2px}
body.projector .tactical-stage,body.projector .wpanel,body.projector .wsteps,
body.projector .wlegend{border-width:2px}
body.projector .wtodo,body.projector .wstep{font-size:18px}
body.projector .wpanel h3{font-size:16px}
/* --- P16 T2 light surfaces (doc 00 V0.2 palette): login / events / roster /
       check-in cards. Strictly class-scoped additions — existing dark view
       classes above are untouched. #F3F5F7 bg / #FFFFFF card / #245B78 primary
       / #D9E0E6 border / 4px radius / 32px buttons; no gradients, no AI boxes. */
.lz-page{background:#F3F5F7;color:#1F2D3A;min-height:100vh;margin:0;padding:0;
  font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.lz-topbar{background:#245B78;color:#fff;display:flex;align-items:center;gap:16px;
  padding:0 20px;height:48px}
.lz-topbar b{font-size:15px;font-weight:600;letter-spacing:.02em}
.lz-topbar a{color:#ffffffcc;font-size:13px;text-decoration:none;padding:13px 2px 11px;
  border-bottom:2px solid transparent}
.lz-topbar a.on{color:#fff;border-bottom-color:#fff;font-weight:600}
.lz-topbar .spacer{margin-left:auto}
.lz-topuser{font-size:13px;color:#ffffffcc;display:flex;align-items:center;gap:8px}
.lz-inlineform{display:inline;margin:0}
.lz-wrap{max-width:1060px;margin:0 auto;padding:20px 16px 60px}
.lz-card{background:#FFFFFF;border:1px solid #D9E0E6;border-radius:4px;padding:16px;margin-bottom:12px}
.lz-card h2{font-size:16px;font-weight:600;margin-bottom:10px;color:#1F2D3A}
.lz-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.lz-note{color:#647382;font-size:12.5px;margin-top:8px}
.lz-dim{color:#647382}
.lz-label{display:block;font-size:13px;color:#1F2D3A;margin-bottom:4px}
.lz-label .req{color:#B84A42;margin-left:2px}
.lz-formrow{margin-bottom:10px}
.lz-input,.lz-select,.lz-textarea{width:100%;background:#fff;border:1px solid #D9E0E6;
  border-radius:4px;padding:5px 10px;height:32px;font:13px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  color:#1F2D3A}
.lz-textarea{height:auto;min-height:72px;font-family:Consolas,"Cascadia Mono",monospace}
.lz-input:focus,.lz-select:focus,.lz-textarea:focus{outline:none;border-color:#245B78}
.lz-btn{display:inline-block;background:#245B78;color:#fff;border:1px solid #245B78;
  border-radius:4px;height:32px;padding:0 12px;font:600 13px/30px -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
  cursor:pointer;text-decoration:none}
.lz-btn:hover{background:#1e4d66}
.lz-btn2{background:#fff;color:#245B78}
.lz-btn2:hover{background:#f0f4f7}
.lz-btn-ok{background:#2F7D68;border-color:#2F7D68;color:#fff}
.lz-btn-ok:hover{background:#276a58}
.lz-btn-danger{background:#fff;color:#B84A42;border-color:#B84A42}
.lz-btn-danger:hover{background:#FBF0EF}
.lz-topbtn{background:transparent;color:#fff;border-color:#ffffff88;font-weight:400}
.lz-topbtn:hover{background:#ffffff22}
.lz-deputy-page fieldset{border:1px solid #D9E0E6;border-radius:4px;padding:12px;min-width:0;margin:12px 0}
.lz-deputy-page fieldset .lz-input{box-sizing:border-box}
@media(max-width:620px){.lz-deputy-page .lz-topbar{height:auto;display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:4px 8px;padding:10px 16px}.lz-deputy-page .lz-topbar>b{grid-column:1/-1;font-size:14px}.lz-deputy-page .lz-topbar>a{text-align:center;padding:8px 2px;font-size:12px}.lz-deputy-page .lz-topbar .spacer{display:none}.lz-deputy-page .lz-topuser{grid-column:1/-1;justify-content:flex-end;flex-wrap:wrap;min-width:0}}
@media(max-width:620px){.lz-captain-page .lz-topbar{height:auto;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 8px;padding:10px 16px}.lz-captain-page .lz-topbar>b{grid-column:1/-1;font-size:14px}.lz-captain-page .lz-topbar>a{text-align:center;padding:8px 2px;font-size:12px}.lz-captain-page .lz-topbar .spacer{display:none}.lz-captain-page .lz-topuser{grid-column:1/-1;justify-content:flex-end;flex-wrap:wrap;min-width:0}}
.lz-link{color:#245B78;text-decoration:none}
.lz-link:hover{text-decoration:underline}
.lz-table{width:100%;border-collapse:collapse;font-size:13px;background:#fff}
.lz-table th{background:#F3F5F7;text-align:left;font-weight:600;color:#1F2D3A;
  padding:7px 10px;border-bottom:1px solid #D9E0E6;white-space:nowrap}
.lz-table td{padding:7px 10px;border-bottom:1px solid #D9E0E6;vertical-align:top}
.lz-table tr:hover td{background:#F3F5F7}
.lz-alert{border:1px solid #D9E0E6;border-left:3px solid #647382;background:#F3F5F7;
  border-radius:4px;padding:8px 12px;font-size:13px;margin-bottom:10px}
.lz-alert.ok{background:#F0F7F4;border-left-color:#2F7D68;color:#22604f}
.lz-alert.warn{background:#FBF6EC;border-left-color:#B77A1A;color:#8a5c14}
.lz-alert.bad{background:#FBF0EF;border-left-color:#B84A42;color:#8f3730}
.lz-chip{display:inline-block;font-size:11px;border-radius:99px;padding:1px 8px;border:1px solid #D9E0E6;white-space:nowrap}
.lz-chip-reg{color:#647382;background:#F3F5F7;border-color:#D9E0E6}
.lz-chip-active{color:#2F7D68;background:#F0F7F4;border-color:#2F7D6855}
.lz-chip-upgraded{color:#B77A1A;background:#FBF6EC;border-color:#B77A1A55}
.lz-chip-closed{color:#647382;background:#F3F5F7;border-color:#D9E0E6;text-decoration:line-through}
.lz-empty{border:1px dashed #D9E0E6;border-radius:4px;color:#647382;padding:18px 14px;
  font-size:13px;background:#fff;margin-bottom:12px}
.lz-mono{font-family:Consolas,"Cascadia Mono",monospace;font-size:12.5px}
.lz-kv{color:#647382;display:inline-block;min-width:96px}
.lz-actions{display:inline-flex;gap:6px;flex-wrap:wrap;align-items:center}
.lz-unresolved{color:#B84A42}
/* --- P17 五页业务面布局：沙盘三栏 / 复盘两栏（页壳统一定义，各视图不重复写） -- */
.lz-wrap:has(.sand-page),.lz-wrap:has(.cmd-page){max-width:1680px}
 .lz-wrap:has(.cmd-page){padding-bottom:12px}
.sand-grid{display:grid;grid-template-columns:340px minmax(0,1fr) 390px;gap:12px;align-items:start}
.sand-col,.sand-left,.sand-center,.sand-right{display:flex;flex-direction:column;gap:12px;min-width:0}
.sand-page section,.sand-page aside{background:#fff;border:1px solid #D9E0E6;border-radius:4px;padding:12px 14px}
.sand-page h3{font-size:14px;margin-bottom:8px;color:#1F2D3A}
.sand-page h4{font-size:13px;margin:10px 0 4px;color:#1F2D3A}
.sand-page table{width:100%;border-collapse:collapse;font-size:12.5px}
.sand-page th{text-align:left;color:#647382;font-weight:600;padding:4px 8px 4px 0;border-bottom:1px solid #D9E0E6}
.sand-page td{padding:5px 8px 5px 0;border-bottom:1px solid #EDF1F4;vertical-align:top}
.sand-page ul{list-style:none}
.sand-page li{padding:4px 0;border-bottom:1px dashed #EDF1F4;font-size:12.5px}
.sand-page li:last-child{border-bottom:none}
.sand-page label{display:block;font-size:12px;color:#647382;margin:6px 0 2px}
.sand-page form{margin-top:8px}
.sand-strip{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.strip-cell{background:#fff;border:1px solid #D9E0E6;border-radius:4px;padding:7px 12px;font-size:12.5px;color:#33475B}
.strip-cell b{color:#1F2D3A}
.sand-map{position:relative;background:#EEE9DC;border:1px solid #D9E0E6;border-radius:4px;overflow:hidden}
.sand-map svg{display:block;width:100%;height:auto}
.sand-maplegend{display:flex;gap:12px;flex-wrap:wrap;font-size:11.5px;color:#647382;padding:6px 2px 0}
.lg-row{display:inline-flex;align-items:center;gap:5px}
.lg-swatch{display:inline-block;width:10px;height:10px;border-radius:2px}
.sand-todos li{padding:6px 0}
.cand-card{border:1px solid #D9E0E6;border-left:3px solid #B77A1A;background:#FBF6EC;border-radius:4px;padding:10px 12px;margin-bottom:10px;font-size:12.5px}
.qa-item{padding:8px 0;border-bottom:1px dashed #EDF1F4;font-size:13px}
.qa-item:last-child{border-bottom:none}
.sand-measure-result{background:#F0F7F4;border:1px solid #2F7D68;border-radius:4px;padding:8px 12px;font-size:12.5px;margin-top:8px}
.rv-grid{display:grid;grid-template-columns:minmax(0,1fr) 350px;gap:12px;align-items:start}
.rv-col-main{display:flex;flex-direction:column;gap:12px;min-width:0}
.rv-tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0 14px}
.rv-tab{border:1px solid #D9E0E6;background:#fff;border-radius:4px;padding:5px 14px;font-size:13px;color:#33475B}
.rv-tab.on{border-color:#245B78;color:#245B78;font-weight:600;background:#F0F4F7}
.issue-card{border:1px solid #D9E0E6;border-radius:4px;background:#fff;padding:10px 12px;margin-bottom:10px;font-size:13px}
.rv-sev-critical{border-left:3px solid #B84A42}
.rv-sev-major{border-left:3px solid #D2772A}
.rv-sev-minor{border-left:3px solid #B77A1A}
.rv-sev-watch{border-left:3px solid #647382}
.rv-kv{color:#647382;margin-right:10px;font-size:12.5px}
/* P02 shared shell for resource and review: align with the approved light sand header. */
@media (max-width:1360px){.sand-grid{grid-template-columns:minmax(0,1fr) 390px}.sand-left{grid-column:1/-1;display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr))}}
@media (max-width:980px){.sand-grid,.rv-grid{grid-template-columns:minmax(0,1fr)}.sand-left{grid-template-columns:minmax(0,1fr)}}
"""

_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 破局智援参谋指挥台</title>
<style>{css}</style></head>
<body class="{body_class}"><div class="shell">
<aside class="side">
  <div class="brand"><b>破局智援参谋 · 指挥台</b><span>指挥协同工作台</span></div>
  <nav class="nav">{nav}</nav>
</aside>
<main class="main">
  <div class="head"><h1>{title}</h1><span class="sub">{scenario} · v{version}</span></div>
  <div class="strip">{strip}</div>
  {body}
</main>
</div></body></html>"""


# ---------------------------------------------------------------------------
# P16 T2 light surfaces (doc 00 V0.2): login / events / roster / member home

_LIGHT_PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 破局智援参谋前指平台</title>
<style>{css}</style><link rel="stylesheet" href="/static/workbench-theme.css?v=20261012-1"></head>
<body class="lz-page{body_class}">
<header class="lz-topbar"><b><span class="wb-brand-mark" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="m3 20 9-17 9 17H3Zm5 0 8-10M7 14h10"/></svg></span><span class="wb-brand-copy">破局智援参谋<small>指挥协同工作台</small></span></b>{nav}<span class="spacer"></span><span class="lz-topuser">{user}</span></header>
<div class="lz-wrap">{body}</div>
</body></html>"""

_COMMANDER_LIGHT_NAV = (
    ("/events", "火情事件"),
    ("/", "人员架构"),
    ("/sand", "沙盘指挥"),
    ("/materials", "人员与物资"),
    ("/review", "复盘"),
)
#: 五页主导航（DEVELOPMENT_PLAN_PAGES_V1 §5.1）：顺序固定，旧路由（/map、
#: /overview、/timeline、/drill 等）保留兼容但不再进入主导航。成员无火情
#: 登记权限，导航不出现「火情登记」入口；/events 页面本身另有 404 门禁。
_MEMBER_LIGHT_NAV = _COMMANDER_LIGHT_NAV[1:]


def _light_wrap(*, title: str, body: str, nav: str, user: str,
                body_class: str = "") -> str:
    return _LIGHT_PAGE.format(title=title, css=_CSS, nav=nav, user=user + ' <a href="/offline">离线资料与原稿</a>',
                              body=body, body_class=body_class)


def _parse_multipart(raw: bytes, content_type: str) -> tuple[dict, dict]:
    """极简 multipart/form-data 解析：返回 (文本字段, {name: (文件名, 字节)})。"""
    marker = "boundary="
    idx = content_type.find(marker)
    if idx < 0:
        raise PanelActionError("上传缺少 multipart boundary")
    boundary = content_type[idx + len(marker):].split(";")[0].strip().strip('"')
    delim = ("--" + boundary).encode()
    fields: dict[str, list[str]] = {}
    files: dict[str, tuple[str, bytes]] = {}
    for part in raw.split(delim):
        if part.startswith(b"--"):
            continue
        # 分段形如 b"\r\nheaders\r\n\r\ndata\r\n"：只去首尾各一个 CRLF，
        # 不用 strip()——避免破坏恰好以 \r\n 结尾的二进制内容。
        if part.startswith(b"\r\n"):
            part = part[2:]
        head, sep, data = part.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        headers = head.decode("utf-8", "replace")
        name_at = headers.find('name="')
        if name_at < 0:
            continue
        name_start = name_at + len('name="')
        name_end = headers.find('"', name_start)
        field_name = headers[name_start:name_end]
        file_at = headers.find('filename="')
        if file_at >= 0:
            fname_start = file_at + len('filename="')
            fname_end = headers.find('"', fname_start)
            files[field_name] = (headers[fname_start:fname_end], data)
        else:
            fields.setdefault(field_name, []).append(
                data.decode("utf-8", "replace")
            )
    return fields, files


def _light_nav(entries, active: str) -> str:
    items = []
    for route, label in entries:
        cls = ' class="on"' if route == active else ""
        items.append(f'<a href="{route}"{cls}>{esc(label)}</a>')
    return "".join(items)


def _business_context(state: PanelState, *, title: str, active: str,
                      public: bool = False) -> str:
    """Shared location cue based only on the current registry event; never invent freshness."""
    event_id = getattr(state, "active_event_id", "") or ""
    registry = getattr(state, "registry", None)
    event = registry.events.get(event_id) if event_id and registry is not None else None
    event_word = event.name if event else "未进入事件任务空间"
    if public:
        event_word = "公共屏 · 仅已发布摘要"
    section = "本事件复盘" if active == "/review" else "长期底账 / 本事件资源"
    note = "公共屏 · 仅已发布摘要" if public else ("当前事件：" + event_word)
    return (
        '<header class="lz-work-context"><p>指挥工作空间　 /　'
        f'{esc(section)}</p><div><h1>{esc(title)}</h1>'
        f'<span>{esc(note)}</span></div></header>'
    )


def _wrap_business_page(
    *,
    body: str,
    title: str,
    active: str,
    commander: bool,
    member=None,
    public: bool = False,
    context: str = "",
    deputy: bool = False,
    business_role: str = '',
) -> str:
    """五页业务面统一页壳：顶栏导航 + 全局样式（P17 修复）。

    此前 /sand /materials /review 返回裸片段：无 DOCTYPE、无样式、无导航，
    浏览器里布局堆平——页面壳必须在这一层统一挂上，各渲染函数只负责正文。
    """
    shared = active in {"/materials", "/review"}
    page_class = " lz-work-ui" if shared else ""
    if public:
        return _light_wrap(
            title=title + " · 公共屏",
            body=context + body,
            body_class=page_class,
            nav="",
            user='<span>公共屏 · 仅已发布内容</span>',
        )
    if commander:
        return _light_wrap(
            title=title,
            body=context + body,
            body_class=page_class,
            nav=_light_nav(_COMMANDER_LIGHT_NAV, active),
            user='<span>指挥员 · 指挥模式</span>' + _topbar_logout_form("/logout", "登出"),
        )
    name = getattr(member, "name", "") or ""
    phone = mask_phone(getattr(member, "phone", "") or "")
    return _light_wrap(
        title=title,
        body=context + body,
        body_class=page_class,
        nav=_light_nav(_COMMANDER_LIGHT_NAV if deputy or business_role else _MEMBER_LIGHT_NAV, active),
        user=(
            f'<span>{esc(_business.LABELS.get(business_role, "副指挥长" if deputy else ""))} {esc(name)}（{esc(phone)}）</span>'
            + _topbar_logout_form("/logout/member", "退出登录")
        ),
    )


def parse_geo_points(text: str) -> tuple[tuple[float, float], ...]:
    """解析标绘地理坐标表单值：空 → ()；否则「纬度,经度」用分号/换行分隔。

    每个顶点必须显式给定，数量与顶点不一致时由领域层校验拦截。
    """
    text = (text or '').strip()
    if not text:
        return ()
    raw_items = [
        chunk.strip()
        for chunk in text.replace('；', ';').replace('，', ',').split(';')
        if chunk.strip()
    ]
    if not raw_items:
        return ()
    geo: list[tuple[float, float]] = []
    for chunk in raw_items:
        parts = [p.strip() for p in chunk.split(',') if p.strip()]
        if len(parts) != 2:
            raise PanelActionError(
                f'地理坐标格式应为「纬度,经度」，多条用分号分隔；无法解析：{chunk}'
            )
        try:
            lat, lon = float(parts[0]), float(parts[1])
        except ValueError:
            raise PanelActionError(
                f'地理坐标必须是数字（纬度,经度）：{chunk}'
            ) from None
        geo.append((lat, lon))
    return tuple(geo)


def parse_coords(lat_text: str, lon_text: str) -> tuple[float | None, float | None]:
    """解析事件坐标表单值：两者都空 → (None, None)；任一非法则显式报错。"""
    lat_text = (lat_text or "").strip()
    lon_text = (lon_text or "").strip()
    if not lat_text and not lon_text:
        return None, None
    try:
        lat = float(lat_text)
        lon = float(lon_text)
    except ValueError:
        raise PanelActionError("坐标必须是数字（纬度/经度），留空则不登记坐标") from None
    if not (-90.0 <= lat <= 90.0):
        raise PanelActionError("纬度超出范围（-90..90）")
    if not (-180.0 <= lon <= 180.0):
        raise PanelActionError("经度超出范围（-180..180）")
    return lat, lon


def _topbar_logout_form(action: str, label: str) -> str:
    return (
        f'<form class="lz-inlineform" method="post" action="{action}">'
        f'<button type="submit" class="lz-btn lz-topbtn">{esc(label)}</button></form>'
    )


_EVENT_CHIP_CLASS = {
    "registered": "lz-chip-reg",
    "active": "lz-chip-active",
    "upgraded": "lz-chip-upgraded",
    "closed": "lz-chip-closed",
}


def _event_status_chip(record) -> str:
    cls = _EVENT_CHIP_CLASS.get(record.status, "lz-chip-reg")
    return f'<span class="lz-chip {cls}">{esc("待启动处置" if record.status == "registered" else record.status_word())}</span>'


def _hidden_post(action: str, fields: dict[str, str], label: str, btn_cls: str = "lz-btn2") -> str:
    hidden = "".join(
        f'<input type="hidden" name="{esc(key)}" value="{esc(value)}">'
        for key, value in fields.items()
    )
    return (
        f'<form class="lz-inlineform" method="post" action="{action}">{hidden}'
        f'<button type="submit" class="lz-btn {btn_cls}">{esc(label)}</button></form>'
    )


# -- 登录页（01 号 §1 三区布局） -------------------------------------------


def _unified_login_page(error: str = "", account: str = "") -> str:
    """One credential form; authorization remains in the existing auth gates."""
    alert = f'<div class="login-alert" role="alert">{esc(error)}</div>' if error else ""
    return f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>登录 · 破局智援参谋</title>
<link rel="stylesheet" href="/static/login.css?v=20261012-1">
<script src="/static/login.js?v=20261012-1" defer></script></head>
<body class="login-page">
<header class="login-brand"><span class="login-brand-mark" aria-hidden="true">
<svg viewBox="0 0 24 24"><path d="m3 20 9-17 9 17H3Zm5 0 8-10M7 14h10"/></svg>
</span><span>破局智援参谋</span></header>
<main class="login-main"><section class="login-card" aria-labelledby="login-title">
<h1 id="login-title">欢迎登录</h1><p class="login-subtitle">破局智援参谋</p>
{alert}<form method="post" action="/login/unified">
<div class="login-field"><label for="login-account">账号</label><div class="login-input-wrap">
<svg class="login-field-icon" viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="8" r="3.5"/><path d="M5 21v-2a7 7 0 0 1 14 0v2"/></svg>
<input id="login-account" name="account" type="text" value="{esc(account)}" placeholder="手机号或 admin" autocomplete="username" autocapitalize="none" spellcheck="false" required></div></div>
<div class="login-field"><label for="login-password">密码</label><div class="login-input-wrap">
<svg class="login-field-icon" viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V6a4 4 0 0 1 8 0v4M12 14v3"/></svg>
<input id="login-password" name="password" type="password" placeholder="请输入密码" autocomplete="current-password" required>
<button class="login-eye" type="button" aria-label="显示密码" aria-pressed="false" aria-controls="login-password" hidden>
<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/></svg></button></div></div>
<button class="login-submit" type="submit">登录</button>
<p class="login-help">登录后按身份进入工作空间</p></form>
<footer class="login-secondary"><a href="/temporary">临时人员登记</a></footer>
</section></main></body></html>'''


def _member_checkin_zone(
    state: PanelState,
    session: MemberSession,
    *,
    back_space: bool,
    scan_alert: str,
    confirm_outcome: CheckinOutcome | None,
    scanned_qr: str,
) -> str:
    parts = ['<div class="lz-card"><h2>刷码入岗确认</h2>'
             '<p><a class="lz-link" href="/temporary">临时人员逐人扫码登记 →</a></p>']
    parts.append(
        '<p class="lz-note">演示刷码方式：演示环境无摄像头，输入二维码编号等效扫码。'
        '码只识别事件，不含身份不含岗位；岗位来自本事件预配置名册。</p>'
    )
    if back_space:
        parts.append(
            '<div class="lz-alert ok"><b>已在岗</b>：直接返回本事件工作空间'
            f'（幂等，不重复记录到场）。事件：{_event_label(state.registry, session.bound_event_id)}。'
            + (f'到场时间 {esc(session.arrived_at)}。' if session.arrived_at else "")
            + "</div>"
        )
    if scan_alert:
        parts.append(f'<div class="lz-alert bad">{esc(scan_alert)}</div>')
    if confirm_outcome is not None and confirm_outcome.kind == OUTCOME_CONFIRM:
        parts.append(
            '<div class="lz-alert ok"><b>即将进入：'
            f'{esc(confirm_outcome.group_name)}—{esc(confirm_outcome.position_name)}</b>'
            f'<br>事件：{esc(_event_label(state.registry, confirm_outcome.event_id))}</div>'
            '<form method="post" action="/checkin/confirm">'
            f'<input type="hidden" name="qr" value="{esc(scanned_qr)}">'
            '<button type="submit" class="lz-btn lz-btn-ok">确认入岗</button> '
            '<a class="lz-link" href="/login">取消</a></form>'
        )
    else:
        if session.onboarded and not back_space:
            parts.append(
                '<div class="lz-alert warn">当前状态：已入岗（事件 '
                f'{_event_label(state.registry, session.bound_event_id)}'
                + (f' · 到场 {esc(session.arrived_at)}' if session.arrived_at else "")
                + '）。再次刷码将幂等返回本事件工作空间。</div>'
            )
        parts.append(
            '<form method="post" action="/checkin/scan">'
            '<div class="lz-formrow"><label class="lz-label" for="qr-input">二维码编号</label>'
            '<input class="lz-input lz-mono" id="qr-input" name="qr" placeholder="例如 qr-001"></div>'
            '<button type="submit" class="lz-btn">刷码</button></form>'
        )
    parts.append("</div>")
    return "".join(parts)


def render_login_page(
    state: PanelState,
    *,
    commander_error: bool = False,
    member_error: str = "",
    member_token: str | None = None,
    back_space: bool = False,
    scan_alert: str = "",
    confirm_outcome: CheckinOutcome | None = None,
    scanned_qr: str = "",
    login_account: str = "",
) -> str:
    """Unified login, followed by the existing authenticated check-in flow."""
    intro = (
        '<div class="lz-card"><h2>登录</h2>'
        '<p class="lz-note">身份认证 · 火情事件管理入口 · 现场刷码入岗。本页不承载业务内容；'
        '未登录不能进入业务页面，任何无法验证的状态一律阻断（fail-closed）。'
        '指挥员会话有效期 8 小时，超时需重新登录（01 号 §8「会话过期」）。</p></div>'
    )
    session = state.member_session(member_token)
    if session is None:
        page = _unified_login_page(
            "账号或密码不正确。" if commander_error else member_error,
            login_account,
        )
        from .resource_demo import panel as demo_panel
        return page.replace('</section></main>',demo_panel(state,public=True)+'</section></main>')
    else:
        checkin_zone = _member_checkin_zone(
            state,
            session,
            back_space=back_space,
            scan_alert=scan_alert,
            confirm_outcome=confirm_outcome,
            scanned_qr=scanned_qr,
        )
        from .resource_demo import panel as demo_panel
        body = intro + demo_panel(state,member_token) + checkin_zone
        user = (
            f'<span>{esc(session.name)}（{esc(mask_phone(session.phone))}）</span>'
            + _topbar_logout_form("/logout/member", "退出登录")
        )
    return _light_wrap(title="登录", body=body, nav=_light_nav(_MEMBER_LIGHT_NAV, "/login"), user=user)


# -- 火情事件管理页 ---------------------------------------------------------


def _event_row_actions(event_id: str, status: str) -> str:
    bits = [f'<a class="lz-link" href="/events/{esc(event_id)}/details">查看详情</a>']
    if status == "registered":
        bits.append('<span class="lz-dim">等待值班人员启动处置</span>')
    if status == "active":
        bits.append(_hidden_post("/events/action", {"event_id": event_id, "action": "upgrade"}, "升级"))
    if status != "closed":
        bits.append(f'<a class="lz-btn lz-btn-danger" href="/events/{esc(event_id)}/close-confirm">关闭</a>')
    if status in ("active", "upgraded"):
        bits.append(_hidden_post("/events/action", {"event_id": event_id, "action": "issue-qr"}, "补发二维码"))
    bits.append(_hidden_post("/events/action", {"event_id": event_id, "action": "enter"}, "进入"))
    bits.append(f'<a class="lz-link" href="/events/{esc(event_id)}/roster">配置名册</a>')
    return f'<span class="lz-actions">{"".join(bits)}</span>'


def _event_qr_lines(state: PanelState, event_id: str, *, allow_revoke: bool = True) -> str:
    from .event_qr import qr_svg,entry_url
    event = state.registry.events.get(event_id)
    qrs = sorted((q for q in state.registry.qrs.values() if q.event_id == event_id),
                 key=lambda q: q.qr_id)
    if not qrs:
        record=state.registry.events.get(event_id)
        note=('启动完成，待值班人员选定副总指挥后生成火情事件入岗二维码。'
              if record and record.status in {'active','upgraded'} else
              '事件已关闭，无可用入岗二维码。' if record and record.status=='closed' else
              '等待值班人员启动处置后生成事件二维码。')
        return '<p class="lz-note">'+note+'</p>'
    lines = []
    for qr in qrs:
        word = EventRegistry.qr_status(qr)
        usable = word == "有效" and event is not None and event.status in {"active", "upgraded"}
        status = word if event is not None and event.status != "closed" else "事件已关闭 · 停止入岗"
        symbol = qr_svg(qr.qr_id,entry_url=entry_url(state,event_id,qr.qr_id)) if usable else '<div class="lz-dim">此码不可用于入岗</div>'
        revoke = (_hidden_post("/events/action", {"action": "revoke-qr", "qr_id": qr.qr_id},
                               "撤销", "lz-btn-danger") if allow_revoke and usable else "")
        lines.append(
            '<div class="event-qr-record" style="display:flex;gap:16px;align-items:center;'
            'flex-wrap:wrap;padding:12px 0;border-top:1px solid #D9E0E6">'
            + symbol + '<div style="min-width:0;overflow-wrap:anywhere">'
            f'<p><b>事件二维码 {esc(qr.qr_id)}</b> · {esc(status)}</p>'
            f'<p>有效期至 {esc(EventRegistry.qr_expiry_iso(qr))}</p>'
            f'<p class="lz-note">签发 {esc(qr.issued_at)} · 签发人 {esc(qr.issued_by or "未记录")}</p>'
            '<p class="lz-note">'+('手机扫码可打开队长入口，登录后核对本队再确认到场。' if getattr(state,'wifi_origin','') else '扫码内容为事件二维码编号；本人登录后的入岗资格仍由系统核对。')+'</p>'
            + revoke + '</div></div>')
    return ''.join(lines)


def render_fire_event_detail(state: PanelState, event_id: str) -> str | None:
    from .fire_demo import fire_source_label
    record = state.registry.events.get(event_id)
    if record is None:
        return None
    fields = [("事件编号", record.event_id), ("名称", record.name),
              ("上游编号", record.report_no), ("发现时间", record.discovered_at),
              ("来源", fire_source_label(record.source)), ("位置", record.location),
              ("状态", "待启动处置" if record.status == "registered" else record.status_word()),
              ("等级", record.level or "未提供"), ("行政区", record.admin_region or "未提供"),
              ("初报", record.initial_report or "未提供"),
              ("重点目标", "、".join(record.key_targets) or "未提供"),
              ("坐标", f"{record.lat}, {record.lon}" if record.lat is not None else "未提供")]
    rows = ''.join(f'<tr><th>{esc(k)}</th><td style="overflow-wrap:anywhere">{esc(str(v))}</td></tr>' for k, v in fields)
    sources = ''.join(f'<p>接入记录编号：{esc(a.source_system)} / {esc(a.external_no)}；接收时间 {esc(a.received_at)}；报文摘要 {esc(a.raw_digest)}。</p>'
                      for a in state.alarm_inbox.alarms.values() if a.event_id == event_id)
    return _light_wrap(title="火情详情", body='<div class="lz-card"><h2>火情详情</h2><table class="lz-table">'+rows+'</table>'+sources+'</div><div class="lz-card"><h2>事件二维码</h2>'+_event_qr_lines(state, event_id, allow_revoke=False)+'</div>',
                       nav='<a href="/events">返回火情事件</a>', user='')


def render_events_page(state: PanelState, *, query=None) -> str:
    from .fire_demo import fire_source_label
    registry = state.registry
    issued_qr_id = ((query or {}).get("issued") or [""])[0]
    alarm_event = ((query or {}).get("alarm_event") or [""])[0]
    events = sorted(registry.events.values(), key=lambda r: r.event_id)
    parts = [
        '<div class="lz-card"><h2>火情事件</h2>'
        f'<p class="lz-note">共 {len(events)} 个事件。多模态火情预警系统推送已确认火情后自动建事件，'
        '由值班人员点击启动处置。</p>'
        '<p class="lz-note">真实推送接口：待对接。当前使用匿名演示数据。</p>'
        '<details><summary>推送演示</summary><form method="post" action="/events/alarm/demo">'
        '<p>仅模拟上游推送，不接收真实报警，不自动启动处置。</p>'
        '<button class="lz-btn lz-btn2">模拟接收已确认火情（演示数据）</button></form></details></div>'
    ]
    if alarm_event and alarm_event in registry.events:
        record = registry.events[alarm_event]
        parts.append('<div class="lz-alert ok">已自动建立火情事件：'
                     f'{esc(record.event_id)} · {esc(record.name)}。等待值班人员启动处置。</div>')
    issued = registry.qrs.get(issued_qr_id)
    if issued is not None:
        parts.append(
            '<div class="lz-alert ok"><b>二维码已签发</b>：编号 <span class="lz-mono">'
            f'{esc(issued.qr_id)}</span> · 事件 {_event_label(registry, issued.event_id)}'
            f' · 有效期至 {esc(EventRegistry.qr_expiry_iso(issued))}。成员在登录页输入该编号等效扫码。</div>'
        )
    table_rows = []
    for record in events:
        table_rows.append(
            "<tr>"
            f'<td class="lz-mono">{esc(record.event_id)}</td>'
            f'<td>{esc(record.name)}<br><span class="lz-dim lz-mono">{esc(record.report_no)}</span></td>'
            f'<td class="lz-mono">{esc(record.discovered_at)}</td>'
            f'<td>{esc(fire_source_label(record.source))}</td>'
            f'<td>{esc(record.location)}</td>'
            f'<td>{_event_status_chip(record)}</td>'
            f'<td>{_event_row_actions(record.event_id, record.status)}</td>'
            "</tr>"
            '<tr><td colspan="7" style="padding:0 10px 6px;border-bottom:1px solid #D9E0E6">'
            f"{_event_qr_lines(state, record.event_id)}</td></tr>"
        )
    if events:
        list_card = (
            '<div class="lz-card"><table class="lz-table">'
            "<tr><th>编号</th><th>名称／报告编号</th><th>发现时间</th><th>来源</th>"
            "<th>位置</th><th>状态</th><th style=\"width:42%\">操作</th></tr>"
            + "".join(table_rows)
            + "</table></div>"
        )
    else:
        list_card = (
            '<div class="lz-empty"><b>暂无火情事件</b>'
            "等待多模态火情预警系统推送已确认火情；接收后自动建事件，由值班人员启动处置。</div>"
        )
    body = "".join(parts) + list_card
    body += (
        '<div class="lz-card"><h2>成员账号解锁</h2>'
        '<p class="lz-note">连续登录失败触发锁定（01 号 §8）；此处按手机号解除。'
        '锁定原因与解除操作均以注册表留痕为准。</p>'
        '<form method="post" action="/account/unlock" style="display:flex;gap:8px">'
        '<input class="lz-input" name="phone" placeholder="手机号" style="max-width:200px">'
        '<button type="submit" class="lz-btn lz-btn2">解锁账号</button></form></div>'
    )
    user = (
        '<span>指挥员 · 指挥模式</span>' + _topbar_logout_form("/logout", "登出")
    )
    return _light_wrap(title="火情事件", body=body, nav=_light_nav(_COMMANDER_LIGHT_NAV, "/events"), user=user)


def render_business_events_page(state: PanelState, member_token: str) -> str:
    member = state.member_session(member_token)
    rows = []
    for event_id in _business.events(state, member_token):
        record = state.registry.events[event_id]
        action = _hidden_post('/events/select', {'event_id': event_id, 'csrf': member.custody_csrf}, '进入事件')
        rows.append(f'<tr><td>{esc(record.name)}<br>{esc(event_id)}</td><td>{_event_status_chip(record)}</td>'
                    f'<td><a href="/events/{esc(event_id)}/details">火情详情</a> {action}</td></tr>')
    body = ('<div class="lz-card"><h2>火情事件</h2><p>仅显示本队已参与的处置中事件。'
            '进入事件后，查看事件概览与本队明细；查看页面不确认到场。</p>'
            '<div style="overflow-x:auto"><table class="lz-table"><tr><th>事件</th><th>状态</th><th>操作</th></tr>'
            + ''.join(rows) + '</table></div>' + ('' if rows else '<p>暂无本队已参与的处置中事件。</p>')
            + '</div>' + _business.captain_actions())
    return _wrap_business_page(body=body, title='火情事件', active='/events', commander=False,
                               member=member, business_role='captain')


def render_deputy_events_page(state: PanelState, member_token: str) -> str:
    """Only appointed events; selection changes the member session, never lifecycle."""
    member = state.member_session(member_token)
    rows = []
    for event_id in state.deputy_events(member_token):
        record = state.registry.events[event_id]
        current = ' · 当前事件' if event_id == state.active_event_id else ''
        action = _hidden_post('/events/select', {
            'event_id': event_id, 'csrf': member.custody_csrf,
        }, '进入事件')
        rows.append(
            f'<tr><td>{esc(record.name)}{current}<br>{esc(event_id)}</td>'
            f'<td>{esc(record.location)}</td><td>{_event_status_chip(record)}</td>'
            f'<td><a href="/events/{esc(event_id)}/details">火情详情</a> {action}</td></tr>')
    body = ('<div class="lz-card"><h2>火情事件</h2><p>副指挥长 · 已获任命的事件。'
            '进入事件后，可通过顶部导航查看人员架构、沙盘指挥、人员与物资和复盘。</p>'
            '<p><a class="lz-link" href="/dispatch">副指挥工作台 →</a></p></div>')
    body += ('<div class="lz-card"><table class="lz-table">'
             '<tr><th>事件</th><th>位置</th><th>状态</th><th>操作</th></tr>'
             + ''.join(rows) + '</table></div>' if rows else
             '<div class="lz-empty">暂无已获任命的处置中事件。</div>')
    return _wrap_business_page(body=body, title='火情事件', active='/events',
                               commander=False, member=member, deputy=True)


def render_duty_events_page(state: PanelState, member: MemberSession, *, query=None, event_ids=None) -> str:
    from .duty_intake import body
    return _light_wrap(title='火情事件',body=body(state,member,query=query,event_ids=event_ids),
        nav=_light_nav(_COMMANDER_LIGHT_NAV,'/events'),
        user=f'<span>{esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             +_topbar_logout_form('/logout/member','登出'))

def render_duty_local_notice_page(state: PanelState, member: MemberSession) -> str:
    cards = [
        '<div class="lz-card"><h2>属地临时人员模拟通知</h2>'
        '<p class="lz-note">仅值班人员本人可按事件准备属地主管领导／协助人员的电话或微信模拟通知。'
        '此处不调用外部通道，所有记录均未发送、未送达；人员须另行本人扫码登记。'
        '通知记录不是入场资格门槛，也不自动记到场或编组。</p></div>'
    ]
    fixed_phones = {phone for squad in state.ledger.squads.values()
                    for phone in squad.member_phones}
    for event_id, record in sorted(state.registry.events.items()):
        if record.status not in {"active", "upgraded"}:
            continue
        roster = state.registry.rosters.get(event_id)
        fixed_roster = set(roster.entries) if roster else set()
        options = ''.join(
            f'<option value="{esc(phone)}">{esc(person.name)} · {esc(person.unit)}'
            f'（{esc(mask_phone(phone))}）</option>'
            for phone, person in sorted(state.registry.library.items())
            if phone not in fixed_phones and phone not in fixed_roster
        )
        form = (
            '<form method="post" action="/events/local-notices/prepare">'
            f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
            f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            '<label class="lz-label">在库属地人员<select class="lz-input" name="phone">'
            + options + '</select></label>'
            '<label class="lz-label">本次属地身份<select class="lz-input" name="local_role">'
            '<option value="local_leader">属地主管领导</option>'
            '<option value="assistant">属地协助人员</option></select></label>'
            '<label class="lz-label">模拟通道<select class="lz-input" name="channel">'
            '<option value="phone">电话（未拨打）</option>'
            '<option value="wechat">微信（未发送）</option></select></label>'
            '<button type="submit" class="lz-btn">准备未发送的模拟通知</button></form>'
            if options else '<p class="lz-dim">暂无可选在库非固定中队人员。</p>'
        )
        registered = state.temporary_people(event_id)
        notices = state.local_notices(event_id)
        rows = ''.join(
            '<tr>'
            f'<td>{esc(n.name)}（{esc(mask_phone(n.phone))}）</td>'
            f'<td>{esc(_local_notice.ROLES[n.local_role])}</td>'
            f'<td>{esc(_callout.CHANNELS[n.channel])}</td>'
            '<td>模拟已准备 · 未发送／未送达</td>'
            f'<td>{"已本人扫码登记 · " + esc(registered[n.phone].registered_at) if n.phone in registered else "尚未扫码登记"}</td>'
            f'<td>{esc(n.prepared_at)}</td></tr>'
            for n in sorted(notices.values(), key=lambda n: (n.prepared_at, n.phone))
        )
        cards.append(
            f'<div class="lz-card"><h2>{esc(record.name)} · {esc(event_id)}</h2>'
            + form
            + '<h3>模拟通知与登记对照</h3><table class="lz-table">'
              '<tr><th>人员</th><th>本次身份</th><th>模拟通道</th>'
              '<th>外部状态</th><th>扫码状态</th><th>准备时间</th></tr>'
            + (rows or '<tr><td colspan="6">尚未准备属地人员模拟通知。</td></tr>')
            + '</table></div>'
        )
    return _light_wrap(
        title="属地人员模拟通知", body=''.join(cards),
        nav='<a href="/events">事件启动</a>'
            '<a class="on" href="/events/local-notices">属地人员模拟通知</a>',
        user=f'<span>值班人员 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_task_material_inputs_page(state: PanelState, token, event_id, query):
    from .task_material_inputs import review
    space, _, _ = state._appointment_space(event_id)
    order_id = (query.get('order_id') or [''])[0]
    item_id = (query.get('item_id') or [''])[0]
    options = lambda values, selected: ''.join(
        f'<option value="{esc(key)}"' + (' selected' if key == selected else '')
        + f'>{esc(getattr(values[key], "name", getattr(values[key], "scope", key)))}（{esc(key)}）</option>' for key in sorted(values))
    body = ('<div class="lz-card"><h2>任务物资输入报告</h2>'
        '<p>指定副总指挥具名补录任务与物资关系、实际投入力量、预计时长和用量来源。'
        '这些是报告，不等于专业核定；不会发布任务、修改库存或填补正式缺口结论。未知留空，明确零填 0。</p>'
        '<form method="get" action="/task-materials">'
        f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
        '<label>任务<select class="lz-select" name="order_id"><option value="">请选择</option>'
        + options(space.orders, order_id) + '</select></label>'
        '<label>物资<select class="lz-select" name="item_id"><option value="">请选择</option>'
        + options(state.ledger.items, item_id) + '</select></label>'
        '<button class="lz-btn">核对输入来源</button></form>')
    if order_id in space.orders and item_id in state.ledger.items:
        result = review(space, state.ledger, event_id=event_id, order_id=order_id, item_id=item_id)
        words = {'missing':'尚无输入报告', 'current':'当前来源一致，仍是报告',
                 'needs_reassessment':'任务或库存已变化，须重新评估', 'source_pending':'当前任务或库存来源待核对，不能新增报告'}
        body += f'<p>{esc(words[result.state])}；报告版本 {result.version}。</p>'
        if result.records:
            body += '<details open><summary>保留的报告历史</summary>'
            for revision in result.records:
                value = json.loads(revision.content_json)
                number = lambda name: '未知' if value[name] is None else format(value[name], 'g')
                body += (f'<p style="overflow-wrap:anywhere">报告版 {revision.reference.version}，'
                    f'任务版 {value["task_version"]}；实际人数 {number("force_count")}；'
                    f'预计小时 {number("duration_hours")}；当前需求 {number("demand_quantity")}；'
                    f'预计消耗 {number("projected_consumption")}；'
                    f'{"模拟用量" if value["usage_kind"] == "simulated" else "用量未核定"}。'
                    f'关系来源 {esc(value["material_relation_source"])}；力量来源 {esc(value["force_source"])}；'
                    f'时长来源 {esc(value["duration_source"])}；用量来源 {esc(value["usage_source"])}；'
                    f'原始依据 {esc(revision.source)}；发生 {esc(revision.times.occurred_at or "未知")}；'
                    f'记录 {esc(revision.times.recorded_at)}；更正/重评 {esc(value["reason"] or "首次报告")}；'
                    f'来源编号 {esc(revision.id)}。</p>')
            body += '</details>'
        if result.source_digest is not None and state.registry.events[event_id].status != 'closed':
            body += '<form method="post" action="/task-materials/report">'
            for name, value in dict(event_id=event_id, order_id=order_id, item_id=item_id,
                    expected_version=str(result.version), expected_source_digest=result.source_digest,
                    csrf=state.member_session(token).custody_csrf,
                    request_id=_secrets.token_urlsafe(18)).items():
                body += f'<input type="hidden" name="{name}" value="{esc(value)}">'
            labels = dict(material_relation_source='任务与物资对应来源', force_count='实际投入人数',
                force_source='实际力量报告来源', duration_hours='预计作业小时', duration_source='预计时长来源',
                demand_quantity='报告当前需求量', projected_consumption='报告预计消耗量', usage_source='用量来源',
                reason='更正或重评依据', evidence='原始报告依据', occurred_at='报告发生时间（带时区，未知留空）')
            values = result.current or {}
            for name, label in labels.items():
                value = values.get(name, '')
                if value is None:
                    value = ''
                required = ' required' if name in {'material_relation_source', 'evidence'} or name == 'reason' and result.version else ''
                body += f'<label class="lz-label">{label}<input class="lz-input" name="{name}" value="{esc(value)}"{required}></label>'
            usage_options = ''.join(f'<option value="{key}"'
                + (' selected' if key == values.get('usage_kind', 'unreviewed') else '')
                + f'>{label}</option>' for key, label in (('unreviewed', '未核定'), ('simulated', '模拟用量')))
            body += ('<label>用量标记<select class="lz-select" name="usage_kind">'
                + usage_options + '</select></label>'
                '<button class="lz-btn">保存具名输入报告</button></form>')
    body += '</div>'
    return _light_wrap(title='任务物资输入报告', body=body,
        user=f'<span>指定副总指挥 {esc(state.member_session(token).name)}</span>'
             + _topbar_logout_form('/logout/member', '登出'),
        nav=f'<a href="/dispatch">选队召集</a><a class="on" href="/task-materials?event_id={esc(event_id)}">任务物资输入</a>')


def render_deputy_dispatch_page(state: PanelState, member: MemberSession,
                                event_ids: list[str]) -> str:
    cards = [
        '<div class="lz-card"><h2>选队召集</h2>'
        '<p class="lz-note">仅本事件已登记的指定副总指挥可确认选队。'
        '下方只生成电话／微信模拟通知记录，未接真实通道，未发送、未送达；'
        '启动事件不会自动召集全部队伍。</p></div>'
    ]
    for event_id in event_ids:
        if _business.selected_event(state, next((t for t,m in state.member_sessions.items() if m is member),None)) != event_id:
            continue
        event = state.registry.events[event_id]
        selected = state.event_callouts(event_id)
        options = []
        rows = []
        for squad in sorted(state.ledger.squads.values(), key=lambda s: s.squad_id):
            person = state.registry.library.get(squad.leader_phone)
            eligible = bool(squad.leader_phone in squad.member_phones
                            and person is not None and person.is_leader)
            captain = (f'{esc(person.name)}（{esc(mask_phone(person.phone))}）'
                       if eligible else '未配置可核验队长')
            callout = selected.get(squad.squad_id)
            if callout is None and eligible:
                options.append(
                    f'<label class="lz-label"><input type="checkbox" name="squad_id" '
                    f'value="{esc(squad.squad_id)}"> {esc(squad.name)} · {captain}</label>'
                )
            elif callout is not None:
                reply = callout.reply
                status = ('队长模拟回执：可出动' if reply and reply['can_depart'] == 'yes'
                          else '队长模拟回执：不能出动' if reply else '模拟通知已生成，未发送／未送达')
                failure = callout.simulation_failure
                if failure:
                    status += ' · 演练假设通知失败（真实未发送／未送达）'
                failure_form = (
                    '<form method="post" action="/dispatch/simulate-failure">'
                    f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                    f'<input type="hidden" name="squad_id" value="{esc(squad.squad_id)}">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    '<label class="lz-label">演练失败原因<input class="lz-input" name="reason" required></label>'
                    '<button type="submit" class="lz-btn">记录模拟通知失败</button></form>'
                    if not failure and not reply else ''
                )
                rows.append(
                    f'<li>{esc(squad.name)} · {esc(_callout.CHANNELS[callout.channel])} · '
                    f'{esc(status)}'
                    + (f' · 原因：{esc(failure["reason"])}' if failure else '')
                    + failure_form + '</li>'
                )
        form = (
            '<form method="post" action="/dispatch/select">'
            f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
            f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            + ''.join(options)
            + '<label class="lz-label">模拟通知方式<select class="lz-input" name="channel">'
              '<option value="phone">电话（模拟，未发送）</option>'
              '<option value="wechat">微信（模拟，未发送）</option></select></label>'
              '<button type="submit" class="lz-btn">确认选队并生成模拟通知</button></form>'
            if options else '<p class="lz-dim">暂无可选且已配置队长的中队。</p>'
        )
        cards.append(
            f'<div class="lz-card"><h2>{esc(event.name)} · {esc(event_id)}</h2>'
            f'<p><a href="/task-materials?event_id={esc(event_id)}">任务物资输入报告</a></p>'
            + _radio_material_panel(state, event_id)
            +
            '<h3>已选队伍</h3>' + ('<ul>' + ''.join(rows) + '</ul>' if rows else '<p class="lz-dim">尚未选队。</p>')
            + '<h3>继续选队</h3>' + form + '</div>'
        )
    return _light_wrap(
        title="选队召集", body=''.join(cards),
        body_class=' lz-deputy-page',
        nav='<a class="on" href="/dispatch">选队召集</a>'
            '<a href="/material-transfers">事件物资调拨</a>'
            '<a href="/dispatch/task-review">任务分类登记</a>'
            '<a href="/dispatch/professionals">专业职责指定</a><a href="/personnel/manage">具名人员管理</a>'
            '<a href="/tasks/release">任务复核与发布</a>'
            '<a href="/tasks/offline">线下任务</a>'
            '<a href="/">人员架构</a><a href="/temporary/assign">人员编组／调组</a>',
        user=f'<span>指定副总指挥 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_task_review_page(state: PanelState, member: MemberSession,
                            event_ids: list[str], deputy_token: str, *, embedded=False) -> str:
    sections = []
    for event_id in event_ids:
        event = state.registry.events[event_id]
        try:
            bundles = state.task_review_bundles(event_id, deputy_token=deputy_token)
        except ValueError:
            sections.append(f'<section class="lz-card"><h2>{esc(event.name)}</h2>'
                            '<p>任务草案或分类审计待核对，暂不展示。</p></section>')
            continue
        entries = []
        for bundle, reports in bundles:
            tasks = []
            for index, task in enumerate(bundle.tasks):
                previous, current = reports[task.id]
                prior = json.loads(previous.content_json) if previous else {}
                review_version = previous.reference.version if previous else 0
                category = prior.get('category', 'undetermined') if current else 'undetermined'
                status = ('本版已登记：' + _task_review.CATEGORIES[category]
                          if current else '旧版登记已失效，需重新判断。'
                          if previous else '本版尚未登记分类判断。')
                options = ''.join(
                    f'<option value="{key}"{ " selected" if key == category else ""}>'
                    f'{esc(label)}</option>'
                    for key, label in _task_review.CATEGORIES.items())
                role = '总任务' if index == 0 else f'子任务 {index}'
                tasks.append(
                    f'<fieldset><legend>{esc(role)} · {esc(task.spec.title or task.id)}</legend>'
                    f'<p class="lz-note">草案 v{bundle.version}；{esc(status)}'
                    + '</p><form method="post" action="/dispatch/task-review">'
                    f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                    f'<input type="hidden" name="decision_id" value="{esc(bundle.decision_id)}">'
                    f'<input type="hidden" name="task_id" value="{esc(task.id)}">'
                    f'<input type="hidden" name="expected_draft_version" value="{bundle.version}">'
                    f'<input type="hidden" name="expected_review_version" value="{review_version}">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    '<label class="lz-label">人工分类判断<select class="lz-input" name="category">'
                    + options + '</select></label>'
                    '<label class="lz-label">判断理由<input class="lz-input" name="reason" required '
                    f'value="{esc(prior.get("reason", "") if current else "")}"></label>'
                    '<label class="lz-label">参考资料<input class="lz-input" name="basis_ref" required '
                    f'value="{esc(prior.get("basis_ref", "") if current else "")}"></label>'
                    '<button class="lz-btn" type="submit">登记分类判断</button></form></fieldset>')
            entries.append(f'<h3>方案 {esc(bundle.option_id)} · 草案 v{bundle.version}</h3>'
                           + ''.join(tasks))
        sections.append(f'<section class="lz-card"><h2>{esc(event.name)}</h2>'
                        + (''.join(entries) if entries else '<p>暂无可核对的采纳任务草案。</p>')
                        + '</section>')
    body=('<p class="lz-note">分类是指定副总指挥报告的当前判断；依据引用尚未经专业签审核真。'
          '登记、草案保存和旧命令确认均不发布任务。</p>' + ''.join(sections))
    if embedded:return body
    return _light_wrap(
        title='任务分类登记',
        body_class=' lz-deputy-page',
        body=body,
        nav='<a href="/dispatch">选队召集</a>'
            '<a class="on" href="/dispatch/task-review">任务分类登记</a>'
            '<a href="/dispatch/professionals">专业职责指定</a>'
            '<a href="/tasks/release">任务复核与发布</a>'
            '<a href="/tasks/offline">线下任务</a>'
            '<a href="/">人员架构</a><a href="/temporary/assign">人员编组／调组</a>',
        user=f'<span>指定副总指挥 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form('/logout/member', '登出'),
    )


def render_offline_tasks_page(state: PanelState, member: MemberSession,
                              views: list, actor: str) -> str:
    states = {'reported_offline': '线下已发生 · 待本人回执',
              'received': '本人已报收到', 'executing': '执行中',
              'blocked': '困难或异常待处理',
              'pending_verification': '完成报告 · 待核验', 'closed': '已核验关闭',
              'paused': '已暂停', 'withdrawn': '已撤回', 'replaced': '已替代'}
    actions = {
        'reported_offline': (('received', '确认收到'),),
        'received': (('started', '开始执行'),),
        'executing': (('progress', '报告进展'), ('difficulty', '报告困难'),
                      ('anomaly', '报告异常'), ('completed', '报告完成')),
        'blocked': (('progress', '补充进展'), ('resolved', '报告问题已处理')),
    }
    history_labels = {'received': '确认收到', 'started': '开始执行',
                      'progress': '进展报告', 'difficulty': '困难报告',
                      'anomaly': '异常报告', 'resolved': '问题已处理',
                      'completed': '完成报告', 'pause': '暂停', 'resume': '恢复',
                      'withdraw': '撤回', 'replace': '替代',
                      'followup': '线下追问', 'escalate': '线下升级'}
    sections = []
    for record, tasks, deputy, eligible, reminders in views:
        entries = []
        if deputy and record.status != 'closed':
            entries.append(
                '<form method="post" action="/tasks/offline"><h3>补录已发生的线下指令</h3>'
                f'<input type="hidden" name="event_id" value="{esc(record.event_id)}">'
                '<input type="hidden" name="action" value="record">'
                f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                '<label class="lz-label">指令内容<input class="lz-input" name="content" required></label>'
                '<label class="lz-label">执行组编号<input class="lz-input" name="group_id"></label>'
                '<label class="lz-label">实际下达人<input class="lz-input" name="issued_by" required></label>'
                '<label class="lz-label">实际接收人账号<input class="lz-input" name="recipient_id" required></label>'
                '<label class="lz-label">指定核验负责人账号<input class="lz-input" name="verifier_id"></label>'
                '<label class="lz-label">实际下达时间（含时区）<input class="lz-input" name="occurred_at" '
                'placeholder="2026-09-29T09:30:00+08:00" required></label>'
                '<label class="lz-label">线下依据<input class="lz-input" name="evidence" required></label>'
                '<label class="lz-label">明确截止时间（含时区，选填）<input class="lz-input" name="due_at" '
                'placeholder="2026-09-29T18:00:00+08:00"></label>'
                '<label class="lz-label">已知执行情况<input class="lz-input" name="known_progress"></label>'
                '<label class="lz-label">关联总任务编号（子任务选填）<input class="lz-input" name="parent_id"></label>'
                '<button class="lz-btn" type="submit">补录线下事实</button></form>')
        for task in tasks:
            history = ''.join(
                f'<li>{esc(item.get("recorded_at", ""))} · '
                f'{esc(history_labels.get(item.get("action"), item.get("action") or ("内容变更" if item.get("next_version") else "线下补录")))}'
                f' · v{esc(item.get("version", ""))}</li>' for item in task.history)
            details = (
                f'<fieldset><legend>{esc(task.id)} · v{task.version} · {esc(states[task.state])}</legend>'
                f'<p>{esc(task.content)}</p><p class="lz-note">实际下达 '
                f'{esc(task.history[0]["occurred_at"])}；本版实际时间 {esc(task.occurred_at)}；'
                f'初次补录 {esc(task.recorded_at)}；执行组 {esc(task.group_id or "待核对")}；'
                f'下达人 {esc(task.issued_by)}；接收人 {esc(mask_phone(task.recipient_id))}；'
                f'核验负责人 {esc(mask_phone(task.verifier_id) if task.verifier_id else "待指定")}。</p>'
                f'<p class="lz-note">明确截止时间：{esc(task.due_at or "未记录")}。'
                + ('已逾期，请人工追问；不自动认定拒收或失联。' if task.id in reminders else '')
                + '</p>'
                f'<p class="lz-note">线下依据：{esc(task.evidence)}；已知执行情况：'
                f'{esc(task.known_progress or "未记录")}。本记录不等于系统正式发布或专业复核。</p>'
                f'<details><summary>历史与当前版回执</summary><ul>{history}</ul></details>')
            if deputy and task.state not in {'closed', 'withdrawn', 'replaced', 'paused'} \
                    and record.status != 'closed':
                details += (
                    '<form method="post" action="/tasks/offline"><h4>补录内容变更</h4>'
                    f'<input type="hidden" name="event_id" value="{esc(record.event_id)}">'
                    f'<input type="hidden" name="task_id" value="{esc(task.id)}">'
                    f'<input type="hidden" name="expected_version" value="{task.version}">'
                    '<input type="hidden" name="action" value="amend">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    f'<label class="lz-label">变更后内容<input class="lz-input" name="content" value="{esc(task.content)}" required></label>'
                    f'<label class="lz-label">执行组编号<input class="lz-input" name="group_id" value="{esc(task.group_id)}"></label>'
                    f'<label class="lz-label">接收人账号<input class="lz-input" name="recipient_id" value="{esc(task.recipient_id)}" required></label>'
                    f'<label class="lz-label">核验负责人账号<input class="lz-input" name="verifier_id" value="{esc(task.verifier_id)}"></label>'
                    '<label class="lz-label">实际变更时间（含时区）<input class="lz-input" name="occurred_at" '
                    'placeholder="2026-09-29T09:45:00+08:00" required></label>'
                    '<label class="lz-label">变更依据<input class="lz-input" name="reason" required></label>'
                    f'<label class="lz-label">明确截止时间（含时区，选填）<input class="lz-input" name="due_at" value="{esc(task.due_at)}"></label>'
                    '<button class="lz-btn" type="submit">补录变更并生成新版</button></form>')
            if deputy and task.state not in {'closed', 'withdrawn', 'replaced'} \
                    and record.status != 'closed':
                control_actions = ((('resume', '恢复'), ('withdraw', '撤回'),
                                    ('replace', '替代')) if task.state == 'paused'
                                   else (('withdraw', '撤回'), ('replace', '替代'))
                                   if task.state == 'pending_verification'
                                   else (('pause', '暂停'), ('withdraw', '撤回'),
                                         ('replace', '替代')))
                control_actions += (('followup', '已线下追问'),
                                    ('escalate', '已线下升级'))
                control_options = ''.join(
                    f'<option value="{esc(key)}">{esc(label)}</option>'
                    for key, label in control_actions)
                details += (
                    '<form method="post" action="/tasks/offline"><h4>补录线下控制事实</h4>'
                    f'<input type="hidden" name="event_id" value="{esc(record.event_id)}">'
                    f'<input type="hidden" name="task_id" value="{esc(task.id)}">'
                    f'<input type="hidden" name="expected_version" value="{task.version}">'
                    '<input type="hidden" name="action" value="control">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    f'<label class="lz-label">控制类型<select class="lz-input" name="control_action">{control_options}</select></label>'
                    '<label class="lz-label">实际发生时间（含时区）<input class="lz-input" '
                    'name="occurred_at" placeholder="2026-09-29T10:00:00+08:00" required></label>'
                    '<label class="lz-label">依据<input class="lz-input" name="reason" required></label>'
                    '<label class="lz-label">替代任务号（仅替代时填写）<input class="lz-input" name="successor_id"></label>'
                    '<button class="lz-btn" type="submit">补录控制事实</button></form>')
            if actor == task.recipient_id and eligible.get(task.id) and task.state in actions:
                choices = ''.join(f'<option value="{esc(key)}">{esc(label)}</option>'
                                  for key, label in actions[task.state])
                details += (
                    '<form method="post" action="/tasks/offline"><h4>执行方本人回执</h4>'
                    f'<input type="hidden" name="event_id" value="{esc(record.event_id)}">'
                    f'<input type="hidden" name="task_id" value="{esc(task.id)}">'
                    f'<input type="hidden" name="expected_version" value="{task.version}">'
                    '<input type="hidden" name="action" value="feedback">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    f'<label class="lz-label">报告类型<select class="lz-input" name="feedback_action">{choices}</select></label>'
                    '<label class="lz-label">回执依据<input class="lz-input" name="basis" required></label>'
                    '<button class="lz-btn" type="submit">提交本版回执</button></form>')
            if actor == task.verifier_id and task.state == 'pending_verification':
                details += (
                    '<form method="post" action="/tasks/offline"><h4>指定负责人独立核验</h4>'
                    f'<input type="hidden" name="event_id" value="{esc(record.event_id)}">'
                    f'<input type="hidden" name="task_id" value="{esc(task.id)}">'
                    f'<input type="hidden" name="expected_version" value="{task.version}">'
                    '<input type="hidden" name="action" value="close">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    '<label class="lz-label">独立核验依据<input class="lz-input" name="evidence" required></label>'
                    + ('' if task.parent_id else
                       '<label class="lz-label">整体目标核验依据<input class="lz-input" '
                       'name="overall_goal_evidence" required></label>')
                    + '<button class="lz-btn" type="submit">核验并关闭</button></form>')
            entries.append(details + '</fieldset>')
        sections.append(
            f'<section class="lz-card"><h2>{esc(record.name)} · {esc(record.event_id)}</h2>'
            + ''.join(entries) + '</section>')
    deputy_nav = ('<a href="/dispatch">选队召集</a>'
                  '<a href="/dispatch/task-review">任务分类</a>'
                  if any(deputy for _, _, deputy, _, _ in views) else '')
    return _light_wrap(
        title='线下任务与回执', body_class=' lz-deputy-page',
        body=('<p class="lz-note">仅记录已经线下发生的指令、变更与报告；补录不重新通知，'
              '不补造当时未做的专业复核或会商。</p>'
              + (''.join(sections) or '<p>当前没有本人可查看的线下任务。</p>')),
        nav='<a href="/">首页</a>' + deputy_nav
            + '<a class="on" href="/tasks/offline">线下任务</a>',
        user=f'<span>{esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form('/logout/member', '登出'))


def render_material_transfer_page(state: PanelState, member: MemberSession,
                                  event_id: str, deputy: bool,
                                  captain_squad: str) -> str:
    """Show only the active event to its appointed deputy or selected captain."""
    selected = state.event_callouts(event_id)
    snapshot = state.ledger.snapshots.get(event_id)
    arrived = [sid for sid in sorted(selected)
               if snapshot and sid in snapshot.squads
               and snapshot.squads[sid].arrival_id
               and not _material_withdrawals.withdrawal_record(
                   state.ledger, event_id=event_id, squad_id=sid)]
    squad_name = lambda sid: (state.ledger.squads[sid].name
                              if sid in state.ledger.squads else sid)
    item_name = lambda iid: (state.ledger.items[iid].name
                             if iid in state.ledger.items else iid)
    event_field = f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
    parts = [
        '<div class="lz-card"><h2>本事件队伍物资调拨与借还</h2>'
        '<p class="lz-note">指定副总指挥确认数量后，转出队长报告实际交出；'
        '交出即从转出队扣除并列在途，接收队长报告实收后才加入接收队。'
        '借用须保留原借出关系，归还损坏不计可用。'
        '均为具名人工报告，尚未独立核验。保存中断请用原表单原样重试。</p></div>'
    ]
    if deputy and len(arrived) >= 2:
        squads = ''.join(f'<option value="{esc(sid)}">{esc(squad_name(sid))}</option>'
                         for sid in arrived)
        items = ''.join(f'<option value="{esc(iid)}">{esc(item.name)}</option>'
                        for iid, item in sorted(state.ledger.items.items()))
        parts.append(
            '<div class="lz-card"><h3>确认调拨或借用</h3>'
            '<form method="post" action="/material-transfers/report">'
            + event_field + '<input type="hidden" name="stage" value="approved">'
            + f'<input type="hidden" name="transfer_id" value="transfer-{_secrets.token_urlsafe(18)}">'
            + f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            + '<label class="lz-label">用途<select class="lz-input" name="purpose">'
              '<option value="transfer">普通调拨</option><option value="loan">借用（需归还）</option>'
              '</select></label>'
            + f'<label class="lz-label">转出队<select class="lz-input" name="from_squad">{squads}</select></label>'
            + f'<label class="lz-label">接收队<select class="lz-input" name="to_squad">{squads}</select></label>'
            + f'<label class="lz-label">物资<select class="lz-input" name="item_id">{items}</select></label>'
            + '<label class="lz-label">确认数量<input class="lz-input" name="quantity" type="number" min="0" step="any" required></label>'
            + '<label class="lz-label">线下确认依据<input class="lz-input" name="evidence" required></label>'
            + '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）<input class="lz-input" name="occurred_at"></label>'
            + '<button class="lz-btn" type="submit">登记调拨确认</button></form></div>'
        )
    ids = sorted({r.reference.object_id for r in state.ledger.transfer_records.values()
                  if r.reference.event_id == event_id})
    cards = []
    for transfer_id in ids:
        history = _transfer_history(state.ledger, event_id, transfer_id)
        first = json.loads(history[0].content_json)
        source, target = first['from_squad'], first['to_squad']
        if not deputy and captain_squad not in {source, target}:
            continue
        purpose = first.get('purpose', 'transfer')
        purpose_label = {'transfer': '调拨', 'loan': '借用', 'return': '归还'}[purpose]
        relation = (f' · 原借出 {esc(first["return_for"])}'
                    if purpose == 'return' else '')
        sent = len(history) >= 2
        received = sum(json.loads(r.content_json)['received_quantity'] for r in history[2:])
        remaining = first['quantity'] - received if sent else 0
        stage = ('待转出队报告交出' if not sent else
                 '在途待实收' if remaining > 1e-9 else '已全部实收')
        details = (f'<b>{esc(item_name(first["item_id"]))} × {first["quantity"]:g}</b>'
                   f'<p class="lz-note">{esc(squad_name(source))} → {esc(squad_name(target))}'
                   f' · {stage} · 已实收 {received:g} · 在途 {remaining:g}'
                   f' · {purpose_label}{relation}'
                   f' · 最新版本 {len(history)} · 编号 {esc(transfer_id)}</p>')
        if purpose == 'return':
            damaged = sum(json.loads(r.content_json).get('damaged_quantity', 0)
                          for r in history[2:])
            details += f'<p class="lz-note">累计归还损坏 {damaged:g}（人工报告，待核验）</p>'
        elif purpose == 'loan':
            status = loan_status(state.ledger, event_id=event_id,
                                 loan_transfer_id=transfer_id)
            details += (f'<p class="lz-note">已借实收 {status.borrowed:g}'
                        f' · 已批准归还 {status.return_approved:g}'
                        f' · 原流水未归还 {status.outstanding:g}</p>')
            approvable = status.borrowed - status.return_approved
            borrower_departure = _material_withdrawals.withdrawal_record(
                state.ledger, event_id=event_id, squad_id=target)
            lender_departure = _material_withdrawals.withdrawal_record(
                state.ledger, event_id=event_id, squad_id=source)
            if borrower_departure and status.outstanding > 1e-9:
                custody = next((entry for entry in json.loads(
                    borrower_departure.content_json).get('borrowed_custody', ())
                    if entry['loan_transfer_id'] == transfer_id), None)
                details += (
                    '<p class="lz-note">借入队伍已报告撤离；原借还流水未归还数量保留。'
                    '撤离位置和保管对象为原报告；撤离后实收、核验及业务结清状态由现任物资供应组长在接收核验页核对，'
                    '本处历史数量不作为业务未结清判据。借出队撤离收尾仍须另行办理。'
                    + (f'报告位置 {esc(custody["location"])}；'
                       f'保管对象 {esc(custody["custodian"])}；'
                       f'报告数量 {custody["quantity"]:g}。' if custody else
                       '撤离保管信息缺失，待核查。') + '</p>'
                )
            if (deputy and approvable > 1e-9 and len(arrived) >= 2
                    and not borrower_departure and not lender_departure):
                details += (
                    '<form method="post" action="/material-transfers/report">'
                    + event_field + '<input type="hidden" name="stage" value="approved">'
                    + '<input type="hidden" name="purpose" value="return">'
                    + f'<input type="hidden" name="return_for" value="{esc(transfer_id)}">'
                    + f'<input type="hidden" name="transfer_id" value="return-{_secrets.token_urlsafe(18)}">'
                    + f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    + f'<input type="hidden" name="item_id" value="{esc(first["item_id"])}">'
                    + f'<input type="hidden" name="from_squad" value="{esc(target)}">'
                    + f'<input type="hidden" name="to_squad" value="{esc(source)}">'
                    + f'<p class="lz-note">{esc(squad_name(target))} → {esc(squad_name(source))}'
                      f'，同物资可批准归还 {approvable:g}</p>'
                    + '<label class="lz-label">批准归还数量'
                      f'<input class="lz-input" name="quantity" type="number" min="0" max="{approvable:g}" step="any" required></label>'
                    + '<label class="lz-label">线下确认依据<input class="lz-input" name="evidence" required></label>'
                    + '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）<input class="lz-input" name="occurred_at"></label>'
                    + '<button class="lz-btn" type="submit">登记归还确认</button></form>'
                )
        if not sent and captain_squad == source:
            details += (
                '<form method="post" action="/material-transfers/report">'
                + event_field + '<input type="hidden" name="stage" value="sent">'
                + f'<input type="hidden" name="transfer_id" value="{esc(transfer_id)}">'
                + '<input type="hidden" name="expected_version" value="1">'
                + f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                + '<label class="lz-label">实际交出依据<input class="lz-input" name="evidence" required></label>'
                + '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）<input class="lz-input" name="occurred_at"></label>'
                + '<button class="lz-btn" type="submit">报告实际交出</button></form>'
            )
        elif sent and remaining > 1e-9 and captain_squad == target:
            details += (
                '<form method="post" action="/material-transfers/report">'
                + event_field + '<input type="hidden" name="stage" value="received">'
                + f'<input type="hidden" name="transfer_id" value="{esc(transfer_id)}">'
                + f'<input type="hidden" name="expected_version" value="{len(history)}">'
                + f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                + f'<label class="lz-label">本次实收数量（在途 {remaining:g}）'
                  '<input class="lz-input" name="quantity" type="number" min="0" step="any" required></label>'
                + ('' if purpose != 'return' else
                   '<label class="lz-label">其中损坏数量（人工报告，待核验）'
                   '<input class="lz-input" name="damaged_quantity" type="number" min="0" step="any" value="0" required></label>')
                + '<label class="lz-label">实际实收依据<input class="lz-input" name="evidence" required></label>'
                + '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）<input class="lz-input" name="occurred_at"></label>'
                + '<button class="lz-btn" type="submit">报告本次实收</button></form>'
            )
        cards.append('<div class="lz-card">' + details + '</div>')
    parts.extend(cards or ['<p class="lz-dim">当前没有本人可查看的调拨流水。</p>'])
    return _light_wrap(
        title='本事件物资调拨', body=''.join(parts), body_class=' lz-deputy-page',
        nav='<a href="/dispatch">选队召集</a>' if deputy else '<a href="/captain-arrival">队长到场</a>',
        user=f'<span>{esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form('/logout/member', '登出'))


def _radio_material_panel(state: PanelState, event_id: str) -> str:
    """Deputy's read-only inbox; no browser control can approve device identity."""
    from .radio_material_reports import assessments
    try:
        space, _, _ = state._appointment_space(event_id)
        reports = assessments(space, state.ledger, event_id)
    except (ValueError, PanelActionError, OSError):
        return '<section><h3>对讲物资报告</h3><p>对讲物资来源待核对，不能据此确认耗损。</p></section>'
    if not reports:
        return ''
    rows = []
    from .radio_material_reports import CAPTURED
    recorded = {entry.payload['capture']['source_call_id']: entry.recorded_at
                for entry in space.events if entry.kind == CAPTURED}
    for report in reports:
        capture = report.capture
        status = '已登记现场报告，非独立核验' if report.state == 'registered_report' else '待核对，未计耗损'
        rows.append('<li style="overflow-wrap:anywhere">'
            f'{esc(capture.source_call_id)} · {esc(status)} · '
            f'{"模拟来源" if capture.data_kind == "simulated" else "接入报告来源"}；'
            f'原文 {esc(capture.transcript or "无转写")}; 音频 {esc(capture.audio_ref or "未提供")}; '
            f'来源 {esc(capture.source_ref)}；设备 {esc(capture.device_id or "未知")}；频道 {esc(capture.channel_id)}；'
            f'呼号 {esc(capture.call_sign or "未知")}；发生 {esc(capture.occurred_at)}；'
            f'采集 {esc(capture.collected_at or "未知")}；接收 {esc(capture.received_at or "未知")}；'
            f'系统记录 {esc(recorded[capture.source_call_id])}；'
            f'耗损引用 {esc(report.loss_record_id or "无")}。</li>')
    return ('<section><h3>对讲物资报告</h3><p>未接可信身份适配器、模拟身份或内容歧义时保留待核。'
            '明确报告只复用现有队长耗损链；此处只读，不确认设备身份或发射对讲。</p><ul>'
            + ''.join(rows) + '</ul></section>')


def render_captain_callouts_page(state: PanelState, member: MemberSession,
                                 callouts: list[_callout.Callout]) -> str:
    cards = [
        '<div class="lz-card"><h2>队长召集回执</h2>'
        '<p class="lz-note">这里展示的是系统内生成的模拟电话／微信通知，'
        '不表示电话已拨通、微信已发送或已经送达。队长本人登录后填写的回执也明确标记为模拟；'
        '回执不等于出发或实际到场。</p>'
        '<p><a class="lz-btn" href="/captain-arrival">扫码载入本队名册并确认到场 →</a></p></div>'
    ]
    for callout in callouts:
        event = state.registry.events[callout.event_id]
        squad = state.ledger.squads.get(callout.squad_id)
        name = squad.name if squad else callout.squad_id
        if callout.reply:
            reply = callout.reply
            detail = (f'可出动 · 预计 {reply["expected_people"]} 人 · 预计到场 {esc(reply["eta"])}'
                      if reply['can_depart'] == 'yes'
                      else f'不能出动 · 原因：{esc(reply["reason"])}')
            action = (f'<p class="lz-note">队长已在系统内确认看到模拟通知；'
                      f'模拟回执已记录：{detail}。实际出发和到场尚未核实。</p>')
        else:
            action = (
                '<form method="post" action="/callouts/reply">'
                f'<input type="hidden" name="event_id" value="{esc(callout.event_id)}">'
                f'<input type="hidden" name="squad_id" value="{esc(callout.squad_id)}">'
                f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                '<label class="lz-label">能否出动<select class="lz-input" name="can_depart">'
                '<option value="yes">可以出动</option><option value="no">不能出动</option></select></label>'
                '<label class="lz-label">预计人数（可以出动时必填）'
                '<input class="lz-input" name="expected_people" inputmode="numeric"></label>'
                '<label class="lz-label">预计到场时间（ISO 含时区，可以出动时必填）'
                '<input class="lz-input" name="eta" placeholder="2026-09-28T15:00:00+08:00"></label>'
                '<label class="lz-label">不能出动原因（不能出动时必填）'
                '<input class="lz-input" name="reason"></label>'
                '<button type="submit" class="lz-btn">提交本人模拟回执</button></form>'
            )
        cards.append(
            f'<div class="lz-card"><h2>{esc(event.name)} · {esc(name)}</h2>'
            f'<p class="lz-note">{esc(_callout.CHANNELS[callout.channel])}模拟通知已生成，'
            '未发送、未送达。</p>'
            + (f'<p class="lz-note">演练假设通知失败：{esc(callout.simulation_failure["reason"])}。'
               '真实通道仍未发送；此记录不代表实际通知失败。</p>'
               if callout.simulation_failure else '')
            + action + '</div>'
        )
    return _light_wrap(
        title="队长召集回执", body=''.join(cards),
        nav='<a class="on" href="/callouts">召集回执</a><a href="/tasks/formal">正式任务接收</a>',
        user=f'<span>队长 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_captain_arrival_page(state: PanelState, member: MemberSession,
                                callouts: list[_callout.Callout], *, member_token=None, include_demo=True) -> str:
    cards = [
        '<div class="lz-card"><h2>队长扫码与到场确认</h2>'
        '<p class="lz-note">仅队长扫码匹配本事件已选中队伍。载入固有名册不等于人员到场；'
        '模拟通知回执也不等于到场。首次到场由队长本人报告，固定随队物资清单仅计入一次；'
        '后续补到只增人员，不重复计物资。到场为队长申报，尚未独立核验。</p></div>'
    ]
    from .resource_demo import panel as demo_panel
    if include_demo:cards.insert(0,demo_panel(state,member_token))
    for callout in callouts:
        event_id, squad_id = callout.event_id, callout.squad_id
        event = state.registry.events[event_id]
        squad = state.ledger.squads.get(squad_id)
        name = squad.name if squad is not None else squad_id
        loaded = state.captain_loaded_roster(event_id, squad_id)
        if loaded is None:
            action = (
                '<p class="lz-note">本队名册尚未载入，当前无人因本步骤计为到场。</p>'
                '<form method="post" action="/captain-arrival/scan">'
                f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                f'<input type="hidden" name="squad_id" value="{esc(squad_id)}">'
                '<label class="lz-label">事件二维码编号'
                '<input class="lz-input" name="qr_id" placeholder="例如 qr-001" required></label>'
                '<button type="submit" class="lz-btn">队长扫码并载入固有名册</button></form>'
            )
        else:
            snapshot = state.ledger.snapshots.get(event_id)
            arrival = snapshot.squads.get(squad_id) if snapshot else None
            withdrawn = _material_withdrawals.withdrawal_record(
                state.ledger, event_id=event_id, squad_id=squad_id) is not None
            arrived = set(arrival.arrived_phones) if arrival else set()
            readiness = state.event_readiness(event_id)
            roster = ''.join(
                f'<li>{esc(p["name"])}（{esc(mask_phone(p["phone"]))}） · '
                f'{"队长" if p["phone"] == loaded.captain_phone else "队员"} · '
                f'{"队长已申报到场" if p["phone"] in arrived else "尚未申报到场"} · '
                f'{esc(_readiness.STATUSES[readiness[p["phone"]].status] if p["phone"] in readiness else "可派状态待核定")}</li>'
                for p in loaded.members
            )
            if arrival is None:
                choices = ''.join(
                    '<label class="lz-label">'
                    f'<input type="checkbox" name="phone" value="{esc(p["phone"])}"> '
                    f'{esc(p["name"])}（{esc(mask_phone(p["phone"]))}）</label>'
                    for p in loaded.members if p["phone"] != loaded.captain_phone
                )
                action = (
                    '<p class="lz-note">名册已载入，尚未确认任何人到场。'
                    '全员已到则不勾选；有缺员时仅勾选未到人员。</p>'
                    '<form method="post" action="/captain-arrival/report">'
                    f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                    f'<input type="hidden" name="squad_id" value="{esc(squad_id)}">'
                    '<input type="hidden" name="mode" value="initial">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    '<h3>未到人员（可不选）</h3>' + (choices or '<p class="lz-dim">仅队长一人。</p>')
                    + '<button type="submit" class="lz-btn">确认本队到场并一次计入随队清单</button></form>'
                )
            else:
                choices = ''.join(
                    '<label class="lz-label">'
                    f'<input type="checkbox" name="phone" value="{esc(p["phone"])}"> '
                    f'{esc(p["name"])}（{esc(mask_phone(p["phone"]))}）</label>'
                    for p in loaded.members if p["phone"] not in arrived
                )
                action = (
                    f'<p class="lz-note">队长已申报到场 {len(arrived)}/{len(loaded.phones)} 人；'
                    '随队物资已按初次到场计入，未独立核验。</p>'
                )
                if choices and not withdrawn:
                    action += (
                        '<form method="post" action="/captain-arrival/report">'
                        f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                        f'<input type="hidden" name="squad_id" value="{esc(squad_id)}">'
                        '<input type="hidden" name="mode" value="supplement">'
                        f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                        '<h3>本次补到人员</h3>' + choices
                        + '<button type="submit" class="lz-btn">确认补到（不重复计物资）</button></form>'
                    )
            action = '<ul>' + roster + '</ul>' + action
            if arrived:
                choices = ''.join(
                    '<label class="lz-label">'
                    f'<input type="checkbox" name="phone" value="{esc(p["phone"])}"> '
                    f'{esc(p["name"])}（{esc(mask_phone(p["phone"]))}）</label>'
                    for p in loaded.members
                    if p["phone"] in arrived
                    and (p["phone"] not in readiness
                         or readiness[p["phone"]].status != "evacuated")
                )
                order_options = ''.join(
                    f'<option value="{esc(order_id)}">{esc(order_id)}</option>'
                    for order_id in state.event_readiness_orders(event_id)
                )
                action += (
                    '<div class="lz-card"><h3>队内部分人员状态报告</h3>'
                    '<p class="lz-note">只更新勾选的已申报到场人员；未报告者可派状态待核定。'
                    '当前可派仅计队长明确报告的“待命可派”，执行中、轮休、暂时无法执行和已撤离不计。'
                    '本报告不改变到场或随队物资。</p>'
                    '<form method="post" action="/captain-arrival/readiness">'
                    f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                    f'<input type="hidden" name="squad_id" value="{esc(squad_id)}">'
                    f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                    + choices
                    + '<label class="lz-label">本次状态<select class="lz-input" name="status">'
                    + ''.join(f'<option value="{esc(key)}">{esc(label)}</option>'
                              for key, label in _readiness.STATUSES.items())
                    + '</select></label>'
                    '<label class="lz-label">执行中关联任务（仅选执行中时必填）'
                    '<select class="lz-input" name="order_id"><option value="">不关联</option>'
                    + order_options + '</select></label>'
                    '<label class="lz-label">报告依据<input class="lz-input" name="basis" required></label>'
                    '<button type="submit" class="lz-btn">记录所选人员状态</button></form></div>'
                )
            action += _group_arrived_preview(state, event_id, member.phone)
            if (arrival is not None and arrival.arrival_id and squad is not None
                    and squad.leader_phone == member.phone):
                if not withdrawn:
                    action += _manual_material_loss_panel(state, event_id, squad_id)
                action += _team_material_withdrawal_panel(state, event_id, squad_id, member_token=member_token)
        cards.append(
            f'<div class="lz-card"><h2>{esc(event.name)} · {esc(name)}</h2>'
            + action + '</div>'
        )
    return _light_wrap(
        title="队长扫码与到场", body=''.join(cards) + '<script src="/static/material-withdrawal.js" defer></script>',
        nav='<a href="/callouts">召集回执</a><a class="on" href="/captain-arrival">队长到场</a>'
            '<a href="/material-transfers">事件物资调拨</a>',
        user=f'<span>队长 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
        body_class=' lz-captain-page',
    )


def _team_material_withdrawal_panel(state: PanelState, event_id: str,
                                    squad_id: str, *, member_token=None) -> str:
    return (f'<section data-withdrawal style="min-width:0;overflow-wrap:anywhere" data-event="{esc(event_id)}" data-squad="{esc(squad_id)}">'
            + _render_team_material_withdrawal_panel(state, event_id, squad_id, member_token=member_token)
            + '<p class="withdrawal-message" role="status" aria-live="polite"></p></section>')


def _render_team_material_withdrawal_panel(state: PanelState, event_id: str,
                                           squad_id: str, *, member_token=None) -> str:
    """Show the current captain's draft without changing custody or audit."""
    heading = ('<div class="lz-card"><h3>本队撤离物资报告</h3>'
               '<p class="lz-note">默认将本队当前剩余物资随队撤出，不再计入现场可用。'
               '留置、移交须填写数量、实际位置与保管对象；这里只记录队长报告，'
               '不等于接收方独立核验，也不改变长期随队底账。</p>')
    try:
        review = state.material_withdrawal_review(event_id, captain_token=member_token, squad_id=squad_id)
        existing = review.existing
        if existing is not None:
            content = json.loads(existing.content_json)
            lines = content['lines']
            summary = ''.join(
                f'<li>{esc(state.ledger.items[line["item_id"]].name if line["item_id"] in state.ledger.items else line["item_id"])}：'
                f'随队撤出 {line["carried_out"]:g}，留置待核 {line["retained"]:g}，'
                f'移交待核 {line["handed_over"]:g}</li>' for line in lines)
            exceptions = ''.join(
                f'<li>{"留置" if entry["kind"] == "retained" else "移交"} '
                f'{esc(state.ledger.items[entry["item_id"]].name if entry["item_id"] in state.ledger.items else entry["item_id"])} '
                f'{entry["quantity"]:g}：位置 {esc(entry["location"])}；'
                f'保管对象 {esc(entry["custodian"])}（待独立核验）</li>'
                for entry in content['exceptions'])
            custody = ''.join(
                f'<li>未归还借用 {esc(entry["loan_transfer_id"])}（撤离时原报告） '
                f'{entry["quantity"]:g}：位置 {esc(entry["location"])}；'
                f'保管对象 {esc(entry["custodian"])}。仅为撤离时报告；'
                '当前接收、核验和业务结清状态由现任物资供应组长另行核对。</li>'
                for entry in content.get('borrowed_custody', ()))
            settled = ''.join(f'<li>已核对借出结清引用：{esc(entry["loan_transfer_id"])}，'
                              f'原流水历史量 {entry["quantity"]:g}；结清记录 {esc(entry["settlement_id"])}。'
                              '后勤实收不计入本队撤出量或现场可用。</li>'
                              for entry in content.get('settled_lender_loans', ()))
            return (heading + f'<p data-withdrawal-record="{esc(existing.id)}">本队已有具名撤离报告，不能重复填报。</p><ul>'
                    + summary + exceptions + custody + settled + '</ul></div>')
        draft = review.draft
    except (PermissionError, PanelActionError, ValueError, KeyError, TypeError, AttributeError):
        return heading + '<p>本队物资来源或数量待核对，暂不能填报撤离。</p></div>'
    if draft.unresolved_transfers:
        transfers = '、'.join(esc(value) for value in draft.unresolved_transfers)
        loans = '、'.join(f'{esc(key)}（未归还 {amount:g}）'
                         for key, amount in draft.open_loans)
        return (heading + '<p>本队仍有关联调拨阶段未结，须先完成交出或实收后再报告撤离。</p>'
                + (f'<p>未结调拨：{transfers}</p>' if transfers else '')
                + (f'<p>未归还借用：{loans}</p>' if loans else '')
                + '<p><a href="/material-transfers">查看事件物资调拨与借还</a></p></div>')
    settled_info = ''.join(f'<li>借出 {esc(entry.loan_transfer_id)}：已核对整笔业务结清；'
                          f'原流水历史量 {entry.quantity:g}；结清记录 {esc(entry.settlement_id)}。'
                          '后勤实收不计入本队撤出量或现场可用。</li>'
                          for entry in review.settled_lender_loans)
    if review.unsettled_lender_loans:
        loans = '、'.join(esc(value) for value in review.unsettled_lender_loans)
        return (heading + f'<p>本队借出装备仍未满足全数后勤实收、全部核验通过及明确结清：{loans}。</p>'
                '<p>原流水未归还借用为历史数量，不直接代表当前业务未结。</p>'
                '<p>当前不能报告物资撤离；请由本事件物资供应组现任组长办理收尾。</p>'
                + ('<ul>' + settled_info + '</ul>' if settled_info else '')
                + '<p><a href="/material-transfers">查看事件物资调拨与借还</a></p></div>')
    if state.registry.events[event_id].status not in {'active', 'upgraded'}:
        return heading + '<p>事件已关闭，不能新增撤离报告。</p></div>'
    loan_rows = []
    owed_by_item = {}
    for loan_id, amount in review.borrowed_loans:
        loan = json.loads(_transfer_history(state.ledger, event_id, loan_id)[0].content_json)
        item = state.ledger.items[loan['item_id']]
        owed_by_item[loan['item_id']] = owed_by_item.get(loan['item_id'], 0) + amount
        loan_rows.append(
            '<div class="lz-card">'
            f'<b>未归还借用 {esc(loan_id)} · {esc(item.name)} {amount:g} {esc(item.unit)}</b>'
            f'<input type="hidden" name="loan_transfer_id" value="{esc(loan_id)}">'
            f'<input type="hidden" name="loan_quantity" value="{amount!r}">'
            '<p class="lz-note">该数量仍需归还，须包含在随队撤出量内；'
            '报告保管位置不等于已归还或经接收方核验。</p>'
            '<label class="lz-label">撤离后实际保管位置'
            '<input class="lz-input" name="loan_location" required></label>'
            '<label class="lz-label">撤离后保管对象'
            '<input class="lz-input" name="loan_custodian" required></label>'
            '</div>')
    if any(next((line.on_site_before for line in draft.lines
                 if line.item_id == item_id), 0) + 1e-9 < amount
           for item_id, amount in owed_by_item.items()):
        return (heading + '<p>本队未归还借用量超过当前账面可用量，须先核对物资去向；'
                '暂不能报告整队撤离。</p></div>')
    prefix = (f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
              f'<input type="hidden" name="squad_id" value="{esc(squad_id)}">'
              f'<input type="hidden" name="csrf" value="{esc(state.member_session(member_token).custody_csrf)}">'
              f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">')
    balances = []
    rows = []
    for line in draft.lines:
        item = state.ledger.items[line.item_id]
        balances.append(f'<li>{esc(item.name)}：当前可用 {line.on_site_before:g} {esc(item.unit)}；'
                        f'无例外则随队撤出 {line.carried_out:g} {esc(item.unit)}</li>')
        if line.on_site_before <= 0:
            continue
        for kind, label in (('retained', '留置'), ('handed_over', '移交')):
            rows.append(
                '<div class="lz-card">'
                f'<b>{esc(item.name)} · {label}例外（可留空）</b>'
                f'<input type="hidden" name="exception_item_id" value="{esc(line.item_id)}">'
                f'<input type="hidden" name="exception_kind" value="{kind}">'
                f'<label class="lz-label">{label}数量'
                f'<input class="lz-input" name="exception_quantity" type="number" min="0" step="any" max="{line.on_site_before!r}"></label>'
                '<label class="lz-label">实际位置<input class="lz-input" name="exception_location"></label>'
                '<label class="lz-label">保管对象<input class="lz-input" name="exception_custodian"></label>'
                '</div>')
    return (heading + '<form method="post" action="/captain-arrival/material-withdrawal">'
            + prefix + '<p>请核对报告前账面数量；如有未登记变化，先补齐相应流水。</p>'
            + '<ul>' + ''.join(balances) + settled_info + '</ul>' + ''.join(rows) + ''.join(loan_rows)
            + '<label class="lz-label">线下撤离清单或原始依据'
              '<input class="lz-input" name="evidence" required></label>'
              '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）'
              '<input class="lz-input" name="occurred_at"></label>'
              '<button type="submit" class="lz-btn">报告本队物资撤离</button>'
              '</form></div>')


def _withdrawal_exceptions_from_form(form: dict) -> tuple[_material_withdrawals.WithdrawalException, ...]:
    fields = ('exception_item_id', 'exception_kind', 'exception_quantity',
              'exception_location', 'exception_custodian')
    columns = [form.get(field, []) for field in fields]
    if len({len(column) for column in columns}) != 1 or len(columns[0]) > 200:
        raise ValueError('撤离例外字段不完整或数量过多。')
    entries = []
    for item_id, kind, raw_quantity, location, custodian in zip(*columns):
        if not raw_quantity.strip():
            if location.strip() or custodian.strip():
                raise ValueError('填写例外位置或保管对象时必须填写数量。')
            continue
        quantity = float(raw_quantity)
        if quantity == 0 and not location.strip() and not custodian.strip():
            continue
        entries.append(_material_withdrawals.WithdrawalException(
            item_id, quantity, kind, location, custodian))
    return tuple(entries)


def _withdrawal_borrowed_custody_from_form(form: dict) -> tuple[_material_withdrawals.BorrowedCustody, ...]:
    fields = ('loan_transfer_id', 'loan_quantity', 'loan_location', 'loan_custodian')
    columns = [form.get(field, []) for field in fields]
    if len({len(column) for column in columns}) != 1 or len(columns[0]) > 200:
        raise ValueError('未归还借用保管字段不完整或数量过多。')
    return tuple(_material_withdrawals.BorrowedCustody(
        loan_id, float(quantity), location, custodian)
        for loan_id, quantity, location, custodian in zip(*columns))


def _manual_material_loss_panel(state: PanelState, event_id: str, squad_id: str) -> str:
    snapshot = state.ledger.snapshots.get(event_id)
    squad = snapshot.squads.get(squad_id) if snapshot else None
    if squad is None or not squad.arrival_id:
        return ''
    from .material_loss_sources import loss_items
    try:
        item_ids = loss_items(state.ledger, event_id, squad_id)
        from .resource_maintenance import standard_line, scope
        item_ids = [iid for iid in item_ids if not standard_line(state.ledger, squad_id, iid)
                    or scope(state.ledger,iid) in {'public','extra'} and any(
                        r.reference.event_id==event_id and json.loads(r.content_json).get('squad_id')==squad_id
                        and json.loads(r.content_json).get('item_id')==iid for r in state.ledger.supply_receipts.values())]
        reported_items = {json.loads(r.content_json)['item_id'] for r in state.ledger.flow_records.values()
                          if r.reference.event_id == event_id and r.reference.object_kind == 'material_loss'
                          and json.loads(r.content_json)['squad_id'] == squad_id}
        for item_id in (set(item_ids) | reported_items) - set(squad.lines):
            from .material_inventory_projection import project
            space, _, _ = state._appointment_space(event_id)
            project(space, state.ledger, event_id=event_id, item_id=item_id)
    except (ValueError, KeyError, TypeError):
        return '<div class="lz-card"><p class="lz-note">物资数量来源待核对，暂不能报告耗损，请先核对到场、调拨和报告流水。</p></div>'
    if not item_ids:
        return '<p class="lz-note">消防队伍自带标配由外部维护，本系统不再办理标配耗损；原耗损记录在历史资料中保留。</p>'
    choices = ''.join(
        f'<option value="{esc(item_id)}">{esc(state.ledger.items[item_id].name if item_id in state.ledger.items else item_id)}</option>'
        for item_id in item_ids
    )
    prefix = (f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
              f'<input type="hidden" name="squad_id" value="{esc(squad_id)}">')
    latest = {}
    for record in state.ledger.flow_records.values():
        ref = record.reference
        if ref.event_id != event_id or ref.object_kind != 'material_loss':
            continue
        if record.data_kind != 'reported' or not record.source.startswith(
                _manual_material_reports.SOURCE_PREFIX):
            continue
        content = json.loads(record.content_json)
        if content.get('squad_id') != squad_id:
            continue
        if ref.object_id not in latest or latest[ref.object_id].reference.version < ref.version:
            latest[ref.object_id] = record
    cards = []
    for record in sorted(latest.values(), key=lambda item: item.reference.object_id):
        content = json.loads(record.content_json)
        item_id = content['item_id']
        kind = content['kind']
        item_name = state.ledger.items[item_id].name if item_id in state.ledger.items else item_id
        basis = content.get('inventory_basis')
        basis_html = ('<details><summary>报告时数量来源</summary><p style="overflow-wrap:anywhere">'
            f'到场：{esc(basis["arrival_ref"])}；调拨：{esc("、".join(basis["transfer_refs"]) or "无")}'
            f'；先前耗损：{esc("、".join(basis["loss_refs"]) or "无")}</p></details>'
            if basis else '<p class="lz-note">旧记录未冻结调拨来源，沿用原到场量核对。</p>')
        source_word = record.source
        if standard_line(state.ledger, squad_id, item_id) and content.get('inventory_scope') != 'public_extra':
            cards.append('<details><summary>原标配耗损历史（只读保留）</summary><p>'+esc(item_name)+' · '+esc(str(content['amount']))+' · '+esc(source_word)+'</p></details>')
            continue
        radio_details = ''
        from .radio_material_reports import PREFIX as RADIO_PREFIX
        if source_word.startswith(_manual_material_reports.SOURCE_PREFIX + RADIO_PREFIX):
            radio_details = ('<details><summary>对讲原始来源详情</summary>'
                f'<p style="overflow-wrap:anywhere">{esc(source_word)}</p></details>')
            source_word = '对讲现场报告，原始通话和身份接入依据保留；非独立核验'
        cards.append(
            '<div class="lz-card"><b>'
            f'{esc(item_name)} · {"消耗" if kind == "consumed" else "损坏"} '
            f'{content["amount"]:g} · 版本 {record.reference.version}</b>'
            f'<p class="lz-note" style="overflow-wrap:anywhere">报告人 {esc(state.registry.library[record.actor_id].name if record.actor_id in state.registry.library else record.actor_id)}'
            f' · 来源 {esc(source_word)} · 发生时间 {esc(record.times.occurred_at or "未提供")}'
            f' · 记录时间 {esc(record.times.recorded_at)}'
            f' · 更正理由 {esc(content.get("reason") or "—")}</p>'
            + basis_html + radio_details
            +
            '<form method="post" action="/captain-arrival/material-loss">'
            + prefix
            + f'<input type="hidden" name="item_id" value="{esc(item_id)}">'
            + f'<input type="hidden" name="kind" value="{esc(kind)}">'
            + f'<input type="hidden" name="loss_id" value="{esc(record.reference.object_id)}">'
            + f'<input type="hidden" name="expected_version" value="{record.reference.version}">'
            + f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            + '<label class="lz-label">更正后的总量<input class="lz-input" name="amount" type="number" min="0" step="any" required></label>'
            + '<label class="lz-label">新报告依据<input class="lz-input" name="evidence" required></label>'
            + '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）<input class="lz-input" name="occurred_at"></label>'
            + '<label class="lz-label">更正理由<input class="lz-input" name="reason" required></label>'
            + '<button type="submit" class="lz-btn lz-btn2">更正本条报告</button></form></div>'
        )
    return (
        '<div class="lz-card"><h3>本队物资消耗／损坏人工报告</h3>'
        '<p class="lz-note">仅记队长本人报告，尚未独立核验；演练依据请明确注明模拟。'
        '补到人员不会重复增加随队清单。更正替换本条现行数量，原版本和依据保留。</p>'
        '<p class="lz-note">原到场基线保持；已实收调拨参与余额核对，批准、在途和归还损坏不计可用。报告时的数量来源随版本保留。</p>'
        '<p class="lz-note">初次清单外的物资，须有本事件已实收调拨才可报告；不会增加原到场或长期随队清单。</p>'
        '<form method="post" action="/captain-arrival/material-loss">'
        + prefix
        + f'<input type="hidden" name="loss_id" value="loss-{_secrets.token_urlsafe(18)}">'
        + '<input type="hidden" name="expected_version" value="0">'
        + f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
        + f'<label class="lz-label">物资<select class="lz-input" name="item_id">{choices}</select></label>'
        + '<label class="lz-label">报告类型<select class="lz-input" name="kind">'
        '<option value="consumed">消耗</option><option value="damaged">损坏</option></select></label>'
        '<label class="lz-label">数量<input class="lz-input" name="amount" type="number" min="0" step="any" required></label>'
        '<label class="lz-label">原始依据<input class="lz-input" name="evidence" required></label>'
        '<label class="lz-label">实际发生时间（已知时填 ISO 含时区）<input class="lz-input" name="occurred_at"></label>'
        '<button type="submit" class="lz-btn">记录人工报告</button></form>'
        + ''.join(cards) + '</div>'
    )


def _group_arrived_preview(state: PanelState, event_id: str, phone: str) -> str:
    """A member sees only the arrived people in their current event group."""
    people = state.event_group_people(event_id)
    mine = people.get(phone)
    if mine is None or not mine.pos_code:
        return '<p class="lz-dim">本事件组别待核定，暂不能显示所在组到场人员。</p>'
    arrived = sorted(
        (p for p in people.values() if p.pos_code == mine.pos_code and p.arrived),
        key=lambda p: (p.name, p.phone),
    )
    lines = ''.join(
        f'<li>{esc(p.name)} · {esc(p.unit)}（{esc(mask_phone(p.phone))}）'
        f' · {esc(p.presence_basis)}</li>'
        for p in arrived
    )
    return (
        f'<h3>{esc(SLOT_INDEX[mine.pos_code].name)} · 本组已申报到场 {len(arrived)} 人</h3>'
        + ('<ul>' + lines + '</ul>' if lines else '<p class="lz-dim">暂无到场申报。</p>')
        + f'<p><a class="lz-link" href="/personnel/groups?event_id={esc(event_id)}">查看本组人员与到场状态 →</a></p>'
    )


def _group_origin_label(state: PanelState, person: _groups.GroupPerson) -> str:
    if person.origin == "temporary":
        return "临时人员"
    if person.origin == "fixed_squad":
        squad = state.ledger.squads.get(person.source_id)
        return squad.name if squad is not None else person.source_id
    return "旧个人入岗"


def render_temporary_page(state: PanelState, member: MemberSession,
                          member_token: str) -> str:
    cards = [
        '<div class="lz-card"><h2>临时人员登记</h2>'
        '<p class="lz-note">本人登录后逐人输入事件二维码编号，登记进入待编组区。'
        '演示环境以输入编号代替摄像头扫码。登记、到场、编组与可派状态分别记录；'
        '本页不修改固定中队名册及随队物资。</p>'
        '<form method="post" action="/temporary/scan">'
        '<label class="lz-label">事件编号<input class="lz-input" name="event_id" required></label>'
        '<label class="lz-label">二维码编号<input class="lz-input" name="qr_id" required></label>'
        '<button type="submit" class="lz-btn">本人扫码登记</button></form></div>'
    ]
    for event_id, notices in state.member_local_notices(member_token).items():
        record = state.registry.events[event_id]
        kinds = '、'.join(
            f'{_local_notice.ROLES[n.local_role]} · {_callout.CHANNELS[n.channel]}'
            for n in notices
        )
        cards.append(
            f'<div class="lz-card"><h2>{esc(record.name)} · 系统内模拟通知记录</h2>'
            f'<p>{esc(kinds)}</p>'
            '<p class="lz-note">值班人员已准备模拟通知记录；电话未拨打、微信未发送，'
            '不表示已送达或本人已到场。请以本人扫码登记结果为准。</p></div>'
        )
    for event_id in state.temporary_events(member_token):
        record = state.registry.events[event_id]
        people = state.temporary_people(event_id)
        mine = people[member.phone]
        if not mine.arrived_at:
            arrival = _hidden_post(
                "/temporary/arrive", {"event_id": event_id}, "本人确认到场"
            )
        else:
            arrival = f'<p>已确认到场：{esc(mine.arrived_at)}</p>'
        current = state.event_group_people(event_id)[member.phone]
        if current.pos_code:
            slot = SLOT_INDEX[current.pos_code]
            leader_link = (
                '<p><a class="lz-link" href="/temporary/leader">报告本组人员状态 →</a></p>'
                if state.event_temporary_group_leaders(event_id).get(current.pos_code)
                == member.phone else ''
            )
            assignment = (
                f'<p><b>系统内编组安排：</b>{esc(slot.name)}</p>'
                '<p class="lz-note">安排已在本页可查看；未接入电话／微信发送通道，'
                '不表示已外发或送达。编组不等于可派。</p>'
                + _group_arrived_preview(state, event_id, member.phone)
                + leader_link
            )
        else:
            assignment = '<p class="lz-dim">待本事件指定副总指挥编组。</p>'
        cards.append(
            f'<div class="lz-card"><h2>{esc(record.name)} · {esc(event_id)}</h2>'
            f'<p>已登记：{esc(mine.registered_at)}</p>{arrival}{assignment}</div>'
        )
    return _light_wrap(
        title="临时人员", body=''.join(cards),
        nav='<a class="on" href="/temporary">临时人员</a><a href="/login">登录页</a>',
        user=f'<span>{esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_temporary_assign_page(state: PanelState, member: MemberSession,
                                 event_ids: list[str]) -> str:
    cards = [
        '<div class="lz-card"><h2>人员编组与调组</h2>'
        '<p class="lz-note">仅本事件指定副总指挥确认临时人员初次编组及逐人调组。'
        '初次编组可单选或批量选择；调组逐人留痕。系统内安排可供本人查看，'
        '不代表外部通知送达、到场或可派。</p></div>'
    ]
    options = ''.join(
        f'<option value="{esc(slot.pos_code)}">{esc(slot.name)}</option>'
        for slot in POSITION_SLOTS
    )
    for event_id in event_ids:
        record = state.registry.events[event_id]
        people = state.temporary_people(event_id)
        all_people = state.event_group_people(event_id)
        leaders = state.event_temporary_group_leaders(event_id)
        pending = [p for p in people.values() if not p.pos_code]
        assigned = [p for p in people.values() if p.pos_code]
        choices = ''.join(
            '<label class="lz-label">'
            f'<input type="checkbox" name="phone" value="{esc(p.phone)}"> '
            f'{esc(p.name)} · {esc(p.unit)}（{esc(mask_phone(p.phone))}）'
            f' · {"已确认到场" if p.arrived_at else "未确认到场"}</label>'
            for p in sorted(pending, key=lambda p: p.name)
        )
        form = (
            '<form method="post" action="/temporary/assign">'
            f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
            f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            + choices
            + '<label class="lz-label">编入现有组<select class="lz-input" name="pos_code">'
            + options + '</select></label>'
            + '<button type="submit" class="lz-btn">确认编组</button></form>'
            if pending else '<p class="lz-dim">暂无待编组临时人员。</p>'
        )
        assigned_lines = ''.join(
            f'<li>{esc(p.name)} · {esc(SLOT_INDEX[all_people[p.phone].pos_code].name)}'
            f' · {"已确认到场" if p.arrived_at else "未确认到场"}</li>'
            for p in sorted(assigned, key=lambda p: p.name)
        )
        transfer_options = ''.join(
            f'<option value="{esc(p.phone)}">{esc(p.name)} · '
            f'{esc(SLOT_INDEX[p.pos_code].name if p.pos_code else "组别待核定")} · '
            f'{esc(_group_origin_label(state, p))}</option>'
            for p in sorted(all_people.values(), key=lambda p: (p.name, p.phone))
            if p.origin != "temporary" or p.pos_code
        )
        transfer_form = (
            '<form method="post" action="/personnel/transfer">'
            f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
            f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            '<label class="lz-label">人员<select class="lz-input" name="phone">'
            + transfer_options + '</select></label>'
            '<label class="lz-label">目标组<select class="lz-input" name="to_pos_code">'
            + options + '</select></label>'
            '<button type="submit" class="lz-btn">确认按人调组</button></form>'
            if transfer_options else '<p class="lz-dim">本事件尚无可调组人员。</p>'
        )
        leader_forms = []
        for code in sorted({p.pos_code for p in all_people.values()
                            if p.origin == "temporary" and p.pos_code and p.arrived}):
            candidates = sorted((p for p in all_people.values()
                                 if p.origin == "temporary" and p.pos_code == code
                                 and p.arrived), key=lambda p: (p.name, p.phone))
            current_leader = leaders.get(code, "")
            leader_forms.append(
                f'<div class="lz-card"><h3>{esc(SLOT_INDEX[code].name)} · '
                f'当前组长：{esc(all_people[current_leader].name) if current_leader else "待指定"}</h3>'
                '<form method="post" action="/temporary/leader/appoint">'
                f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                f'<input type="hidden" name="pos_code" value="{esc(code)}">'
                f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
                '<label class="lz-label">本组已到场临时人员<select class="lz-input" name="phone">'
                + ''.join(f'<option value="{esc(p.phone)}">{esc(p.name)}（{esc(mask_phone(p.phone))}）</option>'
                          for p in candidates)
                + '</select></label>'
                '<label class="lz-label">线下指定依据<input class="lz-input" name="evidence" required></label>'
                '<button type="submit" class="lz-btn">登记本事件组长</button></form></div>'
            )
        cards.append(
            f'<div class="lz-card"><h2>{esc(record.name)} · {esc(event_id)}</h2>'
            f'<p>待编组 {len(pending)} 人 · 已编组 {len(assigned)} 人</p>'
            + form + '<h3>已编组</h3>'
            + ('<ul>' + assigned_lines + '</ul>' if assigned else '<p class="lz-dim">暂无。</p>')
            + '<h3>按人调组</h3><p class="lz-note">仅更新本事件组别；'
              '不改固定中队归属、到场申报、随队物资或组长权限。任务交接请人工核对。</p>'
            + transfer_form
            + '<h3>临时及保障组组长</h3><p class="lz-note">只有本事件指定副总指挥登记的'
              '本组已到场临时人员才获得本组状态报告权限；编组、个人长期“队长”标记和调组'
              '均不自动授予组长权限。更换组长留审计。</p>'
            + (''.join(leader_forms) if leader_forms else '<p class="lz-dim">暂无可指定的已到场临时组员。</p>')
            + f'<p><a class="lz-link" href="/personnel/groups?event_id={esc(event_id)}">查看固定组织与到场情况 →</a></p>'
            + '</div>'
        )
    return _light_wrap(
        title="人员编组／调组", body=''.join(cards),
        nav='<a href="/">人员架构</a><a href="/dispatch">选队召集</a><a class="on" href="/temporary/assign">人员编组／调组</a>',
        user=f'<span>指定副总指挥 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_temporary_leader_page(state: PanelState, member: MemberSession,
                                 assignments: list[tuple[str, str]],
                                 closeout_events: list[str] | None = None) -> str:
    cards = [
        '<div class="lz-card"><h2>本组临时人员状态报告</h2>'
        '<p class="lz-note">组长只可更新本事件本组已申报到场的临时人员。'
        '未报告者可派待核定；待命可派仅是组长报告，未做现场独立核验。'
        '报告不改变本人到场、编组或随队物资。</p></div>'
    ]
    if closeout_events:
        cards.append('<div class="lz-card"><h2>关闭事件物资收尾</h2>'
                     '<p><a href="/materials/closeout">选择关闭事件，办理既有物资收尾 →</a></p></div>')
    for event_id, code in assignments:
        people = state.event_group_people(event_id)
        readiness = state.event_readiness(event_id)
        options = ''.join(
            '<label class="lz-label">'
            f'<input type="checkbox" name="phone" value="{esc(p.phone)}"> '
            f'{esc(p.name)}（{esc(mask_phone(p.phone))}） · '
            f'{esc(_readiness.STATUSES[readiness[p.phone].status] if p.phone in readiness else "可派待核定")}'
            '</label>'
            for p in sorted(people.values(), key=lambda p: (p.name, p.phone))
            if p.origin == "temporary" and p.pos_code == code and p.arrived
            and (p.phone not in readiness or readiness[p.phone].status != "evacuated")
        )
        order_options = ''.join(
            f'<option value="{esc(order_id)}">{esc(order_id)}</option>'
            for order_id in state.event_readiness_orders(event_id)
        )
        form = (
            '<form method="post" action="/temporary/leader/readiness">'
            f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
            f'<input type="hidden" name="pos_code" value="{esc(code)}">'
            f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            + options
            + '<label class="lz-label">本次状态<select class="lz-input" name="status">'
            + ''.join(f'<option value="{esc(key)}">{esc(label)}</option>'
                      for key, label in _readiness.STATUSES.items())
            + '</select></label>'
            '<label class="lz-label">执行中关联任务（仅选执行中时必填）'
            '<select class="lz-input" name="order_id"><option value="">不关联</option>'
            + order_options + '</select></label>'
            '<label class="lz-label">报告依据<input class="lz-input" name="basis" required></label>'
            '<button type="submit" class="lz-btn">报告所选人员状态</button></form>'
            if options else '<p class="lz-dim">本组暂无可更新的已到场临时人员。</p>'
        )
        custody_link = (f'<p><a href="/material-custody?event_id={esc(event_id)}">'
                        '办理撤离后借用装备接收 →</a></p>'
                        if code == 'log-material' and event_id == state.active_event_id else '')
        cards.append(
            f'<div class="lz-card"><h3>{esc(state.registry.events[event_id].name)} · '
            f'{esc(SLOT_INDEX[code].name)}</h3>' + custody_link
            + (f'<p><a href="/material-supply?event_id={esc(event_id)}">登记人工补给联系 →</a> · <a href="/material-supply/execution">核实库存与补给执行 →</a> · <a href="/materials/maintenance-authority">长期物资维护授权 →</a></p>'
               if code == 'log-material' and event_id == state.active_event_id else '')
            + form + '</div>'
        )
    return _light_wrap(
        title="临时组长状态报告", body=''.join(cards),
        nav='<a href="/temporary">临时人员</a><a class="on" href="/temporary/leader">本组状态</a>',
        user=f'<span>组长 {esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_event_groups_page(state: PanelState, event_id: str, *,
                             viewer_phone: str, deputy: bool) -> str:
    people = state.event_group_people(event_id)
    readiness = state.event_readiness(event_id)
    record = state.registry.events[event_id]
    mine = people.get(viewer_phone)
    visible_code = mine.pos_code if mine else ""
    sections = [
        '<div class="lz-card"><h2>本事件固定组织与到场</h2>'
        f'<p>{esc(record.name)} · {esc(event_id)}</p>'
        '<p class="lz-note">沿用原组织槽位；固定中队成员来自队长冻结名册，'
        '临时人员来自本人登记。到场为队长或本人申报，未独立核验；'
        '组别调动不改变中队、物资及可派状态。</p></div>'
    ]
    if deputy:
        needs = state.event_force_needs(event_id)
        state_word = {
            "initial_pending": "首次启动后待核定初步人力需求",
            "current": "当前资料已由指定副总指挥复核",
            "needs_reassessment": "资料变化，需重新评估人力需求",
        }[needs.state]
        changes = '、'.join(_force_needs.SOURCE_LABELS[key]
                           for key in needs.changed_sources)
        rows = ''.join(
            f'<tr><td>{esc(row.label)}</td><td>{row.arrived_reported}</td>'
            f'<td>{row.standby_reported}</td><td>{esc(row.status)}</td></tr>'
            for row in needs.rows
        )
        last = needs.latest_review
        last_text = (
            f'<p>上次复核：{esc(last["reviewed_at"])} · '
            f'{esc(last["note"])}</p>' if last else '<p>尚无人工复核记录。</p>'
        )
        sections.append(
            '<div class="lz-card"><h2>初步人力需求与变化重评</h2>'
            f'<p><b>{esc(state_word)}</b></p>'
            f'<p>需核对来源：{esc(changes or "当前输入未变化")}；'
            f'组别待核定 {needs.unknown_group_count} 人。</p>'
            '<p class="lz-note">系统提示核定作战、通信、医疗及其他保障需求；'
            '到场和待命可派只是人员供给事实，不是需求人数或任务适配结论。'
            '请结合任务、地形、天气、装备、能力及持续时间人工判断；'
            '缺少专业参数时不生成需求缺口数，也不自动召集或调配。</p>'
            '<table class="lz-table"><tr><th>需核定类别</th><th>申报到场</th>'
            '<th>报告待命可派</th><th>初步需求</th></tr>' + rows + '</table>'
            + last_text
            + '<form method="post" action="/personnel/force-needs/review">'
            f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
            f'<input type="hidden" name="expected_fingerprint" value="{esc(needs.fingerprint)}">'
            f'<input type="hidden" name="request_id" value="{_secrets.token_urlsafe(18)}">'
            '<label class="lz-label">本次人工复核意见'
            '<textarea class="lz-textarea" name="note" required></textarea></label>'
            '<button type="submit" class="lz-btn">记录需求复核（不执行调配）</button>'
            '</form></div>'
        )
    current_domain = None
    for slot in POSITION_SLOTS:
        if not deputy and slot.pos_code != visible_code:
            continue
        if slot.domain != current_domain:
            current_domain = slot.domain
            sections.append(
                f'<div class="lz-card"><h2>{esc(group_of_slot(slot.pos_code))}</h2></div>'
            )
        members = sorted(
            (p for p in people.values() if p.pos_code == slot.pos_code),
            key=lambda p: (p.name, p.phone),
        )
        arrived = sum(p.arrived for p in members)
        deployable = sum(p.arrived and p.phone in readiness
                         and readiness[p.phone].status == "standby" for p in members)
        pending = sum(p.arrived and p.phone not in readiness for p in members)
        lines = ''.join(
            f'<li>{esc(p.name)} · {esc(p.unit)}（{esc(mask_phone(p.phone))}）'
            f' · {esc(_group_origin_label(state, p))} · {esc(p.presence_basis)}'
            f' · {esc(_readiness.STATUSES[readiness[p.phone].status] if p.phone in readiness else "可派状态待核定")}'
            f'{" · 报告依据：" + esc(readiness[p.phone].basis) if p.phone in readiness else ""}</li>'
            for p in members
        )
        sections.append(
            f'<div class="lz-card"><h3>{esc(slot.name)}</h3>'
            f'<p>本组 {len(members)} 人 · 已申报到场 {arrived} 人 · '
            f'队长／组长报告待命可派 {deployable} 人 · 到场但可派待核定 {pending} 人</p>'
            + ('<ul>' + lines + '</ul>' if lines else '<p class="lz-dim">暂无人员。</p>')
            + '</div>'
        )
    unplaced = sorted((p for p in people.values() if not p.pos_code),
                      key=lambda p: (p.name, p.phone))
    if deputy:
        rows = unplaced
    else:
        rows = [mine] if mine is not None and not mine.pos_code else []
    if rows:
        lines = ''.join(
            f'<li>{esc(p.name)}（{esc(mask_phone(p.phone))}） · {esc(p.presence_basis)}</li>'
            for p in rows
        )
        sections.append(
            '<div class="lz-card"><h2>组别待核定</h2>'
            '<p class="lz-note">不能仅凭队伍名称推断组织槽位，需指定副总指挥按人确认。</p>'
            '<ul>' + lines + '</ul></div>'
        )
    return _light_wrap(
        title="本事件组织与到场", body=''.join(sections),
        nav='<a href="/temporary/assign">人员编组</a>' if deputy else '<a href="/temporary">临时人员</a>',
        user=f'<span>{esc(viewer_phone[:3])}****{esc(viewer_phone[-4:])}</span>'
             + _topbar_logout_form("/logout/member", "登出"),
    )


def render_materials_page(
    state: PanelState,
    *,
    viewer_role: str = "commander",
    viewer_phone: str = "",
    viewer_event_id: str = "",
    query: dict[str, list[str]] | None = None,
) -> str:
    """人员与物资页（doc 04）：⓪长期库与编制概览 ①长期底账维护
    ②本次到场快照与八态 ③演练参考缺口与正式缺口依据核对。

    维护表单仅指挥员可见（按当前观看者身份判定，非全局会话状态）；成员只读。
    演练测算以固定火线和花名册折算分队；正式依据核对只读、指挥员可见。
    成员的事件数据只在本人绑定当前事件时展示。
    """
    # P17-P4：读写权限跟随当前观看者——此前用「面板存在任一指挥员会话」判定，
    # 成员在指挥员登录期间打开本页会看到维护表单（显示层越权，POST 本就拒绝）。
    command_mode = viewer_role == "commander"
    parts = []

    # ---- ⓪ 长期人员库与编制概览（P17-P4 收口：入口统一在本页） --------------
    library = state.registry.library
    unit_counter: dict[str, int] = {}
    for person in library.values():
        if person.unit:
            unit_counter[person.unit] = unit_counter.get(person.unit, 0) + 1
    units_word = "、".join(
        f"{esc(u)}×{c}" for u, c in sorted(unit_counter.items(), key=lambda x: -x[1])[:5]
    )
    slot_total = sum(s.max_headcount for s in POSITION_SLOTS)
    parts.append(
        '<div class="lz-card"><h2>长期人员库与编制</h2>'
        f'<p>在库人员 <b>{len(library)}</b> 人 · 涉及单位 <b>{len(unit_counter)}</b> 个'
        + (f'（{units_word}）' if units_word else "")
        + '</p>'
        f'<p class="lz-note">编制岗位：{len(POSITION_SLOTS)} 个一线组 · 编制上限合计 {slot_total} 人'
        '（组织架构见「人员架构」页自绘图）。名册导入与逐人指派在「火情事件 → 名册配置」执行；'
        '批量导入见下方「文件导入」。</p></div>'
    )

    # ---- ① 长期资源维护 -----------------------------------------------------
    item_rows = "".join(
        f'<tr><td class="lz-mono">{esc(it.item_id)}</td><td>{esc(it.name)}</td>'
        f'<td>{esc(it.category or "—")}</td><td>{esc(it.spec or "—")}</td>'
        f'<td>{esc(it.unit or "—")}</td><td class="num">{it.default_carry_qty:g}</td></tr>'
        for it in sorted(state.ledger.items.values(), key=lambda x: x.item_id)
    )
    items_card = (
        '<div class="lz-card"><h2>物资档案</h2><table class="lz-table">'
        '<tr><th>编号</th><th>名称</th><th>类别</th><th>规格</th><th>单位</th><th>默认携带</th></tr>'
        + (item_rows or '<tr><td colspan="6" class="lz-dim">暂无物资档案。</td></tr>')
        + "</table>"
    )
    if command_mode:
        items_card += (
            '<form method="post" action="/materials/item" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">'
            '<input class="lz-input" name="name" placeholder="名称*" required style="max-width:160px">'
            '<input class="lz-input" name="category" placeholder="类别" style="max-width:110px">'
            '<input class="lz-input" name="spec" placeholder="规格型号" style="max-width:120px">'
            '<input class="lz-input" name="unit" placeholder="单位" style="max-width:80px">'
            '<input class="lz-input" name="default_carry_qty" placeholder="默认携带数" style="max-width:110px">'
            '<button type="submit" class="lz-btn">新增物资</button></form>'
        )
    items_card += "</div>"
    parts.append(items_card)

    squad_bits = []
    for sq in sorted(state.ledger.squads.values(), key=lambda s: s.squad_id):
        lines = state.ledger.carry_list(sq.squad_id)
        line_text = ", ".join(
            f"{esc(state.ledger.items[l.item_id].name if l.item_id in state.ledger.items else l.item_id)}×{l.quantity:g}"
            for l in lines
        ) or '<span class="lz-dim">未配置携带清单</span>'
        squad_bits.append(
            f'<tr><td class="lz-mono">{esc(sq.squad_id)}</td><td>{esc(sq.name)}</td>'
            f'<td class="num">{sq.headcount_planned}</td><td class="num">{len(sq.member_phones)}</td>'
            f'<td>{esc(state.registry.library[sq.leader_phone].name) if sq.leader_phone in state.registry.library else "未配置"}</td>'
            f'<td>{line_text}</td></tr>'
        )
    squads_card = (
        '<div class="lz-card"><h2>中队与随队携带清单</h2><table class="lz-table">'
        '<tr><th>编号</th><th>名称</th><th>编制数</th><th>成员</th><th>队长</th><th>固定携带清单</th></tr>'
        + ("".join(squad_bits) or '<tr><td colspan="6" class="lz-dim">暂无中队。</td></tr>')
        + "</table>"
    )
    if command_mode:
        item_options = "".join(
            f'<option value="{esc(it.item_id)}">{esc(it.name)}</option>'
            for it in sorted(state.ledger.items.values(), key=lambda x: x.item_id)
        )
        squad_options = "".join(
            f'<option value="{esc(sq.squad_id)}">{esc(sq.name)}</option>'
            for sq in sorted(state.ledger.squads.values(), key=lambda s: s.squad_id)
        )
        if squad_options:
            squads_card += (
                '<form method="post" action="/materials/carry" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">'
                f'<select class="lz-select" name="squad_id" style="max-width:170px">{squad_options}</select>'
                f'<select class="lz-select" name="item_id" style="max-width:170px">{item_options}</select>'
                '<input class="lz-input" name="quantity" placeholder="数量（空=档案默认）" style="max-width:140px">'
                '<button type="submit" class="lz-btn">设置携带行</button></form>'
            )
            squads_card += (
                '<form method="post" action="/materials/squad-leader" '
                'style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
                f'<select class="lz-select" name="squad_id" style="max-width:170px">{squad_options}</select>'
                '<input class="lz-input" name="leader_phone" '
                'placeholder="该队在库队长手机号" style="max-width:190px" required>'
                '<button type="submit" class="lz-btn lz-btn2">设置队长</button>'
                '<span class="lz-note">须为该队成员，人员档案已标记队长。</span></form>'
            )
        squads_card += (
            '<form method="post" action="/materials/squad" style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">'
            '<input class="lz-input" name="name" placeholder="中队名称*" required style="max-width:170px">'
            '<input class="lz-input" name="member_phones" placeholder="成员手机号，逗号分隔" style="max-width:260px">'
            '<input class="lz-input" name="leader_phone" placeholder="队长手机号（选填）" style="max-width:190px">'
            '<input class="lz-input" name="headcount_planned" placeholder="编制数" style="max-width:90px">'
            '<button type="submit" class="lz-btn">新建中队</button>'
            '<span class="lz-note">成员须已在长期人员库（火情事件页导入）。</span></form>'
        )
    squads_card += "</div>"
    parts.append(squads_card)

    point_bits = []
    for pt in sorted(state.ledger.points.values(), key=lambda p: p.point_id):
        supply_texts = []
        for iid, value in sorted(pt.supplies.items()):
            name = state.ledger.items[iid].name if iid in state.ledger.items else iid
            word = (
                "库存未知 · 等待物资系统接口"
                if value is None
                else f"{value:g}"
            )
            supply_texts.append(f"{esc(name)}：{word}")
        point_bits.append(
            f'<tr><td>{esc(pt.name)}</td><td>{esc(pt.location or "—")}</td>'
            f'<td>{esc(pt.contact_name or "—")} {esc(pt.contact_phone or "")}</td>'
            f'<td>{"；".join(supply_texts) or "<span class=lz-dim>—</span>"}</td>'
            f'<td>{esc(pt.source_word())}</td></tr>'
        )
    points_card = (
        '<div class="lz-card"><h2>物资点</h2><table class="lz-table">'
        '<tr><th>名称</th><th>位置</th><th>联系人</th><th>可供应/库存</th><th>来源</th></tr>'
        + ("".join(point_bits) or '<tr><td colspan="5" class="lz-dim">暂无物资点。</td></tr>')
        + "</table>"
    )
    if command_mode:
        points_card += (
            '<form method="post" action="/materials/point" style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap">'
            '<input class="lz-input" name="name" placeholder="物资点名称*" required style="max-width:170px">'
            '<input class="lz-input" name="location" placeholder="位置" style="max-width:150px">'
            '<input class="lz-input" name="contact_name" placeholder="联系人" style="max-width:110px">'
            f'<select class="lz-select" name="item_id" style="max-width:150px"><option value="">（不登记物资）</option>{item_options}</select>'
            '<input class="lz-input" name="stock" placeholder="库存（空=未知）" style="max-width:120px">'
            '<button type="submit" class="lz-btn">新增物资点</button></form>'
            '<p class="lz-note">库存留空即显式「未知 · 等待物资系统接口」，不用 0 冒充有货。</p>'
        )
    points_card += "</div>"
    parts.append(points_card)

    # ---- ② 本次火情到场情况 ---------------------------------------------------
    active_event = state.active_event_id or ""
    event_id = active_event if command_mode or viewer_event_id == active_event else ""
    snapshot = state.ledger.snapshots.get(event_id) if event_id else None
    if not event_id:
        parts.append(
            '<div class="lz-empty"><b>本次火情到场情况</b>尚未进入任何事件任务空间；'
            '请先在「火情事件」页进入事件。</div>'
        )
    elif snapshot is None or not snapshot.squads:
        parts.append(
            '<div class="lz-empty"><b>本次火情到场情况</b>尚无到场快照：'
            '成员刷码入岗且所属中队已建档后自动生成。</div>'
        )
    else:
        snap_rows = []
        from .material_inventory_reads import read_event
        from .material_loss_sources import loss_amounts
        try:
            material_space, _, _ = state._appointment_space(event_id)
            inventory_views = read_event(material_space, state.ledger, event_id)
        except (ValueError, PanelActionError, OSError):
            inventory_views = None
        current_items = set(inventory_views or {})
        def quantity_word(value):
            return '—' if value is None else format(value, 'g')
        for sid in sorted(snapshot.squads):
            sq_snap = snapshot.squads[sid]
            squad_name = (
                state.ledger.squads[sid].name if sid in state.ledger.squads else sid
            )
            for iid in sorted(set(sq_snap.lines) | current_items):
                line = sq_snap.lines.get(iid)
                name = (
                    state.ledger.items[iid].name if iid in state.ledger.items else iid
                )
                status = line.status if line else '调拨物资'
                reason = line.reason if line else '本队原随队清单无此项'
                registered = available = consumed = damaged = missing = None
                if inventory_views is not None and iid in inventory_views:
                    balance = next(b for b in inventory_views[iid].team_balances if b.squad_id == sid)
                    registered, available = balance.arrival_base, balance.available
                    consumed, damaged = loss_amounts(state.ledger, event_id, sid, iid)
                    missing = 0
                    reason = (reason + '；' if reason else '') + '当前可用含已实收、扣已交出和耗损；在途不计；已报告撤离不计'
                else:
                    reason = (reason + '；' if reason else '') + '物资流水来源待核对，当前数量暂不展示'
                snap_rows.append(
                    f'<tr><td>{esc(squad_name)}</td><td class="num">{len(sq_snap.arrived_phones)}</td>'
                    f'<td>{esc(name)}</td><td class="num">{quantity_word(registered)}</td>'
                    f'<td class="num">{quantity_word(available)}</td>'
                    f'<td class="num">{quantity_word(consumed)}</td><td class="num">{quantity_word(damaged)}</td>'
                    f'<td class="num">{quantity_word(missing)}</td>'
                    f'<td><span class="lz-chip lz-chip-reg">{esc(status)}</span></td>'
                    f'<td>{esc(reason or "—")}</td>'
                    + (
                        f'<td><form class="lz-inlineform" method="post" action="/materials/check">'
                        f'<input type="hidden" name="event_id" value="{esc(event_id)}">'
                        f'<input type="hidden" name="squad_id" value="{esc(sid)}">'
                        f'<input type="hidden" name="item_id" value="{esc(iid)}">'
                        f'<input class="lz-input" name="qty_usable" placeholder="可用数" style="width:90px"> '
                        '<button type="submit" class="lz-btn lz-btn2">核对</button></form></td>'
                        if command_mode and not sq_snap.arrival_id and line is not None
                        else '<td class="lz-dim">随队到场已计入，无需另行核对</td>'
                        if sq_snap.arrival_id else '<td></td>'
                    )
                    + "</tr>"
                )
        parts.append(
            '<div class="lz-card"><h2>本次火情到场情况</h2><div style="overflow-x:auto"><table class="lz-table">'
            '<tr><th>中队</th><th>到场人数</th><th>物资</th><th>登记</th><th>当前可用</th>'
            '<th>消耗</th><th>损坏</th><th>缺失</th><th>状态</th><th>差异原因</th>'
            + ("<th>历史快照核对</th>" if command_mode else "<th>计入依据</th>")
            + "</tr>"
            + ("".join(snap_rows) or '<tr><td colspan="11" class="lz-dim">清单为空。</td></tr>')
            + "</table></div></div>"
        )

    # ---- ②b 本次到场人员与八态（doc 04 §4.1；只读展示，判定动作在名册配置页） --
    if event_id:
        person_rows = _home.person_rows_for_group(
            state.registry, state.workspace, event_id
        )
        if person_rows:
            state_trs = []
            for row in person_rows:
                me = row["phone"] == viewer_phone
                phone_shown = _home.mask_for_viewer(
                    row["phone"], viewer_role=viewer_role,
                    viewer_phone=viewer_phone,
                )
                tone = _home._status_tone(row["status_word"])
                basis_attr = (
                    f' title="依据：{esc(row["state_basis"])}"'
                    if row.get("state_basis") else ""
                )
                me_style = ' style="background:#F0F7F4;font-weight:600"' if me else ""
                state_trs.append(
                    f"<tr{me_style}>"
                    f'<td>{esc(row["name"])}</td>'
                    f'<td>{esc(row["unit"] or "—")}</td>'
                    f'<td>{esc(row["group"] or "待指派")}—{esc(row["slot_name"] or "待指派")}</td>'
                    f'<td><span class="lz-chip"{basis_attr} '
                    f'style="color:{tone};border-color:{tone}">{esc(row["status_word"])}</span></td>'
                    f'<td class="lz-mono">{esc(phone_shown)}</td></tr>'
                )
            parts.append(
                '<div class="lz-card"><h2>本次到场人员（八态）</h2>'
                '<table class="lz-table"><tr><th>姓名</th><th>单位</th>'
                '<th>工作组—本次岗位</th><th>状态</th><th>联系电话</th></tr>'
                + "".join(state_trs)
                + "</table>"
                + (
                    '<p class="lz-note">状态依据悬停可见；判定/离场等指挥动作在'
                    '"「火情事件 → 名册配置」页执行。</p>'
                    if command_mode else ""
                )
                + "</div>"
            )
        else:
            parts.append(
                '<div class="lz-empty"><b>本次到场人员（八态）</b>'
                '本事件尚无名册与到场人员。</div>'
            )

    # ---- ③ 演练参考缺口（固定火线×到场人数；不代表正式供需预测）---------
    gap_html = ""
    if event_id and workspace_has_active(state):
        card = wb.staff_estimate(state.workspace)
        mats = card.get("materials")
        if mats is not None:
            demand_by_name = {
                entry["item"]: float(entry["qty"]) for entry in mats["equipment"]
            }
            for entry in mats["consumables"]:
                demand_by_name[entry["item"]] = (
                    demand_by_name.get(entry["item"], 0.0) + float(entry["qty"])
                )
            name_to_item = {it.name: it for it in state.ledger.items.values()}
            demand: dict[str, float] = {}
            unmatched: list[str] = []
            for name_, qty in demand_by_name.items():
                matched = name_to_item.get(name_)
                if matched is None:
                    unmatched.append(f"{name_}×{qty:g}")
                else:
                    demand[matched.item_id] = qty
            quantity_error = False
            try:
                rows = state.ledger.compute_gap(event_id, demand, workspace=state.workspace)
            except ValueError:
                rows = []
                quantity_error = True
            severity_chip = {
                _mat.GAP_BLOCKING: "lz-alert bad",
                _mat.GAP_SHORT: "lz-alert warn",
                _mat.GAP_INCOMING: "lz-alert warn",
                _mat.GAP_OK: "lz-alert ok",
                '待核对': 'lz-alert warn',
            }
            gap_rows = []
            for row in rows:
                points = state.ledger.nearest_points_for(row["item_id"])
                point_text = (
                    f'{esc(points[0].name)}（{esc(points[0].stock_word(row["item_id"]))}）'
                    if points else "<span class=lz-dim>无已登记开放物资点</span>"
                )
                gap_rows.append(
                    f'<div class="{severity_chip[row["severity"]]}"><b>{esc(row["name"])}</b> '
                    f'演练需求 {row["demand"]:g} / 登记到场 {row["on_site"]:g} / '
                    f'演练账面可用 {"待核对" if row["available"] is None else format(row["available"], "g")}'
                    f' → 演练参考缺口 {"待核对" if row["gap"] is None else format(row["gap"], "g")} 【{esc(row["severity"])}】 '
                    f'登记物资点（非距离排序）：{point_text}</div>'
                )
            unmatched_line = (
                f'<p class="lz-note">未建档需求（需先建物资档案）：{_home.esc("、".join(unmatched))}</p>'
                if unmatched
                else ""
            )
            gap_html = (
                '<div class="lz-card"><h2>缺口与补给建议</h2>'
                '<p class="lz-note">仅演练参考，固定水泵接力基线；'
                '下方演练测算未使用已确认任务、实际投入力量、预计作业时长和专业核定用量。'
                '当前不足与预计不足均待核实；物资点不按距离排序，库存以登记值为准。</p>'
                '<p class="lz-note">演练测算口径：火线 '
                f'{wb.DRILL_FIRELINE_M:g} 米 · 蔓延 {wb.DRILL_SPREAD_M_H:g} 米/小时 · '
                f'一线实到 {card["headcount"]} 人折算 {card["estimate"].crews} 个分队。</p>'
                + ('<p class="lz-note">物资流水来源待核对，演练数量暂不展示。</p>'
                   if quantity_error else
                   "".join(gap_rows) or '<p class="lz-note">暂无可比缺口。</p>')
                + unmatched_line
                + "</div>"
            )
        else:
            gap_html = (
                '<div class="lz-empty"><b>缺口与补给建议</b>'
                '一线无实到人员，无法折算分队与需求。</div>'
            )
    else:
        gap_html = (
            '<div class="lz-empty"><b>缺口与补给建议</b>需要事件任务空间在场。</div>'
        )
    parts.append(gap_html)

    if command_mode:
        parts.append(_material_gap_evidence_card(state, event_id, query))

    parts.append(_imports_card(state, command_mode))

    return "".join(parts)


def _material_gap_evidence_card(state: PanelState, event_id: str,
                                query: dict[str, list[str]] | None) -> str:
    """Commander-only read of one explicit task/item pair in the active event."""
    head = (
        '<div class="lz-card"><h2>正式缺口依据核对</h2>'
        '<p class="lz-note">逐项核查当前事件的任务与物资。选择组合不表示该任务需要该物资；'
        '账面自洽不等于现场独立核验，当前与预计不足均待核实。</p>'
    )
    if not event_id or state.workspace.event_id != event_id:
        return head + '<p class="lz-note">请先进入事件任务空间。</p></div>'

    orders = state.workspace.orders
    items = state.ledger.items
    if not orders or not items:
        return head + '<p class="lz-note">当前事件尚无可选择任务或物资档案。</p></div>'

    selected_order = ((query or {}).get("gap_order") or [""])[0]
    selected_item = ((query or {}).get("gap_item") or [""])[0]
    order_options = ''.join(
        f'<option value="{esc(order_id)}"'
        + (' selected' if order_id == selected_order else '')
        + f'>{esc(order_id)}</option>'
        for order_id in sorted(orders)
    )
    item_options = ''.join(
        f'<option value="{esc(item_id)}"'
        + (' selected' if item_id == selected_item else '')
        + f'>{esc(items[item_id].name)}（{esc(item_id)}）</option>'
        for item_id in sorted(items)
    )
    form = (
        '<form method="get" action="/materials" style="display:flex;gap:8px;flex-wrap:wrap">'
        f'<select class="lz-select" name="gap_order"><option value="">选择任务</option>{order_options}</select>'
        f'<select class="lz-select" name="gap_item"><option value="">选择物资</option>{item_options}</select>'
        '<button class="lz-btn" type="submit">核对依据</button></form>'
    )
    if selected_order not in orders or selected_item not in items:
        return head + form + '<p class="lz-note">请选择当前事件中的任务与物资。</p></div>'

    assessment = assess_gap_inputs(
        state.workspace, state.ledger, event_id=event_id,
        order_id=selected_order, item_id=selected_item,
    )
    task_word = assessment.task_ref or '当前版任务待核对'
    book_word = ('待核对' if assessment.book_available is None
                 else f'{assessment.book_available:g} {items[selected_item].unit}')
    arrival_word = '、'.join(assessment.arrival_refs) or '无可追溯到场流水'
    loss_word = '、'.join(assessment.loss_refs) or '无版本化耗损报告'
    missing_html = ''.join(f'<li>{esc(entry)}</li>' for entry in assessment.missing)
    inputs_html = ''
    if assessment.task_inputs is not None:
        values = assessment.task_inputs
        word = lambda key: '未知' if values[key] is None else format(values[key], 'g')
        inputs_html = (f'<p>当前任务输入报告：实际人数 {word("force_count")}；预计作业 {word("duration_hours")} 小时；'
            f'报告需求 {word("demand_quantity")}；报告预计消耗 {word("projected_consumption")}；'
            f'{"模拟用量" if values["usage_kind"] == "simulated" else "用量未核定"}，不作正式缺口结论。</p>'
            f'<p style="overflow-wrap:anywhere">关系来源 {esc(values["material_relation_source"])}；'
            f'力量来源 {esc(values["force_source"])}；时长来源 {esc(values["duration_source"])}；'
            f'用量来源 {esc(values["usage_source"])}；输入版本 {esc(assessment.input_ref)}。</p>')
    inventory_html = ''
    if assessment.inventory is not None:
        inv = assessment.inventory
        unit = esc(items[selected_item].unit)
        numbers = (('到场冻结基线', inv.arrival_base), ('队伍账面可用', inv.team_available),
                   ('调拨在途', inv.in_transit), ('现场耗损', inv.losses),
                   ('归还实收损坏', inv.return_damage), ('随队撤出', inv.carried_out),
                   ('留置另列', inv.retained), ('移交另列', inv.handed_over),
                   ('撤离后实收（已在撤出内）', inv.rear_received),
                   ('撤离后实收损坏（不计可用）', inv.rear_damaged),
                   ('撤离借用装备未收', inv.rear_unreceived))
        inventory_html = '<details class="material-inventory-projection" open><summary>事件物资流水数量核对</summary><ul>'
        inventory_html += ''.join(f'<li>{esc(label)}：{value:g} {unit}</li>' for label, value in numbers)
        inventory_html += '</ul><p class="lz-note">账面数量来自报告，并非独立核验；在途、损坏、撤出及后方实收不计现场可用。'
        inventory_html += '留置/移交按原报告位置和保管对象另列，不推断新的队伍库存归属。</p>'
        inventory_html += ''.join(f'<p style="overflow-wrap:anywhere">{esc("留置" if entry.kind == "retained" else "移交")} '
            f'{entry.quantity:g} {unit} · {esc(entry.location)} · 保管对象 {esc(entry.custodian)} · '
            f'来源 {esc(entry.withdrawal_id)}</p>' for entry in inv.exceptions)
        inventory_html += '<p style="overflow-wrap:anywhere">调拨：' + esc('、'.join(inv.transfer_refs) or '无')
        inventory_html += '；撤离：' + esc('、'.join(inv.withdrawal_refs) or '无')
        inventory_html += '；后方接收/核验/结清：' + esc('、'.join(inv.custody_refs) or '无') + '</p></details>'
    return (
        head + form
        + f'<p>事件 {esc(event_id)} · 任务 {esc(task_word)} · 物资 {esc(items[selected_item].name)}'
        f'：账面可用 {esc(book_word)}；当前不足 {esc(assessment.current_status)}；'
        f'预计不足 {esc(assessment.projected_status)}。</p>'
        + inputs_html
        + '<p class="lz-note">待补/待核对依据：</p><ul>' + missing_html + '</ul>'
        + inventory_html
        + '<details><summary>查看来源编号</summary>'
        f'<p style="overflow-wrap:anywhere">到场：{esc(arrival_word)}；耗损及更正：{esc(loss_word)}</p>'
        '</details>'
        + _material_supply_leads(state, selected_item, assessment.current_gap)
        + '</div>'
    )


def _material_supply_leads(state: PanelState, item_id: str,
                           verified_shortfall: float | None) -> str:
    """Show catalog leads, never a replenishment recommendation or receipt."""
    review = review_supply_leads(
        state.ledger, item_id=item_id, required_quantity=verified_shortfall,
    )
    point_rows = []
    for point in review.points:
        stock = '未知' if point.stock is None else f'{point.stock:g}'
        blockers = '；'.join(point.blockers)
        point_rows.append(
            f'<li><b>{esc(point.name)}</b>（{esc(point.point_id)}）：'
            f'登记库存 {esc(stock)}；来源 {esc(point.source)}；'
            f'登记更新时间 {esc(point.updated_at or "未知")}；'
            f'预计装运 {esc(point.eta_load or "未知")}。'
            f'待核对：{esc(blockers)}。</li>'
        )
    company_rows = []
    for company in review.companies:
        company_rows.append(
            f'<li><b>{esc(company.name)}</b>（{esc(company.company_id)}）：'
            f'登记服务 {esc(company.services or "未填")}；'
            f'来源 {esc(company.source)}；'
            f'来源更新时间 {esc(company.source_updated_at or "未知")}。'
            f'待核对：{esc("；".join(company.blockers))}。</li>'
        )
    return (
        '<h3>供给候选依据（只读）</h3>'
        '<p class="lz-note">库内物资点优先核对；登记库存与预计装运均不证明能及时送达。'
        '外部公司仅为名录线索，不表示该物资有货。当前可补给性待核实，'
        '不生成补给任务，也不计入在途或现场可用。</p>'
        '<p>登记物资点线索：</p><ul>'
        + (''.join(point_rows) or '<li>无登记该物资的物资点。</li>')
        + '</ul><p>外部公司联系核实线索（未按物资匹配）：</p><ul>'
        + (''.join(company_rows) or '<li>无外部公司清单。</li>')
        + '</ul>'
    )


_IMPORT_STATUS_NOTE = {
    "预览": "解析与字段映射完成，等待确认生成草稿",
    "已生成草稿": "查重/查错通过，等待授权发布",
    "已发布": "已写入长期库（可回滚一次）",
    "失败": "存在错误清单，请修正后重新导入",
}


def _imports_card(state, command_mode: bool) -> str:
    """文件导入区（doc 04 §8）：上传→预览→草稿→发布→回滚；留痕完整。"""
    type_options = "".join(
        f'<option value="{key}">{esc(spec.label)}</option>'
        for key, spec in DATA_TYPES.items()
    )
    head = (
        '<div class="lz-card"><h2>文件导入</h2>'
        '<p class="lz-note">支持 TXT / CSV 粘贴导入；Word/Excel/PDF/图片表格需外部解析引擎，待接入。'
        '流程：上传解析→字段自动映射→预览→查重查错→生成草稿→授权发布入长期库，保留原始内容哈希与逐行错误清单。</p>'
    )
    if command_mode:
        head += (
            '<form method="post" action="/imports/upload">'
            f'<div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">'
            f'<select class="lz-select" name="data_type">{type_options}</select>'
            '<input class="lz-input" name="filename" placeholder="文件名（如 roster.csv）" style="max-width:220px">'
            '</div>'
            '<textarea class="lz-input" name="content" rows="5" placeholder="粘贴 CSV/TXT：首行列名，逗号分隔" '
            'style="width:100%;margin-top:6px;font-family:var(--lz-mono,monospace)"></textarea>'
            '<button type="submit" class="lz-btn" style="margin-top:6px">上传并解析</button></form>'
        )
    bits = []
    for b in reversed(state.imports.batches):
        err_html = (
            '<details><summary class="lz-dim">错误清单（' + str(len(b.errors)) + '条）</summary>'
            '<ul class="prewrap">' + "".join(f"<li>{esc(e)}</li>" for e in b.errors[:20]) + "</ul></details>"
            if b.errors else ""
        )
        map_text = " · ".join(f"{k}←{v}" for k, v in b.mapping.items()) or "—"
        preview = ""
        if b.rows and command_mode:
            cols = list(b.rows[0].keys())
            preview = (
                '<details><summary class="lz-dim">预览映射结果（' + str(len(b.rows)) + '行）</summary>'
                '<table class="lz-table"><tr>' + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>"
                + "".join("<tr>" + "".join(f"<td>{esc(r.get(c, ''))}</td>" for c in cols) + "</tr>"
                          for r in b.rows[:10])
                + "</table></details>"
            )
        acts = []
        if command_mode:
            if b.status == "预览":
                acts.append(f'<form method="post" action="/imports/draft" style="display:inline">'
                            f'<input type="hidden" name="batch_id" value="{esc(b.batch_id)}">'
                            '<button type="submit" class="lz-btn">生成草稿</button></form>')
            if b.status == "已生成草稿":
                acts.append(f'<form method="post" action="/imports/publish" style="display:inline">'
                            f'<input type="hidden" name="batch_id" value="{esc(b.batch_id)}">'
                            '<button type="submit" class="lz-btn lz-btn-primary">授权发布</button></form>')
            if b.status == "已发布" and not b.rolled_back:
                acts.append(f'<form method="post" action="/imports/rollback" style="display:inline" '
                            f'onsubmit="return confirm(\'\u786e\u5b9a\u56de\u6eda\uff1f\u53d7\u5f71\u54cd\u96c6\u5408\u5c06\u6062\u590d\u53d1\u5e03\u524d\u72b6\u6001\u3002\')">' 
                            f'<input type="hidden" name="batch_id" value="{esc(b.batch_id)}">'
                            '<button type="submit" class="lz-btn">回滚</button></form>')
        ver = (f"v{b.version} · 发布 {esc(b.published_at)}" if b.published_at else "未发布")
        dig = f" · 摘要 {esc(b.post_digest)}" if b.post_digest else ""
        bits.append(
            f'<div class="lz-card" style="margin-top:8px;padding:8px 10px">'
            f'<div><b class="lz-mono">{esc(b.batch_id)}</b> · {esc(DATA_TYPES[b.data_type].label if b.data_type in DATA_TYPES else b.data_type)}'
            f' · 状态：<b>{esc(b.status)}</b> · {esc(_IMPORT_STATUS_NOTE.get(b.status, ""))}</div>'
            f'<div class="lz-dim" style="font-size:12px;margin-top:2px">文件 {esc(b.filename)} · '
            f'SHA-256 {esc(b.content_sha256[:12])}… · 上传 {esc(b.uploaded_by)} {esc(b.uploaded_at)}</div>'
            f'<div class="lz-dim" style="font-size:12px">映射：{esc(map_text)} · 版本：{ver}{dig}</div>'
            + err_html + preview
            + ("<div style='margin-top:6px;display:flex;gap:8px'>" + "".join(acts) + "</div>" if acts else "")
            + "</div>"
        )
    body = "".join(bits) or '<p class="lz-note">暂无导入批次。</p>'
    return head + body + "</div>"


def workspace_has_active(state: PanelState) -> bool:
    """当前事件空间是否有任一在岗绑定（缺口计算的分队折算输入）。"""
    return any(b.status == "active" for b in state.workspace.roster_bindings.values())


def render_close_confirm_page(state: PanelState, event_id: str) -> str | None:
    record = state.registry.events.get(event_id)
    if record is None:
        return None
    body = (
        '<div class="lz-card"><h2>关闭事件 · 二次确认</h2>'
        f'<p>确认关闭事件 <b class="lz-mono">{esc(record.event_id)}</b>（{esc(record.name)}）？</p>'
        '<p class="lz-note">关闭后归档只读：不可签发二维码、不可刷码入岗，历史全部保留。</p>'
        '<span class="lz-actions">'
        + _hidden_post(
            "/events/action", {"event_id": event_id, "action": "close"}, "确认关闭", "lz-btn-danger"
        )
        + '<a class="lz-btn lz-btn2" href="/events">返回列表</a></span></div>'
    )
    return _light_wrap(
        title="关闭确认", body=body,
        nav=_light_nav(_COMMANDER_LIGHT_NAV, "/events"),
        user='<span>指挥员 · 指挥模式</span>' + _topbar_logout_form("/logout", "登出"),
    )


_SLOT_OPTIONS = "".join(
    f'<option value="{esc(slot.pos_code)}">{esc(slot.name)}（{esc(slot.pos_code)}）</option>'
    for slot in POSITION_SLOTS
)


def render_roster_page(state: PanelState, event_id: str) -> str | None:
    record = state.registry.events.get(event_id)
    if record is None:
        return None
    roster = state.registry.rosters.get(event_id)
    entries = sorted(roster.entries.values(), key=lambda e: e.phone) if roster else []
    in_roster = {entry.phone for entry in entries}
    library = sorted(state.registry.library.items())

    head = (
        f'<div class="lz-card"><h2>本事件名册 · {esc(record.name)}</h2>'
        f'<p style="margin:0 0 6px">{_event_status_chip(record)} '
        f'<span class="lz-dim lz-mono">{esc(record.event_id)} · 名册版本 v{(roster.roster_version if roster else 1)}</span></p>'
        '<p class="lz-note" style="margin-top:4px">名册是提前维护的授权清单（区别于花名册）；'
        '未配置名册的进行中事件不允许刷码入岗。'
        f'<a class="lz-link" href="/events">← 返回火情事件列表</a></p></div>'
    )

    entry_bits = []
    for entry in entries:
        person = state.registry.library.get(entry.phone)
        if entry.pos_code:
            position = f"{SLOT_INDEX[entry.pos_code].name}（{esc(entry.pos_code)}）"
            source_word = "本职解析" if entry.resolved_from_home else "人工指派"
        else:
            position = '<span class="lz-unresolved">岗位未解析·待指派</span>'
            source_word = "待指派"
        assign_form = (
            '<form class="lz-inlineform" method="post" '
            f'action="/events/{esc(event_id)}/roster/assign">'
            f'<input type="hidden" name="phone" value="{esc(entry.phone)}">'
            f'<select class="lz-select" name="pos_code" style="width:auto">{_SLOT_OPTIONS}</select> '
            '<button type="submit" class="lz-btn lz-btn2">指派</button></form>'
        )
        entry_bits.append(
            "<tr>"
            f'<td>{esc(person.name if person else "—")}</td>'
            f'<td class="lz-mono">{esc(entry.phone)}</td>'
            f'<td>{position}</td>'
            f'<td>{esc(source_word)}</td>'
            f'<td class="lz-dim">{esc(entry.added_by or "—")} · {esc(entry.added_at or "—")}</td>'
            f'<td>{assign_form}</td>'
            "</tr>"
        )
    if entry_bits:
        roster_card = (
            '<div class="lz-card"><h2>名册条目</h2><table class="lz-table">'
            "<tr><th>姓名</th><th>手机号</th><th>解析岗位</th><th>来源</th><th>加入</th><th>指派岗位</th></tr>"
            + "".join(entry_bits)
            + "</table></div>"
        )
    else:
        roster_card = (
            '<div class="lz-empty"><b>暂无名册条目</b>从下方长期人员库勾选加入，或使用「全部加入」。</div>'
        )

    lib_bits = []
    for phone, person in library:
        if phone in in_roster:
            continue
        suggested = resolve_home_slot(person.home_position)
        suggest_text = (
            f"建议：{esc(suggested)}" if suggested else "本职无法自动解析 · 需人工指派"
        )
        lib_bits.append(
            "<tr>"
            f'<td><input type="checkbox" name="phones" value="{esc(phone)}"></td>'
            f'<td>{esc(person.name)}</td>'
            f'<td class="lz-mono">{esc(phone)}</td>'
            f'<td>{esc(person.unit or "—")}</td>'
            f'<td>{esc(person.home_position or "—")}</td>'
            f'<td class="lz-dim">{suggest_text}</td>'
            "</tr>"
        )
    if lib_bits:
        add_card = (
            '<div class="lz-card"><h2>从长期人员库加入</h2>'
            '<form method="post" action="' + f'/events/{esc(event_id)}/roster/add' + '">'
            '<table class="lz-table"><tr><th></th><th>姓名</th><th>手机号</th><th>单位</th><th>本职</th><th>解析建议</th></tr>'
            + "".join(lib_bits)
            + '</table><p style="margin-top:10px;display:flex;gap:8px">'
            '<button type="submit" class="lz-btn">加入勾选人员</button></p></form>'
            + _hidden_post(f"/events/{esc(event_id)}/roster/all-add", {}, "全部加入")
            + "</div>"
        )
    elif library:
        add_card = (
            '<div class="lz-empty"><b>长期人员库成员均已在名册</b>如需调整请先在本页指派或补充人员库。</div>'
        )
    else:
        add_card = (
            '<div class="lz-empty"><b>长期人员库为空</b>请先用下方导入表单补录人员档案，再配置名册。</div>'
        )

    import_card = (
        '<div class="lz-card"><h2>导入长期人员库</h2>'
        '<form method="post" action="' + f"/events/{esc(event_id)}/library-import" + '">'
        '<label class="lz-label">CSV 行（姓名,手机号,单位,本职岗位,是否队长）'
        '<textarea class="lz-textarea" name="roster_csv" placeholder="张三,13912340001,县防火办,扑救一组（一分区）,是"></textarea></label>'
        '<button type="submit" class="lz-btn">导入人员库</button>'
        '<p class="lz-note">长期人员库是跨事件底账（注册表层），与事件空间内的花名册副本分离。</p>'
        "</form></div>"
    )
    state_rows = derive_personnel_states(
        state.workspace, (roster.entries.values() if roster else ()))
    active_orders = [
        (o.id, o.id)
        for o in sorted(state.workspace.orders.values(), key=lambda x: x.id)
        if o.status.value in {"confirmed", "active", "paused"}
    ]
    ps_bits = []
    for row in state_rows:
        color = STATE_COLORS.get(row.state, "#647382")
        acts = []
        if True:  # 名册配置页仅指挥员可达（与上方指派表单同权限口径）
            act_form = lambda a, label, extra="": (   # noqa: E731
                '<form class="lz-inlineform" method="post" '
                f'action="/events/{esc(event_id)}/person/state">'
                f'<input type="hidden" name="phone" value="{esc(row.phone)}">'
                f'<input type="hidden" name="action" value="{a}">{extra}'
                f'<button type="submit" class="lz-btn lz-btn2">{label}</button></form>'
            )
            order_select = (
                '<select class="lz-select" name="order_id" style="width:auto">'
                + "".join(f'<option value="{esc(oid)}">{esc(oid)}</option>' for oid, _ in active_orders)
                + "</select>"
            ) if active_orders else (
                '<span class="lz-dim">无可用命令</span>'
            )
            if row.state == "已到场":
                acts.append(act_form("standby", "暂离/轮休"))
                acts.append(
                    '<form class="lz-inlineform" method="post" '
                    f'action="/events/{esc(event_id)}/person/state">'
                    f'<input type="hidden" name="phone" value="{esc(row.phone)}">'
                    '<input type="hidden" name="action" value="executing">'
                    + order_select
                    + '<button type="submit" class="lz-btn lz-btn2">标记执行中</button></form>'
                )
                acts.append(act_form("lost_contact", "标记失联"))
            elif row.state == "执行中":
                acts.append(act_form("resume_duty", "恢复在场"))
                acts.append(act_form("standby", "暂离/轮休"))
            elif row.state == "暂离/轮休":
                acts.append(act_form("resume_duty", "恢复在场"))
            elif row.state == "失联":
                acts.append(act_form("contact_restored", "恢复联络"))
            elif row.state == "已撤离":
                acts.append(act_form("returned_to_unit", "标记已归建"))
        chip = (f'<span style="color:{color};font-weight:bold">●</span> '
                f'<span style="color:{color}">{esc(row.state)}</span>')
        ps_bits.append(
            "<tr>"
            f"<td>{esc(row.name)}</td>"
            f'<td class="lz-mono">{esc(mask_phone(row.phone))}</td>'
            f"<td>{esc(row.slot_name or '—')}</td>"
            f"<td>{chip}</td>"
            f'<td class="lz-dim">{esc(row.basis)}</td>'
            + (f"<td>{''.join(acts)}</td>" if acts else "")
            + "</tr>"
        )
    ps_card = (
        '<div class="lz-card"><h2>到场人员状态（规范 04·§4.1 八态）</h2>'
        '<p class="lz-note">预编入/待到场/已到场/已撤离由名册与签到事实推导；'
        '执行中须挂真实在效命令，暂离/轮休、失联、归建为指挥员人工判定，全部入哈希链留痕。</p>'
        + ('<table class="lz-table"><tr><th>姓名</th><th>手机号</th><th>岗位</th>'
           '<th>状态</th><th>依据</th><th>操作</th></tr>' + "".join(ps_bits) + "</table>"
           if ps_bits else '<p class="lz-note">名册与签到均为空。</p>')
        + "</div>"
    )

    body = head + roster_card + ps_card + add_card + import_card
    # This legacy view retains readable tables. All writes use the deputy's
    # separately authenticated, versioned and CSRF-protected management page.
    import re
    body = re.sub(r'<(input|select|textarea|button)\b', r'<\1 disabled', body)
    body = '<p><a class="lz-btn" href="/personnel/manage">副指挥长具名人员管理 →</a></p><p>本页为名册只读视图；人员安排和状态报告请由本事件指定副指挥长具名办理。</p>' + body
    body = body.replace('归建为指挥员人工判定', '归建为副指挥长具名报告')
    return _light_wrap(
        title="名册配置", body=body,
        nav=_light_nav(_COMMANDER_LIGHT_NAV, "/events"),
        user='<span>指挥员 · 指挥模式</span>' + _topbar_logout_form("/logout", "登出"),
    )


def render_member_home(state: PanelState, session: MemberSession) -> str:
    status_chip = (
        '<span class="lz-chip lz-chip-active">已入岗</span>'
        if session.onboarded
        else '<span class="lz-chip lz-chip-upgraded">待到场</span>'
    )
    bound = (
        esc(_event_label(state.registry, session.bound_event_id))
        if session.onboarded
        else "—"
    )
    next_step = (
        '<p><a class="lz-btn" href="/login">去登录页刷码入岗</a></p>'
        if not session.onboarded
        else ""
    )
    body = (
        '<div class="lz-card"><h2>人员架构</h2>'
        f'<p style="margin:0 0 8px">{status_chip}</p>'
        f'<div><span class="lz-kv">姓名</span>{esc(session.name)}</div>'
        f'<div><span class="lz-kv">账号</span><span class="lz-mono">{esc(mask_phone(session.phone))}</span></div>'
        f'<div><span class="lz-kv">绑定事件</span>{bound}</div>'
        f'<div><span class="lz-kv">到场时间</span>{esc(session.arrived_at or "—")}</div>'
        f"{next_step}"
        '<p><a class="lz-btn" href="/tasks/offline">查看线下任务与回执</a></p>'
        '<p class="lz-note">其他成员业务视图随权限体系后续版本开放。'
        '外部系统接入端点：待对接。</p></div>'
    )
    return _light_wrap(
        title="人员架构", body=body,
        nav=_light_nav(_MEMBER_LIGHT_NAV, "/"),
        user=(
            f'<span>{esc(session.name)}（{esc(mask_phone(session.phone))}）</span>'
            + _topbar_logout_form("/logout/member", "退出登录")
        ),
    )


_NAV_SEQUENCE = ("/map", "/", "/org", "/questions", "/orders", "/timeline", "/reviews")


def _build_nav(active: str, *, command_mode: bool = False, projector: bool = False) -> str:
    """Spec'd P15 order; the drill console shows only with command authority.

    The projector flag rides along so wall mode survives navigation.
    """
    titles = dict(ROUTES)
    entries = [(route, titles[route]) for route in _NAV_SEQUENCE]
    if command_mode:
        entries.append(("/events", "火情事件"))
        entries.append(("/drill", "演练控制台"))
    entries.append(("/export/report", "导出"))
    suffix = "?projector=1" if projector else ""
    items = []
    for route, label in entries:
        cls = ' class="on"' if route == active else ""
        items.append(f'<a href="{route}{suffix}"{cls}><span>{esc(label)}</span></a>')
    return "".join(items)


def _header_strip(state: PanelState, session_token: str | None) -> str:
    broken = state.chain_broken()
    chain = '<span class="dot ok"></span>审计链完整' if not broken else (
        f'<span class="dot bad"></span>审计链断于 {esc(",".join(map(str, broken)))}'
    )
    space = f"<span>空间 <b>{esc(state.workspace.scenario_id)}</b></span>"
    if state.command_mode(session_token):
        auth_zone = (
            '<span class="chip chip-ok">指挥模式</span>'
            "<span>指挥员入口已认证</span>"
            '<form class="inline" method="post" action="/logout">'
            '<button type="submit">登出</button></form>'
        )
    elif state.entry_unlocked:
        auth_zone = "<span>指挥员入口已认证（本会话未持令牌）</span>"
    else:
        auth_zone = (
            '<span class="chip chip-warn">锁定</span>'
            "<span>指挥员入口锁定 —— 只读视图</span>"
            '<a href="/login">入口认证</a>'
        )
    return f"<span>{chain}</span>{space}{auth_zone}"


# ---------------------------------------------------------------------------
# Views


def _view_overview(state: PanelState, session_token: str | None = None) -> str:
    ws = state.workspace
    parts = ['<section><h2>共享态势 · 事实账</h2>', wb.plain_note("事实账")]
    if ws.claims:
        parts.append("<table><tr><th>主体</th><th>取值</th><th>状态</th><th class=\"num\">置信度</th></tr>")
        for subject in sorted(ws.claims):
            claim = ws.claims[subject]
            parts.append(
                f"<tr><td class='mono'>{esc(subject)}</td>"
                f"<td class='mono'>{esc(claim.value)}</td>"
                f"<td>{_status_chip(claim.status.value)}</td>"
                f"<td class='num'>{claim.confidence:.2f}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append(
            '<div class="empty"><b>尚无事实记录</b>接入观测流或装载快照后，此处显示领域条件账。</div>'
        )
    score_line = default_scorer().score(ws.claims).render()
    parts.append("<section><h2>态势评分</h2>")
    parts.append(wb.plain_note("态势评分"))
    parts.append(f'<div class="score">{esc(score_line)}</div>')
    parts.append("</section>")

    violations = check_campaign_invariants(ws)
    if not violations:
        parts.append(
            '<section><h2>集成不变量</h2>'
            '<span class="chip chip-ok">全部通过</span>'
            '<span class="dim"> 审计链 / 传感器不验证 / L3先批后活 / 记忆纯度</span></section>'
        )
    else:
        rows = "".join(
            f"<div class='row'><span class='chip chip-bad'>{esc(v.kind)}</span>"
            f"<span>{esc(v.detail)}</span></div>"
            for v in violations[:8]
        )
        parts.append(
            f'<section><h2>集成不变量 · {len(violations)} 项违规</h2>{rows}</section>'
        )
    parts.append(
        '<section><h2>战后档案</h2>'
        '<a class="btn" href="/export/report">导出战后报告（Markdown）</a>'
        '<span class="dim"> 只读导出，与 render_after_action_report 逐字一致。</span>'
        "</section>"
    )
    parts.append("</section>")
    return "".join(parts)


def _view_brief(state: PanelState, session_token: str | None = None) -> str:
    ws = state.workspace
    if not ws.decision_briefs:
        return '<div class="empty"><b>尚无决策简报</b>参谋部完成一轮编排后，最新简报在此呈现。</div>'
    brief = max(ws.decision_briefs.values(), key=lambda b: b.source_version)
    parts = ["<section><h2>问题与推荐</h2>",
             wb.plain_note("决断简报", "目标框"),
             f'<div class="row"><span class="kv">议题</span><span>{esc(brief.question)}</span></div>',
             f'<div class="row"><span class="kv">参谋推荐</span><span>{esc(brief.recommendation)}</span></div>']
    if brief.goal_frame_id:
        frame = ws.goal_frames.get(brief.goal_frame_id)
        statement = esc(frame.statement) if frame is not None else ""
        if frame is not None and frame.confirmed_by:
            goal_chip = "<span class='chip chip-ok'>已确认</span>"
            confirm_ui = ""
        elif frame is not None:
            goal_chip = "<span class='chip chip-warn'>待指挥员确认</span>"
            confirm_ui = (
                "<form class='inline' method='post' action='/confirm-goal'>"
                f"<input type='hidden' name='goal_id' value='{esc(frame.id)}'>"
                "<button type='submit'>确认目标框</button></form>"
            ) if state.command_mode(session_token) else ""
        else:
            goal_chip = "<span class='chip chip-dim'>目标框缺失</span>"
            confirm_ui = ""
        parts.append(
            f'<div class="row"><span class="kv">目标框</span>'
            f"<span class='mono id'>{esc(brief.goal_frame_id)}</span>"
            f"<span>{statement}</span>{goal_chip}{confirm_ui}</div>"
        )
    parts.append("</section>")

    parts.append(wb.plain_note("对比矩阵"))
    parts.append("<section><h2>方案对比矩阵</h2>")
    if brief.option_matrix:
        parts.append(
            "<table><tr><th>方案</th><th>前提已满足</th><th>前提未满足</th>"
            "<th class='num'>成本</th><th>可逆性</th><th>可行性</th><th>阻塞项</th></tr>"
        )
        for row in brief.option_matrix:
            blocking = ", ".join(row.blocking_conditions) or "—"
            parts.append(
                f"<tr><td class='mono id'>{esc(row.option_id)}</td>"
                f"<td class='mono dim'>{esc(', '.join(row.preconditions_met) or '—')}</td>"
                f"<td class='mono'>{esc(', '.join(row.preconditions_unmet) or '—')}</td>"
                f"<td class='num'>{esc(row.cost_estimate)}</td>"
                f"<td>{_status_chip(row.reversibility)}</td>"
                f"<td>{_status_chip(row.feasibility_status)}</td>"
                f"<td class='mono dim'>{esc(blocking)}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append('<div class="empty"><b>无活跃方案</b>当前没有进入对比的活跃选项。</div>')
    parts.append("</section>")

    if state.command_mode(session_token):
        parts.append(
            "<section><h2>指挥员自提方案</h2>"
            "<form class='stack' method='post' action='/propose-option'>"
            "<label class='fld'>标题（必填）<input type='text' name='title'></label>"
            "<label class='fld'>概述（必填）<input type='text' name='summary'></label>"
            "<label class='fld'>依据 claim 主语（逗号分隔，如 water:W1, route:R2）<input type='text' name='claim_subjects'></label>"
            "<label class='fld'>风险（逗号分隔，可选）<input type='text' name='risks'></label>"
            "<label class='fld'>No-go 条件（逗号分隔，可选）<input type='text' name='no_go'></label>"
            "<label class='fld'>前置条件（逗号分隔，可选）<input type='text' name='preconditions'></label>"
            "<label class='fld'>可逆性<select name='reversibility'>"
            "<option value='reversible'>可逆</option>"
            "<option value='partially_reversible'>部分可逆</option>"
            "<option value='irreversible'>不可逆</option>"
            "</select></label>"
            "<button type='submit'>提交自提方案</button>"
            "</form>"
            "<form class='inline' method='post' action='/restaff'>"
            "<button type='submit'>重新参谋（重跑一轮编排+简报）</button>"
            "</form></section>"
        )
    parts.append("<section><h2>待取舍项与相关经验</h2><ul class='plain'>")
    for item in brief.question_items:
        parts.append(f"<li><span class='chip chip-warn'>{esc(item.kind)}</span> {esc(item.text)}</li>")
    for experience in brief.relevant_experiences:
        parts.append(f"<li><span class='chip chip-ok'>经验</span> {esc(experience)}</li>")
    if not brief.question_items and not brief.relevant_experiences:
        parts.append("<li class='dim'>无待取舍项，无注入经验。</li>")
    parts.append("</ul></section>")
    return "".join(parts)


def _view_orders(state: PanelState, session_token: str | None = None) -> str:
    ws = state.workspace
    command_mode = state.command_mode(session_token)
    live_orders = sorted(
        (o for o in ws.orders.values() if o.status.value not in _TERMINAL_ORDER_VALUES),
        key=lambda o: o.id,
    )
    history_count = len(ws.orders) - len(live_orders)
    parts = ["<section><h2>在途命令</h2>"]
    if live_orders:
        for order in live_orders:
            contract = next(
                (c for c in sorted(ws.action_contracts.values(), key=lambda c: c.source_version)
                 if c.order_id == order.id),
                None,
            )
            risk = f"L{int(contract.risk_level)}" if contract else "—"
            approved = any(a.action_contract_id == contract.id for a in ws.approvals.values()) if contract else False
            approval_note = "已批" if approved or contract is None or contract.risk_level < RiskLevel.L3 else "待批"
            parts.append(
                f"<div class='row'><span class='mono id'>{esc(order.id)}</span>"
                f"<span>{_status_chip(order.status.value)}</span>"
                f"<span class='mono dim'>{esc(order.option_id)}</span>"
                f"<span class='chip'>风险 {risk}</span>"
                f"<span class='chip chip-{('ok' if approved else 'warn')}'>{approval_note}</span></div>"
            )
            if command_mode and order.status == OrderStatus.ACTIVE:
                parts.append(
                    "<form class='inline' method='post' action='/pause'>"
                    f"<input type='hidden' name='order_id' value='{esc(order.id)}'>"
                    f"<input type='text' name='reason' placeholder='暂停原因（必填）'>"
                    "<button type='submit'>暂停</button></form>"
                )
    else:
        parts.append('<div class="empty"><b>无在途命令</b>指挥员决断采纳方案后，命令链在此追踪。</div>')
    if history_count:
        parts.append(f'<p class="note">另有 {history_count} 条历史命令（终态）未列出。</p>')
    parts.append("</section>")
    return "".join(parts)


def _view_questions(state: PanelState, session_token: str | None = None) -> str:
    ws = state.workspace
    open_questions = [
        q for q in sorted(ws.general_questions.values(), key=lambda q: q.id) if q.status == "open"
    ]
    answered_count = sum(1 for q in ws.general_questions.values() if q.status != "open")
    command_mode = state.command_mode(session_token)
    parts = ["<section><h2>待你回答</h2>", wb.plain_note("请示")]
    if open_questions:
        for question in open_questions:
            deadline = (
                f" · 建议 v{question.suggested_reply_by_version} 前答复"
                if question.suggested_reply_by_version is not None
                else ""
            )
            parts.append(
                f"<div class='row'><span class='chip chip-warn'>{esc(question.kind)}</span>"
                f"<span>{esc(question.question)}"
                f"<span class='dim mono'> · {esc(question.subject or '')}{esc(deadline)}</span></span></div>"
            )
            if command_mode:
                parts.append(
                    "<form class='inline' method='post' action='/answer'>"
                    f"<input type='hidden' name='question_id' value='{esc(question.id)}'>"
                    f"<input type='text' name='answer' placeholder='你的答复'>"
                    "<button type='submit'>答复并定向重排</button></form>"
                )
    else:
        parts.append('<div class="empty"><b>无待答请示</b>参谋部没有悬而未决的问题需要你裁决。</div>')
    if answered_count:
        parts.append(f'<p class="note">已有 {answered_count} 条请示答复归档。</p>')
    parts.append("</section>")
    return "".join(parts)


def _view_reviews(state: PanelState, session_token: str | None = None) -> str:
    ws = state.workspace
    parts = ["<section><h2>复盘记录</h2>"]
    reviews = sorted(ws.reviews.values(), key=lambda r: r.source_version)
    if reviews:
        for review in reviews[-3:]:
            parts.append(f"<div class='row'><span class='mono id'>{esc(review.id)}</span>"
                         f"<span class='dim'>{esc(review.scope)}</span>"
                         f"<span>回放 {review.entries_replayed} 条</span></div>")
            for finding in review.findings:
                parts.append(f"<div class='row'><span class='dim'>└</span><span>{esc(finding)}</span></div>")
    else:
        parts.append('<div class="empty"><b>尚无复盘</b>日界收束时自动生成复盘。</div>')
    parts.append("</section>")

    parts.append("<section><h2>受控记忆候选</h2>")
    candidates = sorted(ws.memory_candidates.values(), key=lambda c: c.id)
    command_mode = state.command_mode(session_token)
    if candidates:
        confirmed = sum(1 for c in candidates if c.status.value == "confirmed")
        pending = len(candidates) - confirmed
        parts.append(f'<p class="note">共 {len(candidates)} 条，确认 {confirmed} 条，待定 {pending} 条。</p>')
        parts.append(wb.plain_note("记忆候选"))
        for candidate in candidates[-6:]:
            row = (f"<div class='row'>{_status_chip(candidate.status.value)}"
                   f"<span>{esc(candidate.content)}</span>")
            if command_mode and candidate.status.value == "proposed":
                row += (
                    "<form class='inline' method='post' action='/memory'>"
                    f"<input type='hidden' name='candidate_id' value='{esc(candidate.id)}'>"
                    "<button type='submit' name='decision' value='confirm'>确认</button>"
                    "<button type='submit' name='decision' value='reject'>拒绝</button></form>"
                )
            row += "</div>"
            parts.append(row)
    else:
        parts.append('<div class="empty"><b>无记忆候选</b>复盘发现经你确认后才成为可用经验。</div>')
    parts.append("</section>")
    return "".join(parts)


_PAYLOAD_KEYS = (
    "decision",
    "order",
    "approval",
    "contract",
    "report",
    "verification",
    "review",
    "candidate",
    "question",
    "check",
)


def _payload_summary(payload: dict) -> str:
    for key in _PAYLOAD_KEYS:
        if key in payload:
            inner = payload[key]
            if isinstance(inner, dict):
                identifier = inner.get("id") or inner.get("order_id") or ""
                status = inner.get("status")
                label = f"{identifier}" + (f" [{status}]" if status else "")
                if label:
                    return label
    for key in ("order_id", "question_id", "reason", "scenario_id"):
        if key in payload:
            return str(payload[key])
    text = str(payload)
    return text if len(text) <= 96 else text[:93] + "..."


_TIMELINE_PAGE_SIZE = 80


def _view_timeline(state: PanelState, session_token: str | None = None, query=None) -> str:
    ws = state.workspace
    kind_filter = ((query or {}).get("kind") or [""])[0] or None
    raw_page = ((query or {}).get("page") or [""])[0]
    all_events = list(ws.events)
    kinds_seen = sorted({e.kind for e in all_events})
    events = [e for e in all_events if kind_filter is None or e.kind == kind_filter]
    keep_projector = ((query or {}).get("projector") or [""])[0] == "1"

    total_pages = max(1, -(-len(events) // _TIMELINE_PAGE_SIZE))
    try:
        page = int(raw_page) if raw_page else 0
    except ValueError:
        page = 0
    if page < 0:
        page = 0
    # Out-of-range pages render an empty page gracefully -- never a crash.
    # Page 0 is the NEWEST block (continuity with the pre-pager default view);
    # higher pages walk back in time toward the oldest block.
    end_index = len(events) - page * _TIMELINE_PAGE_SIZE
    start_index = max(0, end_index - _TIMELINE_PAGE_SIZE)
    page_events = events[start_index:end_index] if end_index > 0 else []
    newest_first = list(reversed(page_events))
    broken = set(ws.verify_audit_chain())

    def _qs(kind: str | None, target_page: int) -> str:
        params = []
        if kind:
            params.append(f"kind={esc(kind)}")
        params.append(f"page={target_page}")
        if keep_projector:  # wall mode survives pagination
            params.append("projector=1")
        return "&amp;".join(params)

    chips = [f'<a href="/timeline?{_qs(None, page)}">全部</a>']
    for kind in kinds_seen:
        mark = ' style="color:#3fb950"' if kind == kind_filter else ""
        chips.append(
            f'<a href="/timeline?{_qs(kind, page)}"{mark}>{esc(kind)}</a>'
        )

    nav_bits = []
    if page > 0:
        nav_bits.append(f'<a href="/timeline?{_qs(kind_filter, page - 1)}">← 上一页</a>')
    nav_bits.append(f"<span class='dim'>第 {page + 1} / {total_pages} 页 · 每页 {_TIMELINE_PAGE_SIZE} 条</span>")
    if page_events and page < total_pages - 1:
        nav_bits.append(f'<a href="/timeline?{_qs(kind_filter, page + 1)}">下一页 →</a>')

    parts = [
        "<section><h2>事件链 · 按时间倒序</h2>",
        wb.plain_note("审计链"),
        '<div class="note">共 '
        f"{len(all_events)} 条"
        + (f"，过滤 {esc(kind_filter)} 后剩 {len(events)} 条" if kind_filter else "")
        + "。</div><p></p>",
        " ".join(chips),
        '<div class="note">' + " · ".join(nav_bits) + "</div><p></p>",
    ]
    for event in newest_first:
        tone = "chip-bad" if event.sequence in broken else "chip-dim"
        seq_style = "color:#f85149" if event.sequence in broken else ""
        parts.append(
            f"<div class='row'>"
            f"<span class='mono' style='{seq_style}'>{event.sequence:04d}</span>"
            f"<span class='chip {tone}'>{esc(event.kind)}</span>"
            f"<span class='mono dim'>{esc(_payload_summary(event.payload))}</span>"
            f"</div>"
        )
    if not events:
        parts.append('<div class="empty"><b>无匹配事件</b>调整过滤器后重试。</div>')
    parts.append("</section>")
    return "".join(parts)


_VIEWS = {
    "/overview": _view_overview,
    "/brief": _view_brief,
    "/orders": _view_orders,
    "/questions": _view_questions,
    "/reviews": _view_reviews,
}

_TIMELINE_VIEW = _VIEWS.setdefault  # noqa: E305  (placeholder guard)

def _dispatch_view(state, route, session_token, query):
    if route == "/timeline":
        return _view_timeline(state, session_token, query)
    if route == "/map":
        return wb.render_map_view(state, session_token, query)
    if route == "/org":
        return wb.render_org_view(state, session_token, query)
    if route == "/drill":
        return wb.render_drill_view(state, session_token, query)
    view = _VIEWS.get(route)
    if view is None:
        return None
    return view(state, session_token)


def _render_login(state: PanelState, *, error: bool = False) -> str:
    """Legacy single-box login renderer — kept as a thin alias of the P16 T2
    three-zone light page so existing call sites keep their semantics."""
    return render_login_page(state, commander_error=error)


_VIEW_TITLES = {**dict(ROUTES), "/drill": "演练控制台"}


# ---------------------------------------------------------------------------
# 首页（doc 02：组织架构原图 + 动态叠加；P16 T3）


def _selected_deputy_event(state: PanelState, member_token) -> str:
    member = state.member_session(member_token)
    if member is None:
        return ''
    try:
        allowed = state.deputy_events(member_token)
    except PermissionError:
        return ''
    if member.selected_event_id in allowed:
        return member.selected_event_id
    if state.active_event_id in allowed:
        return state.active_event_id
    return allowed[0] if allowed else ''


@contextmanager
def _deputy_event_context(state: PanelState, route: str, *, session_token=None, member_token=None):
    """Business-role request context; restore admin's workspace on every exit.

    Session selection is separate from check-in. Snapshot writes remain subject to
    existing named-role gates and persist to this event, not admin's open space.
    """
    # Event list and intake operations address explicit registry event IDs.
    # They must work before selection, even when all older events are closed.
    if route in {'/materials', '/materials/daily'} or route.startswith('/materials/daily/') or route == '/events' or route.startswith('/events/'):
        yield
        return
    scoped = bool(_unified.parent(route)) or route.startswith(
        ('/workflow', '/sand/', '/review/', '/materials/', '/personnel/', '/tasks/', '/dispatch/', '/temporary/', '/captain-arrival', '/material-', '/export/', '/uploads/'))
    if not scoped or state.command_mode(session_token):
        yield
        return
    viewer_role = _business.role(state, member_token)
    event_id = _business.selected_event(state, member_token)
    if not event_id and route.startswith('/workflow'):
        from .workflow import closed_event
        event_id=closed_event(state,member_token)
    if not event_id:
        member = state.member_session(member_token)
        if member and member.selected_event_id and not viewer_role:
            raise PermissionError('当前副指挥任命已不可用，请返回登录页。')
        yield
        return
    with state._event_authority_lock:
        if state.registry.events[event_id].status == 'registered':
            space = ScenarioWorkspace('pending-' + event_id, general_auth=state.auth)
            space.event_id = event_id
            target = None
        else:
            space, target, _ = state._appointment_space(event_id)
        previous = state.workspace, state.active_event_id, state.db_path
        previous_teams = getattr(state, '_business_team_ids', None)
        if state.active_event_id != event_id or state.workspace.event_id != event_id:
            state.workspace = space
            state.db_path = target
        state.active_event_id = event_id
        state._business_team_ids = (_business.teams(state, member_token, event_id)
                                    if viewer_role == 'captain' else None)
        try:
            yield
        finally:
            updated = state.workspace
            state.workspace, state.active_event_id, state.db_path = previous
            if previous[1] == event_id and updated.event_id == event_id and previous[0].event_id == event_id:
                state.workspace = updated
            state._business_team_ids = previous_teams


def home_gate(state: PanelState, *, session_token=None, member_token=None) -> str:
    """首页门禁：未登录不能进入首页（01 号 §9）；成员仅登录未刷码停留登录页。

    返回 "ok" / "login"。fail-closed：一切无法验证的身份一律送回登录页。
    """
    if state.command_mode(session_token):
        return "ok"
    session = state.member_session(member_token)
    if session is None:
        return "login"
    try:
        if _business.role(state, member_token):
            return "ok"
    except PermissionError:
        return "login"
    if not state._effective_bound_event(session):
        return "login"  # 仅登录未刷码 → 待到场卡（01 号 §7 成功跳转）
    return "ok"


def _home_viewer(state: PanelState, *, session_token, member_token) -> tuple[str, str, str]:
    """返回 (role, viewer_phone, event_id)：指挥员取当前事件上下文，成员取绑定事件。"""
    if state.command_mode(session_token):
        return "commander", "", state.active_event_id or ""
    session = state.member_session(member_token)
    assert session is not None  # gate 已过
    event_id = _business.selected_event(state, member_token) or state._effective_bound_event(session)
    if not event_id:
        permitted = state.deputy_events(member_token)
        if state.active_event_id in permitted:
            event_id = state.active_event_id
        elif len(permitted) == 1:
            event_id = permitted[0]
    return "member", session.phone, event_id


def _render_home_personnel_actions(state: PanelState, *, role: str,
                                   event_id: str, member_token=None) -> str:
    """Read-only event summary; arrangement authority stays in existing routes."""
    try:
        deputy = bool(event_id) and event_id in state.deputy_events(member_token)
    except PermissionError:
        deputy = False
    record = state.registry.events.get(event_id)
    parts = ['<div class="lz-card" id="personnel-arrangements"><h2>待安排临时人员</h2>']
    if role in {"commander", "duty"} or deputy:
        if record is None:
            parts.append('<p class="lz-note">请先进入事件，查看本事件临时人员与安排情况。</p>')
        elif record.status == "registered":
            parts.append('<p class="lz-note">本事件尚未启动，临时人员登记与编组暂未开放。</p>')
        else:
            try:
                people = state.temporary_people(event_id)
            except (PanelActionError, ValueError):
                parts.append('<p class="lz-note">本事件临时人员记录暂无法核对，不能据此判断人数。</p>')
            else:
                pending = sorted((p for p in people.values() if not p.pos_code),
                                 key=lambda p: (p.name, p.phone))
                parts.append(f'<p>{esc(record.name)} · {esc(event_id)}：'
                             f'待安排 <b>{len(pending)}</b> 人 · '
                             f'已编组 <b>{len(people) - len(pending)}</b> 人</p>')
                if pending:
                    parts.append('<ul>' + ''.join(
                        f'<li>{esc(p.name)} · {esc(p.unit)} · {esc(mask_phone(p.phone))}'
                        f' · {"已确认到场" if p.arrived_at else "未确认到场"}</li>'
                        for p in pending) + '</ul>')
                else:
                    parts.append('<p class="lz-dim">暂无待安排临时人员。</p>')
        if deputy:
            parts.append('<p><a class="lz-btn" href="/temporary/assign?event_id='
                         + esc(event_id) + '">人员编组／调组</a> '
                         '<a class="lz-link" href="/dispatch">副指挥工作台 →</a></p>')
        else:
            parts.append('<p class="lz-note">此处只读。人员编组与调组由本事件指定副指挥办理；'
                         '请使用其本人账号登录。</p>')
    else:
        parts.append('<p class="lz-note">临时人员登记后进入本事件待安排区，'
                     '由指定副指挥编组；登记、到场和可派状态分别记录。</p>')
    if role != "commander":
        parts.append('<p><a class="lz-link" href="/temporary">临时人员登记／查看本人安排 →</a></p>')
    parts.append('</div>')
    return ''.join(parts)


def _render_home_body(state: PanelState, *, session_token=None, member_token=None) -> str:
    role, viewer_phone, event_id = _home_viewer(state, session_token=session_token,
                                                member_token=member_token)
    record = state.registry.events.get(event_id)
    arrivals = {
        s.phone: s.arrived_at
        for s in state.member_sessions.values()
        if s.arrived_at and s.bound_event_id == event_id
    }
    # 到场统计只信当前已打开的事件任务空间；否则显式空态，不跨空间读数
    workspace_ok = bool(event_id) and state.active_event_id == event_id
    business_role = '' if role == 'commander' else _business.role(state, member_token)
    detail_phones = None
    actions = _render_home_personnel_actions(state, role='duty' if business_role else role,
                   event_id=event_id, member_token=member_token)
    if business_role:
        actions += _unified.event_records(state, '/')
    body = actions + _home.render_homepage_view(
        event_record=record if workspace_ok else None,
        registry=state.registry if record is not None else None,
        workspace=state.workspace if workspace_ok else None,
        event_id=event_id if workspace_ok else "",
        viewer_phone=viewer_phone,
        viewer_role='commander' if business_role else role,
        arrivals=arrivals,
        generated_at=utc_now_iso(),
        detail_phones=detail_phones,
    )
    if role == "commander" and not event_id:
        body += (
            '<div class="lz-empty"><b>尚未进入任何事件任务空间</b>'
            '到场与组织动态在「火情事件」页选择事件进入后展示。'
            '<a class="lz-link" href="/events">前往火情事件 →</a></div>'
        )
    if not workspace_ok and event_id:
        body += (
            f'<div class="lz-note">事件 {_home.esc(event_id)} 的任务空间未在本地打开，'
            '到场统计以指挥部现场为准。</div>'
        )
    if business_role and workspace_ok:
        from .org_float import render
        body = render(state,member_token,event_id=event_id,viewer_phone=viewer_phone,generated_at=utc_now_iso())
    user = (
        "指挥员"
        if role == "commander"
        else f"{_home.esc(state.member_session(member_token).name)}（"
             f"{_home.esc(mask_phone(viewer_phone))}）"
    )
    user += _topbar_logout_form(
        "/logout" if role == "commander" else "/logout/member", "登出",
    )
    nav_active = "/"
    return _light_wrap(title="人员架构", body=body,
                       nav=_light_nav(_COMMANDER_LIGHT_NAV if role == "commander" or business_role else _MEMBER_LIGHT_NAV, nav_active),
                       user=user)



def render_model_settings_page(state: PanelState, token: str | None, *, error: str = '') -> str:
    session = state.model_session(token)
    from .sand_command import render_model_binding
    return _light_wrap(
        title='大模型配置（Key）', body=_models.render(session, error=error)+render_model_binding(state, token),
        nav=_light_nav(_COMMANDER_LIGHT_NAV, '/sand'),
        body_class=' lz-model-page',
        user='<span>指挥员 · 指挥模式</span>' + _topbar_logout_form('/logout', '登出'))


def render_page(state: PanelState, route: str, *, session_token=None, query=None,
                member_token=None) -> tuple[int, str]:
    with state._event_authority_lock:
        try:
            if route in {'/materials', '/materials/daily'}:
                from . import resource_pages, workflow
                member = state.member_session(member_token)
                admin = state.command_mode(session_token)
                if not member and not admin: return 303, ''
                if query: return 404, '<h1>404</h1>'
                if route == '/materials/daily':
                    from .resource_directory_page import daily
                    body = daily(state, member_token)
                else:
                    body = resource_pages.STYLE + resource_pages.switch() + '<section class="lz-card"><h1>人员与物资</h1><p>日常资料独立维护；进入本火情查看人员、公共保障和额外物资需求。</p></section>'
                return 200, _unified.daily_entry(state,member_token,workflow.decorate(state,'/materials',_light_wrap(title='人员与物资', body=body,
                    nav=_light_nav(_COMMANDER_LIGHT_NAV, '/materials'),
                    user=_topbar_logout_form('/logout' if admin else '/logout/member', '登出')),member_token))
            with _deputy_event_context(state, route, session_token=session_token, member_token=member_token):
                if route in {'/materials/event', '/materials/adviser-feed'}:
                    from . import resource_pages, resource_advisor, workflow
                    eid = state.active_event_id
                    member = state.member_session(member_token)
                    admin = state.command_mode(session_token)
                    if not member and not admin: return 303, ''
                    if query and query != {'event_id': [eid]}: return 404, '<h1>404</h1>'
                    if route.endswith('adviser-feed'):
                        if not admin:
                            if eid not in _business.events(state, member_token): raise PermissionError('无权读取当前火情。')
                            if _business.role(state, member_token) not in {'duty', 'deputy'}:
                                resource_advisor.require_planner(state, member_token, eid)
                        return 200, resource_pages.advice(state, eid)
                    body = resource_pages.event_page(state, member_token, admin=admin)
                    return 200, _unified.daily_entry(state,member_token,workflow.decorate(state,'/materials',_light_wrap(title='本火情人员与物资', body=body,
                        nav=_light_nav(_COMMANDER_LIGHT_NAV, '/materials'),
                        user=_topbar_logout_form('/logout' if admin else '/logout/member', '登出')),member_token))
                role = _business.role(state, member_token)
                canonical = _unified.ALIASES.get(route, route)
                main = _unified.parent(canonical)
                if route == '/events/model-settings':
                    from . import deputy_models
                    with _deputy_event_context(state, '/sand/model-settings', member_token=member_token):
                        _, eid = deputy_models.authority(state, member_token)
                        if query and query != {'event_id': [eid]}:
                            return 404, '<h1>404</h1>'
                        return 200, deputy_models.page(state, member_token)
                if route == '/workflow/records':
                    from . import workflow, nine_flow_view
                    member, record = workflow.context(state, member_token)
                    params = query or {}
                    if set(params) != {'step'} or len(params['step']) != 1 or params['step'][0] not in {str(i) for i in range(9)}:
                        return 409, '<h1>工作环节无效</h1><a href="/workflow/guide">返回九步走</a>'
                    body = nine_flow_view.page(state, member_token, int(params['step'][0]))
                    page = _light_wrap(title='九步办理与交接',body=body,nav=_light_nav(_COMMANDER_LIGHT_NAV,''),
                        user=f'<span>{esc(member.name)}</span>'+_topbar_logout_form('/logout/member','登出'))
                    return 200, _unified.controls(state,member_token,workflow.decorate(state,route,page,member_token))
                if route in {'/workflow', '/workflow/guide', '/workflow/functions', '/workflow/inbox'}:
                    from . import workflow
                    member, record = workflow.context(state, member_token)
                    allowed = {'event_id', 'step'}
                    if query and (set(query) - allowed
                                  or query.get('event_id', [record.event_id]) != [record.event_id]):
                        return 404, '<h1>404</h1>'
                    step_q = None
                    if query and 'step' in query:
                        raw = query['step'][0] if len(query['step']) == 1 else ''
                        step_q = int(raw) if raw.isdigit() and int(raw) in range(9) else None
                    body = (workflow.guide_page(state, member_token, step_q) if route == '/workflow/guide'
                            else workflow.functions_page(state, member_token) if route == '/workflow/functions'
                            else workflow.assistant_view.inbox(state,member_token) if route == '/workflow/inbox'
                            else workflow.choice(state, member_token))
                    page = _light_wrap(title='事件使用方式', body=body,
                        nav=_light_nav(_COMMANDER_LIGHT_NAV, ''),
                        user=f'<span>{esc(member.name)}</span>' + _topbar_logout_form('/logout/member', '登出'))
                    return 200, _unified.controls(state, member_token,
                        workflow.decorate(state, route, page, member_token))
                if route == '/events' and role == 'deputy' and not (query or {}).get('list'):
                    if len(_business.events(state, member_token)) == 1:
                        from . import workflow
                        body = workflow.choice(state, member_token)
                        page = _light_wrap(title='事件使用方式', body=body,
                            nav=_light_nav(_COMMANDER_LIGHT_NAV, '/events'),
                            user=_topbar_logout_form('/logout/member', '登出'))
                        from .deputy_models import decorate
                        return 200, decorate(state, _unified.decorate(state, route, page, member_token), member_token)
                if main and canonical not in _business.MODULES and canonical in _unified.CHILD_LABELS:
                    if not role:
                        member = state.member_session(member_token)
                        # A currently assigned professional must be able to read
                        # the named request and use the original scope checks.
                        if member and canonical == '/sand/task-release' and state.task_release_views(member_token):
                            return _render_page_in_context(state, '/tasks/release', session_token=session_token,
                                query=query, member_token=member_token)
                        if member is None and not state.command_mode(session_token):
                            return 404, '<h1>404</h1>'
                        page = _light_wrap(title='统一业务框架', body='<div class="lz-empty">原角色独立工作台已暂停。请从统一模块进入业务；账号和历史记录保留。</div>',
                            nav=_light_nav(_COMMANDER_LIGHT_NAV,main), user=_topbar_logout_form('/logout/member' if member else '/logout','登出'))
                        return 200, page
                    selected = _business.selected_event(state,member_token)
                    if not selected:
                        return 200, _unified.decorate(state,canonical,_wrap_business_page(body='<div class="lz-empty">暂无本人可进入的事件。</div>',title=_unified.CHILD_LABELS[canonical],active=main,commander=False,member=state.member_session(member_token),business_role=role),member_token)
                    if query and query.get('event_id',[selected]) != [selected]:
                        return 404, '<h1>404</h1>'
                    legacy = _unified.LEGACY.get(canonical,canonical)
                    q = dict(query or {})
                    q.setdefault('event_id',[state.active_event_id])
                    if legacy == '/dispatch' and role == 'captain':
                        legacy = '/callouts'
                    status, page = _render_page_in_context(state,legacy,session_token=session_token,query=q,member_token=member_token)
                    if status not in (200,404):
                        return status, page
                    try:
                        common = _unified.event_records(state,main)+_unified.child_records(state,canonical)
                    except ValueError as error:
                        return 409, _light_wrap(title='记录待核对',body='<p>'+esc(str(error))+'</p>',nav='',user='')
                    if status != 200:
                        page = _wrap_business_page(body=common+'<p>本项办理权限按职责核对，当前账号可查看事件信息。</p>',
                            title=_unified.CHILD_LABELS[canonical],active=main,commander=False,
                            member=state.member_session(member_token),business_role=role)
                    else:
                        page = page.replace('<div class="lz-wrap">','<div class="lz-wrap">'+common,1)
                    return 200, _unified.decorate(state,canonical,page,member_token)
                status, page = _render_page_in_context(state,route,session_token=session_token,query=query,member_token=member_token)
                # 现场办理页只留"任务卡 + 一个按钮"，隐藏页壳自带的
                # 全部任务 / 查看办理记录 / 办理说明 三块折叠（管理页不受影响）。
                if status == 200 and route in {'/sand/task-release', '/sand/task-receipts',
                                               '/sand/task-feedback'}:
                    page = page.replace('<body', '<style>.emergency-overview,.emergency-raw{display:none!important}'
                                        '</style><body', 1)
                if status == 200 and role and main:
                    page = _unified.decorate(state,route,page,member_token)
                if status == 200 and (route == '/events' or route.startswith('/events/') and route.endswith('/details')):
                    from .deputy_models import decorate
                    page = decorate(state, page, member_token, admin=state.command_mode(session_token))
                return status,_unified.daily_entry(state,member_token,page) if status==200 else page
        except PermissionError:
            return 404, '<h1>404</h1>'
        except PanelActionError:
            return 409, '<h1>409</h1><p>事件任务空间待核对。</p>'
        except ValueError:
            if route.startswith('/materials'):
                return 409, '<h1>物资资料待核对</h1><p>当前资料、版本或审计来源不一致；原记录保留，未回退旧版。</p>'
            raise


def _render_page_in_context(
    state: PanelState,
    route: str,
    *,
    session_token: str | None = None,
    query: dict[str, list[str]] | None = None,
    member_token: str | None = None,
) -> tuple[int, str]:
    version_before = state.workspace.version
    business_role = '' if state.command_mode(session_token) else _business.role(state, member_token)
    if business_role and route in _business.MODULES and route != '/events':
        selected = _business.selected_event(state, member_token)
        record = state.registry.events.get(selected)
        if record is None or record.status == 'registered':
            note = ('本事件尚未启动，人员架构、沙盘、资源快照与复盘尚未建立。'
                    if record else '暂无本人可进入的事件；队长须先由副指挥召集，查看页面不记到场。')
            body = f'<div class="lz-empty">{esc(note)}<p><a href="/events">查看火情事件 →</a></p></div>'
            return 200, _wrap_business_page(body=body, title=_business.MODULES[route], active=route,
                commander=False, member=state.member_session(member_token), business_role=business_role)
    if route == '/models':
        if not state.command_mode(session_token):
            return 403, '<h1>403</h1><p>模型连接设置仅指挥员可访问。</p>'
        return 200, render_model_settings_page(state, session_token)
    if route == "/login":
        if state.command_mode(session_token):
            return 303, ""
        return 200, render_login_page(
            state,
            member_token=member_token,
            back_space=((query or {}).get("back") or [""])[0] == "space",
        )
    if route == "/":
        # 首页门禁（doc 01 §9）：未登录/未入岗一律送回登录页（HTTP 层重定向）。
        if home_gate(state, session_token=session_token, member_token=member_token) != "ok":
            return 303, ""
        page = _render_home_body(state, session_token=session_token, member_token=member_token)
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/materials":
        # 人员与物资（doc 04）：登录即可读；维护动作另由 POST 路由限指挥员。
        commander_mat = state.command_mode(session_token)
        member_mat = None if commander_mat else state.member_session(member_token)
        if not commander_mat and member_mat is None:
            return 303, ""
        page = render_materials_page(
            state,
            viewer_role="commander" if commander_mat or business_role else "member",
            viewer_phone="" if commander_mat else getattr(member_mat, "phone", ""),
            viewer_event_id="" if commander_mat else (_business.selected_event(state, member_token) or state._effective_bound_event(member_mat)),
            query=query,
        )
        page = _wrap_business_page(
            body=page, title="人员与物资", active="/materials",
            commander=commander_mat, member=member_mat,
            deputy=bool(_selected_deputy_event(state, member_token)),
            business_role=business_role,
            context=_business_context(state, title="人员与物资", active="/materials"),
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/sand":
        # 指挥调度沙盘（doc 03）：登录即可看；projector=1 为公共屏模式，
        # 只显示已发布内容（doc 03 §12 公共屏行）。
        commander = state.command_mode(session_token)
        member = None if commander else state.member_session(member_token)
        if not commander and member is None:
            return 303, ""
        projector = ((query or {}).get("projector") or [""])[0] == "1"
        role = "public" if projector else ("commander" if commander or business_role else "member")
        if (role == 'member' and state.active_event_id
                and not state.member_can_view_current_event(member_token)):
            return 404, '<h1>404</h1>'
        if business_role:
            viewer_word = f"{_business.LABELS[business_role]} {member.name}（{mask_phone(member.phone)}）"
        elif role == "commander":
            viewer_word = "指挥员"
        elif role in {"member", "captain"}:
            viewer_word = (
                f"{_business.LABELS.get(business_role, '到场人员')} {getattr(member, 'name', '') or ''}"
                f"（{mask_phone(getattr(member, 'phone', ''))}）"
            )
        else:
            viewer_word = "公共屏 · 仅已发布内容"
        draft_owner, draft_event = draft_context(state, 'member' if business_role else role, session_token if commander else member_token)
        page = render_sand_page(state, role=role, viewer_word=viewer_word,
                                draft_owner=draft_owner, draft_event=draft_event,
                                viewer_id=member.phone if role in {'member', 'captain'} else '',
                                viewer_token=member_token if role == 'captain' else None,
                                simulation_token=session_token if role == 'commander' else None)
        page = _wrap_business_page(
            body=page, title="沙盘指挥", active="/sand",
            commander=commander, member=member, public=(role == "public"),
            deputy=bool(_selected_deputy_event(state, member_token)),
            business_role=business_role,
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/public":
        # 公共屏免登录只读入口（doc 01 §9 / doc 05 §9）：已发布摘要。
        # HTTP 层在 do_GET 中先行重定向；此处仅作直接调用的一致信号。
        return 303, ""
    if route == "/review":
        # 复盘与经验（doc 05）：登录即可读；作业动作另由 POST 限权。
        commander = state.command_mode(session_token)
        member = state.member_session(member_token)
        projector = ((query or {}).get("projector") or [""])[0] == "1"
        if not commander and member is None and not projector:
            return 303, ""
        from . import review_meeting as meeting
        bound = bool(state.workspace.event_id)
        from .review_inquiry import viewer_key
        if bound:
            if projector or not (commander or meeting.is_owner(state, member_token)
                    or meeting.is_deputy(state, member_token) or state.member_can_view_current_event(member_token)):
                return 404, '<h1>404</h1>'
            try:
                meeting.service(state)
            except ValueError:
                return 409, '<h1>409</h1><p>复盘事件审计待核对。</p>'
        owner_view = (meeting.is_owner(state, member_token) if bound else member and state.review_ledger.owner_phone
                      and member.phone == state.review_ledger.owner_phone)
        role = "public" if projector else ('owner' if owner_view else 'commander' if commander or business_role else 'member')
        if role == "commander":
            viewer_word = "指挥员"
        elif role == "owner":
            viewer_word = (
                f"复盘负责人 {getattr(member, 'name', '') or ''}"
                f"（{mask_phone(getattr(member, 'phone', ''))}）"
            )
        elif role == "member":
            viewer_word = (
                f"{'副指挥长' if _selected_deputy_event(state, member_token) else '到场人员'} {getattr(member, 'name', '') or ''}"
                f"（{mask_phone(getattr(member, 'phone', ''))}）"
            )
        else:
            viewer_word = "公共屏 · 仅已发布摘要"
        tab = ((query or {}).get("tab") or ["summary"])[0]
        if bound and tab == 'timeline' and not (commander or business_role or owner_view or meeting.is_deputy(state, member_token)):
            return 403, '<h1>403</h1><p>历史明细仅本事件指挥、指定副总指挥与复盘负责人可读。</p>'
        msg = ((query or {}).get("msg") or [""])[0]
        rid = ((query or {}).get("rid") or [""])[0]
        try:
            page = render_review_page(
                state, role=role, viewer_word=viewer_word, tab=tab, message=msg,
                selected_report=rid,
                review_token=member_token if member else session_token,
                review_deputy=bound and meeting.is_deputy(state, member_token),
                inquiry_viewer=viewer_key(state, session_token, member_token) if bound else '',
            )
        except (ValueError, KeyError, TypeError):
            if not bound:
                raise
            return 409, '<h1>409</h1><p>复盘历史版本待核对。</p>'
        page = _wrap_business_page(
            body=page, title="复盘", active="/review",
            commander=commander, member=member, public=(role == "public"),
            deputy=bool(_selected_deputy_event(state, member_token)),
            business_role=business_role,
            context=_business_context(state, title="复盘与经验", active="/review",
                                      public=(role == "public")),
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/task-materials":
        try:
            permitted = state.deputy_events(member_token)
            if set(query or {}) - {'event_id', 'order_id', 'item_id'} or any(len(v) != 1 for v in (query or {}).values()):
                raise ValueError('任务输入查询字段重复或不受支持。')
            selected_event = ((query or {}).get('event_id') or [''])[0]
            if selected_event not in permitted:
                return 404, '<h1>404</h1>'
            page = render_task_material_inputs_page(state, member_token, selected_event, query or {})
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError, OSError) as exc:
            return 409, f'<p>{esc(exc)}</p>'
        return 200, page
    if route == "/dispatch":
        try:
            permitted = state.deputy_events(member_token)
        except PermissionError:
            permitted = []
        if not permitted:
            return 404, "<h1>404</h1>"
        page = render_deputy_dispatch_page(
            state, state.member_session(member_token), permitted
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == '/supply-draft':
        with state._event_authority_lock:
            try:
                selected = _material_supply_draft_page.selection(state, query)
                body = _material_supply_draft_page.body(state, session_token, *selected)
            except PermissionError:
                return 404, '<h1>404</h1>'
            except (PanelActionError, ValueError):
                return 409, '<h1>来源待核对</h1><p>当前事件或草案来源不一致。</p>'
            page = _light_wrap(title='编制补给任务附件', body=body, body_class=' lz-work-ui',
                nav='<a href="/sand">返回任务沙盘</a>',
                user='<span>指挥编制入口</span>' + _topbar_logout_form('/logout', '登出'))
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/material-supply':
        with state._event_authority_lock:
            try:
                event_id, item_id, source = _material_supply_page.selection(query, state)
                member = _material_supply_page.require_page(state, member_token, event_id)
                body = _material_supply_page.body(state, member_token, event_id, item_id, source)
            except PermissionError:
                return 404, '<h1>404</h1>'
            except (PanelActionError, ValueError):
                return 409, '<h1>来源待核对</h1><p>当前事件、身份或联系资料不一致。</p>'
            page = _light_wrap(title='人工补给联系', body=body, body_class=' lz-work-ui',
                nav='<a href="/temporary/leader">本组状态</a>',
                user=f'<span>{esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
                     + _topbar_logout_form('/logout/member', '登出'))
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/material-custody':
        event_fields = (query or {}).get('event_id', [state.active_event_id or ''])
        if len(event_fields) != 1:
            return 404, '<h1>404</h1>'
        with state._event_authority_lock:
            try:
                member = _material_custody_page.require_page(state, member_token, event_fields[0])
                body = _material_custody_page.body(state, member_token, event_fields[0])
            except PermissionError:
                return 404, '<h1>404</h1>'
            except (PanelActionError, ValueError):
                return 409, '<h1>来源待核对</h1><p>当前事件身份或审计来源不一致。</p>'
            closeout_nav = ('<a href="/materials/closeout">关闭事件物资收尾</a>'
                            if state.registry.events[event_fields[0]].status == 'closed'
                            else '<a href="/temporary/leader">本组状态</a>')
            page = _light_wrap(title='撤离后装备接收与核验结清', body=body, body_class=' lz-work-ui',
                nav=closeout_nav,
                user=f'<span>{esc(member.name)}（{esc(mask_phone(member.phone))}）</span>'
                     + _topbar_logout_form('/logout/member', '登出'))
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == "/material-transfers":
        member = state.member_session(member_token)
        event_id = state.active_event_id or ''
        if member is None or not event_id:
            return 404, '<h1>404</h1>'
        try:
            deputy = event_id in state.deputy_events(member_token)
        except PermissionError:
            deputy = False
        selected = state.event_callouts(event_id)
        captain_squad = ''
        if state._effective_bound_event(member) == event_id:
            for sid, callout in selected.items():
                squad = state.ledger.squads.get(sid)
                if (callout.leader_phone == member.phone and squad is not None
                        and squad.leader_phone == member.phone):
                    captain_squad = sid
                    break
        if not deputy and not captain_squad:
            return 404, '<h1>404</h1>'
        page = render_material_transfer_page(state, member, event_id, deputy, captain_squad)
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/material-supply/receipts':
        try:
            from .material_supply_receipts_page import body
            page = _light_wrap(title='补给在途与分批实收', body=body(state, member_token),
                nav='<a href="/material-supply/execution">核实库存与补给执行</a><a href="/tasks/formal">正式任务接收</a>',
                user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='补给实收来源待核对', body='<p>'+esc(str(error))+'</p>', nav='', user='')
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/material-supply/execution':
        try:
            from .material_supply_execution_page import body
            page = _light_wrap(title='核实库存与补给执行', body=body(state, member_token),
                nav='<a href="/tasks/release">任务复核与发布</a><a href="/material-supply">补给联系</a><a href="/material-supply/receipts">补给在途与分批实收</a><a href="/materials/maintenance-authority">长期物资维护授权</a>',
                user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='共享补给来源待核对', body='<p>'+esc(str(error))+'</p>', nav='', user='')
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/tasks/relations':
        try:
            from .task_relations_page import body
            page = _light_wrap(title='任务关联与开始', body=body(state, member_token),
                nav='<a href="/tasks/formal">正式任务接收</a><a href="/tasks/release">任务复核与发布</a>',
                user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            page = _light_wrap(title='任务关联待核对', body='<p>' + esc(str(error)) + '</p>', nav='', user='')
            return 409, page
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/materials/maintenance-authority':
        try:
            from .material_maintenance_authority_page import body
            page = _light_wrap(title='长期物资维护授权', body=body(state, member_token),
                nav='<a href="/material-supply/execution">物资供应执行</a><a href="/materials">人员与物资</a>', user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='长期维护授权待核对', body='<p>' + esc(str(error)) + '</p>', nav='', user='')
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/personnel/manage':
        try:
            from .personnel_management_page import body
            return 200, _light_wrap(title='具名人员管理', body=body(state, member_token, state.active_event_id),
                nav='<a href="/dispatch">人员安排</a>', user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='人员安排待核对', body='<p>'+esc(str(error))+'</p>', nav='', user='')
    if route == '/materials/accounts':
        try:
            from .material_closeout_accounts_page import body
            page = _light_wrap(title='收尾核实与长期底账调整', body=body(state, member_token, query),
                nav='<a href="/materials/closeout">关闭事件收尾</a><a href="/materials/records">长期资料</a>', user=_topbar_logout_form('/logout/member', '登出'))
            return 200, page
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, KeyError, PanelActionError) as error:
            return 409, _light_wrap(title='收尾来源待核对', body='<p>'+esc(str(error))+'</p>', nav='', user='')
    if route == '/materials/records':
        try:
            from .material_catalog_page import body
            page = _light_wrap(title='长期物资资料维护', body=body(state, member_token),
                nav='<a href="/materials/maintenance-authority">长期维护授权</a>', user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='资料来源待核对', body='<p>' + esc(str(error)) + '</p>', nav='', user='')
        return 200, page
    if route == '/tasks/feedback':
        try:
            from .formal_task_feedback_page import body
            page = _light_wrap(title='执行反馈与核验关闭', body=body(state, member_token),
                nav='<a href="/tasks/formal">正式任务接收</a><a href="/tasks/relations">任务关联与开始</a>', user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='执行依据待核对', body='<p>' + esc(str(error)) + '</p>', nav='', user='')
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/tasks/formal':
        try:
            from .formal_task_receipts import body
            page = _light_wrap(title='正式任务接收', body=body(state, member_token),
                nav='<a href="/tasks/release">任务复核与发布</a><a href="/tasks/relations">任务关联与开始</a><a href="/tasks/feedback">执行反馈与核验关闭</a>', user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='正式任务依据待核对', body='<p>' + esc(str(error)) + '</p>', nav='', user='')
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/tasks/release':
        try:
            if not state.task_release_views(member_token):
                return 404, '<h1>404</h1>'
            from .task_release_page import body
            page = _light_wrap(title='当前任务复核与发布', body=body(state, member_token),
                nav='<a href="/dispatch">选队召集</a><a href="/dispatch/task-review">任务分类登记</a><a href="/dispatch/professionals">专业职责指定</a><a href="/tasks/formal">正式任务接收</a><a href="/material-supply/execution">核实库存与补给执行</a>',
                user=_topbar_logout_form('/logout/member', '登出'))
        except PermissionError:
            return 404, '<h1>404</h1>'
        except (ValueError, PanelActionError) as error:
            return 409, _light_wrap(title='任务复核依据待核对', body='<p>' + esc(str(error)) + '</p>', nav='', user='')
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == '/dispatch/professionals':
        try:
            permitted = state.deputy_events(member_token)
        except PermissionError:
            permitted = []
        if not permitted:
            return 404, '<h1>404</h1>'
        from .professional_assignments_page import body
        page = _light_wrap(title='专业职责指定', body=body(state, member_token, permitted),
            body_class=' lz-deputy-page', nav='<a href="/dispatch">选队召集</a><a href="/dispatch/task-review">任务分类登记</a><a href="/tasks/release">任务复核与发布</a>',
            user=f'<span>指定副总指挥 {esc(state.member_session(member_token).name)}</span>'
                 + _topbar_logout_form('/logout/member', '登出'))
        if state.workspace.version != version_before:
            raise RuntimeError('panel rendering mutated the workspace')
        return 200, page
    if route == "/dispatch/task-review":
        try:
            permitted = state.deputy_events(member_token)
        except PermissionError:
            permitted = []
        if not permitted:
            return 404, "<h1>404</h1>"
        page = render_task_review_page(
            state, state.member_session(member_token), permitted, member_token)
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/tasks/offline":
        try:
            actor = state.authorization_subject(member_token)
            page = render_offline_tasks_page(
                state, state.member_session(member_token),
                state.offline_task_views(member_token), actor)
        except PermissionError:
            return 404, "<h1>404</h1>"
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/temporary":
        try:
            state.authorization_subject(member_token)
        except PermissionError:
            return 404, "<h1>404</h1>"
        page = render_temporary_page(
            state, state.member_session(member_token), member_token
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/events/local-notices":
        try:
            state.duty_subject(member_token)
        except PermissionError:
            return 404, "<h1>404</h1>"
        page = render_duty_local_notice_page(
            state, state.member_session(member_token)
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/temporary/assign":
        try:
            permitted = state.deputy_events(member_token)
        except PermissionError:
            permitted = []
        requested = ((query or {}).get("event_id") or [""])[0]
        if requested:
            permitted = [requested] if requested in permitted else []
        if not permitted:
            return 404, "<h1>404</h1>"
        page = render_temporary_assign_page(
            state, state.member_session(member_token), permitted
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route in {"/materials/closeout", "/temporary/leader"}:
        with state._event_authority_lock:
            try:
                assignments = state.member_temporary_leadership(member_token)
                closeout_events = state.member_material_closeout_events(member_token)
                if route == "/materials/closeout" or not assignments:
                    if not closeout_events:
                        return 404, "<h1>404</h1>"
                    from .material_closeout_page import render
                    page = render(state, state.member_session(member_token), closeout_events,
                                  has_active_groups=bool(assignments))
                else:
                    page = render_temporary_leader_page(
                        state, state.member_session(member_token), assignments, closeout_events)
            except PermissionError:
                return 404, "<h1>404</h1>"
            except (PanelActionError, ValueError, OSError):
                return 409, '<h1>来源待核对</h1><p>事件或组长职责来源不一致。</p>'
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/personnel/groups":
        event_id = ((query or {}).get("event_id") or [""])[0]
        record = state.registry.events.get(event_id)
        if record is None or record.status not in {"active", "upgraded"}:
            return 404, "<h1>404</h1>"
        try:
            phone = state.authorization_subject(member_token)
            deputy = event_id in state.deputy_events(member_token)
            people = state.event_group_people(event_id)
        except PermissionError:
            return 404, "<h1>404</h1>"
        except (PanelActionError, ValueError) as exc:
            return 409, f"<h1>409</h1><p>{esc(exc)}</p>"
        if not deputy and phone not in people:
            return 404, "<h1>404</h1>"
        page = render_event_groups_page(
            state, event_id, viewer_phone=phone, deputy=deputy
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/callouts":
        try:
            assigned = state.captain_callouts(member_token)
        except PermissionError:
            assigned = []
        if not assigned:
            return 404, "<h1>404</h1>"
        page = render_captain_callouts_page(
            state, state.member_session(member_token), assigned
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route == "/captain-arrival":
        try:
            assigned = state.captain_callouts(member_token)
        except PermissionError:
            assigned = []
        if not assigned:
            return 404, "<h1>404</h1>"
        page = render_captain_arrival_page(
            state, state.member_session(member_token), assigned, member_token=member_token
        )
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    # High-risk consoles (drill console + fire-event management): invisible
    # without command authority — locked callers see a plain 404, matching the
    # unknown-route convention.
    duty_member = None
    if route == "/events":
        try:
            state.duty_subject(member_token)
            duty_member = state.member_session(member_token)
        except PermissionError:
            pass
    if route.startswith("/events/") and route.endswith("/details"):
        parts = route.strip('/').split('/')
        if not state.command_mode(session_token):
            try:
                state.duty_subject(member_token)
            except PermissionError:
                try:
                    if len(parts) != 3 or parts[1] not in _business.events(state, member_token):
                        return 404, '<h1>404</h1>'
                except PermissionError:
                    return 404, '<h1>404</h1>'
        page = render_fire_event_detail(state, parts[1]) if len(parts) == 3 else None
        return (200, page) if page is not None else (404, '<h1>404</h1>')
    if (route == "/drill" or route.startswith("/events/")) and not state.command_mode(session_token):
        return 404, "<h1>404</h1>"
    if route == "/events":
        if business_role:
            return 200, render_duty_events_page(state,state.member_session(member_token),query=query,
                                               event_ids=_business.events(state,member_token))
        if not state.command_mode(session_token) and duty_member is None:
            if _selected_deputy_event(state, member_token):
                return 200, render_deputy_events_page(state, member_token)
        if not state.command_mode(session_token) and duty_member is None:
            return 404, "<h1>404</h1>"
        page = (render_events_page(state, query=query)
                if state.command_mode(session_token)
                else render_duty_events_page(state, duty_member, query=query))
        if state.workspace.version != version_before:  # read-only guarantee for GET
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    if route.startswith("/events/"):
        parts = [part for part in route.split("/") if part]  # ["events", id, sub]
        page = None
        if len(parts) == 3 and parts[2] == "roster":
            page = render_roster_page(state, parts[1])
        elif len(parts) == 3 and parts[2] == "close-confirm":
            page = render_close_confirm_page(state, parts[1])
        if page is None:
            return 404, "<h1>404</h1>"
        if state.workspace.version != version_before:
            raise RuntimeError("panel rendering mutated the workspace")
        return 200, page
    # Member-only sessions see exactly two surfaces: the login/check-in page
    # and the minimal home placeholder. Everything else stays invisible (404);
    # command power never loosens through a member session.
    member_session = None if state.command_mode(session_token) else state.member_session(member_token)
    if member_session is not None:
        if route == "/":
            return 200, render_member_home(state, member_session)
        return 404, "<h1>404</h1>"
    body = _dispatch_view(state, route, session_token, query)
    if body is None:
        return 404, "<h1>404</h1>"
    projector = ((query or {}).get("projector") or [""])[0] == "1"
    page = _PAGE.format(
        title=_VIEW_TITLES.get(route, route),
        css=_CSS,
        nav=_build_nav(
            route,
            command_mode=state.command_mode(session_token),
            projector=projector,
        ),
        body_class="projector" if projector else "",
        scenario=esc(state.workspace.scenario_id),
        version=state.workspace.version,
        strip=_header_strip(state, session_token),
        body=body,
    )
    if state.workspace.version != version_before:  # read-only guarantee for GET
        raise RuntimeError("panel rendering mutated the workspace")
    return 200, page


# ---------------------------------------------------------------------------
# HTTP layer


class LoginRejected(Exception):
    pass


def handle_login(state: PanelState, form: dict[str, list[str]]) -> str | None:
    """Returns a fresh session token, or raises LoginRejected.

    P16 T2 keeps the legacy ``secret``-only semantics untouched (old clients
    and tests keep working); the optional ``username`` field must be ``admin``
    when provided — the single commander account (01 号 §2).
    """
    username = ((form.get("username") or [""])[0] or "").strip().lower()
    if username and username != "admin":
        raise LoginRejected()
    secret = (form.get("secret") or [""])[0]
    token = state.login(secret)
    if token is None:
        raise LoginRejected()
    return token


def make_handler(state: PanelState) -> type[BaseHTTPRequestHandler]:
    class PanelHandler(BaseHTTPRequestHandler):
        # 手机端一屏要拉几十张地图瓦片：HTTP/1.0 会为每张图新建一次 TCP 连接，
        # 握手开销比图片本身还大。开启长连接后同一批瓦片共用一条连接。
        protocol_version = "HTTP/1.1"
        def log_message(self, *args) -> None:  # quiet by default
            pass

        def _cookie_token(self) -> str | None:
            raw = self.headers.get("Cookie", "")
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except Exception:
                return None
            morsel = cookie.get(_SESSION_COOKIE)
            return morsel.value if morsel else None

        def _member_cookie(self) -> str | None:
            raw = self.headers.get("Cookie", "")
            cookie = SimpleCookie()
            try:
                cookie.load(raw)
            except Exception:
                return None
            morsel = cookie.get(_MEMBER_COOKIE)
            return morsel.value if morsel else None

        def _respond(self, status: int, body: str, *, headers: list[tuple[str, str]] | None = None) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            for key, value in headers or []:
                self.send_header(key, value)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if self.path.split('?', 1)[0] in {'/models', '/material-custody', '/material-supply', '/supply-draft', '/material-supply/execution','/material-supply/receipts', '/materials/closeout'}:
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _respond_json(self, payload, *, status: int = 200, close: bool = False) -> None:
            """只读 JSON 响应（态势馈送等）；不写任何数据。"""
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            if close:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)

        def _redirect(self, location: str, *, clear_cookie: bool = False, headers: list[tuple[str, str]] | None = None) -> None:
            from .captain_mobile import target
            location=target(state,self._member_cookie(),location,self.path.split('?',1)[0])
            if getattr(self, '_sand_draft_reply', False):
                if location.split('#', 1)[0] == '/sand':
                    self._respond_json({'ok': True, 'redirect': location})
                else:
                    self._respond_json({'ok': False, 'message': '登录已失效，请重新登录；草稿未提交。'}, status=401)
                return
            all_headers = list(headers or [])
            all_headers.insert(0, ("Location", location))
            if clear_cookie:
                all_headers.append(("Set-Cookie", f"{_SESSION_COOKIE}=; Max-Age=0; Path=/"))
            self.send_response(303)
            for key, value in all_headers:
                self.send_header(key, value)
            self.send_header("Content-Length", "0")
            self.end_headers()
            self.wfile.write(b"")

        def _export_report(self) -> None:
            from .review_meeting import is_owner
            if not (state.command_mode(self._cookie_token())
                    or is_owner(state, self._member_cookie())):
                self._respond(404, '<h1>404</h1>')
                return
            if state.workspace.event_id and state.workspace.event_id != state.active_event_id:
                self._respond(409, '<h1>409</h1><p>报告事件与当前工作区不一致。</p>')
                return
            version_before = state.workspace.version
            payload = export_report_markdown(state.workspace).encode("utf-8")
            if state.workspace.version != version_before:  # export stays read-only
                raise RuntimeError("panel export mutated the workspace")
            safe_id = "".join(
                ch if ch.isalnum() or ch in "-_" else "-"
                for ch in state.workspace.scenario_id
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/markdown; charset=utf-8")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{safe_id or "after-action"}-report.md"',
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _export_review(self) -> None:
            from .review_meeting import is_owner, scoped_state
            if not (state.command_mode(self._cookie_token()) or is_owner(state, self._member_cookie()) or _unified.can_read(state,self._member_cookie())):
                self._respond(404, "<h1>404</h1>")
                return
            version_before = state.workspace.version
            content_type = 'text/markdown; charset=utf-8'
            extension = 'md'
            report_suffix = ''
            try:
                _, _, raw_query = self.path.partition('?')
                params = parse_qs(raw_query, keep_blank_values=True)
                if any(len(values) != 1 for values in params.values()) or set(params)-{'event_id','report_id','format'}:
                    raise ValueError('报告导出查询字段无效。')
                if params:
                    from .review_exports import export_version, CONTENT_TYPES
                    from .review_reports import checked_source
                    if params.get('event_id') != [state.active_event_id]:
                        raise ValueError('报告事件不匹配。')
                    report = scoped_state(state).review_ledger.reports.get(params.get('report_id', [''])[0])
                    if report is None:
                        raise KeyError('所选报告不存在或不属于本事件。')
                    if report.source_snapshot_json:
                        checked_source(report, state.active_event_id)
                    extension = params.get('format', ['md'])[0]
                    if extension == 'json':
                        from .review_handoff import export_package
                        payload = export_package(report, state.active_event_id)
                        content_type = 'application/json; charset=utf-8'
                    else:
                        payload = export_version(report, extension)
                        content_type = CONTENT_TYPES[extension]
                    report_suffix = f'-v{report.version}'
                else:
                    payload = export_review_markdown(scoped_state(state)).encode("utf-8")
            except ImportError:
                self._respond(503, '<h1>503</h1><p>报告导出依赖未安装，请安装 requirements-review-exports.txt。</p>')
                return
            except ValueError as exc:
                self._respond(409, '<h1>409</h1><p>' + esc(str(exc)) + '</p>')
                return
            except (KeyError, TypeError):
                self._respond(409, '<h1>409</h1><p>所选报告或来源待核对，无法导出。</p>')
                return
            if state.workspace.version != version_before:  # 导出只读不变式
                raise RuntimeError("panel review export mutated the workspace")
            safe_id = "".join(
                ch if ch.isalnum() or ch in "-_" else "-"
                for ch in (state.active_event_id or state.workspace.scenario_id)
            )
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="review-{safe_id or "event"}{report_suffix}.{extension}"',
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _serve_offline_tile(self, rel: str) -> None:
            from .offline_http import legacy_tile
            legacy_tile(self, state, rel)

        def _serve_upload(self, rel: str) -> None:
            """原图仅指挥员可看（doc 03 §12：公共屏/成员不可见原始图像）。"""
            if not state.command_mode(self._cookie_token()):
                if not _unified.can_read(state,self._member_cookie()):
                    self._respond(404, "<h1>404</h1>"); return
                referenced = {str(image.file_path).removeprefix('uploads/') for image in state.imagery.images.values() if image.event_id == state.active_event_id}
                if rel.removeprefix('uploads/') not in referenced:
                    self._respond(404, "<h1>404</h1>"); return
            base = state.upload_dir.resolve()
            # file_path 本身以 "uploads/" 开头，与 URL 前缀重合：此处归一，
            # 避免 base/uploads/... 双层路径导致原图永远 404。
            if rel.startswith("uploads/"):
                rel = rel[len("uploads/"):]
            target = (base / rel).resolve()
            if not str(target).startswith(str(base)) or not target.is_file():
                self._respond(404, "<h1>404</h1>")
                return
            suffix = target.suffix.lower()
            ctype = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                     "png": "image/png"}.get(suffix.lstrip("."), "")
            if not ctype:
                self._respond(404, "<h1>404</h1>")
                return
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _serve_static(self, name: str) -> None:
            entry = _STATIC_WHITELIST.get(name)
            if entry is None or not entry[0].exists():
                self._respond(404, "<h1>404</h1>")
                return
            payload = entry[0].read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", entry[1])
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            self._sand_draft_reply = False
            route, _, raw_query = self.path.partition("?")
            if route == '/materials/daily/download':
                from .resource_directory_page import download
                with state._event_authority_lock:
                    return download(self,state,raw_query)
            if route == '/sand/command-data':
                from .sand_command import summary, imagery_payload
                try:
                    params = parse_qs(raw_query, keep_blank_values=True)
                    if params != {'event_id': [state.active_event_id]}:
                        raise PermissionError('当前火情已变化。')
                    data = summary(state, self._member_cookie())
                    data['images'] = imagery_payload(state, 'member')
                    self._respond_json(data)
                except PermissionError as exc:
                    self._respond_json({'error': str(exc)}, status=403)
                except ValueError as exc:
                    self._respond_json({'error': str(exc)}, status=409)
                return
            if route == '/offline' or route.startswith('/offline/'):
                from .offline_http import get
                get(self, state, route, raw_query)
                return
            if route == "/export/report":
                self._export_report()
                return
            if route == "/export/review":
                self._export_review()
                return
            if route == '/review/inquiry-source':
                from . import review_meeting as meeting, review_inquiry as inquiry
                member_token, command_token = self._member_cookie(), self._cookie_token()
                privileged = state.command_mode(command_token) or meeting.is_owner(state, member_token)
                try:
                    meeting.service(state)
                    if not (privileged or meeting.is_deputy(state, member_token)
                            or state.member_can_view_current_event(member_token)):
                        self._respond(404, '<h1>404</h1>'); return
                    params = parse_qs(raw_query, keep_blank_values=True)
                    if (set(params) != {'event_id', 'digest', 'ref'}
                            or any(len(v) != 1 for v in params.values())
                            or params['event_id'] != [state.active_event_id]):
                        raise ValueError('问询来源查询不匹配。')
                    body = inquiry.source_page(state, 'commander' if privileged else 'member',
                        inquiry.viewer_key(state, command_token, member_token), params['digest'][0], params['ref'][0])
                except (ValueError, KeyError, TypeError):
                    self._respond(409, '<h1>409</h1><p>此回答来源已不可读取，请重新问询。</p>'); return
                self._respond(200, body); return
            if route == '/review/report-source':
                from .review_meeting import is_owner, scoped_state
                from .review_reports import source_html
                if not (state.command_mode(self._cookie_token()) or is_owner(state, self._member_cookie()) or _unified.can_read(state,self._member_cookie())):
                    self._respond(404, '<h1>404</h1>')
                    return
                try:
                    params = parse_qs(raw_query, keep_blank_values=True)
                    if (any(len(values) != 1 for values in params.values())
                            or set(params)-{'event_id','report_id','ref'}
                            or params.get('event_id') != [state.active_event_id]):
                        raise ValueError('报告来源查询不匹配。')
                    report = scoped_state(state).review_ledger.reports.get(params.get('report_id', [''])[0])
                    if report is None:
                        raise KeyError('报告不存在。')
                    body = source_html(report, state.active_event_id, params.get('ref', [''])[0])
                except (ValueError, KeyError, TypeError):
                    self._respond(409, '<h1>409</h1><p>报告来源不存在或待核对；不使用当前资料回填。</p>')
                    return
                self._respond(200, body)
                return
            if route.startswith("/uploads/"):
                self._serve_upload(route[len("/uploads/"):])
                return
            if route.startswith("/offlinemap/"):
                self._serve_offline_tile(route[len("/offlinemap/"):])
                return
            if route.startswith('/sand/local-map/'):
                from .local_satellite import serve
                serve(self, state, route[len('/sand/local-map/'):], raw_query)
                return
            if route.startswith("/static/"):
                self._serve_static(route[len("/static/"):])
                return
            if route == '/supply-draft':
                try:
                    if len(raw_query) > 4096:
                        raise ValueError('查询过大。')
                    query = parse_qs(raw_query, keep_blank_values=True, encoding='utf-8',
                                     errors='strict', max_num_fields=8)
                except ValueError:
                    self._respond(409, '<h1>草案查询待核对</h1>')
                    return
            else:
                query = parse_qs(raw_query) if raw_query else None
            if route == '/workflow/sand':
                from . import sop_sand
                try:
                    self._respond_json(sop_sand.operation(state,self._member_cookie(),query))
                except PermissionError as exc:self._respond_json({'error':str(exc)},status=403)
                except (ValueError,PanelActionError) as exc:self._respond_json({'error':str(exc)},status=409)
                return
            if route == '/workflow/status':
                from . import workflow
                try:
                    member, record = workflow.context(state, self._member_cookie())
                    if query != {'event_id': [record.event_id]}:
                        self._respond_json({'error': '事件已变化，请重新进入。'}, status=404); return
                    parts = workflow.fragments(state)
                    assistance = workflow.assistant_view.current(state,record.event_id)
                    states = workflow.assistant_view.states(assistance)
                    pref = workflow.preference(member, record.event_id)
                    self._respond_json(dict(event_id=record.event_id, states=states, step=pref['step'], mode=pref['mode'],
                        phase_status=[step['phase_status'] for step in assistance['steps']],step_title=workflow.WORK_ITEMS[pref['step']][0],
                        status_labels=workflow.assistant_view.status_labels(assistance),
                        guide_html=workflow.guide_fragment(state, self._member_cookie(), parts, assistance)))
                except PermissionError as exc:
                    self._respond_json({'error': str(exc)}, status=403)
                except (ValueError, PanelActionError) as exc:
                    self._respond_json({'error': str(exc)}, status=409)
                return
            if route == '/review/replay':
                from . import review_meeting as meeting
                from .review_replay import replay, render_snapshot
                token, member_token = self._cookie_token(), self._member_cookie()
                if not (state.command_mode(token) or meeting.is_owner(state, member_token)
                        or meeting.is_deputy(state, member_token) or _unified.can_read(state, member_token)):
                    self._respond_json({'error':'无本事件复盘读取权限。'}, status=403)
                    return
                try:
                    params = query or {}
                    if any(len(v)!=1 for v in params.values()) or set(params)-{'event_id','sequence','known_at'}:
                        raise ValueError('回放查询字段无效。')
                    if params.get('event_id') != [state.active_event_id]:
                        raise ValueError('回放事件不匹配。')
                    raw_seq = params.get('sequence', [str(state.workspace.version)])[0]
                    if not raw_seq.isdecimal(): raise ValueError('回放序列无效。')
                    snapshot, marks = replay(state.workspace, state.active_event_id,
                        sequence=int(raw_seq), known_at=params.get('known_at',[''])[0])
                    self._respond_json(dict(event_id=state.active_event_id, sequence=snapshot['sequence'],
                        html=render_snapshot(snapshot), marks=[dict(id=m.id,title=m.title,
                            status=m.status.value,version=m.version,geo_points=m.geo_points,
                            geometry=m.geometry) for m in marks if m.geo_points]))
                except (ValueError, KeyError, TypeError) as exc:
                    self._respond_json({'error':str(exc)}, status=409)
                return
            if route == '/sand/live-context':
                if not (state.command_mode(self._cookie_token()) or _unified.can_read(state,self._member_cookie())):
                    self._respond_json({'error': '需要有效指挥员会话。'}, status=403)
                    return
                event_id = state.active_event_id
                if not event_id or (query or {}).get('event_id') != [event_id]:
                    self._respond_json({'error': '资料与当前事件不匹配。'}, status=404)
                    return
                from .live_context import event_context, render_group
                from .sandview import _mark_payload
                try:
                    payload, marks = event_context(state, event_id)
                    payload['group_html'] = {kind: render_group(payload, kind) for kind in payload['groups']}
                    from . import sandview as sv
                    from .sand_shell import _current_focus
                    sop_fragments = [render(state, 'commander') for render in (
                        sv.render_early_response, sv.render_situation, sv.render_force, sv.render_deployment,
                        sv.render_execution, sv.render_cleanup, sv.render_emergency, sv.render_publication,
                        sv.render_handover)]
                    payload['sop_states'] = [_current_focus(fragment) for fragment in sop_fragments]
                    from .sop_guide import guide, recommendation_html
                    payload['sop_guide_html'] = [guide(i, fragment) for i, fragment in enumerate(sop_fragments)]
                    payload['sop_recommendation_html'] = recommendation_html(payload['sop_states'])
                    payload['marks'] = [_mark_payload(m, 'commander', tip=True) for m in marks if m.geo_points]
                    for mark in payload['marks']:
                        row = next(r for r in payload['groups']['maps'] if r['id'] == mark['id'])
                        mark['linked_evidence'] = [('共同版本依据', row['text'])]
                    from .intelligence import collect, content, view, fire_projection
                    report, spatial = collect(state, event_id, context=payload, marks=marks)
                    existing_ids={m.id for m in marks}
                    for mark in spatial:
                        if mark.id not in existing_ids and mark.geo_points:
                            payload['marks'].append(_mark_payload(mark, 'commander', tip=mark.id.startswith('intel-fire-')))
                    payload['intelligence'] = dict(report=report, content_html=content(report), runtime=view(state, report))
                    payload['fingerprint'] = report['fingerprint']
                    if state.workspace.version != payload['revision']:
                        raise ValueError('资料正在变化，请稍后重读。')
                except ValueError as exc:
                    self._respond_json({'error': str(exc)}, status=409)
                    return
                self._respond_json(payload)
                return
            if route == '/sand/source-review':
                if not (state.command_mode(self._cookie_token()) or _unified.can_read(state,self._member_cookie())):
                    self._respond_json({'error': '需要有效指挥员会话。'}, status=403)
                    return
                event_id = state.active_event_id
                if (not event_id or state.workspace.event_id != event_id
                        or (query or {}).get('event_id') != [event_id]):
                    self._respond_json({'error': '资料与当前事件不匹配。'}, status=404)
                    return
                from .source_review import source_review_feed, render_content
                try:
                    payload = source_review_feed(state.workspace, event_id)
                except ValueError as exc:
                    self._respond_json({'error': str(exc)}, status=409)
                    return
                payload['content_html'] = render_content(payload)
                from .observation_context import current_context, render_content as render_context
                try:
                    context = current_context(state.workspace, event_id)
                    if context['revision'] != payload['revision']:
                        raise ValueError('资料正在变化，请稍后重读。')
                except ValueError as exc:
                    self._respond_json({'error': str(exc)}, status=409)
                    return
                payload['context_html'] = render_context(context)
                payload['context_fingerprint'] = context['fingerprint']
                self._respond_json(payload)
                return
            if route == '/sand/runtime':
                commander = state.command_mode(self._cookie_token())
                member = state.member_can_view_current_event(self._member_cookie())
                if not commander and not member:
                    self._respond_json({'error': '需要当前事件的有效会话。'}, status=403)
                    return
                host = state.simulation_host
                view = host.view() if host is not None else None
                if view is None or (query or {}).get('event_id', [''])[0] != view['event_id']:
                    self._respond_json({'error': '当前事件未启用模拟宿主。'}, status=404)
                    return
                self._respond_json(view)
                return
            if route == "/public":
                self._redirect("/review?projector=1")  # 公共屏免登录只读入口
                return
            if route == "/" and home_gate(
                state,
                session_token=self._cookie_token(),
                member_token=self._member_cookie(),
            ) != "ok":
                self._redirect("/login")  # 未登录/未入岗：不能进入首页（01 号 §9）
                return
            if route == "/materials" and not (
                state.command_mode(self._cookie_token())
                or state.member_session(self._member_cookie())
            ):
                self._redirect("/login")  # 未登录不可读人员与物资（doc 04 §10）
                return
            if route == "/sand" and not (
                state.command_mode(self._cookie_token())
                or state.member_session(self._member_cookie())
            ):
                self._redirect("/login")  # 未登录不可见沙盘（doc 03 §12）
                return
            if route == "/review" and not (
                state.command_mode(self._cookie_token())
                or state.member_session(self._member_cookie())
                or (query or {}).get("projector", [""])[0] == "1"
                # 公共屏免登录只读入口（doc 01 §9 / doc 05 §9）
            ):
                self._redirect("/login")  # 未登录不可见复盘页（doc 05 §9）
                return
            if route == "/sand/situation-feed":
                # P20 只读态势馈送：指挥员/现场人员可读；本方法不写任何数据。
                commander = state.command_mode(self._cookie_token())
                member = None if commander else state.member_session(self._member_cookie())
                if not commander and member is None:
                    self._redirect("/login")
                    return
                if (not state.active_event_id or state.workspace.event_id not in
                        {'', state.active_event_id}):
                    self._respond(404, '<h1>404</h1>')
                    return
                if member is not None and not state.member_can_view_current_event(
                        self._member_cookie()):
                    self._respond(404, '<h1>404</h1>')
                    return
                payload = state.workspace.situation_feed()
                payload['advice'] = _grounded_inquiry.current_change_review_feed(
                    state.workspace, state.active_event_id)
                if commander:
                    from .sop import force_review_cue
                    payload['force_review'] = force_review_cue(state)
                self._respond_json(payload)
                return
            status, body = render_page(
                state,
                route,
                session_token=self._cookie_token(),
                query=query,
                member_token=self._member_cookie(),
            )
            if status == 303:
                self._redirect("/")
                return
            self._respond(status, body)

        def do_POST(self) -> None:
            try:
                if self.path.split('?', 1)[0].startswith('/review/') and state.workspace.event_id:
                    with state._event_authority_lock:
                        return self._do_POST()
                return self._do_POST()
            except PermissionError:
                self._respond(403, '<h1>403</h1><p>当前职责不允许此写入，请使用具名办理入口。</p>')

        def _do_POST(self) -> None:
            route = self.path.split("?", 1)[0]
            if route in {'/materials/daily/save', '/materials/daily/preview', '/materials/daily/confirm', '/materials/planning'}:
                from .resource_pages import post
                post(self, state, route)
                return
            if route == '/events/model-settings':
                from .deputy_models import post
                post(self, state)
                return
            if route == '/sand/command-assistant':
                from .sand_command import post
                post(self, state)
                return
            if route == '/workflow/assistance-reply':
                from .http_rejection import request_length
                try:
                    length=request_length(self,16384)
                    if self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded':
                        raise ValueError('补问回复格式不正确。')
                    self.connection.settimeout(15)
                    fields=parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True,
                        encoding='utf-8',errors='strict',max_num_fields=8)
                    state.assistant_runtime.reply(self._member_cookie(),fields)
                except PermissionError as exc:
                    self._respond(403,'<h1>403</h1><p>'+esc(exc)+'</p>');return
                except (ValueError,PanelActionError,UnicodeError) as exc:
                    self._respond(409,'<h1>回复未保存</h1><p>'+esc(exc)+'</p>');return
                except OSError:
                    self._respond(503,'<h1>回复保存中断</h1><p>请保留原内容并刷新核对。</p>');return
                self._redirect('/workflow/inbox');return
            if route in {'/offline/map', '/offline/reports'}:
                from .offline_http import post
                post(self, state, route)
                return
            self._sand_draft_reply = route in FEEDBACK_ACTIONS and self.headers.get('X-Sand-Draft') == '1'
            if route == '/material-supply/receipts':
                from .material_supply_receipts_page import post
                post(self, state)
                return
            if route == '/material-supply/execution':
                from .material_supply_execution_page import post
                post(self, state)
                return
            if route == '/tasks/relations':
                from .task_relations_page import post
                post(self, state)
                return
            if route == '/tasks/formal':
                from .http_rejection import request_length
                member = state.member_session(self._member_cookie())
                if member is None:
                    self._respond(403, '<h1>403</h1>'); return
                try:
                    length = request_length(self, 8192)
                except ValueError:
                    self._respond(413, '<h1>413</h1>'); return
                try:
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                        raise ValueError('表单格式不正确。')
                    self.connection.settimeout(15)
                    form = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                        encoding='utf-8', errors='strict', max_num_fields=16)
                    keys = {'csrf', 'event_id', 'order_id', 'publication_ref', 'expected_version', 'request_id', 'action', 'basis'}
                    if set(form) != keys or any(len(value) != 1 for value in form.values()):
                        raise ValueError('字段缺失、重复或不受支持。')
                    values = {key: value[0] for key, value in form.items()}
                    if not _secrets.compare_digest(values.pop('csrf').encode(), member.custody_csrf.encode()):
                        raise PermissionError('会话已变化。')
                    raw = values['expected_version']
                    if not raw.isascii() or not raw.isdigit() or len(raw) > 9: raise ValueError('版本不正确。')
                    values['expected_version'] = int(raw)
                    state.handle_formal_task_receipt(values.pop('event_id'), member_token=self._member_cookie(), **values)
                except PermissionError:
                    self._respond(403, '<h1>403</h1>'); return
                except (ValueError, PanelActionError) as error:
                    self._respond(409, '<h1>409</h1><p>' + esc(str(error)) + '</p>'); return
                self._redirect('/tasks/formal'); return
            if route == '/materials/maintenance-authority':
                from .material_maintenance_authority_page import post
                post(self, state)
                return
            if route == '/personnel/manage':
                from .personnel_management_page import post
                post(self, state)
                return
            if route == '/materials/accounts':
                from .material_closeout_accounts_page import post
                post(self, state)
                return
            if route == '/materials/records':
                from .material_catalog_page import post
                post(self, state)
                return
            if route == '/tasks/feedback':
                from .formal_task_feedback_page import post
                post(self, state)
                return
            if route == '/tasks/release':
                from .task_release_page import post
                post(self, state)
                return
            if route == '/dispatch/professionals':
                from .http_rejection import request_length
                try:
                    member = state.member_session(self._member_cookie())
                    permitted = state.deputy_events(self._member_cookie()) if member else []
                    if member is None or not permitted:
                        raise PermissionError('需要指定副指挥长本人会话。')
                except PermissionError:
                    self._respond(403, '<h1>403</h1>'); return
                try:
                    length = request_length(self, 8192)
                except ValueError as exc:
                    self._respond(413, f'<h1>413</h1><p>{esc(exc)}</p>'); return
                try:
                    self.connection.settimeout(15)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                        raise ValueError('专业职责表单格式不正确。')
                    form = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                                    encoding='utf-8', errors='strict', max_num_fields=16)
                    allowed = {'event_id','subject_id','professional_scope','expected_version','evidence','reason','request_id','action','csrf'}
                    if set(form) != allowed or any(len(v) != 1 for v in form.values()):
                        raise ValueError('专业职责字段缺失、重复或不受支持。')
                    fields = {k: v[0] for k, v in form.items()}
                    if not _secrets.compare_digest(fields.pop('csrf').encode(), member.custody_csrf.encode()):
                        raise PermissionError('表单会话已变化。')
                    if fields['event_id'] not in permitted:
                        raise PermissionError('本事件未指定当前人员为副指挥长。')
                    action = fields.pop('action'); raw = fields['expected_version']
                    if action not in {'grant','revoke'} or not raw.isascii() or not raw.isdigit() or len(raw) > 9:
                        raise ValueError('专业职责操作或版本不正确。')
                    fields['expected_version'] = int(raw); fields['active'] = action == 'grant'
                    event_id = fields.pop('event_id')
                    state.handle_professional_duty(event_id, deputy_token=self._member_cookie(), **fields)
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>仅本事件指定副指挥长可确定专业职责。</p>'); return
                except (ValueError, PanelActionError) as exc:
                    self._respond(409, f'<h1>409</h1><p>{esc(exc)}</p>'); return
                self._redirect('/dispatch/professionals'); return
            if route == "/task-materials/report":
                from .http_rejection import request_length
                try:
                    member = state.member_session(self._member_cookie())
                    if member is None or not state.deputy_events(self._member_cookie()):
                        raise PermissionError('需要指定副总指挥会话。')
                except PermissionError:
                    self._respond(403, '<h1>403</h1>'); return
                try:
                    length = request_length(self, 8192)
                except ValueError as exc:
                    self._respond(413, f'<h1>413</h1><p>{esc(exc)}</p>'); return
                try:
                    from .task_material_inputs import FIELDS
                    self.connection.settimeout(15)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                        raise ValueError('任务输入表单格式不正确。')
                    form = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                                    encoding='utf-8', errors='strict', max_num_fields=32)
                    allowed = set(FIELDS) | {'event_id','order_id','item_id','expected_version',
                        'expected_source_digest','evidence','occurred_at','request_id','csrf'}
                    if set(form) != allowed or any(len(v) != 1 for v in form.values()):
                        raise ValueError('任务输入字段缺失、重复或不受支持。')
                    if not _secrets.compare_digest(form['csrf'][0].encode('utf-8'), member.custody_csrf.encode('utf-8')):
                        raise PermissionError('表单会话已变化，请保留输入并刷新。')
                    def field(name):
                        return (form.get(name) or [''])[0]
                    values = {name: field(name) for name in FIELDS}
                    for name in ('force_count', 'duration_hours', 'demand_quantity', 'projected_consumption'):
                        raw = values[name]
                        values[name] = (None if raw == '' else int(raw) if name == 'force_count' else float(raw))
                    raw_version = field('expected_version')
                    if not raw_version.isascii() or not raw_version.isdigit() or len(raw_version) > 9:
                        raise ValueError('报告版本不正确。')
                    state.handle_task_material_inputs(field('event_id'), member_token=self._member_cookie(),
                        order_id=field('order_id'), item_id=field('item_id'), expected_version=int(raw_version),
                        expected_source_digest=field('expected_source_digest'), evidence=field('evidence'),
                        occurred_at=field('occurred_at'), request_id=field('request_id'), **values)
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>仅本事件指定副总指挥可记录任务输入。</p>')
                    return
                except (ValueError, PanelActionError) as exc:
                    self._respond(409, f'<h1>409</h1><p>{esc(exc)}</p>')
                    return
                except (OSError, sqlite3.Error, RuntimeError):
                    self._respond(503, '<h1>保存中断</h1><p>保留原请求原样重试。</p>')
                    return
                from urllib.parse import urlencode
                self._redirect('/task-materials?' + urlencode({name: field(name) for name in ('event_id','order_id','item_id')}))
                return
            if route == '/captain-arrival/material-withdrawal':
                from .http_rejection import request_length, reject_json
                token = self._member_cookie()
                try:
                    state.authorization_subject(token)
                    member = state.member_session(token)
                except PermissionError:
                    reject_json(self, {'message': '需要现任队长本人会话。'}, status=403)
                    return
                try:
                    length = request_length(self, 8192)
                except ValueError as exc:
                    reject_json(self, {'message': str(exc)}, status=413)
                    return
                try:
                    self.connection.settimeout(15)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                        raise ValueError('撤离表单格式不正确。')
                    form = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                                    encoding='utf-8', errors='strict', max_num_fields=2000)
                    common = {'event_id', 'squad_id', 'csrf', 'evidence', 'request_id'}
                    repeated = {'exception_item_id', 'exception_kind', 'exception_quantity',
                                'exception_location', 'exception_custodian', 'loan_transfer_id',
                                'loan_quantity', 'loan_location', 'loan_custodian'}
                    if (not common <= set(form) or set(form) - common - repeated - {'occurred_at'}
                            or any(len(form[key]) != 1 for key in common | ({'occurred_at'} & set(form)))):
                        raise ValueError('撤离字段缺失、重复或不受支持。')
                    if not _secrets.compare_digest(form['csrf'][0].encode('utf-8'), member.custody_csrf.encode('utf-8')):
                        raise PermissionError('表单会话已变化，请保留输入并核对。')
                    with state._event_authority_lock:
                        state.handle_team_material_withdrawal(form['event_id'][0], captain_token=token,
                            squad_id=form['squad_id'][0], exceptions=_withdrawal_exceptions_from_form(form),
                            borrowed_custody=_withdrawal_borrowed_custody_from_form(form),
                            evidence=form['evidence'][0], request_id=form['request_id'][0],
                            occurred_at=(form.get('occurred_at') or [''])[0])
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>仅本事件现任队长及有效本人表单可报告本队撤离物资。</p>')
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f'<h1>409</h1><p>{esc(exc)}</p>')
                    return
                except (OSError, sqlite3.Error, RuntimeError):
                    self._respond(503, '<h1>保存中断</h1><p>结果待核对，请保留原输入和请求号并原样重试。</p>')
                    return
                self._redirect('/captain-arrival')
                return
            if route == '/supply-draft/save':
                from .http_rejection import request_length, reject_json
                token = self._cookie_token()
                with state._event_authority_lock:
                    try:
                        _material_supply_draft_page.require_page(state, token, state.active_event_id, for_write=True)
                    except (PermissionError, PanelActionError):
                        reject_json(self, {'ok': False, 'message': '需要当前可写事件的有效指挥编制会话。'}, status=403)
                        return
                    except ValueError:
                        reject_json(self, {'ok': False, 'message': '事件来源待核对。'}, status=409)
                        return
                    try:
                        length = request_length(self, 32768)
                    except ValueError as exc:
                        reject_json(self, {'ok': False, 'message': str(exc)}, status=413)
                        return
                    try:
                        self.connection.settimeout(15)
                        raw = self.rfile.read(length)
                        if len(raw) != length or self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                            raise ValueError('附件表单格式或内容长度不正确。')
                        fields = parse_qs(raw.decode('utf-8'), keep_blank_values=True,
                                          encoding='utf-8', errors='strict', max_num_fields=45)
                        result = _material_supply_draft_page.submit(state, token, fields)
                        value = lambda key: fields[key][0]
                        html = _material_supply_draft_page.main(state, token, value('event_id'),
                            value('decision_id'), value('task_id'), value('item_id'))
                    except PermissionError as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=403); return
                    except (PanelActionError, ValueError) as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=409); return
                    except (OSError, sqlite3.Error, RuntimeError):
                        self._respond_json({'ok': False, 'message': '保存中断，结果待核对；请保留原输入和请求号原样重试。'}, status=503); return
                    self._respond_json({'ok': True, 'event_id': value('event_id'),
                        'decision_id': value('decision_id'), 'task_id': value('task_id'), 'item_id': value('item_id'),
                        'request_id': result.request_id, 'record_id': result.id, 'main_html': html})
                    return
            if route == '/material-supply/report':
                from .http_rejection import request_length, reject_json
                member_token = self._member_cookie()
                with state._event_authority_lock:
                    try:
                        member = _material_supply_page.require_page(
                            state, member_token, state.active_event_id, for_write=True)
                    except (PermissionError, PanelActionError, ValueError):
                        reject_json(self, {'ok': False, 'message': '需要当前可写事件物资供应组现任组长本人会话。'}, status=403)
                        return
                    try:
                        length = request_length(self, 8192)
                    except ValueError as exc:
                        reject_json(self, {'ok': False, 'message': str(exc)}, status=413)
                        return
                    try:
                        self.connection.settimeout(15)
                        if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                            raise ValueError('联系表单格式不正确。')
                        fields = parse_qs(self.rfile.read(length).decode('utf-8'), keep_blank_values=True,
                                          encoding='utf-8', errors='strict', max_num_fields=20)
                        result = _material_supply_page.submit(state, member_token, fields, member)
                        value = lambda key: fields[key][0]
                        html = _material_supply_page.card(state, member_token, value('event_id'),
                            value('item_id'), value('source_kind'), value('source_id'))
                    except PermissionError as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=403); return
                    except (PanelActionError, ValueError) as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=409); return
                    except (OSError, sqlite3.Error, RuntimeError):
                        self._respond_json({'ok': False, 'message': '保存中断，结果待核对，请保留原输入和请求号并重试。'}, status=503); return
                    self._respond_json({'ok': True, 'event_id': value('event_id'), 'item_id': value('item_id'),
                        'source_kind': value('source_kind'), 'source_id': value('source_id'),
                        'request_id': result.request_id, 'record_id': result.id, 'card_html': html})
                    return
            if route in {'/material-custody/report', '/material-custody/verify', '/material-custody/settle', '/material-custody/correct', '/material-custody/reverify', '/material-custody/invalidate'}:
                from .http_rejection import request_length, reject_json
                member_token = self._member_cookie()
                with state._event_authority_lock:
                    try:
                        state.authorization_subject(member_token)
                        member = state.member_session(member_token)
                    except (PermissionError, PanelActionError, ValueError):
                        reject_json(self, {'ok': False, 'message': '需要本事件物资供应组现任组长本人会话。'}, status=403)
                        return
                    try:
                        length = request_length(self, 8192)
                    except ValueError as exc:
                        reject_json(self, {'ok': False, 'message': str(exc)}, status=413)
                        return
                    try:
                        self.connection.settimeout(15)
                        if self.headers.get('Content-Type', '').split(';')[0] != 'application/x-www-form-urlencoded':
                            reject_json(self, {'ok': False, 'message': '接收核验结清表单格式不正确。'}, status=409)
                            return
                        fields = parse_qs(self.rfile.read(length).decode('utf-8'),
                                          keep_blank_values=True, encoding='utf-8', errors='strict')
                        result, message = _material_custody_page.submit(state, member_token, route, fields, member)
                        value = lambda key: fields[key][0]
                        html = _material_custody_page.card(state, member_token, value('event_id'), value('loan_transfer_id'))
                    except PermissionError as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=403); return
                    except (PanelActionError, ValueError) as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=409); return
                    except (OSError, sqlite3.Error, RuntimeError):
                        self._respond_json({'ok': False, 'message': '保存中断，结果待核对，请保留原请求并重试。'}, status=503); return
                    self._respond_json({'ok': True, 'event_id': value('event_id'),
                        'loan_transfer_id': value('loan_transfer_id'), 'record_id': result.id, 'card_html': html,
                        'operation': route.rsplit('/', 1)[-1], 'message': message})
                    return
            if route in {'/review/word-preview', '/review/word-import'}:
                from . import review_meeting as meeting, review_word as word
                from .http_rejection import request_length, reject_json
                member_token = self._member_cookie()
                if not state.workspace.event_id or not meeting.is_owner(state, member_token):
                    reject_json(self, {'ok': False, 'message': '仅本事件复盘负责人本人可回收 Word。'}, status=403)
                    return
                try:
                    length = request_length(self, word.MAX_REQUEST_BYTES if route.endswith('preview') else 4096)
                except ValueError as exc:
                    reject_json(self, {'ok': False, 'message': str(exc)}, status=413)
                    return
                try:
                    self.connection.settimeout(15)
                    raw_word = self.rfile.read(length)
                    if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                        raise ValueError('Word 回收请求格式不正确。')
                    fields = word.json_fields(raw_word)
                    expected = {'event_id', 'csrf', 'expected_version'} | (
                        {'report_id', 'file_b64'} if route.endswith('preview') else {'preview_id', 'confirm'})
                    if set(fields) != expected:
                        raise ValueError('Word 回收字段不匹配。')
                    checked = {k: [v] for k, v in fields.items()}
                    if route.endswith('import'):
                        checked['expected_version'] = [str(state.workspace.version)]
                    meeting.validate_form(state, member_token, checked)
                    if route.endswith('preview'):
                        payload = word.decode_file(fields['file_b64'])
                        result = word.preview(state, member_token, fields['report_id'], fields['expected_version'], payload)
                    else:
                        if fields['confirm'] != '1':
                            raise ValueError('请先预览并明确确认回收。')
                        # An identical completed preview can be retried after the version advanced.
                        rid = word.confirm(state, member_token, fields['preview_id'], fields['expected_version'])
                        result = {'report_id': rid}
                except PermissionError as exc:
                    self._respond_json({'ok': False, 'message': str(exc)}, status=403); return
                except ImportError:
                    self._respond_json({'ok': False, 'message': 'Word 回收依赖不可用，请安装报告导出依赖。'}, status=503); return
                except (OSError, sqlite3.Error, RuntimeError) as exc:
                    self._respond_json({'ok': False, 'message': '回收未完成，请核对底账后重试：'+str(exc)}, status=503); return
                except (ValueError, KeyError, TypeError) as exc:
                    self._respond_json({'ok': False, 'message': str(exc)}, status=409); return
                self._respond_json({'ok': True, **result}); return
            if route in {'/sand/runtime', '/sand/channel'}:
                from .http_rejection import request_length, reject_json
                try:
                    length = request_length(self, 4096 if route == '/sand/runtime' else 32768)
                except ValueError:
                    reject_json(self, {'error': '模拟控制请求过大、为空或长度格式不正确。'}, status=413)
                    return
            else:
                length = int(self.headers.get("Content-Length") or 0)
            raw_bytes = self.rfile.read(length) if length else b""
            # 文本表单按 UTF-8 解（urlencoded 体里中文已百分号编码，安全）；
            # 二进制上传（multipart）走 raw_bytes，不经过文本解码。
            raw = raw_bytes.decode("utf-8", "replace")
            form = parse_qs(raw, keep_blank_values=route in {
                '/workflow/mode', '/workflow/records', '/tasks/recommendation',
                '/sand/task-draft', '/captain-arrival/material-withdrawal', '/models'})
            token = self._cookie_token()

            if route in {'/sand/runtime', '/sand/channel'}:
                from .simulation_panel import SimulationConflict
                try:
                    if state.simulation_host is None:
                        raise PermissionError('当前面板未启用模拟宿主。')
                    if any(len(values) != 1 for values in form.values()):
                        raise ValueError('模拟控制字段不能重复。')
                    method = (state.simulation_host.control if route == '/sand/runtime'
                              else state.simulation_host.receive_channel_packet)
                    result = method({k: v[0] for k, v in form.items()}, session_token=token)
                except PermissionError as exc:
                    self._respond_json({'error': str(exc)}, status=403)
                except SimulationConflict as exc:
                    self._respond_json({'error': str(exc)}, status=409)
                except ValueError as exc:
                    self._respond_json({'error': str(exc)}, status=422)
                else:
                    self._respond_json(result)
                return

            if route == '/models':
                try:
                    state.handle_model_settings({k: v[0] for k, v in form.items()}, session_token=token)
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>模型设置需要有效指挥员会话和当前表单。</p>')
                    return
                except PanelActionError as exc:
                    self._respond(422, render_model_settings_page(state, token, error=str(exc)))
                    return
                self._redirect('/models')
                return

            if route == "/login" or (route == "/login/unified" and
                                      ((form.get("account") or [""])[0]).strip().lower() == "admin"):
                unified = route == "/login/unified"
                credentials = ({"username": ["admin"], "secret": form.get("password", [""])}
                               if unified else form)
                try:
                    new_token = handle_login(state, credentials)
                except LoginRejected:
                    self._respond(401, render_login_page(state, commander_error=True,
                                  login_account=(form.get("account") or [""])[0] if unified else "admin"))
                    return
                login_headers = [("Set-Cookie", f"{_SESSION_COOKIE}={new_token}; Path=/; HttpOnly")]
                if unified:
                    state.member_logout(self._member_cookie())
                    login_headers.append(("Set-Cookie", f"{_MEMBER_COOKIE}=; Max-Age=0; Path=/; HttpOnly"))
                self._redirect(
                    "/events",   # doc 01 §7：指挥员登录成功 → 火情管理页
                    headers=login_headers,
                )
                return

            if route == "/logout":
                state.logout(token)
                self._redirect("/", clear_cookie=True)
                return

            # -- P16 T2: member auth + check-in --------------------------------

            if route in {"/login/member", "/login/unified"}:
                unified = route == "/login/unified"
                account = (form.get("account" if unified else "phone") or [""])[0].strip()
                member_token, member_error = state.member_login(
                    account, (form.get("password") or [""])[0]
                )
                if member_token is None:
                    self._respond(401, render_login_page(state, member_error=member_error,
                                                        login_account=account))
                    return
                # 五页口径首屏（DEVELOPMENT_PLAN_PAGES_V1 §4.1.C）：无登记权限
                # 成员登录后直接进人员架构页「/」；未刷码者由首页门禁送回登录页。
                try:
                    state.duty_subject(member_token)
                    landing = "/events"
                except PermissionError:
                    business_event = _business.selected_event(state, member_token)
                    if business_event:
                        state.member_session(member_token).selected_event_id = business_event
                    landing = ("/events" if _business.role(state, member_token)
                               else "/callouts" if state.captain_callouts(member_token)
                               else "/tasks/release" if state.task_release_views(member_token)
                               else "/materials/closeout" if state.member_material_closeout_events(member_token)
                               else "/materials/records" if state.member_material_maintenance(member_token)
                               else "/")
                from .review_meeting import is_owner
                if _business.role(state, member_token):
                    state.member_session(member_token).selected_event_id = _business.selected_event(state, member_token)
                if _business.role(state,member_token)=='captain':
                    landing='/mobile'
                if landing != '/events' and state.workspace.event_id and is_owner(state, member_token):
                    landing = "/review"
                login_headers = [("Set-Cookie", f"{_MEMBER_COOKIE}={member_token}; Path=/; HttpOnly")]
                if unified:
                    state.logout(token)
                    login_headers.append(("Set-Cookie", f"{_SESSION_COOKIE}=; Max-Age=0; Path=/; HttpOnly"))
                self._redirect(
                    landing,
                    headers=login_headers,
                )
                return

            if route == "/logout/member":
                state.member_logout(self._member_cookie())
                self._redirect(
                    "/login",
                    headers=[("Set-Cookie", f"{_MEMBER_COOKIE}=; Max-Age=0; Path=/")],
                )
                return

            if route in {"/checkin/scan", "/checkin/confirm"}:
                mtoken = self._member_cookie()
                if state.member_session(mtoken) is None:
                    self._redirect("/login")  # fail-closed: no session, no scan
                    return
                qr_value = (form.get("qr") or [""])[0]
                if route == "/checkin/scan":
                    outcome = state.member_scan(mtoken, qr_value)
                    if outcome is None:
                        self._redirect("/login")
                    elif outcome.kind == OUTCOME_CONFIRM:
                        self._respond(
                            200,
                            render_login_page(
                                state, member_token=mtoken,
                                confirm_outcome=outcome, scanned_qr=qr_value,
                            ),
                        )
                    elif outcome.kind == OUTCOME_IDEMPOTENT:
                        self._redirect("/login?back=space")
                    else:
                        # 错误文案来自领域层映射表，不暴露内部细节。
                        self._respond(
                            200,
                            render_login_page(state, member_token=mtoken, scan_alert=outcome.message),
                        )
                    return
                try:
                    result = state.member_confirm(mtoken, qr_value)
                except PanelActionError as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                except PermissionError:
                    self._redirect("/login")
                    return
                if result["kind"] == "onboarded":
                    self._redirect("/")  # 入岗成功 → 该事件首页（成员占位视图）
                elif result["kind"] == "idempotent":
                    self._redirect("/login?back=space")
                else:
                    self._respond(
                        200,
                        render_login_page(state, member_token=mtoken, scan_alert=result["message"]),
                    )
                return

            # -- P03-04: named deputy selects squads; named captain replies ----
            if route == "/tasks/offline":
                try:
                    def task_field(name):
                        values = form.get(name)
                        if values is None or len(values) != 1:
                            raise PanelActionError('线下任务表单不完整，请刷新后重试。')
                        return values[0]

                    def optional_field(name):
                        values = form.get(name, [''])
                        if len(values) != 1:
                            raise PanelActionError('线下任务表单字段重复。')
                        return values[0]

                    action = task_field('action')
                    event_id = task_field('event_id')
                    request_id = task_field('request_id')
                    if action == 'record':
                        state.handle_offline_task_record(
                            event_id, deputy_token=self._member_cookie(),
                            content=task_field('content'),
                            group_id=optional_field('group_id'),
                            issued_by=task_field('issued_by'),
                            recipient_id=task_field('recipient_id'),
                            verifier_id=optional_field('verifier_id'),
                            occurred_at=task_field('occurred_at'),
                            evidence=task_field('evidence'),
                            known_progress=optional_field('known_progress'),
                            parent_id=optional_field('parent_id'),
                            request_id=request_id,
                            due_at=optional_field('due_at'))
                    elif action in {'amend', 'control', 'feedback', 'close'}:
                        raw_version = task_field('expected_version')
                        if (not raw_version.isascii() or not raw_version.isdigit()
                                or len(raw_version) > 9):
                            raise PanelActionError('任务版本不正确，请刷新后重试。')
                        task_id = task_field('task_id')
                        version = int(raw_version)
                        if action == 'amend':
                            state.handle_offline_task_amend(
                                event_id, deputy_token=self._member_cookie(),
                                task_id=task_id, expected_version=version,
                                content=task_field('content'),
                                group_id=optional_field('group_id'),
                                recipient_id=task_field('recipient_id'),
                                verifier_id=optional_field('verifier_id'),
                                occurred_at=task_field('occurred_at'),
                                reason=task_field('reason'), request_id=request_id,
                                due_at=optional_field('due_at'))
                        elif action == 'control':
                            state.handle_offline_task_control(
                                event_id, deputy_token=self._member_cookie(),
                                task_id=task_id, expected_version=version,
                                action=task_field('control_action'),
                                occurred_at=task_field('occurred_at'),
                                reason=task_field('reason'),
                                successor_id=optional_field('successor_id'),
                                request_id=request_id)
                        elif action == 'feedback':
                            state.handle_offline_task_feedback(
                                event_id, member_token=self._member_cookie(),
                                task_id=task_id, expected_version=version,
                                action=task_field('feedback_action'),
                                basis=task_field('basis'), request_id=request_id)
                        else:
                            state.handle_offline_task_close(
                                event_id, member_token=self._member_cookie(),
                                task_id=task_id, expected_version=version,
                                evidence=task_field('evidence'),
                                overall_goal_evidence=optional_field('overall_goal_evidence'),
                                request_id=request_id)
                    else:
                        raise PanelActionError('未知线下任务操作。')
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>当前身份无权操作本事件任务。</p>')
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect('/tasks/offline')
                return
            if route == "/dispatch/task-review":
                try:
                    def review_version(name):
                        raw_version = (form.get(name) or [''])[0]
                        if (not raw_version.isascii() or not raw_version.isdigit()
                                or len(raw_version) > 9):
                            raise PanelActionError('任务或分类版本不正确，请刷新后重试。')
                        return int(raw_version)

                    state.handle_task_review_assessment(
                        (form.get('event_id') or [''])[0],
                        deputy_token=self._member_cookie(),
                        decision_id=(form.get('decision_id') or [''])[0],
                        task_id=(form.get('task_id') or [''])[0],
                        expected_draft_version=review_version('expected_draft_version'),
                        expected_review_version=review_version('expected_review_version'),
                        category=(form.get('category') or [''])[0],
                        reason=(form.get('reason') or [''])[0].strip(),
                        basis_ref=(form.get('basis_ref') or [''])[0].strip(),
                        request_id=(form.get('request_id') or [''])[0],
                    )
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>')
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect('/dispatch/task-review')
                return
            if route == "/dispatch/select":
                try:
                    state.handle_select_squads(
                        (form.get("event_id") or [""])[0],
                        deputy_token=self._member_cookie(),
                        squad_ids=form.get("squad_id") or [],
                        channel=(form.get("channel") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/dispatch")
                return
            if route == "/dispatch/simulate-failure":
                try:
                    state.handle_callout_simulation_failure(
                        (form.get("event_id") or [""])[0],
                        deputy_token=self._member_cookie(),
                        squad_id=(form.get("squad_id") or [""])[0],
                        reason=(form.get("reason") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/dispatch")
                return
            if route == "/callouts/reply":
                try:
                    state.handle_captain_reply(
                        (form.get("event_id") or [""])[0],
                        captain_token=self._member_cookie(),
                        squad_id=(form.get("squad_id") or [""])[0],
                        can_depart=(form.get("can_depart") or [""])[0],
                        expected_people=(form.get("expected_people") or [""])[0],
                        eta=(form.get("eta") or [""])[0],
                        reason=(form.get("reason") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要所选中队队长本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/callouts")
                return

            if route == "/captain-arrival/scan":
                try:
                    state.handle_captain_scan(
                        (form.get("event_id") or [""])[0],
                        captain_token=self._member_cookie(),
                        squad_id=(form.get("squad_id") or [""])[0],
                        qr_id=(form.get("qr_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>仅本事件已选中队伍的队长本人可扫码。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/captain-arrival")
                return
            if route == "/captain-arrival/report":
                try:
                    state.handle_captain_arrival(
                        (form.get("event_id") or [""])[0],
                        captain_token=self._member_cookie(),
                        squad_id=(form.get("squad_id") or [""])[0],
                        mode=(form.get("mode") or [""])[0],
                        phones=form.get("phone") or [],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>仅本事件已选中队伍的队长本人可确认到场。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/captain-arrival")
                return

            if route == "/captain-arrival/readiness":
                try:
                    state.handle_captain_readiness(
                        (form.get("event_id") or [""])[0],
                        captain_token=self._member_cookie(),
                        squad_id=(form.get("squad_id") or [""])[0],
                        phones=form.get("phone") or [],
                        status=(form.get("status") or [""])[0],
                        basis=(form.get("basis") or [""])[0],
                        order_id=(form.get("order_id") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>仅本事件已选中队伍的队长本人可报告队内状态。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/captain-arrival")
                return

            if route == "/captain-arrival/material-loss":
                try:
                    state.handle_manual_material_loss(
                        (form.get("event_id") or [""])[0],
                        captain_token=self._member_cookie(),
                        squad_id=(form.get("squad_id") or [""])[0],
                        item_id=(form.get("item_id") or [""])[0],
                        loss_id=(form.get("loss_id") or [""])[0],
                        kind=(form.get("kind") or [""])[0],
                        amount=(form.get("amount") or [""])[0],
                        expected_version=(form.get("expected_version") or [""])[0],
                        evidence=(form.get("evidence") or [""])[0],
                        occurred_at=(form.get("occurred_at") or [""])[0],
                        reason=(form.get("reason") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>仅本事件已选中队伍的现任队长可报告本队物资。</p>')
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                except OSError:
                    self._respond(503, '<h1>保存中断</h1><p>请返回原表单并原样重试，勿新增报告；持续失败请联系管理员核对底账与审计。</p>')
                    return
                self._redirect("/captain-arrival")
                return

            if route == "/material-transfers/report":
                try:
                    state.handle_manual_material_transfer(
                        (form.get("event_id") or [""])[0],
                        member_token=self._member_cookie(),
                        stage=(form.get("stage") or [""])[0],
                        transfer_id=(form.get("transfer_id") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                        evidence=(form.get("evidence") or [""])[0],
                        item_id=(form.get("item_id") or [""])[0],
                        from_squad=(form.get("from_squad") or [""])[0],
                        to_squad=(form.get("to_squad") or [""])[0],
                        quantity=(form.get("quantity") or [""])[0],
                        expected_version=(form.get("expected_version") or [""])[0],
                        occurred_at=(form.get("occurred_at") or [""])[0],
                        purpose=(form.get("purpose") or ["transfer"])[0],
                        return_for=(form.get("return_for") or [""])[0],
                        damaged_quantity=(form.get("damaged_quantity") or ["0"])[0],
                    )
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>需要本事件指定副总指挥或相应队长本人会话。</p>')
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f'<h1>409</h1><p>{esc(exc)}</p>')
                    return
                except OSError:
                    self._respond(503, '<h1>保存中断</h1><p>请用原表单原样重试；持续失败请核对物资底账与事件审计。</p>')
                    return
                self._redirect('/material-transfers')
                return

            if route == "/temporary/scan":
                try:
                    state.handle_temporary_scan(
                        (form.get("event_id") or [""])[0],
                        member_token=self._member_cookie(),
                        qr_id=(form.get("qr_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本人有效人员会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/temporary")
                return
            if route == "/temporary/arrive":
                try:
                    state.handle_temporary_arrival(
                        (form.get("event_id") or [""])[0],
                        member_token=self._member_cookie(),
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本人有效人员会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/temporary")
                return
            if route == "/temporary/assign":
                try:
                    state.handle_temporary_assign(
                        (form.get("event_id") or [""])[0],
                        deputy_token=self._member_cookie(),
                        phones=form.get("phone") or [],
                        pos_code=(form.get("pos_code") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/temporary/assign")
                return
            if route == "/temporary/leader/appoint":
                try:
                    state.handle_temporary_group_leader(
                        (form.get("event_id") or [""])[0],
                        deputy_token=self._member_cookie(),
                        pos_code=(form.get("pos_code") or [""])[0],
                        phone=(form.get("phone") or [""])[0],
                        evidence=(form.get("evidence") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/temporary/assign")
                return
            if route == "/personnel/force-needs/review":
                event_id = (form.get("event_id") or [""])[0]
                try:
                    state.handle_force_needs_review(
                        event_id, deputy_token=self._member_cookie(),
                        note=(form.get("note") or [""])[0],
                        expected_fingerprint=(form.get("expected_fingerprint") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect(f"/personnel/groups?event_id={quote(event_id, safe='')}")
                return
            if route == "/temporary/leader/readiness":
                try:
                    state.handle_temporary_group_readiness(
                        (form.get("event_id") or [""])[0],
                        leader_token=self._member_cookie(),
                        pos_code=(form.get("pos_code") or [""])[0],
                        phones=form.get("phone") or [],
                        status=(form.get("status") or [""])[0],
                        basis=(form.get("basis") or [""])[0],
                        order_id=(form.get("order_id") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件当前组长本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/temporary/leader")
                return
            if route == "/personnel/transfer":
                try:
                    state.handle_person_group_transfer(
                        (form.get("event_id") or [""])[0],
                        deputy_token=self._member_cookie(),
                        phone=(form.get("phone") or [""])[0],
                        to_pos_code=(form.get("to_pos_code") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要本事件指定副总指挥本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/temporary/assign")
                return

            if route == "/events/local-notices/prepare":
                try:
                    state.handle_prepare_local_notice(
                        (form.get("event_id") or [""])[0],
                        duty_token=self._member_cookie(),
                        phone=(form.get("phone") or [""])[0],
                        local_role=(form.get("local_role") or [""])[0],
                        channel=(form.get("channel") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要已授权值班人员本人会话。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                    return
                self._redirect("/events/local-notices")
                return

            # -- P03-03: named duty records an existing offline appointment ----
            if route in {'/events/alarm/simulate','/events/deputy-select'}:
                member=state.member_session(self._member_cookie())
                expected={'csrf','request_id'} | ({'event_id','subject_id'} if route=='/events/deputy-select' else set())
                try:
                    state.duty_subject(self._member_cookie())
                    if (set(form)!=expected or any(len(v)!=1 for v in form.values())
                            or member is None or form.get('csrf')!=[member.custody_csrf]):
                        raise PermissionError('当前值班会话校验失败。')
                    if route=='/events/alarm/simulate':
                        result=state.handle_duty_demo(duty_token=self._member_cookie(),request_id=form['request_id'][0])
                        self._redirect('/events?alarm_event='+quote(result['event_id'],safe='')); return
                    result=state.handle_duty_deputy_selection(form['event_id'][0],duty_token=self._member_cookie(),
                        subject_id=form['subject_id'][0],request_id=form['request_id'][0])
                    self._redirect('/events?completed='+quote(result['event_id'],safe='')); return
                except PermissionError:
                    self._respond(403,'<h1>403</h1><p>需要有效值班人员本人会话。</p>'); return
                except (PanelActionError,ValueError,KeyError) as exc:
                    self._respond(409,'<h1>409</h1><p>'+esc(str(exc))+'</p>'); return
                except OSError:
                    self._respond(503,'<h1>保存失败</h1><p>未完成本次办理，请保留输入后重试。</p>'); return

            if route == "/events/appointment":
                try:
                    state.duty_subject(self._member_cookie())
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>需要已授权值班人员本人会话。</p>")
                    return
                try:
                    state.handle_deputy_appointment(
                        (form.get("event_id") or [""])[0],
                        duty_token=self._member_cookie(),
                        subject_id=(form.get("subject_id") or [""])[0],
                        evidence=(form.get("evidence") or [""])[0],
                        request_id=(form.get("request_id") or [""])[0],
                        occurred_at=(form.get("occurred_at") or [""])[0],
                    )
                except PermissionError:
                    self._respond(403, "<h1>403</h1><p>无权登记本事件任命。</p>")
                    return
                except (PanelActionError, ValueError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/events")
                return

            if route == '/tasks/recommendation':
                from .deployment_recommendations import post
                post(self, state, form)
                return

            if route == '/workflow/records':
                from . import nine_flow_view
                try:
                    with state._event_authority_lock, _deputy_event_context(state,route,member_token=self._member_cookie()):
                        target=nine_flow_view.post(state,self._member_cookie(),form)
                except PermissionError as exc:
                    self._respond(403,'<h1>无权办理</h1><p>'+esc(exc)+'</p><a href="/workflow/guide">返回九步走</a>'); return
                except (ValueError,PanelActionError) as exc:
                    raw_step=(form.get('step') or ['0'])[0]
                    step=int(raw_step) if raw_step in {str(i) for i in range(9)} else 0
                    with _deputy_event_context(state,route,member_token=self._member_cookie()):
                        body=nine_flow_view.page(state,self._member_cookie(),step,str(exc))
                    self._respond(409,_light_wrap(title='办理待核对',body=body,nav=_light_nav(_COMMANDER_LIGHT_NAV,''),user='')); return
                self._redirect(target); return

            if route == '/workflow/assign-gap':
                from . import workflow as _wf
                from .persistence import save_workspace
                member, record = _wf.context(state, self._member_cookie())
                text = (form.get('gap_text') or [''])[0].strip()
                owner = (form.get('owner') or [''])[0].strip()
                due = (form.get('due') or [''])[0].strip()
                csrf = (form.get('csrf') or [''])[0]
                request_id = (form.get('request_id') or [''])[0].strip()
                if csrf != member.custody_csrf or not text or not request_id or owner not in state.registry.library:
                    raise PanelActionError('缺口指派字段无效。')
                if record.event_id not in state.deputy_events(self._member_cookie()):
                    raise PermissionError('只有本事件副指挥长可以派单。')
                with self._event_authority_lock:
                    space, target, active = self._appointment_space(record.event_id, for_write=True)
                    space.record_gap_assignment(gap_text=text, owner_phone=owner, due_at=due,
                                                assigned_by=member.phone, request_id=request_id)
                    save_workspace(space, target)
                self._redirect('/workflow/guide?step=2'); return

            if route == '/workflow/mode':
                from . import workflow
                try:
                    target = workflow.select(state, self._member_cookie(), form)
                except PermissionError as exc:
                    self._respond_json({'ok': False, 'message': str(exc)}, status=403); return
                except (ValueError, PanelActionError) as exc:
                    self._respond_json({'ok': False, 'message': str(exc)}, status=409); return
                if self.headers.get('X-Workflow-Mode') == '1':
                    self._respond_json(dict(ok=True, bar=workflow.bar(state, self._member_cookie())))
                else:
                    self._redirect(target)
                return

            if route == '/events/select':
                member = state.member_session(self._member_cookie())
                try:
                    event_id = (form.get('event_id') or [''])[0]
                    if (member is None or set(form) != {'event_id', 'csrf'}
                            or any(len(values) != 1 for values in form.values())
                            or form.get('csrf') != [member.custody_csrf]
                            or event_id not in _business.events(state, self._member_cookie())):
                        raise PermissionError('只能进入本人职责范围内的事件。')
                    member.selected_event_id = event_id
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>只能进入本人职责范围内的事件。</p>')
                    return
                self._redirect('/workflow')
                return

            # -- P16 T2: fire-event management; P03-02 duty start only -------

            is_event_route = route == "/events/create" or route.startswith("/events/")
            if is_event_route:
                action_name = (form.get("action") or [""])[0]
                event_id_form = (form.get("event_id") or [""])[0]
                duty_start = route == "/events/action" and action_name == "start"
                if duty_start:
                    try:
                        state.require_duty_start(self._member_cookie(), event_id_form)
                        member=state.member_session(self._member_cookie())
                        if (member is None or form.get('csrf')!=[member.custody_csrf]
                                or any(len(v)!=1 for v in form.values())):
                            raise PermissionError('当前值班会话校验失败。')
                    except PermissionError:
                        self._respond(403, "<h1>403</h1><p>需要已授权值班人员本人会话。</p>")
                        return
                elif not state.command_mode(token):
                    self._respond(403, "<h1>403</h1><p>指挥动作需要指挥员入口认证。</p>")
                    return
                parts = [part for part in route.split("/") if part]
                try:
                    if route in {"/events/create", "/events/alarm/action"}:
                        self._respond(410, '<h1>入口已停用</h1><p>已确认火情由上游推送后自动建事件，无需人工登记或再次审核。</p>')
                        return
                    if route == "/events/action":
                        action = (form.get("action") or [""])[0]
                        result = state.handle_event_action(
                            (form.get("event_id") or [""])[0],
                            action,
                            qr_id=(form.get("qr_id") or [""])[0],
                            duty_token=self._member_cookie(),
                            defer_initial_qr=action=="start",
                        )
                        if action == "start":
                            self._redirect("/events?started="+quote(result["event_id"],safe=""))
                        elif action == "issue-qr":
                            self._redirect(f"/events?issued={result['qr_id']}")
                        elif action == "enter":
                            self._redirect("/")  # 进入事件 → 落到该空间指挥台首页
                        else:
                            self._redirect("/events")
                        return
                    if route == "/events/alarm/demo":
                        # P17-P1：演示报警入口（显式演示数据，非实时接入）。
                        alarm_id = state.handle_alarm_demo_ingest()
                        event_id = state.alarm_inbox.alarms[alarm_id].event_id
                        self._redirect(f"/events?alarm_event={event_id}")
                        return
                    if (
                        len(parts) == 4 and parts[0] == "events" and parts[2] == "roster"
                        and parts[3] in {"add", "all-add", "assign"}
                    ):
                        event_id = parts[1]
                        if parts[3] == "add":
                            state.handle_roster_add(event_id, form.get("phones") or [])
                        elif parts[3] == "all-add":
                            state.handle_roster_all_add(event_id)
                        else:
                            state.handle_roster_assign(
                                event_id,
                                (form.get("phone") or [""])[0],
                                (form.get("pos_code") or [""])[0],
                            )
                        self._redirect(f"/events/{event_id}/roster")
                        return
                    if (
                        len(parts) == 3 and parts[0] == "events" and parts[2] == "library-import"
                    ):
                        state.handle_import_member_library((form.get("roster_csv") or [""])[0])
                        self._redirect(f"/events/{parts[1]}/roster")
                        return
                    if (
                        len(parts) == 4 and parts[0] == "events" and parts[2] == "person"
                        and parts[3] == "state"
                    ):
                        state.handle_person_state_mark(
                            parts[1],
                            (form.get("action") or [""])[0],
                            phone=(form.get("phone") or [""])[0],
                            order_id=(form.get("order_id") or [""])[0],
                            note=(form.get("note") or [""])[0],
                        )
                        self._redirect(f"/events/{parts[1]}/roster")
                        return
                except PanelActionError as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._respond(404, "<h1>404</h1>")
                return

            # -- P16 T5: materials ledger maintenance (commander only) ----------

            if route.startswith("/materials/"):
                import secrets
                member = state.member_session(self._member_cookie())
                try:
                    state.require_material_maintenance(self._member_cookie())
                    csrf = form.get('csrf', [])
                    if member is None or len(csrf) != 1 or not secrets.compare_digest(csrf[0].encode(), member.custody_csrf.encode()):
                        raise PermissionError('具名维护会话及CSRF必须有效。')
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>需要具名长期物资维护授权。</p>')
                    return
                except (ValueError, PanelActionError) as error:
                    self._respond(409, '<h1>409</h1><p>'+esc(str(error))+'</p>')
                    return
                try:
                    if route == "/materials/item":
                        state.handle_material_item(
                            member_token=self._member_cookie(),
                            name=(form.get("name") or [""])[0],
                            category=(form.get("category") or [""])[0],
                            spec=(form.get("spec") or [""])[0],
                            unit=(form.get("unit") or [""])[0],
                            default_carry_qty=(form.get("default_carry_qty") or [""])[0],
                        )
                    elif route == "/materials/squad":
                        state.handle_material_squad(
                            member_token=self._member_cookie(),
                            name=(form.get("name") or [""])[0],
                            member_phones=(form.get("member_phones") or [""])[0],
                            headcount_planned=(form.get("headcount_planned") or [""])[0],
                            leader_phone=(form.get("leader_phone") or [""])[0],
                        )
                    elif route == "/materials/squad-leader":
                        state.handle_material_squad_leader(
                            (form.get("squad_id") or [""])[0],
                            (form.get("leader_phone") or [""])[0],
                        )
                    elif route == "/materials/carry":
                        state.handle_material_carry(
                            member_token=self._member_cookie(),
                            squad_id=(form.get("squad_id") or [""])[0],
                            item_id=(form.get("item_id") or [""])[0],
                            quantity=(form.get("quantity") or [""])[0],
                        )
                    elif route == "/materials/point":
                        state.handle_material_point(
                            member_token=self._member_cookie(),
                            name=(form.get("name") or [""])[0],
                            location=(form.get("location") or [""])[0],
                            contact_name=(form.get("contact_name") or [""])[0],
                            contact_phone=(form.get("contact_phone") or [""])[0],
                            item_id=(form.get("item_id") or [""])[0],
                            stock=(form.get("stock") or [""])[0],
                        )
                    elif route == "/materials/check":
                        state.handle_material_check(
                            event_id=(form.get("event_id") or [""])[0],
                            squad_id=(form.get("squad_id") or [""])[0],
                            item_id=(form.get("item_id") or [""])[0],
                            qty_usable=(form.get("qty_usable") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                        )
                    elif route == "/materials/consume":
                        state.handle_material_consume(
                            event_id=(form.get("event_id") or [""])[0],
                            squad_id=(form.get("squad_id") or [""])[0],
                            item_id=(form.get("item_id") or [""])[0],
                            amount=(form.get("amount") or [""])[0],
                        )
                    else:
                        self._respond(404, "<h1>404</h1>")
                        return
                except PermissionError:
                    self._respond(403, '<h1>403</h1>')
                    return
                except (ValueError, PanelActionError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/materials")
                return

            # -- P16 T20: member account unlock (doc 01 §8 锁定→解除) ----------

            if route == "/account/unlock":
                if not state.command_mode(token):
                    self._respond(403, "<h1>403</h1><p>解锁动作需要指挥员入口认证。</p>")
                    return
                try:
                    state.handle_account_unlock((form.get("phone") or [""])[0])
                except PanelActionError as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/events")
                return

            # -- P16 T18: file import pipeline (doc 04 §8, commander only) ----

            if route.startswith("/imports/"):
                import secrets
                batch = state.imports.get((form.get('batch_id') or [''])[0])
                data_type = (form.get('data_type') or [''])[0] if route == '/imports/upload' else (batch.data_type if batch else '')
                try:
                    state._require_import_authority(data_type, self._member_cookie())
                    if data_type in {'personnel', 'squad'} and not state.command_mode(token):
                        raise PermissionError('人员导入须沿用既有认证入口，不接受匿名或物资维护权。')
                    if data_type not in {'personnel', 'squad'}:
                        member = state.member_session(self._member_cookie())
                        csrf = form.get('csrf', [])
                        if member is None or len(csrf) != 1 or not secrets.compare_digest(csrf[0].encode(), member.custody_csrf.encode()):
                            raise PermissionError('具名导入会话及CSRF必须有效。')
                except PermissionError:
                    self._respond(403, '<h1>403</h1><p>需要相应具名维护授权。</p>')
                    return
                except (ValueError, PanelActionError) as error:
                    self._respond(409, '<h1>409</h1><p>'+esc(str(error))+'</p>')
                    return
                try:
                    if route == "/imports/upload":
                        state.handle_import_upload(
                            member_token=self._member_cookie(),
                            data_type=(form.get("data_type") or [""])[0],
                            filename=(form.get("filename") or [""])[0],
                            content=(form.get("content") or [""])[0],
                        )
                    elif route == "/imports/draft":
                        state.handle_import_draft((form.get("batch_id") or [""])[0], member_token=self._member_cookie())
                    elif route == "/imports/publish":
                        state.handle_import_publish((form.get("batch_id") or [""])[0], member_token=self._member_cookie())
                    elif route == "/imports/rollback":
                        state.handle_import_rollback((form.get("batch_id") or [""])[0], member_token=self._member_cookie())
                    else:
                        self._respond(404, "<h1>404</h1>")
                        return
                except PermissionError as exc:
                    self._respond(403, f"<h1>403</h1><p>{esc(exc)}</p>")
                    return
                except PermissionError:
                    self._respond(403, '<h1>403</h1>')
                    return
                except (ValueError, PanelActionError) as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                self._redirect("/materials")
                return

            # -- P16 T6: sandtable actions (doc 03 §12 permission matrix) ----

            if route.startswith("/sand/"):
                commander = state.command_mode(token) or state.named_sand_authorized()
                member = None if commander else state.member_session(self._member_cookie())
                if not commander and member is None:
                    self._redirect("/login")
                    return
                if (member is not None and state.active_event_id
                        and not state.member_can_view_current_event(self._member_cookie())):
                    self._respond(403, '<h1>403</h1><p>当前成员无权访问此事件沙盘。</p>')
                    return
                ask_only = {"/sand/ask", "/sand/source-check", "/sand/intent", "/sand/observe"}
                if route not in ask_only and not commander:
                    if self._sand_draft_reply:
                        self._respond_json({'ok': False, 'message': '当前身份没有此操作权限；草稿未提交。'}, status=403)
                        return
                    self._respond(403, "<h1>403</h1><p>沙盘作业动作需要指挥员权限。</p>")
                    return
                if state.named_sand_authorized():
                    member_session = state.member_session(self._member_cookie())
                    if route == '/sand/image-upload' and self.headers.get('Content-Type','').startswith('multipart/form-data'):
                        form = _parse_multipart(raw_bytes,self.headers['Content-Type'])[0]
                    supplied = form.get('_business_csrf') or []
                    if len(supplied) != 1 or not _secrets.compare_digest(supplied[0],member_session.custody_csrf):
                        self._respond(403,'<h1>403</h1><p>当前事件会话校验失败。</p>'); return
                multipart = None
                if self._sand_draft_reply and route == '/sand/image-upload':
                    try:
                        ctype = self.headers.get('Content-Type', '')
                        if not ctype.startswith('multipart/form-data'):
                            raise PanelActionError('上传必须是 multipart/form-data 表单')
                        multipart = _parse_multipart(raw_bytes, ctype)
                        form = multipart[0]
                    except PanelActionError as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=422)
                        return
                if self._sand_draft_reply:
                    owner, event = draft_context(state, 'commander' if state.command_mode(token) else 'member', token if state.command_mode(token) else self._member_cookie())
                    if (form.get('_draft_owner') or [''])[0] != owner or (form.get('_draft_event') or [''])[0] != event:
                        self._respond_json({'ok': False, 'message': '登录或当前事件已变化，请核对事件后刷新；原草稿仍保留。'}, status=409)
                        return
                try:
                    if route == "/sand/image-upload":
                        # 多媒体表单特例：文件不走 urlencoded form。
                        ctype = self.headers.get("Content-Type", "")
                        if not ctype.startswith("multipart/form-data"):
                            raise PanelActionError("上传必须是 multipart/form-data 表单")
                        fields, file_map = multipart if multipart is not None else _parse_multipart(raw_bytes, ctype)
                        picked = file_map.get("file")
                        state.handle_image_upload(
                            source_system=(fields.get("source_system") or [""])[0],
                            collected_by=(fields.get("collected_by") or [""])[0],
                            collected_at=(fields.get("collected_at") or [""])[0],
                            has_spatial=(fields.get("has_spatial") or [""])[0] == "1",
                            spatial_range=(fields.get("spatial_range") or [""])[0],
                            geo_precision=(fields.get("geo_precision") or [""])[0],
                            filename=picked[0] if picked else "",
                            payload=picked[1] if picked else b"",
                            note=(fields.get("note") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/image-status":
                        state.handle_image_status(
                            (form.get("image_id") or [""])[0],
                            (form.get("target") or [""])[0],
                            spatial_range=(form.get("spatial_range") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/image-analyze":
                        state.handle_image_analyze((form.get("image_id") or [""])[0])
                        self._redirect("/sand")
                        return
                    if route == "/sand/image-decision":
                        state.handle_image_decision(
                            (form.get("image_id") or [""])[0],
                            (form.get("decision") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/annotate":
                        form_details = {
                            key[2:]: (values[0] if values else "")
                            for key, values in form.items()
                            if key.startswith("d_")
                        }
                        state.handle_sand_annotate(
                            kind=(form.get("kind") or [""])[0],
                            title=(form.get("title") or [""])[0],
                            points=(form.get("points") or [""])[0],
                            certainty=(form.get("certainty") or [""])[0],
                            note=(form.get("note") or [""])[0],
                            geo_text=(form.get("geo") or [""])[0],
                            details=form_details,
                            action=(form.get("action") or ["draft"])[0],
                            responsible_unit=(form.get("responsible_unit") or [""])[0],
                            route_status=(form.get("route_status") or [""])[0],
                            positioning_status=(form.get("positioning_status") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/annotate-geo":
                        state.handle_sand_annotation_geo(
                            (form.get("annotation_id") or [""])[0],
                            (form.get("geo") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/annotate-status":
                        state.handle_sand_annotation_status(
                            (form.get("annotation_id") or [""])[0],
                            (form.get("target") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/annotate-edit":
                        form_details = {
                            key[2:]: (values[0] if values else "")
                            for key, values in form.items()
                            if key.startswith("d_")
                        }
                        state.handle_sand_annotation_edit(
                            (form.get("annotation_id") or [""])[0],
                            title=(form.get("title") or [""])[0],
                            note=(form.get("note") or [""])[0],
                            details=form_details,
                            responsible_unit=(form.get("responsible_unit") or [""])[0],
                            route_status=(form.get("route_status") or [""])[0],
                            positioning_status=(form.get("positioning_status") or [""])[0],
                            base_version=(form.get("base_version") or [""])[0],
                            as_revision=bool(form.get("as_revision")),
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/conflict-resolve":
                        state.handle_sand_conflict_resolve(
                            (form.get("conflict_id") or [""])[0],
                            resolution=(form.get("resolution") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/candidate":
                        action = (form.get("action") or [""])[0]
                        target = {
                            "verify": AnnotationStatus.VERIFIED.value,
                            "withdraw": AnnotationStatus.WITHDRAWN.value,
                        }.get(action)
                        if target is None:
                            raise PanelActionError(f"未知候选处理动作：{action}")
                        state.handle_sand_annotation_status(
                            (form.get("annotation_id") or [""])[0], target
                        )
                        self._redirect("/sand")
                        return
                    if route == "/sand/ai-candidate":
                        state.handle_sand_ai_candidate()
                        self._redirect("/sand")
                        return
                    if route == "/sand/measure":
                        points_text = (form.get("points") or [""])[0]
                        save_flag = (form.get("save") or [""])[0] == "1"
                        area_type = ((form.get("area_type") or [""])[0]).strip() or "待核查区"
                        try:
                            result = sandtable.measure_polygon(
                                sandtable.parse_points(points_text)
                            )
                        except sandtable.MeasureError as exc:
                            if self._sand_draft_reply:
                                self._respond_json({'ok': False, 'message': str(exc)}, status=422)
                                return
                            result = None
                            body = render_sand_page(
                                state,
                                role="commander",
                                viewer_word="指挥员",
                                measure_error=str(exc),
                            )
                        else:
                            saved = ""
                            if save_flag:
                                # doc 03 §11.2：存为业务对象＝进草稿走核验链，不直接发布。
                                kind_value = (
                                    AnnotationKind.ISOLATION.value
                                    if area_type == "隔离带"
                                    else AnnotationKind.AREA.value
                                )
                                note = (
                                    "来源：自由区域测算存档（示意值·非实测）；"
                                    f"面积约 {result['area_m2']:,.1f} m²；"
                                    f"周长约 {result['perimeter_m']:,.1f} m"
                                )
                                try:
                                    saved = state.handle_sand_annotate(
                                        kind=kind_value,
                                        title=f"{area_type}·自由测算",
                                        points=points_text,
                                        note=note,
                                    )
                                except PanelActionError as exc:
                                    if self._sand_draft_reply:
                                        self._respond_json({'ok': False, 'message': str(exc)}, status=409)
                                        return
                                    self._respond(
                                        409,
                                        f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>",
                                    )
                                    return
                            if self._sand_draft_reply:
                                if save_flag:
                                    self._redirect('/sand#drawer-annotate')
                                else:
                                    self._respond_json({'ok': True, 'kind': 'measurement', 'result': result})
                                return
                            body = render_sand_page(
                                state,
                                role="commander",
                                viewer_word="指挥员",
                                measure_result=result,
                                measure_saved=saved,
                            )
                        user = '<span>指挥员 · 指挥模式</span>' + _topbar_logout_form("/logout", "登出")
                        self._respond(
                            200,
                            _light_wrap(
                                title="指挥调度沙盘",
                                body=body,
                                nav=_light_nav(_COMMANDER_LIGHT_NAV, "/sand"),
                                user=user,
                            ),
                        )
                        return
                    if route == "/sand/ask":
                        role = "commander" if commander else "member"
                        try:
                            state.handle_grounded_sand_ask(
                                (form.get("question") or [""])[0],
                                role=role,
                                session_token=self._cookie_token(),
                                member_token=self._member_cookie(),
                                mask_phone_fn=None if commander else mask_phone,
                            )
                        except OSError:
                            question = (form.get('question') or [''])[0]
                            self._respond(503, '<h1>问询记录保存失败</h1>'
                                          '<p>本次回答未写入历史。可按原问题重试。</p>'
                                          '<form method="post" action="/sand/ask">'
                                          f'<input name="question" value="{esc(question)}">'
                                          '<button type="submit">原问题重试</button></form>')
                            return
                        except PanelActionError as exc:
                            user = (
                                '<span>指挥员 · 指挥模式</span>'
                                + _topbar_logout_form("/logout", "登出")
                                if commander
                                else (
                                    f"<span>{esc(getattr(member, 'name', '') or '')}（{esc(mask_phone(getattr(member, 'phone', '')))}）</span>"
                                    + _topbar_logout_form("/logout/member", "退出登录")
                                )
                            )
                            body = render_sand_page(
                                state,
                                role=role,
                                viewer_word="指挥员" if commander else "到场人员",
                                ask_error=str(exc),
                                ask_question=(form.get("question") or [""])[0],
                            )
                            if self._sand_draft_reply:
                                self._respond_json({'ok': False, 'message': str(exc)}, status=422)
                                return
                            self._respond(
                                200,
                                _light_wrap(
                                    title="指挥调度沙盘",
                                    body=body,
                                    nav=_light_nav(
                                        _COMMANDER_LIGHT_NAV if commander else _MEMBER_LIGHT_NAV,
                                        "/sand",
                                    ),
                                    user=user,
                                ),
                            )
                            return
                        self._redirect("/sand#qa")
                        return
                    if route in ('/sand/deepseek-preview', '/sand/deepseek-confirm', '/sand/model-preview', '/sand/model-confirm'):
                        preview, qa_error = None, ''
                        try:
                            if route in ('/sand/deepseek-preview', '/sand/model-preview'):
                                preview = state.prepare_deepseek_qa(
                                    (form.get('inquiry_id') or [''])[0],
                                    session_token=token)
                            else:
                                state.confirm_deepseek_qa(
                                    (form.get('preview_token') or [''])[0],
                                    session_token=token)
                        except (PanelActionError, OSError) as exc:
                            qa_error = str(exc)
                        if route in ('/sand/deepseek-confirm', '/sand/model-confirm') and not qa_error:
                            self._redirect('/sand#qa')
                            return
                        body = render_sand_page(
                            state, role='commander', viewer_word='指挥员',
                            ask_error=qa_error, deepseek_preview=preview)
                        self._respond(
                            422 if qa_error else 200,
                            _light_wrap(
                                title='指挥调度沙盘', body=body,
                                nav=_light_nav(_COMMANDER_LIGHT_NAV, '/sand'),
                                user='<span>指挥员 · 指挥模式</span>'
                                     + _topbar_logout_form('/logout', '登出')))
                        return
                    if route == "/sand/source-check":
                        from .official_source import OfficialSourceUnavailable, read_official_page
                        from .settings import load_settings

                        role = "commander" if commander else "member"
                        source_url = (form.get("source_url") or [""])[0]
                        source_page, source_error = None, ""
                        try:
                            source_page = read_official_page(
                                source_url, network_enabled=load_settings().network_enabled)
                        except (OfficialSourceUnavailable, ValueError) as exc:
                            source_error = str(exc)
                        user = (
                            '<span>指挥员 · 指挥模式</span>'
                            + _topbar_logout_form("/logout", "登出")
                            if commander
                            else (
                                f"<span>{esc(getattr(member, 'name', '') or '')}（{esc(mask_phone(getattr(member, 'phone', '')))}）</span>"
                                + _topbar_logout_form("/logout/member", "退出登录")
                            )
                        )
                        body = render_sand_page(
                            state, role=role,
                            viewer_word="指挥员" if commander else "到场人员",
                            viewer_id="" if commander else getattr(member, "phone", ""),
                            source_page=source_page, source_error=source_error,
                            source_url=source_url,
                        )
                        self._respond(
                            200, _light_wrap(
                                title="指挥调度沙盘", body=body,
                                nav=_light_nav(
                                    _COMMANDER_LIGHT_NAV if commander else _MEMBER_LIGHT_NAV,
                                    "/sand"),
                                user=user))
                        return
                    if route == "/sand/intent":
                        role = "commander" if commander else "member"
                        try:
                            state.handle_voice_input(
                                (form.get("text") or [""])[0],
                                role=role,
                                mask_phone_fn=None if commander else mask_phone,
                                session_token=self._cookie_token(),
                                member_token=self._member_cookie(),
                            )
                        except OSError:
                            self._respond(503, '<h1>问询记录保存失败</h1><p>请保留原输入并重试。</p>')
                            return
                        except PanelActionError as exc:
                            state.intent_notice = f"输入未处理：{exc}"
                            if self._sand_draft_reply:
                                self._respond_json({'ok': False, 'message': str(exc)}, status=422)
                                return
                        self._redirect("/sand#intent")
                        return
                    if route in {"/sand/draft-confirm", "/sand/draft-discard"}:
                        draft_id = (form.get("draft_id") or [""])[0]
                        if route == "/sand/draft-confirm":
                            state.handle_voice_draft_confirm(draft_id)
                        else:
                            state.handle_voice_draft_discard(draft_id)
                        self._redirect("/sand#intent")
                        return
                    if route == "/sand/review-current":
                        before_reviews = len(state.workspace.situation_reviews)
                        batch_id = state.handle_sand_review_current()
                        if self._sand_draft_reply:
                            from .situation_review_ui import result
                            self._respond_json(dict(ok=True, kind='situation-review', result=result(
                                state, batch_id, created=len(state.workspace.situation_reviews) > before_reviews)))
                            return
                        self._redirect("/sand#cmd-situation")
                        return
                    if route == "/sand/observe":
                        form_details = {
                            key[2:]: (values[0] if values else "")
                            for key, values in form.items()
                            if key.startswith("d_")
                        }
                        reporter = (
                            "本机指挥端" if commander
                            else f"{getattr(member, 'name', '') or '现场人员'}"
                        )
                        state.handle_sand_observe(
                            kind=(form.get("kind") or [""])[0],
                            title=(form.get("title") or [""])[0],
                            geo_text=(form.get("geo") or [""])[0],
                            observed_at=(form.get("observed_at") or [""])[0],
                            source_ref=(form.get("source_ref") or [""])[0],
                            positioning_status=(form.get("positioning_status") or [""])[0],
                            details=form_details,
                            note=(form.get("note") or [""])[0],
                            is_demo=(form.get("is_demo") or [""])[0] == "1",
                            reporter=reporter,
                        )
                        self._redirect("/sand#cmd-advisor")
                        return
                    if route == "/sand/change-action":
                        state.handle_sand_change_action(
                            (form.get("change_id") or [""])[0],
                            action=(form.get("action") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                        )
                        self._redirect("/sand#cmd-advisor")
                        return
                    if route == "/sand/goal":
                        state.handle_sand_goal(
                            (form.get("statement") or [""])[0],
                        )
                        self._redirect("/sand#cmd-advisor")
                        return
                    if route == "/sand/gap-defer":
                        state.handle_sand_gap_defer(
                            (form.get("gap_id") or [""])[0],
                        )
                        self._redirect("/sand#cmd-advisor")
                        return
                    if route == "/sand/restaff-sand":
                        state.handle_restaff(
                            (form.get("question") or [""])[0],
                            session_token=self._cookie_token(),
                        )
                        self._redirect("/sand#cmd-plans")
                        return
                    if route == "/sand/self-option":
                        state.handle_propose_option(
                            title=(form.get("title") or [""])[0],
                            summary=(form.get("summary") or [""])[0],
                            risks=(form.get("risks") or [""])[0],
                            no_go_conditions=(form.get("no_go_conditions") or [""])[0],
                            preconditions=(form.get("preconditions") or [""])[0],
                            reversibility=(form.get("reversibility") or [""])[0],
                        )
                        self._redirect("/sand#cmd-plans")
                        return
                    if route == "/sand/plan-decision":
                        state.handle_plan_decision(
                            (form.get("option_id") or [""])[0],
                            (form.get("action") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                            accept_risks=(form.get("accept_risks") or [""])[0] == "on",
                        )
                        self._redirect("/sand#cmd-plans")
                        return
                    if route == "/sand/task-draft":
                        draft = state.handle_task_draft_edit(form)
                        self._redirect("/sand#" + quote(draft.id, safe=''))
                        return
                    self._respond(404, "<h1>404</h1>")
                    return
                except PanelActionError as exc:
                    if self._sand_draft_reply:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=409)
                        return
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return

            if route.startswith('/review/') and state.workspace.event_id:
                from . import review_meeting as meeting
                member_token = self._member_cookie()
                if route == '/review/ask':
                    if not state.command_mode(token) and state.member_session(member_token) is None:
                        self._redirect('/login'); return
                    if not (state.command_mode(token) or meeting.is_owner(state, member_token)
                            or meeting.is_deputy(state, member_token) or state.member_can_view_current_event(member_token)):
                        self._respond(403, '<h1>403</h1>'); return
                    try:
                        from .review_inquiry import viewer_key
                        form_token = member_token if meeting.is_owner(state, member_token) else token if state.command_mode(token) else member_token
                        if (form.get('event_id') != [state.active_event_id]
                                or form.get('csrf') != [meeting.csrf(state, form_token)]
                                or any(len(v) != 1 for v in form.values())):
                            raise ValueError('问询会话或事件已变化，请重新载入。')
                    except ValueError as exc:
                        self._respond_json({'ok': False, 'message': str(exc)}, status=409) if self.headers.get('X-Review-Ask') == '1' else self._respond(409, '<p>'+esc(exc)+'</p>')
                        return
                else:
                    permitted = (meeting.is_deputy(state, member_token) if route == '/review/owner'
                                 else meeting.is_owner(state, member_token))
                    if not permitted:
                        self._respond(403, '<h1>403</h1><p>仅本事件复盘负责人本人可执行；指定负责人仅限具名指定副总指挥。</p>'); return
                    try:
                        meeting.validate_form(state, member_token, form)
                        for field, collection in (('issue_id','issues'),('rect_id','rects'),('exp_id','experiences'),
                                                  ('hyp_id','hypotheses'),('report_id','reports')):
                            if field in form:
                                record = getattr(state.review_ledger, collection).get(form[field][0])
                                if record is None or meeting.record_event(record) != state.active_event_id:
                                    raise ValueError('目标复盘记录不属于当前事件。')
                        for related_field in ('related_id',):
                            related = form.get(related_field,[''])[0]
                            if related and not any(related in getattr(state.review_ledger,name) and getattr(state.review_ledger,name)[related].event_id == state.active_event_id for name in ('issues','rects','hypotheses')):
                                raise ValueError('关联复盘记录不属于当前事件。')
                        for issue_id in form.get('related_issues',[''])[0].split(','):
                            issue_id = issue_id.strip()
                            if issue_id and (issue_id not in state.review_ledger.issues
                                    or state.review_ledger.issues[issue_id].event_id != state.active_event_id):
                                raise ValueError('关联问题不属于当前事件。')
                        if route == '/review/owner':
                            state.handle_review_owner_assign(form.get('phone',[''])[0], deputy_token=member_token,
                                evidence=form.get('evidence',[''])[0],request_id=form.get('request_id',[''])[0],
                                occurred_at=form.get('occurred_at',[''])[0])
                            self._redirect('/review'); return
                        if route == '/review/meeting':
                            meeting.record_note(state,member_token,{k:v[0] for k,v in form.items()
                                if k not in {'csrf','event_id','expected_version'}})
                            self._redirect('/review?tab=timeline'); return
                        if route.startswith('/review/flow-'):
                            from .review_flow import dispatch
                            from urllib.parse import urlencode
                            back,notice=dispatch(state,member_token,route.removeprefix('/review/'),
                                {k:v[0] for k,v in form.items() if k not in {'csrf','event_id','expected_version'}})
                            self._redirect('/review?'+urlencode(dict(tab=back,msg=notice)));return
                        if route == '/review/assessment':
                            from .review_causality import record_assessment
                            record_assessment(state, member_token, {k:v[0] for k,v in form.items()
                                if k not in {'csrf','event_id','expected_version'}})
                            self._redirect('/review?tab=analysis'); return
                        if route in {'/review/rect-new', '/review/rect-status',
                                     '/review/exp-new', '/review/exp-status'}:
                            raise ValueError('本事件须使用 P12 审核、反馈和验收入口。')
                        if route.startswith('/review/rect-') or route.startswith('/review/exp-'):
                            from . import review_closure as closure
                            value = lambda key: form.get(key, [''])[0]
                            if route == '/review/rect-draft':
                                closure.create_rect_draft(state, member_token, value('issue_id'), value('hyp_id'))
                            elif route == '/review/rect-publish':
                                closure.publish_rect(state, member_token, value('rect_id'),
                                    measures=value('measures'), owner_unit=value('owner_unit'),
                                    owner_person=value('owner_person'), deadline=value('deadline'),
                                    acceptance_standard=value('acceptance_standard'),
                                    verifier_name=value('verifier_name'),
                                    responsible_phone=value('responsible_phone'),
                                    verifier_phone=value('verifier_phone'))
                            elif route == '/review/rect-feedback':
                                closure.record_rect_feedback(state, member_token, value('rect_id'),
                                    provided_by=value('provided_by'), duty=value('duty'),
                                    content=value('content'), evidence=value('evidence'),
                                    completed=value('completed') == '1')
                            elif route == '/review/rect-verify':
                                closure.record_rect_verification(state, member_token, value('rect_id'),
                                    verifier=value('verifier'), outcome=value('outcome'),
                                    finding=value('finding'), evidence=value('evidence'))
                            elif route == '/review/exp-propose':
                                closure.propose_experience(state, member_token,
                                    source_kind=value('source_kind'), source_id=value('source_id'))
                            elif route == '/review/exp-revise':
                                closure.revise_experience(state, member_token, value('exp_id'),
                                    content=value('content'), applicability=value('applicability'),
                                    limitations=value('limitations'), condition_tags=value('condition_tags'),
                                    evidence=value('evidence'), reason=value('reason'))
                            elif route == '/review/exp-decide':
                                closure.decide_experience(state, member_token, value('exp_id'),
                                    target=value('target'), reason=value('reason'), evidence=value('evidence'))
                            else:
                                raise ValueError('未知 P12 复盘动作。')
                            self._redirect('/review?tab='+('rect' if route.startswith('/review/rect-') else 'exp')); return
                    except PermissionError as exc:
                        self._respond(403, '<h1>403</h1><p>'+esc(exc)+'</p>'); return
                    except (ValueError, OSError, sqlite3.Error) as exc:
                        if route.startswith('/review/flow-'):
                            back=('rect' if route.startswith('/review/flow-rect-') else
                                'exp' if route.startswith('/review/flow-exp-') else
                                'timeline' if route=='/review/flow-note' else 'issues')
                            _,body=render_page(state,'/review',session_token=token,member_token=member_token,
                                query={'tab':[back],'msg':[str(exc)]})
                            self._respond(409,body);return
                        self._respond(409, '<h1>409</h1><p>'+esc(exc)+'</p>'); return

            # -- P16 T10: review inquiry (read-only; commander + member) ------

            if route == "/review/ask":
                commander_ask = state.command_mode(token)
                if state.workspace.event_id:
                    commander_ask = commander_ask or meeting.is_owner(state, self._member_cookie())
                member_ask = (
                    None if commander_ask
                    else state.member_session(self._member_cookie())
                )
                if not commander_ask and member_ask is None:
                    self._redirect("/login")
                    return
                try:
                    state.handle_review_ask(
                        (form.get("question") or [""])[0],
                        role="commander" if commander_ask else "member",
                        mask_phone_fn=None if commander_ask else mask_phone,
                        viewer_id=viewer_key(state, token, self._member_cookie()) if state.workspace.event_id else '',
                        follow_up=form.get('follow_up') == ['1'],
                    )
                except (PanelActionError, ValueError) as exc:
                    if self.headers.get('X-Review-Ask') == '1':
                        self._respond_json({'ok': False, 'message': str(exc)}, status=409); return
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                if self.headers.get('X-Review-Ask') == '1':
                    self._respond_json({'ok': True}); return
                from urllib.parse import quote as _q
                self._redirect("/review?tab=summary&msg=" + _q("问询完成，结果见下方问询记录。"))
                return

            # -- P16 T7: review workbench actions (commander only) -------------

            if route.startswith("/review/"):
                commander_rv = state.command_mode(token)
                member_rv = (state.member_session(self._member_cookie()) if state.workspace.event_id else
                    None if commander_rv else state.member_session(self._member_cookie()))
                from .review_meeting import is_owner
                is_owner_rv = (is_owner(state, self._member_cookie()) if state.workspace.event_id else
                    member_rv is not None and bool(state.review_ledger.owner_phone)
                    and member_rv.phone == state.review_ledger.owner_phone)
                if not commander_rv and member_rv is None:
                    self._redirect("/login")
                    return
                # 普通成员会话无任何复盘作业动作（doc 05 §9 矩阵）；
                # 负责人本人会话全量放行，指挥员秘钥会话不变。
                if member_rv is not None and not is_owner_rv:
                    self._respond(
                        403,
                        "<h1>403</h1><p>复盘作业动作需要指挥员或复盘负责人权限。</p>")
                    return
                # doc 05 §9 + A-2（P17-P5 严格落实）：六类结论级动作仅复盘负责人本人
                # 会话可执行；指挥员秘钥会话一律 403（可指派负责人/登记候选输入/查看）。
                _OWNER_ONLY_ROUTES = {
                    "/review/hyp-status", "/review/hyp-resolve",
                    "/review/rect-status", "/review/exp-status",
                    "/review/report-status", "/review/report-edit",
                }
                if route in _OWNER_ONLY_ROUTES and not is_owner_rv:
                    self._respond(
                        403,
                        "<h1>403</h1><p>此动作属结论级操作，仅复盘负责人本人可执行（规范 05·§9）。"
                        "请指挥员先在复盘页指派复盘负责人，由其本人会话执行。</p>")
                    return
                state._pending_review_actor = (
                    ("owner", member_rv.name, state.active_event_id, self._member_cookie())
                    if is_owner_rv and (state.workspace.event_id or not commander_rv) else None
                )
                try:
                    message = ""
                    if route == "/review/generate":
                        added = state.handle_review_generate()
                        message = (
                            f"规则扫描完成：新增候选 {added} 条。"
                            if added else "规则扫描完成：本事件未触发新候选。"
                        )
                    elif route == "/review/issue-status":
                        state.handle_review_issue_status(
                            (form.get("issue_id") or [""])[0],
                            (form.get("target") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                            assessment_ref=(form.get("assessment_ref") or [""])[0],
                        )
                    elif route == "/review/issue-reopen":
                        state.handle_review_issue_reopen(
                            (form.get("issue_id") or [""])[0],
                            (form.get("reason") or [""])[0],
                        )
                    elif route == "/review/rect-new":
                        rect_id = state.handle_review_rect_new(
                            related_id=(form.get("related_id") or [""])[0],
                            measures=(form.get("measures") or [""])[0],
                            owner_unit=(form.get("owner_unit") or [""])[0],
                            owner_person=(form.get("owner_person") or [""])[0],
                            priority=(form.get("priority") or ["中"])[0],
                            deadline=(form.get("deadline") or [""])[0],
                            verify_method=(form.get("verify_method") or [""])[0],
                        )
                        message = f"整改任务已建立：{rect_id}（待分派）。"
                    elif route == "/review/rect-status":
                        state.handle_review_rect_status(
                            (form.get("rect_id") or [""])[0],
                            (form.get("target") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                            operator=state.review_operator(),
                        )
                    elif route == "/review/exp-new":
                        exp_id = state.handle_review_exp_new(
                            content=(form.get("content") or [""])[0],
                            applicability=(form.get("applicability") or [""])[0],
                        )
                        message = f"经验候选项已登记：{exp_id}。"
                    elif route == "/review/exp-status":
                        state.handle_review_exp_status(
                            (form.get("exp_id") or [""])[0],
                            (form.get("target") or [""])[0],
                            operator=state.review_operator(),
                        )
                    elif route == "/review/hyp-new":
                        hyp_id = state.handle_review_hyp_new(
                            statement=(form.get("statement") or [""])[0],
                            correlation=(form.get("correlation") or ["弱"])[0],
                            evidence=(form.get("evidence") or [""])[0],
                            counter_examples=(form.get("counter_examples") or [""])[0],
                            to_verify=(form.get("to_verify") or [""])[0],
                            related_issues=(form.get("related_issues") or [""])[0],
                        )
                        message = f"根因假设已登记：{hyp_id}（待验证）。"
                    elif route == "/review/hyp-status":
                        state.handle_review_hyp_status(
                            (form.get("hyp_id") or [""])[0],
                            (form.get("target") or [""])[0],
                            reason=(form.get("reason") or [""])[0],
                            assessment_ref=(form.get("assessment_ref") or [""])[0],
                            operator=state.review_operator(),
                        )
                    elif route == "/review/hyp-resolve":
                        state.handle_review_hyp_resolve(
                            (form.get("hyp_id") or [""])[0],
                            (form.get("item") or [""])[0],
                            (form.get("answer") or [""])[0],
                            operator=state.review_operator(),
                        )
                    elif route == "/review/report-generate":
                        rid_ = state.handle_report_generate(stage=form.get('stage') == ['1'])
                        message = f"已生成报告新版本：{rid_}（AI 草稿）。"
                    elif route == "/review/report-edit":
                        state.handle_report_edit(
                            (form.get("report_id") or [""])[0],
                            (form.get("content_md") or [""])[0],
                            operator=state.review_operator(),
                        )
                    elif route == "/review/report-status":
                        state.handle_report_status(
                            (form.get("report_id") or [""])[0],
                            (form.get("target") or [""])[0],
                            operator=state.review_operator(),
                        )
                    elif route == "/review/owner":
                        state.handle_review_owner_assign(
                            (form.get("phone") or [""])[0])
                        message = "复盘负责人已指派。"
                    elif route == "/review/maintainer":
                        result = state.handle_maintenance_grant(
                            (form.get("phone") or [""])[0],
                            revoke=(form.get("revoke") or [""])[0] == "1",
                        )
                        message = (
                            f"已撤销维护授权：{result['phone']}。" if result["granted"] is False
                            else f"已授予维护权限：{result['phone']}。")
                    else:
                        self._respond(404, "<h1>404</h1>")
                        return
                except (PanelActionError, ValueError) as exc:
                    state._pending_review_actor = None
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                    return
                finally:
                    state._pending_review_actor = None
                back_tab = {
                    "/review/generate": "issues", "/review/issue-status": "issues",
                    "/review/issue-reopen": "issues", "/review/rect-new": "rect",
                    "/review/rect-status": "rect", "/review/exp-new": "exp",
                    "/review/exp-status": "exp",
                    "/review/hyp-new": "root", "/review/hyp-status": "root",
                    "/review/hyp-resolve": "root",
                    "/review/report-generate": "report",
                    "/review/report-edit": "report",
                    "/review/report-status": "report",
                    "/review/owner": "summary",
                    "/review/maintainer": "summary",
                }.get(route, "summary")
                # 操作反馈消息经查询串带回页面顶部提示条。
                from urllib.parse import quote as _q
                suffix = f"&msg={_q(message)}" if message else ""
                self._redirect(f"/review?tab={back_tab}{suffix}")
                return

            if route in _ACTION_ROUTES:
                if not state.command_mode(token):
                    self._respond(403, "<h1>403</h1><p>指挥动作需要指挥员入口认证。</p>")
                    return
                try:
                    if route == "/answer":
                        state.handle_answer(
                            (form.get("question_id") or [""])[0],
                            (form.get("answer") or [""])[0],
                        )
                        self._redirect("/questions")
                    elif route == "/pause":
                        state.handle_pause(
                            (form.get("order_id") or [""])[0],
                            (form.get("reason") or [""])[0],
                        )
                        self._redirect("/orders")
                    elif route == "/confirm-goal":
                        state.handle_confirm_goal(
                            (form.get("goal_id") or [""])[0]
                        )
                        self._redirect("/brief")
                    elif route == "/propose-option":
                        state.handle_propose_option(
                            title=(form.get("title") or [""])[0],
                            summary=(form.get("summary") or [""])[0],
                            claim_subjects=(form.get("claim_subjects") or [""])[0],
                            risks=(form.get("risks") or [""])[0],
                            no_go_conditions=(form.get("no_go") or [""])[0],
                            preconditions=(form.get("preconditions") or [""])[0],
                            reversibility=(form.get("reversibility") or [""])[0],
                        )
                        self._redirect("/brief")
                    elif route == "/restaff":
                        state.handle_restaff(
                            (form.get("question") or [""])[0],
                            session_token=token,
                        )
                        self._redirect("/brief")
                    else:
                        state.handle_memory(
                            (form.get("candidate_id") or [""])[0],
                            (form.get("decision") or [""])[0],
                        )
                        self._redirect("/reviews")
                except PanelActionError as exc:
                    self._respond(409, f"<h1>409</h1><p>{esc(exc)}</p>")
                return

            if route in _DRILL_ACTION_ROUTES:
                if not state.command_mode(token):
                    self._respond(403, "<h1>403</h1><p>指挥动作需要指挥员入口认证。</p>")
                    return
                try:
                    if route == "/drill/checkin":
                        state.handle_checkin(
                            (form.get("pos_code") or [""])[0],
                            (form.get("phone") or [""])[0],
                        )
                        self._redirect("/org")
                    elif route == "/drill/checkout":
                        state.handle_checkout((form.get("binding") or [""])[0])
                        self._redirect("/org")
                    elif route == "/drill/import-roster":
                        state.handle_import_roster((form.get("roster_csv") or [""])[0])
                        self._redirect("/org")
                    elif route == "/drill/save":
                        state.handle_save_snapshot()
                        self._redirect("/drill")
                    elif route == "/drill/load":
                        state.handle_load_snapshot((form.get("snapshot") or [""])[0])
                        self._redirect("/")
                    elif route == "/drill/reset":
                        state.handle_reset()
                        self._redirect("/drill")
                except PanelActionError as exc:
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                except (ValueError, KeyError) as exc:
                    # Workspace refusals surface verbatim (一人一岗 / 岗位已满…).
                    self._respond(409, f"<h1>409</h1><p class='prewrap'>{esc(exc)}</p>")
                return

            self._respond(404, "<h1>404</h1>")

    # Serialize requests with the optional scheduler. This also protects the
    # renderer's no-mutation check and event/workspace swaps during a tick.
    original_get, original_post = PanelHandler.do_GET, PanelHandler.do_POST
    def scoped_request(handler, operation):
        try:
            with _deputy_event_context(state, handler.path.split('?', 1)[0],
                    session_token=handler._cookie_token(), member_token=handler._member_cookie()):
                route = handler.path.split('?', 1)[0]
                member_token = handler._member_cookie()
                if route == '/captain-reports/photo' and handler.command == 'GET':
                    from .captain_reports import serve_photo
                    try:
                        with _deputy_event_context(state,'/materials/event',member_token=member_token):
                            return serve_photo(handler,state,member_token)
                    except PermissionError as exc:handler._respond_json(dict(ok=False,message=str(exc)),status=403)
                    except (ValueError,OSError) as exc:handler._respond_json(dict(ok=False,message=str(exc)),status=409)
                    return
                if route.startswith('/app/v2/'):
                    from .captain_field_http import handle
                    return handle(handler,state,original_post)
                if route == '/app/download' and handler.command == 'GET':
                    from .android_api import download
                    return download(handler,state)
                if route.startswith('/app/v1/'):
                    from .android_api import handle
                    return handle(handler,state,original_post)
                if route == '/mobile' and handler.command == 'GET':
                    # 网页版队长端已停用（与沙盘菜单互相干扰）；统一引导去装 App。
                    from .wifi_demo import info_page
                    handler._respond(200, info_page(state)); return
                if route == '/mobile/login' and handler.command == 'POST':
                    from .wifi_demo import info_page
                    handler._respond(200, info_page(state)); return
                if route == '/wifi' and handler.command == 'GET':
                    from .wifi_demo import info_page
                    handler._respond(200,info_page(state));return
                expected_event=handler.headers.get('X-Sand-Event')
                if handler.command=='POST' and expected_event is not None and (not expected_event or expected_event!=_business.selected_event(state,member_token)):
                    handler._respond_json({'error':'当前火情已变化，未提交原沙盘操作；请保留输入并重新核对。'},status=409,close=True)
                    return
                if handler.command == 'GET' and route in _unified.ALIASES and state.member_session(member_token):
                    from urllib.parse import urlsplit
                    query = urlsplit(handler.path).query
                    handler._redirect(_unified.ALIASES[route]+('?' + query if query else ''))
                    return
                if handler.command == 'POST' and route in _unified.SAND_COMMANDS and _unified.deputy_command(state,member_token):
                    from contextlib import contextmanager
                    @contextmanager
                    def grant():
                        space = state.workspace
                        previous = getattr(state,'_named_sand_token',None)
                        prior = getattr(space,'_named_command_check',None)
                        state._named_sand_token = member_token
                        space._named_command_check = lambda: state.named_sand_authorized()
                        space._named_command_subject = state.authorization_subject(member_token)
                        try:
                            yield
                        finally:
                            state._named_sand_token = previous
                            space._named_command_check = prior
                            space._named_command_subject = None
                    with grant():
                        return operation(handler)
                return operation(handler)
        except PermissionError:
            handler._respond(403 if handler.command == 'POST' else 404, '<h1>访问权限已失效</h1>')
        except PanelActionError:
            handler._respond(409, '<h1>409</h1><p>事件任务空间待核对。</p>')
    def simulation_get(handler):
        if handler.path.split('?', 1)[0].startswith('/sand/online-imagery/'):
            from .online_imagery import serve
            return serve(handler, state)
        if state.simulation_host is None:
            with state._event_authority_lock:
                from .read_scope import read_scope
                with read_scope():
                    return scoped_request(handler, original_get)
        with state._simulation_lock:
            with state._event_authority_lock:
                from .read_scope import read_scope
                with read_scope():
                    return scoped_request(handler, original_get)
    def simulation_post(handler):
        if handler.path.split('?',1)[0] == '/app/v2/report':
            from .captain_reports import prepare
            if not prepare(handler,state):return
        if state.simulation_host is None:
            with state._event_authority_lock:
                return scoped_request(handler, original_post)
        with state._simulation_lock:
            with state._event_authority_lock:
                return scoped_request(handler, original_post)
    PanelHandler.do_GET, PanelHandler.do_POST = simulation_get, simulation_post
    return PanelHandler


def make_server(state: PanelState, *, port: int = 0) -> ThreadingHTTPServer:
    """Bind loopback only — the panel never listens on external interfaces."""
    from .intelligence import IntelligenceRuntime
    from .assistant_followups import Runtime as AssistantRuntime
    state.intelligence_runtime = IntelligenceRuntime(state)
    state.assistant_runtime = AssistantRuntime(state)
    class PanelServer(ThreadingHTTPServer):
        # A page now loads several local map/UI assets together. The inherited
        # backlog of five can refuse a script before its handler is accepted.
        request_queue_size = 32
        def service_actions(self):
            if state.simulation_host is not None:
                state.simulation_host.tick()
            state.intelligence_runtime.tick()
            state.assistant_runtime.tick()
        def handle_request(self):
            self.service_actions()
            return super().handle_request()
    return PanelServer(("127.0.0.1", port), make_handler(state))


def resolve_secret(*, cli_secret: str | None, demo: bool) -> tuple[str, str]:
    """Secret resolution: CLI > environment > demo constant > ephemeral random.

    Returns (secret, origin_description). The ephemeral case is printed once
    by the caller — trusted local harness only (doc 11 §3.5 premise).
    """
    if cli_secret:
        return cli_secret, "--secret 参数"
    env_secret = os.environ.get("GENERAL_ENTRY_SECRET")
    if env_secret:
        return env_secret, "环境变量 GENERAL_ENTRY_SECRET"
    if demo:
        from .campaign_runner import DRILL_SECRET

        return DRILL_SECRET, "演示模式固定口令"
    return _secrets.token_urlsafe(12), "本次随机生成（仅打印这一次）"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="破局智援参谋本地控制面板（仅 127.0.0.1）")
    parser.add_argument("--db", help="SQLite 快照路径；缺省运行为战役演练实况")
    parser.add_argument("--port", type=int, default=8642)
    parser.add_argument("--secret", help="指挥员入口口令；缺省读环境变量或演示口令")
    parser.add_argument("--duty-phone", default=os.environ.get("PZ_DUTY_PHONE", ""),
                        help="可信配置中的值班人员手机号；必须已在人员库并以本人账号登录")
    args = parser.parse_args(argv)

    secret, origin = resolve_secret(cli_secret=args.secret, demo=args.db is None)
    auth = GeneralEntryAuth(secret=secret)
    print(f"指挥员入口口令来源：{origin}")
    if origin.startswith("本次随机"):
        print(f"本次会话口令：{secret}")

    state = load_state(db_path=args.db, demo=args.db is None, auth=auth,
                       duty_subjects=((args.duty_phone,) if args.duty_phone else ()))
    if args.db is None:
        state.workspace.unlock_general_entry(auth.authenticate(secret))
    else:
        # 战役库模式：自动选中最新的未关闭事件，沙盘/名册直接有业务上下文。
        open_events = [
            eid for eid in sorted(state.registry.events)
            if state.registry.events[eid].status != "closed"
        ]
        if open_events:
            state.active_event_id = open_events[-1]
            print(f"当前事件：{state.active_event_id}（{state.registry.events[state.active_event_id].name}）")
    server = make_server(state, port=args.port)
    bound_port = server.server_address[1]
    print(f"破局智援参谋指挥台：http://127.0.0.1:{bound_port}/  （Ctrl+C 退出）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
