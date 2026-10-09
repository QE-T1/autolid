from __future__ import annotations

import re
from pathlib import Path
from typing import NamedTuple


class ChatLink(NamedTuple):
    key: str
    link: str
    kind: str


TG_RESERVED = {"joinchat", "addstickers", "addemoji", "share", "proxy", "socks", "s", "c", "iv", "login",
               "setlanguage", "addlist", "boost", "contact", "invoice", "m", "bg", "addtheme", "confirmphone"}
TG_USERNAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


def parse_tg(raw: str) -> ChatLink | None:
    s = raw.strip().strip("<>\"'").rstrip("/.,;")
    if not s:
        return None
    m = re.match(r"^tg://resolve\?domain=([A-Za-z0-9_]+)", s)
    if m:
        s = "@" + m.group(1)
    m = re.match(r"^tg://join\?invite=([A-Za-z0-9_\-]+)", s)
    if m:
        h = m.group(1)
        return ChatLink("+" + h, f"https://t.me/+{h}", "invite")
    if s.startswith("@"):
        name = s[1:]
        if TG_USERNAME.match(name):
            return ChatLink("@" + name.lower(), f"https://t.me/{name}", "username")
        return None
    m = re.match(r"^(?:https?://)?([A-Za-z0-9_]{4,32})\.t\.me/?$", s, re.I)
    if m:
        name = m.group(1)
        return ChatLink("@" + name.lower(), f"https://t.me/{name}", "username")
    m = re.match(r"^(?:https?://)?(?:www\.)?(?:t|telegram)\.(?:me|dog)/(.+)$", s, re.I)
    if not m:
        if TG_USERNAME.match(s):
            return ChatLink("@" + s.lower(), f"https://t.me/{s}", "username")
        return None
    path = m.group(1).split("?")[0].split("#")[0].strip("/")
    parts = path.split("/")
    first = parts[0]
    if first.startswith("+") and len(first) > 1:
        h = first[1:]
        return ChatLink("+" + h, f"https://t.me/+{h}", "invite")
    if first.lower() == "joinchat" and len(parts) > 1:
        h = parts[1]
        return ChatLink("+" + h, f"https://t.me/+{h}", "invite")
    if first.lower() == "c" and len(parts) > 1 and parts[1].isdigit():
        return ChatLink("c:" + parts[1], f"https://t.me/c/{parts[1]}", "private")
    if first.lower() == "s" and len(parts) > 1:
        first = parts[1]
    if first.lower() in TG_RESERVED or not TG_USERNAME.match(first):
        return None
    return ChatLink("@" + first.lower(), f"https://t.me/{first}", "username")


VK_DOMAIN = r"(?:https?://)?(?:www\.|m\.)?(?:vk\.com|vk\.ru|vkontakte\.ru)"
VK_NAME = re.compile(r"^[A-Za-z0-9_.]{2,64}$")
VK_RESERVED = {"feed", "im", "groups", "friends", "search", "settings", "video", "music", "audios", "photos",
               "albums", "docs", "apps", "market", "services", "login", "away.php", "share.php", "write"}


def parse_vk(raw: str) -> ChatLink | None:
    s = raw.strip().strip("<>\"'").rstrip("/.,;")
    if not s:
        return None
    m = re.match(r"^(?:https?://)?(?:www\.)?vk\.(?:me|com|ru)/join/([A-Za-z0-9_\-/=]+)", s, re.I)
    if m:
        h = m.group(1).rstrip("/")
        return ChatLink("chat:" + h, f"https://vk.me/join/{h}", "chat")
    m = re.match(rf"^{VK_DOMAIN}/(.+)$", s, re.I)
    path = m.group(1) if m else s
    path = path.split("#")[0]
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    path = path.strip("/")
    qm = re.search(r"w=wall(-\d+)_\d+", query)
    if qm:
        path = "wall" + qm.group(1) + "_0"
    m = re.match(r"^topic-(\d+)_(\d+)$", path)
    if m:
        return ChatLink(f"topic:-{m.group(1)}_{m.group(2)}", f"https://vk.com/topic-{m.group(1)}_{m.group(2)}", "topic")
    m = re.match(r"^wall-(\d+)(?:_\d+)?$", path)
    if m:
        return ChatLink(f"club{m.group(1)}", f"https://vk.com/club{m.group(1)}", "group")
    m = re.match(r"^-?(\d+)$", path)
    if m and path.startswith("-"):
        return ChatLink(f"club{m.group(1)}", f"https://vk.com/club{m.group(1)}", "group")
    m = re.match(r"^(?:club|public|event)(\d+)$", path, re.I)
    if m:
        return ChatLink(f"club{m.group(1)}", f"https://vk.com/club{m.group(1)}", "group")
    if "/" in path or not VK_NAME.match(path) or path.lower() in VK_RESERVED:
        return None
    return ChatLink("name:" + path.lower(), f"https://vk.com/{path}", "group")


def read_link_file(path: Path) -> list[str]:
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = data.decode("utf-8", errors="ignore")
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("//"):
            continue
        lines.extend(p for p in re.split(r"[\s,;]+", line) if p)
    return lines


def scan_folder(folder: Path, parser) -> tuple[dict[str, tuple[str, str, str]], list[str]]:
    folder.mkdir(parents=True, exist_ok=True)
    items: dict[str, tuple[str, str, str]] = {}
    bad: list[str] = []
    for f in sorted(folder.glob("*.txt")):
        if f.name.startswith("_"):
            continue
        try:
            raw_lines = read_link_file(f)
        except OSError:
            continue
        for raw in raw_lines:
            link = parser(raw)
            if link is None:
                bad.append(f"{f.name}: {raw}")
            elif link.key not in items:
                items[link.key] = (link.link, link.kind, f.name)
    return items, bad
