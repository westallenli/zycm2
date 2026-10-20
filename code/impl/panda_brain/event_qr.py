"""Local QR symbols for real registry IDs; encoding grants no login or entry authority."""
from functools import lru_cache
from html import escape
import re

from ._vendor_qrcodegen import QrCode
from urllib.parse import urlencode,urlsplit,parse_qs

def entry_url(state,event_id,qr_id):
    origin=getattr(state,'wifi_origin','')
    return origin+'/mobile?'+urlencode(dict(event_id=event_id,qr_id=qr_id)) if origin else None


def link_svg(url):
    from .wifi_demo import network
    target=urlsplit(url)
    if target.scheme!='http' or target.username or target.password or target.fragment or target.path!='/mobile':raise ValueError('手机入口须为已配置的内网地址。')
    network(target.hostname,24,target.port)
    fields=parse_qs(target.query,keep_blank_values=True)
    if fields:
        if set(fields)!={'event_id','qr_id'} or any(len(v)!=1 for v in fields.values()):raise ValueError('手机二维码参数无效。')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}',fields['event_id'][0]) or not re.fullmatch(r'qr-[0-9]{1,64}',fields['qr_id'][0]):raise ValueError('手机二维码编号无效。')
    return _svg(url,'队长手机访问二维码')


@lru_cache(maxsize=128)
def qr_svg(qr_id: str, *, entry_url=None) -> str:
    """Encode the registered ID or explicitly configured LAN entry URL.

    Never encode credentials, identity, or a public URL.
    Status must be checked on every rendering/request by the caller.
    """
    if not isinstance(qr_id, str) or not re.fullmatch(r'qr-[0-9]{1,64}', qr_id):
        raise ValueError('无效事件二维码编号')
    if entry_url:
        link_svg(entry_url)
        if parse_qs(urlsplit(entry_url).query).get('qr_id')!=[qr_id]:raise ValueError('手机入口与事件码不一致。')
    return _svg(entry_url or qr_id,'事件二维码 '+qr_id)


def _svg(text,label):
    code = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    border = 4
    size = code.get_size() + border * 2
    path = ' '.join(f'M{x+border},{y+border}h1v1h-1z'
                    for y in range(code.get_size()) for x in range(code.get_size())
                    if code.get_module(x, y))
    return (f'<svg class="event-qr-symbol" xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {size} {size}" width="168" height="168" role="img" '
            f'aria-label="{escape(label)}" shape-rendering="crispEdges" '
            'style="display:block;flex:none;max-width:100%;height:auto;background:white">'
            '<rect width="100%" height="100%" fill="#fff"/>'
            f'<path d="{path}" fill="#000"/></svg>')
