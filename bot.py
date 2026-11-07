import asyncio
import html
import itertools
import logging
import math
import os
import re
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
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
BIZ_PAGE_CHARS = 800  # символов списка бизнесов на одной странице (под подпись к фото)
MAX_ALERTS = 10  # уведомлений на один чат
ALERT_CHECK_EVERY = max(10, CACHE_TTL)  # как часто проверять уведомления, сек

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_photo_file_id: str | None = None
_favs: dict[int, set[int]] = {}  # избранное в памяти (сбрасывается при перезапуске)
_alerts: list[dict] = []  # уведомления в памяти (сбрасываются при перезапуске)
_alert_ids = itertools.count(1)


# ---------- бизнесы на карте ----------
# Формат строки: Название (номер) - место    или    АЗС #номер - место
# Чтобы добавить бизнес, просто допиши строку сюда.

BIZ_RAW = '''
Одежда (1) - г.Южный, около банка
Одежда (5) - пгт.Батырево, около военкомата
Одежда (9) - г.Арзамас, на перекрестке
Одежда (56) - г.Арзамас, около офиса СК
Одежда (67) - г.Лыткарино, напротив ФСБ
Одежда (72) - п.Рублевка
Одежда (120) - г.Нижегородск, около ГИБДД

24/7 (3) - г.Южный, около СМИ, ближе к телевышке
24/7 (6) - пгт.Батырево, около банка
24/7 (8) - г.Арзамас
24/7 (13) - г.Арзамас, около поста ГИБДД
24/7 (15) - пгт.Батырево, около военкомата
24/7 (16) - д.Гарель, АЗС
24/7 (18) - г.Южный, около банка
24/7 (20) - г.Южный, около УМВД
24/7 (21) - г.Южный, около СМИ, ближе к гаражам
24/7 (22) - д.Гарель, АЗС у низкого автосалона
24/7 (23) - г.Арзамас, около технического центра
24/7 (24) - г.Лыткарино, около ФСБ
24/7 (25) - д.Корякино, АЗС
24/7 (26) - пгт.Батырево, АЗС около кладмэна
24/7 (36) - п.Егоровка, АЗС
24/7 (39) - г.Южный, АЗС
24/7 (40) - АЗС над казино
24/7 (57) - г.Арзамас, около офиса СК
24/7 (58) - г.Арзамас, "Пятёрочка"
24/7 (63) - д.Бусаево
24/7 (64) - г.Лыткарино - АЗС
24/7 (66) - г.Лыткарино, около банка
24/7 (71) - п.Рублевка
24/7 (97) - г.Нижегородск, около стройки
24/7 (98) - г.Нижегородск, около "ТЦ РИО"
24/7 (99) - г.Нижегородск, около стадиона
24/7 (100) - г.Нижегородск, около ГИБДД
24/7 (101) - г.Нижегородск, около работы «электрик»
24/7 (105) - г.Нижегородск, около кремля
24/7 (106) - г.Нижегородск, около железнодорожного вокзала
24/7 (107) - г.Нижегородск, около банка
24/7 (108) - г.Нижегородск, около центральной развязки

Аксессуары (31) - г.Южный, около банка
Аксессуары (32) - г.Арзамас, на перекрестке
Аксессуары (69) - г.Лыткарино, около банка
Аксессуары (77) - пгт.Батырево, около Банка
Аксессуары (113) - г.Нижегородск, "ТЦ РИО"
Аксессуары (119) - г.Нижегородск, около центральной развязки

Закусочная (2) - г.Южный, около СМИ
Закусочная (4) - пгт.Батырево, около военкомата
Закусочная (7) - пгт.Батырево, около воинской части
Закусочная (11) - г.Арзамас
Закусочная (12) - г.Арзамас, на перекрестке
Закусочная (17) - г.Арзамас, старый вокзал
Закусочная (37) - п.Егоровка
Закусочная (68) - г.Лыткарино, около стройки
Закусочная (109) - г.Нижегородск, около больницы
Закусочная (114) - г.Нижегородск, "ТЦ РИО"

Ларек (42) - г.Арзамас, ближе к старому спавну
Ларек (43) - г.Арзамас, дальше от старого спавна
Ларек (44) - г.Арзамас, "ДМЗ Хлеб"
Ларек (45) - г.Арзамас, около офиса СК
Ларек (46) - г.Арзамас, около больницы
Ларек (47) - г.Лыткарино, вокзал
Ларек (48) - пгт.Батырево, вокзал
Ларек (49) - пгт.Батырево, около банка
Ларек (50) - г.Южный, вокзал
Ларек (52) - г.Южный, около банка
Ларек (53) - г.Южный, около ГИБДД, на другой стороне дороги
Ларек (54) - г.Южный, около ГИБДД
Ларек (55) - г.Арзамас, вокзал
Ларек (70) - г.Лыткарино, около Больницы
Ларек (74) - п.Рублевка
Ларек (110) - г.Нижегородск, морской вокзал

Амуниция (14) - г.Арзамас, на перекрестке
Амуниция (19) - г.Южный, около банка
Амуниция (65) - г.Лыткарино, около банка
Амуниция (73) - п.Рублевка
Амуниция (76) - пгт.Батырево
Амуниция (104) - г.Нижегородск, около центральной развязки
Амуниция (118) - г.Нижегородск, около "ТЦ РИО"

Клуб (62) - г.Арзамас, рядом с работой «Водолаз»

Рыболовный магазин (78) - г.Южный
Рыболовный магазин (79) - г.Арзамас
Рыболовный магазин (80) - пгт.Батырево
Рыболовный магазин (81) - г.Лыткарино

Мастерская (83) - г.Арзамас
Мастерская (84) - пгт.Батырево
Мастерская (85) - д.Бусаево
Мастерская (86) - г.Лыткарино
Мастерская (87) - г.Южный
Мастерская (103) - г.Нижегородск, около ГИБДД
Мастерская (116) - г.Нижегородск, около стадиона

Пункт выдачи заказа (88) - г.Арзамас, около gTAXI
Пункт выдачи заказа (89) - г.Лыткарино, около вокзала
Пункт выдачи заказа (90) - г.Арзамас, около "Пятерочка"
Пункт выдачи заказа (91) - г.Лыткарино, около стройки
Пункт выдачи заказа (92) - д.Бусаево
Пункт выдачи заказа (93) - пгт.Батырево
Пункт выдачи заказа (94) - г.Южный, около УМВД
Пункт выдачи заказа (95) - г.Южный, около СМИ
Пункт выдачи заказа (96) - п.Рублевка
Пункт выдачи заказа (102) - г.Нижегородск, около ГИБДД
Пункт выдачи заказа (112) - г.Нижегородск, "ТЦ РИО"

Автосалон низкого класса (0) - г.Арзамас
Автосалон среднего класса (27) - г.Арзамас, как ехать в г.Южный
Автосалон высокого класса (28) - г.Арзамас, как ехать в пгт.Батырево
Салон грузовых автомобилей (38) - около завода
Мотосалон (41) - недалеко от п.Егоровка
Мотосалон (51) - недалеко от п.Егоровка
Лодочный салон (75) - г.Южный

Стайлинг центр (33) - г.Южный
Шиномонтажный центр (34) - пгт.Батырево
Технический центр (35) - г.Арзамас

Казино (30)

АЗС #0 - г.Южный
АЗС #1 - Бывший автосалон низкого класса
АЗС #2 - Охота
АЗС #3 - Автосалон низкого класса
АЗС #4 - Кладмэн
АЗС #5 - Егоровка
АЗС #6 - г.Лыткарино
АЗС #7 - над Казино
АЗС #8 - Водная
АЗС #9 - г.Нижегородск
'''

# (название раздела, с чего начинается название бизнеса)
BIZ_CATEGORIES = [
    ("Одежда", ("Одежда",)),
    ("24/7", ("24/7",)),
    ("Аксессуары", ("Аксессуары",)),
    ("Закусочные", ("Закусочная",)),
    ("Ларьки", ("Ларек",)),
    ("Амуниция", ("Амуниция",)),
    ("Рыболовные магазины", ("Рыболовный магазин",)),
    ("Мастерские", ("Мастерская",)),
    ("Пункты выдачи", ("Пункт выдачи заказа",)),
    ("Салоны", ("Автосалон", "Салон", "Мотосалон", "Лодочный салон")),
    ("Прочее", ("Клуб", "Стайлинг", "Шиномонтажный", "Технический центр", "Казино")),
    ("АЗС", ("АЗС",)),
]

_BIZ_RE = re.compile(r"^(.+?)\s*(?:\((\d+)\)|#(\d+))\s*(?:-\s*(.*))?$")


def _load_biz():
    seen: dict[tuple[str, str], str] = {}
    for raw in BIZ_RAW.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = _BIZ_RE.match(line)
        if not m:
            logging.warning("business line skipped: %s", line)
            continue
        name, n1, n2, place = m.groups()
        tag = f"№{n1}" if n1 else f"#{n2}"
        key = (name.strip(), tag)
        if key not in seen:  # дубликаты отбрасываем
            seen[key] = (place or "").strip()

    cats, used = [], set()
    for label, prefixes in BIZ_CATEGORIES:
        items = []
        for (name, tag), place in seen.items():
            if (name, tag) not in used and name.startswith(prefixes):
                items.append((name, tag, place))
                used.add((name, tag))
        items.sort(key=lambda e: int(e[1][1:]))
        cats.append((label, items))
    for key in seen:
        if key not in used:
            logging.warning("business without category: %s", key)

    by_num: dict[int, list[tuple[str, str, str]]] = {}
    for _, items in cats:
        for name, tag, place in items:
            if tag.startswith("№"):
                by_num.setdefault(int(tag[1:]), []).append((name, tag, place))
    return cats, by_num


BIZ, BIZ_BY_NUM = _load_biz()


def biz_line(entry: tuple[str, str, str], mixed: bool) -> str:
    name, tag, place = entry
    head = f"{name} {tag}" if mixed else tag
    return f"{head} — {place}" if place else head


def paginate(lines: list[str], limit: int) -> list[list[str]]:
    pages, cur, size = [], [], 0
    for ln in lines:
        add = len(ln) + 1
        if cur and size + add > limit:
            pages.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += add
    if cur:
        pages.append(cur)
    return pages or [[]]


# ---------- данные серверов ----------


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


def server_key(s: dict):
    return s.get("id") if s.get("id") is not None else s.get("name")


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
    ("biz", "◈ Бизнес на карте"),
    ("msg", "◉ Уведомления"),
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
    "/business — бизнес на карте (или /business 57 — поиск по номеру)\n"
    "/message 90 -1300 — сообщить, когда онлайн сервера №90 станет меньше 1300 "
    "(+ вместо - — когда станет больше)\n"
    "/help — эта справка\n\n"
    f"Данные обновляются примерно раз в {CACHE_TTL} сек."
)
ERROR_TEXT = "Не удалось получить онлайн, попробуй позже."
BIZ_TEXT = (
    "<b>Бизнес на карте</b>\n"
    "Выбери тип бизнеса или отправь /business и номер, например /business 57."
)
ALERT_HELP = (
    "Сообщу один раз, когда онлайн сервера станет выше или ниже порога.\n"
    "/message 90 -1300 — онлайн сервера №90 станет меньше 1300\n"
    "/message 90 +1300 — онлайн сервера №90 станет больше 1300\n"
    "Номер сервера такой же, как в списке /online."
)


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


async def push(chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None):
    """Сообщение без входящего Message (для уведомлений): фото с подписью."""
    global _photo_file_id
    try:
        src = _photo_file_id or FSInputFile(PHOTO_PATH)
        sent = await bot.send_photo(chat_id, src, caption=text, reply_markup=markup)
        if not _photo_file_id and sent.photo:
            _photo_file_id = sent.photo[-1].file_id
        return
    except TelegramForbiddenError:
        raise
    except Exception:
        _photo_file_id = None
        logging.exception("send_photo failed (path=%s, exists=%s)", PHOTO_PATH, PHOTO_PATH.exists())
    await bot.send_message(chat_id, text, reply_markup=markup)


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


def chat_of(event) -> int:
    if isinstance(event, CallbackQuery):
        return event.message.chat.id if event.message else uid_of(event)
    return event.chat.id


def biz_kb() -> InlineKeyboardMarkup:
    btns = [btn(f"{label} ({len(items)})", f"bc:{i}:0") for i, (label, items) in enumerate(BIZ)]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([btn("← Назад", "nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def build_biz_cat(ci: int, page: int):
    label, items = BIZ[ci]
    mixed = len({e[0] for e in items}) > 1
    pages = paginate([biz_line(e, mixed) for e in items], BIZ_PAGE_CHARS)
    page = min(max(page, 0), len(pages) - 1)
    body = "\n".join(html.escape(ln) for ln in pages[page])
    text = f"<b>{html.escape(label)}</b> ({len(items)})\n{body}"

    rows = []
    if len(pages) > 1:
        text += f"\n\nСтраница {page + 1}/{len(pages)}"
        pager = []
        if page > 0:
            pager.append(btn("‹", f"bc:{ci}:{page - 1}"))
        pager.append(btn(f"{page + 1}/{len(pages)}", "noop"))
        if page < len(pages) - 1:
            pager.append(btn("›", f"bc:{ci}:{page + 1}"))
        rows.append(pager)
    rows.append([btn("← Назад", "nav:biz")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


async def show_biz_cat(event, ci: int, page: int):
    if not 0 <= ci < len(BIZ):
        ci = 0
    text, markup = build_biz_cat(ci, page)
    await render(event, text, markup, True)


# ---------- уведомления об онлайне ----------


def alert_hit(op: str, cur: int, val: int) -> bool:
    return cur < val if op == "-" else cur > val


def alert_label(a: dict) -> str:
    sign = "<" if a["op"] == "-" else ">"
    return f"✕ {a['name']} №{a['num']} {sign} {a['val']}"


def build_alerts(chat: int):
    mine = [a for a in _alerts if a["chat"] == chat]
    if mine:
        head = f"Активных: {len(mine)}/{MAX_ALERTS}. Нажми на уведомление, чтобы удалить."
    else:
        head = "Активных уведомлений нет."
    text = f"<b>Уведомления</b>\n{ALERT_HELP}\n\n{head}"
    rows = [[btn(alert_label(a), f"ad:{a['id']}")] for a in mine]
    return text, screen_kb("msg", extra=rows, refresh=False)


async def notify(a: dict, cur: int):
    word = "ниже" if a["op"] == "-" else "выше"
    text = (
        "<b>Уведомление</b>\n"
        f"{html.escape(a['name'])} (№{a['num']}): онлайн {cur}, это {word} {a['val']}."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("☰ Все серверы", "nav:online"), btn("◉ Уведомления", "nav:msg")],
    ])
    await push(a["chat"], text, kb)


async def alerts_loop():
    while True:
        await asyncio.sleep(ALERT_CHECK_EVERY)
        if not _alerts:
            continue
        try:
            servers = await fetch_servers(force=True)
        except Exception:
            logging.exception("alerts fetch failed")
            continue
        by_key = {server_key(s): s for s in servers}
        for a in list(_alerts):
            s = by_key.get(a["key"])
            if s is None:
                continue
            cur = num(s.get("online"))
            if not alert_hit(a["op"], cur, a["val"]):
                continue
            try:
                await notify(a, cur)
            except TelegramForbiddenError:
                # пользователь заблокировал бота — убираем все его уведомления
                _alerts[:] = [x for x in _alerts if x["chat"] != a["chat"]]
                continue
            except Exception:
                logging.exception("notify failed, will retry")
                continue
            if a in _alerts:
                _alerts.remove(a)


_ALERT_RE = re.compile(r"^№?(\d+)\s+([+\-−–<>])\s*(\d+)$")


@dp.message(Command("message"))
async def cmd_message(m: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        await show_screen(m, "msg")
        return

    kb = screen_kb("msg", refresh=False)
    mt = _ALERT_RE.match(args)
    if not mt:
        await send_screen(m, "Не понял. Пример: /message 90 -1300\n\n" + ALERT_HELP, kb, True)
        return

    n = int(mt.group(1))
    op = "-" if mt.group(2) in "-−–<" else "+"
    val = int(mt.group(3))
    word = "меньше" if op == "-" else "больше"

    try:
        servers = await fetch_servers()
    except Exception:
        logging.exception("fetch failed")
        await send_screen(m, "Не удалось получить список серверов, попробуй позже.", kb, True)
        return
    if not 1 <= n <= len(servers):
        await send_screen(m, f"Сервера №{n} нет. Всего серверов: {len(servers)}.", kb, True)
        return

    s = servers[n - 1]
    name = str(s.get("name", "?"))
    cur = num(s.get("online"))
    safe = html.escape(name)

    if alert_hit(op, cur, val):
        text = (
            f"Уже выполнено: на {safe} (№{n}) сейчас {cur}, это {word} {val}. "
            "Задай другой порог."
        )
        await send_screen(m, text, kb, True)
        return

    chat = chat_of(m)
    key = server_key(s)
    mine = [a for a in _alerts if a["chat"] == chat]
    if any(a["key"] == key and a["op"] == op and a["val"] == val for a in mine):
        await send_screen(m, "Такое уведомление уже есть.", kb, True)
        return
    if len(mine) >= MAX_ALERTS:
        await send_screen(
            m, f"Лимит уведомлений: {MAX_ALERTS}. Удали ненужные в разделе «Уведомления».", kb, True
        )
        return

    _alerts.append({
        "id": next(_alert_ids), "chat": chat, "key": key,
        "num": n, "name": name, "op": op, "val": val,
    })
    text = (
        f"Готово. Сообщу один раз, когда онлайн {safe} (№{n}) станет {word} {val}. "
        f"Сейчас: {cur}."
    )
    await send_screen(m, text, kb, True)


# ---------- экраны ----------


async def build_screen(screen: str, uid: int, force: bool = False):
    """Возвращает (текст, клавиатура, с_фото)."""
    if screen == "menu":
        return MENU_TEXT, screen_kb("menu", refresh=False), True
    if screen == "help":
        return HELP_TEXT, screen_kb("help", refresh=False), True
    if screen == "biz":
        return BIZ_TEXT, biz_kb(), True

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
    if screen == "msg":
        text, markup = build_alerts(chat_of(event))
        await render(event, text, markup, True)
        return
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


@dp.message(Command("business"))
async def cmd_business(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lstrip("№#").strip()
    if not arg:
        await show_screen(m, "biz")
        return
    back = InlineKeyboardMarkup(inline_keyboard=[[btn("← Назад", "nav:biz")]])
    if arg.isdigit() and int(arg) in BIZ_BY_NUM:
        lines = [biz_line(e, True) for e in BIZ_BY_NUM[int(arg)]]
        text = "<b>Найдено:</b>\n" + "\n".join(html.escape(ln) for ln in lines)
    else:
        text = f"Бизнес «{html.escape(arg)}» не найден. Отправь номер, например /business 57."
    await send_screen(m, text, back, True)


# ---------- инлайн-кнопки ----------


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(c: CallbackQuery):
    await show_screen(c, c.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("rf:"))
async def cb_refresh(c: CallbackQuery):
    await show_screen(c, c.data.split(":", 1)[1], force=True)


@dp.callback_query(F.data.startswith("bc:"))
async def cb_biz_cat(c: CallbackQuery):
    _, ci, page = c.data.split(":")
    await show_biz_cat(c, num(ci), num(page))


@dp.callback_query(F.data.startswith("ad:"))
async def cb_alert_del(c: CallbackQuery):
    aid = num(c.data.split(":")[1])
    chat = chat_of(c)
    _alerts[:] = [a for a in _alerts if not (a["id"] == aid and a["chat"] == chat)]
    await show_screen(c, "msg")


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
        BotCommand(command="business", description="Бизнес на карте"),
        BotCommand(command="message", description="Уведомление об онлайне"),
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
        task = asyncio.create_task(alerts_loop())
        await dp.start_polling(bot)
    finally:
        if task:
            task.cancel()
        await _session.close()


if __name__ == "__main__":
    asyncio.run(main())
