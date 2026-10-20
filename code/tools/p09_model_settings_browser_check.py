"""Model settings browser check on fresh anonymous fixtures and a loopback model only."""

import argparse
import datetime
import json
import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument('--chromium', required=True)
    args = parser.parse_args()
    output = root / '.p02-preview' / ('model-settings-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S'))
    output.mkdir(parents=True)
    (output / 'tmp').mkdir()
    tempfile.tempdir = str(output / 'tmp')
    sys.dont_write_bytecode = True
    for key in list(os.environ):
        if any(word in key.upper() for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL')) or key.upper().endswith('_PROXY'):
            os.environ.pop(key, None)
    sys.path.insert(0, str(root / 'impl'))
    from playwright.sync_api import sync_playwright
    from panda_brain import model_connections as mc
    from panda_brain.panel import make_server
    from tests.test_upgrade_grounded_inquiry import GroundedInquiryTests
    fixture = GroundedInquiryTests()
    fixture.setUp()
    state = fixture.state
    assert fixture.root.resolve().is_relative_to(output.resolve())
    captured = []
    class Model(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            captured.append(body)
            content = {'ok': True} if 'connectivity test' in body['messages'][0]['content'] else {
                'answer': '应核对模拟物资记录', 'source_ids': ['S1'], 'unknown': '模拟实物状态待核'}
            raw = json.dumps({'choices': [{'message': {'content': json.dumps(content, ensure_ascii=False)}}]}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def log_message(self, *_):
            pass
    model = HTTPServer(('127.0.0.1', 0), Model)
    server = make_server(state, port=0)
    for value in (server, model):
        threading.Thread(target=value.serve_forever, daemon=True).start()
    checks, errors = [], []
    result = {'checks': checks, 'page_errors': errors, 'policy': 'anonymous synthetic fixture; local fake model; all browser non-loopback requests blocked; cloud test not performed'}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=args.chromium)
            context = browser.new_context(viewport={'width': 1440, 'height': 900})
            context.route('**/*', lambda r: r.continue_() if r.request.url.startswith('http://127.0.0.1:') else r.abort())
            page = context.new_page()
            page.on('pageerror', lambda e: errors.append(str(e)))
            def open_qa():
                trigger = page.get_by_label('展开态势问询记录与官方资料', exact=True)
                if trigger.count():
                    page.get_by_role('button', name='更多面板', exact=True).click()
                    trigger.click()
            base = f'http://127.0.0.1:{server.server_port}'
            assert context.request.post(base + '/login', form={'secret': 'p09-grounded-demo'}).ok
            token = next(c['value'] for c in context.cookies() if c['name'] == 'panel_session')
            fixture.ask('物资')
            page.goto(base + '/sand')
            open_qa()
            page.get_by_role('link', name='模型连接设置：国内云模型 / 本地算力').click()
            assert page.url.endswith('/models')
            checks.append('commander enters settings from actual sand inquiry panel')
            for vendor in ('deepseek', 'qwen', 'doubao'):
                form = page.locator('form').filter(has=page.locator('#model-vendor'))
                page.locator('#model-vendor').select_option(vendor)
                assert form.locator('[name=model]').input_value() == mc.CLOUDS[vendor][2]
                form.locator('[name=api_key]').fill('browser-synthetic-key-' + vendor)
                form.locator('[name=enabled]').check()
                form.get_by_role('button', name='保存 / 更换云端配置').click()
                assert state.model_session(token).cloud.vendor == vendor
                assert 'browser-synthetic-key' not in page.content()
            checks.append('three domestic vendor presets and key replacement work; secret never echoed')
            page.screenshot(path=str(output / 'desktop-cloud.png'))
            local = page.locator('form').filter(has=page.locator('[name=remember]'))
            local.locator('[name=base_url]').fill(f'http://127.0.0.1:{model.server_port}/v1')
            local.locator('[name=model]').fill('qwen3:8b')
            local.locator('[name=enabled]').check()
            local.locator('[name=remember]').check()
            local.get_by_role('button', name='保存 / 更换本地配置').click()
            page.get_by_role('button', name='清除 Key 并停用云端').click()
            page.get_by_role('button', name='测试本地服务', exact=True).click()
            assert '固定匿名样例连通测试通过' in page.inner_text('body')
            assert len(captured) == 1
            page.locator('[name=mode]').select_option('local')
            page.get_by_role('button', name='切换模式', exact=True).click()
            assert state.model_session(token).mode == 'local'
            assert not state.model_session(token).cloud.enabled
            checks.append('local test and selection work with cloud permission off; saved file has no token')
            assert 'api_key' not in (fixture.root / 'model-connections.json').read_text()
            page.get_by_role('heading', name='本地算力', exact=True).scroll_into_view_if_needed()
            page.screenshot(path=str(output / 'desktop-local.png'))
            page.get_by_role('link', name='返回沙盘问答', exact=True).click()
            open_qa()
            page.get_by_role('button', name='预览模型分析：物资', exact=True).click()
            page.wait_for_url('**/sand/model-preview#qa')
            page.get_by_role('heading', name='模型逐次请求预览', exact=True).wait_for()
            assert '本地模型服务' in page.inner_text('body')
            page.get_by_role('button', name='确认本次外发并提问', exact=True).click()
            page.wait_for_url('**/sand#qa')
            open_qa()
            page.get_by_text('结论：模型分析·待人工核验：应核对模拟物资记录', exact=True).wait_for()
            assert '应核对模拟物资记录' in page.inner_text('body')
            assert len(captured) == 2
            checks.append('actual preview and confirmation use local model with grounded anonymous source')
            page.goto(base + '/models')
            page.set_viewport_size({'width': 390, 'height': 844})
            assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            assert page.evaluate('document.querySelector(".lz-topbar").getBoundingClientRect().bottom <= document.querySelector(".model-settings").getBoundingClientRect().top')
            page.screenshot(path=str(output / 'mobile-settings.png'))
            checks.append('mobile form has no horizontal overflow')
            assert context.request.post(base + '/logout').ok
            assert token not in state.model_connections
            assert context.request.get(base + '/models').status == 403
            checks.append('logout clears credential state and closes settings access')
            assert not errors, errors
            browser.close()
    except Exception as exc:
        result['error'] = str(exc)
        raise
    finally:
        for value in (server, model):
            value.shutdown()
            value.server_close()
        fixture.doCleanups()
        (output / 'browser-result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print('OUTPUT=' + str(output))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
