"""Read-only commander console (doc 11 §7 P8-T4) — the minimal predecessor
of the full control panel.

Usage:
    python -m panda_brain.console [--db PATH]

Without ``--db`` a fresh in-memory demo scenario is rendered. The console
performs no write operations of any kind; its read-only guarantee is asserted
in the acceptance tests. Viewing is not a command act, but command authority
still requires authentication (doc 11 §3.5): the entry status is always shown.
"""

from __future__ import annotations

import argparse
import os
import sys

from .auth import GeneralEntryAuth
from .models import GENERAL_ENTRY_ID
from .workspace import ScenarioWorkspace


def _entry_status_line(workspace: ScenarioWorkspace) -> str:
    if workspace._general_entry_unlocked:
        return f"指挥员入口：已认证（{GENERAL_ENTRY_ID}）"
    return "指挥员入口：锁定 —— 命令权需要认证；当前为只读视图。"


def build_console_text(workspace: ScenarioWorkspace) -> str:
    """Render the full read-only briefing. Must not mutate the workspace."""
    lines: list[str] = []
    broken = workspace.verify_audit_chain()
    lines.append(f"=== 破局智援参谋 指挥台 · {workspace.scenario_id} · v{workspace.version} ===")
    lines.append(_entry_status_line(workspace))
    lines.append(
        "审计链：" + ("完整" if not broken else f"已断链！异常序号 {broken}")
    )

    lines.append("")
    lines.append("-- 共享态势 --")
    if workspace.claims:
        for subject in sorted(workspace.claims):
            claim = workspace.claims[subject]
            lines.append(f"  {subject} = {claim.value!r} ({claim.status.value})")
    else:
        lines.append("  （暂无事实记录）")

    lines.append("")
    lines.append("-- 最新决策简报 --")
    if workspace.decision_briefs:
        brief = max(
            workspace.decision_briefs.values(), key=lambda b: b.source_version
        )
        lines.append(f"  问题：{brief.question}")
        lines.append(f"  推荐：{brief.recommendation}")
        if brief.option_matrix:
            lines.append(f"  方案对比：{len(brief.option_matrix)} 行")
        if brief.question_items:
            lines.append(f"  待取舍项：{len(brief.question_items)}")
        if brief.relevant_experiences:
            lines.append(f"  相关经验：{len(brief.relevant_experiences)} 条")
        for summary_line in brief.situation_summary[-1:]:
            lines.append(f"  附件：{summary_line}")
    else:
        lines.append("  （尚无简报）")

    lines.append("")
    lines.append("-- 在途命令 --")
    active_orders = [
        order
        for order in workspace.orders.values()
        if order.status.value not in {"completed", "failed", "cancelled", "expired", "superseded"}
    ]
    if active_orders:
        for order in sorted(active_orders, key=lambda o: o.id):
            lines.append(f"  {order.id} [{order.status.value}] 选项={order.option_id}")
    else:
        lines.append("  （无在途命令）")

    lines.append("")
    lines.append("-- 待你回答 --")
    open_questions = [
        q for q in workspace.general_questions.values() if q.status == "open"
    ]
    if open_questions:
        for question in open_questions:
            deadline = (
                f"，建议 v{question.suggested_reply_by_version} 前答复"
                if question.suggested_reply_by_version is not None
                else ""
            )
            lines.append(f"  [{question.kind}] {question.question}{deadline}")
    else:
        lines.append("  （无待答问题）")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="破局智援参谋只读指挥台")
    parser.add_argument("--db", help="SQLite 快照路径；缺省使用内存演示场景")
    args = parser.parse_args(argv)

    if args.db:
        from .persistence import load_workspace

        secret = os.environ.get("GENERAL_ENTRY_SECRET")
        auth = GeneralEntryAuth(secret=secret) if secret else GeneralEntryAuth()
        workspace = load_workspace(args.db, general_auth=auth)
        if secret:
            workspace.unlock_general_entry(auth.authenticate(secret))
    else:
        from .simulator import ForestFireScenario

        workspace = ForestFireScenario().workspace

    version_before = workspace.version
    text = build_console_text(workspace)
    print(text)
    if workspace.version != version_before:  # read-only guarantee
        print("错误：控制台意外修改了工作区状态。", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
