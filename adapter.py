"""Sabha gateway adapter for Hermes Agent.

Hermes joins a Sabha workspace as an app. Events come in over the app WebSocket
(``AppEventsChannel``), and replies go out through the apps API (``/api/v1``). Sabha delivers a
room message only when it mentions the app; a DM or a thread the app is in, every message.

Environment (or ``platforms.sabha.extra`` in config.yaml):
    SABHA_API_URL        API base URL from the app's page, e.g. https://chat.example.com/api/v1
    SABHA_WEBSOCKET_URL  WebSocket URL from the app's page, e.g. wss://chat.example.com/cable
    SABHA_APP_KEY        The app's bearer key (sabha_app_…)
    SABHA_ALLOWED_USERS  Comma-separated Sabha user ids allowed to talk to the agent
    SABHA_HOME_CHANNEL   Room id for cron and notification delivery
    SABHA_REPLY_MODE     "thread" (default) answers a room mention in a thread; "off" in the room

While the agent works, Hermes's typing indicator and its live phrases ("Searching the web…") show as
the app's status: the line under a working shell where the reply will land.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from pathlib import Path
from typing import Any, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms._shared import (
    apply_yaml_bridge, env_is_connected, extra_or_secret, get_scoped_secret, seed_extra_from_env, send_error,
)
from gateway.platforms.base import (
    BasePlatformAdapter, SendResult, cache_audio_from_bytes_async, cache_document_from_bytes_async,
    cache_image_from_bytes_async, cache_video_from_bytes_async, gateway_trust_env,
)
from gateway.platforms.event import MessageEvent, MessageType
from gateway.platforms.helpers import MessageDeduplicator, cancel_task

from . import cards
from . import commands
from . import markup
from . import routing
from .retries import RetryIds
from .status import StatusLines

logger = logging.getLogger(__name__)

# Replies read for a thread's context: enough for the start of most threads, in one request.
_THREAD_CONTEXT_LIMIT = 50

PLATFORM = "sabha"

# Sabha has no hard cap this small; it keeps a streamed reply's edits cheap and a long answer readable.
MAX_MESSAGE_LENGTH = 12000

_Metadata = Optional[dict[str, Any]]
_ENV = {"api_url": "SABHA_API_URL", "websocket_url": "SABHA_WEBSOCKET_URL", "key": "SABHA_APP_KEY"}
_RESPONSE_URL_USES = 5
# A parent that is gone (404) or can't hold a thread (422): post in the room instead.
_THREAD_REFUSALS = {404, 422}
# The response_url of the command being handled, so its first reply goes privately to whoever ran it.
_command_reply: contextvars.ContextVar[Optional[dict[str, Any]]] = contextvars.ContextVar(
    "sabha_command_reply", default=None)
_MEDIA_TYPES = (("image/", MessageType.PHOTO, cache_image_from_bytes_async, ".png"),
                ("audio/", MessageType.VOICE, cache_audio_from_bytes_async, ".ogg"),
                ("video/", MessageType.VIDEO, cache_video_from_bytes_async, ".mp4"))


def _setting(config: Any, name: str, default: str = "") -> str:
    return str(extra_or_secret(getattr(config, "extra", None) or {}, name, _ENV[name], default) or "").strip()


def _replay_path() -> Optional[Path]:
    """Where the event stream's position survives a restart: Hermes's per-plugin data directory."""
    try:
        from plugins.plugin_storage import plugin_data_dir
        return plugin_data_dir("sabha-platform") / "replay.json"
    except Exception as exc:
        logger.warning("Sabha: no plugin data directory (%s); a restart may miss events", exc)
        return None


def check_requirements() -> bool:
    try:
        import aiohttp  # noqa: F401
        import markdown_it  # noqa: F401
    except ImportError:
        return False
    return True


def validate_config(config: PlatformConfig) -> bool:
    missing = [_ENV[name] for name in _ENV if not _setting(config, name)]
    if missing:
        logger.warning("Sabha: missing %s", ", ".join(missing))
    return not missing


class SabhaAdapter(BasePlatformAdapter):
    serial_receive_loop = True
    splits_long_messages = True
    supports_code_blocks = True
    supports_status_text = True

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform(PLATFORM))
        self._api_url, self._socket_url, self._key = (_setting(config, n) for n in ("api_url", "websocket_url", "key"))
        mode = str(extra_or_secret(config.extra, "reply_mode", "SABHA_REPLY_MODE", "thread")).strip().lower()
        self._reply_in_thread = mode != "off"
        self._session: Any = None
        self._api: Any = None
        self._stream: Any = None
        self._stream_task: Optional[asyncio.Task] = None
        self._app_user_id = ""
        self._app_name = ""
        self._status_lines = StatusLines()
        self._dedup = MessageDeduplicator(ttl_seconds=600)
        self._retry_ids = RetryIds()
        self._cards = cards.Cards()

    # --- Lifecycle ---

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        import aiohttp
        from .api import SabhaApi, SabhaApiError
        from .cable import EventStream

        if not (self._api_url and self._socket_url and self._key):
            self._set_fatal_error("sabha_config", "SABHA_API_URL, SABHA_WEBSOCKET_URL and SABHA_APP_KEY are all "
                                  "needed; copy them from the app's page in Sabha (Settings → Apps).", retryable=False)
            return False
        if not self._acquire_platform_lock("sabha-app-key", self._key, "Sabha app key"):
            return False
        self._session = aiohttp.ClientSession(trust_env=gateway_trust_env())
        self._api = SabhaApi(self._session, self._api_url, self._key)
        try:
            me = await self._api.me()
        except (SabhaApiError, aiohttp.ClientError, asyncio.TimeoutError) as exc:
            refused = isinstance(exc, SabhaApiError) and exc.status in (401, 403)
            logger.error("Sabha: couldn't reach %s as the app (%s)", self._api_url, exc)
            self._set_fatal_error("sabha_auth" if refused else "sabha_unreachable",
                                  "Sabha refused the app key. Mint a new one on the app's page." if refused
                                  else f"Couldn't reach Sabha at {self._api_url}: {exc}", retryable=not refused)
            await self._close_session()
            return False
        self._app_user_id, self._app_name = str(me.get("id", "")), str(me.get("name", ""))
        workspace = (me.get("workspace") or {}).get("name") or self._api_url
        logger.info("Sabha: connected as %s (%s) in %s", self._app_name, self._app_user_id, workspace)

        from .replay import Replay
        self._stream = EventStream(self._session, self._socket_url, self._key, self._on_event,
                                   replay=Replay(_replay_path(), self._api_url))
        self._stream_task = asyncio.create_task(self._run_stream())
        await self._declare_commands()
        self._mark_connected()
        return True

    async def disconnect(self) -> None:
        if self._stream is not None:
            await self._stream.close()
        await cancel_task(self._stream_task)
        await self._close_session()
        self._release_platform_lock()
        self._mark_disconnected()

    async def _close_session(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()

    async def _declare_commands(self) -> None:
        """Another app may already hold a name; declare the rest, and those stay reachable as
        ``/hermes <name>``."""
        try:
            from hermes_cli.commands_platforms import slack_native_slashes
            declared = commands.declarations(slack_native_slashes())
            refused = await self._api.declare_commands(declared)
            if refused:
                logger.info("Sabha: %s taken in this workspace; reachable as /hermes <name>", ", ".join(refused))
                await self._api.declare_commands([c for c in declared if c["name"] not in refused])
        except Exception as exc:
            logger.warning("Sabha: couldn't declare slash commands (%s)", exc)

    async def _run_stream(self) -> None:
        from .cable import KeyRefused
        try:
            await self._stream.run()
        except asyncio.CancelledError:
            raise
        except KeyRefused as exc:
            logger.error("Sabha: %s", exc)
            self._set_fatal_error("sabha_auth", f"Sabha refused the app's event stream: {exc}. Check the app key "
                                  "and the WebSocket URL on the app's page.", retryable=False)
            await self._notify_fatal_error()

    # --- Inbound ---

    async def _on_event(self, event: dict[str, Any]) -> None:
        if event.get("event") == "command":
            return await self._on_command(event)
        if event.get("event") == "block_actions":
            return await self._on_click(event)
        if event.get("event") != "message_created" or routing.is_from_self(event, self._app_user_id):
            return
        message, user, room = event.get("message") or {}, event.get("user") or {}, event.get("room") or {}
        message_id = str(message.get("id", ""))
        if not message_id or self._dedup.is_duplicate(message_id):
            return

        route = routing.inbound_route(event, reply_in_thread=self._reply_in_thread)
        text = markup.strip_mention(routing.message_text(event), self._app_name)
        media_urls, media_types = await self._download_attachment(message.get("attachment"))
        if text.startswith("/"):
            kind = MessageType.COMMAND
        elif media_types:
            kind = next((t for prefix, t, _, _ in _MEDIA_TYPES if media_types[0].startswith(prefix)),
                        MessageType.DOCUMENT)
        else:
            kind = MessageType.TEXT

        source = self.build_source(
            chat_id=route.chat_id, chat_name=room.get("name") or None, chat_type=route.chat_type,
            user_id=str(user.get("id", "")), user_name=user.get("name") or None, thread_id=route.thread_id,
            message_id=message_id, is_bot=user.get("role") == "bot")
        context = await self._thread_context(message) if not self._has_session(source) else None
        await self.handle_message(MessageEvent(
            text=text, message_type=kind, source=source, raw_message=event, message_id=message_id,
            media_urls=media_urls, media_types=media_types, channel_context=context))

    # Mentioned in a thread it hasn't been in, the app would otherwise see only the mention. Loaded
    # once, for a session's first turn, as Hermes's Slack adapter does; later turns have the history.
    async def _thread_context(self, message: dict[str, Any]) -> Optional[str]:
        thread = message.get("thread") or {}
        if not thread.get("id") or not thread.get("parent_message_id"):
            return None
        try:
            parent = await self._api.message(str(thread["parent_message_id"]))
            replies = await self._api.messages(str(thread["id"]), limit=_THREAD_CONTEXT_LIMIT)
        except Exception as exc:
            logger.warning("Sabha: couldn't read thread %s for context (%s)", thread["id"], exc)
            return None
        return routing.thread_context(parent, replies, current_id=message.get("id"), app_user_id=self._app_user_id)

    def _has_session(self, source: Any) -> bool:
        store = getattr(self, "_session_store", None)
        if not store:
            return False
        try:
            return store.peek_session_id(self._source_session_key(source)) is not None
        except Exception:
            return False

    async def _on_command(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("id", ""))
        if not event_id or self._dedup.is_duplicate(event_id):
            return
        from hermes_cli.commands_platforms import slack_subcommand_map
        text = commands.command_text(event, slack_subcommand_map())
        chat_id, chat_type, thread_id = await self._command_place(event)
        source = self.build_source(
            chat_id=chat_id, chat_name=event.get("room_name") or None, chat_type=chat_type,
            user_id=str(event.get("user_id", "")), user_name=event.get("user_name") or None, thread_id=thread_id)
        is_command = text.startswith("/")
        # A question asked through /hermes is a turn like any other, answered (and streamed) in the open.
        token = _command_reply.set({"response_url": event.get("response_url"), "used": not is_command})
        try:
            await self.handle_message(MessageEvent(
                text=text, message_type=MessageType.COMMAND if is_command else MessageType.TEXT,
                source=source, raw_message=event, message_id=event_id))
        finally:
            _command_reply.reset(token)

    async def _command_place(self, event: dict[str, Any]) -> tuple[str, str, Optional[str]]:
        """A command event names its room but not the room's kind, so a room outside a thread is
        looked up to tell a DM from a channel."""
        thread = event.get("thread") or {}
        if thread.get("parent_room_id") and thread.get("parent_message_id"):
            return str(thread["parent_room_id"]), "channel", str(thread["parent_message_id"])
        room_id = str(event.get("room_id", ""))
        return room_id, "dm" if (await self.get_chat_info(room_id))["type"] == "dm" else "channel", None

    async def _download_attachment(self, attachment: Optional[dict]) -> tuple[list[str], list[str]]:
        if not attachment or not attachment.get("url"):
            return [], []
        mime = attachment.get("content_type") or "application/octet-stream"
        filename = attachment.get("filename") or "attachment"
        try:
            data = await self._api.download(attachment["url"])
        except Exception as exc:
            logger.warning("Sabha: couldn't download attachment %s (%s)", filename, exc)
            return [], []
        cache = next(((fn, ext) for prefix, _, fn, ext in _MEDIA_TYPES if mime.startswith(prefix)), None)
        if cache:
            path = await cache[0](data, Path(filename).suffix or cache[1])
        else:
            path = await cache_document_from_bytes_async(data, filename)
        return [path], [mime]

    # --- Outbound ---

    async def send(self, chat_id: str, content: str, reply_to: Optional[str] = None,
                   metadata: _Metadata = None) -> SendResult:
        if not content or not content.strip():
            return SendResult(success=True)
        reply = _command_reply.get()
        if reply and reply.get("response_url") and not reply["used"]:
            reply["used"] = True
            return await self._answer_command(reply["response_url"], content)
        thread_id = _thread_of(metadata)
        chunks = self.truncate_message(content, MAX_MESSAGE_LENGTH)
        send = (chat_id, thread_id, content)
        ids = self._retry_ids.take(send, len(chunks))
        result = SendResult(success=True)
        for chunk, client_message_id in zip(chunks, ids):
            result = await self._post(chat_id, markup.to_html(chunk), thread_id, client_message_id)
            if not result.success:
                # Chunks already posted answer a retry with themselves, so it posts only the rest.
                self._retry_ids.hold(send, ids)
                break
        return result

    async def _answer_command(self, response_url: str, content: str) -> SendResult:
        """Privately, to whoever ran the command. It's never posted for the room instead: an answer
        meant for one member mustn't leak because the private path failed."""
        try:
            for chunk in self.truncate_message(content, MAX_MESSAGE_LENGTH)[:_RESPONSE_URL_USES]:
                await self._api.respond(response_url, {"body": markup.to_html(chunk)})
        except Exception as exc:
            logger.warning("Sabha: couldn't answer the command (%s)", exc)
            return SendResult(success=False, error=f"Sabha command answer failed: {exc}")
        return SendResult(success=True)

    async def _post(self, chat_id: str, html: str, thread_id: Optional[str], client_message_id: str) -> SendResult:
        from .api import SabhaApiError
        try:
            posted = await self._api.post_message(chat_id, html, parent_message_id=thread_id,
                                                  client_message_id=client_message_id)
        except SabhaApiError as exc:
            if not (thread_id and exc.status in _THREAD_REFUSALS):
                return _failed(exc)
            logger.warning("Sabha: couldn't reply in the thread of %s (%s); posting in the room", thread_id, exc)
            try:
                posted = await self._api.post_message(chat_id, html, client_message_id=client_message_id)
            except SabhaApiError as retry_exc:
                return _failed(retry_exc)
            thread_id = None
        except Exception as exc:
            return SendResult(success=False, error=f"Sabha post failed: {exc}", retryable=True)
        self._status_lines.posted((str(chat_id), thread_id))
        return SendResult(success=True, message_id=posted.id)

    async def edit_message(self, chat_id: str, message_id: str, content: str, *,
                           finalize: bool = False) -> SendResult:
        # Hermes edits an approval card in place when nobody answered in time; its buttons go with it.
        drop_card = self._cards.pop(str(message_id)) is not None
        try:
            await self._api.edit_message(message_id, markup.to_html(content[:MAX_MESSAGE_LENGTH]), drop_card=drop_card)
        except Exception as exc:
            return SendResult(success=False, error=f"Sabha edit failed: {exc}")
        return SendResult(success=True, message_id=message_id)

    async def delete_message(self, chat_id: str, message_id: str) -> bool:
        try:
            await self._api.delete_message(message_id)
        except Exception as exc:
            logger.debug("Sabha: couldn't delete %s (%s)", message_id, exc)
            return False
        return True

    async def get_chat_info(self, chat_id: str) -> dict[str, Any]:
        try:
            room = await self._api.room(chat_id)
        except Exception:
            return {"name": chat_id, "type": "channel"}
        kind = "dm" if room.get("type") == "Direct" else "channel"
        return {"name": room.get("name") or chat_id, "type": kind}

    # --- Files ---

    async def send_image_file(self, chat_id: str, image_path: str, caption: Optional[str] = None,
                              reply_to: Optional[str] = None, metadata: _Metadata = None, **kwargs: Any) -> SendResult:
        return await self._send_file(chat_id, image_path, caption, metadata)

    async def send_document(self, chat_id: str, file_path: str, caption: Optional[str] = None,
                            file_name: Optional[str] = None, reply_to: Optional[str] = None,
                            metadata: _Metadata = None, **kwargs: Any) -> SendResult:
        return await self._send_file(chat_id, file_path, caption, metadata, file_name)

    async def send_voice(self, chat_id: str, audio_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: _Metadata = None, **kwargs: Any) -> SendResult:
        return await self._send_file(chat_id, audio_path, caption, metadata)

    async def send_video(self, chat_id: str, video_path: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: _Metadata = None, **kwargs: Any) -> SendResult:
        return await self._send_file(chat_id, video_path, caption, metadata)

    async def send_image(self, chat_id: str, image_url: str, caption: Optional[str] = None,
                         reply_to: Optional[str] = None, metadata: _Metadata = None, **kwargs: Any) -> SendResult:
        return await self.send(chat_id, f"{caption}\n\n{image_url}" if caption else image_url, metadata=metadata)

    async def _send_file(self, chat_id: str, path: str, caption: Optional[str], metadata: _Metadata,
                         file_name: Optional[str] = None) -> SendResult:
        """A Sabha message holds a body or a file, not both, so a caption goes first on its own."""
        if not Path(path).is_file():
            return SendResult(success=False, error=f"File not found: {path}")
        if caption:
            sent = await self.send(chat_id, caption, metadata=metadata)
            if not sent.success:
                return sent
        thread_id = _thread_of(metadata)
        send = (chat_id, thread_id, "file", path, file_name)
        [client_message_id] = self._retry_ids.take(send, 1)
        result = await self._post_file(chat_id, path, file_name, thread_id, client_message_id)
        if not result.success:
            self._retry_ids.hold(send, [client_message_id])
        return result

    async def _post_file(self, chat_id: str, path: str, file_name: Optional[str], thread_id: Optional[str],
                         client_message_id: str) -> SendResult:
        from .api import SabhaApiError
        try:
            posted = await self._api.post_file(chat_id, path, filename=file_name, parent_message_id=thread_id,
                                               client_message_id=client_message_id)
        except SabhaApiError as exc:
            if not (thread_id and exc.status in _THREAD_REFUSALS):
                return _failed(exc)
            try:
                posted = await self._api.post_file(chat_id, path, filename=file_name,
                                                   client_message_id=client_message_id)
            except SabhaApiError as retry_exc:
                return _failed(retry_exc)
            thread_id = None
        except Exception as exc:
            return SendResult(success=False, error=f"Sabha upload failed: {exc}", retryable=True)
        self._status_lines.posted((str(chat_id), thread_id))
        return SendResult(success=True, message_id=posted.id)

    # --- Approval and clarify cards ---

    _EA_REASON_BUDGET = 500

    def _ea_escape(self, text: str) -> str:
        return cards.escape(text)

    def _exec_approval_cmd_budget(self, description: str, smart_denied: bool) -> int:
        """The command preview gets what's left of the card's section once the rest of the prompt fits."""
        fixed = (len(self._EA_HEADER) + len(self._EA_CODE_OPEN) + len(self._EA_CODE_CLOSE) + len(self._EA_REASON_LABEL)
                 + len(cards.escape(description)) + len("...") + len(self._ea_deadline_line())
                 + (len(self._EA_SMART_DENY_LINE) if smart_denied else 0))
        return max(0, cards.SECTION_LIMIT - fixed)

    async def _send_exec_approval_prompt(self, prompt: Any) -> SendResult:
        result = await self._post_card(prompt.chat_id, prompt.metadata, f"Approve: {prompt.command[:100]}",
                                       cards.approval(prompt.text, prompt.actions, prompt.session_key))
        if result.success:
            self._cards.add(result.message_id, cards.Card(
                "approval", prompt.text.replace(self._ea_deadline_line(), ""), str(prompt.chat_id),
                session_key=prompt.session_key, request_id=prompt.request_id))
        return result

    async def send_slash_confirm(self, chat_id: str, title: str, message: str, session_key: str,
                                 confirm_id: str, metadata: _Metadata = None) -> SendResult:
        text = cards.confirm_text(title, message)
        result = await self._post_card(chat_id, metadata, f"{title}: {message[:100]}", cards.confirm(text, confirm_id))
        if result.success:
            self._cards.add(result.message_id, cards.Card(
                "confirm", text, str(chat_id), session_key=session_key, confirm_id=confirm_id,
                thread_id=_thread_of(metadata)))
            # Hermes sends nothing else for a command whose confirmation got buttons, and an
            # unanswered command tells its member that Hermes didn't answer.
            reply = _command_reply.get()
            if reply and not reply["used"]:
                await self.send(chat_id, "Confirm or cancel on the card below.")
        return result

    async def send_clarify(self, chat_id: str, question: str, choices: Optional[list], clarify_id: str,
                           session_key: str, metadata: _Metadata = None) -> SendResult:
        if choices:
            result = await self._post_card(chat_id, metadata, (question or "")[:200],
                                           cards.clarify(question, choices, clarify_id))
            if result.success:
                self._cards.add(result.message_id, cards.Card(
                    "clarify", cards.question_text(question), str(chat_id), session_key=session_key,
                    clarify_id=clarify_id))
                return result
        return await super().send_clarify(chat_id=chat_id, question=question, choices=choices,
                                          clarify_id=clarify_id, session_key=session_key, metadata=metadata)

    async def retire_clarify_card(self, clarify_id: str, notice: str) -> None:
        """The gateway ends a clarify nobody clicked: it timed out, the session reset, or a typed
        reply answered it. The card then stops offering buttons that can't answer anything."""
        found = self._cards.pop_clarify(clarify_id)
        if not found:
            return
        message_id, card = found
        try:
            await self._api.edit_card(message_id, notice, cards.answered(card.text, notice))
        except Exception as exc:
            logger.debug("Sabha: couldn't retire clarify card %s (%s)", message_id, exc)

    async def _post_card(self, chat_id: str, metadata: _Metadata, text: str, blocks: list) -> SendResult:
        from .api import SabhaApiError
        thread_id = _thread_of(metadata)
        try:
            posted = await self._api.post_card(chat_id, text, blocks, parent_message_id=thread_id)
        except SabhaApiError as exc:
            logger.warning("Sabha: couldn't post a card in %s (%s)", chat_id, exc)
            return _failed(exc)
        except Exception as exc:
            return SendResult(success=False, error=f"Sabha card post failed: {exc}", retryable=True)
        self._status_lines.posted((str(chat_id), thread_id))
        return SendResult(success=True, message_id=posted.id)

    async def _on_click(self, event: dict[str, Any]) -> None:
        event_id = str(event.get("id", ""))
        if not event_id or self._dedup.is_duplicate(event_id):
            return
        action = (event.get("actions") or [{}])[0]
        action_id, value = str(action.get("action_id", "")), str(action.get("value", ""))
        message_id = str((event.get("message") or {}).get("id", ""))
        user = event.get("user") or {}
        response_url = str(event.get("response_url") or "")
        card = self._cards.get(message_id)
        if card is None:
            await self._respond(response_url, {"text": "This prompt has expired."})
            return
        if not await self._may_answer(user, card):
            await self._respond(response_url, {"text": "Only people allowed to use Hermes can answer this."})
            return
        name = str(user.get("name") or "someone")
        if card.kind == "approval":
            note = self._answer_approval(message_id, card, action_id, name)
        elif card.kind == "confirm":
            note = await self._answer_confirm(message_id, card, action_id, value, name)
        else:
            note = self._answer_clarify(message_id, card, action_id, value, name)
        if note:
            await self._respond(response_url, {"replace_original": True, "text": note,
                                               "blocks": cards.answered(card.text, note)})

    async def _may_answer(self, user: dict[str, Any], card: cards.Card) -> bool:
        """Whoever can talk to Hermes in the card's room. Sabha says who clicked; the gateway's
        allowlist says whether they count."""
        if user.get("role") == "bot":
            return False
        chat_type = (await self.get_chat_info(card.chat_id))["type"]
        return self._is_sender_authorized(str(user.get("id", "")), chat_type, card.chat_id) is True

    def _answer_approval(self, message_id: str, card: cards.Card, action_id: str, name: str) -> Optional[str]:
        choice = cards.APPROVAL_CHOICES.get(action_id)
        if choice is None:
            return None
        self._cards.pop(message_id)
        from tools.approval import resolve_gateway_approval_card
        try:
            resolved = resolve_gateway_approval_card(card.session_key, choice, card.request_id)
        except Exception as exc:
            logger.error("Sabha: couldn't resolve the approval (%s)", exc)
            resolved = 0
        logger.info("Sabha: %s chose %s for %s (%d resolved)", name, choice, card.session_key, resolved)
        if not resolved:
            return "This approval expired, so the command didn't run."
        return f"{cards.APPROVAL_OUTCOMES[choice]} by {name}"

    async def _answer_confirm(self, message_id: str, card: cards.Card, action_id: str, value: str,
                              name: str) -> Optional[str]:
        choice = cards.CONFIRM_CHOICES.get(action_id)
        if choice is None or value != card.confirm_id:
            return None
        self._cards.pop(message_id)
        from tools import slash_confirm
        result = await slash_confirm.resolve(card.session_key, card.confirm_id, choice)
        if result is None:
            return "This confirmation expired."
        await self.send(card.chat_id, result, metadata={"thread_id": card.thread_id} if card.thread_id else None)
        return f"{cards.CONFIRM_OUTCOMES[choice]} by {name}"

    def _answer_clarify(self, message_id: str, card: cards.Card, action_id: str, value: str,
                        name: str) -> Optional[str]:
        from tools import clarify_gateway
        clarify_id, _, token = value.partition("|")
        if action_id == cards.CLARIFY_OTHER:
            # The card stays known: the clarify now waits for typed text, and the gateway retires it.
            if clarify_gateway.mark_awaiting_text(clarify_id):
                return f"Waiting for {name} to type an answer"
            self._cards.pop(message_id)
            return "This question expired."
        if not action_id.startswith(cards.CLARIFY_CHOICE) or not token.isdigit():
            return None
        self._cards.pop(message_id)
        entry = clarify_gateway.get_pending(clarify_id)
        choices = list(getattr(entry, "choices", None) or [])
        index = int(token)
        if index < len(choices) and clarify_gateway.resolve_gateway_clarify(clarify_id, str(choices[index])):
            return f"{name} chose {choices[index]}"
        return "This question expired."

    async def _respond(self, response_url: str, payload: dict[str, Any]) -> None:
        if not response_url:
            return
        try:
            await self._api.respond(response_url, payload)
        except Exception as exc:
            logger.warning("Sabha: couldn't answer the click (%s)", exc)

    # --- Status line ---

    async def send_typing(self, chat_id: str, metadata: _Metadata = None) -> None:
        place = (str(chat_id), _thread_of(metadata))
        line = self._status_lines.due(place, getattr(self, "_status_text", {}).get(str(chat_id)))
        if not (line and self._api):
            return
        try:
            await self._api.set_status(place[0], line, parent_message_id=place[1])
        except Exception as exc:
            logger.debug("Sabha: couldn't set the status in %s (%s)", chat_id, exc)
            return
        self._status_lines.sent(place, line)

    async def stop_typing(self, chat_id: str, metadata: _Metadata = None) -> None:
        for room_id, thread_id in self._status_lines.finish(str(chat_id), _thread_of(metadata)):
            try:
                await self._api.clear_status(room_id, parent_message_id=thread_id)
            except Exception as exc:
                logger.debug("Sabha: couldn't clear the status in %s (%s)", room_id, exc)

    async def on_processing_start(self, event: MessageEvent) -> None:
        if self._sender_refused(event):
            self._status_lines.quiet(str(event.source.chat_id))

    def _sender_refused(self, event: MessageEvent) -> bool:
        """The gateway runs this hook, and starts typing, before its allowlist, so without asking first
        a sender it's about to turn away would still see a working shell flash up."""
        check = getattr(self, "_is_sender_authorized", None)
        source = getattr(event, "source", None)
        if not callable(check) or source is None:
            return False
        return check(source.user_id, source.chat_type, source.chat_id,
                     is_bot=bool(getattr(source, "is_bot", False)), thread_id=source.thread_id) is False


def _thread_of(metadata: _Metadata) -> Optional[str]:
    if isinstance(metadata, dict) and metadata.get("thread_id"):
        return str(metadata["thread_id"])
    return None


def _failed(exc: Any) -> SendResult:
    retryable = getattr(exc, "status", 0) == 429 or getattr(exc, "status", 0) >= 500
    return SendResult(success=False, error=str(exc), retryable=retryable)


# --- Out-of-process delivery (cron) ---

async def _standalone_send(pconfig, chat_id: str, message: str, *, thread_id: Optional[str] = None,
                           media_files: Optional[list] = None, force_document: bool = False) -> dict[str, Any]:
    import aiohttp
    from .api import SabhaApi

    api_url, key = _setting(pconfig, "api_url"), _setting(pconfig, "key")
    if not (api_url and key):
        return send_error("Sabha: SABHA_API_URL and SABHA_APP_KEY must both be set")
    try:
        async with aiohttp.ClientSession(trust_env=gateway_trust_env()) as session:
            api = SabhaApi(session, api_url, key)
            last_id = None
            if message and message.strip():
                last_id = (await api.post_message(chat_id, markup.to_html(message),
                                                  parent_message_id=thread_id)).id
            for media in media_files or []:
                path = media.get("path") if isinstance(media, dict) else media
                if path and Path(path).is_file():
                    last_id = (await api.post_file(chat_id, path, parent_message_id=thread_id)).id
        return {"success": True, "platform": PLATFORM, "chat_id": chat_id, "message_id": last_id}
    except Exception as exc:
        return send_error(f"Sabha send failed: {exc}")


# --- Configuration ---

def _env_enablement() -> dict | None:
    if not all(get_scoped_secret(env, "").strip() for env in _ENV.values()):
        return None
    return seed_extra_from_env(
        (("SABHA_API_URL", "api_url", None), ("SABHA_WEBSOCKET_URL", "websocket_url", None),
         ("SABHA_APP_KEY", "key", None), ("SABHA_REPLY_MODE", "reply_mode", None)),
        home_env="SABHA_HOME_CHANNEL")


_YAML_BRIDGE = (("reply_mode", "SABHA_REPLY_MODE", "str"),)


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict | None:
    return apply_yaml_bridge(platform_cfg, _YAML_BRIDGE)


def interactive_setup() -> None:
    from hermes_cli.cli_output import print_info, print_success, print_warning
    from hermes_cli.gateway_setup_wizard import confirm_reconfigure, print_setup_header, prompt_csv, save_env_values
    from hermes_cli.setup import prompt

    print_setup_header("Sabha")
    if not confirm_reconfigure("Sabha", *_ENV.values()):
        return
    print_info("  In Sabha, an admin creates the app: Settings → Apps → New.")
    print_info("  Its page shows the API base URL, the WebSocket URL and the key (once).")
    print_info("  Add the app to the rooms it should work in; DMs work without that.")
    print()
    api_url = prompt("API base URL (e.g. https://chat.example.com/api/v1)").strip().rstrip("/")
    socket_url = prompt("WebSocket URL (e.g. wss://chat.example.com/cable)").strip()
    key = prompt("App key (sabha_app_…)", password=True).strip()
    if not (api_url and socket_url and key):
        print_warning("  All three are needed; nothing saved.")
        return
    save_env_values(SABHA_API_URL=api_url, SABHA_WEBSOCKET_URL=socket_url, SABHA_APP_KEY=key)
    print_success("  Sabha app saved")
    print()
    print_info("  Who may talk to the agent? A user's id is the number in their profile URL (/users/42).")
    allowed = prompt_csv("Allowed user ids (comma-separated, empty for none yet)", "")
    if allowed:
        save_env_values(SABHA_ALLOWED_USERS=allowed)
        print_success("  Allowlist saved")
    else:
        print_info("  No allowlist: unknown people get a pairing code to approve with `hermes pairing approve sabha`.")
    home = prompt("Home room id for cron results (optional; the number in /rooms/12)").strip()
    if home:
        save_env_values(SABHA_HOME_CHANNEL=home)


_PLATFORM_HINT = (
    "You are in Sabha, a team chat app, talking with your user. Write standard Markdown: bold, italics, "
    "headings, lists, links, blockquotes and code blocks all render. Tables don't, so use lists instead. "
    "To mention someone, write @{user_id} with their numeric Sabha id; it notifies them only if they are "
    "in this room. To send a file, include MEDIA:/absolute/path/to/file in your response."
)


# register_platform's own parameters; everything else is a PlatformEntry field, dropped on older cores.
_REGISTER_ARGS = {"name", "label", "adapter_factory", "check_fn", "validate_config", "required_env", "install_hint"}


def register(ctx) -> None:
    import dataclasses
    from gateway.platform_registry import PlatformEntry

    kwargs: dict[str, Any] = dict(
        name=PLATFORM, label="Sabha", adapter_factory=SabhaAdapter, check_fn=check_requirements,
        validate_config=validate_config, required_env=list(_ENV.values()),
        install_hint="Install this plugin's Python dependencies (aiohttp, markdown-it-py), then restart the gateway",
        setup_fn=interactive_setup, env_enablement_fn=_env_enablement, apply_yaml_config_fn=_apply_yaml_config,
        is_connected=env_is_connected(*_ENV.values()),
        cron_deliver_env_var="SABHA_HOME_CHANNEL", standalone_sender_fn=_standalone_send,
        allowed_users_env="SABHA_ALLOWED_USERS", allow_all_env="SABHA_ALLOW_ALL_USERS",
        max_message_length=MAX_MESSAGE_LENGTH, platform_hint=_PLATFORM_HINT, emoji="🪷",
    )
    known = {f.name for f in dataclasses.fields(PlatformEntry)}
    for key, value in (("allowlist_suppresses_pairing", True), ("display_tier", "medium"),
                       ("dashboard_description", "Connect Hermes to a Sabha workspace as an app."),
                       ("docs_url", "https://github.com/sabha-co/hermes-sabha")):
        if key in known:
            kwargs[key] = value
    ctx.register_platform(**{k: v for k, v in kwargs.items() if k in known or k in _REGISTER_ARGS})
