"""LLM provider layer (doc 11 §7 P7-T2).

The LLM is an enhancement layer, never a dependency (offline-first). The
factory's default is the null provider: with no key configured, the existing
rule-based staff council IS the system, unchanged.

P16-T22：联网传输实装（仍受 P8-T5 联网开关门控）。三个网络槽共用一条
OpenAI 兼容 chat/completions 传输（纯标准库 urllib）：

- ``nvidia``        —— NVIDIA NIM 云端（integrate.api.nvidia.com），需 API key；
- ``local_nim``     —— 本机/内网 OpenAI 兼容端点，需 base_url；
- ``openai_compatible`` —— 其他厂商的 OpenAI 兼容端点（base_url + api_key）。

纪律：任何传输失败（网络/鉴权/超时/坏响应）一律抛 ProviderUnavailableError，
上层 Enhancer 会留审计事件并回落规则参谋部——LLM 永不阻塞指挥流程。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .settings import SystemSettings


class ProviderUnavailableError(RuntimeError):
    """Raised when no LLM is available or the slot is not implemented yet."""


NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "meta/llama-3.1-70b-instruct"
DEFAULT_TIMEOUT_SECONDS = 30.0


def _chat_completions(
    *, endpoint: str, api_key: str, model: str,
    system: str, prompt: str, timeout_seconds: float,
    response_format: dict | None = None,
) -> str:
    """OpenAI 兼容 chat/completions 单次调用；失败一律抛不可用异常。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        # 确定性优先（doc 11 §6.6）：温度 0，输出仅作措辞增强。
        "temperature": 0,
        "max_tokens": 1024,
        "stream": False,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            body = resp.read().decode("utf-8")
            status = resp.status
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        raise ProviderUnavailableError(
            f"LLM request failed: HTTP {exc.code}{'; ' + detail if detail else ''}") from exc
    except Exception as exc:   # URLError / Timeout / 连接失败等
        raise ProviderUnavailableError(
            f"LLM request failed: {type(exc).__name__}: {exc}") from exc
    if status != 200:
        raise ProviderUnavailableError(f"LLM request failed: HTTP {status}")
    try:
        data = json.loads(body)
        content = data["choices"][0]["message"]["content"]
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ProviderUnavailableError(
            f"LLM reply malformed: {type(exc).__name__}") from exc
    if not isinstance(content, str) or not content.strip():
        raise ProviderUnavailableError("LLM reply malformed: empty content")
    return content


@runtime_checkable
class AdvisorLLMProvider(Protocol):
    """Anything that can answer a staff-enhancement request."""

    name: str

    def complete(self, *, system: str, prompt: str) -> str: ...


class NullProvider:
    """No LLM present. Calling it is a programming error upstream of the factory."""

    name = "null"

    def complete(self, *, system: str, prompt: str) -> str:
        raise ProviderUnavailableError(
            "No LLM provider configured; the rule-based council runs as-is."
        )


@dataclass
class MockProvider:
    """Scripted offline provider for tests; records every call it receives."""

    script: dict[str, str] = field(default_factory=dict)
    default_reply: str = "[mock] no scripted reply matched"
    calls: list[tuple[str, str]] = field(default_factory=list)
    name: str = "mock"

    def complete(self, *, system: str, prompt: str) -> str:
        self.calls.append((system, prompt))
        for needle, reply in self.script.items():
            if needle in prompt or needle in system:
                return reply
        return self.default_reply


@dataclass(frozen=True)
class NvidiaNimProvider:
    """NVIDIA NIM 云端（OpenAI 兼容）。无 key 时保持惰性：调用即报缺凭证。"""

    api_key: str = ""
    model: str = NVIDIA_DEFAULT_MODEL
    base_url: str = NVIDIA_DEFAULT_BASE_URL
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    name: str = "nvidia"

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.api_key:
            raise ProviderUnavailableError(
                "Provider 'nvidia' needs credentials: set api_key or NVIDIA_API_KEY.")
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        return _chat_completions(
            endpoint=endpoint, api_key=self.api_key, model=self.model,
            system=system, prompt=prompt, timeout_seconds=self.timeout_seconds)

    @staticmethod
    def api_key_from_env(env_name: str = "NVIDIA_API_KEY") -> str:
        return os.environ.get(env_name, "")


@dataclass(frozen=True)
class LocalNimProvider:
    """本机/内网 OpenAI 兼容端点（如 NIM 容器、Ollama、vLLM）。无 base_url 即惰性。"""

    base_url: str = ""
    api_key: str = ""
    model: str = "local"
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    name: str = "local_nim"

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.base_url:
            raise ProviderUnavailableError(
                "Provider 'local_nim' needs a base_url for the local endpoint.")
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        return _chat_completions(
            endpoint=endpoint, api_key=self.api_key, model=self.model,
            system=system, prompt=prompt, timeout_seconds=self.timeout_seconds)


@dataclass(frozen=True)
class OpenAICompatibleProvider:
    """其他厂商的 OpenAI 兼容端点（base_url + api_key 均必配）。"""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    response_format: dict | None = None
    name: str = "openai_compatible"

    def complete(self, *, system: str, prompt: str) -> str:
        if not self.base_url or not self.api_key or not self.model:
            raise ProviderUnavailableError(
                "Provider 'openai_compatible' needs base_url, api_key and model.")
        endpoint = self.base_url.rstrip("/") + "/chat/completions"
        return _chat_completions(
            endpoint=endpoint, api_key=self.api_key, model=self.model,
            system=system, prompt=prompt, timeout_seconds=self.timeout_seconds,
            response_format=self.response_format)


def create_provider(
    settings: dict | None = None,
    *,
    system_settings: "SystemSettings | None" = None,
) -> AdvisorLLMProvider:
    """Build a provider from a small settings mapping; default is null.

    When ``system_settings`` is supplied it acts as the network gate (doc 11
    §7 P8-T5): with ``network_enabled`` false the network-capable slots are
    refused outright; with it true, missing credentials are reported by name.
    """
    resolved = dict(settings or {})
    kind = str(resolved.get("provider", "null"))
    model = str(resolved.get("model") or "")
    base_url = str(resolved.get("base_url", "")).strip()
    api_key = str(resolved.get("api_key") or "")
    timeout = float(resolved.get("timeout_seconds") or DEFAULT_TIMEOUT_SECONDS)
    if kind in {"nvidia", "local_nim", "openai_compatible"} and system_settings is not None:
        if not system_settings.network_enabled:
            raise ProviderUnavailableError(
                f"Provider {kind!r} refused: the network switch is off"
                " (settings.network_enabled=false)."
            )
        if kind == "nvidia":
            key = api_key or NvidiaNimProvider.api_key_from_env()
            if not key:
                raise ProviderUnavailableError(
                    "Provider 'nvidia' needs credentials: set api_key or NVIDIA_API_KEY.")
            return NvidiaNimProvider(
                api_key=key,
                model=model or NVIDIA_DEFAULT_MODEL,
                base_url=base_url or NVIDIA_DEFAULT_BASE_URL,
                timeout_seconds=timeout)
        if kind == "openai_compatible":
            if not (base_url and api_key and model):
                raise ProviderUnavailableError(
                    "Provider 'openai_compatible' needs base_url, api_key and model.")
            return OpenAICompatibleProvider(
                base_url=base_url, api_key=api_key, model=model,
                timeout_seconds=timeout)
        # local_nim
        if not base_url:
            raise ProviderUnavailableError(
                "Provider 'local_nim' needs a base_url for the local endpoint.")
        return LocalNimProvider(
            base_url=base_url, api_key=api_key,
            model=model or "local", timeout_seconds=timeout)
    if kind == "null":
        return NullProvider()
    if kind == "mock":
        return MockProvider(script=dict(resolved.get("script", {})), default_reply=str(resolved.get("default_reply", MockProvider().default_reply)))
    # 未提供联网门上下文：返回惰性实例（缺凭证/base_url 时调用即报错，不触网）。
    if kind == "nvidia":
        return NvidiaNimProvider(
            api_key=api_key, model=model or NVIDIA_DEFAULT_MODEL,
            base_url=base_url or NVIDIA_DEFAULT_BASE_URL, timeout_seconds=timeout)
    if kind == "local_nim":
        return LocalNimProvider(
            base_url=base_url, api_key=api_key,
            model=model or "local", timeout_seconds=timeout)
    if kind == "openai_compatible":
        return OpenAICompatibleProvider(
            base_url=base_url, api_key=api_key, model=model, timeout_seconds=timeout)
    raise ValueError(f"Unknown LLM provider kind: {kind!r}")
