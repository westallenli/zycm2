"""P17-P0 验收：LLM 前台出口移除 + 后台运维自检保留（五页口径纠偏）。

产品口径（DEVELOPMENT_PLAN_PAGES_V1 §1/§5.3）：参谋部、LLM、多 Agent 的
工作全部在后台完成，前台不出现「LLM 增强层」等品牌化状态卡或自检按钮。

因此本套件验证两件事：
1. 前台出口不存在——/events 无 LLM 卡片与自检表单；POST /llm/selfcheck
   路由已删除（404）；五页源码不出现禁用自述词。
2. 后台运维诊断仍可用——handle_llm_selfcheck 门禁、缺凭证 fail-closed、
   桩成功、传输失败四条路径成败均入哈希审计链且不含凭证。

纪律：单测绝不把外部网络当测试基准（doc 11 §6.4）——逐类显式摘除
LLM 凭证（discover 跑法下 tests/__init__ 不保证执行），成功与传输失败
路径用注入桩/本机不可达端口取证。
"""

from __future__ import annotations

import os
import socket
import tempfile
import threading
import unittest
import unittest.mock
import urllib.error
import urllib.parse
from pathlib import Path

from panda_brain import workbench as wb
from panda_brain.auth import GeneralEntryAuth
from panda_brain.panel import PanelState, make_server
from panda_brain.providers import OpenAICompatibleProvider
from panda_brain.settings import SystemSettings
from panda_brain.workspace import ScenarioWorkspace

_SECRET = "p23-secret"


class _StubProvider:
    """离线桩：记录调用参数，返回固定回执（成功路径取证用）。"""

    name = "stub"
    model = "stub-1"

    def __init__(self):
        self.calls = []

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        return "OK"


def _closed_local_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _strip_llm_credentials(testcase):
    """显式摘除 LLM 凭证并注册恢复（不依赖 tests/__init__ 的执行时机）。

    实测发现：discover 按文件路径加载用例时，包 __init__ 不保证先执行；
    本套件的确定性绝不允许建立在环境巧合上（doc 11 §6.4），故逐类自带防护。
    """
    saved = {k: os.environ.get(k) for k in ("LLM_API_KEY", "NVIDIA_API_KEY")}
    for key in saved:
        os.environ.pop(key, None)
    testcase.addCleanup(lambda: [
        os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None)
        for k, v in saved.items()
    ])


class SelfcheckDomainTests(unittest.TestCase):
    """域级（后台运维诊断）：门禁 / 缺凭证失败 / 桩成功 / 传输失败——成败均入哈希链。"""

    def setUp(self):
        _strip_llm_credentials(self)
        settings_patch = unittest.mock.patch(
            'panda_brain.settings.load_settings',
            return_value=SystemSettings(
                network_enabled=True, llm_provider='openai_compatible',
                llm_model='test-model', llm_base_url='https://example.invalid'))
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        self.auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p23", general_auth=self.auth),
            auth=self.auth,
            registry_path=Path(tempfile.mkdtemp()) / "reg.json",
        )

    def _unlock(self):
        self.state.workspace.unlock_general_entry(self.auth.authenticate(_SECRET))

    def test_requires_unlocked_entry_and_writes_nothing(self):
        with self.assertRaises(PermissionError):
            self.state.handle_llm_selfcheck(provider=_StubProvider())
        kinds = [e.kind for e in self.state.workspace.events]
        self.assertNotIn("LLMSelfCheck", kinds)

    def test_missing_credentials_fails_closed_and_audits(self):
        # 凭证已摘除，显式夹具指定 openai_compatible；装配必然缺凭证，
        # 与工作树配置或隔离构建临时配置无关，绝不触外网。
        self._unlock()
        result = self.state.handle_llm_selfcheck()
        self.assertFalse(result["ok"])
        self.assertIn("LLM 未装配", result["detail"])
        self.assertIn("api_key", result["detail"])
        event = [e for e in self.state.workspace.events if e.kind == "LLMSelfCheck"][-1]
        self.assertFalse(event.payload["ok"])
        self.assertEqual("(未装配)", event.payload["provider"])
        self.assertIsInstance(event.payload["latency_ms"], int)
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        self.assertIs(result, self.state.llm_selfcheck)

    def test_stub_success_records_probe_and_audits(self):
        self._unlock()
        stub = _StubProvider()
        result = self.state.handle_llm_selfcheck(provider=stub)
        self.assertTrue(result["ok"])
        self.assertEqual("OK", result["detail"])
        self.assertEqual("stub", result["provider"])
        self.assertEqual("stub-1", result["model"])
        # 最小调用契约：固定探针文案，不携带任何业务内容。
        system, prompt = stub.calls[0]
        self.assertIn("probe", system)
        self.assertIn("OK", prompt)
        event = [e for e in self.state.workspace.events if e.kind == "LLMSelfCheck"][-1]
        self.assertTrue(event.payload["ok"])
        self.assertEqual("stub", event.payload["provider"])
        self.assertEqual((), self.state.workspace.verify_audit_chain())

    def test_transport_failure_audits_reason(self):
        self._unlock()
        provider = OpenAICompatibleProvider(
            base_url=f"http://127.0.0.1:{_closed_local_port()}",
            api_key="test-key", model="test-model")
        result = self.state.handle_llm_selfcheck(provider=provider)
        self.assertFalse(result["ok"])
        self.assertTrue(result["detail"].startswith("ProviderUnavailableError"))
        self.assertLessEqual(len(result["detail"]), 300)
        event = [e for e in self.state.workspace.events if e.kind == "LLMSelfCheck"][-1]
        self.assertFalse(event.payload["ok"])
        self.assertEqual("openai_compatible", event.payload["provider"])
        self.assertEqual("test-model", event.payload["model"])
        self.assertEqual((), self.state.workspace.verify_audit_chain())
        # 审计载荷永不含凭证。
        self.assertNotIn("test-key", repr(event.payload))

    def test_provider_assembly_reports_reason_offline(self):
        # 原「状态卡」测试改写：装配诊断只供后台使用，返回 (None, 原因)。
        provider, reason = self.state._build_llm_provider()
        self.assertIsNone(provider)
        self.assertIn("api_key", reason)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


def _http(method, url, form=None, cookie=""):
    data = urllib.parse.urlencode(form).encode() if form is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(req, timeout=5) as resp:
            return resp.status, resp.headers.get("Location", ""), resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("Location", ""), exc.read().decode("utf-8")


class FrontDoorRemovedTests(unittest.TestCase):
    """HTTP e2e：LLM 前台出口必须不存在（卡片、表单、路由全部消失）。"""

    def setUp(self):
        _strip_llm_credentials(self)
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        patcher = unittest.mock.patch.object(wb, "SNAPSHOT_DIR", tmp / "snapshots")
        patcher.start()
        self.addCleanup(patcher.stop)
        auth = GeneralEntryAuth(secret=_SECRET)
        self.state = PanelState(
            ScenarioWorkspace("p23-e2e", general_auth=auth),
            auth=auth,
            registry_path=tmp / "reg.json",
            materials_path=tmp / "materials.json",
            reviews_path=tmp / "reviews.json",
        )
        self.server = make_server(self.state, port=0)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.shutdown)

    def _cmd_cookie(self):
        req = urllib.request.Request(
            f"{self.base}/login",
            data=urllib.parse.urlencode({"username": "admin", "secret": _SECRET}).encode(),
            method="POST")
        opener = urllib.request.build_opener(_NoRedirect)
        try:
            resp = opener.open(req, timeout=5)
            raw = resp.headers.get("Set-Cookie", "")
            resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.headers.get("Set-Cookie", "")
            exc.read()
        return raw.split(";")[0]

    def test_selfcheck_route_is_gone(self):
        st, _, _ = _http("POST", f"{self.base}/llm/selfcheck", form={})
        self.assertEqual(404, st)
        cookie = self._cmd_cookie()
        st, _, _ = _http("POST", f"{self.base}/llm/selfcheck", form={}, cookie=cookie)
        self.assertEqual(404, st)

    def test_events_page_has_no_llm_card_or_form(self):
        cookie = self._cmd_cookie()
        st, _, page = _http("GET", f"{self.base}/events", cookie=cookie)
        self.assertEqual(200, st)
        self.assertNotIn("LLM", page)
        self.assertNotIn("llm", page.lower())
        self.assertNotIn('id="llm"', page)
        self.assertNotIn("连通自检", page)
        # 页面渲染不得改动工作区（只读保证）。
        kinds = [e.kind for e in self.state.workspace.events]
        self.assertNotIn("LLMSelfCheck", kinds)


if __name__ == "__main__":
    unittest.main()
