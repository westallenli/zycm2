"""Fixed-source image bridge; no arbitrary URL, credential, export or disk cache."""
from contextlib import ExitStack, contextmanager
import re
import urllib.error
import urllib.request
from urllib.parse import parse_qs

REL = re.compile(r'/sand/online-imagery/(0|[1-9]\d?)/(0|[1-9]\d{0,5})/(0|[1-9]\d{0,5})\.jpg')


def fetch_tile(z, x, y):
    if (any(type(v) is not int for v in (z, x, y)) or not 0 <= z <= 18
            or not 0 <= x < 2**z or not 0 <= y < 2**z):
        raise ValueError('影像瓦片坐标越界。')
    from .runtime_access import map_image_egress
    from .model_connections import NoRedirect
    from .offline_resources import image_type
    host = f'webst0{(x+y)%4+1}.is.autonavi.com'
    url = f'https://{host}/appmaptile?style=6&x={x}&y={y}&z={z}'
    request = urllib.request.Request(url, headers={'Accept': 'image/jpeg,image/png', 'User-Agent': 'Poju-map/1.0'})
    with map_image_egress(host):
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=6) as response:
            raw = response.read(1024*1024+1)
    if len(raw) > 1024*1024:
        raise ValueError('影像瓦片超过大小限制。')
    kind = image_type(raw)
    if kind not in ('image/png', 'image/jpeg'):
        raise ValueError('影像服务未返回有效图片。')
    return raw, kind


@contextmanager
def authorized(handler, state, route, query):
    from .panel import _deputy_event_context
    from . import unified_ui as ui, offline_resources as maps
    with ExitStack() as stack:
        if state.simulation_host is not None:
            stack.enter_context(state._simulation_lock)
        stack.enter_context(state._event_authority_lock)
        from .read_scope import read_scope
        stack.enter_context(read_scope())
        stack.enter_context(_deputy_event_context(state, route,
            session_token=handler._cookie_token(), member_token=handler._member_cookie()))
        match = REL.fullmatch(route)
        params = parse_qs(query, keep_blank_values=True, max_num_fields=2)
        eid = state.active_event_id
        record = state.registry.events.get(eid)
        if (not match or len(query) > 512 or params != {'event_id': [eid]}
                or not record or record.status == 'registered'
                or getattr(state, '_storage_interrupted', False)
                or not (state.command_mode(handler._cookie_token()) or ui.can_read(state, handler._member_cookie()))
                or maps.view_status(state)['error']):
            raise PermissionError('当前影像查看权限或火情上下文已失效。')
        coords = tuple(int(v) for v in match.groups())
        z, x, y = coords
        if not 0 <= z <= 18 or not 0 <= x < 2**z or not 0 <= y < 2**z:
            raise ValueError('影像瓦片坐标越界。')
        yield coords


def serve(handler, state):
    from .offline_http import binary
    from .panel import PanelActionError
    route, _, query = handler.path.partition('?')
    try:
        with authorized(handler, state, route, query) as coords:
            requested = coords
        # Network waits must not serialize business pages, model calls or logins.
        raw, kind = fetch_tile(*requested)
        with authorized(handler, state, route, query) as coords:
            if coords != requested:
                raise PermissionError('影像上下文已变化。')
            binary(handler, 200, raw, kind)
    except (PermissionError, ValueError, KeyError, TypeError, PanelActionError):
        binary(handler, 404, b'', 'text/plain')
    except (OSError, RuntimeError):
        binary(handler, 503, b'', 'text/plain')
