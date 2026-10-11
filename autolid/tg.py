from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import time
from typing import TYPE_CHECKING

from telethon import TelegramClient, events, errors, utils
from telethon.tl.functions.channels import GetFullChannelRequest, JoinChannelRequest
from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
from telethon.tl.types import (Channel, Chat, ChatInviteAlready, ChatInvitePeek, InputPeerUserFromMessage,
                               PeerChannel, User)

from .models import Message
from .responder import SendError

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("telegram")

SEND_ORDER = {"chat": ["chat"], "chat_then_dm": ["chat", "dm"], "dm": ["dm"], "dm_then_chat": ["dm", "chat"]}


def make_client(cfg, data_dir) -> TelegramClient:
    return TelegramClient(
        str(data_dir / "telegram"),
        int(cfg.telegram.api_id),
        str(cfg.telegram.api_hash),
        lang_code="ru",
        system_lang_code="ru",
        flood_sleep_threshold=60,
    )


def _username(entity) -> str:
    name = getattr(entity, "username", None)
    if name:
        return name
    for u in getattr(entity, "usernames", None) or []:
        if getattr(u, "active", True):
            return u.username
    return ""


def _display_name(entity) -> str:
    if isinstance(entity, User):
        return " ".join(x for x in (entity.first_name, entity.last_name) if x) or (entity.username or str(entity.id))
    return getattr(entity, "title", "") or ""


class TelegramWorker:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg.telegram
        self.client = make_client(app.cfg, app.data_dir)
        self.me: User | None = None
        self.active_ids: set[int] = set()
        self.dialogs: dict[str, object] = {}
        self.dialog_ids: set[int] = set()
        self.wake = asyncio.Event()
        self.joins_stopped = ""

    async def start(self) -> None:
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise RuntimeError("Telegram-аккаунт не авторизован. Запустите setup (мастер настройки).")
        self.me = await self.client.get_me()
        log.info("Telegram: вошли как %s (@%s)", _display_name(self.me), self.me.username or "-")
        await self._load_dialogs()
        self.reload_active()
        self.client.add_event_handler(self._on_message, events.NewMessage(incoming=True))

    async def _load_dialogs(self) -> None:
        count = 0
        async for d in self.client.iter_dialogs():
            ent = d.entity
            if isinstance(ent, (Channel, Chat)):
                count += 1
                self.dialog_ids.add(utils.get_peer_id(ent))
                name = _username(ent)
                if name:
                    self.dialogs[name.lower()] = ent
        log.info("Telegram: аккаунт состоит в %d группах/каналах", count)

    def reload_active(self) -> None:
        ids: set[int] = set()
        for row in self.app.db.chats("tg", status="active"):
            if row["peer_id"]:
                ids.add(row["peer_id"])
            extra = json.loads(row["extra"]) if row["extra"] else {}
            if extra.get("linked_peer_id"):
                ids.add(extra["linked_peer_id"])
        self.active_ids = ids

    def describe(self) -> str:
        if not self.me:
            return "не подключён"
        name = f"@{self.me.username}" if self.me.username else _display_name(self.me)
        st = self.app.db.chat_stats("tg")
        parts = [f"активно {st.get('active', 0)}"]
        if st.get("pending"):
            parts.append(f"в очереди на вступление {st['pending']}")
        if st.get("requested"):
            parts.append(f"заявок на рассмотрении {st['requested']}")
        if st.get("failed"):
            parts.append(f"ошибок {st['failed']}")
        s = f"{name} - чаты: " + ", ".join(parts)
        if self.cfg.monitor_all_joined:
            s += f" (+ все группы аккаунта: {len(self.dialog_ids)})"
        if self.joins_stopped:
            s += f"\n   ⚠️ вступления остановлены: {self.joins_stopped}"
        return s

    async def run(self) -> None:
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        await self.client.disconnect()

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        if chat_id is None:
            return
        if chat_id not in self.active_ids:
            if not (self.cfg.monitor_all_joined and event.is_group):
                return
        await self._handle(chat_id, event.message)

    async def history_loop(self) -> None:
        minutes = float(self.cfg.get("history_check_minutes", 10) or 0)
        if minutes <= 0:
            return
        while True:
            await asyncio.sleep(minutes * 60)
            for peer in list(self.active_ids):
                try:
                    msgs = await self.client.get_messages(peer, limit=15)
                    for msg in reversed(msgs):
                        if not msg.out:
                            await self._handle(peer, msg)
                except errors.FloodWaitError as e:
                    await asyncio.sleep(e.seconds + 1)
                except (ValueError, TypeError, errors.RPCError) as e:
                    log.debug("Telegram: история %s: %s", peer, e)
                await asyncio.sleep(1.0)

    async def _handle(self, chat_id: int, msg) -> None:
        try:
            text = msg.message or ""
            pipeline = self.app.pipeline
            score = pipeline.prefilter("tg", chat_id, str(msg.id), text, msg.date.timestamp())
            if score is None:
                return
            sender = await msg.get_sender()
            if isinstance(sender, User) and (sender.bot or sender.is_self):
                return
            chat = await msg.get_chat()
            chat_username = _username(chat)
            chat_link = f"https://t.me/{chat_username}" if chat_username else ""
            if chat_username:
                msg_link = f"https://t.me/{chat_username}/{msg.id}"
            elif isinstance(chat, Channel):
                msg_link = f"https://t.me/c/{chat.id}/{msg.id}"
            else:
                msg_link = ""
            author_id = None
            author_username = ""
            author_link = ""
            author_name = ""
            if isinstance(sender, User):
                author_id = sender.id
                author_username = _username(sender)
                author_name = _display_name(sender)
                author_link = f"https://t.me/{author_username}" if author_username else f"tg://user?id={sender.id}"
            elif sender is not None:
                author_name = _display_name(sender)
            m = Message(
                platform="tg", chat_id=chat_id, msg_id=str(msg.id), text=text, date=msg.date.timestamp(),
                chat_title=_display_name(chat), chat_link=chat_link, msg_link=msg_link,
                author_id=author_id, author_name=author_name, author_username=author_username,
                author_link=author_link, reply_ctx={"chat_id": chat_id, "msg_id": msg.id},
            )
            await pipeline.submit(m, score)
        except Exception:
            log.exception("Ошибка обработки сообщения Telegram")

    async def join_loop(self) -> None:
        last_requested_check = 0.0
        while True:
            block = float(self.app.db.kv_get("tg_join_block", 0) or 0)
            if block > time.time():
                await asyncio.sleep(min(600, block - time.time()) + 1)
                continue
            if time.time() - last_requested_check > 600:
                last_requested_check = time.time()
                await self._recheck_requested()
            pending = [] if self.joins_stopped else self.app.db.chats("tg", status="pending")
            if not pending:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue
            row = pending[0]
            used_network = await self._join(row)
            self.reload_active()
            if used_network:
                await asyncio.sleep(random.uniform(float(self.cfg.join_delay_min),
                                                   max(float(self.cfg.join_delay_min), float(self.cfg.join_delay_max))))

    async def _activate(self, key: str, entity, extra: dict | None = None) -> None:
        peer_id = utils.get_peer_id(entity)
        self.dialog_ids.add(peer_id)
        self.app.db.update_chat("tg", key, status="active", peer_id=peer_id, title=_display_name(entity),
                                username=_username(entity) or None, error=None, extra=extra or {})
        log.info("Telegram: слежу за «%s»", _display_name(entity))

    async def _join_linked(self, channel: Channel) -> dict:
        if not (channel.broadcast and self.cfg.join_linked_discussion):
            return {}
        try:
            full = await self.client(GetFullChannelRequest(channel))
            linked_id = full.full_chat.linked_chat_id
            if not linked_id:
                return {}
            linked = next((c for c in full.chats if c.id == linked_id), None)
            if linked is None:
                return {}
            if getattr(linked, "left", True):
                await asyncio.sleep(random.uniform(5, 15))
                await self.client(JoinChannelRequest(linked))
            return {"linked_peer_id": utils.get_peer_id(linked), "linked_title": linked.title}
        except errors.RPCError as e:
            log.warning("Не удалось вступить в чат комментариев канала %s: %s", channel.title, e)
            return {}

    async def _join(self, row) -> bool:
        key, kind = row["key"], row["kind"]
        db = self.app.db
        try:
            if kind == "username":
                name = key[1:]
                ent = self.dialogs.get(name)
                if ent is not None and not getattr(ent, "left", False):
                    extra = await self._join_linked(ent) if isinstance(ent, Channel) else {}
                    await self._activate(key, ent, extra)
                    return bool(extra)
                ent = await self.client.get_entity(name)
                if isinstance(ent, User):
                    db.update_chat("tg", key, status="failed", error="это пользователь/бот, а не чат")
                    return True
                if isinstance(ent, Channel) and getattr(ent, "left", True):
                    try:
                        await self.client(JoinChannelRequest(ent))
                    except errors.InviteRequestSentError:
                        db.update_chat("tg", key, status="requested", title=ent.title,
                                       error="вступление по заявке - заявка отправлена, жду одобрения админа")
                        log.info("Telegram: отправлена заявка в «%s»", ent.title)
                        return True
                    ent = await self.client.get_entity(ent)
                extra = await self._join_linked(ent) if isinstance(ent, Channel) else {}
                await self._activate(key, ent, extra)
                return True

            if kind == "invite":
                h = key[1:]
                info = await self.client(CheckChatInviteRequest(h))
                if isinstance(info, (ChatInviteAlready, ChatInvitePeek)) and not getattr(info.chat, "left", False):
                    await self._activate(key, info.chat)
                    return True
                try:
                    upd = await self.client(ImportChatInviteRequest(h))
                    chat = upd.chats[0]
                except errors.UserAlreadyParticipantError:
                    info = await self.client(CheckChatInviteRequest(h))
                    chat = info.chat
                except errors.InviteRequestSentError:
                    title = getattr(info, "title", None)
                    db.update_chat("tg", key, status="requested", title=title,
                                   error="вступление по заявке - заявка отправлена, жду одобрения админа")
                    log.info("Telegram: отправлена заявка в «%s»", title or row["link"])
                    return True
                extra = await self._join_linked(chat) if isinstance(chat, Channel) else {}
                await self._activate(key, chat, extra)
                return True

            if kind == "private":
                try:
                    ent = await self.client.get_entity(PeerChannel(int(key[2:])))
                    await self._activate(key, ent)
                except (ValueError, errors.RPCError):
                    db.update_chat("tg", key, status="failed",
                                   error="ссылка t.me/c/... работает только если вы уже в чате - нужна ссылка-приглашение")
                return False
            db.update_chat("tg", key, status="failed", error="неизвестный формат ссылки")
            return False

        except errors.FloodWaitError as e:
            until = time.time() + e.seconds + 5
            db.kv_set("tg_join_block", until)
            log.warning("Telegram: лимит на вступления, жду %d с", e.seconds)
            if e.seconds > 300:
                await self.app.bot.notify_admins(
                    f"⏳ Telegram временно ограничил вступление в чаты на {e.seconds // 60} мин. "
                    f"Продолжу автоматически. Осталось в очереди: {len(db.chats('tg', status='pending'))}")
            return False
        except (errors.ChannelsTooMuchError, errors.UserChannelsTooMuchError):
            self.joins_stopped = "аккаунт состоит в максимальном числе групп (500, с Premium - 1000)"
            await self.app.bot.notify_admins(
                "⚠️ Telegram: достигнут лимит групп на аккаунте (500, с Premium - 1000). "
                "Выйдите из ненужных чатов или подключите второй аккаунт, затем перезапустите программу.")
            return False
        except (errors.InviteHashExpiredError, errors.InviteHashInvalidError, errors.InviteHashEmptyError):
            db.update_chat("tg", key, status="failed", error="ссылка-приглашение недействительна или истекла")
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
            db.update_chat("tg", key, status="failed", error="чат с таким именем не найден")
        except errors.ChannelPrivateError:
            db.update_chat("tg", key, status="failed", error="чат приватный или вас там заблокировали")
        except ValueError as e:
            db.update_chat("tg", key, status="failed", error=f"не найден: {e}"[:200])
        except errors.RPCError as e:
            db.update_chat("tg", key, status="failed", error=f"{type(e).__name__}: {e}"[:200])
        except (ConnectionError, asyncio.TimeoutError) as e:
            log.warning("Telegram: сеть при вступлении: %s", e)
            await asyncio.sleep(30)
            return False
        except Exception as e:
            log.exception("Telegram: неожиданная ошибка при добавлении %s", row["link"])
            db.update_chat("tg", key, status="failed", error=f"{type(e).__name__}: {e}"[:200])
        log.info("Telegram: не удалось добавить %s - %s", row["link"], db.one(
            "SELECT error FROM chats WHERE platform='tg' AND key=?", (key,))["error"])
        return True

    async def _recheck_requested(self) -> None:
        for row in self.app.db.chats("tg", status="requested"):
            chat = None
            try:
                if row["kind"] == "invite":
                    info = await self.client(CheckChatInviteRequest(row["key"][1:]))
                    if isinstance(info, ChatInviteAlready):
                        chat = info.chat
                elif row["kind"] == "username":
                    ent = await self.client.get_entity(row["key"][1:])
                    if not getattr(ent, "left", True):
                        chat = ent
            except (errors.RPCError, ValueError):
                pass
            if chat is not None:
                extra = await self._join_linked(chat) if isinstance(chat, Channel) else {}
                await self._activate(row["key"], chat, extra)
                await self.app.bot.notify_admins(
                    f"✅ Заявку одобрили - теперь слежу за чатом «{html.escape(_display_name(chat))}»")
            await asyncio.sleep(3)
        self.reload_active()

    async def show_typing(self, lead: dict, seconds: float) -> None:
        order = SEND_ORDER.get(self.app.cfg.reply.telegram_method, ["chat"])
        ctx = lead["extra"].get("ctx", {})
        if order[0] == "chat" or not lead["author_id"]:
            peer = ctx.get("chat_id")
        else:
            peer = await self._user_peer(lead, ctx.get("chat_id"), ctx.get("msg_id"))
        if peer is None:
            await asyncio.sleep(seconds)
            return
        async with self.client.action(peer, "typing"):
            await asyncio.sleep(seconds)

    @staticmethod
    def _typing_time(text: str, typing: float | None) -> float:
        return min(6.0, 1.5 + len(text) / 30) if typing is None else typing

    async def send_reply(self, lead: dict, text: str, typing: float | None = None) -> str:
        order = SEND_ORDER.get(self.app.cfg.reply.telegram_method, ["chat"])
        ctx = lead["extra"].get("ctx", {})
        chat_id, msg_id = ctx.get("chat_id"), ctx.get("msg_id")
        failed: list[str] = []
        for way in order:
            try:
                if way == "chat":
                    await self._send_chat(text, chat_id, msg_id, typing)
                    result = "ответом в чате"
                else:
                    if not lead["author_id"]:
                        failed.append("в личку: автор неизвестен")
                        continue
                    await self._send_dm(lead, text, chat_id, msg_id, typing)
                    result = "в личку"
                return result + (f" ({'; '.join(failed)})" if failed else "")
            except SendError as e:
                if e.block_seconds:
                    raise
                failed.append(str(e))
            except Exception as e:
                failed.append(f"{'в чат' if way == 'chat' else 'в личку'}: {e or type(e).__name__}")
            log.info("Лид #%d: не вышло (%s)", lead["id"], failed[-1])
        raise SendError("; ".join(failed) or "некуда ответить")

    async def _send_chat(self, text: str, chat_id, msg_id, typing: float | None = None) -> None:
        if chat_id is None or msg_id is None:
            raise SendError("в чат: нет данных о сообщении")
        seconds = self._typing_time(text, typing)
        if seconds > 0:
            try:
                async with self.client.action(chat_id, "typing"):
                    await asyncio.sleep(seconds)
            except errors.RPCError:
                pass
        for attempt in range(2):
            try:
                await self.client.send_message(chat_id, text, reply_to=int(msg_id))
                return
            except errors.SlowModeWaitError as e:
                if e.seconds <= 60 and attempt == 0:
                    await asyncio.sleep(e.seconds + 1)
                    continue
                raise SendError(f"в чат: медленный режим, писать можно через {e.seconds} с")
            except errors.FloodWaitError as e:
                if e.seconds <= 30 and attempt == 0:
                    await asyncio.sleep(e.seconds + 1)
                    continue
                raise SendError(f"флуд-лимит Telegram {e.seconds} с", block_seconds=e.seconds if e.seconds > 60 else 0)
            except (errors.ChatWriteForbiddenError, errors.ChatSendPlainForbiddenError, errors.ChatAdminRequiredError,
                    errors.ChatGuestSendForbiddenError):
                raise SendError("в чат: в этом чате нельзя писать (нет прав)")
            except (errors.UserBannedInChannelError, errors.ChatRestrictedError):
                raise SendError("в чат: аккаунт ограничен в этом чате "
                                "(если так во всех чатах - проверьте спам-блок через @SpamBot)")
            except errors.MsgIdInvalidError:
                raise SendError("в чат: сообщение лида уже удалено")
            except errors.RPCError as e:
                raise SendError(f"в чат: {e}")

    async def _user_peer(self, lead: dict, chat_id, msg_id):
        uid = lead["author_id"]
        try:
            return await self.client.get_input_entity(uid)
        except (ValueError, TypeError):
            if lead["author_username"]:
                return await self.client.get_input_entity(lead["author_username"])
            if chat_id is not None and msg_id is not None:
                return InputPeerUserFromMessage(await self.client.get_input_entity(chat_id), int(msg_id), uid)
            raise

    async def _send_dm(self, lead: dict, text: str, chat_id, msg_id, typing: float | None = None) -> None:
        uid = lead["author_id"]
        peer = await self._user_peer(lead, chat_id, msg_id)
        seconds = self._typing_time(text, typing)
        if seconds > 0:
            try:
                async with self.client.action(peer, "typing"):
                    await asyncio.sleep(seconds)
            except errors.RPCError:
                pass
        try:
            await self.client.send_message(peer, text)
        except errors.PeerFloodError:
            raise SendError("Telegram считает отправку спамом (PeerFlood) - пишите меньше незнакомым людям",
                            block_seconds=6 * 3600)
        except errors.FloodWaitError as e:
            if e.seconds <= 30:
                await asyncio.sleep(e.seconds + 1)
                await self.client.send_message(peer, text)
            else:
                raise SendError(f"флуд-лимит {e.seconds} с", block_seconds=e.seconds)
        except errors.PeerIdInvalidError:
            if chat_id is None or msg_id is None:
                raise
            peer = InputPeerUserFromMessage(await self.client.get_input_entity(chat_id), int(msg_id), uid)
            await self.client.send_message(peer, text)
