"""Serve a copied P00 synthetic scenario from a fresh local preview data root."""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=65452)
    args = parser.parse_args()
    seed_run = args.run.resolve()
    seed = seed_run / "impl" / "data" / "demo"
    target = args.data_root.resolve()
    allowed = ROOT / ".p02-preview"
    if not seed_run.is_relative_to(ROOT / ".p00-runs") or not (seed / "demo_hanjiang.db").exists():
        parser.error("--run must be a completed isolated P00 run")
    if not target.is_relative_to(allowed) or target == allowed or target.exists():
        parser.error("--data-root must be a new child of W/.p02-preview")
    target.mkdir(parents=True)
    (target / "tmp").mkdir()
    (target / "snapshots").mkdir()
    for item in seed.iterdir():
        shutil.copy2(item, target / item.name)
    for item in (seed_run / "impl" / "snapshots").glob("*.db"):
        shutil.copy2(item, target / "snapshots" / item.name)
    for key in list(os.environ):
        if any(word in key.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL")) or key.upper().endswith("_PROXY"):
            os.environ.pop(key, None)
    os.environ.update(TEMP=str(target / "tmp"), TMP=str(target / "tmp"),
                      TMPDIR=str(target / "tmp"))
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT / "impl"))
    from p00_baseline import guard
    guard(target)
    from panda_brain import workbench as wb
    from panda_brain.auth import GeneralEntryAuth
    from panda_brain.campaign_runner import DRILL_SECRET
    from panda_brain.panel import load_state, make_server

    wb.SNAPSHOT_DIR = target / "snapshots"
    state = load_state(db_path=target / "demo_hanjiang.db",
                       auth=GeneralEntryAuth(secret=DRILL_SECRET))
    for event_id, event in state.registry.events.items():
        state.registry.events[event_id] = replace(
            event, snapshot_path=str(target / "snapshots" / f"{event_id}.db"))
    state.active_event_id = sorted(state.registry.events)[-1]
    state._save_registry()
    server = make_server(state, port=args.port)
    (target / "preview.json").write_text(json.dumps({
        "url": f"http://127.0.0.1:{server.server_port}/sand",
        "data_root": str(target), "source_run": str(seed_run),
        "data_policy": "fresh copy of isolated P00 synthetic scenario",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"PREVIEW=http://127.0.0.1:{server.server_port}/sand", flush=True)
    print(f"LOCAL_DEMO_LOGIN={DRILL_SECRET}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
