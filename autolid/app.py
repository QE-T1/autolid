from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .ai import AI
from .bot import Bot, BotError, esc
from .config import Section, path_of
from .db import DB
from .keywords import KeywordFilter
from .links import parse_tg, parse_vk, scan_folder
from .pipeline import Pipeline
from .responder import Responder
from .tg import TelegramWorker
from .vk import VKError, VKWorker

log = logging.getLogger("app")


class App:
    def __init__(self, cfg: Section):
        self.cfg = cfg
        self.started_at = time.time()
        self.data_dir = path_of(cfg, "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db = DB(self.data_dir / "autolid.db")
        self.keywords = KeywordFilter(path_of(cfg, "keywords"))
        self.ai = AI(cfg, path_of(cfg, "prompts"), self.db)
        self.bot = Bot(self)
        self.pipeline = Pipeline(self)
        self.responder = Responder(self)
        self.tg: TelegramWorker | None = None
        self.vk: VKWorker | None = None
        self._bad_links_reported: set[str] = set()

    def wake_workers(self) -> None:
        if self.tg:
            self.tg.wake.set()
        if self.vk:
            self.vk.wake.set()

    async def run(self) -> None:
        try:
            await self.bot.start()
        except BotError as e:
            raise SystemExit(f"Бот не запустился: {e}. Проверьте bot.token в config.yaml и интернет.") from e

        if self.cfg.telegram.api_id and self.cfg.telegram.api_hash:
            tg = TelegramWorker(self)
            try:
                await tg.start()
                self.tg = tg
            except Exception as e:
                log.error("Telegram не запущен: %s", e)
                await self.bot.notify_admins(f"⚠️ Telegram-аккаунт не подключён: {esc(e)}\nЗапустите setup.")
        else:
            log.warning("Telegram не настроен (нет api_id/api_hash) - запустите setup")

        if str(self.cfg.vk.token).strip():
            vk = VKWorker(self)
            try:
                await vk.start()
                self.vk = vk
            except VKError as e:
                log.error("VK не запущен: %s", e)
                await self.bot.notify_admins(f"⚠️ VK не подключён: {esc(e)}")
        else:
            log.info("VK выключен (нет токена)")

        if self.ai.enabled:
            try:
                await self.ai.ping()
                log.info("ИИ отвечает (%s)", self.cfg.ai.model)
            except Exception as e:
                log.warning("ИИ сейчас не отвечает: %s - буду работать по ключевым словам и пробовать снова", e)

        await self.sync_chat_files(initial=True)

        tasks: list[tuple[str, Callable[[], Awaitable[None]]]] = [
            ("бот", self.bot.poll_loop),
            ("ИИ-пачки", self.pipeline.fast.run),
            ("ИИ-глубокая", self.pipeline.deep.run),
            ("папки с чатами", self._files_loop),
            ("обслуживание", self._maintenance_loop),
        ]
        if self.tg:
            tasks += [("telegram", self.tg.run), ("вступление TG", self.tg.join_loop),
                      ("история TG", self.tg.history_loop)]
        if self.vk:
            tasks += [("VK стены", self.vk.poll_loop), ("VK группы", self.vk.resolve_loop),
                      ("VK беседы", self.vk.longpoll_loop)]

        await self.bot.notify_admins("🚀 <b>AutoLid запущен</b>\n\n" + self.bot.status_text())
        log.info("Запущено. Остановить - Ctrl+C")
        try:
            await asyncio.gather(*(self._supervise(name, fn) for name, fn in tasks))
        finally:
            await self.shutdown()

    async def _supervise(self, name: str, fn: Callable[[], Awaitable[None]]) -> None:
        delay = 5
        while True:
            try:
                await fn()
                if name == "telegram":
                    log.warning("Telegram отключился, переподключаюсь...")
                    await self.tg.client.connect()
                    continue
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Сбой в «%s», перезапуск через %d с", name, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)

    async def shutdown(self) -> None:
        for closer in (self.tg.stop if self.tg else None, self.vk.stop if self.vk else None, self.ai.close,
                       self.bot.close):
            if closer:
                try:
                    await closer()
                except Exception:
                    pass

    async def sync_chat_files(self, initial: bool = False) -> None:
        added_total = {}
        for platform, key, parser in (("tg", "telegram_chats", parse_tg), ("vk", "vk_chats", parse_vk)):
            items, bad = scan_folder(path_of(self.cfg, key), parser)
            added = self.db.sync_chat_links(platform, items)
            added_total[platform] = added
            new_bad = [b for b in bad if b not in self._bad_links_reported]
            if new_bad:
                self._bad_links_reported.update(new_bad)
                log.warning("Не распознаны ссылки (%s): %s", platform, "; ".join(new_bad[:20]))
                if not initial:
                    await self.bot.notify_admins("⚠️ Не понял эти строки:\n" + "\n".join(esc(b) for b in new_bad[:20]))
        if self.tg:
            self.tg.reload_active()
        if self.vk:
            self.vk.reload_active()
        if any(added_total.values()):
            self.wake_workers()
            msg = (f"📥 Новые ссылки: Telegram +{added_total['tg']}, VK +{added_total['vk']}. Начинаю подключать.")
            log.info(msg)
            if not initial:
                await self.bot.notify_admins(msg)

    async def _files_loop(self) -> None:
        while True:
            await asyncio.sleep(20)
            await self.sync_chat_files()

    async def _maintenance_loop(self) -> None:
        n = 0
        while True:
            await asyncio.sleep(5)
            self.keywords.maybe_reload()
            n += 1
            if n % 720 == 0:
                self.db.cleanup()
