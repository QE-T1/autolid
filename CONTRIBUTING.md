# Как помочь

Буду рад issue и pull request'ам: новые площадки, улучшения фильтра, исправления.

## Перед началом

```bash
git clone <ваш форк>
cd autolid
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Для проверки фильтра и ИИ без запуска всей программы:

```bash
.venv/bin/python main.py test
.venv/bin/python main.py check "текст сообщения"
```

## Pull request

- Одна ветка - одно изменение.
- Если добавляете настройку, допишите её с пояснением в `config.example.yaml`
  (программа берёт оттуда значения по умолчанию).
- Новые ключевые слова - в `keywords.yaml`, синтаксис описан в начале файла.
- Проверьте, что `python main.py test` отрабатывает без ошибок.

## Чего не коммитить

`config.yaml`, папку `data/` (там сессия Telegram и база) и свои списки чатов
из `chats/` - они уже в `.gitignore`, но проверьте `git status` перед коммитом.
Токены и ключи в issue тоже не вставляйте.

## Новая площадка

Посмотрите, как устроены `tg.py` и `vk.py`: воркер приводит сообщения к `models.Message`,
отдаёт их в `app.pipeline.prefilter` / `submit` и умеет `send_reply` и `show_typing`.
Дальше подключите его в `app.py` и `responder.py`.
