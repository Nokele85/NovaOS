"""The Telegram channel (SPEC §10.5) — she's in your pocket.

A thin adapter on the `base.Channel` contract, raw Bot API over httpx — no
bot-framework dependency for two HTTP calls:

  - **inbound**: one long-poll task on `getUpdates`. A text message from *the
    one configured chat* becomes an ordinary text turn (`rt.turns.run`), with
    a `typing…` chat action while she thinks. A photo becomes the same turn with
    a picture on it (SPEC §35) — downloaded, shrunk onto her upload shelf and
    handed to the model, when the model she runs on can see; when it cannot, she
    says so in words rather than ignoring what you sent. Telegram is an asynchronous
    inbox, so the channel never posts `user_present` — she is *reachable*
    there, not watched; the `user_message` signal each real message produces
    is what preempts the mind to ENGAGED.
  - **outbound**: one delivery task draining an EventHub subscription. Every
    committed assistant `message` — replies to any channel, greetings, and
    the mind's proactive lines — is sent to the chat, so a reach-out decided
    at 3pm lands on your phone even though no page is open. A selfie entry
    sends the PNG itself (the file is local; a chat client can't reach
    `/selfies/`). The sanctuary's telegram switch flips `sending_enabled` at
    runtime (routes/channels.py): inbound keeps working — she still reads you
    there — but nothing leaves for the chat while it's off.

Single-user discipline (the companion is one-on-one, SPEC §1): messages from
any chat but `TELEGRAM_CHAT_ID` are dropped. With the id unset the channel
starts in **pairing mode**: it replies to the first message with the chat id
to put in `.env` and processes nothing — binding her to a stranger's DM by
accident is the failure this refuses.

One bot, one character: `getUpdates` is exclusive per token, so each character
gets her own credentials — `TELEGRAM_BOT_TOKEN_<ID>` / `TELEGRAM_CHAT_ID_<ID>`,
resolved per runtime in `world/host.py` — and the adapter carries the name of
its own chat-id variable so pairing mode asks for hers.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path

import httpx

from yurios.world.turns import EmptyReply

from .base import Channel

log = logging.getLogger("world.telegram")

API_BASE = "https://api.telegram.org"
POLL_TIMEOUT_S = 50          # Telegram's long-poll window
RETRY_BACKOFF_S = 3.0        # after a network error; polling is idempotent
MAX_MESSAGE_CHARS = 4096     # Telegram's hard sendMessage cap


def _bubbles(text: str) -> list[str]:
    """Blank-line-separated paragraphs, empties dropped: one message each."""
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


class TelegramChannel(Channel):
    name = "telegram"

    def __init__(self, token: str, chat_id: str = "", *,
                 selfie_dir: Path | None = None,
                 chat_id_env: str = "TELEGRAM_CHAT_ID",
                 sending_enabled: bool = False,
                 api_base: str = API_BASE,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.token = token
        self.chat_id = str(chat_id or "")
        self.selfie_dir = selfie_dir
        # which variable pairing mode should name. With more than one character
        # in the house each has her own bot, so the answer is hers —
        # TELEGRAM_CHAT_ID_MIA, not the shared TELEGRAM_CHAT_ID (SPEC §10.5).
        self.chat_id_env = chat_id_env or "TELEGRAM_CHAT_ID"
        self.api_base = api_base
        self._transport = transport          # tests inject a MockTransport
        # Telegram turns always answer where they arrived. This controls only
        # cross-chat copies from the browser, voice, CLI and API.
        self.sending_enabled = sending_enabled
        self.rt = None
        self._client: httpx.AsyncClient | None = None
        self._tasks: list[asyncio.Task] = []
        self._queue: asyncio.Queue | None = None
        # ChannelManager protects duplicate claims inside one YuriOS process.
        # This second, OS-level lease protects against a second YuriOS process
        # on the same host. Telegram allows only one getUpdates consumer per
        # bot token; flock is released automatically if a process dies.
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()[:20]
        self._poll_lock_path = Path(tempfile.gettempdir()) / f"yurios-telegram-{digest}.lock"
        self._poll_lock_handle = None
        # per-chat session ids: her *memory* is the Vault; this only keeps the
        # short conversation window stitched — and across a restart too
        # (`state/telegram_sessions.json`, SPEC §10.5). Kept in the process
        # alone, every restart opened a new session with an empty window: on
        # 2026-10-07 she answered "that didn't work" with nothing of the
        # conversation in front of her, fifteen restarts into the day.
        self._sessions: dict[str, str | None] = {}
        self._sessions_loaded = False

    # ---- lifecycle ----

    @property
    def claim(self) -> tuple[str, str]:
        """One bot, one character. Telegram's getUpdates is exclusive per token,
        so the chat belongs to whichever character starts first; give a second
        character its own TELEGRAM_BOT_TOKEN to reach her there too."""
        return (self.name, self.token)

    async def start(self, rt) -> str:
        self.rt = rt
        self._acquire_poll_lock()
        self._client = httpx.AsyncClient(
            base_url=f"{self.api_base}/bot{self.token}",
            timeout=httpx.Timeout(POLL_TIMEOUT_S + 10, connect=10),
            transport=self._transport)
        me = await self._api("getMe")        # validates the token, loudly, now
        self._queue = rt.hub.subscribe(viewer=False)
        self._tasks = [
            asyncio.create_task(self._poll(), name="telegram-poll"),
            asyncio.create_task(self._deliver(), name="telegram-deliver"),
        ]
        who = "@" + me.get("username", "?")
        return who if self.chat_id else f"{who} · pairing ({self.chat_id_env} unset)"

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        if self._queue is not None and self.rt is not None:
            self.rt.hub.unsubscribe(self._queue)
            self._queue = None
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._release_poll_lock()

    def _acquire_poll_lock(self) -> None:
        """Refuse a second same-host poller before it reaches Telegram."""
        handle = self._poll_lock_path.open("a+")
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                handle.write("0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError) as exc:
            handle.close()
            raise RuntimeError(
                "telegram poller already held on this host; refusing duplicate "
                "getUpdates consumer") from exc
        self._poll_lock_handle = handle

    def _release_poll_lock(self) -> None:
        handle = self._poll_lock_handle
        self._poll_lock_handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    # ---- the Bot API ----

    async def _api(self, method: str, **params):
        resp = await self._client.post(f"/{method}", json=params)
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram {method}: "
                               f"{data.get('description', resp.status_code)}")
        return data["result"]

    # ---- inbound: long poll → text turns ----

    async def _poll(self) -> None:
        offset = None
        while True:
            try:
                updates = await self._api(
                    "getUpdates", timeout=POLL_TIMEOUT_S,
                    allowed_updates=["message"],
                    **({"offset": offset} if offset is not None else {}))
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the net flaps; polling resumes
                detail = repr(e)
                conflict = "Conflict: terminated by other getUpdates request" in str(e)
                delay = 30.0 if conflict else RETRY_BACKOFF_S
                level = logging.ERROR if conflict else logging.WARNING
                log.log(level, "telegram poll error (retrying in %.1fs): %s: %s",
                        delay, type(e).__name__, detail)
                await asyncio.sleep(delay)
                continue
            for update in updates:
                offset = update["update_id"] + 1
                try:
                    await self._handle_update(update)
                except asyncio.CancelledError:
                    raise
                except Exception:   # one bad message never stops the inbox
                    log.exception("telegram update failed")

    async def _handle_update(self, update: dict) -> None:
        msg = update.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if not chat_id:
            return
        if not self.chat_id:                 # pairing mode: introduce, bind nothing
            log.warning("telegram: message from chat %s but %s is unset — set "
                        "%s=%s to pair", chat_id, self.chat_id_env,
                        self.chat_id_env, chat_id)
            await self._api("sendMessage", chat_id=chat_id, text=(
                "This companion isn't paired yet. If this is your bot, set "
                f"{self.chat_id_env}={chat_id} in its .env and restart."))
            return
        if chat_id != self.chat_id:          # one person, one chat (SPEC §1)
            log.warning("telegram: ignoring message from unconfigured chat %s",
                        chat_id)
            return
        # A photo arrives as `photo` (compressed, several sizes) or as a
        # `document` (sent uncompressed), and its words — when there are any —
        # arrive as the caption rather than as text.
        text = msg.get("text") or msg.get("caption") or ""
        picture = None
        if self._picture_file(msg) is not None:
            if not self.rt.image_input:
                await self._api("sendMessage", chat_id=chat_id, text=(
                    "(I can't see pictures with the model I'm running — "
                    "tell me about it instead?)"))
                return
            picture = await self._take_photo(msg)
            if picture is None:                  # the download failed; say so
                await self._api("sendMessage", chat_id=chat_id, text=(
                    "(That picture didn't come through — try sending it again?)"))
                return
        if not text and picture is None:
            await self._api("sendMessage", chat_id=chat_id,
                            text="(I can only read text and pictures here, for now.)")
            return
        await self._api("sendChatAction", chat_id=chat_id, action="typing")
        try:
            result = await self.rt.turns.run(
                text, channel=self.name, session_id=self._session_for(chat_id),
                image_id=picture.id if picture else None)
        except EmptyReply as e:              # silence reads as being ignored; say so
            await self._api("sendMessage", chat_id=chat_id, text=(
                f"(No reply came back — {e.why}. Try sending it again?)"))
            return
        self._remember_session(chat_id, result["session_id"])
        # the reply itself arrives via _deliver — one outbound path, no echoes

    # ---- the chat's session, kept across restarts (SPEC §10.5) ----

    def _sessions_file(self) -> Path | None:
        vault = getattr(getattr(self.rt, "cfg", None), "vault_dir", None)
        return Path(vault) / "state" / "telegram_sessions.json" if vault else None

    def _session_for(self, chat_id: str) -> str | None:
        if not self._sessions_loaded:
            self._sessions_loaded = True
            path = self._sessions_file()
            try:
                if path is not None and path.is_file():
                    kept = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(kept, dict):
                        self._sessions = {**{str(k): str(v) for k, v in kept.items() if v},
                                          **self._sessions}
            except Exception:  # noqa: BLE001 — a lost map costs a window, not a turn
                log.warning("telegram: could not read the kept sessions", exc_info=True)
        return self._sessions.get(chat_id)

    def _remember_session(self, chat_id: str, session_id: str | None) -> None:
        if not session_id or self._sessions.get(chat_id) == session_id:
            return
        self._sessions[chat_id] = session_id
        path = self._sessions_file()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({k: v for k, v in self._sessions.items() if v}),
                           encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001
            log.warning("telegram: could not keep the session", exc_info=True)

    # ---- outbound: the hub → the chat ----

    async def _deliver(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                await self._deliver_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — a dropped line, not a dead channel
                log.warning("telegram delivery failed: %s", e)

    async def _deliver_event(self, event: dict) -> None:
        """Send one hub event to the chat, if it's hers to hear: committed
        assistant lines only (drafts and puppet traffic stay in the room).
        Telegram replies always return here; other origins require opt-in."""
        if not self.chat_id:
            return
        if event.get("type") != "message" or event.get("role") != "assistant":
            return
        text = event.get("text", "")
        audio = Path(event["audio_path"]) if event.get("audio_path") else None
        # A finished recording is delivery, which is what this channel is for:
        # a story voiced over twenty minutes belongs on the phone whether it was
        # asked for here, in the room, or on the desktop. Words still follow the
        # opt-in — that is the part that would be company rather than delivery.
        if audio is None and event.get("channel") != self.name \
                and not self.sending_enabled:
            return
        if audio is not None and audio.is_file():
            await self._send_audio(audio, caption=text)
            return
        image = self._selfie_path(event.get("image_url"))
        if image is not None:
            await self._send_photo(image, caption=text)
            return
        if not text:
            return
        # A blank line is where she hit send (SPEC §10.5): each paragraph is
        # its own bubble, the way a person double- and triple-texts. Each
        # bubble is still cut at Telegram's cap.
        for bubble in _bubbles(text):
            for i in range(0, len(bubble), MAX_MESSAGE_CHARS):
                await self._api("sendMessage", chat_id=self.chat_id,
                                text=bubble[i:i + MAX_MESSAGE_CHARS])

    # ---- inbound pictures (SPEC §35) ----

    def _picture_file(self, msg: dict) -> dict | None:
        """The file object to fetch, if this message is (or carries) a picture.

        `photo` is Telegram's compressed ladder, smallest first — the last entry
        is the biggest, and the biggest is the one worth showing her, because the
        shelf re-encodes it down to `CHAT_IMAGE_MAX_PX` anyway (world/uploads.py).
        A `document` is the same picture sent uncompressed, and refusing that
        would mean "I can see your photos unless you sent them properly"."""
        photos = msg.get("photo")
        if isinstance(photos, list) and photos:
            return max(photos, key=lambda p: p.get("file_size") or p.get("width") or 0)
        document = msg.get("document")
        if isinstance(document, dict) and \
                str(document.get("mime_type", "")).startswith("image/"):
            return document
        return None

    async def _take_photo(self, msg: dict):
        """Download the picture and put it on her shelf. None on any failure —
        the caller answers in words, because a picture that vanished silently is
        the one thing worse than one that was refused."""
        file = self._picture_file(msg) or {}
        size = file.get("file_size") or 0
        if size and size > self.rt.cfg.upload_max_bytes:
            log.warning("telegram: picture is %d bytes, over UPLOAD_MAX_BYTES", size)
            return None
        try:
            described = await self._api("getFile", file_id=file["file_id"])
            # The file itself is served off a different path than the API, so
            # this is the one absolute URL this adapter builds by hand.
            response = await self._client.get(
                f"{self.api_base}/file/bot{self.token}/{described['file_path']}")
            response.raise_for_status()
            data = response.content
        except asyncio.CancelledError:
            raise
        except Exception as e:      # noqa: BLE001 — a flaky net, an expired id
            log.warning("telegram: could not fetch the picture (%s)", e)
            return None
        if len(data) > self.rt.cfg.upload_max_bytes:
            log.warning("telegram: picture is %d bytes, over UPLOAD_MAX_BYTES",
                        len(data))
            return None
        try:
            return await self.rt.save_upload(data)
        except Exception as e:      # noqa: BLE001 — not an image, a full disk
            log.warning("telegram: could not keep the picture (%s)", e)
            return None

    def _selfie_path(self, image_url: str | None) -> Path | None:
        """A `message` with an image carries a local `/selfies/<name>` URL;
        resolve it against the selfie dir (jailed, like the route is)."""
        if not image_url or self.selfie_dir is None:
            return None
        base = self.selfie_dir.resolve()
        path = (base / Path(image_url).name).resolve()
        return path if path.parent == base and path.is_file() else None

    async def _send_photo(self, path: Path, caption: str) -> None:
        resp = await self._client.post(
            "/sendPhoto",
            data={"chat_id": self.chat_id, "caption": caption[:1024]},
            files={"photo": (path.name, path.read_bytes(), "image/png")})
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram sendPhoto: {data.get('description')}")
        # The same proof `_send_audio` logs: "did the page arrive?" could
        # otherwise only be answered by asking the person holding the phone.
        log.info("telegram: delivered %s (%.1f MB) as message %s", path.name,
                 path.stat().st_size / 1e6,
                 (data.get("result") or {}).get("message_id"))

    async def _send_audio(self, path: Path, caption: str) -> None:
        mime = "audio/mpeg" if path.suffix.lower() == ".mp3" else "audio/wav"
        resp = await self._client.post(
            "/sendAudio", data={"chat_id": self.chat_id, "caption": caption[:1024],
                                "title": path.stem},
            files={"audio": (path.name, path.read_bytes(), mime)})
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"telegram sendAudio: {data.get('description')}")
        # A failure was always loud and a success always silent, so "it arrived"
        # could only ever be inferred from the absence of a complaint. Telegram
        # hands back the message_id it filed; that is the proof, so log it.
        log.info("telegram: delivered %s (%.1f MB) as message %s", path.name,
                 path.stat().st_size / 1e6,
                 (data.get("result") or {}).get("message_id"))
