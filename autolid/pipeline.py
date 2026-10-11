from __future__ import annotations

import asyncio
import hashlib
import html
import logging
import re
import time
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable

from .ai import AIError
from .keywords import Score, normalize
from .models import Message, Verdict

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("pipeline")


@dataclass
class Item:
    msg: Message
    score: Score
    retries: int = 0


class Batcher:
    def __init__(self, name: str, size: Callable[[], int], wait: Callable[[], float],
                 process: Callable[[list[Item]], Awaitable[None]]):
        self.name = name
        self.size = size
        self.wait = wait
        self.process = process
        self.queue: asyncio.Queue[Item] = asyncio.Queue()
        self._tasks: set[asyncio.Task] = set()

    def put(self, item: Item) -> None:
        self.queue.put_nowait(item)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            batch = [await self.queue.get()]
            deadline = loop.time() + self.wait()
            while len(batch) < self.size():
                timeout = deadline - loop.time()
                if timeout <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(self.queue.get(), timeout))
                except asyncio.TimeoutError:
                    break
            task = asyncio.create_task(self._safe(batch))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    async def _safe(self, batch: list[Item]) -> None:
        try:
            await self.process(batch)
        except Exception:
            log.exception("Ошибка обработки пачки %s", self.name)


def text_fingerprint(text: str) -> str:
    t = re.sub(r"[^a-zа-я0-9]+", " ", normalize(text)).strip()
    return hashlib.sha1(t.encode()).hexdigest()


class Pipeline:
    def __init__(self, app: "App"):
        self.app = app
        cfg = app.cfg
        self.fast = Batcher("fast", lambda: int(cfg.ai.batch_size), lambda: float(cfg.ai.batch_wait), self._process)
        self.deep = Batcher("deep", lambda: int(cfg.ai.deep_scan_batch_size), lambda: float(cfg.ai.deep_scan_wait),
                            self._process)
        self.deep_times: deque[float] = deque()
        self.counters = {"messages": 0, "candidates": 0, "deep": 0, "leads": 0}

    @property
    def paused(self) -> bool:
        return bool(self.app.db.kv_get("paused", False))

    def prefilter(self, platform: str, chat_id: int, msg_id: str, text: str, date: float) -> Score | None:
        if self.paused or not text:
            return None
        fcfg = self.app.cfg.filter
        self.counters["messages"] += 1
        text_len = len(text.strip())
        if text_len < int(fcfg.min_length) or text_len > int(fcfg.max_length):
            return None
        if time.time() - date > float(self.app.cfg.reply.max_message_age_minutes) * 60:
            return None
        if not self.app.db.mark_seen(platform, chat_id, msg_id):
            return None
        kw = self.app.keywords
        score = kw.score(text)
        if score.blacklisted:
            return None
        threshold = kw.min_score
        if self.app.ai.enabled and self.app.cfg.ai.deep_scan:
            threshold = min(threshold, kw.deep_scan_min_score)
        if score.score < threshold:
            return None
        return score

    async def submit(self, msg: Message, score: Score) -> None:
        app = self.app
        if msg.author_id is not None and app.db.is_ignored(msg.platform, msg.author_id):
            return
        if not app.db.fingerprint_new(text_fingerprint(msg.text), float(app.cfg.filter.duplicate_window_hours)):
            log.debug("Дубликат текста, пропуск: %s", msg.text[:60])
            return
        kw = app.keywords
        item = Item(msg, score)
        if not app.ai.enabled:
            if score.score >= kw.min_score:
                await self._lead_from_keywords(item, "ИИ выключен")
            return
        if score.score >= kw.min_score:
            self.counters["candidates"] += 1
            if app.ai.available:
                self.fast.put(item)
            elif score.score >= kw.sure_score:
                await self._lead_from_keywords(item, "ИИ недоступен")
            else:
                self.deep.put(item)
        elif self._deep_allowed():
            self.counters["deep"] += 1
            self.deep.put(item)

    def _deep_allowed(self) -> bool:
        now = time.time()
        while self.deep_times and now - self.deep_times[0] > 3600:
            self.deep_times.popleft()
        if len(self.deep_times) >= int(self.app.cfg.ai.deep_scan_max_per_hour):
            return False
        self.deep_times.append(now)
        return True

    async def _process(self, batch: list[Item]) -> None:
        app = self.app
        max_age = float(app.cfg.reply.max_message_age_minutes) * 60
        batch = [it for it in batch if time.time() - it.msg.date <= max_age * 2]
        if not batch:
            return
        try:
            verdicts = await app.ai.classify([it.msg for it in batch])
        except AIError as e:
            log.warning("ИИ не ответил на пачку из %d: %s", len(batch), e)
            for it in batch:
                if it.score.score >= app.keywords.sure_score:
                    await self._lead_from_keywords(it, "ИИ недоступен")
                elif it.retries < 2:
                    it.retries += 1
                    self.deep.put(it)
            return
        min_conf = int(app.cfg.ai.min_confidence)
        for it, v in zip(batch, verdicts):
            if v is None:
                if it.retries < 1:
                    it.retries += 1
                    self.fast.put(it)
                continue
            log.info("%s %3d%% [%d] %s", "ЛИД " if v.is_lead else "нет ", v.confidence, it.score.score,
                     it.msg.text[:90].replace("\n", " "))
            if v.is_lead and v.confidence >= min_conf:
                await self.create_lead(it, v)

    async def _lead_from_keywords(self, item: Item, why: str) -> None:
        v = Verdict(is_lead=True, confidence=0, source="keywords", summary=why,
                    reply=self.app.ai.template_reply(item.msg.author_name if item.msg.author_id else ""))
        await self.create_lead(item, v)

    async def create_lead(self, item: Item, v: Verdict) -> int:
        app = self.app
        m = item.msg
        if not v.reply:
            try:
                v.reply = await app.ai.write_reply(m.text, v.as_dict()) if app.ai.available else ""
            except AIError:
                v.reply = ""
            v.reply = v.reply or app.ai.template_reply(m.author_name if m.author_id else "")
        lead_id = app.db.add_lead(
            platform=m.platform, chat_id=m.chat_id, chat_title=m.chat_title, chat_link=m.chat_link,
            msg_id=m.msg_id, msg_link=m.msg_link, author_id=m.author_id, author_name=m.author_name,
            author_username=m.author_username, author_link=m.author_link, text=m.text,
            score=item.score.score, matched=item.score.words(), ai=v.as_dict(), reply=v.reply,
            status="new", extra={"ctx": m.reply_ctx, "msg_date": m.date},
        )
        self.counters["leads"] += 1
        log.info("Новый лид #%d (%s, %s): %s", lead_id, m.platform, m.chat_title, m.text[:80].replace("\n", " "))
        reason = app.responder.auto_block_reason(lead_id)
        if reason is None:
            app.responder.schedule(lead_id)
        else:
            app.db.update_lead(lead_id, extra={**app.db.lead(lead_id)["extra"], "auto_note": reason})
        await app.bot.send_lead(lead_id)
        return lead_id

    async def check_text(self, text: str) -> str:
        score = self.app.keywords.score(text)
        lines = [f"Баллы по ключевым словам: <b>{score.score}</b> "
                 f"(на ИИ от {self.app.keywords.min_score}, глубокая проверка от {self.app.keywords.deep_scan_min_score})"]
        esc = html.escape
        if score.blacklisted:
            lines.append(f"⛔ Стоп-слово: {esc(score.blacklisted)}")
        for cat, words in score.matched.items():
            title = esc(self.app.keywords.titles.get(cat, cat))
            lines.append(f"• {title} ({self.app.keywords.weights.get(cat, 0):+d}): {esc(', '.join(words))}")
        if self.app.ai.enabled:
            m = Message(platform="test", chat_id=0, msg_id="0", text=text, date=time.time(), chat_title="тест")
            try:
                v = (await self.app.ai.classify([m]))[0]
                if v is None:
                    lines.append("ИИ: пустой ответ")
                else:
                    lines.append(f"ИИ: {'✅ ЛИД' if v.is_lead else '❌ не лид'}, уверенность {v.confidence}%")
                    details = " · ".join(x for x in (v.work_type, v.subject, v.university, v.deadline, v.budget) if x)
                    if details:
                        lines.append(f"📌 {esc(details)}")
                    if v.reply:
                        lines.append(f"✉️ {esc(v.reply)}")
            except AIError as e:
                lines.append(f"ИИ недоступен: {esc(str(e))}")
        return "\n".join(lines)
