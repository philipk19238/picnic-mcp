"""picnic-mcp command line.

    picnic-mcp login you@company.com     # emails a one-time code, prompts for it
    picnic-mcp auth set --cookie '<Cookie header from DevTools>'
    picnic-mcp auth set --token '<Bearer token>'
    picnic-mcp auth status      # who the session belongs to + cookie age
    picnic-mcp auth refresh     # touch the session so oauth2-proxy rotates it
    picnic-mcp whoami
    picnic-mcp serve            # stdio MCP server (default)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime

from .auth import CREDENTIALS_PATH, MissingCredentialsError, PicnicCredentials
from .client import PicnicClient
from .http_base_client import ResponseError
from .login import LoginError, start_login, verify_login


def _client() -> PicnicClient:
    return PicnicClient(PicnicCredentials.load(), on_credentials_rotated=PicnicCredentials.save)


def _report(err: ResponseError) -> int:
    print(f"error {err.status_code}: {err.message}", file=sys.stderr)
    if err.status_code == 401:
        print("Session expired — run `picnic-mcp login you@company.com`.", file=sys.stderr)
    return 1


async def _whoami() -> int:
    async with _client() as client:
        try:
            viewer = await client.get_viewer()
        except ResponseError as err:
            return _report(err)
    print(json.dumps(viewer, indent=2))
    return 0


async def _login(email: str, channel: str) -> int:
    try:
        pending = await start_login(email, channel)
    except LoginError as err:
        print(err, file=sys.stderr)
        return 1
    print(f"Picnic sent a login code to your {'phone' if channel == 'sms' else 'email'}.")
    try:
        for _ in range(3):
            code = await asyncio.to_thread(input, "Code: ")
            try:
                creds = await verify_login(pending, code)
                break
            except LoginError as err:
                print(err, file=sys.stderr)
        else:
            return 1
    finally:
        await pending.aclose()
    print(f"logged in; saved credentials to {creds.save()}")
    return 0


async def _auth_status(refresh: bool) -> int:
    async with _client() as client:
        before = client.credentials.session_issued_at
        try:
            info = await client.refresh_session()
        except ResponseError as err:
            return _report(err)
        after = client.credentials.session_issued_at
    print(f"account: {info.get('username')} ({info.get('accountId')})")
    if after:
        age = datetime.now(UTC) - after
        print(f"session cookie issued: {after:%Y-%m-%d %H:%M:%S} UTC ({age.total_seconds() / 3600:.1f}h ago)")
    if refresh:
        print("session rotated and saved" if after != before else "session still fresh; no rotation needed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="picnic-mcp")
    parser.add_argument("-v", "--verbose", action="store_true", help="log HTTP requests")
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth", help="manage stored credentials").add_subparsers(
        dest="auth_command", required=True
    )
    auth_set = auth.add_parser("set", help=f"store credentials in {CREDENTIALS_PATH}")
    auth_set.add_argument("--cookie", help="raw Cookie header value for order.trypicnic.com")
    auth_set.add_argument("--token", help="bearer access token")
    auth.add_parser("clear", help="delete stored credentials")
    auth.add_parser("status", help="check the session and show its age")
    auth.add_parser("refresh", help="touch the session so the proxy re-issues it when due")

    login = sub.add_parser("login", help="log in with a one-time code")
    login.add_argument("email")
    login.add_argument("--sms", action="store_true", help="text the code instead of emailing it")
    sub.add_parser("whoami", help="print the signed-in eater")
    sub.add_parser("serve", help="run the MCP server over stdio")

    args = parser.parse_args(argv)
    # MCP stdio uses stdout, so logs always go to stderr.
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, stream=sys.stderr)

    try:
        match args.command:
            case "auth" if args.auth_command == "set":
                path = PicnicCredentials(cookie=args.cookie, token=args.token).save()
                print(f"saved credentials to {path}")
            case "auth" if args.auth_command in ("status", "refresh"):
                return asyncio.run(_auth_status(refresh=args.auth_command == "refresh"))
            case "auth":
                CREDENTIALS_PATH.unlink(missing_ok=True)
                print("cleared credentials")
            case "login":
                return asyncio.run(_login(args.email, "sms" if args.sms else "email"))
            case "whoami":
                return asyncio.run(_whoami())
            case "serve" | None:
                from .server import mcp

                mcp.run()
    except MissingCredentialsError as err:
        print(err, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
