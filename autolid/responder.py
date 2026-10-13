from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("responder")

PLATFORM_NAMES = {"tg": "Telegram", "vk": "VK"}


class SendError(Exception):
    def __init__(self, message: str, block_seconds: int = 0):
        super().__init__(message)
        self.block_seconds = block_seconds


class Responder:
    def __init__(self, app: "App"):
        self.app = app
        self.tasks: dict[int, asyncio.Task] = {}

    @property
    def mode(self) -> str:
        return self.app.db.kv_get("mode", self.app.cfg.reply.mode)

    def set_mode(self, mode: str) -> None:
        self.app.db.kv_set("mode", mode)

    def blocked_until(self, platform: str) -> float:
        return float(self.app.db.kv_get(f"send_block_{platform}", 0) or 0)

    def limits_reason(self) -> str | None:
        rc = self.app.cfg.reply
        if self.app.db.sent_count(3600) >= int(rc.max_per_hour):
            return f"достигнут лимит {rc.max_per_hour} сообщений в час"
        if self.app.db.sent_count(86400) >= int(rc.max_per_day):
            return f"достигнут лимит {rc.max_per_day} сообщений в сутки"
        return None

    def auto_block_reason(self, lead_id: int) -> str | None:
        if self.mode != "auto":
            return "ручной режим"
        lead = self.app.db.lead(lead_id)
        if lead is None:
            return "лид не найден"
        if lead["ai"].get("source") == "keywords" and self.app.ai.enabled:
            return "найден без ИИ (ИИ был недоступен) - проверьте вручную"
        platform = lead["platform"]
        if platform == "tg" and not self.app.tg:
            return "Telegram не подключён"
        if platform == "vk" and not self.app.vk:
            return "VK не подключён"
        if self.blocked_until(platform) > time.time():
            left = int((self.blocked_until(platform) - time.time()) / 60) + 1
            return f"{PLATFORM_NAMES[platform]} ограничил отправку, автоответы на паузе ещё ~{left} мин"
        if not lead["author_id"] and not self._can_reply_in_place(lead):
            return "автор неизвестен, а ответ в чат выключен - ответить можно только вручную"
        if lead["author_id"] and self.app.db.contacted_recently(
                platform, lead["author_id"], float(self.app.cfg.reply.author_cooldown_hours)):
            return "этому человеку уже писали недавно"
        return self.limits_reason()

    def _can_reply_in_place(self, lead: dict) -> bool:
        if lead["platform"] == "tg":
            return "chat" in self.app.cfg.reply.telegram_method
        ctx = lead["extra"].get("ctx", {})
        method = self.app.cfg.reply.vk_method
        return method != "dm" and bool(ctx.get("post_id") or ctx.get("topic_id") or ctx.get("peer_id"))

    def schedule(self, lead_id: int) -> None:
        rc = self.app.cfg.reply
        delay = random.uniform(float(rc.delay_min), max(float(rc.delay_min), float(rc.delay_max)))
        lead = self.app.db.lead(lead_id)
        self.app.db.update_lead(lead_id, status="scheduled",
                                extra={**lead["extra"], "send_at": time.time() + delay})
        task = asyncio.create_task(self._delayed(lead_id, delay))
        self.tasks[lead_id] = task
        task.add_done_callback(lambda _t, i=lead_id: self.tasks.pop(i, None))

    async def _delayed(self, lead_id: int, delay: float) -> None:
        lead = self.app.db.lead(lead_id)
        typing = min(max(0.0, delay - 1), max(4.0, len(lead["reply"] or "") / 12)) if lead else 0
        await asyncio.sleep(delay - typing)
        lead = self.app.db.lead(lead_id)
        if not lead or lead["status"] != "scheduled":
            return
        reason = self.limits_reason()
        if reason:
            self.app.db.update_lead(lead_id, status="new", extra={**lead["extra"], "auto_note": reason})
            await self.app.bot.refresh_lead(lead_id)
            return
        worker = self.app.tg if lead["platform"] == "tg" else self.app.vk
        if worker and typing > 0:
            try:
                await worker.show_typing(lead, typing)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.debug("Лид #%d: не удалось показать «печатает»: %s", lead_id, e)
        lead = self.app.db.lead(lead_id)
        if not lead or lead["status"] != "scheduled":
            return
        await self.send(lead_id, typing=0)

    def cancel(self, lead_id: int) -> bool:
        task = self.tasks.pop(lead_id, None)
        if task:
            task.cancel()
        lead = self.app.db.lead(lead_id)
        if lead and lead["status"] == "scheduled":
            self.app.db.update_lead(lead_id, status="new", extra={**lead["extra"], "auto_note": "автоответ отменён"})
            return True
        return False

    async def send(self, lead_id: int, text: str | None = None, manual: bool = False,
                   typing: float | None = None) -> tuple[bool, str]:
        app = self.app
        lead = app.db.lead(lead_id)
        if lead is None:
            return False, "лид не найден"
        if lead["status"] == "sent":
            return False, "уже отправлено"
        if lead["status"] == "sending":
            return False, "уже отправляется"
        text = (text or lead["reply"] or "").strip()
        if not text:
            return False, "пустой текст ответа"
        platform = lead["platform"]
        worker = app.tg if platform == "tg" else app.vk
        if worker is None:
            return False, f"{PLATFORM_NAMES.get(platform, platform)} не подключён"
        app.db.update_lead(lead_id, status="sending", reply=text)
        try:
            method = await worker.send_reply(lead, text, typing=typing)
        except SendError as e:
            if e.block_seconds:
                until = time.time() + e.block_seconds
                app.db.kv_set(f"send_block_{platform}", until)
                await app.bot.notify_admins(
                    f"⚠️ {PLATFORM_NAMES[platform]} ограничил отправку сообщений: {e}.\n"
                    f"Автоответы для {PLATFORM_NAMES[platform]} приостановлены на ~{e.block_seconds // 60} мин.")
            app.db.update_lead(lead_id, status="failed", error=str(e))
            await app.bot.refresh_lead(lead_id)
            log.warning("Лид #%d: не отправлено - %s", lead_id, e)
            return False, str(e)
        except Exception as e:
            log.exception("Лид #%d: ошибка отправки", lead_id)
            app.db.update_lead(lead_id, status="failed", error=f"{type(e).__name__}: {e}")
            await app.bot.refresh_lead(lead_id)
            return False, str(e)
        app.db.update_lead(lead_id, status="sent", sent_at=time.time(), method=method, error=None)
        if lead["author_id"]:
            app.db.mark_contacted(platform, lead["author_id"])
        if manual and self.app.cfg.ai.use_feedback:
            app.db.add_feedback(lead["text"], 1)
        log.info("Лид #%d: отправлено (%s)", lead_id, method)
        await app.bot.refresh_lead(lead_id)
        await app.bot.notify_sent(lead_id)
        return True, method
