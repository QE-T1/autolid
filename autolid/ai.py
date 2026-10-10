from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from pathlib import Path
from typing import Any

import anthropic
import httpx

from .db import DB
from .models import Message, Verdict

log = logging.getLogger("ai")


class AIError(Exception):
    pass


class _Fail(Exception):
    def __init__(self, message: str, fatal: bool = False):
        super().__init__(message)
        self.fatal = fatal


def _extract_json(text: str) -> Any:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    try:
        return json.loads(text)
    except ValueError:
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start, end = text.find(open_ch), text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise AIError(f"ИИ вернул не JSON: {text[:200]!r}")


def _clean_reply(text: str) -> str:
    text = (text or "").strip().strip('"«»').strip()
    text = re.sub(r"^(ответ|сообщение)\s*:\s*", "", text, flags=re.I)
    return text[:700]


def _int(v: Any, default: int = 0) -> int:
    try:
        return max(0, min(100, int(float(v))))
    except (TypeError, ValueError):
        return default


class AI:
    def __init__(self, cfg, prompts_dir: Path, db: DB):
        self.cfg = cfg.ai
        self.prompts_dir = prompts_dir
        self.db = db
        self.enabled = bool(self.cfg.enabled and self.cfg.api_key and self.cfg.base_url)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(float(self.cfg.timeout), connect=20.0))
        fmt = str(self.cfg.get("api_format") or "auto").lower()
        if fmt == "auto":
            fmt = "anthropic" if str(self.cfg.model).lower().startswith("claude") else "openai"
        self.api_format = fmt
        self.anthropic: anthropic.AsyncAnthropic | None = None
        self.no_effort = False
        if self.enabled and fmt == "anthropic":
            root = re.sub(r"/v1/?$", "", str(self.cfg.base_url).rstrip("/"))
            self.anthropic = anthropic.AsyncAnthropic(api_key=str(self.cfg.api_key), base_url=root,
                                                      max_retries=0, timeout=float(self.cfg.timeout))
        self.sem = asyncio.Semaphore(max(1, int(self.cfg.max_concurrency)))
        self.fail_streak = 0
        self.down_until = 0.0
        self.last_error = ""
        self.stats = {"requests": 0, "errors": 0, "checked": 0}

    @property
    def available(self) -> bool:
        return self.enabled and time.time() >= self.down_until

    def status(self) -> str:
        if not self.enabled:
            return "выключен"
        if not self.available:
            return f"недоступен ({self.last_error[:80]}), пробую снова через {int(self.down_until - time.time())} с"
        if self.fail_streak:
            return f"последний запрос не удался ({self.last_error[:80]})"
        return "работает"

    def prompt(self, name: str) -> str:
        try:
            return (self.prompts_dir / name).read_text(encoding="utf-8-sig")
        except OSError:
            return ""

    async def close(self) -> None:
        await self.http.aclose()
        if self.anthropic is not None:
            await self.anthropic.close()

    async def chat(self, messages: list[dict], temperature: float = 0.3, attempts: int = 3) -> str:
        if not self.enabled:
            raise AIError("ИИ выключен")
        last = ""
        for attempt in range(attempts):
            fatal = False
            async with self.sem:
                self.stats["requests"] += 1
                try:
                    if self.anthropic is not None:
                        content = await self._call_anthropic(messages)
                    else:
                        content = await self._call_openai(messages, temperature)
                    if not content.strip():
                        raise _Fail("пустой ответ")
                    self.fail_streak = 0
                    self.down_until = 0
                    return content
                except _Fail as e:
                    last, fatal = str(e), e.fatal
            self.stats["errors"] += 1
            log.warning("Ошибка ИИ (попытка %d): %s", attempt + 1, last)
            if fatal or attempt >= attempts - 1:
                break
            await asyncio.sleep(2 * (attempt + 1) + random.random())
        self.fail_streak += 1
        self.last_error = last
        if self.fail_streak >= 2:
            self.down_until = time.time() + min(300, 30 * self.fail_streak)
        raise AIError(last)

    async def _call_openai(self, messages: list[dict], temperature: float) -> str:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = {"model": self.cfg.model, "messages": messages, "temperature": temperature}
        try:
            r = await self.http.post(url, json=payload, headers={"Authorization": f"Bearer {self.cfg.api_key}"})
        except httpx.HTTPError as e:
            raise _Fail(f"{type(e).__name__}: {e}") from e
        if r.status_code != 200:
            raise _Fail(f"HTTP {r.status_code}: {r.text[:200]}", fatal=r.status_code in (400, 401, 403, 404))
        try:
            return r.json()["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, ValueError) as e:
            raise _Fail(f"странный ответ: {r.text[:200]}") from e

    async def _call_anthropic(self, messages: list[dict]) -> str:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        params: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": int(self.cfg.get("max_tokens") or 8000),
            "messages": [m for m in messages if m["role"] != "system"],
        }
        if system:
            params["system"] = system
        if self.cfg.get("effort") and not self.no_effort:
            params["output_config"] = {"effort": str(self.cfg.effort)}
        try:
            try:
                resp = await self.anthropic.messages.create(**params)
            except anthropic.BadRequestError as e:
                if "output_config" not in params or not re.search(r"effort|output_config", str(e), re.I):
                    raise
                self.no_effort = True
                params.pop("output_config")
                resp = await self.anthropic.messages.create(**params)
        except anthropic.APIStatusError as e:
            raise _Fail(f"HTTP {e.status_code}: {str(e)[:200]}", fatal=e.status_code in (400, 401, 403, 404)) from e
        except anthropic.APIConnectionError as e:
            raise _Fail(f"{type(e).__name__}: {e}") from e
        if resp.stop_reason == "refusal":
            raise _Fail("модель отказалась отвечать (refusal)", fatal=True)
        if resp.stop_reason == "max_tokens":
            log.warning("ИИ упёрся в max_tokens - увеличьте ai.max_tokens или уменьшите размер пачек")
        return "".join(b.text for b in resp.content if b.type == "text")

    def _system_prompt(self) -> str:
        base = self.prompt("classify.txt")
        rules = self.prompt("reply.txt").strip()
        examples = ""
        if self.cfg.use_feedback:
            neg = self.db.feedback_examples(0, 6)
            pos = self.db.feedback_examples(1, 4)
            if neg:
                examples += "\nОператор пометил эти сообщения как НЕ лиды - похожие тоже не лиды:\n"
                examples += "\n".join(f"- {t[:300]}" for t in neg) + "\n"
            if pos:
                examples += "\nПримеры настоящих лидов, которым оператор ответил:\n"
                examples += "\n".join(f"- {t[:300]}" for t in pos) + "\n"
        if "{reply_rules}" in base:
            base = base.replace("{reply_rules}", rules)
        else:
            base += "\n\nПРАВИЛА ОТВЕТА:\n" + rules
        if "{examples}" in base:
            base = base.replace("{examples}", examples)
        else:
            base += examples
        return base

    async def classify(self, msgs: list[Message]) -> list[Verdict | None]:
        items = [{"id": i + 1, "chat": m.chat_title[:80], "text": m.text[:1500]} for i, m in enumerate(msgs)]
        user = "Сообщения:\n" + json.dumps(items, ensure_ascii=False, indent=0)
        content = await self.chat(
            [{"role": "system", "content": self._system_prompt()}, {"role": "user", "content": user}],
            temperature=0.3,
        )
        data = _extract_json(content)
        results = data.get("results", data) if isinstance(data, dict) else data
        if isinstance(results, dict):
            results = [results]
        out: list[Verdict | None] = [None] * len(msgs)
        for r in results or []:
            if not isinstance(r, dict):
                continue
            idx = _int(r.get("id"), 0) - 1
            if not 0 <= idx < len(msgs):
                continue
            lead = r.get("lead")
            if isinstance(lead, str):
                lead = lead.strip().lower() in ("true", "1", "да", "yes")
            out[idx] = Verdict(
                is_lead=bool(lead),
                confidence=_int(r.get("confidence"), 50),
                work_type=str(r.get("work_type") or "")[:60],
                subject=str(r.get("subject") or "")[:80],
                university=str(r.get("university") or "")[:60],
                deadline=str(r.get("deadline") or "")[:60],
                budget=str(r.get("budget") or "")[:60],
                summary=str(r.get("summary") or "")[:150],
                reply=_clean_reply(str(r.get("reply") or "")),
            )
        self.stats["checked"] += len(msgs)
        return out

    async def write_reply(self, text: str, verdict: dict | None = None, previous: str = "") -> str:
        rules = self.prompt("reply.txt").strip()
        system = (
            "Ты пишешь ответ человеку, который в студенческом чате ищет исполнителя учебной работы.\n"
            f"Правила:\n{rules}\n\nВерни только текст сообщения, без кавычек и пояснений."
        )
        user = f"Сообщение человека:\n{text[:1500]}"
        if verdict:
            info = ", ".join(f"{k}: {v}" for k, v in verdict.items()
                             if k in ("work_type", "subject", "university", "deadline", "budget") and v)
            if info:
                user += f"\n\nЧто удалось понять: {info}"
        if previous:
            user += f"\n\nПредыдущий вариант не подошёл, напиши заметно по-другому:\n{previous}"
        content = await self.chat([{"role": "system", "content": system}, {"role": "user", "content": user}],
                                  temperature=0.9)
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.S)
        return _clean_reply(content)

    def template_reply(self, author_name: str = "") -> str:
        lines = [ln.strip() for ln in self.prompt("templates.txt").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
        if not lines:
            lines = ["Могу помочь, напиши в лс 🙂"]
        tpl = random.choice(lines)
        name = (author_name or "").split()[0] if author_name else ""
        if name and re.match(r"^[A-Za-zА-Яа-яЁё\-]{2,20}$", name):
            text = tpl.replace("{name}", name)
        else:
            text = re.sub(r"^\s*\{name\}\s*,?\s*", "", tpl)
            text = re.sub(r",?\s*\{name\}", "", text)
        return text[:1].upper() + text[1:]

    async def ping(self) -> str:
        content = await self.chat([{"role": "user", "content": "Ответь одним словом: работает"}], attempts=1)
        return content.strip()[:100]
