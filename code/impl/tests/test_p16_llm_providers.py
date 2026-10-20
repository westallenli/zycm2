"""P16-T22 验收：LLM 联网传输层实装（providers.py 三槽 + settings 扩展）。

纪律：单测用本地假 OpenAI 兼容服务器取证——成功路径/HTTP 401/坏 JSON/
超时/缺凭证全部确定性覆盖，绝不把外部网络当测试基准（doc 11 §6.4）。
"""

from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from panda_brain.providers import (
    LocalNimProvider,
    NvidiaNimProvider,
    OpenAICompatibleProvider,
    ProviderUnavailableError,
    _chat_completions,
    create_provider,
)
from panda_brain.settings import SystemSettings, load_settings


class _FakeOpenAI(BaseHTTPRequestHandler):
    """可编排响应的假端点：记录请求体，按 mode 返回。"""

    mode = "ok"
    requests: list[dict] = []
    sleep_seconds = 0.0

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length))
        _FakeOpenAI.requests.append(
            {"path": self.path, "auth": self.headers.get("Authorization", ""), **body})
        if _FakeOpenAI.sleep_seconds:
            import time
            time.sleep(_FakeOpenAI.sleep_seconds)
        if self.headers.get("Authorization") != "Bearer good-key":
            payload = json.dumps({"status": 401, "detail": "Authentication failed"})
            self.send_response(401)
            self.end_headers()
            self.wfile.write(payload.encode())
            return
        if _FakeOpenAI.mode == "bad_json":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{not-json")
            return
        if _FakeOpenAI.mode == "missing_choices":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"object": "chat.completion"}).encode())
            return
        reply = {
            "choices": [{"message": {
                "role": "assistant",
                "content": "SUMMARY: polished\nRISKS:\n- ok\nEVIDENCE: e1"}}],
        }
        raw = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):  # 静默
        pass


class FakeServerBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), _FakeOpenAI)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        _FakeOpenAI.mode = "ok"
        _FakeOpenAI.requests = []
        _FakeOpenAI.sleep_seconds = 0.0


class TransportTests(FakeServerBase):
    def test_success_returns_content_and_sends_contract(self):
        out = _chat_completions(
            endpoint=f"{self.base}/chat/completions", api_key="good-key",
            model="test-model", system="SYS", prompt="USER", timeout_seconds=5)
        self.assertIn("SUMMARY:", out)
        sent = _FakeOpenAI.requests[-1]
        self.assertEqual("/v1/chat/completions", sent["path"])
        self.assertEqual("Bearer good-key", sent["auth"])
        self.assertEqual("test-model", sent["model"])
        self.assertEqual(0, sent["temperature"])          # 确定性优先（doc 11 §6.6）
        self.assertEqual("SYS", sent["messages"][0]["content"])
        self.assertEqual("USER", sent["messages"][1]["content"])

    def test_http_401_surfaces_as_unavailable(self):
        with self.assertRaises(ProviderUnavailableError) as ctx:
            _chat_completions(
                endpoint=f"{self.base}/chat/completions", api_key="wrong",
                model="m", system="s", prompt="p", timeout_seconds=5)
        self.assertIn("HTTP 401", str(ctx.exception))

    def test_bad_json_rejected(self):
        _FakeOpenAI.mode = "bad_json"
        with self.assertRaises(ProviderUnavailableError) as ctx:
            _chat_completions(
                endpoint=f"{self.base}/chat/completions", api_key="good-key",
                model="m", system="s", prompt="p", timeout_seconds=5)
        self.assertIn("malformed", str(ctx.exception))

    def test_missing_choices_rejected(self):
        _FakeOpenAI.mode = "missing_choices"
        with self.assertRaises(ProviderUnavailableError):
            _chat_completions(
                endpoint=f"{self.base}/chat/completions", api_key="good-key",
                model="m", system="s", prompt="p", timeout_seconds=5)

    def test_timeout_surfaces_as_unavailable(self):
        _FakeOpenAI.sleep_seconds = 1.0
        with self.assertRaises(ProviderUnavailableError) as ctx:
            _chat_completions(
                endpoint=f"{self.base}/chat/completions", api_key="good-key",
                model="m", system="s", prompt="p", timeout_seconds=0.2)
        self.assertIn("LLM request failed", str(ctx.exception))


class SlotTests(FakeServerBase):
    def test_nvidia_slot_against_fake_endpoint(self):
        provider = create_provider(
            {"provider": "nvidia", "api_key": "good-key",
             "base_url": self.base, "model": "meta/test"},
            system_settings=SystemSettings(network_enabled=True))
        self.assertIsInstance(provider, NvidiaNimProvider)
        out = provider.complete(system="S", prompt="P")
        self.assertIn("SUMMARY:", out)

    def test_local_nim_and_openai_compatible_slots(self):
        local = create_provider(
            {"provider": "local_nim", "base_url": self.base,
             "model": "qwen-test", "api_key": "good-key"},
            system_settings=SystemSettings(network_enabled=True))
        self.assertIsInstance(local, LocalNimProvider)
        self.assertIn("SUMMARY:", local.complete(system="s", prompt="p"))
        oc = create_provider(
            {"provider": "openai_compatible", "base_url": self.base,
             "model": "deepseek-test", "api_key": "good-key"},
            system_settings=SystemSettings(network_enabled=True))
        self.assertIsInstance(oc, OpenAICompatibleProvider)
        self.assertIn("SUMMARY:", oc.complete(system="s", prompt="p"))

    def test_incomplete_openai_compatible_refused_at_factory(self):
        with self.assertRaises(ProviderUnavailableError):
            create_provider(
                {"provider": "openai_compatible", "base_url": self.base},
                system_settings=SystemSettings(network_enabled=True))


class SettingsExtensionTests(unittest.TestCase):
    def test_new_keys_roundtrip_and_unknown_still_rejected(self):
        import tempfile
        from pathlib import Path
        tmp = Path(tempfile.mkdtemp()) / "settings.yaml"
        tmp.write_text(
            "network_enabled: true\nllm_provider: nvidia\nllm_model: m1\n"
            "llm_base_url: https://example.invalid/v1\nllm_timeout_seconds: 12\n",
            encoding="utf-8")
        s = load_settings(tmp)
        self.assertTrue(s.network_enabled)
        self.assertEqual("nvidia", s.llm_provider)
        self.assertEqual("m1", s.llm_model)
        self.assertEqual(12.0, s.llm_timeout_seconds)
        bad = Path(tempfile.mkdtemp()) / "bad.yaml"
        bad.write_text("api_key: leaked-in-file\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            load_settings(bad)   # 凭证类键永远不允许进配置文件


if __name__ == "__main__":
    unittest.main()
