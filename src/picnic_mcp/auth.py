"""Picnic credentials: where they come from and how they become headers.

Picnic's order app (order.trypicnic.com) signs eaters in via an email/SMS
OTP flow backed by an OIDC provider. After login the browser holds one of:

  - a session cookie (the default "cookie" auth strategy), sent automatically
    to `order.trypicnic.com/api/picnic/graphql`, or
  - a bearer access token, sent as `authorization: Bearer <token>`.

We don't replay the OTP flow; instead the user copies whichever credential
their browser is using out of DevTools and we store it locally.

Resolution order: `PICNIC_COOKIE` / `PICNIC_TOKEN` env vars, then the
credentials file (`~/.config/picnic-mcp/credentials.json`).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

CREDENTIALS_PATH = Path(
    os.environ.get("PICNIC_CREDENTIALS_PATH", "~/.config/picnic-mcp/credentials.json")
).expanduser()


class MissingCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PicnicCredentials:
    cookie: str | None = None
    token: str | None = None

    def __post_init__(self):
        if not (self.cookie or self.token):
            raise MissingCredentialsError("PicnicCredentials needs a cookie or a token")

    @property
    def session_issued_at(self) -> datetime | None:
        """When oauth2-proxy last (re)issued the session cookie.

        The cookie value is `<encrypted session>|<unix ts>|<signature>`.
        """
        for part in (self.cookie or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == "_oauth2_proxy" and value.count("|") == 2:
                return datetime.fromtimestamp(int(value.split("|")[1]), UTC)
        return None

    def to_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        if self.cookie:
            headers["Cookie"] = self.cookie
        if self.token:
            headers["authorization"] = f"Bearer {self.token.removeprefix('Bearer ').strip()}"
        return headers

    @classmethod
    def load(cls) -> PicnicCredentials:
        cookie, token = os.environ.get("PICNIC_COOKIE"), os.environ.get("PICNIC_TOKEN")
        if cookie or token:
            return cls(cookie=cookie, token=token)
        if CREDENTIALS_PATH.exists():
            return cls(**json.loads(CREDENTIALS_PATH.read_text()))
        raise MissingCredentialsError(
            "No Picnic credentials found. Run `picnic-mcp login you@company.com` "
            "(or set PICNIC_COOKIE / PICNIC_TOKEN)."
        )

    def save(self) -> Path:
        CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
        CREDENTIALS_PATH.touch(mode=0o600, exist_ok=True)
        CREDENTIALS_PATH.chmod(0o600)
        CREDENTIALS_PATH.write_text(json.dumps(asdict(self), indent=2))
        return CREDENTIALS_PATH
