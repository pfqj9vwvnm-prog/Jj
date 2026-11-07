import asyncio
import html
import logging
import math
import os
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "https://blackrussia.online/api/gameservers/")
CACHE_TTL = int(os.getenv("CACHE_TTL", "15"))  # секунд
HISTORY_INTERVAL = int(os.getenv("HISTORY_INTERVAL", "300"))  # как часто писать историю
DB_PATH = os.getenv("DB_PATH", "bot.db")

PHOTO_PATH = Path(__file__).parent / "brtools.png"
CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото
PAGE_SIZE = 15  # серверов на странице при выборе избранного
MSK = timezone(timedelta(hours=3))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_photo_file_id: str | None = None

# ---------- база (избранное и история) ----------

db = sqlite3.connect(DB_PATH)
db.execute(
    "CREATE TABLE IF NOT EXISTS favs ("
    "user_id INTEGER, server_id INTEGER, PRIMARY KEY (user_id, server_id))"
)
db.execute("CREATE TABLE IF NOT EXISTS history (ts INTEGER, total INTEGER)")
db.commit()


def get_favs(uid: int) -> set[int]:
    rows = db.execute("SELECT server_id FROM favs WHERE user_id=?", (uid,))
    return {r[0] for r in rows}


def toggle_fav(uid: int, sid: int) -> None:
    if sid in get_favs(uid):
        db.execute("DELETE FROM favs WHERE user_id=? AND server_id=?", (uid, sid))
    else:
        db.execute("INSERT OR IGNORE INTO favs VALUES (?, ?)", (uid, sid))
    db.commit()


# ---------- данные ----------


def num(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


async def fetch_servers(force: bool = False) -> list[dict]:
    max_age = 3 if force else CACHE_TTL
    async with _lock:
        if _cache["data"] and time.monotonic() - _cache["ts"] < max_age:
            return _cache["data"]
        try:
            async with _session.get(API_URL) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
            if not isinstance(data, list):
                raise ValueError("unexpected API response")
        except Exception:
            if _cache["data"]:  # сайт моргнул — отдаём последние данные
                logging.exception("fetch failed, using stale cache")
                return _cache["data"]
            raise
        data = [
            s for s in data
            if isinstance(s, dict) and s.get("publish_status", "published") == "published"
        ]
        data.sort(key=lambda s: num(s.get("id")))
        _cache.update(ts=time.monotonic(), data=data)
        return data


def total_online(servers: list[dict]) -> int:
    return sum(num(s.get("online")) for s in servers)


def fmt_line(i: int, s: dict) -> str:
    name = str(s.get("name", "?"))
    x = " x2" if s.get("x2") else ""
    return f"{i}. {name}: {num(s.get('online'))}/{num(s.get('max_online'))}{x}"


def render_list(title: str, lines: list[str], footer: str) -> str:
    """Список в раскрывающейся цитате. Если не влезает в подпись — обрезаем с конца."""
    if not lines:
        lines = ["—"]
    shown = len(lines)
    while True:
        body = "\n".join(lines[:shown])
        if shown < len(lines):
            body += f"\n…и ещё {len(lines) - shown}"
        if len(title) + len(body) + len(footer) + 2 <= CAPTION_LIMIT or shown == 0:
            break
        shown -= 1
    return (
        f"<b>{html.escape(title)}</b>\n"
        f"<blockquote expandable>{html.escape(body)}</blockquote>\n"
        f"<b>{html.escape(footer)}</b>"
    )


def fmt_time(ts: int) -> str:
    return datetime.fromtimestamp(ts, MSK).strftime("%H:%M")


def build_stats(servers: list[dict]) -> str:
    total = total_online(servers)
    cnt = len(servers)
    avg = total // cnt if cnt else 0
    x2 = sum(1 for s in servers if s.get("x2"))
    best = max(servers, key=lambda s: num(s.get("online")), default=None)

    lines = [
        "<b>Статистика</b>",
        f"Серверов: {cnt}",
        f"Общий онлайн: {total}",
        f"Среднее на сервер: {avg}",
        f"Серверов с x2: {x2}",
    ]
    if best:
        lines.append(
            f"Самый загруженный: {html.escape(str(best.get('name', '?')))} "
            f"({num(best.get('online'))})"
        )

    rows = db.execute(
        "SELECT ts, total FROM history WHERE ts >= ? ORDER BY ts",
        (int(time.time()) - 86400,),
    ).fetchall()
    lines += ["", "<b>За 24 часа</b>"]
    if len(rows) >= 2:
        peak = max(rows, key=lambda r: r[1])
        low = min(rows, key=lambda r: r[1])
        mean = sum(r[1] for r in rows) // len(rows)
        lines += [
            f"Пик: {peak[1]} ({fmt_time(peak[0])} МСК)",
            f"Минимум: {low[1]} ({fmt_time(low[0])} МСК)",
            f"Среднее: {mean}",
        ]
    else:
        lines.append("Данные ещё копятся, загляни позже.")
    return "\n".join(lines)


async def history_loop():
    while True:
        try:
            servers = await fetch_servers()
            now = int(time.time())
            db.execute("INSERT INTO history VALUES (?, ?)", (now, total_online(servers)))
            db.execute("DELETE FROM history WHERE ts < ?", (now - 7 * 86400,))
            db.commit()
        except Exception:
            logging.exception("history failed")
        await asyncio.sleep(HISTORY_INTERVAL)


# ---------- кнопки ----------

NAV = [
    ("online", "📊 Все серверы"),
    ("top", "🏆 Топ"),
    ("fav", "⭐ Избранное"),
    ("stats", "📈 Статистика"),
]


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def screen_kb(current: str, extra=None, refresh: bool = True) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    items = [btn(label, f"nav:{key}") for key, label in NAV if key != current]
    if current == "menu":
        items.append(btn("❓ Помощь", "nav:help"))
    else:
        items.append(btn("🏠 Меню", "nav:menu"))
    rows += [items[i:i + 2] for i in range(0, len(items), 2)]
    if refresh:
        rows.append([btn("🔄 Обновить", f"rf:{current}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_TEXT = "<b>Главное меню</b>\nВыбери раздел:"
HELP_TEXT = (
    "<b>Помощь</b>\n"
    "/menu — главное меню\n"
    "/online — онлайн всех серверов (или напиши «онлайн»)\n"
    "/top — топ серверов по онлайну\n"
    "/fav — избранные серверы\n"
    "/stats — статистика\n"
    "/help — эта справка\n\n"
    f"Данные обновляются примерно раз в {CACHE_TTL} сек."
)
ERROR_TEXT = "Не удалось получить онлайн, попробуй позже."


# ---------- отправка и редактирование (всегда одно сообщение с фото) ----------


async def send_photo(m: Message, caption: str, markup: InlineKeyboardMarkup | None = None):
    global _photo_file_id
    try:
        photo = _photo_file_id or FSInputFile(PHOTO_PATH)
        sent = await m.answer_photo(photo, caption=caption, reply_markup=markup)
        if not _photo_file_id and sent.photo:
            _photo_file_id = sent.photo[-1].file_id  # дальше шлём по file_id
    except Exception:
        _photo_file_id = None
        logging.exception(
            "answer_photo failed (path=%s, exists=%s)", PHOTO_PATH, PHOTO_PATH.exists()
        )
        await m.answer(caption, reply_markup=markup)  # запасной вариант, чтобы бот не молчал


async def render(event, text: str, markup: InlineKeyboardMarkup | None):
    if isinstance(event, CallbackQuery):
        toast = None
        msg = event.message
        if isinstance(msg, Message):
            try:
                if msg.photo:
                    await msg.edit_caption(caption=text, reply_markup=markup)
                else:
                    await msg.edit_text(text, reply_markup=markup)
            except TelegramBadRequest as e:
                if "not modified" in str(e):
                    toast = "Ничего не изменилось"
                else:
                    logging.exception("edit failed")
                    await send_photo(msg, text, markup)
        await event.answer(toast)
    else:
        await send_photo(event, text, markup)


def uid_of(event) -> int:
    return event.from_user.id if event.from_user else 0


async def build_screen(screen: str, uid: int, force: bool = False):
    if screen == "menu":
        return MENU_TEXT, screen_kb("menu", refresh=False)
    if screen == "help":
        return HELP_TEXT, screen_kb("help", refresh=False)

    if screen not in ("top", "fav", "stats"):
        screen = "online"

    try:
        servers = await fetch_servers(force)
    except Exception:
        logging.exception("fetch failed")
        return ERROR_TEXT, screen_kb(screen)

    total = total_online(servers)

    if screen == "top":
        best = sorted(servers, key=lambda s: num(s.get("online")), reverse=True)[:10]
        lines = [fmt_line(i, s) for i, s in enumerate(best, 1)]
        return render_list("Топ по онлайну:", lines, f"Общий онлайн: {total}"), screen_kb("top")

    if screen == "stats":
        return build_stats(servers), screen_kb("stats")

    if screen == "fav":
        by_id = {num(s.get("id")): s for s in servers if s.get("id") is not None}
        mine = [by_id[i] for i in sorted(get_favs(uid)) if i in by_id]
        edit_row = [[btn("✏️ Изменить", "fe:0")]]
        if not mine:
            text = "<b>Избранное пусто.</b>\nНажми «Изменить», чтобы выбрать серверы."
        else:
            lines = [fmt_line(i, s) for i, s in enumerate(mine, 1)]
            fav_total = sum(num(s.get("online")) for s in mine)
            text = render_list("Избранные серверы:", lines, f"Онлайн избранных: {fav_total}")
        return text, screen_kb("fav", extra=edit_row)

    # online — текст такой же, как был раньше
    lines = [fmt_line(i, s) for i, s in enumerate(servers, 1)]
    return render_list("Онлайн серверов:", lines, f"Общий онлайн: {total}"), screen_kb("online")


async def show_screen(event, screen: str, force: bool = False):
    text, markup = await build_screen(screen, uid_of(event), force)
    await render(event, text, markup)


async def show_fav_edit(c: CallbackQuery, page: int):
    try:
        servers = await fetch_servers()
    except Exception:
        logging.exception("fetch failed")
        await c.answer("Не удалось загрузить список серверов", show_alert=True)
        return
    servers = [s for s in servers if s.get("id") is not None]
    favs = get_favs(uid_of(c))
    pages = max(1, math.ceil(len(servers) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = servers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    buttons = [
        btn(("⭐ " if num(s["id"]) in favs else "") + str(s.get("name", "?")),
            f"ft:{num(s['id'])}:{page}")
        for s in chunk
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    pager = []
    if page > 0:
        pager.append(btn("◀️", f"fe:{page - 1}"))
    pager.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        pager.append(btn("▶️", f"fe:{page + 1}"))
    rows.append(pager)
    rows.append([btn("✅ Готово", "nav:fav")])

    text = (
        "<b>Выбери серверы для избранного</b>\n"
        f"Страница {page + 1}/{pages}. Нажми на сервер, чтобы добавить или убрать ⭐."
    )
    await render(c, text, InlineKeyboardMarkup(inline_keyboard=rows))


# ---------- команды ----------


@dp.message(CommandStart())
@dp.message(Command("online"))
@dp.message(F.text.func(lambda t: t.strip().lower() == "онлайн"))
async def cmd_online(m: Message):
    await show_screen(m, "online")


@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    await show_screen(m, "menu")


@dp.message(Command("top"))
async def cmd_top(m: Message):
    await show_screen(m, "top")


@dp.message(Command("fav"))
async def cmd_fav(m: Message):
    await show_screen(m, "fav")


@dp.message(Command("stats"))
async def cmd_stats(m: Message):
    await show_screen(m, "stats")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await show_screen(m, "help")


# ---------- инлайн-кнопки ----------


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(c: CallbackQuery):
    await show_screen(c, c.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("rf:"))
async def cb_refresh(c: CallbackQuery):
    await show_screen(c, c.data.split(":", 1)[1], force=True)


@dp.callback_query(F.data.startswith("fe:"))
async def cb_fav_edit(c: CallbackQuery):
    await show_fav_edit(c, num(c.data.split(":")[1]))


@dp.callback_query(F.data.startswith("ft:"))
async def cb_fav_toggle(c: CallbackQuery):
    _, sid, page = c.data.split(":")
    toggle_fav(uid_of(c), int(sid))
    await show_fav_edit(c, int(page))


@dp.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery):
    await c.answer()


# ---------- запуск ----------


async def set_commands():
    await bot.set_my_commands([
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="online", description="Все серверы"),
        BotCommand(command="top", description="Топ по онлайну"),
        BotCommand(command="fav", description="Избранное"),
        BotCommand(command="stats", description="Статистика"),
        BotCommand(command="help", description="Помощь"),
    ])


async def main():
    global _session
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    if not PHOTO_PATH.exists():
        logging.warning("brtools.png не найден: %s — ответы пойдут без фото", PHOTO_PATH)
    task = None
    try:
        try:
            await set_commands()
        except Exception:
            logging.exception("set_my_commands failed")
        task = asyncio.create_task(history_loop())
        await dp.start_polling(bot)
    finally:
        if task:
            task.cancel()
        await _session.close()


if __name__ == "__main__":
    asyncio.run(main())
