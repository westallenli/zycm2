"""我的开发版启动器。

目标：跑**我这份代码**，但用**Codex 最新交付那版的演示数据**（含值班员、副指挥长、
队长三套账号与已生效的任命关系），从而恢复三种身份的权限。

原则：
  1. 绝不写入原交付目录——只从它复制一次 data 到本目录 demo_data。
  2. 复制后立刻把 registry.json 里指向原目录的绝对快照路径改指到副本，
     否则会读写原交付的数据库。
  3. 用 resource_demo.open_state() 装配（老交付的启动方式），不用 panel 演练态。
  4. 启动前自检：端口是否被占、人员库有几个账号、三个演示账号能否用 123456 登录。

用法：
    python -X utf8 run_my_demo.py                 # 启动
    python -X utf8 run_my_demo.py --check         # 只自检，不启动
    python -X utf8 run_my_demo.py --port 18707    # 换端口（18706 被占时）
"""
from __future__ import annotations

import argparse
import json
import shutil
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
IMPL = ROOT / "impl"
DATA = ROOT / "demo_data"
DEFAULT_SOURCE = ROOT.parent / "release" / "秦岭北侧林火演示_20261019_153056"

DEMO_PASSWORD = "123456"
DEMO_ACCOUNTS = (
    ("13900000119", "值班员"),
    ("13900000120", "副指挥长"),
    ("13900000031", "队长"),
)


def prepare(source: Path) -> Path:
    """把老交付的 data 复制到 demo_data，并把快照绝对路径改到副本。"""
    if (DATA / "registry.json").is_file():
        return DATA
    src = source / "data"
    if not (src / "registry.json").is_file():
        raise SystemExit(f"找不到演示数据：{src}\n用 --source 指定交付目录。")
    if DATA.exists():
        shutil.rmtree(DATA)
    shutil.copytree(src, DATA)
    _repoint_snapshots()
    print(f"已从 {src} 复制演示数据到 {DATA}")
    return DATA


def _repoint_snapshots() -> None:
    registry = DATA / "registry.json"
    value = json.loads(registry.read_text(encoding="utf-8"))
    changed = 0
    for event in (value.get("events") or {}).values():
        old = event.get("snapshot_path") or ""
        if not old:
            continue
        event["snapshot_path"] = str(DATA / "snapshots" / Path(old).name)
        changed += 1
    registry.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已把 {changed} 个事件的快照路径改到副本，原交付目录不再被读写")


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.4)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def diagnose(state, secret: str) -> bool:
    """打印身份自检结果。返回三个演示账号是否都可用。"""
    from panda_brain import fire_events as fe

    registry = state.registry
    eid = state.active_event_id or ""
    record = registry.events.get(eid) if eid else None
    print(f"事件库    : {len(registry.events)} 个；当前事件 = {eid or '（无）'}"
          + (f"（{record.name}）" if record is not None else ""))
    print(f"人员库    : {len(registry.library)} 人；已开通账号 {len(registry.credentials)} 个")
    ok_all = True
    for phone, role in DEMO_ACCOUNTS:
        in_lib = phone in registry.library
        cred = registry.credentials.get(phone)
        password_ok = bool(cred) and fe._verify_password(DEMO_PASSWORD, cred.password_hash)
        locked = bool(cred and cred.locked)
        usable = in_lib and password_ok and not locked
        ok_all = ok_all and usable
        print(f"  {phone} {role:<4} 在库={in_lib} 账号={bool(cred)} "
              f"密码{DEMO_PASSWORD}={password_ok} 锁定={locked} -> {'可用' if usable else '不可用'}")
    print(f"指挥员    : admin / {secret}")
    return ok_all


def seed_weather(state) -> None:
    """给演示事件补一组**明确标注的模拟气象**观测，走正式接入链路。

    - 只用 ingestion → intake_bridge 的公开入口，产生的证据/结论/审计与真实接入同构；
    - data_kind 固定 simulated，界面会显示"模拟观测 · 待核对"，不会被当成真实来源；
    - 已有观测时不重复写入。
    """
    from datetime import datetime, timezone

    from panda_brain.ingestion import ObservationStore
    from panda_brain.intake_bridge import IntakeBridge
    from panda_brain.sand_command import weather_snapshot

    eid = state.active_event_id or ""
    if not eid:
        print("气象种子跳过：没有当前事件")
        return
    try:
        if weather_snapshot(state.workspace, eid)["available"]:
            print("气象：已有观测，不重复写入")
            return
    except Exception as exc:  # noqa: BLE001
        print(f"气象：读取现有观测失败，跳过种子（{exc}）")
        return

    stamp = datetime.now(timezone.utc).isoformat()
    items = [
        dict(event_id=eid, source_record_id=f"demo-weather-{index}", source_version=1,
             site_id="秦岭北侧-演示气象站", metric=metric, value=value, unit=unit,
             observed_at=stamp, received_at=stamp,
             quality_note="演示数据：人工填入，未接真实气象设备", coordinate_system="unknown")
        for index, (metric, unit, value) in enumerate((
            ("temperature_c", "C", 17.5),
            ("humidity_pct", "%", 32),
            ("wind_speed_m_s", "m/s", 4.2),
        ), start=1)
    ]
    store = ObservationStore()
    report = store.import_source_json(
        items, event_id=eid, source="演示气象站（模拟，未接设备）", data_kind="simulated")
    if report.rejected:
        print("气象种子被拒：", [str(item) for item in report.rejected])
        return
    try:
        IntakeBridge(store).bridge_pending(state.workspace)
        state.persist_if_db()
    except Exception as exc:  # noqa: BLE001 — 种子失败不阻止启动
        print(f"气象种子写入失败（不阻止启动）：{exc}")
        return
    snapshot = weather_snapshot(state.workspace, eid)
    print(f"气象：{snapshot['notice']}；"
          f"已写入 {len(snapshot['readings'])} 项（温度/湿度/风速）")


def seed_names(state) -> None:
    """把演示人员的名字改成一眼能懂的：副指挥长 / 值班员 / 队长1…N。

    只改显示名，手机号、岗位、权限、名册绑定都不动。
    """
    from dataclasses import replace

    from panda_brain.resource_demo import DEPUTY, DUTY

    library = state.registry.library
    changed = 0
    for phone, name in ((DEPUTY, "副指挥长"), (DUTY, "值班员")):
        if phone in library and library[phone].name != name:
            library[phone] = replace(library[phone], name=name)
            changed += 1
    leaders = sorted(phone for phone, row in library.items()
                     if getattr(row, "is_leader", False) and phone not in {DEPUTY, DUTY})
    for index, phone in enumerate(leaders, 1):
        name = f"队长{index}"
        if library[phone].name != name:
            library[phone] = replace(library[phone], name=name)
            changed += 1
    if changed:
        state._save_registry()
        print(f"演示人名已整理：{changed} 个（副指挥长／值班员／队长1…{len(leaders)}）")


def seed_people(state, *, min_per_squad: int = 4, temporary: int = 6) -> None:
    """补齐演示人员：每个中队都有到场的人；临时编组里也有待编组的人。

    走正式流程（队长扫码载名册→到场；临时人员扫码登记→到场），不直接改账。
    """
    from panda_brain.personnel_groups import group_people

    eid = state.active_event_id or ""
    if not eid:
        print("人员种子跳过：没有当前事件")
        return
    # 临时人员只在第一次补；否则每次重启都会再登记一遍。
    marker = DATA / ".seeded_people"
    if marker.exists():
        temporary = 0
    qr = next((q.qr_id for q in state.registry.qrs.values() if q.event_id == eid), "")
    if not qr:
        print("人员种子跳过：本事件没有二维码")
        return

    filled, failed = 0, 0
    for squad in sorted(state.ledger.squads.values(), key=lambda s: s.squad_id):
        try:
            people = group_people(state.workspace, eid, ledger=state.ledger)
        except Exception as exc:  # noqa: BLE001
            print(f"人员种子跳过：组织视图待核对（{exc}）")
            return
        have = sum(1 for p in people.values() if p.source_id == squad.squad_id)
        if have >= 1:
            continue
        token, error = state.member_login(squad.leader_phone, "123456")
        if error:
            failed += 1
            continue
        try:
            state.handle_captain_scan(eid, captain_token=token, squad_id=squad.squad_id, qr_id=qr)
            state.handle_captain_arrival(eid, captain_token=token, squad_id=squad.squad_id,
                                         mode="initial", phones=[],
                                         request_id=f"seed-arr-{squad.squad_id}")
            filled += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  中队 {squad.name} 补到场失败：{exc}")

    # 临时编组：造几个人登记到场，等副指挥长编组
    from panda_brain.models import PersonRecord

    made = 0
    for index in range(1, temporary + 1):
        phone = f"13900090{index:03d}"
        if phone not in state.registry.library:
            state.registry.import_person_library([PersonRecord(
                name=f"临时{index}", phone=phone, unit="属地支援", home_position="临时")])
            state._save_registry()
        token, error = state.member_login(phone, "123456")
        if error:
            continue
        try:
            state.handle_temporary_scan(eid, member_token=token, qr_id=qr)
            state.handle_temporary_arrival(eid, member_token=token)
            made += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  临时人员 {index} 登记失败：{exc}")

    print(f"人员：补到场 {filled} 个中队（失败 {failed}）；临时编组新增 {made} 人")
    marker.write_text("1", encoding="utf-8")


def seed_apk(data_root) -> None:
    """把队长 App 安装包放到安装页读取的位置——放不到这里，下载二维码就不会出现。"""
    import shutil

    code = Path(data_root).parent
    target_dir = code / "android"
    target_dir.mkdir(parents=True, exist_ok=True)
    names = ("PojuCaptain-0.3.0-demo.apk", "PojuCaptain-0.2.0-demo.apk")
    sources = (code / "android-captain" / "out", code.parent / "release")
    for name in names:
        for base in sources:
            found = next(iter(sorted(base.glob(f"**/{name}"))), None) if base.is_dir() else None
            if not found or not found.is_file():
                continue
            target = target_dir / name
            # 新打包的安装包要比旧的新：把它覆盖过去，扫码才能装到新版。
            if target.is_file() and target.stat().st_mtime >= found.stat().st_mtime:
                return
            import shutil as _shutil

            _shutil.copy2(found, target)
            print(f"队长 App 安装包已就位：{name}")
            return
    if not any((target_dir / name).is_file() for name in names):
        print("队长 App 安装包未找到（不影响网页版手机端）")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="我的开发版（演示数据 + 我的代码）")
    parser.add_argument("--port", type=int, default=18706)
    parser.add_argument("--secret", default=DEMO_PASSWORD, help="指挥员 admin 的口令")
    parser.add_argument("--source", default=str(DEFAULT_SOURCE), help="老交付目录（只读）")
    parser.add_argument("--check", action="store_true", help="只做自检，不启动服务")
    parser.add_argument("--wifi-address", default="", help="手机同网访问用的本机内网 IP（留空则不开手机入口）")
    parser.add_argument("--wifi-prefix", type=int, default=24)
    parser.add_argument("--phone-port", type=int, default=18705)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(IMPL))

    if not args.check and port_in_use(args.port):
        print(f"端口 {args.port} 已被占用。")
        print("很可能是上一次启动的进程还在跑——那时人员库是空的，所以你登不进去。")
        print("请在任务管理器结束占用该端口的 python 进程，或换端口重试：")
        print(f"    python -X utf8 run_my_demo.py --port {args.port + 1}")
        return 2

    data = prepare(Path(args.source))

    from panda_brain.resource_demo import open_state
    from panda_brain.panel import make_server

    state = open_state(data, secret=args.secret)

    try:
        seed_weather(state)
    except Exception as exc:  # noqa: BLE001 — 演示数据补种失败不应阻止启动
        print(f"气象种子异常（不阻止启动）：{exc}")

    try:
        seed_names(state)
    except Exception as exc:  # noqa: BLE001 — 改名失败不影响使用
        print(f"人名整理异常（不阻止启动）：{exc}")

    try:
        seed_people(state)
    except Exception as exc:  # noqa: BLE001 — 人员补种失败不影响使用
        print(f"人员种子异常（不阻止启动）：{exc}")

    # 「模拟饮水保障」是物资演示的任务蓝本，不是方案；已存进数据的话启动时清掉。
    try:
        if state.workspace.options.pop("demo-water-plan", None) is not None:
            from panda_brain.persistence import save_workspace

            save_workspace(state.workspace, state.db_path)
            print("已移除演示用的「模拟饮水保障」选项")
    except Exception as exc:  # noqa: BLE001
        print(f"移除演示选项异常（不阻止启动）：{exc}")

    try:
        seed_apk(DATA)
    except Exception as exc:  # noqa: BLE001 — 安装包缺失不影响网页版手机端
        print(f"安装包就位异常（不阻止启动）：{exc}")

    try:
        from panda_brain.local_satellite import load_installed
        state.local_satellite = load_installed(Path(args.source))
    except Exception as exc:  # noqa: BLE001 — 底图缺失不应阻止启动
        print(f"离线底图未加载（可用在线底图或纯色底）：{exc}")

    print("-" * 52)
    ready = diagnose(state, args.secret)
    print("-" * 52)
    if args.check:
        return 0 if ready else 1
    if not ready:
        print("注意：有演示账号不可用，登录会失败。上面那几行就是原因。")

    server = make_server(state, port=args.port)
    port = server.server_address[1]
    wifi = wifi_thread = None
    if args.wifi_address == "auto":
        try:
            import socket

            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            args.wifi_address = probe.getsockname()[0]
            probe.close()
            print(f"自动检测内网地址：{args.wifi_address}")
        except Exception as exc:  # noqa: BLE001
            print(f"内网地址检测失败，手机入口不启用：{exc}")
            args.wifi_address = ""
    if args.wifi_address:
        try:
            from panda_brain.runtime_access import install_headers
            install_headers(server)
        except Exception:  # noqa: BLE001 — 头部加固失败不阻止演示
            pass
        try:
            from panda_brain.wifi_demo import start as start_wifi
            wifi, wifi_thread = start_wifi(state, args.wifi_address, args.wifi_prefix, args.phone_port)
            origin = getattr(state, "wifi_origin", "")
            if origin:
                print()
                print(f"手机访问（同一 Wi-Fi）  {origin}/mobile")
                print("  扫码登录、看任务、接收、回报；安装页会给 App 下载二维码。")
                try:
                    from panda_brain.event_qr import _svg
                    base = Path(__file__).parent
                    (base / "手机访问二维码.svg").write_text(_svg(origin + "/mobile", "手机访问"), encoding="utf-8")
                    apk_qr = base / "App下载二维码.svg"
                    apk_qr.write_text(_svg(origin + "/app/download", "App 下载"), encoding="utf-8")
                    print(f"  手机访问码：{base / '手机访问二维码.svg'}")
                    print(f"  App 下载码：{apk_qr}  ← 扫这个直接下安装包")
                except Exception as exc:  # noqa: BLE001
                    print(f"  二维码文件没生成（页面里仍有二维码）：{exc}")
        except Exception as exc:  # noqa: BLE001 — 手机入口失败不影响电脑端
            import traceback
            print(f"手机入口未启用：{exc}")
            traceback.print_exc()
    print()
    print(f"沙盘  http://127.0.0.1:{port}/sand")
    print(f"登录  http://127.0.0.1:{port}/login")
    print("（副指挥长请用 13900000120 / 123456）")
    print()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if wifi:
            try:
                wifi.shutdown()
                wifi.server_close()
            except Exception:  # noqa: BLE001
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
