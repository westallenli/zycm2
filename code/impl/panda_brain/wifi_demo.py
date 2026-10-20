"""Explicit same-subnet demo listener; the normal desktop listener stays local."""
import ipaddress,threading
from http.server import ThreadingHTTPServer
from html import escape as esc
from . import runtime_access

PRIVATE=tuple(ipaddress.ip_network(v) for v in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))

def network(address,prefix,port):
    ip=ipaddress.ip_address(address)
    if ip.version!=4 or not any(ip in block for block in PRIVATE) or type(prefix) is not int or not 16<=prefix<=30 or type(port) is not int or not 1024<=port<=65535:
        raise ValueError('手机演示须指定本机内网IPv4、16至30位前缀和1024以上端口。')
    net=ipaddress.ip_network(f'{ip}/{prefix}',strict=False)
    if ip in {net.network_address,net.broadcast_address} or not any(net.subnet_of(block) for block in PRIVATE):raise ValueError('手机演示网段无效。')
    return net

def start(state,address,prefix,port):
    net=network(address,prefix,port);origin=f'http://{address}:{port}'
    if not getattr(state,'resource_demo',None):raise ValueError('同网入口仅用于本次明确的演示数据。')
    from .panel import make_handler
    base=make_handler(state)
    class Handler(base):
        def allowed(self):
            if self.headers.get_all('Host')!=[f'{address}:{port}'] or not self.path.startswith('/') or self.path.startswith('//'):
                self._respond(400,'<h1>请使用电脑提供的手机地址</h1>');return False
            if self.command=='POST':
                origins=[o for o in self.headers.get_all('Origin',[]) if o and o!='null']
                # 演示放宽：只要来源主机就是本机内网地址就放行（App 内嵌浏览器、
                # 微信内置浏览器常带不同端口或 null）。Host 校验仍然严格。
                if origins and not all(origin.split('://')[-1].split(':')[0]==address for origin in origins):
                    import sys
                    print(f"来源不匹配：收到 {origins}，期望 {origin}",file=sys.stderr)
                    self._respond(403,'<h1>请求来源不匹配，请从队长入口操作</h1>');return False
                if self.headers.get('Sec-Fetch-Site')=='cross-site':self._respond(403,'<h1>请从本系统页面操作</h1>');return False
                length=self.headers.get('Content-Length','')
                if self.headers.get('Transfer-Encoding') or not length.isascii() or not length.isdigit() or int(length)>16*1024*1024:
                    self._respond(413,'<h1>请求大小或格式不正确</h1>');return False
            return True
        def do_GET(self):
            if self.allowed():return super().do_GET()
        def do_POST(self):
            if self.allowed():return super().do_POST()
        def send_header(self,key,value):
            if key.lower()=='set-cookie' and 'samesite=' not in value.lower():value+='; SameSite=Lax'
            return super().send_header(key,value)
    class Server(ThreadingHTTPServer):
        daemon_threads=True
        request_queue_size=32
        def verify_request(self,request,client_address):
            try:return ipaddress.ip_address(client_address[0]) in net
            except ValueError:return False
    with runtime_access.wifi_listener(address,port):server=Server((address,port),Handler)
    runtime_access.install_headers(server);state.wifi_origin=origin
    thread=threading.Thread(target=server.serve_forever,kwargs=dict(poll_interval=.3),daemon=True)
    thread.start()
    return server,thread

def info_page(state):
    from .android_api import install_page
    native=install_page(state)
    if native:return native
    from .event_qr import entry_url,link_svg
    from .captain_mobile import page
    origin=getattr(state,'wifi_origin','')
    if not origin:return page('手机访问','<section class="lz-card"><h1>尚未启用手机入口</h1><p>请使用“启动同网手机演示.ps1”。</p></section>')
    eid=state.active_event_id
    qrs=[q for q in state.registry.qrs.values() if q.event_id==eid and state.registry.qr_status(q)=='有效' and state.registry.events[eid].status in {'active','upgraded'}]
    url=entry_url(state,eid,qrs[-1].qr_id) if qrs else origin+'/mobile'
    body='<section class="lz-card"><h1>队长手机访问</h1><p>电脑和手机连接同一 Wi-Fi，电脑保持运行。</p><p><a href="'+esc(url,quote=True)+'">'+esc(url)+'</a></p>'+link_svg(url)+'<p>手机相机或扫一扫打开上面的地址，使用队长账号登录。扫码不自动确认到场。</p><p>此为同网演示，手机断网时暂不能提交；不需要安装 App。</p></section>'
    return page('手机访问',body)
