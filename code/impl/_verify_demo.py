# -*- coding: utf-8 -*-
"""临时脚本：验证演示场景数据 + 页面渲染（用后即删）。"""
import io
import re
import sys
import threading
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

sys.path.insert(0, ".")
from panda_brain.panel import load_state, make_server
from panda_brain.auth import GeneralEntryAuth
from panda_brain.campaign_runner import DRILL_SECRET

DEMO = Path("data/demo")
auth = GeneralEntryAuth(secret=DRILL_SECRET)
state = load_state(db_path=DEMO / "demo_hanjiang.db", auth=auth)
evs = [e for e in sorted(state.registry.events) if state.registry.events[e].status != "closed"]
if evs:
    state.active_event_id = evs[-1]   # 与 main() 的自动选择同逻辑
print("active:", state.active_event_id)
print("alarms:", len(state.alarm_inbox.alarms),
      "| annotations:", len(state.workspace.annotations),
      "| imagery:", len(state.imagery.images),
      "| options:", len(state.workspace.options),
      "| orders:", len(state.workspace.orders),
      "| review issues:", len(state.review_ledger.issues),
      "| owner:", state.review_ledger.owner_phone)

state.workspace.unlock_general_entry(auth.authenticate(DRILL_SECRET))
server = make_server(state, port=0)
threading.Thread(target=server.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{server.server_address[1]}"


class NR(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


data = urllib.parse.urlencode({"username": "admin", "secret": DRILL_SECRET}).encode()
op = urllib.request.build_opener(NR)
try:
    r = op.open(urllib.request.Request(base + "/login", data=data), timeout=10)
except urllib.error.HTTPError as e:
    r = e
cookie = re.search(r"[A-Za-z_]+=[^;]+", r.headers.get("Set-Cookie", "")).group(0)
op2 = urllib.request.build_opener()

fails = []
for path, marks in (
    ("/events", ["卫星热点", "林区烟感", "待确认"]),
    ("/sand", ["东线火点", "青岗河取水点", "火线", "待核验候选"]),
    ("/materials", ["缺口", "高压水泵", "已撤离"]),
    ("/review", ["李四", "待核验"]),
    ("/review?tab=rect", ["飞火下风侧必封"]),
    ("/review?tab=exp", ["先封下风侧"]),
    ("/review?tab=root", ["隔离带开口位置偏东"]),
    ("/", ["张三", "已到场", "<svg"]),
):
    req = urllib.request.Request(base + path, headers={"Cookie": cookie})
    html = op2.open(req, timeout=20).read().decode("utf-8")
    miss = [m for m in marks if m not in html]
    st = "OK" if not miss else f"缺 {miss}"
    if miss:
        fails.append(path)
    print(path, "->", st, f"({len(html)//1024}KB)")
server.shutdown()
print("RESULT:", "ALL PASS" if not fails else fails)
