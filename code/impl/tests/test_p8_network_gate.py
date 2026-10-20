"""P8-T5 acceptance tests: network gate and settings (doc 11 §7 P8-T5)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from panda_brain.providers import (
    NvidiaNimProvider,
    ProviderUnavailableError,
    create_provider,
)
from panda_brain.settings import SystemSettings, load_settings


class P8NetworkGateTests(unittest.TestCase):
    def test_default_settings_are_offline(self) -> None:
        # 安全基线：无配置文件时默认离线（P16-T22 起仓库文件由指挥员拍板开启）。
        self.assertFalse(SystemSettings().network_enabled)

    def test_reviewed_settings_file_loads(self) -> None:
        settings = load_settings()  # 仓库文件：2026-08-24 指挥员拍板接入 DeepSeek
        self.assertTrue(settings.network_enabled)
        self.assertEqual("openai_compatible", settings.llm_provider)
        self.assertEqual("https://api.deepseek.com", settings.llm_base_url)
        self.assertEqual("deepseek-chat", settings.llm_model)
        # 配置文件中永不允许出现凭证键
        raw = Path(load_settings.__globals__["DEFAULT_SETTINGS_PATH"]).read_text(encoding="utf-8")
        self.assertNotIn("sk-", raw)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text(yaml.safe_dump({"network_enabled": True}), encoding="utf-8")
            self.assertTrue(load_settings(path).network_enabled)

    def test_unknown_settings_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "settings.yaml"
            path.write_text(yaml.safe_dump({"network_enabled": True, "auto_decide": True}), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_settings(path)

    def test_gate_closed_refuses_network_slots(self) -> None:
        closed = SystemSettings(network_enabled=False)
        for kind in ("nvidia", "local_nim"):
            with self.assertRaises(ProviderUnavailableError) as ctx:
                create_provider({"provider": kind}, system_settings=closed)
            self.assertIn("network switch is off", str(ctx.exception))

    def test_gate_open_without_credentials_reports_by_name(self) -> None:
        import os

        opened = SystemSettings(network_enabled=True)
        saved = os.environ.pop("NVIDIA_API_KEY", None)
        try:
            with self.assertRaises(ProviderUnavailableError) as ctx:
                create_provider({"provider": "nvidia"}, system_settings=opened)
            self.assertIn("NVIDIA_API_KEY", str(ctx.exception))

            with self.assertRaises(ProviderUnavailableError) as ctx:
                create_provider({"provider": "local_nim"}, system_settings=opened)
            self.assertIn("base_url", str(ctx.exception))
        finally:
            if saved is not None:
                os.environ["NVIDIA_API_KEY"] = saved

    def test_gate_open_with_credentials_constructs_real_provider(self) -> None:
        """P16-T22：门开+凭证 → 真实传输就绪；指向不可达地址时确定性失败。"""
        opened = SystemSettings(network_enabled=True)
        provider = create_provider(
            {"provider": "nvidia", "api_key": "test-key",
             "base_url": "http://127.0.0.1:1/v1", "timeout_seconds": 1},
            system_settings=opened,
        )
        self.assertIsInstance(provider, NvidiaNimProvider)
        with self.assertRaises(ProviderUnavailableError) as ctx:
            provider.complete(system="s", prompt="p")
        self.assertIn("LLM request failed", str(ctx.exception))

    def test_no_system_settings_keeps_legacy_behavior(self) -> None:
        """Existing call sites without the gate keep working (P7 behavior)."""
        provider = create_provider({"provider": "nvidia"})
        self.assertIsInstance(provider, NvidiaNimProvider)


if __name__ == "__main__":
    unittest.main()
