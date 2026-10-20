# -*- coding: utf-8 -*-
"""临时脚本：盘点演示库内容（只读，用后即删）。"""
import sys
from pathlib import Path

sys.path.insert(0, ".")
from panda_brain.persistence import load_workspace

ws = load_workspace(Path("data/demo_hanjiang.db"), general_auth=None)
print("version:", ws.version)
for name in ("annotations", "advisor_proposals", "options", "decisions", "orders",
             "execution_reports", "policies", "approvals", "objectives", "questions"):
    try:
        col = getattr(ws, name)
        items = list(col.values()) if hasattr(col, "values") else list(col)
        print(f"{name}: {len(items)}")
    except AttributeError:
        print(f"{name}: <no attr>")
# 事件注册表（不在 ws 上的部分由 PanelState 管，这里只列 ws 的）
attrs = [a for a in dir(ws) if not a.startswith("_") and isinstance(getattr(ws, a), (list, dict, tuple))]
print("collections attrs:", [a for a in attrs if getattr(ws, a) and len(getattr(ws, a)) > 0][:20])
