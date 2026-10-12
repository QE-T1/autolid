from __future__ import annotations

import asyncio
import datetime as dt
import html
import logging
import time
from typing import TYPE_CHECKING, Any

import httpx

from .ai import AIError

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("bot")

PLATFORM_ICON = {"tg": "✈️ Telegram", "vk": "🟦 VK"}
MENU = [["📊 Статус", "⚙️ Режим"], ["📋 Чаты", "📈 Статистика"], ["⏸ Пауза", "▶️ Продолжить"], ["❓ Помощь"]]
MENU_ALIASES = {"📊 Статус": "/status", "⚙️ Режим": "/mode", "📋 Чаты": "/chats", "📈 Статистика": "/stats",
                "⏸ Пауза": "/pause", "▶️ Продолжить": "/resume", "❓ Помощь": "/help"}

HELP = """<b>AutoLid - поиск лидов в чатах Telegram и VK</b>

Я присылаю сюда сообщения, где люди ищут исполнителя, вместе с готовым ответом.
Ответ уходит с вашего аккаунта реплаем на сообщение лида прямо в чате (в VK - комментарием/ответом),
и зовёт человека написать вам в личку.

<b>Кнопки под лидом</b>
✅ Отправить - ответить лиду с вашего аккаунта
🔄 Другой текст - ИИ перепишет ответ
✏️ Свой текст - ответить своим текстом
🚫 Не лид - пометить ошибку (ИИ учтёт в следующий раз)
⛔ Игнор автора - больше не присылать сообщения этого человека

<b>Команды</b>
/status - состояние программы
/mode - режим: ручной или автоответ
/chats - чаты и ошибки при вступлении
/stats - статистика лидов
/test <i>текст</i> - проверить, как программа оценит сообщение
/pause, /resume - остановить/возобновить поиск
/retry - повторить добавление чатов с ошибками

<b>Как добавить чаты</b>: положите .txt файл со ссылками (по одной на строку) в папку
<code>chats/telegram</code> или <code>chats/vk</code> - программа подхватит их сама."""


def esc(s: Any) -> str:
    return html.escape(str(s or ""), quote=False)


class BotError(Exception):
    pass


class Bot:
    def __init__(self, app: "App"):
        self.app = app
        self.token = str(app.cfg.bot.token).strip()
        self.base = f"https://api.telegram.org/bot{self.token}/"
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0))
        self.me: dict = {}
        self.awaiting_text: dict[int, int] = {}
        self.offset = 0

    async def call(self, method: str, **params: Any) -> Any:
        payload = {k: v for k, v in params.items() if v is not None}
        for attempt in range(4):
            try:
                r = await self.http.post(self.base + method, json=payload)
                data = r.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == 3:
                    raise BotError(f"сеть: {e}") from e
                await asyncio.sleep(2 + attempt * 2)
                continue
            if data.get("ok"):
                return data["result"]
            retry = (data.get("parameters") or {}).get("retry_after")
            if retry and attempt < 3:
                await asyncio.sleep(float(retry) + 0.5)
                continue
            raise BotError(data.get("description", "ошибка Bot API"))
        raise BotError("Bot API не отвечает")

    async def start(self) -> None:
        self.me = await self.call("getMe")
        await self.call("deleteWebhook", drop_pending_updates=False)
        await self.call("setMyCommands", commands=[
            {"command": "status", "description": "Состояние программы"},
            {"command": "mode", "description": "Режим: ручной / автоответ"},
            {"command": "chats", "description": "Чаты и ошибки"},
            {"command": "stats", "description": "Статистика лидов"},
            {"command": "test", "description": "Проверить текст: /test текст"},
            {"command": "pause", "description": "Пауза"},
            {"command": "resume", "description": "Продолжить"},
            {"command": "help", "description": "Помощь"},
        ])
        log.info("Бот: @%s", self.me.get("username"))
        if not self.admins():
            who = ", ".join("@" + u for u in self.allowed_usernames()) or "вы"
            log.warning("Бот пока никому не может писать: %s - откройте @%s и нажмите /start",
                        who, self.me.get("username"))

    async def close(self) -> None:
        await self.http.aclose()

    def allowed_usernames(self) -> set[str]:
        return {str(u).strip().lstrip("@").lower() for u in (self.app.cfg.bot.get("admin_usernames") or [])
                if str(u).strip()}

    def admins(self) -> set[int]:
        ids = {int(x) for x in (self.app.cfg.bot.admin_ids or []) if str(x).strip().lstrip("-").isdigit()}
        allowed = self.allowed_usernames()
        for uid, uname in (self.app.db.kv_get("admin_users", {}) or {}).items():
            if not allowed or (uname or "").lower() in allowed:
                ids.add(int(uid))
        return ids

    def _register_admin(self, user: dict) -> bool:
        allowed = self.allowed_usernames()
        uname = (user.get("username") or "").lower()
        if allowed and uname not in allowed:
            return False
        if not allowed and self.admins():
            return False
        users = self.app.db.kv_get("admin_users", {}) or {}
        users[str(user["id"])] = uname
        self.app.db.kv_set("admin_users", users)
        log.info("Бот: администратор @%s (id %s)", uname or "-", user["id"])
        return True

    async def send(self, chat_id: int, text: str, buttons: list | None = None, reply_markup: dict | None = None,
                   **kw: Any) -> dict | None:
        markup = reply_markup or ({"inline_keyboard": buttons} if buttons else None)
        try:
            return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=markup, **kw)
        except BotError as e:
            log.warning("Бот: не удалось отправить в %s: %s - пробую без кнопок-ссылок", chat_id, e)
            try:
                return await self.call("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
                                       disable_web_page_preview=True,
                                       reply_markup={"inline_keyboard": _strip_url_buttons(buttons)} if buttons else None)
            except BotError as e2:
                log.error("Бот: сообщение не доставлено (%s). Отправьте боту /start.", e2)
                return None

    async def notify_admins(self, text: str) -> None:
        for chat_id in self.admins():
            await self.send(chat_id, text)

    def lead_card(self, lead: dict) -> tuple[str, list]:
        ai = lead["ai"] or {}
        extra = lead["extra"] or {}
        conf = f" · {ai.get('confidence')}%" if ai.get("source") == "ai" else " · по ключевым словам"
        lines = [f"🔥 <b>Лид #{lead['id']}</b> · {PLATFORM_ICON.get(lead['platform'], lead['platform'])}{conf}"]
        when = dt.datetime.fromtimestamp(extra.get("msg_date") or lead["created_at"]).strftime("%H:%M")
        lines.append(f"👤 <b>Ник:</b> {author_nick(lead)}")
        lines.append(f"💬 <b>Чат:</b> {chat_ref(lead)} · 🕒 {when}")
        text = lead["text"]
        if len(text) > 1500:
            text = text[:1500] + "..."
        lines.append(f"📝 <b>Сообщение:</b>\n<blockquote>{esc(text)}</blockquote>")
        details = []
        for key, icon in (("work_type", "📌"), ("subject", "📚"), ("university", "🎓"), ("deadline", "⏰"), ("budget", "💰")):
            if ai.get(key):
                details.append(f"{icon} {esc(ai[key])}")
        if details:
            lines.append(" · ".join(details))
        if ai.get("source") == "keywords" and ai.get("summary"):
            lines.append(f"ℹ️ {esc(ai['summary'])}")
        if lead["matched"]:
            lines.append(f"🔑 <i>{esc(', '.join(lead['matched'][:10]))}</i>")
        lines.append("")
        lines.append(f"🤖 <b>Ответ ИИ:</b>\n<code>{esc(lead['reply'])}</code>")

        status = lead["status"]
        buttons: list[list[dict]] = []
        lid = lead["id"]
        if status == "scheduled":
            left = max(0, int(extra.get("send_at", time.time()) - time.time()))
            lines.append(f"\n⏳ <b>Автоответ через ~{left} с</b> (перед отправкой «печатает...»)")
            buttons.append([{"text": "⛔ Отменить автоответ", "callback_data": f"c:{lid}"}])
        elif status == "sending":
            lines.append("\n📤 Отправляю...")
        elif status == "sent":
            when_sent = dt.datetime.fromtimestamp(lead["sent_at"] or time.time()).strftime("%H:%M")
            lines.append(f"\n✅ <b>Отправлено</b> {esc(lead['method'] or '')} в {when_sent}")
        elif status == "skipped":
            lines.append("\n🚫 Помечено «не лид»")
        elif status == "ignored":
            lines.append("\n⛔ Автор в игноре")
        else:
            if status == "failed":
                lines.append(f"\n⚠️ <b>Не отправлено:</b> {esc(lead['error'])}")
            elif extra.get("auto_note") and self.app.responder.mode == "auto":
                lines.append(f"\n✋ Без автоответа: {esc(extra['auto_note'])}")
            buttons.append([{"text": "✅ Отправить", "callback_data": f"s:{lid}"},
                            {"text": "🔄 Другой текст", "callback_data": f"r:{lid}"}])
            buttons.append([{"text": "✏️ Свой текст", "callback_data": f"e:{lid}"},
                            {"text": "🚫 Не лид", "callback_data": f"n:{lid}"}])
            if lead["author_id"]:
                buttons.append([{"text": "⛔ Игнор автора", "callback_data": f"i:{lid}"}])
        links = []
        if lead["msg_link"]:
            links.append({"text": "🔗 Сообщение", "url": lead["msg_link"]})
        if lead["author_link"] and lead["author_link"].startswith("http"):
            links.append({"text": "👤 Автор", "url": lead["author_link"]})
        if links:
            buttons.append(links)
        return "\n".join(lines), buttons

    async def send_lead(self, lead_id: int) -> None:
        lead = self.app.db.lead(lead_id)
        if not lead:
            return
        admins = self.admins()
        if not admins:
            log.warning("Лид #%d не отправлен в бот: нет администратора (отправьте боту /start)", lead_id)
            return
        text, buttons = self.lead_card(lead)
        sent = []
        for chat_id in admins:
            res = await self.send(chat_id, text, buttons)
            if res:
                sent.append([chat_id, res["message_id"]])
        lead = self.app.db.lead(lead_id)
        self.app.db.update_lead(lead_id, extra={**lead["extra"], "bot_msgs": sent})

    async def notify_sent(self, lead_id: int) -> None:
        lead = self.app.db.lead(lead_id)
        if not lead:
            return
        text = lead["text"] if len(lead["text"]) <= 400 else lead["text"][:400] + "..."
        msg = (f"✅ <b>Ответил</b> {author_nick(lead)} {esc(lead['method'] or '')}\n"
               f"💬 {chat_ref(lead)}\n"
               f"📝 <i>{esc(text)}</i>\n"
               f"🤖 {esc(lead['reply'])}")
        cards = {chat_id: msg_id for chat_id, msg_id in lead["extra"].get("bot_msgs", [])}
        for chat_id in self.admins():
            reply = {"message_id": cards[chat_id], "allow_sending_without_reply": True} if chat_id in cards else None
            await self.send(chat_id, msg, reply_parameters=reply)

    async def refresh_lead(self, lead_id: int) -> None:
        lead = self.app.db.lead(lead_id)
        if not lead:
            return
        text, buttons = self.lead_card(lead)
        for chat_id, msg_id in lead["extra"].get("bot_msgs", []):
            try:
                await self.call("editMessageText", chat_id=chat_id, message_id=msg_id, text=text, parse_mode="HTML",
                                disable_web_page_preview=True, reply_markup={"inline_keyboard": buttons})
            except BotError as e:
                if "not modified" not in str(e):
                    log.debug("Бот: не удалось обновить карточку #%d: %s", lead_id, e)

    async def poll_loop(self) -> None:
        while True:
            try:
                updates = await self.call("getUpdates", offset=self.offset, timeout=50,
                                          allowed_updates=["message", "callback_query"])
            except BotError as e:
                log.warning("Бот: getUpdates: %s", e)
                await asyncio.sleep(5)
                continue
            for u in updates:
                self.offset = u["update_id"] + 1
                try:
                    if "callback_query" in u:
                        await self._on_callback(u["callback_query"])
                    elif "message" in u:
                        await self._on_message(u["message"])
                except Exception:
                    log.exception("Бот: ошибка обработки апдейта")

    async def _on_message(self, msg: dict) -> None:
        chat_id = msg["chat"]["id"]
        user_id = msg.get("from", {}).get("id")
        text = (msg.get("text") or "").strip()
        if msg["chat"].get("type") != "private":
            return
        if user_id not in self.admins():
            if text.startswith("/start") and self._register_admin(msg.get("from", {})):
                await self.send(chat_id, "✅ Вы администратор. Сюда будут приходить лиды и ответы ИИ.",
                                reply_markup={"keyboard": [[{"text": b} for b in row] for row in MENU],
                                              "resize_keyboard": True})
                await self.send(chat_id, HELP)
            else:
                await self.send(chat_id, "⛔ Нет доступа.")
            return
        text = MENU_ALIASES.get(text, text)
        if chat_id in self.awaiting_text and not text.startswith("/"):
            lead_id = self.awaiting_text.pop(chat_id)
            await self.send(chat_id, f"📤 Отправляю ваш текст лиду #{lead_id}...")
            ok, info = await self.app.responder.send(lead_id, text, manual=True)
            await self.send(chat_id, f"✅ Отправлено {esc(info)}" if ok else f"⚠️ Не отправлено: {esc(info)}")
            return
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        if cmd == "/cancel":
            self.awaiting_text.pop(chat_id, None)
            await self.send(chat_id, "Отменено.")
        elif cmd in ("/start", "/help"):
            await self.send(chat_id, HELP,
                            reply_markup={"keyboard": [[{"text": b} for b in row] for row in MENU], "resize_keyboard": True})
        elif cmd == "/status":
            await self.send(chat_id, self.status_text())
        elif cmd == "/mode":
            await self.send(chat_id, self._mode_text(), self._mode_buttons())
        elif cmd == "/pause":
            self.app.db.kv_set("paused", True)
            await self.send(chat_id, "⏸ Поиск лидов на паузе. /resume - продолжить.")
        elif cmd == "/resume":
            self.app.db.kv_set("paused", False)
            await self.send(chat_id, "▶️ Поиск лидов продолжен.")
        elif cmd == "/chats":
            await self.send(chat_id, self.chats_text())
        elif cmd == "/stats":
            await self.send(chat_id, self.stats_text())
        elif cmd == "/retry":
            n = self.app.db.x("UPDATE chats SET status='pending', error=NULL WHERE status='failed' AND in_files=1").rowcount
            self.app.wake_workers()
            await self.send(chat_id, f"🔁 Повторно добавляю {n} чатов.")
        elif cmd == "/test":
            if not arg.strip():
                await self.send(chat_id, "Напишите так: <code>/test текст сообщения</code>")
                return
            await self.send(chat_id, "🔎 Проверяю...")
            await self.send(chat_id, await self.app.pipeline.check_text(arg.strip()))
        else:
            await self.send(chat_id, "Не понял команду. /help - список команд.")

    async def _on_callback(self, cq: dict) -> None:
        user_id = cq.get("from", {}).get("id")
        data = cq.get("data") or ""
        chat_id = (cq.get("message") or {}).get("chat", {}).get("id")
        if user_id not in self.admins():
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Нет доступа")
            return
        action, _, arg = data.partition(":")
        answer = ""
        responder = self.app.responder
        if action == "m":
            if arg in ("auto", "manual"):
                responder.set_mode(arg)
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Режим изменён")
            try:
                await self.call("editMessageText", chat_id=chat_id, message_id=cq["message"]["message_id"],
                                text=self._mode_text(), parse_mode="HTML",
                                reply_markup={"inline_keyboard": self._mode_buttons()})
            except BotError:
                pass
            return
        lead_id = int(arg) if arg.isdigit() else 0
        lead = self.app.db.lead(lead_id)
        if not lead:
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Лид не найден")
            return
        if action == "s":
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Отправляю...")
            ok, info = await responder.send(lead_id, manual=True)
            if not ok:
                await self.send(chat_id, f"⚠️ Лид #{lead_id}: не отправлено - {esc(info)}")
            return
        if action == "r":
            await self.call("answerCallbackQuery", callback_query_id=cq["id"], text="Пишу другой вариант...")
            try:
                reply = await self.app.ai.write_reply(lead["text"], lead["ai"], previous=lead["reply"] or "")
            except AIError as e:
                reply = self.app.ai.template_reply(lead["author_name"] if lead["author_id"] else "")
                await self.send(chat_id, f"ИИ недоступен ({esc(e)}), взял шаблон из prompts/templates.txt")
            self.app.db.update_lead(lead_id, reply=reply)
            await self.refresh_lead(lead_id)
            return
        if action == "e":
            self.awaiting_text[chat_id] = lead_id
            answer = "Пришлите текст ответа"
            await self.send(chat_id, f"✏️ Пришлите текст, который отправить лиду #{lead_id}. /cancel - отмена.")
        elif action == "n":
            responder.cancel(lead_id)
            self.app.db.update_lead(lead_id, status="skipped")
            if self.app.cfg.ai.use_feedback:
                self.app.db.add_feedback(lead["text"], 0)
            answer = "Учту"
        elif action == "i":
            responder.cancel(lead_id)
            if lead["author_id"]:
                self.app.db.ignore_author(lead["platform"], lead["author_id"])
            self.app.db.update_lead(lead_id, status="ignored")
            answer = "Автор в игноре"
        elif action == "c":
            answer = "Автоответ отменён" if responder.cancel(lead_id) else "Уже поздно"
        await self.call("answerCallbackQuery", callback_query_id=cq["id"], text=answer)
        await self.refresh_lead(lead_id)

    def _mode_text(self) -> str:
        mode = self.app.responder.mode
        rc = self.app.cfg.reply
        return ("<b>Режим ответа лидам</b>\n\n"
                f"{'🔘' if mode == 'manual' else '⚪️'} <b>Ручной</b> - присылаю лида с готовым ответом, "
                "вы жмёте «Отправить»\n"
                f"{'🔘' if mode == 'auto' else '⚪️'} <b>Автоответ</b> - отвечаю сам через {_delay_text(rc)} "
                "(последние секунды аккаунт «печатает...», можно отменить), "
                f"не больше {rc.max_per_hour} в час и {rc.max_per_day} в сутки")

    def _mode_buttons(self) -> list:
        return [[{"text": "✋ Ручной", "callback_data": "m:manual"}, {"text": "🤖 Автоответ", "callback_data": "m:auto"}]]

    def status_text(self) -> str:
        app = self.app
        paused = app.db.kv_get("paused", False)
        mode = "🤖 автоответ" if app.responder.mode == "auto" else "✋ ручной"
        lines = [f"{'⏸ <b>На паузе</b>' if paused else '🟢 <b>Работает</b>'} · режим: {mode}", ""]
        lines.append(f"✈️ Telegram: {esc(app.tg.describe()) if app.tg else 'не подключён'}")
        lines.append(f"🟦 VK: {esc(app.vk.describe()) if app.vk else 'не подключён (нет токена)'}")
        lines.append(f"🤖 ИИ: {esc(app.ai.status())}" + (f" · {esc(app.cfg.ai.model)}" if app.ai.enabled else ""))
        c = app.pipeline.counters
        up = int((time.time() - app.started_at) / 60)
        lines.append("")
        lines.append(f"С момента запуска ({up // 60} ч {up % 60} мин): сообщений {c['messages']}, "
                     f"кандидатов {c['candidates']}, глубокая проверка {c['deep']}, лидов {c['leads']}")
        lines.append(f"ИИ: запросов {app.ai.stats['requests']}, ошибок {app.ai.stats['errors']}")
        rc = app.cfg.reply
        lines.append(f"Отправлено: за час {app.db.sent_count(3600)}/{rc.max_per_hour}, "
                     f"за сутки {app.db.sent_count(86400)}/{rc.max_per_day}")
        for p, name in (("tg", "Telegram"), ("vk", "VK")):
            until = app.responder.blocked_until(p)
            if until > time.time():
                lines.append(f"⚠️ {name}: отправка ограничена ещё ~{int((until - time.time()) / 60) + 1} мин")
        return "\n".join(lines)

    def chats_text(self) -> str:
        app = self.app
        lines = ["<b>Чаты</b>"]
        for platform, name in (("tg", "✈️ Telegram"), ("vk", "🟦 VK")):
            st = app.db.chat_stats(platform)
            total = sum(st.values())
            lines.append(f"\n{name}: всего {total} - активно {st.get('active', 0)}, в очереди {st.get('pending', 0)}, "
                         f"заявок {st.get('requested', 0)}, ошибок {st.get('failed', 0)}")
            failed = app.db.chats(platform, status="failed")[:15]
            for r in failed:
                lines.append(f"  • {esc(r['link'])} - {esc(r['error'])}")
            if len(app.db.chats(platform, status="failed")) > 15:
                lines.append("  ...")
        pending_tg = app.db.chat_stats("tg").get("pending", 0)
        if pending_tg:
            avg = (float(app.cfg.telegram.join_delay_min) + float(app.cfg.telegram.join_delay_max)) / 2
            lines.append(f"\n⏳ Вступление в {pending_tg} чатов TG займёт ~{int(pending_tg * avg / 60)} мин "
                         "(паузы защищают аккаунт от бана)")
        lines.append("\n/retry - повторить чаты с ошибками")
        return "\n".join(lines)

    def stats_text(self) -> str:
        db = self.app.db
        lines = ["<b>Статистика лидов</b>"]
        for title, sec in (("24 часа", 86400), ("7 дней", 7 * 86400), ("30 дней", 30 * 86400)):
            since = time.time() - sec
            rows = db.q("SELECT status, COUNT(*) c FROM leads WHERE created_at > ? GROUP BY status", (since,))
            st = {r["status"]: r["c"] for r in rows}
            total = sum(st.values())
            lines.append(f"\n<b>{title}</b>: лидов {total}, отправлено {st.get('sent', 0)}, "
                         f"не лид {st.get('skipped', 0)}, ошибок {st.get('failed', 0)}")
            top = db.q("SELECT chat_title, COUNT(*) c FROM leads WHERE created_at > ? AND chat_title != '' GROUP BY chat_title "
                       "ORDER BY c DESC LIMIT 3", (since,))
            if top and sec == 7 * 86400:
                lines.append("Топ чатов: " + ", ".join(f"{esc(r['chat_title'])} ({r['c']})" for r in top))
        return "\n".join(lines)


def _delay_text(rc) -> str:
    lo, hi = float(rc.delay_min), float(rc.delay_max)
    return f"{lo:g} с" if hi <= lo else f"{lo:g}–{hi:g} с"


def author_nick(lead: dict) -> str:
    name = esc(lead.get("author_name") or "")
    uname = esc(lead.get("author_username") or "")
    uid = lead.get("author_id")
    if lead.get("platform") == "tg":
        if uname:
            return f'<a href="https://t.me/{uname}">@{uname}</a>' + (f" ({name})" if name else "")
        if uid:
            return f'<a href="tg://user?id={uid}">{name or uid}</a> (ника нет, id {uid})'
    else:
        if uname:
            return f'<a href="https://vk.com/{uname}">vk.com/{uname}</a>' + (f" ({name})" if name else "")
        if uid and uid > 0:
            return f'<a href="https://vk.com/id{uid}">vk.com/id{uid}</a>' + (f" ({name})" if name else "")
    return (name + " - " if name else "") + "автор неизвестен"


def chat_ref(lead: dict) -> str:
    title = esc(lead.get("chat_title") or "чат")
    return f'<a href="{esc(lead["chat_link"])}">{title}</a>' if lead.get("chat_link") else title


def _strip_url_buttons(buttons: list | None) -> list:
    out = []
    for row in buttons or []:
        row2 = [b for b in row if "url" not in b]
        if row2:
            out.append(row2)
    return out
