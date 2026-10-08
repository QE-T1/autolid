from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS chats (
    platform TEXT NOT NULL,
    key TEXT NOT NULL,
    link TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    peer_id INTEGER,
    title TEXT,
    username TEXT,
    extra TEXT,
    error TEXT,
    in_files INTEGER NOT NULL DEFAULT 1,
    source TEXT,
    added_at REAL,
    updated_at REAL,
    PRIMARY KEY (platform, key)
);
CREATE INDEX IF NOT EXISTS chats_peer ON chats(platform, peer_id);

CREATE TABLE IF NOT EXISTS seen (
    platform TEXT, chat_id INTEGER, msg_id TEXT, ts REAL,
    PRIMARY KEY (platform, chat_id, msg_id)
);
CREATE TABLE IF NOT EXISTS fingerprints (hash TEXT PRIMARY KEY, ts REAL);

CREATE TABLE IF NOT EXISTS leads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT, chat_id INTEGER, chat_title TEXT, chat_link TEXT,
    msg_id TEXT, msg_link TEXT,
    author_id INTEGER, author_name TEXT, author_username TEXT, author_link TEXT,
    text TEXT, score INTEGER, matched TEXT, ai TEXT, reply TEXT,
    status TEXT DEFAULT 'new', method TEXT, error TEXT,
    created_at REAL, sent_at REAL,
    bot_chat_id INTEGER, bot_msg_id INTEGER,
    extra TEXT
);
CREATE INDEX IF NOT EXISTS leads_sent ON leads(status, sent_at);

CREATE TABLE IF NOT EXISTS contacted (platform TEXT, author_id INTEGER, ts REAL, PRIMARY KEY (platform, author_id));
CREATE TABLE IF NOT EXISTS ignored_authors (platform TEXT, author_id INTEGER, PRIMARY KEY (platform, author_id));

CREATE TABLE IF NOT EXISTS vk_walls (owner_id INTEGER PRIMARY KEY, last_post_id INTEGER DEFAULT 0, initialized INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS vk_posts (
    owner_id INTEGER, post_id INTEGER, date INTEGER, comments_count INTEGER DEFAULT 0, last_comment_id INTEGER DEFAULT 0,
    PRIMARY KEY (owner_id, post_id)
);
CREATE TABLE IF NOT EXISTS vk_topics (group_id INTEGER, topic_id INTEGER, last_comment_id INTEGER DEFAULT 0, initialized INTEGER DEFAULT 0,
    PRIMARY KEY (group_id, topic_id));

CREATE TABLE IF NOT EXISTS feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, label INTEGER, ts REAL);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)

    def q(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, tuple(params)).fetchone()

    def x(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, tuple(params))

    def kv_get(self, key: str, default: Any = None) -> Any:
        row = self.one("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(row["value"]) if row else default

    def kv_set(self, key: str, value: Any) -> None:
        self.x("INSERT OR REPLACE INTO kv(key, value) VALUES(?, ?)", (key, json.dumps(value, ensure_ascii=False)))

    def sync_chat_links(self, platform: str, items: dict[str, tuple[str, str, str]]) -> int:
        now = time.time()
        existing = {r["key"]: r for r in self.q("SELECT key, in_files FROM chats WHERE platform=?", (platform,))}
        added = 0
        for key, (link, kind, source) in items.items():
            if key not in existing:
                self.x(
                    "INSERT INTO chats(platform, key, link, kind, status, in_files, source, added_at, updated_at) "
                    "VALUES(?,?,?,?, 'pending', 1, ?, ?, ?)",
                    (platform, key, link, kind, source, now, now),
                )
                added += 1
            elif not existing[key]["in_files"]:
                self.x("UPDATE chats SET in_files=1, source=?, updated_at=? WHERE platform=? AND key=?",
                       (source, now, platform, key))
        gone = set(existing) - set(items)
        for key in gone:
            if existing[key]["in_files"]:
                self.x("UPDATE chats SET in_files=0, updated_at=? WHERE platform=? AND key=?", (now, platform, key))
        return added

    def update_chat(self, platform: str, key: str, **fields: Any) -> None:
        if "extra" in fields and not isinstance(fields["extra"], (str, type(None))):
            fields["extra"] = json.dumps(fields["extra"], ensure_ascii=False)
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE chats SET {cols} WHERE platform=? AND key=?", (*fields.values(), platform, key))

    def chats(self, platform: str, status: str | None = None, in_files: bool = True) -> list[sqlite3.Row]:
        sql = "SELECT * FROM chats WHERE platform=?"
        params: list[Any] = [platform]
        if in_files:
            sql += " AND in_files=1"
        if status:
            sql += " AND status=?"
            params.append(status)
        return self.q(sql + " ORDER BY added_at", params)

    def chat_stats(self, platform: str) -> dict[str, int]:
        rows = self.q("SELECT status, COUNT(*) c FROM chats WHERE platform=? AND in_files=1 GROUP BY status", (platform,))
        return {r["status"]: r["c"] for r in rows}

    def mark_seen(self, platform: str, chat_id: int, msg_id: str) -> bool:
        cur = self.x("INSERT OR IGNORE INTO seen(platform, chat_id, msg_id, ts) VALUES(?,?,?,?)",
                     (platform, chat_id, str(msg_id), time.time()))
        return cur.rowcount > 0

    def fingerprint_new(self, h: str, window_hours: float) -> bool:
        row = self.one("SELECT ts FROM fingerprints WHERE hash=?", (h,))
        now = time.time()
        if row and now - row["ts"] < window_hours * 3600:
            return False
        self.x("INSERT OR REPLACE INTO fingerprints(hash, ts) VALUES(?, ?)", (h, now))
        return True

    def cleanup(self) -> None:
        old = time.time() - 4 * 86400
        self.x("DELETE FROM seen WHERE ts < ?", (old,))
        self.x("DELETE FROM fingerprints WHERE ts < ?", (old,))
        self.x("DELETE FROM vk_posts WHERE date < ?", (int(time.time()) - 7 * 86400,))

    def add_lead(self, **fields: Any) -> int:
        for k in ("matched", "ai", "extra"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        fields.setdefault("created_at", time.time())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        return self.x(f"INSERT INTO leads({cols}) VALUES({marks})", tuple(fields.values())).lastrowid

    def update_lead(self, lead_id: int, **fields: Any) -> None:
        for k in ("matched", "ai", "extra"):
            if k in fields and not isinstance(fields[k], (str, type(None))):
                fields[k] = json.dumps(fields[k], ensure_ascii=False)
        cols = ", ".join(f"{k}=?" for k in fields)
        self.x(f"UPDATE leads SET {cols} WHERE id=?", (*fields.values(), lead_id))

    def lead(self, lead_id: int) -> dict | None:
        row = self.one("SELECT * FROM leads WHERE id=?", (lead_id,))
        if not row:
            return None
        d = dict(row)
        for k in ("matched", "ai", "extra"):
            d[k] = json.loads(d[k]) if d.get(k) else ({} if k != "matched" else [])
        return d

    def sent_count(self, since_seconds: float) -> int:
        row = self.one("SELECT COUNT(*) c FROM leads WHERE status='sent' AND sent_at > ?", (time.time() - since_seconds,))
        return row["c"]

    def contacted_recently(self, platform: str, author_id: int, hours: float) -> bool:
        row = self.one("SELECT ts FROM contacted WHERE platform=? AND author_id=?", (platform, author_id))
        return bool(row and time.time() - row["ts"] < hours * 3600)

    def mark_contacted(self, platform: str, author_id: int) -> None:
        self.x("INSERT OR REPLACE INTO contacted(platform, author_id, ts) VALUES(?,?,?)", (platform, author_id, time.time()))

    def is_ignored(self, platform: str, author_id: int | None) -> bool:
        if author_id is None:
            return False
        return self.one("SELECT 1 FROM ignored_authors WHERE platform=? AND author_id=?", (platform, author_id)) is not None

    def ignore_author(self, platform: str, author_id: int) -> None:
        self.x("INSERT OR IGNORE INTO ignored_authors(platform, author_id) VALUES(?,?)", (platform, author_id))

    def add_feedback(self, text: str, label: int) -> None:
        self.x("INSERT INTO feedback(text, label, ts) VALUES(?,?,?)", (text[:600], label, time.time()))

    def feedback_examples(self, label: int, limit: int) -> list[str]:
        rows = self.q("SELECT text FROM feedback WHERE label=? ORDER BY id DESC LIMIT ?", (label, limit))
        return [r["text"] for r in rows]
