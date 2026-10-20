"""Domestic cloud and explicitly configured local models; credentials stay in memory."""

from __future__ import annotations

import html
import ipaddress
import json
import re
import secrets
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path


CLOUDS = {
    'deepseek': ('DeepSeek', 'https://api.deepseek.com', 'deepseek-flash'),
    'qwen': ('阿里千问', 'https://dashscope.aliyuncs.com/compatible-mode/v1', 'qwen3.7-plus'),
    'doubao': ('豆包·火山方舟', 'https://ark.cn-beijing.volces.com/api/v3', 'doubao-seed-2-1-pro-260628'),
}
LOCAL_NETWORKS = tuple(ipaddress.ip_network(x) for x in (
    '127.0.0.0/8', '10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16', '::1/128', 'fc00::/7'))


def validate_url(value: str, vendor: str) -> str:
    value = value.strip().rstrip('/')
    try:
        parsed = urllib.parse.urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError('模型服务地址格式不正确。') from None
    if (not value or len(value) > 400 or not value.isascii()
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or '%' in value
            or any(c.isspace() for c in value) or '\\' in value):
        raise ValueError('服务地址不能包含账号、查询参数或特殊转义。')
    host = (parsed.hostname or '').lower()
    if vendor != 'local':
        if vendor not in CLOUDS:
            raise ValueError('仅支持列表中的国内云厂商。')
        allowed = {
            'deepseek': host == 'api.deepseek.com' and parsed.path in ('', '/v1'),
            'qwen': (host == 'dashscope.aliyuncs.com' or bool(re.fullmatch(
                r'[a-z0-9][a-z0-9-]*\.cn-beijing\.maas\.aliyuncs\.com', host)))
                and parsed.path == '/compatible-mode/v1',
            'doubao': host == 'ark.cn-beijing.volces.com' and parsed.path == '/api/v3',
        }
        if parsed.scheme != 'https' or port not in (None, 443) or not allowed[vendor]:
            raise ValueError('请选择该厂商官方国内服务地址。')
    else:
        if host == 'localhost':
            host = '127.0.0.1'
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            raise ValueError('本地服务请填写本机或内网 IP，不使用公网域名。') from None
        if (parsed.scheme not in ('http', 'https') or not any(address in n for n in LOCAL_NETWORKS)
                or not re.fullmatch(r'(?:/[A-Za-z0-9_-]+)*', parsed.path)
                or port == 0):
            raise ValueError('本地服务仅允许本机或私有内网地址及普通接口路径。')
        authority = f'[{host}]' if ':' in host else host
        if port is not None:
            authority += f':{port}'
        value = urllib.parse.urlunsplit((parsed.scheme, authority, parsed.path, '', ''))
    return value


@dataclass(frozen=True)
class Profile:
    vendor: str
    base_url: str
    model: str
    api_key: str = field(default='', repr=False)
    enabled: bool = False
    timeout: float = 30.0

    @property
    def endpoint(self) -> str:
        return self.base_url + '/chat/completions'

    @property
    def label(self) -> str:
        return '本地模型服务' if self.vendor == 'local' else CLOUDS[self.vendor][0]


def profile(vendor: str, base_url: str, model: str, key: str = '',
            enabled: bool = False, timeout: float = 30) -> Profile:
    if vendor != 'local' and vendor not in CLOUDS:
        raise ValueError('仅支持列表中的国内云厂商。')
    base_url = base_url.strip() or (CLOUDS[vendor][1] if vendor != 'local' else 'http://127.0.0.1:11434/v1')
    base_url = validate_url(base_url, vendor)
    model = model.strip()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,159}', model):
        raise ValueError('请填写有效的模型名称或模型接入点 ID。')
    if vendor == 'local' and (':cloud' in model.lower() or '-cloud' in model.lower()):
        raise ValueError('本地模式不能选择云托管模型。')
    key = key.strip()
    if len(key) > 4096 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('Key 或访问令牌格式不正确。')
    try:
        timeout = float(timeout)
    except (ValueError, TypeError):
        raise ValueError('超时应为 1 至 60 秒。') from None
    if not 1 <= timeout <= 60:
        raise ValueError('超时应为 1 至 60 秒。')
    return Profile(vendor, base_url, model, key, enabled, timeout)


def default_cloud() -> Profile:
    return Profile('deepseek', CLOUDS['deepseek'][1], CLOUDS['deepseek'][2])


@dataclass(frozen=True)
class Session:
    cloud: Profile = field(default_factory=default_cloud)
    local: Profile | None = None
    mode: str = 'cloud'
    revision: int = 0
    csrf: str = field(default_factory=lambda: secrets.token_urlsafe(24), repr=False)
    cloud_status: str = '未测试'
    local_status: str = '未测试；实际本地计算尚未验收'

    @property
    def selected(self) -> Profile:
        if self.mode == 'local':
            if self.local is None:
                raise ValueError('本地模型尚未配置，请先进入模型连接设置。')
            return self.local
        return self.cloud


def load_local(path: Path) -> Profile | None:
    if not path.is_file():
        return None
    if path.stat().st_size > 4096:
        raise ValueError('已保存的本地配置过大，请重新配置。')
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(raw, dict) or set(raw) != {'base_url', 'model', 'timeout'}:
            raise ValueError()
        return profile('local', raw['base_url'], raw['model'], timeout=raw['timeout'])
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ValueError('已保存的本地配置无效，请重新配置。') from None


def save_local(path: Path, value: Profile) -> None:
    """Only the non-secret local profile is durable; permission must be enabled per session."""
    raw = {'base_url': value.base_url, 'model': value.model, 'timeout': value.timeout}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + secrets.token_hex(8) + '.tmp')
    try:
        temp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def update(session: Session, fields: dict[str, str], local_path: Path) -> Session:
    action = fields.get('action', '')
    if action == 'save-cloud':
        vendor = fields.get('vendor', '')
        key = fields.get('api_key', '')
        if not key and vendor == session.cloud.vendor:
            key = session.cloud.api_key
        value = profile(vendor, fields.get('base_url', ''), fields.get('model', ''), key,
                        fields.get('enabled') == 'on', fields.get('timeout', '30'))
        return replace(session, cloud=value, revision=session.revision + 1, cloud_status='配置已更新，未测试')
    if action == 'save-local':
        value = profile('local', fields.get('base_url', ''), fields.get('model', ''),
                        fields.get('api_key', '') or (session.local.api_key if session.local else ''),
                        fields.get('enabled') == 'on', fields.get('timeout', '30'))
        if fields.get('remember') == 'on':
            save_local(local_path, value)
        return replace(session, local=value, revision=session.revision + 1, local_status='配置已更新，未测试；本地计算尚未验收')
    if action == 'select-mode':
        mode = fields.get('mode')
        if mode not in ('cloud', 'local'):
            raise ValueError('请选择云端或本地模式。')
        if mode == 'local' and session.local is None:
            raise ValueError('请先保存本地模型配置。')
        return replace(session, mode=mode, revision=session.revision + 1)
    if action in ('clear-cloud', 'clear-local'):
        mode = action.split('-')[1]
        value = getattr(session, mode)
        if value is not None:
            value = replace(value, api_key='', enabled=False)
        return replace(session, **{mode: value, mode + '_status': '凭证已清除，调用已停用'}, revision=session.revision + 1)
    if action == 'forget-local':
        local_path.unlink(missing_ok=True)
        return replace(session, local=None, mode='cloud', revision=session.revision + 1, local_status='本地配置已删除')
    raise ValueError('未知模型设置操作。')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ConnectionUnavailable(ValueError):
    """Sanitized user-facing transport failure; never includes a response body."""


@dataclass(frozen=True)
class SafeProvider:
    connection: Profile

    def probe(self) -> None:
        """Check service authentication without queuing or billing a completion."""
        p = self.connection
        validate_url(p.base_url, p.vendor)
        if not p.enabled:
            raise ConnectionUnavailable('当前模型调用许可已关闭。')
        if p.vendor != 'local' and not p.api_key:
            raise ConnectionUnavailable('当前云厂商未配置 Key。')
        headers = {'Accept': 'application/json'}
        if p.api_key:
            headers['Authorization'] = 'Bearer ' + p.api_key
        request = urllib.request.Request(p.base_url + '/models', headers=headers, method='GET')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        from .runtime_access import model_egress
        try:
            with model_egress(p), opener.open(request, timeout=min(p.timeout, 10)) as response:
                raw = response.read(256 * 1024 + 1)
                if len(raw) > 256 * 1024:
                    raise ValueError('large response')
            value = json.loads(raw)
            if (not isinstance(value, dict) or not isinstance(value.get('data'), list)
                    or any(not isinstance(item, dict) or not isinstance(item.get('id'), str)
                           for item in value['data'])):
                raise ValueError('invalid model list')
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            hint = ('Key 鉴权失败，请核对或更换 Key。' if code in (401, 403) else
                    '账户余额不足，请在 DeepSeek 平台核对余额。' if code == 402 else
                    '服务繁忙或请求受限，请稍后再试。' if code == 429 else
                    'DeepSeek 服务暂不可用，请稍后再试。' if 500 <= code < 600 else
                    '服务接口返回错误，请核对配置。')
            raise ConnectionUnavailable(f'HTTP {code}；{hint}') from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, 'reason', exc)
            if isinstance(reason, TimeoutError):
                hint = '等待 DeepSeek 服务响应超时；不能判定Key错误，请稍后再试。'
            elif isinstance(reason, ssl.SSLError):
                hint = '无法建立安全连接，请检查电脑时间、证书或网络。'
            elif isinstance(reason, socket.gaierror):
                hint = 'DeepSeek 域名解析失败，请检查网络或 DNS。'
            else:
                hint = '无法连接 DeepSeek 服务，请检查网络；不能判定Key错误。'
            raise ConnectionUnavailable(hint) from None
        except (ValueError, KeyError, TypeError, UnicodeError):
            raise ConnectionUnavailable('服务响应格式异常，尚未验证连接。') from None

    def complete(self, *, system: str, prompt: str) -> str:
        p = self.connection
        validate_url(p.base_url, p.vendor)
        if not p.enabled:
            raise ConnectionUnavailable('当前模型调用许可已关闭。')
        if p.vendor != 'local' and not p.api_key:
            raise ConnectionUnavailable('当前云厂商未配置 Key。')
        payload = {'model': p.model, 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': prompt}],
            'temperature': 0, 'max_tokens': 1024, 'stream': False,
            'response_format': {'type': 'json_object'}}
        headers = {'Content-Type': 'application/json'}
        if p.api_key:
            headers['Authorization'] = 'Bearer ' + p.api_key
        request = urllib.request.Request(p.endpoint, data=json.dumps(payload).encode('utf-8'), headers=headers, method='POST')
        # Direct transport: no inherited public proxy, redirects or implicit cloud fallback.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        from .runtime_access import model_egress
        try:
            with model_egress(p), opener.open(request, timeout=p.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError('模型响应超过限制。')
            content = json.loads(raw)['choices'][0]['message']['content']
            if not isinstance(content, str) or not content.strip():
                raise ValueError('模型返回空答案。')
            return content
        except urllib.error.HTTPError as exc:
            code = exc.code
            exc.close()
            hint = '鉴权失败，请更换 Key 或访问令牌。' if code in (401, 403) else '请核对模型、服务地址及服务状态。'
            raise ConnectionUnavailable(f'模型请求失败（HTTP {code}）；{hint}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ConnectionUnavailable('模型服务无法连接或请求超时；输入已保留，可检查服务或切换到已配置的本地模式。') from None
        except (ValueError, KeyError, IndexError, TypeError, UnicodeError):
            raise ConnectionUnavailable('模型响应无效或超过限制，请核对所选模型的接口能力。') from None


def render(session: Session, *, error: str = '', notice: str = '') -> str:
    esc = html.escape
    hidden = '<input type="hidden" name="csrf" value="' + esc(session.csrf) + '">'
    def form(body: str) -> str:
        return '<form method="post" action="/models">' + hidden + body + '</form>'
    def button(action: str, label: str) -> str:
        return '<button class="lz-btn lz-btn2" name="action" value="' + action + '">' + label + '</button>'
    def fields(p: Profile, cloud: bool) -> str:
        choices = ''.join('<option value="' + k + '"' + (' selected' if k == p.vendor else '') + '>' + esc(v[0]) + '</option>' for k, v in CLOUDS.items())
        select = '<label>国内厂商 <select id="model-vendor" name="vendor">' + choices + '</select></label>' if cloud else ''
        return ('<label><b>' + ('API Key' if cloud else '访问令牌（服务未设鉴权时留空）')
                + '</b> <input type="password" name="api_key" autocomplete="new-password" maxlength="4096" placeholder="在这里粘贴Key；已配置时留空保留"></label>'
                '<p>凭证：' + ('已配置' if p.api_key else '未配置') + '。只保存在当前会话，退出或重启后清除。</p>'
                + select + '<label>服务地址 <input name="base_url" value="' + esc(p.base_url) + '" maxlength="400" required></label>'
                '<label>模型名称 / 接入点 ID <input name="model" value="' + esc(p.model) + '" maxlength="160" required></label>'
                '<label>超时（秒） <input type="number" name="timeout" min="1" max="60" value="' + str(p.timeout) + '"></label>'
                '<label><input type="checkbox" name="enabled"' + (' checked' if p.enabled else '') + '> '
                + ('允许当前会话使用公网云模型' if cloud else '允许当前会话使用本机 / 内网模型') + '</label>')
    local = session.local or Profile('local', 'http://127.0.0.1:11434/v1', '')
    cloud_body = form(fields(session.cloud, True) + button('save-cloud', '保存 / 更换云端配置'))
    cloud_body += form(button('test-cloud', '测试云端连接') + button('clear-cloud', '清除 Key 并停用云端'))
    local_body = form(fields(local, False) + '<label><input type="checkbox" name="remember"> 保存地址和模型名（不保存令牌或调用许可）</label>' + button('save-local', '保存 / 更换本地配置'))
    local_body += form(button('test-local', '测试本地服务') + button('clear-local', '清除令牌并停用本地') + button('forget-local', '删除本地配置'))
    modes = ''.join('<option value="' + mode + '"' + (' selected' if session.mode == mode else '') + '>' + label + '</option>' for mode, label in (('cloud', '国内云模型'), ('local', '本地算力')))
    defaults = {k: {'base': v[1], 'model': v[2]} for k, v in CLOUDS.items()}
    script = '<script>const presets=' + json.dumps(defaults) + ';document.getElementById("model-vendor").addEventListener("change",function(){const f=this.form,p=presets[this.value];f.elements.base_url.value=p.base;f.elements.model.value=p.model;f.elements.api_key.value="";});</script>'
    style = '<style>.model-settings{max-width:1100px;margin:24px auto}.model-settings label{display:block;margin:12px 0}.model-settings input:not([type=checkbox]):not([type=hidden]),.model-settings select{display:block;width:100%;max-width:650px;box-sizing:border-box;padding:10px;margin-top:6px}.model-settings button{margin:6px 8px 6px 0}.model-settings h2{margin-top:28px}.model-settings [role=alert]{padding:12px;color:#a22;background:#fff0f0}.model-settings [role=status]{padding:12px;background:#edf7ef}</style>'
    style += '<style>@media(max-width:620px){.lz-model-page .lz-topbar{height:auto;flex-wrap:wrap;padding:12px 16px;gap:8px}.lz-model-page .lz-topbar>b{width:100%}.lz-model-page .lz-topbar>a{white-space:nowrap;padding:8px 2px}.lz-model-page .lz-topbar .spacer{display:none}.lz-model-page .lz-topuser{width:100%;justify-content:flex-end}.lz-model-page .lz-wrap{padding:16px}.lz-model-page .model-settings{margin:12px auto}}</style>'
    return (style + '<section class="lz-card model-settings"><h1>大模型配置（Key）</h1><p>模型连接设置</p><p><a href="/sand#qa">返回沙盘问答</a></p>'
            + ('<p role="alert">' + esc(error) + '</p>' if error else '')
            + ('<p role="status">' + esc(notice) + '</p>' if notice else '')
            + '<p>先保存配置，再测试、选择运行模式。填写 Key 或保存配置不会发送事件资料。</p>'
            + form('<label>当前模式 <select name="mode">' + modes + '</select></label>' + button('select-mode', '切换模式'))
            + '<h2>国内云模型</h2><p>' + esc(session.cloud_status) + '</p>' + cloud_body
            + '<h2>本地算力</h2><p>' + esc(session.local_status) + '</p>' + local_body
            + '<p>本地地址是应用服务器可访问的地址；localhost 指应用服务器自身。模型文件与推理服务需提前准备，Ollama 需关闭云功能。本地连接成功不证明实际计算全程离线，也不证明答案正确。</p>'
            + '<p>连接测试只发送固定匿名测试文本，可能产生云端调用费用。断网后可手动切换本地；恢复网络不会自动切回云端。</p></section>' + script)
