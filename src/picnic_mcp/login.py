"""Email/SMS one-time-code login for Picnic, without a browser.

The web app's login is an OIDC dance fronted by oauth2-proxy, with Picnic's
IAM acting as an external IdP for Zitadel (ssoprovider.com):

  1. GET  /api/oauth2/start?rd=/&login_hint=<email>|otp_email
       → ssoprovider.com/oauth/v2/authorize → /ui/login/login
       → order.trypicnic.com/api/iam/oauth2/v1/authorize   (sends the code)
       → order.trypicnic.com/otp-login
  2. POST /api/iam/signin/oidc/otp {email, otp}
       → ssoprovider.com/ui/login/login/externalidp/callback
       → ssoprovider.com/oauth/v2/authorize/callback
       → order.trypicnic.com/api/oauth2/callback           (sets _oauth2_proxy)

Both halves must share one cookie jar (oauth2-proxy CSRF + Zitadel user
agent cookies), so `start_login` returns a `PendingLogin` holding it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

import httpx

from .auth import PicnicCredentials
from .client import ORIGIN, SESSION_COOKIE

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
)


class LoginError(RuntimeError):
    pass


@dataclass(slots=True)
class PendingLogin:
    email: str
    http: httpx.AsyncClient
    otp_page_url: str

    async def aclose(self) -> None:
        await self.http.aclose()


async def start_login(
    email: str, channel: Literal["email", "sms"] = "email"
) -> PendingLogin:
    """Begin a login; Picnic sends a one-time code to the account's email/phone."""
    http = httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT}, follow_redirects=True, timeout=30
    )
    try:
        resp = await http.get(
            f"{ORIGIN}/api/oauth2/start",
            params={"rd": f"{ORIGIN}/", "login_hint": f"{email}|otp_{channel}"},
        )
    except httpx.HTTPError as err:
        await http.aclose()
        raise LoginError(f"Could not reach Picnic: {err}") from err
    landed = urlparse(str(resp.url))
    if landed.netloc != urlparse(ORIGIN).netloc or landed.path != "/otp-login":
        await http.aclose()
        raise LoginError(
            f"Picnic didn't offer a code login for {email} (ended at "
            f"{landed.netloc}{landed.path}); the account may not exist."
        )
    return PendingLogin(email=email, http=http, otp_page_url=str(resp.url))


async def verify_login(pending: PendingLogin, code: str) -> PicnicCredentials:
    """Exchange the one-time code for a session. Closes `pending` on success."""
    resp = await pending.http.post(
        f"{ORIGIN}/api/iam/signin/oidc/otp",
        json={"email": pending.email, "otp": code.strip()},
        headers={"Origin": ORIGIN, "Referer": pending.otp_page_url, "Accept": "*/*"},
    )
    # The web app treats "the POST was redirected" as success.
    session = pending.http.cookies.get(SESSION_COOKIE, domain=urlparse(ORIGIN).netloc)
    if not resp.history or not session:
        raise LoginError("Picnic rejected that code (wrong or expired). Try again.")
    await pending.aclose()
    return PicnicCredentials(cookie=f"{SESSION_COOKIE}={session}")
