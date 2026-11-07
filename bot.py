import asyncio
import html
import logging
import math
import os
import time
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

PHOTO_PATH = Path(__file__).parent / "brtools.png"
CAPTION_LIMIT = 1000  # лимит подписи к фото 1024, берём с запасом
TEXT_LIMIT = 4000  # лимит текстового сообщения 4096, берём с запасом
PAGE_SIZE = 15  # серверов на странице при выборе избранного

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_photo_file_id: str | None = None
_favs: dict[int, set[int]] = {}  # избранное в памяти (сбрасывается при перезапуске)


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


def render_list(title: str, lines: list[str], footer: str, limit: int) -> str:
    """Список в раскрывающейся цитате. Режет с конца, только если не влезает в limit."""
    if not lines:
        lines = ["—"]
    shown = len(lines)
    while True:
        body = "\n".join(lines[:shown])
        if shown < len(lines):
            body += f"\n…и ещё {len(lines) - shown}"
        if len(title) + len(body) + len(footer) + 2 <= limit or shown == 0:
            break
        shown -= 1
    return (
        f"<b>{html.escape(title)}</b>\n"
        f"<blockquote expandable>{html.escape(body)}</blockquote>\n"
        f"<b>{html.escape(footer)}</b>"
    )


# ---------- кнопки ----------

NAV = [
    ("online", "☰ Все серверы"),
    ("top", "▲ Топ"),
    ("fav", "★ Избранное"),
]


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def screen_kb(current: str, extra=None, refresh: bool = True) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    items = [btn(label, f"nav:{key}") for key, label in NAV if key != current]
    if current == "menu":
        items.append(btn("? Помощь", "nav:help"))
    rows += [items[i:i + 2] for i in range(0, len(items), 2)]
    bottom = []
    if current != "menu":
        bottom.append(btn("← Назад", "nav:menu"))
    if refresh:
        bottom.append(btn("↻ Обновить", f"rf:{current}"))
    if bottom:
        rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


MENU_TEXT = "<b>Главное меню</b>\nВыбери раздел:"
HELP_TEXT = (
    "<b>Помощь</b>\n"
    "/menu — главное меню\n"
    "/online — онлайн всех серверов (или напиши «онлайн»)\n"
    "/top — топ серверов по онлайну\n"
    "/fav — избранные серверы\n"
    "/help — эта справка\n\n"
    f"Данные обновляются примерно раз в {CACHE_TTL} сек."
)
ERROR_TEXT = "Не удалось получить онлайн, попробуй позже."


# ---------- отправка и редактирование ----------


async def send_screen(m: Message, text: str, markup: InlineKeyboardMarkup | None, photo: bool):
    """Одно сообщение: либо фото с подписью, либо обычный текст."""
    global _photo_file_id
    if photo:
        try:
            src = _photo_file_id or FSInputFile(PHOTO_PATH)
            sent = await m.answer_photo(src, caption=text, reply_markup=markup)
            if not _photo_file_id and sent.photo:
                _photo_file_id = sent.photo[-1].file_id  # дальше шлём по file_id
            return
        except Exception:
            _photo_file_id = None
            logging.exception(
                "answer_photo failed (path=%s, exists=%s)", PHOTO_PATH, PHOTO_PATH.exists()
            )
    await m.answer(text, reply_markup=markup)  # запасной вариант, чтобы бот не молчал


async def render(event, text: str, markup: InlineKeyboardMarkup | None, photo: bool):
    if not isinstance(event, CallbackQuery):
        await send_screen(event, text, markup, photo)
        return

    toast = None
    msg = event.message
    if isinstance(msg, Message):
        is_photo = bool(msg.photo)
        try:
            if is_photo == photo:
                # тот же тип сообщения — правим на месте
                if photo:
                    await msg.edit_caption(caption=text, reply_markup=markup)
                else:
                    await msg.edit_text(text, reply_markup=markup)
            else:
                # фото <-> текст редактированием не превратить: удаляем и шлём новое
                try:
                    await msg.delete()
                except Exception:
                    logging.exception("delete failed")
                await send_screen(msg, text, markup, photo)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                toast = "Ничего не изменилось"
            else:
                logging.exception("edit failed")
                await send_screen(msg, text, markup, photo)
    await event.answer(toast)


def uid_of(event) -> int:
    return event.from_user.id if event.from_user else 0


async def build_screen(screen: str, uid: int, force: bool = False):
    """Возвращает (текст, клавиатура, с_фото)."""
    if screen == "menu":
        return MENU_TEXT, screen_kb("menu", refresh=False), True
    if screen == "help":
        return HELP_TEXT, screen_kb("help", refresh=False), True

    if screen not in ("top", "fav"):
        screen = "online"
    photo = screen != "online"  # у /online фото нет — иначе не влезут все серверы

    try:
        servers = await fetch_servers(force)
    except Exception:
        logging.exception("fetch failed")
        return ERROR_TEXT, screen_kb(screen), photo

    total = total_online(servers)
    limit = CAPTION_LIMIT if photo else TEXT_LIMIT

    if screen == "top":
        best = sorted(servers, key=lambda s: num(s.get("online")), reverse=True)[:10]
        lines = [fmt_line(i, s) for i, s in enumerate(best, 1)]
        text = render_list("Топ по онлайну:", lines, f"Общий онлайн: {total}", limit)
        return text, screen_kb("top"), photo

    if screen == "fav":
        by_id = {num(s.get("id")): s for s in servers if s.get("id") is not None}
        mine = [by_id[i] for i in sorted(_favs.get(uid, set())) if i in by_id]
        edit_row = [[btn("✎ Изменить", "fe:0")]]
        if not mine:
            text = "<b>Избранное пусто.</b>\nНажми «Изменить», чтобы выбрать серверы."
        else:
            lines = [fmt_line(i, s) for i, s in enumerate(mine, 1)]
            fav_total = sum(num(s.get("online")) for s in mine)
            text = render_list("Избранные серверы:", lines, f"Онлайн избранных: {fav_total}", limit)
        return text, screen_kb("fav", extra=edit_row), photo

    # online — все серверы, обычным текстом
    lines = [fmt_line(i, s) for i, s in enumerate(servers, 1)]
    text = render_list("Онлайн серверов:", lines, f"Общий онлайн: {total}", limit)
    return text, screen_kb("online"), photo


async def show_screen(event, screen: str, force: bool = False):
    text, markup, photo = await build_screen(screen, uid_of(event), force)
    await render(event, text, markup, photo)


async def show_fav_edit(c: CallbackQuery, page: int):
    try:
        servers = await fetch_servers()
    except Exception:
        logging.exception("fetch failed")
        await c.answer("Не удалось загрузить список серверов", show_alert=True)
        return
    servers = [s for s in servers if s.get("id") is not None]
    favs = _favs.get(uid_of(c), set())
    pages = max(1, math.ceil(len(servers) / PAGE_SIZE))
    page = min(max(page, 0), pages - 1)
    chunk = servers[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    buttons = [
        btn(("★ " if num(s["id"]) in favs else "") + str(s.get("name", "?")),
            f"ft:{num(s['id'])}:{page}")
        for s in chunk
    ]
    rows = [buttons[i:i + 3] for i in range(0, len(buttons), 3)]
    pager = []
    if page > 0:
        pager.append(btn("‹", f"fe:{page - 1}"))
    pager.append(btn(f"{page + 1}/{pages}", "noop"))
    if page < pages - 1:
        pager.append(btn("›", f"fe:{page + 1}"))
    rows.append(pager)
    rows.append([btn("← Назад", "nav:fav")])

    text = (
        "<b>Выбери серверы для избранного</b>\n"
        f"Страница {page + 1}/{pages}. Нажми на сервер, чтобы добавить или убрать ★."
    )
    await render(c, text, InlineKeyboardMarkup(inline_keyboard=rows), True)


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
    favs = _favs.setdefault(uid_of(c), set())
    favs.symmetric_difference_update({int(sid)})
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
    try:
        try:
            await set_commands()
        except Exception:
            logging.exception("set_my_commands failed")
        await dp.start_polling(bot)
    finally:
        await _session.close()


if __name__ == "__main__":
    asyncio.run(main())
