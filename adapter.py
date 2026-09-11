"""
Sabha platform adapter for Hermes Agent.

Out-of-tree plugin that wires Sabha (https://sabha.co) into a Hermes
gateway via ``ctx.register_platform()``. Mirrors the IRC plugin's
packaging idiom: single ``register(ctx)`` entry point, lazy imports
from ``gateway.*``.

Configuration via environment variables (preferred):
    SABHA_BASE_URL          # e.g. https://sabha.co/1000006
    SABHA_API_BASE_URL      # optional override; defaults to {BASE_URL}/api/bots
    SABHA_BOT_KEY           # bearer credential, format: "{bot_id}-{token}"
    SABHA_WEBSOCKET_URL     # optional override; derived from BASE_URL
    SABHA_BOT_ID            # optional override; otherwise discovered via /me
    SABHA_ALLOWED_USERS     # comma-separated user ids
    SABHA_ALLOW_ALL_USERS   # truthy to allow all senders

Or under ``gateway.platforms.sabha.extra`` in config.yaml:
    base_url, api_base_url, bot_key, websocket_url, bot_id,
    typing_enabled, max_message_length
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any, Optional

import aiohttp

from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    cache_audio_from_bytes,
    cache_document_from_bytes,
    cache_image_from_bytes,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.config import Platform

from .client import SabhaApiError, SabhaClient
from .monitor import SabhaMonitor, build_websocket_url
from .webhook import resolve_chat_type, was_bot_mentioned

logger = logging.getLogger(__name__)


_DEFAULT_MAX_MESSAGE_LENGTH = 8000
_ATTACHMENT_KIND_BY_PREFIX = {
    "image/": MessageType.PHOTO,
    "video/": MessageType.VIDEO,
    "audio/": MessageType.AUDIO,
}


def _coerce_extra(config: Any) -> dict[str, Any]:
    return dict(getattr(config, "extra", {}) or {})


def _config_value(extra: dict[str, Any], env_key: str, cfg_key: str, default: str = "") -> str:
    return os.getenv(env_key) or extra.get(cfg_key, default) or ""


def _api_base_url(base_url: str, override: str = "") -> str:
    if override:
        return override.rstrip("/")
    return base_url.rstrip("/") + "/api/bots"


def _ext_from(filename: str, content_type: str, default: str) -> str:
    """Best-effort file extension (leading dot) for the image/audio cache
    helpers — they name the cached file by *extension*, not by the original
    filename, so a filename must not be passed through as-is."""
    import mimetypes
    import os

    _, ext = os.path.splitext(filename or "")
    if ext:
        return ext
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return guessed or default


class SabhaAdapter(BasePlatformAdapter):
    """Adapter implementing BasePlatformAdapter for Sabha."""

    def __init__(self, config: Any, **_kwargs: Any) -> None:
        super().__init__(config=config, platform=Platform("sabha"))

        extra = _coerce_extra(config)
        self._base_url = _config_value(extra, "SABHA_BASE_URL", "base_url")
        self._api_base_url = _api_base_url(
            self._base_url,
            _config_value(extra, "SABHA_API_BASE_URL", "api_base_url"),
        )
        self._bot_key = _config_value(extra, "SABHA_BOT_KEY", "bot_key")
        self._websocket_url = _config_value(
            extra, "SABHA_WEBSOCKET_URL", "websocket_url"
        )

        bot_id_raw = _config_value(extra, "SABHA_BOT_ID", "bot_id")
        self._bot_user_id: Optional[int] = int(bot_id_raw) if bot_id_raw else None

        self.max_message_length = int(
            extra.get("max_message_length") or _DEFAULT_MAX_MESSAGE_LENGTH
        )

        self._http_session: Optional[aiohttp.ClientSession] = None
        self._client: Optional[SabhaClient] = None
        self._monitor: Optional[SabhaMonitor] = None
        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._workspace_name: str = ""

    @property
    def name(self) -> str:
        return "Sabha"

    # ── Connection lifecycle ──────────────────────────────────────────────

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        if not self._base_url or not self._bot_key:
            self._set_fatal_error(
                "config_missing",
                "SABHA_BASE_URL and SABHA_BOT_KEY must be set",
                retryable=False,
            )
            return False

        # Per-process lock keyed on (base_url, bot_key) — stops two profiles
        # from racing the same identity over the same WS.
        if not self._acquire_platform_lock(
            "sabha",
            f"{self._base_url}::{self._bot_key[:8]}",
            f"Sabha bot at {self._base_url}",
        ):
            return False

        self._http_session = aiohttp.ClientSession()
        self._client = SabhaClient(
            self._api_base_url, self._bot_key, session=self._http_session
        )

        try:
            if self._bot_user_id is None:
                me = await self._client.get_bot_self()
                self._bot_user_id = int(me.get("id"))
                self._workspace_name = me.get("workspace_name") or me.get("name", "")
                logger.info(
                    "Sabha: connected as bot id=%s name=%s",
                    self._bot_user_id,
                    me.get("name", ""),
                )
        except SabhaApiError as e:
            await self._teardown()
            retryable = e.status not in (401, 403)
            self._set_fatal_error(
                "auth_failed" if not retryable else "bootstrap_failed",
                f"GET /me failed: {e}",
                retryable=retryable,
            )
            return False
        except Exception as e:
            await self._teardown()
            self._set_fatal_error("bootstrap_failed", str(e), retryable=True)
            return False

        ws_url = build_websocket_url(
            self._base_url, self._bot_key, self._websocket_url or None
        )
        self._monitor = SabhaMonitor(
            ws_url=ws_url,
            bot_id=self._bot_user_id,
            on_payload=self._dispatch_inbound,
            session=self._http_session,
            on_fatal=lambda code, msg: self._set_fatal_error(
                code, msg, retryable=False
            ),
        )
        self._monitor_task = self._monitor.start()
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        await self._teardown()
        self._mark_disconnected()
        self._release_platform_lock()

    async def _teardown(self) -> None:
        if self._monitor is not None:
            await self._monitor.stop()
            self._monitor = None
            self._monitor_task = None
        if self._client is not None:
            try:
                await self._client.close()
            except Exception:
                pass
            self._client = None
        if self._http_session is not None and not self._http_session.closed:
            await self._http_session.close()
        self._http_session = None

    # ── Sending ───────────────────────────────────────────────────────────

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="Not connected", retryable=True)
        try:
            result = await self._client.send_message(
                chat_id,
                content,
                parent_message_id=reply_to,
            )
        except SabhaApiError as e:
            return SendResult(
                success=False, error=str(e), retryable=e.status >= 500
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)

        if result is None:
            return SendResult(
                success=False,
                error="Sabha returned an empty/malformed send response",
                retryable=True,
            )
        return SendResult(success=True, message_id=str(result["id"]))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        *,
        finalize: bool = False,
    ) -> SendResult:
        if self._client is None:
            return SendResult(success=False, error="Not connected", retryable=True)
        try:
            await self._client.edit_message(message_id, content)
        except SabhaApiError as e:
            return SendResult(
                success=False, error=str(e), retryable=e.status >= 500
            )
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)
        return SendResult(success=True, message_id=str(message_id))

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        if self._client is None:
            return False
        try:
            await self._client.delete_message(message_id)
            return True
        except Exception:
            return False

    # ── Outbound attachments ──────────────────────────────────────────────

    async def _send_local_file(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str],
        reply_to: Optional[str],
        filename: Optional[str] = None,
    ) -> SendResult:
        """Upload a local file to a room (or thread) via multipart. Sabha
        uploads carry no caption field, so a caption is delivered as a
        follow-up message in the same location as the attachment."""
        if self._client is None:
            return SendResult(success=False, error="Not connected", retryable=True)
        try:
            result = await self._client.send_attachment(
                chat_id,
                file_path,
                filename=filename,
                parent_message_id=reply_to,
            )
        except FileNotFoundError as e:
            return SendResult(success=False, error=str(e), retryable=False)
        except SabhaApiError as e:
            return SendResult(success=False, error=str(e), retryable=e.status >= 500)
        except Exception as e:
            return SendResult(success=False, error=str(e), retryable=True)

        if result is None:
            return SendResult(
                success=False,
                error="Sabha returned an empty/malformed upload response",
                retryable=True,
            )
        if caption and caption.strip():
            # Reuse the attachment's reply_to so the caption lands alongside it
            # (same top-level room, or same thread) rather than under the file.
            await self.send(chat_id, caption, reply_to=reply_to)
        return SendResult(success=True, message_id=str(result["id"]))

    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> SendResult:
        return await self._send_local_file(
            chat_id, file_path, caption, reply_to, filename=file_name
        )

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> SendResult:
        return await self._send_local_file(chat_id, image_path, caption, reply_to)

    async def send_voice(
        self,
        chat_id: str,
        audio_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> SendResult:
        return await self._send_local_file(chat_id, audio_path, caption, reply_to)

    async def send_video(
        self,
        chat_id: str,
        video_path: str,
        caption: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
        **_kwargs: Any,
    ) -> SendResult:
        return await self._send_local_file(chat_id, video_path, caption, reply_to)

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        # Typing indicators ride on AnyCable whispers — see typing.py for the
        # follow-up implementation. No-op for now so the base class's
        # periodic-typing pump doesn't error.
        return None

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        return {"name": chat_id, "type": "group", "chat_id": chat_id}

    # ── Inbound dispatch ──────────────────────────────────────────────────

    async def _dispatch_inbound(self, payload: dict[str, Any]) -> None:
        if payload.get("event") != "message_created":
            return
        if self._bot_user_id is None:
            return

        room = payload.get("room") or {}
        message = payload.get("message") or {}
        user = payload.get("user") or {}

        room_type = str(room.get("type") or "")
        chat_type = resolve_chat_type(room_type)

        # Group filter: in non-DMs, only react when @-mentioned.
        if chat_type != "dm" and not was_bot_mentioned(payload, self._bot_user_id):
            return

        text = ((message.get("body") or {}).get("plain")) or ""
        chat_id = str(room.get("id") or "")
        chat_name = room.get("name") or chat_id
        user_id = str(user.get("id") or "")
        user_name = user.get("name") or user_id

        media_urls: list[str] = []
        media_types: list[str] = []
        msg_type = MessageType.TEXT
        attachment = message.get("attachment")
        if isinstance(attachment, dict) and attachment.get("url"):
            media_urls, media_types, msg_type = await self._cache_attachment(
                attachment
            )

        thread = message.get("thread") or {}
        reply_to = (
            str(thread.get("parent_message_id"))
            if isinstance(thread, dict) and thread.get("parent_message_id")
            else None
        )

        source = self.build_source(
            chat_id=chat_id,
            chat_name=chat_name,
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
        )
        event = MessageEvent(
            text=text,
            message_type=msg_type,
            source=source,
            message_id=str(message.get("id")),
            timestamp=datetime.now(),
            media_urls=media_urls,
            media_types=media_types,
            reply_to_message_id=reply_to,
            raw_message=payload,
        )
        await self.handle_message(event)

    async def _cache_attachment(
        self, attachment: dict[str, Any]
    ) -> tuple[list[str], list[str], MessageType]:
        """Download the signed URL inline; signed URLs expire ~1h."""
        if self._client is None:
            return [], [], MessageType.TEXT
        try:
            data = await self._client.fetch_attachment(attachment["url"])
        except Exception as e:
            logger.warning("Sabha: attachment fetch failed: %s", e)
            return [], [], MessageType.DOCUMENT

        content_type = (attachment.get("content_type") or "").lower()
        filename = attachment.get("filename") or "attachment"
        msg_type = MessageType.DOCUMENT
        cache_path: Optional[str] = None

        try:
            for prefix, kind in _ATTACHMENT_KIND_BY_PREFIX.items():
                if content_type.startswith(prefix):
                    msg_type = kind
                    break
            if msg_type == MessageType.PHOTO:
                cache_path = cache_image_from_bytes(
                    data, _ext_from(filename, content_type, ".jpg")
                )
            elif msg_type == MessageType.AUDIO:
                cache_path = cache_audio_from_bytes(
                    data, _ext_from(filename, content_type, ".ogg")
                )
            else:
                cache_path = cache_document_from_bytes(data, filename)
        except Exception as e:
            logger.warning("Sabha: attachment cache failed: %s", e)
            return [], [], MessageType.DOCUMENT

        if not cache_path:
            return [], [], MessageType.DOCUMENT
        return [cache_path], [content_type or "application/octet-stream"], msg_type


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------


def check_requirements() -> bool:
    """Passive dependency probe — are this plugin's Python deps importable
    right now?

    ``register_platform`` treats ``check_fn`` as a side-effect-free "deps
    present?" check (status displays call it freely, so it must never install
    or touch config). Whether Sabha is *configured* is answered separately by
    ``validate_config``. aiohttp is the only third-party dependency.
    """
    try:
        import aiohttp  # noqa: F401
    except Exception:
        return False
    return True


def validate_config(config: Any) -> bool:
    extra = _coerce_extra(config)
    base_url = os.getenv("SABHA_BASE_URL") or extra.get("base_url", "")
    bot_key = os.getenv("SABHA_BOT_KEY") or extra.get("bot_key", "")
    return bool(base_url and bot_key)


def is_connected(config: Any) -> bool:
    return validate_config(config)


def env_enablement() -> Optional[dict[str, Any]]:
    """Seed ``PlatformConfig.extra`` from env vars BEFORE the adapter is
    constructed, so env-only setups surface in ``hermes gateway status`` and
    ``get_connected_platforms()`` without waiting for the SDK to instantiate.
    Returns ``None`` when Sabha isn't minimally configured (the caller then
    skips auto-enabling it)."""
    base_url = (os.getenv("SABHA_BASE_URL") or "").strip()
    bot_key = (os.getenv("SABHA_BOT_KEY") or "").strip()
    if not (base_url and bot_key):
        return None
    seed: dict[str, Any] = {"base_url": base_url, "bot_key": bot_key}
    for env_key, cfg_key in (
        ("SABHA_API_BASE_URL", "api_base_url"),
        ("SABHA_WEBSOCKET_URL", "websocket_url"),
        ("SABHA_BOT_ID", "bot_id"),
    ):
        if value := (os.getenv(env_key) or "").strip():
            seed[cfg_key] = value
    # Home room for cron ``deliver=sabha`` and gateway notifications.
    if home := (os.getenv("SABHA_HOME_CHANNEL") or "").strip():
        seed["home_channel"] = {
            "chat_id": home,
            "name": os.getenv("SABHA_HOME_CHANNEL_NAME", home),
        }
    return seed


async def standalone_send(
    pconfig: Any,
    chat_id: str,
    message: str,
    *,
    thread_id: Optional[str] = None,
    media_files: Optional[list[str]] = None,
    force_document: bool = False,
) -> dict[str, Any]:
    """Out-of-process delivery for cron jobs (``deliver=sabha``) and the
    ``send_message`` tool when they run separately from the gateway. Opens a
    short-lived REST client, sends one message, and closes it.

    ``thread_id`` routes into a thread (Sabha ``parent_message_id``).
    ``media_files``/``force_document`` are accepted for signature parity with
    the platform contract; attachment delivery is handled by the live adapter.
    """
    extra = _coerce_extra(pconfig)
    base_url = _config_value(extra, "SABHA_BASE_URL", "base_url")
    bot_key = _config_value(extra, "SABHA_BOT_KEY", "bot_key")
    if not base_url or not bot_key:
        return {"error": "SABHA_BASE_URL and SABHA_BOT_KEY must be configured"}
    api_base_url = _api_base_url(
        base_url, _config_value(extra, "SABHA_API_BASE_URL", "api_base_url")
    )
    client = SabhaClient(api_base_url, bot_key)
    try:
        result = await client.send_message(
            chat_id, message, parent_message_id=thread_id
        )
    except Exception as e:
        return {"error": f"Sabha standalone send failed: {e}"}
    finally:
        await client.close()
    if result is None:
        return {"error": "Sabha returned an empty/malformed send response"}
    return {"success": True, "message_id": str(result["id"])}


def interactive_setup() -> None:
    """Hermes ``gateway setup`` flow for Sabha."""
    from hermes_cli.setup import (
        prompt,
        prompt_yes_no,
        save_env_value,
        get_env_value,
        print_header,
        print_info,
        print_warning,
        print_success,
    )

    print_header("Sabha")
    existing = get_env_value("SABHA_BASE_URL")
    if existing:
        print_info(f"Sabha: already configured (base URL: {existing})")
        if not prompt_yes_no("Reconfigure Sabha?", False):
            return

    print_info(
        "Connect Hermes to a Sabha workspace. You'll need a bot key from "
        "your Sabha admin console."
    )
    print()

    base_url = prompt(
        "Sabha base URL (e.g. https://sabha.co/1000006)",
        default=existing or "",
    )
    if not base_url:
        print_warning("Base URL is required — skipping Sabha setup")
        return
    save_env_value("SABHA_BASE_URL", base_url.strip())

    bot_key = prompt("Bot key (format: bot_id-token)", password=True)
    if not bot_key:
        print_warning("Bot key is required — skipping Sabha setup")
        return
    save_env_value("SABHA_BOT_KEY", bot_key.strip())

    if prompt_yes_no("Override the WebSocket URL? (uncommon)", False):
        ws_url = prompt(
            "WebSocket URL (e.g. wss://sabha.co/cable)",
            default=get_env_value("SABHA_WEBSOCKET_URL") or "",
        )
        if ws_url:
            save_env_value("SABHA_WEBSOCKET_URL", ws_url.strip())

    print()
    print_info("🔒 Access control")
    allow_all = prompt_yes_no(
        "Allow all senders the bot can see to talk to it?", False
    )
    if allow_all:
        save_env_value("SABHA_ALLOW_ALL_USERS", "true")
        save_env_value("SABHA_ALLOWED_USERS", "")
        print_warning("⚠️  Open access — any visible user can command the bot.")
    else:
        save_env_value("SABHA_ALLOW_ALL_USERS", "false")
        allowed = prompt(
            "Allowed user ids (comma-separated, leave empty to deny everyone)",
            default=get_env_value("SABHA_ALLOWED_USERS") or "",
        )
        save_env_value("SABHA_ALLOWED_USERS", (allowed or "").replace(" ", ""))

    print()
    print_success("Sabha configuration saved to ~/.hermes/.env")
    print_info("Restart the gateway: hermes gateway restart")


def register(ctx: Any) -> None:
    """Plugin entry point — Hermes' plugin loader calls this."""
    ctx.register_platform(
        name="sabha",
        label="Sabha",
        adapter_factory=lambda cfg: SabhaAdapter(cfg),
        check_fn=check_requirements,
        validate_config=validate_config,
        is_connected=is_connected,
        setup_fn=interactive_setup,
        env_enablement_fn=env_enablement,  # env-only setups show in gateway status
        cron_deliver_env_var="SABHA_HOME_CHANNEL",  # cron deliver=sabha target
        standalone_sender_fn=standalone_send,  # cron/send_message outside the gateway
        required_env=["SABHA_BASE_URL", "SABHA_BOT_KEY"],
        install_hint="pip install aiohttp",
        allowed_users_env="SABHA_ALLOWED_USERS",
        allow_all_env="SABHA_ALLOW_ALL_USERS",
        max_message_length=_DEFAULT_MAX_MESSAGE_LENGTH,
        pii_safe=True,
        emoji="🪷",
        allow_update_command=True,
        platform_hint=(
            "You are on Sabha — NOT Discord, Slack, Teams, or Telegram. "
            "Mention users by `@{user_id}` (numeric) using the user id from "
            "the inbound payload. Sabha renders standard markdown including "
            "h2–h6, lists, code fences, links, and inline emphasis. Tables "
            "are not supported and will be downgraded to preformatted text "
            "— prefer lists or short paragraphs instead."
        ),
    )
