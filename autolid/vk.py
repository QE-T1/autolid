from __future__ import annotations

import asyncio
import html
import json
import logging
import random
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

from .models import Message
from .responder import SendError

if TYPE_CHECKING:
    from .app import App

log = logging.getLogger("vk")

API_URL = "https://api.vk.com/method/"
API_VERSION = "5.199"
CHAT_PEER = 2_000_000_000

ERRORS_RU = {
    5: "токен VK недействителен или отозван",
    6: "слишком много запросов",
    7: "у токена нет нужного разрешения",
    9: "VK: флуд-контроль, слишком много одинаковых действий",
    14: "VK требует капчу - слишком частые действия",
    15: "доступ запрещён",
    18: "страница удалена или заблокирована",
    30: "профиль закрыт",
    100: "неверный параметр",
    203: "нет доступа к группе",
    212: "комментарии закрыты",
    213: "комментарии закрыты",
    222: "ссылки запрещены",
    900: "вы в чёрном списке у пользователя",
    901: "пользователь запретил сообщения от сообществ",
    902: "пользователь ограничил личные сообщения настройками приватности",
    917: "нет доступа к беседе",
}


class VKError(Exception):
    def __init__(self, code: int, msg: str):
        self.code = code
        self.msg = msg
        super().__init__(f"[{code}] {ERRORS_RU.get(code, msg)}")


def chunks(seq: list, n: int):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


class VKApi:
    def __init__(self, token: str, rps: float):
        self.token = token.strip()
        self.interval = 1.0 / max(0.5, float(rps)) + 0.02
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(40.0, connect=15.0))
        self.lock = asyncio.Lock()
        self.last = 0.0
        self.auth_failed = False

    async def _throttle(self) -> None:
        async with self.lock:
            wait = self.last + self.interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self.last = time.monotonic()

    async def raw(self, method: str, **params: Any) -> dict:
        data = {k: v for k, v in params.items() if v is not None}
        data.update(access_token=self.token, v=API_VERSION)
        for attempt in range(4):
            await self._throttle()
            try:
                r = await self.http.post(API_URL + method, data=data)
                body = r.json()
            except (httpx.HTTPError, ValueError) as e:
                if attempt == 3:
                    raise VKError(-1, f"сеть: {e}") from e
                await asyncio.sleep(2 + attempt * 3)
                continue
            err = body.get("error")
            if err:
                code = int(err.get("error_code", 0))
                if code == 6 and attempt < 3:
                    await asyncio.sleep(1.0 + attempt)
                    continue
                if code == 5:
                    self.auth_failed = True
                raise VKError(code, err.get("error_msg", ""))
            return body
        raise VKError(6, "слишком много запросов")

    async def call(self, method: str, **params: Any) -> Any:
        return (await self.raw(method, **params))["response"]

    async def execute(self, calls: list[tuple[str, dict]]) -> list[Any]:
        code = "return [" + ",".join(f"API.{m}({json.dumps(p)})" for m, p in calls) + "];"
        body = await self.raw("execute", code=code)
        results = body.get("response") or []
        errs = list(body.get("execute_errors") or [])
        out: list[Any] = []
        for res in results:
            if res is False:
                e = errs.pop(0) if errs else {}
                out.append(VKError(int(e.get("error_code", 0)), e.get("error_msg", "ошибка")))
            else:
                out.append(res)
        while len(out) < len(calls):
            out.append(VKError(0, "нет ответа"))
        return out

    async def close(self) -> None:
        await self.http.aclose()


SEND_ORDER = {"reply": ["place"], "reply_then_dm": ["place", "dm"], "dm": ["dm"], "dm_then_reply": ["dm", "place"],
              "comment": ["place"], "dm_then_comment": ["dm", "place"]}

MENTION_RE = re.compile(r"\[id(\d+)\|[^\]]*\]|(?:vk\.com|vk\.ru)/id(\d+)\b|@id(\d+)\b")


class VKWorker:
    def __init__(self, app: "App"):
        self.app = app
        self.cfg = app.cfg.vk
        self.api = VKApi(self.cfg.token, self.cfg.requests_per_second)
        self.lp_http = httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=15.0))
        self.me: dict | None = None
        self.wake = asyncio.Event()
        self.chat_peers: set[int] = set()
        self.names: dict[int, tuple[str, str]] = {}
        self.lp_error = ""
        self.auth_notified = False
        self.join_block_until = 0.0

    async def start(self) -> None:
        users = await self.api.call("users.get", fields="screen_name")
        self.me = users[0]
        log.info("VK: вошли как %s %s (id%s)", self.me["first_name"], self.me["last_name"], self.me["id"])
        self.reload_active()

    def reload_active(self) -> None:
        self.chat_peers = {r["peer_id"] for r in self.app.db.chats("vk", status="active")
                           if r["kind"] == "chat" and r["peer_id"]}

    def describe(self) -> str:
        if self.api.auth_failed:
            return "⚠️ токен недействителен - обновите vk.token в config.yaml"
        if not self.me:
            return "не подключён"
        rows = self.app.db.chats("vk")
        by_kind: dict[str, dict[str, int]] = {}
        for r in rows:
            by_kind.setdefault(r["kind"], {}).setdefault(r["status"], 0)
            by_kind[r["kind"]][r["status"]] += 1
        names = {"group": "группы", "topic": "обсуждения", "chat": "беседы"}
        parts = []
        for kind, st in by_kind.items():
            p = f"{names.get(kind, kind)}: активно {st.get('active', 0)}"
            if st.get("pending"):
                p += f", в очереди {st['pending']}"
            if st.get("requested"):
                p += f", заявок {st['requested']}"
            if st.get("failed"):
                p += f", ошибок {st['failed']}"
            parts.append(p)
        s = f"{self.me['first_name']} {self.me['last_name']} - " + ("; ".join(parts) or "ссылок нет")
        if self.lp_error:
            s += f"\n   ⚠️ беседы: {self.lp_error}"
        return s

    async def stop(self) -> None:
        await self.api.close()
        await self.lp_http.aclose()

    async def _check_auth(self) -> bool:
        if self.api.auth_failed:
            if not self.auth_notified:
                self.auth_notified = True
                await self.app.bot.notify_admins("⚠️ VK: токен недействителен или отозван. "
                                                 "Получите новый (см. README), впишите в config.yaml и перезапустите.")
            return False
        return True

    async def resolve_loop(self) -> None:
        last_requested = 0.0
        while True:
            if not await self._check_auth():
                await asyncio.sleep(600)
                continue
            if time.time() - last_requested > 1800:
                last_requested = time.time()
                await self._recheck_requested()
            pending = self.app.db.chats("vk", status="pending")
            if not pending:
                self.wake.clear()
                try:
                    await asyncio.wait_for(self.wake.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self._resolve(pending[:300])
            except VKError as e:
                log.warning("VK: ошибка при добавлении групп: %s", e)
                await asyncio.sleep(30)
            except Exception:
                log.exception("VK: ошибка при добавлении групп")
                await asyncio.sleep(120)
            self.reload_active()
            still = self.app.db.chats("vk", status="pending")
            if still and {r["key"] for r in still} >= {r["key"] for r in pending[:300]}:
                await asyncio.sleep(120)

    def _fail(self, key: str, error: str) -> None:
        self.app.db.update_chat("vk", key, status="failed", error=error[:200])
        log.info("VK: не удалось добавить %s - %s", key, error)

    async def _resolve(self, rows: list) -> None:
        db = self.app.db
        gid_of: dict[str, int] = {}
        name_rows = []
        for r in rows:
            if r["kind"] == "group":
                if r["key"].startswith("club"):
                    gid_of[r["key"]] = int(r["key"][4:])
                else:
                    name_rows.append(r)
            elif r["kind"] == "topic":
                gid_of[r["key"]] = int(r["key"].split(":-")[1].split("_")[0])
        for part in chunks(name_rows, 25):
            res = await self.api.execute([("utils.resolveScreenName", {"screen_name": r["key"][5:]}) for r in part])
            for r, x in zip(part, res):
                if isinstance(x, VKError):
                    self._fail(r["key"], str(x))
                elif not x or not isinstance(x, dict):
                    self._fail(r["key"], "страница не найдена")
                elif x.get("type") in ("group", "page", "event"):
                    gid_of[r["key"]] = int(x["object_id"])
                elif x.get("type") == "user":
                    self._fail(r["key"], "это страница пользователя, а не группа")
                else:
                    self._fail(r["key"], f"неподдерживаемый тип: {x.get('type')}")

        info: dict[int, dict] = {}
        gids = sorted(set(gid_of.values()))
        for part in chunks(gids, 400):
            resp = await self.api.call("groups.getById", group_ids=",".join(map(str, part)),
                                       fields="is_closed,is_member,screen_name,deactivated")
            for g in (resp.get("groups", []) if isinstance(resp, dict) else resp):
                info[int(g["id"])] = g

        closed: list[tuple] = []
        for r in rows:
            key = r["key"]
            if r["kind"] == "chat":
                await self._join_chat(r)
                continue
            gid = gid_of.get(key)
            if gid is None:
                continue
            g = info.get(gid)
            if not g:
                self._fail(key, "группа не найдена")
                continue
            if g.get("deactivated"):
                self._fail(key, "группа удалена или заблокирована")
                continue
            if g.get("is_closed") and not g.get("is_member"):
                if g.get("is_closed") == 2:
                    self._fail(key, "частная группа - вступить можно только по приглашению")
                elif self.cfg.auto_join_closed:
                    closed.append((key, gid, g))
                else:
                    self._fail(key, "закрытая группа - вступите вручную или включите vk.auto_join_closed")
                continue
            extra: dict = {"screen_name": g.get("screen_name", "")}
            title = g.get("name", "")
            if r["kind"] == "topic":
                extra["topic_id"] = int(key.split("_")[-1])
                title += " · обсуждение"
            db.update_chat("vk", key, status="active", peer_id=-gid, title=title,
                           username=g.get("screen_name"), error=None, extra=extra)
            log.info("VK: слежу за «%s»", title)

        for key, gid, g in closed[:10]:
            if time.time() < self.join_block_until:
                break
            try:
                await self.api.call("groups.join", group_id=gid)
                db.update_chat("vk", key, status="requested", peer_id=-gid, title=g.get("name"),
                               error="закрытая группа - заявка отправлена, жду одобрения")
                log.info("VK: заявка на вступление в «%s»", g.get("name"))
            except VKError as e:
                if e.code in (9, 14):
                    self.join_block_until = time.time() + 3600
                    log.warning("VK: капча/флуд при вступлении - продолжу через час")
                    await self.app.bot.notify_admins("⏳ VK ограничил заявки в закрытые группы (капча). "
                                                     "Продолжу автоматически через час.")
                    break
                self._fail(key, f"закрытая группа, заявку отправить не удалось: {e}")
            await asyncio.sleep(random.uniform(20, 40))

    async def _join_chat(self, row) -> None:
        link = row["link"]
        try:
            resp = await self.api.call("messages.joinChatByInviteLink", link=link)
            chat_id = int(resp["chat_id"])
        except VKError as e:
            try:
                prev = await self.api.call("messages.getChatPreview", link=link)
                chat_id = int(prev.get("preview", {}).get("local_id") or 0)
            except VKError:
                chat_id = 0
            if not chat_id:
                self._fail(row["key"], f"не удалось вступить в беседу: {e}")
                return
        peer = CHAT_PEER + chat_id
        title = ""
        try:
            conv = await self.api.call("messages.getConversationsById", peer_ids=peer)
            title = conv["items"][0]["chat_settings"]["title"]
        except (VKError, KeyError, IndexError):
            pass
        self.app.db.update_chat("vk", row["key"], status="active", peer_id=peer, title=title or f"Беседа {chat_id}",
                                error=None)
        log.info("VK: слежу за беседой «%s»", title)

    async def _recheck_requested(self) -> None:
        rows = self.app.db.chats("vk", status="requested")
        if not rows:
            return
        try:
            resp = await self.api.call("groups.getById", group_ids=",".join(str(-r["peer_id"]) for r in rows),
                                       fields="is_member,screen_name")
        except VKError:
            return
        members = {int(g["id"]) for g in (resp.get("groups", []) if isinstance(resp, dict) else resp)
                   if g.get("is_member")}
        for r in rows:
            if -r["peer_id"] in members:
                self.app.db.update_chat("vk", r["key"], status="active", error=None)
                await self.app.bot.notify_admins(
                    f"✅ VK: заявку одобрили - теперь слежу за группой «{html.escape(r['title'] or r['link'])}»")

    async def poll_loop(self) -> None:
        while True:
            started = time.time()
            if await self._check_auth():
                try:
                    await self._poll_walls()
                    await self._poll_topics()
                except VKError as e:
                    log.warning("VK: ошибка опроса: %s", e)
                    if e.code == 14:
                        await self.app.bot.notify_admins("⚠️ VK запросил капчу - опрос приостановлен на 15 минут.")
                        await asyncio.sleep(900)
                except Exception:
                    log.exception("VK: ошибка опроса")
            elapsed = time.time() - started
            await asyncio.sleep(max(5.0, float(self.cfg.poll_interval) - elapsed))

    def _wall_meta(self) -> dict[int, dict]:
        meta: dict[int, dict] = {}
        for r in self.app.db.chats("vk", status="active"):
            if r["kind"] != "group" or not r["peer_id"]:
                continue
            gid = -r["peer_id"]
            m = meta.setdefault(gid, {"title": r["title"] or "", "screen": r["username"] or f"club{gid}", "keys": []})
            m["keys"].append(r["key"])
        return meta

    async def _poll_walls(self) -> None:
        meta = self._wall_meta()
        if not meta:
            return
        n = max(1, min(100, int(self.cfg.posts_per_check)))
        need_comments: list[tuple[int, int, int, int]] = []
        for part in chunks(list(meta), 25):
            calls = [("wall.get", {"owner_id": -gid, "count": n, "extended": 1, "fields": "screen_name"}) for gid in part]
            for gid, res in zip(part, await self.api.execute(calls)):
                if isinstance(res, VKError):
                    if res.code in (15, 18, 30, 203):
                        for key in meta[gid]["keys"]:
                            self._fail(key, f"стена недоступна: {res}")
                    else:
                        log.debug("VK wall.get %s: %s", gid, res)
                    continue
                need_comments += await self._handle_wall(gid, res, meta[gid])
        if need_comments and self.cfg.check_comments:
            await self._fetch_comments(need_comments, meta)

    async def _handle_wall(self, gid: int, res: dict, meta: dict) -> list[tuple[int, int, int, int]]:
        db = self.app.db
        owner = -gid
        items = res.get("items", [])
        profiles = {p["id"]: p for p in res.get("profiles", [])}
        groups = {g["id"]: g for g in res.get("groups", [])}
        state = db.one("SELECT * FROM vk_walls WHERE owner_id=?", (owner,))
        last = state["last_post_id"] if state else 0
        initialized = bool(state and state["initialized"])
        now = time.time()
        window = float(self.cfg.comments_window_hours) * 3600
        need: list[tuple[int, int, int, int]] = []
        for p in sorted(items, key=lambda x: x["id"]):
            pid, date = int(p["id"]), int(p.get("date", 0))
            ccount = int((p.get("comments") or {}).get("count", 0))
            if pid > last:
                await self._emit_post(gid, p, profiles, groups, meta)
                if self.cfg.check_comments and now - date < window:
                    db.x("INSERT OR IGNORE INTO vk_posts(owner_id, post_id, date, comments_count, last_comment_id) "
                         "VALUES(?,?,?,?,0)", (owner, pid, date, 0 if initialized else ccount))
                    if initialized and ccount:
                        need.append((gid, pid, 0, ccount))
            elif self.cfg.check_comments and now - date < window:
                row = db.one("SELECT * FROM vk_posts WHERE owner_id=? AND post_id=?", (owner, pid))
                if row is None:
                    db.x("INSERT OR IGNORE INTO vk_posts(owner_id, post_id, date, comments_count, last_comment_id) "
                         "VALUES(?,?,?,?,0)", (owner, pid, date, ccount))
                elif ccount > row["comments_count"]:
                    need.append((gid, pid, row["last_comment_id"], ccount))
        max_id = max([last] + [int(p["id"]) for p in items])
        db.x("INSERT OR REPLACE INTO vk_walls(owner_id, last_post_id, initialized) VALUES(?,?,1)", (owner, max_id))
        return need

    def _author(self, from_id: int, profiles: dict, groups: dict, text: str = "") -> tuple[int | None, str, str, str]:
        if from_id and from_id > 0:
            p = profiles.get(from_id) or {}
            name = " ".join(x for x in (p.get("first_name"), p.get("last_name")) if x) or self.names.get(from_id, ("", ""))[0]
            screen = p.get("screen_name") or ""
            if name:
                self.names[from_id] = (name, screen)
            return from_id, name or f"id{from_id}", screen, f"https://vk.com/id{from_id}"
        m = MENTION_RE.search(text or "")
        if m:
            uid = int(next(g for g in m.groups() if g))
            return uid, f"id{uid}", "", f"https://vk.com/id{uid}"
        if from_id and from_id < 0:
            g = groups.get(-from_id) or {}
            return None, g.get("name", "сообщество"), "", f"https://vk.com/club{-from_id}"
        return None, "", "", ""

    async def _emit_post(self, gid: int, p: dict, profiles: dict, groups: dict, meta: dict) -> None:
        text = p.get("text") or ""
        if not text and p.get("copy_history"):
            text = p["copy_history"][0].get("text") or ""
        msg_id = f"w{p['id']}"
        score = self.app.pipeline.prefilter("vk", -gid, msg_id, text, float(p.get("date", 0)))
        if score is None:
            return
        from_id = int(p.get("signer_id") or p.get("from_id") or 0)
        aid, name, screen, link = self._author(from_id, profiles, groups, text)
        await self.app.pipeline.submit(Message(
            platform="vk", chat_id=-gid, msg_id=msg_id, text=text, date=float(p.get("date", 0)),
            chat_title=meta["title"], chat_link=f"https://vk.com/{meta['screen']}",
            msg_link=f"https://vk.com/wall-{gid}_{p['id']}",
            author_id=aid, author_name=name, author_username=screen, author_link=link,
            reply_ctx={"owner_id": -gid, "post_id": int(p["id"])},
        ), score)

    async def _fetch_comments(self, need: list[tuple[int, int, int, int]], meta: dict) -> None:
        db = self.app.db
        for part in chunks(need, 25):
            calls = [("wall.getComments", {"owner_id": -gid, "post_id": pid, "count": 100, "sort": "desc",
                                           "extended": 1, "thread_items_count": 10, "fields": "screen_name"})
                     for gid, pid, _last, _cnt in part]
            for (gid, pid, last_cid, cnt), res in zip(part, await self.api.execute(calls)):
                if isinstance(res, VKError):
                    db.x("UPDATE vk_posts SET comments_count=? WHERE owner_id=? AND post_id=?", (cnt, -gid, pid))
                    continue
                profiles = {p["id"]: p for p in res.get("profiles", [])}
                groups = {g["id"]: g for g in res.get("groups", [])}
                comments = []
                for c in res.get("items", []):
                    comments.append((c, None))
                    for t in (c.get("thread") or {}).get("items", []):
                        comments.append((t, c["id"]))
                max_cid = last_cid
                for c, parent in sorted(comments, key=lambda x: x[0]["id"]):
                    cid = int(c["id"])
                    max_cid = max(max_cid, cid)
                    if cid <= last_cid:
                        continue
                    await self._emit_comment(gid, pid, c, parent, profiles, groups, meta.get(gid, {}))
                db.x("UPDATE vk_posts SET comments_count=?, last_comment_id=? WHERE owner_id=? AND post_id=?",
                     (cnt, max_cid, -gid, pid))

    async def _emit_comment(self, gid: int, pid: int, c: dict, parent: int | None, profiles: dict, groups: dict,
                            meta: dict) -> None:
        text = c.get("text") or ""
        msg_id = f"c{pid}_{c['id']}"
        score = self.app.pipeline.prefilter("vk", -gid, msg_id, text, float(c.get("date", 0)))
        if score is None:
            return
        aid, name, screen, link = self._author(int(c.get("from_id") or 0), profiles, groups)
        url = f"https://vk.com/wall-{gid}_{pid}?reply={c['id']}" + (f"&thread={parent}" if parent else "")
        await self.app.pipeline.submit(Message(
            platform="vk", chat_id=-gid, msg_id=msg_id, text=text, date=float(c.get("date", 0)),
            chat_title=(meta.get("title") or "") + " · комментарий", chat_link=f"https://vk.com/{meta.get('screen', '')}",
            msg_link=url, author_id=aid, author_name=name, author_username=screen, author_link=link,
            reply_ctx={"owner_id": -gid, "post_id": pid, "comment_id": int(c["id"])},
        ), score)

    async def _poll_topics(self) -> None:
        db = self.app.db
        rows = [r for r in db.chats("vk", status="active") if r["kind"] == "topic"]
        for part in chunks(rows, 25):
            calls = []
            for r in part:
                extra = json.loads(r["extra"] or "{}")
                calls.append(("board.getComments", {"group_id": -r["peer_id"], "topic_id": extra.get("topic_id"),
                                                    "count": 20, "sort": "desc", "extended": 1}))
            for r, res in zip(part, await self.api.execute(calls)):
                if isinstance(res, VKError):
                    if res.code in (15, 18, 100, 203):
                        self._fail(r["key"], f"обсуждение недоступно: {res}")
                    continue
                gid, tid = -r["peer_id"], json.loads(r["extra"] or "{}").get("topic_id")
                state = db.one("SELECT * FROM vk_topics WHERE group_id=? AND topic_id=?", (gid, tid))
                last = state["last_comment_id"] if state else 0
                profiles = {p["id"]: p for p in res.get("profiles", [])}
                groups = {g["id"]: g for g in res.get("groups", [])}
                items = sorted(res.get("items", []), key=lambda x: x["id"])
                for c in items:
                    if int(c["id"]) <= last:
                        continue
                    text = c.get("text") or ""
                    msg_id = f"t{tid}_{c['id']}"
                    score = self.app.pipeline.prefilter("vk", -gid, msg_id, text, float(c.get("date", 0)))
                    if score is None:
                        continue
                    aid, name, screen, link = self._author(int(c.get("from_id") or 0), profiles, groups)
                    await self.app.pipeline.submit(Message(
                        platform="vk", chat_id=-gid, msg_id=msg_id, text=text, date=float(c.get("date", 0)),
                        chat_title=r["title"] or "", chat_link=r["link"],
                        msg_link=f"https://vk.com/topic-{gid}_{tid}?post={c['id']}",
                        author_id=aid, author_name=name, author_username=screen, author_link=link,
                        reply_ctx={"group_id": gid, "topic_id": tid, "comment_id": int(c["id"])},
                    ), score)
                max_id = max([last] + [int(c["id"]) for c in items])
                db.x("INSERT OR REPLACE INTO vk_topics(group_id, topic_id, last_comment_id, initialized) VALUES(?,?,?,1)",
                     (gid, tid, max_id))

    async def longpoll_loop(self) -> None:
        while True:
            if not await self._check_auth():
                await asyncio.sleep(600)
                continue
            if not self.chat_peers and not self.cfg.monitor_all_chats:
                await asyncio.sleep(30)
                continue
            try:
                srv = await self.api.call("messages.getLongPollServer", lp_version=3)
                self.lp_error = ""
            except VKError as e:
                self.lp_error = f"нет доступа к сообщениям ({e}). Нужен токен с правом messages - см. README"
                log.warning("VK Long Poll: %s", e)
                await asyncio.sleep(600)
                continue
            ts = srv["ts"]
            while True:
                try:
                    r = await self.lp_http.get(f"https://{srv['server']}", params={
                        "act": "a_check", "key": srv["key"], "ts": ts, "wait": 25, "mode": 2, "version": 3})
                    data = r.json()
                except (httpx.HTTPError, ValueError) as e:
                    log.debug("VK Long Poll: %s", e)
                    await asyncio.sleep(5)
                    continue
                if "failed" in data:
                    if data["failed"] == 1:
                        ts = data.get("ts", ts)
                        continue
                    break
                ts = data.get("ts", ts)
                for u in data.get("updates", []):
                    if u and u[0] == 4:
                        try:
                            await self._on_lp_message(u)
                        except Exception:
                            log.exception("VK: ошибка обработки сообщения беседы")

    async def _on_lp_message(self, u: list) -> None:
        msg_id, flags, peer_id = int(u[1]), int(u[2]), int(u[3])
        date = float(u[4]) if len(u) > 4 else time.time()
        text = u[5] if len(u) > 5 else ""
        extra = u[6] if len(u) > 6 and isinstance(u[6], dict) else {}
        if flags & 2 or peer_id < CHAT_PEER:
            return
        if peer_id not in self.chat_peers and not self.cfg.monitor_all_chats:
            return
        text = html.unescape((text or "").replace("<br>", "\n"))
        mid = f"m{msg_id}"
        score = self.app.pipeline.prefilter("vk", peer_id, mid, text, date)
        if score is None:
            return
        from_id = int(extra.get("from") or 0)
        name, screen = await self._user_name(from_id)
        title = extra.get("title") or ""
        if not title:
            row = self.app.db.one("SELECT title FROM chats WHERE platform='vk' AND peer_id=?", (peer_id,))
            title = row["title"] if row else f"Беседа {peer_id - CHAT_PEER}"
        await self.app.pipeline.submit(Message(
            platform="vk", chat_id=peer_id, msg_id=mid, text=text, date=date, chat_title=title,
            author_id=from_id or None, author_name=name, author_username=screen,
            author_link=f"https://vk.com/id{from_id}" if from_id > 0 else "",
            reply_ctx={"peer_id": peer_id, "message_id": msg_id},
        ), score)

    async def _user_name(self, uid: int) -> tuple[str, str]:
        if uid <= 0:
            return "", ""
        if uid not in self.names:
            try:
                u = (await self.api.call("users.get", user_ids=uid, fields="screen_name"))[0]
                self.names[uid] = (f"{u.get('first_name', '')} {u.get('last_name', '')}".strip(), u.get("screen_name", ""))
            except (VKError, IndexError):
                return f"id{uid}", ""
        return self.names[uid]

    async def show_typing(self, lead: dict, seconds: float) -> None:
        order = SEND_ORDER.get(self.app.cfg.reply.vk_method, ["place"])
        ctx = lead["extra"].get("ctx", {})
        peer = ctx.get("peer_id") if order[0] == "place" else lead["author_id"]
        end = time.time() + seconds
        while time.time() < end:
            if peer:
                try:
                    await self.api.call("messages.setActivity", peer_id=peer, type="typing")
                except VKError:
                    peer = None
            await asyncio.sleep(min(5.0, max(0.0, end - time.time())))

    async def send_reply(self, lead: dict, text: str, typing: float | None = None) -> str:
        order = SEND_ORDER.get(self.app.cfg.reply.vk_method, ["place"])
        ctx = lead["extra"].get("ctx", {})
        if typing is None and (ctx.get("peer_id") or order[0] == "dm"):
            await self.show_typing(lead, min(6.0, 1.5 + len(text) / 30))
        failed: list[str] = []
        for way in order:
            try:
                if way == "place":
                    result = await self._send_in_place(lead, text)
                else:
                    if not lead["author_id"] or lead["author_id"] < 0:
                        failed.append("в личку: автор неизвестен")
                        continue
                    try:
                        await self.api.call("messages.send", user_id=lead["author_id"],
                                            random_id=random.randint(1, 2**31 - 1), message=text)
                    except VKError as e:
                        raise SendError(f"в личку: {e}", block_seconds=1800 if e.code in (9, 14) else 0)
                    result = "в личку"
                return result + (f" ({'; '.join(failed)})" if failed else "")
            except SendError as e:
                if e.block_seconds:
                    raise
                failed.append(str(e))
                log.info("Лид #%d: не вышло (%s)", lead["id"], e)
        raise SendError("; ".join(failed) or "некуда ответить")

    async def _send_in_place(self, lead: dict, text: str) -> str:
        ctx = lead["extra"].get("ctx", {})
        author = lead["author_id"]
        first = (lead["author_name"] or "").split()[0] if lead["author_name"] else ""
        mention = ""
        if author and author > 0:
            mention = f"[id{author}|{first or 'id' + str(author)}], "
            if first and text.lower().startswith(first.lower()):
                rest = re.sub(r"^[\s,!.:--]+", "", text[len(first):])
                text = rest[:1].lower() + rest[1:] if rest else text
        try:
            if ctx.get("post_id"):
                params: dict[str, Any] = {"owner_id": ctx["owner_id"], "post_id": ctx["post_id"],
                                          "message": mention + text}
                if ctx.get("comment_id"):
                    params["reply_to_comment"] = ctx["comment_id"]
                try:
                    await self.api.call("wall.createComment", **params)
                except VKError as e:
                    if e.code not in (15, 213) or ctx["owner_id"] >= 0:
                        raise
                    await self.api.call("groups.join", group_id=-ctx["owner_id"])
                    await asyncio.sleep(2)
                    await self.api.call("wall.createComment", **params)
                return "ответом на комментарий" if ctx.get("comment_id") else "комментарием под постом"
            if ctx.get("topic_id"):
                await self.api.call("board.createComment", group_id=ctx["group_id"], topic_id=ctx["topic_id"],
                                    message=mention + text)
                return "ответом в обсуждении"
            if ctx.get("peer_id"):
                params = {"peer_id": ctx["peer_id"], "random_id": random.randint(1, 2**31 - 1),
                          "message": mention + text}
                try:
                    await self.api.call("messages.send", reply_to=ctx.get("message_id"), **params)
                except VKError as e:
                    if e.code in (9, 14):
                        raise
                    params["random_id"] = random.randint(1, 2**31 - 1)
                    await self.api.call("messages.send", **params)
                return "ответом в беседе"
        except VKError as e:
            raise SendError(f"на месте: {e}", block_seconds=1800 if e.code in (9, 14) else 0)
        raise SendError("на месте: неизвестно, куда отвечать")
