from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

if sys.version_info < (3, 10):
    sys.exit("Нужен Python 3.10 или новее")

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except Exception:
        pass

from autolid.config import ROOT, load_config, path_of


def setup_logging(cfg) -> None:
    log_dir = path_of(cfg, "data") / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%d.%m %H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    file = RotatingFileHandler(log_dir / "autolid.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8")
    file.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console)
    root.addHandler(file)
    for noisy in ("telethon", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


SAMPLES = [
    "ребят, кто может решить практическую по эконометрике? ВШЭ, 2 курс. срочно до завтра. недорого. пишите в лс",
    "Нужна помощь с контрольной по бухучету. РЭУ Плеханова. Завтра сдавать. Кто шарит? 🙏",
    "Помогите сделать курсовую по финансам. Финэк. Тема: управление капиталом. Срок до конца недели. Бюджет 5к.",
    "кто может решить задачу по микроэкономике? МГУ, 1 курс. срочно, завтра зачет. недорого",
    "Нужен отчет по практике. Экономика предприятия. СПбГЭУ. Срок 3 дня. Оплата сразу. Пишите.",
    "контра по макро, помогите плиз. КФУ. до завтра надо. срочно 😭",
    "Сделаю практику по статистике. НГУ. Недорого. В лс.",
    "кто шарит в эконом анализе? нужна помощь с контрольной. СПбГУ. завтра сдавать",
    "Ребят, а кто-нибудь знает когда сессия начинается?",
    "Помогу с курсовыми и дипломами по экономике, все предметы, гарантия, антиплагиат",
    "можете подсказать по тервер задачке, не понимаю как решать",
    "Всем привет, кто идет завтра на пары?",
]


async def run_test(texts: list[str]) -> None:
    import time

    from autolid.ai import AI, AIError
    from autolid.db import DB
    from autolid.keywords import KeywordFilter
    from autolid.models import Message

    cfg = load_config()
    kw = KeywordFilter(path_of(cfg, "keywords"))
    ai = AI(cfg, path_of(cfg, "prompts"), DB(path_of(cfg, "data") / "autolid.db"))
    print(f"Порог: на ИИ от {kw.min_score} баллов, глубокая проверка от {kw.deep_scan_min_score}, "
          f"лид без ИИ от {kw.sure_score}\n")
    to_ai = []
    for t in texts:
        s = kw.score(t)
        route = ("⛔ стоп-слово " + s.blacklisted if s.blacklisted else
                 "→ ИИ" if s.score >= kw.min_score else
                 "→ ИИ (глубокая)" if s.score >= kw.deep_scan_min_score and cfg.ai.deep_scan else "отброшено")
        print(f"[{s.score:>3}] {route:<16} {t[:90]}")
        if not s.blacklisted and (s.score >= kw.min_score or (cfg.ai.deep_scan and s.score >= kw.deep_scan_min_score)):
            to_ai.append(t)
    if not ai.enabled or not to_ai:
        await ai.close()
        return
    print(f"\nОтправляю {len(to_ai)} сообщений ИИ ({cfg.ai.model})...")
    started = time.time()
    try:
        msgs = [Message(platform="test", chat_id=0, msg_id=str(i), text=t, date=time.time(), chat_title="тест")
                for i, t in enumerate(to_ai)]
        verdicts = await ai.classify(msgs)
        print(f"Ответ за {time.time() - started:.1f} с\n")
        for t, v in zip(to_ai, verdicts):
            if v is None:
                print(f"  ?  нет ответа: {t[:70]}")
                continue
            mark = "✅ ЛИД" if v.is_lead and v.confidence >= int(cfg.ai.min_confidence) else "❌ нет"
            print(f"{mark} {v.confidence:>3}%  {t[:80]}")
            details = " · ".join(x for x in (v.work_type, v.subject, v.university, v.deadline, v.budget) if x)
            if details:
                print(f"           {details}")
            if v.reply:
                print(f"           ✉️  {v.reply}")
    except AIError as e:
        print(f"ИИ недоступен: {e}")
    finally:
        await ai.close()


def main() -> None:
    args = sys.argv[1:]
    cmd = args[0].lower() if args else "run"
    os.chdir(ROOT)
    cfg = load_config()

    if cmd == "setup":
        from autolid.setup_wizard import run_wizard
        asyncio.run(run_wizard())
        return
    if cmd == "ai":
        from autolid.setup_wizard import change_ai
        asyncio.run(change_ai())
        return
    if cmd == "test":
        asyncio.run(run_test(SAMPLES))
        return
    if cmd == "check":
        asyncio.run(run_test([" ".join(args[1:])]))
        return

    from autolid.setup_wizard import needs_setup, run_wizard
    if needs_setup(cfg):
        print("Программа ещё не настроена - запускаю мастер настройки.\n")
        asyncio.run(run_wizard())
        cfg = load_config()
        if needs_setup(cfg):
            sys.exit("Настройка не завершена. Запустите ещё раз: python main.py setup")

    setup_logging(cfg)
    from autolid.app import App
    try:
        asyncio.run(App(cfg).run())
    except KeyboardInterrupt:
        print("\nОстановлено.")
    except SystemExit:
        raise
    except Exception:
        logging.getLogger("main").exception("Программа остановилась из-за ошибки")
        sys.exit(1)


if __name__ == "__main__":
    main()
