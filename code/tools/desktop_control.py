"""Windows desktop controls for an existing delivery; never seed or kill data/processes."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
from datetime import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import webbrowser

import psutil


PORTS = (18703, 18705)
LOGIN = 'http://127.0.0.1:18703/login'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def listeners():
    return [c for c in psutil.net_connections(kind='tcp')
            if c.status == psutil.CONN_LISTEN and c.laddr.port in PORTS]


def owned_process(root, runtime):
    number = int(runtime['pid'])
    try:
        process = psutil.Process(number)
        command = process.cmdline()
    except psutil.NoSuchProcess:
        return None
    target = str(root / 'run_fire_demo.py').casefold()
    if not any(arg.casefold() == target for arg in command):
        raise RuntimeError('运行记录中的进程号已被其他程序使用，请保留日志并联系维护人员。')
    return process


def current_process(root):
    path = root / 'runtime.json'
    process = owned_process(root, read_json(path)) if path.exists() else None
    if process is None:
        target = str(root / 'run_fire_demo.py').casefold()
        for candidate in psutil.process_iter(['pid', 'cmdline']):
            if any(arg.casefold() == target for arg in candidate.info['cmdline'] or []):
                raise RuntimeError('发现尚未登记完成的启动进程，请稍后再试，勿重复启动。')
    return process


def healthy(process):
    ports = listeners()
    if not any(c.pid == process.pid and c.laddr.port == 18703 for c in ports):
        return False
    if any(c.pid != process.pid for c in ports):
        raise RuntimeError('演示端口被其他程序占用，请先正常停止其他演示。')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(LOGIN, timeout=3) as response:
            return response.status == 200 and '破局智援参谋' in response.read().decode('utf-8')
    except (OSError, ValueError):
        return False


def wifi_addresses():
    script = """
    [Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
    $ErrorActionPreference='Stop'
    $items=@(Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway -and $_.IPv4Address } | ForEach-Object {
        $adapter=Get-NetAdapter -InterfaceIndex $_.InterfaceIndex
        if ($adapter.HardwareInterface) {
            foreach ($address in $_.IPv4Address) {
                if ($address.IPAddress -match '^(10\\.|192\\.168\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.)' -and $address.PrefixLength -ge 16 -and $address.PrefixLength -le 30) {
                    [pscustomobject]@{address=$address.IPAddress;prefix=$address.PrefixLength}
                }
            }
        }
    })
    ConvertTo-Json -InputObject $items -Compress
    """
    result = subprocess.run(
        ['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
        capture_output=True, encoding='utf-8-sig', timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW, check=True)
    return json.loads(result.stdout)


def start(root):
    process = current_process(root)
    if process:
        if not healthy(process):
            raise RuntimeError('服务进程仍在，但登录页没有正常响应。请稍后重试或使用“停止系统”正常停止后再启动。')
        return dict(action='already_running', pid=process.pid, login=LOGIN,
                    phone_origin=read_json(root / 'runtime.json').get('phone_origin', ''))
    if listeners():
        raise RuntimeError('18703或18705端口已被其他程序占用，请先正常停止其他演示。')
    lock = root / 'process.lock'
    if lock.exists():
        previous = int(lock.read_text(encoding='utf-8').strip())
        if psutil.pid_exists(previous):
            raise RuntimeError('运行标记仍对应现有进程，不能自动清理。请联系维护人员核对。')
        lock.rename(root / ('process.lock.stale-' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')))
    addresses = wifi_addresses()
    command = [sys.executable, '-B', str(root / 'run_fire_demo.py'), '--port', '18703']
    # pythonw keeps the server independent of the temporary desktop control window.
    windowless = Path(sys.executable).with_name('pythonw.exe')
    if windowless.exists():
        command[0] = str(windowless)
    if len(addresses) == 1:
        command += ['--wifi-address', addresses[0]['address'], '--wifi-prefix',
                    str(addresses[0]['prefix']), '--phone-port', '18705']
    (root / 'stop').unlink(missing_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    with (root / (stamp + '-desktop-stdout.log')).open('wb') as out, \
            (root / (stamp + '-desktop-stderr.log')).open('wb') as err:
        child = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL,
                                 stdout=out, stderr=err, close_fds=True,
                                 creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError('启动未完成，请查看交付目录中最新的desktop-stderr.log；未重置数据。')
        runtime = root / 'runtime.json'
        if runtime.exists() and read_json(runtime)['pid'] == child.pid:
            if healthy(psutil.Process(child.pid)):
                return dict(action='started', pid=child.pid, login=LOGIN,
                            phone_origin=read_json(runtime).get('phone_origin', ''))
        time.sleep(.4)
    raise RuntimeError('启动尚未就绪，未强制停止进程。请保留日志，勿反复启动。')


def stop(root):
    process = current_process(root)
    if process is None:
        if listeners():
            raise RuntimeError('当前秦岭系统已停止，但端口属于其他程序；没有停止其他程序。')
        return dict(action='already_stopped')
    if any(c.pid != process.pid for c in listeners()):
        raise RuntimeError('端口与当前运行记录不一致，请联系维护人员核对。')
    started = time.time()
    (root / 'stop').write_text('normal desktop user stop', encoding='utf-8')
    try:
        process.wait(timeout=35)
    except psutil.TimeoutExpired:
        raise RuntimeError('已发出正常停止请求，服务仍在处理；未强制结束。请稍后重试。')
    exit_path = root / 'last-exit.json'
    if not exit_path.exists() or exit_path.stat().st_mtime < started - 1:
        raise RuntimeError('进程已退出，但缺少本次正常停止结果，请保留日志并联系维护人员。')
    result = read_json(exit_path)
    if not result.get('normal') or not result.get('audit_valid'):
        raise RuntimeError('停止结果需要核对，请保留日志和数据并联系维护人员。')
    if (root / 'process.lock').exists() or any(c.pid == process.pid for c in listeners()):
        raise RuntimeError('停止后运行标记或端口尚未释放，请稍后核对。')
    return dict(action='stopped', pid=process.pid, audit_valid=True)


def dialog(message, error=False):
    import tkinter as tk
    from tkinter import messagebox
    window = tk.Tk()
    window.withdraw()
    window.attributes('-topmost', True)
    (messagebox.showerror if error else messagebox.showinfo)(
        '破局智援参谋', message, parent=window)
    window.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop'))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--quiet', action='store_true', help='Print result instead of showing dialogs')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    root = args.root.resolve(strict=True)
    if not (root / 'run_fire_demo.py').is_file() or not (root / 'source-manifest.json').is_file():
        raise RuntimeError('交付目录不完整，不能启动。')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    mutex = kernel.CreateMutexW(None, False, 'Local\\Pojuzhiyuan-' + hashlib.sha256(str(root).casefold().encode()).hexdigest())
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    acquired = kernel.WaitForSingleObject(mutex, 0) in (0, 0x80)
    try:
        if not acquired:
            raise RuntimeError('正在启动或停止，请等待当前操作完成后再试。')
        result = start(root) if args.action == 'start' else stop(root)
        if args.action == 'start' and not args.no_browser:
            webbrowser.open(LOGIN)
        if args.quiet:
            print(json.dumps(result, ensure_ascii=False))
        elif args.action == 'stop':
            dialog('系统已正常停止。' if result['action'] == 'stopped' else '系统已经停止，无需重复操作。')
        elif not result.get('phone_origin'):
            dialog('电脑系统已启动，登录页已打开。未检测到唯一可用的局域网网卡，本次仅启用电脑端。手机演示请连接同一Wi-Fi后停止并重新启动。')
        return 0
    finally:
        if acquired:
            kernel.ReleaseMutex(mutex)
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        message = str(error)
        if '--quiet' in sys.argv:
            print(json.dumps(dict(error=message), ensure_ascii=False))
        else:
            dialog(message, error=True)
        raise SystemExit(1)
