from __future__ import annotations

import asyncio
import getpass
import re

import httpx

from .config import load_config, path_of, set_config_value

LINE = "─" * 60


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def yes(prompt: str, default: bool = True) -> bool:
    d = "Д/н" if default else "д/Н"
    v = input(f"{prompt} ({d}): ").strip().lower()
    if not v:
        return default
    return v in ("д", "да", "y", "yes", "l")


async def setup_telegram(cfg) -> bool:
    print(f"\n{LINE}\n1. АККАУНТ TELEGRAM (с него читаются чаты и отправляются ответы)\n{LINE}")
    api_id, api_hash = str(cfg.telegram.api_id).strip(), str(cfg.telegram.api_hash).strip()
    if not api_id or not api_hash:
        print("Нужны api_id и api_hash - это бесплатно и занимает минуту:\n"
              "  1) откройте https://my.telegram.org и войдите по номеру телефона\n"
              "  2) раздел «API development tools»\n"
              "  3) заполните App title и Short name любыми словами (например AutoLid / autolid), Platform: Desktop\n"
              "  4) скопируйте сюда App api_id и App api_hash")
        while True:
            api_id = ask("api_id")
            if api_id.isdigit():
                break
            print("  api_id - это число, попробуйте ещё раз")
        api_hash = ask("api_hash")
        set_config_value("telegram", "api_id", api_id)
        set_config_value("telegram", "api_hash", api_hash)
        cfg = load_config()

    from .tg import make_client
    data_dir = path_of(cfg, "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    client = make_client(cfg, data_dir)
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"✔ Уже вошли как {me.first_name or ''} {me.last_name or ''} (@{me.username or '-'})")
            if not yes("Оставить этот аккаунт?"):
                await client.log_out()
                client = make_client(cfg, data_dir)
                await client.connect()
            else:
                return True
        phone = ask("Номер телефона аккаунта (в формате +79991234567)", str(cfg.telegram.phone or ""))
        set_config_value("telegram", "phone", phone)
        await client.start(
            phone=lambda: phone,
            code_callback=lambda: ask("Код из Telegram (придёт в приложение)"),
            password=lambda: getpass.getpass("Пароль двухэтапной проверки (ввод скрыт): "),
        )
        me = await client.get_me()
        print(f"✔ Вошли как {me.first_name or ''} {me.last_name or ''} (@{me.username or '-'})")
        return True
    except Exception as e:
        print(f"✖ Не удалось войти: {e}")
        return False
    finally:
        await client.disconnect()


async def setup_bot(cfg) -> None:
    print(f"\n{LINE}\n2. БОТ ДЛЯ УВЕДОМЛЕНИЙ (сюда будут приходить лиды)\n{LINE}")
    token = str(cfg.bot.token).strip()
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            if token:
                try:
                    me = (await http.get(f"https://api.telegram.org/bot{token}/getMe")).json()
                except httpx.HTTPError as e:
                    print(f"✖ Нет связи с api.telegram.org: {e}")
                    me = {}
                if me.get("ok"):
                    username = me["result"]["username"]
                    print(f"✔ Бот @{username}")
                    set_config_value("bot", "token", token)
                    break
                print("✖ Токен не подошёл")
            print("Создайте бота: откройте @BotFather в Telegram → /newbot → придумайте имя → скопируйте токен")
            token = ask("Токен бота")

        from .db import DB
        db = DB(path_of(cfg, "data") / "autolid.db")
        allowed = {str(u).strip().lstrip("@").lower() for u in (cfg.bot.get("admin_usernames") or []) if str(u).strip()}
        users = db.kv_get("admin_users", {}) or {}
        known = {uid: u for uid, u in users.items() if not allowed or (u or "").lower() in allowed}
        if known or cfg.bot.admin_ids:
            print("✔ Администраторы бота: " + ", ".join([f"@{u}" if u else uid for uid, u in known.items()]
                                                      + [str(x) for x in cfg.bot.admin_ids or []]))
            return
        who = " или ".join("@" + u for u in sorted(allowed)) if allowed else "вы"
        print(f"\nЛиды будет получать: {who}.\n"
              f"Откройте https://t.me/{username} с этого аккаунта и нажмите «Старт» (/start).")
        for _attempt in range(3):
            input("Когда отправите боту /start - нажмите Enter здесь...")
            r = (await http.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"timeout": 5},
                                timeout=20)).json()
            starters = [u["message"] for u in r.get("result", [])
                        if (u.get("message") or {}).get("chat", {}).get("type") == "private"
                        and (u["message"].get("text") or "").startswith("/start")
                        and (not allowed or (u["message"]["from"].get("username") or "").lower() in allowed)]
            if starters:
                msg = starters[-1]
                uid = msg["from"]["id"]
                uname = (msg["from"].get("username") or "").lower()
                users[str(uid)] = uname
                db.kv_set("admin_users", users)
                await http.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": uid, "text": "✅ Вы администратор AutoLid. "
                                      "Сюда будут приходить лиды, когда программа запущена."})
                last = r["result"][-1]["update_id"] + 1
                await http.get(f"https://api.telegram.org/bot{token}/getUpdates", params={"offset": last})
                print(f"✔ Администратор: @{uname or '-'} (id {uid})")
                return
            print(f"  Пока не вижу /start от {who}.")
        print("Ничего страшного: после запуска программы достаточно нажать /start в боте.")


async def setup_vk(cfg) -> None:
    print(f"\n{LINE}\n3. ВКОНТАКТЕ (необязательно)\n{LINE}")
    token = str(cfg.vk.token).strip()
    async with httpx.AsyncClient(timeout=30) as http:
        while True:
            if token:
                r = (await http.post("https://api.vk.com/method/users.get",
                                     data={"access_token": token, "v": "5.199"})).json()
                if "response" in r:
                    u = r["response"][0]
                    print(f"✔ VK: {u['first_name']} {u['last_name']} (id{u['id']})")
                    set_config_value("vk", "token", token)
                    return
                print(f"✖ Токен не подошёл: {r.get('error', {}).get('error_msg')}")
            print("Как получить токен - см. раздел «Токен VK» в README.md.\n"
                  "Можно вставить сам токен или всю ссылку из адресной строки (с access_token=...).\n"
                  "Пусто + Enter - пропустить VK.")
            raw = ask("Токен VK")
            if not raw:
                print("VK пропущен.")
                return
            m = re.search(r"access_token=([^&\s]+)", raw)
            token = m.group(1) if m else raw.strip()


async def check_ai(cfg) -> None:
    print(f"\n{LINE}\n4. ПРОВЕРКА ИИ\n{LINE}")
    if not (cfg.ai.enabled and cfg.ai.api_key):
        print("ИИ выключен или нет ключа - программа будет работать только по ключевым словам.")
        return
    from .ai import AI
    from .db import DB
    ai = AI(cfg, path_of(cfg, "prompts"), DB(path_of(cfg, "data") / "autolid.db"))
    try:
        answer = await ai.ping()
        print(f"✔ ИИ отвечает ({cfg.ai.model}): {answer}")
    except Exception as e:
        print(f"⚠ ИИ сейчас не отвечает: {e}\n  Программа всё равно запустится и будет повторять попытки.")
    finally:
        await ai.close()


async def run_wizard() -> None:
    print(LINE)
    print("  МАСТЕР НАСТРОЙКИ AutoLid")
    print(LINE)
    cfg = load_config()
    await setup_telegram(cfg)
    cfg = load_config()
    await setup_bot(cfg)
    cfg = load_config()
    await setup_vk(cfg)
    cfg = load_config()
    await check_ai(cfg)
    print(f"\n{LINE}\nГотово! Положите файлы со ссылками в папки chats/telegram и chats/vk\n"
          f"и запустите программу (start.bat / start.sh).\n{LINE}")


def _mask(key: str) -> str:
    key = str(key or "")
    return key[:5] + "..." + key[-4:] if len(key) > 12 else ("задан" if key else "не задан")


def _normalize_base_url(url: str) -> str:
    url = url.strip().rstrip("/")
    if not url.startswith("http"):
        url = "https://" + url
    if url.count("/") == 2:
        url += "/v1"
    return url


async def _fetch_models(base_url: str, api_key: str) -> list[str]:
    headers = {"Authorization": f"Bearer {api_key}", "x-api-key": api_key, "anthropic-version": "2023-06-01"}
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.get(base_url.rstrip("/") + "/models", headers=headers)
        return sorted(m.get("id") for m in r.json().get("data", []) if m.get("id"))
    except Exception:
        return []


async def change_ai() -> None:
    cfg = load_config()
    print(LINE)
    print("  СМЕНА ИИ (адрес API, ключ, модель)")
    print(LINE)
    print(f"Сейчас: адрес {cfg.ai.base_url}\n        ключ  {_mask(cfg.ai.api_key)}\n        модель {cfg.ai.model}\n")
    print("Enter без текста - оставить как есть.\n")
    base_url = _normalize_base_url(ask("Адрес API (Base URL)", str(cfg.ai.base_url)))
    api_key = ask("Ключ API (sk-...)", "") or str(cfg.ai.api_key)

    models = await _fetch_models(base_url, api_key)
    model = str(cfg.ai.model)
    if models:
        print(f"\nДоступные модели ({len(models)}):")
        for i, m in enumerate(models, 1):
            print(f"  {i:>3}. {m}{'   ← сейчас' if m == model else ''}")
        while True:
            choice = ask("\nНомер или название модели", model)
            if choice.isdigit() and 1 <= int(choice) <= len(models):
                model = models[int(choice) - 1]
                break
            if choice:
                model = choice
                break
    else:
        print("\nСписок моделей получить не удалось (проверьте адрес и ключ) - введите название вручную.")
        model = ask("Название модели", model)

    from .ai import AI
    from .db import DB
    test_cfg = load_config()
    test_cfg["ai"].update(base_url=base_url, api_key=api_key, model=model, enabled=True)
    ai = AI(test_cfg, path_of(test_cfg, "prompts"), DB(path_of(test_cfg, "data") / "autolid.db"))
    print(f"\nПроверяю {model}...")
    ok = False
    try:
        answer = await ai.ping()
        print(f"✔ Модель отвечает: {answer}")
        ok = True
    except Exception as e:
        print(f"✖ Модель не ответила: {e}")
    finally:
        await ai.close()
    if not ok and not yes("Сохранить всё равно?", default=False):
        print("Ничего не изменено.")
        return
    set_config_value("ai", "base_url", base_url)
    set_config_value("ai", "api_key", api_key)
    set_config_value("ai", "model", model)
    set_config_value("ai", "enabled", True)
    print(f"\n✔ Сохранено: {model} @ {base_url}\nЕсли программа запущена - перезапустите start.bat.")


def needs_setup(cfg) -> bool:
    if not str(cfg.bot.token).strip():
        return True
    if not (str(cfg.telegram.api_id).strip() and str(cfg.telegram.api_hash).strip()):
        return True
    session = path_of(cfg, "data") / "telegram.session"
    return not session.exists()


if __name__ == "__main__":
    asyncio.run(run_wizard())
