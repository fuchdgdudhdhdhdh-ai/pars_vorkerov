import asyncio
import io
import os
import re
import sqlite3
import time
import logging
from datetime import datetime, timezone

from aiohttp import web
from telethon import Button, TelegramClient, events
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    InviteHashExpiredError,
    InviteHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    UserAlreadyParticipantError,
)
from telethon.sessions import StringSession
from telethon.tl.functions.messages import (
    CheckChatInviteRequest,
    ImportChatInviteRequest,
)
from telethon.tl.types import User

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("collector")

# ---------------------------------------------------------------- конфиг
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
PHONE = os.getenv("PHONE", "").strip()

PORT = int(os.getenv("PORT", "8080"))
SESSION_FILE = os.getenv("SESSION_FILE", "session.txt")
DB_PATH = os.getenv("DB_PATH", "data.db")
PAGE_SIZE = 15
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")

# ---------------------------------------------------------------- клиенты
def _load_session():
    s = os.getenv("SESSION_STRING", "").strip()
    if s:
        return StringSession(s)
    if os.path.exists(SESSION_FILE):
        try:
            raw = open(SESSION_FILE).read().strip()
            if raw:
                return StringSession(raw)
        except Exception:
            pass
    return StringSession()


user_client = TelegramClient(_load_session(), API_ID, API_HASH)
bot = TelegramClient(StringSession(), API_ID, API_HASH)  # сессия бота не нужна на диск

state = {"step": "idle", "phone": "", "code": "", "code_hash": None}
job = {"running": False, "stop": False, "scanned": 0, "new": 0, "seen": 0,
       "label": "", "started": 0.0}
view = {"items": [], "page": 0}

conn = None


# ---------------------------------------------------------------- БД
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def init_db():
    global conn
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS collected(
        username TEXT PRIMARY KEY,
        user_id  INTEGER,
        source   TEXT,
        added_at TEXT
    )""")
    conn.commit()


def add_user(username, user_id, source):
    cur = conn.execute(
        "INSERT OR IGNORE INTO collected(username,user_id,source,added_at) VALUES(?,?,?,?)",
        (username.lower(), user_id, source, now_iso()),
    )
    conn.commit()
    return cur.rowcount > 0


def all_usernames():
    return [r[0] for r in conn.execute("SELECT username FROM collected ORDER BY added_at DESC")]


def total_count():
    return conn.execute("SELECT COUNT(*) FROM collected").fetchone()[0]


# ---------------------------------------------------------------- клавиатуры
def code_keyboard():
    rows = []
    for trio in ("123", "456", "789"):
        rows.append([Button.inline(d, f"d:{d}".encode()) for d in trio])
    rows.append([
        Button.inline("⌫", b"d:back"),
        Button.inline("0", b"d:0"),
        Button.inline("✅", b"d:ok"),
    ])
    rows.append([Button.inline("🔁 Отправить код заново", b"d:resend")])
    return rows


def main_menu():
    return [
        [Button.inline("➕ Добавить источник", b"src")],
        [Button.inline("📄 Скачать базу юзернеймов", b"dump")],
        [Button.inline("📥 Проверить файл юзернеймов", b"check")],
        [Button.inline("📊 Статус", b"status"), Button.inline("⏹ Стоп", b"stop")],
    ]


def src_menu():
    return [
        [Button.inline("🔗 Ссылка на группу", b"src:link")],
        [Button.inline("🆔 ID группы", b"src:id")],
        [Button.inline("🧵 Ссылка на тему", b"src:topic")],
        [Button.inline("⬅️ Назад", b"menu")],
    ]


def view_keyboard(page, pages):
    nav = [
        Button.inline("◀️", f"p:{max(0, page - 1)}".encode()),
        Button.inline(f"{page + 1}/{pages}", b"p:noop"),
        Button.inline("▶️", f"p:{min(pages - 1, page + 1)}".encode()),
    ]
    return [nav, [Button.inline("✖️ Закрыть", b"p:close")]]


# ---------------------------------------------------------------- сессия
def save_session():
    s = user_client.session.save()
    try:
        with open(SESSION_FILE, "w") as f:
            f.write(s)
    except Exception as e:
        log.warning("не сохранил файл сессии: %s", e)
    log.info("SESSION_STRING=%s", s)


async def authorized():
    try:
        return user_client.is_connected() and await user_client.is_user_authorized()
    except Exception:
        return False


async def ask_login(chat_id):
    state.update(step="await_phone", phone=PHONE, code="", code_hash=None)
    if PHONE:
        await send_code(chat_id)
    else:
        await bot.send_message(chat_id, "Введи номер телефона в формате `+79991234567`",
                               parse_mode="md")


async def send_code(chat_id):
    try:
        sent = await user_client.send_code_request(state["phone"])
    except FloodWaitError as e:
        await bot.send_message(chat_id, f"Флуд-лимит, подожди {e.seconds} сек и жми /start")
        return
    except Exception as e:
        await bot.send_message(chat_id, f"Не смог отправить код: {e}")
        state["step"] = "await_phone"
        return
    state["code_hash"] = sent.phone_code_hash
    state["code"] = ""
    state["step"] = "await_code"
    await bot.send_message(
        chat_id,
        f"Код отправлен в Telegram на {state['phone']}.\nНабери его кнопками ниже:",
        buttons=code_keyboard(),
    )


# ---------------------------------------------------------------- меню
async def show_menu(chat_id, edit=None):
    text = (f"Главное меню\n\nЗаписей в базе: {total_count()}\n"
            f"Аккаунт: {'подключён' if await authorized() else 'не подключён'}")
    if edit is not None:
        await edit.edit(text, buttons=main_menu())
    else:
        await bot.send_message(chat_id, text, buttons=main_menu())


# ---------------------------------------------------------------- резолв источника
async def join_by_hash(h):
    try:
        res = await user_client(CheckChatInviteRequest(h))
        if getattr(res, "chat", None) is not None:
            return res.chat
    except (InviteHashExpiredError, InviteHashInvalidError):
        raise ValueError("ссылка-приглашение протухла или неверна")
    try:
        upd = await user_client(ImportChatInviteRequest(h))
        return upd.chats[0]
    except UserAlreadyParticipantError:
        res = await user_client(CheckChatInviteRequest(h))
        return res.chat


async def resolve_source(text):
    text = text.strip()
    topic = None

    m = re.search(r"t\.me/c/(\d+)(?:/(\d+))?", text)
    if m:
        entity = await user_client.get_entity(int("-100" + m.group(1)))
        if m.group(2):
            topic = int(m.group(2))
        return entity, topic

    m = re.search(r"(?:t\.me/|telegram\.me/)\+([\w-]+)|joinchat/([\w-]+)", text)
    if m:
        return await join_by_hash(m.group(1) or m.group(2)), None

    m = re.search(r"(?:t\.me/|telegram\.me/)([A-Za-z0-9_]{4,32})(?:/(\d+))?", text)
    if m:
        entity = await user_client.get_entity(m.group(1))
        if m.group(2):
            topic = int(m.group(2))
        return entity, topic

    if text.startswith("@"):
        return await user_client.get_entity(text), None

    if re.fullmatch(r"-?\d+", text):
        return await user_client.get_entity(int(text)), None

    raise ValueError("не распознал ссылку/ID")


# ---------------------------------------------------------------- сбор
def fmt(n):
    return f"{n:,}".replace(",", " ")


async def do_scrape(chat_id, entity, topic_id, label):
    job.update(running=True, stop=False, scanned=0, new=0, seen=0,
               label=label, started=time.time())
    msg = await bot.send_message(
        chat_id, "⏳ Запускаю сбор…", buttons=[[Button.inline("⏹ Остановить", b"stop")]]
    )

    me_id = None
    try:
        me_id = (await user_client.get_me()).id
    except Exception:
        pass

    last_edit = 0.0

    async def render(force=False):
        nonlocal last_edit
        if not force and time.time() - last_edit < 2.0:
            return
        last_edit = time.time()
        el = int(time.time() - job["started"])
        speed = job["scanned"] / el if el > 0 else 0
        txt = (f"⏳ Идёт сбор\n\nИсточник: {label}\n"
               f"Просканировано сообщений: {fmt(job['scanned'])}\n"
               f"Уникальных авторов: {fmt(job['seen'])}\n"
               f"Новых записей: {fmt(job['new'])}\n"
               f"Время: {el} с · {speed:.0f} сообщ/с")
        try:
            await msg.edit(txt, buttons=[[Button.inline("⏹ Остановить", b"stop")]])
        except Exception:
            pass

    senders = set()
    try:
        if topic_id:
            try:
                stream = user_client.iter_messages(entity, reply_to=topic_id, wait_time=0)
                async for m in stream:
                    if job["stop"]:
                        break
                    job["scanned"] += 1
                    if m.sender_id and m.sender_id != me_id:
                        senders.add(m.sender_id)
                    await render()
            except TypeError:
                async for m in user_client.iter_messages(entity, wait_time=0):
                    if job["stop"]:
                        break
                    rt = getattr(m, "reply_to", None)
                    tid = getattr(rt, "reply_to_top_id", None) or getattr(rt, "reply_to_msg_id", None)
                    if tid != topic_id:
                        continue
                    job["scanned"] += 1
                    if m.sender_id and m.sender_id != me_id:
                        senders.add(m.sender_id)
                    await render()
        else:
            async for m in user_client.iter_messages(entity, wait_time=0):
                if job["stop"]:
                    break
                job["scanned"] += 1
                if m.sender_id and m.sender_id != me_id:
                    senders.add(m.sender_id)
                await render()
    except FloodWaitError as e:
        await bot.send_message(chat_id, f"Флуд-лимит {e.seconds} сек — жду и продолжаю")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        await bot.send_message(chat_id, f"Ошибка обхода: {e}")

    await render(force=True)
    job["seen"] = len(senders)

    # вытаскиваем юзернеймы
    resolved = 0
    for uid in senders:
        if job["stop"]:
            break
        try:
            ent = await user_client.get_entity(uid)
        except Exception:
            continue
        if isinstance(ent, User) and not ent.bot and ent.username:
            if add_user(ent.username, ent.id, label):
                job["new"] += 1
        resolved += 1
        if resolved % 25 == 0:
            await render(force=True)

    job["running"] = False
    await render(force=True)
    summary = (f"{'⏹ Остановлено' if job['stop'] else '✅ Готово'}\n\n"
               f"Источник: {label}\nПросканировано: {fmt(job['scanned'])}\n"
               f"Найдено авторов: {fmt(job['seen'])}\nНовых в базе: {fmt(job['new'])}\n"
               f"Всего в базе: {fmt(total_count())}")
    await msg.edit(summary, buttons=[[Button.inline("⬅️ В меню", b"menu")]])


# ---------------------------------------------------------------- парсинг файла
def parse_usernames(raw: str):
    out, seen = [], set()
    for chunk in re.split(r"[\s,;]+", raw):
        u = chunk.strip().lstrip("@")
        u = re.sub(r"^(?:https?://)?(?:t\.me/|telegram\.me/)", "", u, flags=re.I)
        if USERNAME_RE.match(u):
            k = u.lower()
            if k not in seen:
                seen.add(k)
                out.append(u)
    return out


async def show_page(chat_id, page, edit=None):
    items, pages = view["items"], max(1, (len(view["items"]) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(pages - 1, page))
    view["page"] = page
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    body = "\n".join(f"{page * PAGE_SIZE + i + 1}. @{u}" for i, u in enumerate(chunk))
    text = f"Файл: {len(items)} юзернеймов\n\n{body}"
    if edit is not None:
        await edit.edit(text, buttons=view_keyboard(page, pages))
    else:
        await bot.send_message(chat_id, text, buttons=view_keyboard(page, pages))


# ---------------------------------------------------------------- хендлеры
@bot.on(events.NewMessage(pattern=r"^/start$"))
async def on_start(event):
    if event.sender_id != OWNER_ID:
        await event.respond("Доступ только для владельца.")
        return
    if await authorized():
        state["step"] = "idle"
        await show_menu(event.chat_id)
    else:
        await ask_login(event.chat_id)


@bot.on(events.CallbackQuery())
async def on_cb(event):
    if event.sender_id != OWNER_ID:
        return
    data = event.data.decode()

    if data.startswith("d:"):
        action = data[2:]
        if action == "resend":
            await send_code(event.chat_id)
            return
        if action == "back":
            state["code"] = state["code"][:-1]
        elif action == "ok":
            if not state["code"]:
                await event.answer("Пусто")
                return
            try:
                await user_client.sign_in(
                    phone=state["phone"], code=state["code"],
                    phone_code_hash=state["code_hash"],
                )
            except SessionPasswordNeededError:
                state["step"] = "await_password"
                await event.edit("Включена 2FA. Отправь пароль сообщением.", buttons=None)
                return
            except PhoneCodeInvalidError:
                await event.answer("Неверный код", alert=True)
                return
            except PhoneCodeExpiredError:
                await event.answer("Код истёк, запроси новый", alert=True)
                state["step"] = "await_code"
                return
            except Exception as e:
                await event.answer(f"Ошибка: {e}", alert=True)
                return
            save_session()
            state["step"] = "idle"
            await event.edit("✅ Аккаунт подключён.")
            await show_menu(event.chat_id)
            return
        else:
            if len(state["code"]) < 8:
                state["code"] += action
        await event.edit(f"Введено: `{state['code'] or '—'}`", parse_mode="md",
                         buttons=code_keyboard())
        return

    if data == "menu":
        state["step"] = "idle"
        await show_menu(event.chat_id, edit=event)
        return

    if data == "src":
        await event.edit("Что добавляем?", buttons=src_menu())
        return

    if data in ("src:link", "src:id", "src:topic"):
        state["step"] = data
        hints = {
            "src:link": "Пришли ссылку на группу: `t.me/name` или `t.me/+invite`",
            "src:id": "Пришли ID группы, например `-1001234567890`",
            "src:topic": "Пришли ссылку на тему: `t.me/name/123` или `t.me/c/1234567890/123`",
        }
        await event.edit(hints[data], parse_mode="md", buttons=[[Button.inline("⬅️ Назад", b"src")]])
        return

    if data == "dump":
        names = all_usernames()
        if not names:
            await event.answer("База пуста", alert=True)
            return
        buf = io.BytesIO("\n".join(names).encode())
        buf.name = f"usernames_{int(time.time())}.txt"
        await bot.send_file(event.chat_id, buf,
                            caption=f"Всего: {len(names)} юзернеймов")
        await event.answer("Отправил файл")
        return

    if data == "check":
        state["step"] = "await_file"
        await event.edit("Пришли .txt файл с юзернеймами (по одному в строке).",
                         buttons=[[Button.inline("⬅️ Назад", b"menu")]])
        return

    if data == "status":
        if job["running"]:
            el = int(time.time() - job["started"])
            await event.answer(
                f"Сканирую {job['label']}: {job['scanned']} сообщ., {job['seen']} авторов, {el} с",
                alert=True)
        else:
            await event.answer(f"Сбор не идёт. В базе {total_count()} записей", alert=True)
        return

    if data == "stop":
        job["stop"] = True
        await event.answer("Останавливаю…")
        return

    if data.startswith("p:"):
        if data == "p:noop":
            await event.answer()
        elif data == "p:close":
            await event.edit("Закрыто.")
        else:
            await show_page(event.chat_id, int(data[2:]), edit=event)
        return


@bot.on(events.NewMessage())
async def on_text(event):
    if event.sender_id != OWNER_ID or not event.raw_text:
        return
    text = event.raw_text.strip()
    step = state["step"]

    if step == "await_phone":
        state["phone"] = text
        await send_code(event.chat_id)
        return

    if step == "await_password":
        try:
            await user_client.sign_in(password=text)
        except Exception as e:
            await event.respond(f"Пароль не подошёл: {e}")
            return
        save_session()
        state["step"] = "idle"
        await event.respond("✅ Аккаунт подключён.")
        await show_menu(event.chat_id)
        return

    if step in ("src:link", "src:id", "src:topic"):
        if not await authorized():
            await event.respond("Сначала подключи аккаунт: /start")
            return
        if job["running"]:
            await event.respond("Сбор уже идёт, сначала останови его.")
            return
        try:
            entity, topic = await resolve_source(text)
        except Exception as e:
            await event.respond(f"Не смог открыть источник: {e}")
            return
        label = f"{text} · тема {topic}" if topic else text
        state["step"] = "idle"
        await do_scrape(event.chat_id, entity, topic, label)
        return


@bot.on(events.NewMessage(func=lambda e: e.document is not None))
async def on_file(event):
    if event.sender_id != OWNER_ID:
        return
    if state["step"] != "await_file":
        return
    raw = await event.download_media(file=bytes)
    try:
        text = raw.decode("utf-8", errors="ignore")
    except Exception:
        await event.respond("Не смог прочитать файл.")
        return
    names = parse_usernames(text)
    if not names:
        await event.respond("Юзернеймов не нашёл.")
        return
    view["items"] = names
    state["step"] = "idle"
    await show_page(event.chat_id, 0)


# ---------------------------------------------------------------- веб-сервер для Render
async def health(request):
    return web.Response(text="ok")


async def index(request):
    return web.Response(
        text=f"collector alive · base={total_count()} · running={job['running']}"
    )


async def start_web():
    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("web on %s", PORT)


# ---------------------------------------------------------------- main
async def main():
    init_db()
    await bot.start(bot_token=BOT_TOKEN)
    await user_client.connect()
    try:
        if not await user_client.is_user_authorized():
            log.info("юзер-клиент не авторизован — нужен /start")
    except AuthKeyUnregisteredError:
        log.warning("сессия недействительна — нужен повторный вход")

    await start_web()
    me = await bot.get_me()
    log.info("bot @%s ready", me.username)
    await bot.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())