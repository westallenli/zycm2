"""Explicit anonymous P10 panel, isolated from all legacy data/settings."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from panda_brain.panel import make_server, resolve_secret
from panda_brain.simulation_panel import create_simulation_state


def main(argv=None):
    parser = argparse.ArgumentParser(description='破局智援参谋 · 匿名模拟沙盘')
    parser.add_argument('--data-root', required=True, help='新的独立模拟数据根；无标识的非空目录拒绝使用')
    parser.add_argument('--port', type=int, default=8642)
    parser.add_argument('--source-channels', action='store_true', help='在新根启用匿名预制摄像头/气象状态，未接真设备')
    args = parser.parse_args(argv)
    secret, origin = resolve_secret(cli_secret=None, demo=False)
    state = create_simulation_state(args.data_root, secret=secret, source_channels=args.source_channels)
    server = make_server(state, port=args.port)
    print(f'仅匿名模拟，初始暂停。数据根：{Path(args.data_root).resolve()}', flush=True)
    print(f'http://127.0.0.1:{server.server_port}/sand', flush=True)
    if origin.startswith('本次随机'):
        print(f'本次指挥员登录口令：{secret}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
