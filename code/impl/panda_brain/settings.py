"""Global system settings (doc 11 §7 P8-T5).

The network switch is the single gate in front of every network-capable
provider: ``network_enabled`` defaults to false, and no provider may reach
outside the machine unless it is explicitly turned on in reviewed settings.
Credentials are loaded from configuration — never hardcoded.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

DEFAULT_SETTINGS_PATH = (
    Path(__file__).resolve().parent.parent / "config" / "settings.yaml"
)


@dataclass(frozen=True)
class SystemSettings:
    """First-version settings surface. Extend with reviewed fields only.

    P16-T22：新增 LLM 槽位字段（仅非敏感项）。api_key 绝不入配置文件，
    只从环境变量（如 NVIDIA_API_KEY）或内存注入读取。
    """

    network_enabled: bool = False
    llm_provider: str = "null"
    llm_model: str = ""
    llm_base_url: str = ""
    llm_timeout_seconds: float = 30.0


_ALLOWED_KEYS = {
    "network_enabled", "llm_provider", "llm_model",
    "llm_base_url", "llm_timeout_seconds",
}


def load_settings(path: str | Path | None = None) -> SystemSettings:
    """Load settings from YAML; a missing file means the safe defaults."""
    file_path = Path(path) if path is not None else DEFAULT_SETTINGS_PATH
    if not file_path.exists():
        return SystemSettings()
    raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"Settings file must be a mapping: {file_path}")
    unknown = sorted(set(raw) - _ALLOWED_KEYS)
    if unknown:
        raise ValueError(f"Unknown settings keys: {unknown}")
    return SystemSettings(
        network_enabled=bool(raw.get("network_enabled", False)),
        llm_provider=str(raw.get("llm_provider", "null")),
        llm_model=str(raw.get("llm_model", "")),
        llm_base_url=str(raw.get("llm_base_url", "")),
        llm_timeout_seconds=float(raw.get("llm_timeout_seconds") or 30.0),
    )
