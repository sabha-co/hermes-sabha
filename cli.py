"""``hermes sabha setup``: connect Hermes to a Sabha app in one line, with the
values the app's page in Sabha (Settings → Apps → API) gives: its URL, its
WebSocket URL and its key. Checks the key with Sabha before saving anything.

The rules for those values are plain functions, so they're tested without
Hermes; checking the key and saving it only run inside Hermes."""

from __future__ import annotations

import argparse
import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit

APP_KEY = re.compile(r"\Asabha_app_[0-9a-f]{32}\Z")
USER_IDS = re.compile(r"\A\d+(,\d+)*\Z")
# On sabha.co a workspace is a numeric path prefix for the API, but the cable
# lives at the host's /cable and is told the workspace as `wid`.
SABHA_CO_WORKSPACE = re.compile(r"\A/(\d{7,})\Z")


class SetupError(ValueError):
    pass


def restart_command(system_unit: bool, user_unit: bool) -> str:
    """How to restart the gateway once the values are saved. Installed as a
    system-wide service (and not per user, which Hermes picks first), it only
    restarts as root."""
    return "sudo hermes gateway restart --system" if system_unit and not user_unit else "hermes gateway restart"


def workspace_url(url: str) -> str:
    """The workspace's URL from either it or its API base URL, as the app's
    page shows it. Empty when it isn't an http(s) URL."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    path = parts.path.rstrip("/")
    path = path.removesuffix("/api/v1")
    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def cable_url(workspace: str) -> str:
    """The WebSocket URL a workspace's app page shows, when none is given."""
    parts = urlsplit(workspace)
    scheme = "wss" if parts.scheme == "https" else "ws"
    if (sabha_co := SABHA_CO_WORKSPACE.match(parts.path)):
        return urlunsplit((scheme, parts.netloc, "/cable", f"wid={sabha_co[1]}", ""))
    return urlunsplit((scheme, parts.netloc, f"{parts.path}/cable", "", ""))


def settings(url: str, key: str, cable: str | None = None, allowed_users: str | None = None) -> dict[str, str]:
    """What setup writes to Hermes's .env, or a SetupError saying what to fix."""
    workspace = workspace_url(url or "")
    if not workspace:
        raise SetupError(f"Not an http(s) URL: {url}" if url else "Pass --url with the URL from the app's page.")
    key = (key or "").strip()
    if not APP_KEY.match(key):
        raise SetupError("The app key should look like sabha_app_ followed by 32 hex characters.")
    if cable and urlsplit(cable.strip()).scheme not in ("ws", "wss"):
        raise SetupError(f"Not a ws(s) URL: {cable}")

    values = {"SABHA_API_URL": f"{workspace}/api/v1", "SABHA_WEBSOCKET_URL": (cable or "").strip() or cable_url(workspace),
              "SABHA_APP_KEY": key}
    if allowed_users:
        users = allowed_users.replace(" ", "")
        if not USER_IDS.match(users):
            raise SetupError("--allowed-users takes Sabha user ids, comma-separated, like 1,42.")
        values["SABHA_ALLOWED_USERS"] = users
    return values


def refusal(status: int, api_url: str) -> str:
    """Why Sabha said no to the key check, and what to do about it."""
    if status == 401:
        return "Sabha doesn't know this key. Copy it again from the app's page, or fill in a new one there."
    if status == 404:
        return f"Nothing answers at {api_url}. Use the URL from the app's page in Sabha."
    return f"Sabha answered {status} at {api_url}."


def register_cli(parser: argparse.ArgumentParser) -> None:
    subs = parser.add_subparsers(dest="sabha_command")
    setup = subs.add_parser("setup", help="Connect a Sabha app: check its key with Sabha, then save it",
                            description="Takes what the app's API tab in Sabha shows. With no flags on a "
                                        "terminal, asks for each value instead.")
    setup.add_argument("--url", help="API base URL from the app's page (or the workspace URL)")
    setup.add_argument("--cable-url", help="WebSocket URL from the app's page (default: /cable on the same host)")
    setup.add_argument("--key", help="app key (sabha_app_…); defaults to $SABHA_APP_KEY")
    setup.add_argument("--allowed-users", help="Sabha user ids allowed to talk to the agent, comma-separated")
    setup.add_argument("--skip-verify", action="store_true", help="save without checking the key with Sabha")
    parser.set_defaults(func=dispatch)


def dispatch(args: argparse.Namespace) -> int:
    if args.sabha_command == "setup":
        return _setup(args)
    print("Usage: hermes sabha setup --url URL --key KEY", file=sys.stderr)
    return 2


def _setup(args: argparse.Namespace) -> int:
    if not args.url and sys.stdin.isatty():
        from .adapter import interactive_setup

        interactive_setup()
        return 0

    try:
        values = settings(args.url, args.key or os.environ.get("SABHA_APP_KEY", ""), args.cable_url, args.allowed_users)
    except SetupError as error:
        return _fail(str(error))

    if not args.skip_verify:
        import asyncio

        import aiohttp

        from .api import SabhaApiError

        try:
            me = _me(values["SABHA_API_URL"], values["SABHA_APP_KEY"])
        except SabhaApiError as error:
            return _fail(refusal(error.status, values["SABHA_API_URL"]))
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
            return _fail(f"Couldn't reach Sabha at {values['SABHA_API_URL']}: {error}")
        workspace = (me.get("workspace") or {}).get("name")
        print(f'✓ Sabha knows this key: app "{me.get("name")}" (id {me.get("id")})' + (f" in {workspace}" if workspace else ""))

    from hermes_cli.gateway_setup_wizard import save_env_values

    save_env_values(**values)
    print(f"✓ Saved {', '.join(values)} to Hermes's .env")
    print(f"Then restart the gateway: {_restart_command()}")
    return 0


def _restart_command() -> str:
    if not sys.platform.startswith("linux"):
        return restart_command(False, False)
    from hermes_cli.gateway import get_systemd_unit_path

    return restart_command(get_systemd_unit_path(system=True).exists(), get_systemd_unit_path(system=False).exists())


def _me(api_url: str, key: str) -> dict:
    import asyncio

    import aiohttp
    from gateway.platforms.base import gateway_trust_env

    from .api import SabhaApi

    async def ask() -> dict:
        async with aiohttp.ClientSession(trust_env=gateway_trust_env()) as session:
            return await SabhaApi(session, api_url, key).me()

    return asyncio.run(ask())


def _fail(message: str) -> int:
    print(f"✗ {message}", file=sys.stderr)
    return 1
