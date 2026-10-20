# -*- coding: utf-8 -*-
"""临时探测 2：全部 32 槽位 + homepage org chart 渲染区。"""
import io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from panda_brain.org_model import POSITION_SLOTS
for s in POSITION_SLOTS:
    print(f"{s.pos_code:16s} {s.name}")

src = open("panda_brain/homepage.py", encoding="utf-8").read()
print("=" * 50, "homepage chars:", len(src))
i = src.find("org_chart")
print(src[max(0, i-300):i+500] if i >= 0 else "org_chart not referenced in homepage.py")
