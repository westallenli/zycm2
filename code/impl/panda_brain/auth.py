"""Real authentication for the general entry (doc 11 §3.5 item 4 red line).

Before this module the entry was a string constant: whoever typed
``"local-general-entry"`` could act as the general. Identity now requires proof
of possession of a secret. The offline simulation harness may provision an
ephemeral in-memory secret (single-user, local-run premise); a real deployment
binds a persistent secret file, and the workspace starts locked until
``authenticate`` succeeds.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from pathlib import Path

from .models import GENERAL_ENTRY_ID, content_digest


@dataclass(frozen=True)
class GeneralCredentials:
    """Proof-of-possession artifact. It carries no reusable secret — only the
    fingerprint of the secret it was derived from."""

    entry_id: str
    secret_fingerprint: str


class GeneralEntryAuth:
    """Secret lifecycle and verification for the single general entry."""

    def __init__(
        self,
        secret_path: str | Path | None = None,
        *,
        secret: str | None = None,
    ) -> None:
        if secret is not None:
            self._secret = secret
        elif secret_path is not None:
            path = Path(secret_path)
            if path.exists():
                self._secret = path.read_text(encoding="utf-8").strip()
            else:
                self._secret = secrets.token_hex(32)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(self._secret, encoding="utf-8")
        else:
            # Ephemeral harness entry: exists only inside this process.
            self._secret = secrets.token_hex(32)

    @property
    def fingerprint(self) -> str:
        return content_digest(self._secret)

    @property
    def entry_id(self) -> str:
        return GENERAL_ENTRY_ID

    def reveal_secret(self) -> str:
        """Trusted local harness only; never exposed across any boundary."""
        return self._secret

    def authenticate(self, candidate: str) -> GeneralCredentials:
        if not isinstance(candidate, str) or not hmac.compare_digest(candidate, self._secret):
            raise PermissionError("General entry authentication failed: wrong secret.")
        return GeneralCredentials(entry_id=GENERAL_ENTRY_ID, secret_fingerprint=self.fingerprint)
