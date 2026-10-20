"""Narrow host-runtime access: map images and an approved DeepSeek request only.

P00 tests never enable this runtime policy. No credential is stored here.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import ipaddress
import socket
from urllib.parse import urlsplit

MAP_IMAGE_ORIGINS = (
    *(f'https://webrd0{i}.is.autonavi.com' for i in range(1, 5)),
    *(f'https://webst0{i}.is.autonavi.com' for i in range(1, 5)),
    *(f'https://{s}.tile.opentopomap.org' for s in 'abc'),
    'https://tile.openstreetmap.org',
)
_runtime_enabled = False
_permit = ContextVar('deepseek_request_egress', default=None)
_map_permit = ContextVar('map_image_request_egress', default=None)
_lan_bind = ContextVar('wifi_demo_listener',default=None)
IMAGERY_HOSTS = frozenset(f'webst0{i}.is.autonavi.com' for i in range(1, 5))


def content_security_policy(path):
    images = "'self' data:"
    if urlsplit(path).path == '/sand':
        images += ' ' + ' '.join(MAP_IMAGE_ORIGINS)
    return ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; img-src " + images + "; "
            "connect-src 'self'; font-src 'self' data:; worker-src 'self'; object-src 'none'")


def install_headers(server):
    original = server.RequestHandlerClass.end_headers
    def headers(handler):
        handler.send_header('Content-Security-Policy', content_security_policy(handler.path))
        handler.send_header('Referrer-Policy', 'no-referrer')
        original(handler)
    server.RequestHandlerClass.end_headers = headers


def enable_runtime():
    global _runtime_enabled
    _runtime_enabled = True


def permit_model_socket_event(event, args):
    """Demo opt-in: only an enabled, credentialed DeepSeek request context.

    Images and other network clients keep the demo's original loopback policy.
    The same official-host, public-DNS and pinned-address rules apply as runtime.
    """
    return _permit.get() is not None and permit_socket_event(event, args)


def permit_socket_event(event, args):
    if event == 'socket.bind' and _runtime_enabled and _lan_bind.get() is not None:
        return args[1] == _lan_bind.get()
    image = _map_permit.get()
    if _runtime_enabled and image is not None:
        host, addresses = image
        if event == 'socket.getaddrinfo':
            return args[0] == host and args[1] == 443
        if event == 'socket.connect':
            address = args[1]
            return (isinstance(address, tuple) and len(address) >= 2
                    and address[1] == 443 and address[0] in addresses)
        return False
    permit = _permit.get()
    if not _runtime_enabled or permit is None:
        return False
    if event == 'socket.getaddrinfo':
        return args[0] == 'api.deepseek.com' and args[1] == 443
    if event == 'socket.connect':
        address = args[1]
        return (isinstance(address, tuple) and len(address) >= 2
                and address[1] == 443 and address[0] in permit)
    return False


@contextmanager
def wifi_listener(address,port):
    from .wifi_demo import network
    network(address,24,port)
    mark=_lan_bind.set((address,port))
    try:yield
    finally:_lan_bind.reset(mark)


@contextmanager
def map_image_egress(host):
    """One authenticated raster request, fixed image hosts and public TLS only."""
    if host not in IMAGERY_HOSTS:
        raise ValueError('影像来源不在既有高德图源范围内。')
    if not _runtime_enabled:
        raise PermissionError('当前运行环境未启用在线影像。')
    token = _map_permit.set((host, frozenset()))
    try:
        records = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        addresses = frozenset(row[4][0] for row in records)
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError('高德影像地址未解析为有效公网服务。')
        _map_permit.set((host, addresses))
        yield
    finally:
        _map_permit.reset(token)


@contextmanager
def model_egress(profile):
    """Called only at SafeProvider transport, after profile permission checks.

    Resolve exactly the official host, reject private DNS results, pin connect
    addresses for this thread/context, then remove permission even on failure.
    Redirects and proxies are separately denied by SafeProvider.
    """
    if (not _runtime_enabled or profile.vendor != 'deepseek'
            or not profile.enabled or not profile.api_key):
        yield
        return
    from .model_connections import validate_url
    validate_url(profile.base_url, profile.vendor)
    token = _permit.set(frozenset())
    try:
        records = socket.getaddrinfo('api.deepseek.com', 443, type=socket.SOCK_STREAM)
        addresses = frozenset(row[4][0] for row in records)
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise ValueError('DeepSeek 地址未解析为有效公网服务，请检查 DNS。')
        _permit.set(addresses)
        yield
    finally:
        _permit.reset(token)
