from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import snowballstemmer
import yaml

log = logging.getLogger("keywords")

_stemmer = snowballstemmer.stemmer("russian")
_TOKEN = re.compile(r"[a-zа-я0-9₽+]+(?:-[a-zа-я0-9]+)*")


def normalize(text: str) -> str:
    return text.lower().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    for t in _TOKEN.findall(normalize(text)):
        tokens.append(t)
        if "-" in t:
            tokens.extend(t.split("-"))
    return tokens


def stem(token: str) -> str:
    return _stemmer.stemWord(token)


Pred = Callable[[str, str], bool]


@dataclass
class _Rule:
    category: str
    word: str
    preds: list[Pred] = field(default_factory=list)
    regex: re.Pattern | None = None

    def match(self, text_norm: str, tokens: list[str], stems: list[str]) -> bool:
        if self.regex is not None:
            return self.regex.search(text_norm) is not None
        n = len(tokens)
        first = self.preds[0]
        for i in range(n):
            if not first(tokens[i], stems[i]):
                continue
            pos = i
            ok = True
            for pred in self.preds[1:]:
                for j in (pos + 1, pos + 2):
                    if j < n and pred(tokens[j], stems[j]):
                        pos = j
                        break
                else:
                    ok = False
                    break
            if ok:
                return True
        return False


def _compile(category: str, word: str) -> _Rule | None:
    word = str(word).strip()
    if not word:
        return None
    if word.startswith("re:"):
        try:
            return _Rule(category, word, regex=re.compile(word[3:], re.I))
        except re.error as e:
            log.warning("Неверная регулярка %r: %s", word, e)
            return None
    exact = word.startswith("=")
    body = word[1:] if exact else word
    preds: list[Pred] = []
    for part in normalize(body).split():
        prefix = part.endswith("*")
        part = part.rstrip("*")
        toks = tokenize(part)
        if not toks:
            continue
        tok = toks[0] if len(toks) == 1 else part
        if prefix:
            preds.append(lambda t, s, p=tok: t.startswith(p))
        elif exact:
            preds.append(lambda t, s, p=tok: t == p)
        else:
            st = stem(tok)
            preds.append(lambda t, s, p=st, raw=tok: s == p or t == raw)
    if not preds:
        return None
    return _Rule(category, word, preds=preds)


@dataclass
class Score:
    score: int = 0
    matched: dict[str, list[str]] = field(default_factory=dict)
    blacklisted: str = ""

    def words(self) -> list[str]:
        out: list[str] = []
        stems: set[str] = set()
        for cat, words in self.matched.items():
            if cat == "offer":
                continue
            for w in words:
                if w.startswith("re:"):
                    continue
                w = w.lstrip("=").rstrip("*")
                key = " ".join(stem(t)[:5] for t in tokenize(w))
                if key not in stems:
                    stems.add(key)
                    out.append(w)
        return out


class KeywordFilter:
    def __init__(self, path: Path):
        self.path = path
        self.mtime = 0.0
        self.rules: list[_Rule] = []
        self.blacklist: list[_Rule] = []
        self.weights: dict[str, int] = {}
        self.titles: dict[str, str] = {}
        self.min_score = 4
        self.deep_scan_min_score = 2
        self.sure_score = 7
        self.maybe_reload(force=True)

    def maybe_reload(self, force: bool = False) -> None:
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            if force:
                log.error("Не найден файл ключевых слов %s", self.path)
            return
        if not force and mtime == self.mtime:
            return
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8-sig")) or {}
        except Exception as e:
            log.error("Ошибка в %s: %s - оставляю прежние ключевые слова", self.path.name, e)
            self.mtime = mtime
            return
        rules, weights, titles = [], {}, {}
        for cat, spec in (data.get("categories") or {}).items():
            weights[cat] = int(spec.get("weight", 1))
            titles[cat] = spec.get("title", cat)
            for w in spec.get("words") or []:
                r = _compile(cat, w)
                if r:
                    rules.append(r)
        self.rules = rules
        self.weights = weights
        self.titles = titles
        self.blacklist = [r for r in (_compile("blacklist", w) for w in data.get("blacklist") or []) if r]
        self.min_score = int(data.get("min_score", 4))
        self.deep_scan_min_score = int(data.get("deep_scan_min_score", 2))
        self.sure_score = int(data.get("sure_score", 7))
        self.mtime = mtime
        log.info("Ключевые слова загружены: %d правил, %d категорий", len(rules), len(weights))

    def score(self, text: str) -> Score:
        text_norm = normalize(text)
        tokens = tokenize(text)
        stems = [stem(t) for t in tokens]
        res = Score()
        for r in self.blacklist:
            if r.match(text_norm, tokens, stems):
                res.blacklisted = r.word
                return res
        for r in self.rules:
            if r.match(text_norm, tokens, stems):
                res.matched.setdefault(r.category, []).append(r.word)
        res.score = sum(self.weights.get(cat, 0) for cat in res.matched)
        return res
