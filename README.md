# TG username collector

Бот-пульт для выкачивания юзернеймов авторов из группы/темы.

## Переменные окружения (Render → Environment)
- `API_ID`, `API_HASH` — с https://my.telegram.org
- `BOT_TOKEN` — от @BotFather
- `OWNER_ID` — твой numeric id (узнать у @userinfobot)
- `PHONE` — необязательно, номер для входа
- `SESSION_STRING` — после первого входа берётся из логов (строка `SESSION_STRING=...`)
- `DB_PATH` — на диске, например `/data/data.db`

## Деплой на Render
1. Push в GitHub.
2. New → Web Service, Build: `pip install -r requirements.txt`, Start: `python bot.py`.
3. Health check path: `/`.
4. Free-инстанс засыпает — для постоянного сбора нужен платный, либо внешний пинг `/health`.
5. Для сохранения сессии и базы прикрепи Persistent Disk и укажи `DB_PATH=/data/data.db`, `SESSION_FILE=/data/session.txt`.

## Замечания
- Аккаунт должен состоять в приватной группе, иначе доступ к истории не будет.
- Сбор идёт от твоего аккаунта — возможны флуд-лимиты, бот их пережидает.
- Дедуп по нику (регистронезависимый), боты и сам аккаунт пропускаются.
