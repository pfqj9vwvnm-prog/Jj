import asyncio
import html
import io
import logging
import math
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "https://blackrussia.online/api/gameservers/")
CACHE_TTL = int(os.getenv("CACHE_TTL", "15"))  # секунд
DB_PATH = os.getenv("DB_PATH", "bot.db")
ADMIN_ID = int(os.getenv("ADMIN_ID", "5845336275"))
WATCH_EVERY = max(15, int(os.getenv("WATCH_EVERY", "30")))  # как часто проверять уведомления, сек
HISTORY_INTERVAL = int(os.getenv("HISTORY_INTERVAL", "300"))  # как часто писать историю онлайна, сек

PHOTO_PATH = Path(__file__).parent / "brtools.png"
CAPTION_LIMIT = 1000  # лимит подписи к фото 1024, берём с запасом
TEXT_LIMIT = 4000  # лимит текстового сообщения 4096, берём с запасом
PAGE_SIZE = 15  # серверов на странице при выборе избранного
BIZ_PAGE_CHARS = 800  # символов списка бизнесов на одной странице (под подпись к фото)

BASE_ALERTS = 10  # уведомлений об онлайне по умолчанию
REF_BONUS = 5  # плюс к лимиту за каждого приглашённого друга
MAX_ALERTS = 50  # максимум уведомлений
MAX_X2 = 20  # максимум подписок на x2

MSK = timezone(timedelta(hours=3))
START_TS = time.time()

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_photo_file_id: str | None = None
_photo_uid: str | None = None  # file_unique_id фирменной картинки
_x2_state: dict[str, bool] = {}
BOT_USERNAME: str | None = None


# ---------- база данных ----------

Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.executescript(
    """
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY, first_seen INTEGER, last_seen INTEGER, referrer INTEGER);
    CREATE TABLE IF NOT EXISTS favs (
        user_id INTEGER, server_id INTEGER, PRIMARY KEY (user_id, server_id));
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, skey TEXT, name TEXT,
        op TEXT, val INTEGER, repeat INTEGER DEFAULT 0, armed INTEGER DEFAULT 1);
    CREATE TABLE IF NOT EXISTS x2subs (
        id INTEGER PRIMARY KEY AUTOINCREMENT, chat INTEGER, skey TEXT, name TEXT,
        UNIQUE (chat, skey));
    CREATE TABLE IF NOT EXISTS digest (
        chat INTEGER PRIMARY KEY, hour INTEGER, last_date TEXT);
    CREATE TABLE IF NOT EXISTS history (ts INTEGER, total INTEGER);
    CREATE TABLE IF NOT EXISTS cmdstats (cmd TEXT PRIMARY KEY, n INTEGER);
    """
)
db.commit()


def run(sql: str, params=()):
    cur = db.execute(sql, params)
    db.commit()
    return cur


def get_favs(uid: int) -> set[int]:
    return {r[0] for r in db.execute("SELECT server_id FROM favs WHERE user_id=?", (uid,))}


def toggle_fav(uid: int, sid: int) -> None:
    cur = db.execute("DELETE FROM favs WHERE user_id=? AND server_id=?", (uid, sid))
    if cur.rowcount == 0:
        db.execute("INSERT OR IGNORE INTO favs VALUES (?, ?)", (uid, sid))
    db.commit()


def purge_chat(chat: int) -> None:
    db.execute("DELETE FROM alerts WHERE chat=?", (chat,))
    db.execute("DELETE FROM x2subs WHERE chat=?", (chat,))
    db.execute("DELETE FROM digest WHERE chat=?", (chat,))
    db.commit()


def alert_limit(uid: int) -> tuple[int, int]:
    refs = db.execute("SELECT COUNT(*) FROM users WHERE referrer=?", (uid,)).fetchone()[0]
    return min(BASE_ALERTS + REF_BONUS * refs, MAX_ALERTS), refs


# ---------- учёт пользователей и команд ----------


def track(event) -> bool:
    """Записывает пользователя и команду. Возвращает True, если пользователь новый."""
    user = event.from_user
    if not user or user.is_bot:
        return False
    now = int(time.time())
    cur = db.execute(
        "INSERT OR IGNORE INTO users (user_id, first_seen, last_seen) VALUES (?, ?, ?)",
        (user.id, now, now),
    )
    is_new = cur.rowcount == 1
    if not is_new:
        db.execute("UPDATE users SET last_seen=? WHERE user_id=?", (now, user.id))

    cmd = None
    if isinstance(event, Message):
        t = (event.text or "").strip()
        if t.startswith("/"):
            cmd = t.split()[0][1:].split("@")[0].lower()
        elif t.lower() == "онлайн":
            cmd = "онлайн"
    elif isinstance(event, CallbackQuery) and event.data and event.data != "noop":
        cmd = "btn:" + event.data.split(":")[0]
    if cmd:
        db.execute(
            "INSERT INTO cmdstats (cmd, n) VALUES (?, 1) "
            "ON CONFLICT(cmd) DO UPDATE SET n = n + 1",
            (cmd,),
        )
    db.commit()
    return is_new


class TrackMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            data["is_new"] = track(event)
        except Exception:
            logging.exception("track failed")
            data["is_new"] = False
        return await handler(event, data)


dp.message.outer_middleware(TrackMiddleware())
dp.callback_query.outer_middleware(TrackMiddleware())


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


def fmt_num_line(n: int, s: dict) -> str:
    name = str(s.get("name", "?"))
    x = " x2" if s.get("x2") else ""
    return f"№{n} {name}: {num(s.get('online'))}/{num(s.get('max_online'))}{x}"


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


def server_card(n: int, s: dict) -> str:
    name = html.escape(str(s.get("name", "?")))
    x2 = "включён" if s.get("x2") else "нет"
    return (
        f"<b>{name}</b> (№{n})\n"
        f"Онлайн: {num(s.get('online'))}/{num(s.get('max_online'))}\n"
        f"x2: {x2}"
    )


# ---------- поиск сервера по номеру или названию ----------

_TR = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o",
    "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "c",
    "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}
# названия, которые простой транслитерацией не получить
ALIASES = {
    "москва": "moscow", "питер": "spb", "санкт-петербург": "spb", "спб": "spb",
    "белый": "white", "золотой": "gold", "золото": "gold", "серый": "gray",
    "лед": "ice", "лёд": "ice", "платина": "platinum", "екатеринбург": "ekb",
    "новосибирск": "novosib", "шоколад": "choco", "чили": "chilli",
}


def translit(s: str) -> str:
    return "".join(_TR.get(ch, ch) for ch in s.lower())


def find_servers(servers: list[dict], q: str) -> list[tuple[int, dict]]:
    """Возвращает [(номер в списке, сервер)]. Номер — как в /online."""
    q = q.strip().lstrip("№#").strip().lower()
    if not q:
        return []
    if q.isdigit():
        n = int(q)
        return [(n, servers[n - 1])] if 1 <= n <= len(servers) else []
    variants = {q}
    t = translit(q)
    variants |= {t, t.replace("yy", "y")}
    if q in ALIASES:
        variants.add(ALIASES[q])
    variants.discard("")
    return [
        (i, s) for i, s in enumerate(servers, 1)
        if any(v in str(s.get("name", "")).lower() for v in variants)
    ]


def ambiguous(found: list[tuple[int, dict]]) -> str:
    lines = [f"№{n} {s.get('name', '?')}" for n, s in found[:8]]
    more = f"\n…и ещё {len(found) - 8}" if len(found) > 8 else ""
    return "Нашлось несколько серверов, укажи номер:\n" + html.escape("\n".join(lines)) + more


# ---------- кнопки ----------

NAV = {
    "online": "☰ Все серверы",
    "top": "▲ Топ",
    "free": "▽ Свободные",
    "fav": "★ Избранное",
    "graph": "▤ График",
    "biz": "◈ Бизнес на карте",
    "msg": "◉ Уведомления",
    "dg": "◔ Сводка",
    "invite": "+ Пригласить",
    "help": "? Помощь",
    "admin": "◆ Админ",
}
MENU_ORDER = ["online", "top", "free", "fav", "graph", "biz", "msg", "dg", "invite"]
QUICK = ["online", "top", "fav", "graph"]


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def screen_kb(current: str, extra=None, refresh: bool = True, admin: bool = False) -> InlineKeyboardMarkup:
    rows = list(extra or [])
    keys = MENU_ORDER if current == "menu" else QUICK
    items = [btn(NAV[k], f"nav:{k}") for k in keys if k != current]
    if current == "menu":
        items.append(btn(NAV["help"], "nav:help"))
        if admin:
            items.append(btn(NAV["admin"], "nav:admin"))
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
    "/online [сервер] — онлайн, например /online 90 или /online норильск\n"
    "/top — топ по онлайну\n"
    "/free — самые свободные серверы\n"
    "/fav — избранное\n"
    "/graph — график онлайна за сутки\n"
    "/business [номер] — бизнес на карте\n"
    "/message — уведомление об онлайне\n"
    "/x2 90 — сообщать о включении x2\n"
    "/digest 9 — ежедневная сводка в 9:00 МСК\n"
    "/invite — пригласить друзей\n\n"
    f"Данные обновляются примерно раз в {CACHE_TTL} сек."
)
ERROR_TEXT = "Не удалось получить онлайн, попробуй позже."
BIZ_TEXT = (
    "<b>Бизнес на карте</b>\n"
    "Выбери тип бизнеса или отправь /business и номер, например /business 57."
)
ALERT_HELP = (
    "Сообщу, когда онлайн сервера станет выше или ниже порога.\n"
    "/message 90 -1300 — станет меньше 1300 (один раз)\n"
    "/message норильск +1300 — станет больше 1300\n"
    "«повтор» в конце — сообщать каждый раз.\n"
    "/x2 90 — сообщать о включении и выключении x2"
)
GRAPH_EMPTY = (
    "<b>График онлайна</b>\n"
    "Данные ещё копятся: бот записывает общий онлайн каждые 5 минут. "
    "Загляни через несколько минут."
)


# ---------- отправка и редактирование ----------


def remember_photo(sent: Message) -> None:
    global _photo_file_id, _photo_uid
    if sent.photo and not _photo_file_id:
        _photo_file_id = sent.photo[-1].file_id
        _photo_uid = sent.photo[-1].file_unique_id


def is_brand(msg: Message) -> bool:
    """True, если в сообщении фирменная картинка brtools.png (а не график)."""
    return (
        bool(msg.photo)
        and _photo_uid is not None
        and msg.photo[-1].file_unique_id == _photo_uid
    )


async def send_screen(m: Message, text: str, markup: InlineKeyboardMarkup | None, photo: bool):
    """Одно сообщение: либо фото с подписью, либо обычный текст."""
    global _photo_file_id, _photo_uid
    if photo:
        try:
            src = _photo_file_id or FSInputFile(PHOTO_PATH)
            sent = await m.answer_photo(src, caption=text, reply_markup=markup)
            remember_photo(sent)
            return
        except Exception:
            _photo_file_id = None
            _photo_uid = None
            logging.exception(
                "answer_photo failed (path=%s, exists=%s)", PHOTO_PATH, PHOTO_PATH.exists()
            )
    await m.answer(text, reply_markup=markup)  # запасной вариант, чтобы бот не молчал


async def push(chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None):
    """Сообщение без входящего Message (для уведомлений): фото с подписью."""
    global _photo_file_id, _photo_uid
    try:
        src = _photo_file_id or FSInputFile(PHOTO_PATH)
        sent = await bot.send_photo(chat_id, src, caption=text, reply_markup=markup)
        remember_photo(sent)
        return
    except TelegramForbiddenError:
        raise
    except Exception:
        _photo_file_id = None
        _photo_uid = None
        logging.exception("send_photo failed (path=%s, exists=%s)", PHOTO_PATH, PHOTO_PATH.exists())
    await bot.send_message(chat_id, text, reply_markup=markup)


async def render(event, text: str, markup: InlineKeyboardMarkup | None, photo: bool):
    if not isinstance(event, CallbackQuery):
        await send_screen(event, text, markup, photo)
        return

    toast = None
    msg = event.message
    if isinstance(msg, Message):
        # править на месте можно только если тип сообщения совпадает
        in_place = is_brand(msg) if photo else not msg.photo
        try:
            if in_place:
                if photo:
                    await msg.edit_caption(caption=text, reply_markup=markup)
                else:
                    await msg.edit_text(text, reply_markup=markup)
            else:
                # фото <-> текст (и график -> экран) редактированием не превратить
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


# ---------- бизнесы: экраны ----------


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


# ---------- уведомления, x2, сводка: экраны ----------


def alert_hit(op: str, cur: int, val: int) -> bool:
    return cur < val if op == "-" else cur > val


def alert_label(a) -> str:
    sign = "<" if a["op"] == "-" else ">"
    rep = " ∞" if a["repeat"] else ""
    return f"✕ {a['name']} {sign} {a['val']}{rep}"


def build_alerts(chat: int, uid: int):
    alerts = db.execute("SELECT * FROM alerts WHERE chat=? ORDER BY id", (chat,)).fetchall()
    subs = db.execute("SELECT * FROM x2subs WHERE chat=? ORDER BY id", (chat,)).fetchall()
    limit, _ = alert_limit(uid)
    if alerts or subs:
        head = (
            f"Уведомлений: {len(alerts)}/{limit}, x2: {len(subs)}. "
            "Нажми на уведомление, чтобы удалить."
        )
    else:
        head = "Активных уведомлений нет."
    text = f"<b>Уведомления</b>\n{ALERT_HELP}\n\n{head}"
    rows = [[btn(alert_label(a), f"ad:{a['id']}")] for a in alerts]
    rows += [[btn(f"✕ {s['name']} x2", f"xd:{s['id']}")] for s in subs]
    return text, screen_kb("msg", extra=rows, refresh=False)


def build_digest_screen(chat: int):
    row = db.execute("SELECT hour FROM digest WHERE chat=?", (chat,)).fetchone()
    status = f"включена, {row['hour']:02d}:00 МСК" if row else "выключена"
    text = (
        "<b>Ежедневная сводка</b>\n"
        "Каждый день в выбранный час (МСК) бот пришлёт онлайн избранных серверов, "
        "а если избранного нет, топ-5.\n\n"
        f"Сейчас: {status}.\nВыбери время:"
    )
    hours = [7, 9, 12, 15, 18, 21]
    cur = row["hour"] if row else None
    btns = [btn(("✓ " if h == cur else "") + f"{h:02d}:00", f"dg:{h}") for h in hours]
    rows = [btns[:3], btns[3:]]
    if row:
        rows.append([btn("✕ Выключить", "dg:off")])
    return text, screen_kb("dg", extra=rows, refresh=False)


def set_digest(chat: int, hour: int) -> None:
    now = datetime.now(MSK)
    # если час уже наступил, первая сводка придёт завтра
    last = now.strftime("%Y-%m-%d") if now.hour >= hour else None
    run(
        "INSERT INTO digest (chat, hour, last_date) VALUES (?, ?, ?) "
        "ON CONFLICT(chat) DO UPDATE SET hour=excluded.hour, last_date=excluded.last_date",
        (chat, hour, last),
    )


def build_invite(uid: int):
    limit, refs = alert_limit(uid)
    rows = []
    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=ref_{uid}"
        share = (
            "https://t.me/share/url?url=" + quote(link, safe="")
            + "&text=" + quote("Онлайн серверов Black Russia", safe="")
        )
        rows.append([InlineKeyboardButton(text="Отправить другу", url=share)])
        link_text = f"Твоя ссылка:\n{link}"
    else:
        link_text = "Ссылка пока недоступна, попробуй позже."
    text = (
        "<b>Пригласить друзей</b>\n"
        f"{link_text}\n\n"
        f"Приглашено: {refs}\n"
        f"Лимит уведомлений: {limit} из {MAX_ALERTS}\n"
        f"За каждого друга +{REF_BONUS} к лимиту."
    )
    return text, screen_kb("invite", extra=rows, refresh=False)


def build_admin() -> str:
    now = int(time.time())

    def cnt(sql, *p):
        return db.execute(sql, p).fetchone()[0]

    up = int(time.time() - START_TS)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    mi = rem // 60
    cmds = db.execute(
        "SELECT cmd, n FROM cmdstats WHERE cmd NOT LIKE 'btn:%' ORDER BY n DESC LIMIT 6"
    ).fetchall()
    top = "\n".join(
        f"{'' if r['cmd'] == 'онлайн' else '/'}{html.escape(r['cmd'])}: {r['n']}" for r in cmds
    ) or "—"
    try:
        size = os.path.getsize(DB_PATH) // 1024
    except OSError:
        size = 0

    lines = [
        "<b>Админ-панель</b>",
        f"Пользователей: {cnt('SELECT COUNT(*) FROM users')}",
        f"Новых за 24 ч / 7 дн: {cnt('SELECT COUNT(*) FROM users WHERE first_seen>=?', now - 86400)}"
        f" / {cnt('SELECT COUNT(*) FROM users WHERE first_seen>=?', now - 7 * 86400)}",
        f"Активных за 24 ч / 7 дн: {cnt('SELECT COUNT(*) FROM users WHERE last_seen>=?', now - 86400)}"
        f" / {cnt('SELECT COUNT(*) FROM users WHERE last_seen>=?', now - 7 * 86400)}",
        f"Пришли по приглашению: {cnt('SELECT COUNT(*) FROM users WHERE referrer IS NOT NULL')}",
        "",
        f"Уведомлений: {cnt('SELECT COUNT(*) FROM alerts WHERE repeat=0')} разовых, "
        f"{cnt('SELECT COUNT(*) FROM alerts WHERE repeat=1')} повторных",
        f"Подписок на x2: {cnt('SELECT COUNT(*) FROM x2subs')}",
        f"Включённых сводок: {cnt('SELECT COUNT(*) FROM digest')}",
        f"Пользователей с избранным: {cnt('SELECT COUNT(DISTINCT user_id) FROM favs')}",
        "",
        f"Нажатий кнопок: {cnt(\"SELECT COALESCE(SUM(n), 0) FROM cmdstats WHERE cmd LIKE 'btn:%'\")}",
        "<b>Популярные команды</b>",
        top,
        "",
        f"Записей истории: {cnt('SELECT COUNT(*) FROM history')}",
        f"База: {size} КБ",
        f"Аптайм: {d} д {h} ч {mi} мин",
    ]
    return "\n".join(lines)


# ---------- график ----------


def make_graph(points: list[tuple[int, int]]) -> bytes:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.dates as mdates
    import matplotlib.pyplot as plt
    from matplotlib.ticker import FuncFormatter

    xs = [datetime.fromtimestamp(t, MSK) for t, _ in points]
    ys = [v for _, v in points]
    ip, im = ys.index(max(ys)), ys.index(min(ys))

    fig, ax = plt.subplots(figsize=(8, 4), dpi=110)
    ax.plot(xs, ys, linewidth=2, color="#2a7de1")
    ax.fill_between(xs, ys, alpha=0.15, color="#2a7de1")
    ax.scatter([xs[ip], xs[im]], [ys[ip], ys[im]], color="#e14a2a", zorder=3)
    ax.annotate(str(ys[ip]), (xs[ip], ys[ip]), textcoords="offset points", xytext=(0, 8), ha="center")
    ax.annotate(str(ys[im]), (xs[im], ys[im]), textcoords="offset points", xytext=(0, -14), ha="center")
    ax.xaxis.set_major_locator(mdates.HourLocator(interval=3, tz=MSK))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=MSK))
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{int(v):,}".replace(",", " ")))
    ax.grid(alpha=0.3)
    ax.set_title("Общий онлайн за 24 часа (МСК)")
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png")
    plt.close(fig)
    return buf.getvalue()


async def send_graph(event, png: bytes, caption: str, kb: InlineKeyboardMarkup):
    media = BufferedInputFile(png, filename="online.png")
    if isinstance(event, CallbackQuery):
        msg = event.message
        if isinstance(msg, Message):
            if msg.photo and not is_brand(msg):  # там уже график — меняем картинку на месте
                try:
                    await msg.edit_media(
                        InputMediaPhoto(media=media, caption=caption, parse_mode=ParseMode.HTML),
                        reply_markup=kb,
                    )
                    await event.answer()
                    return
                except TelegramBadRequest as e:
                    if "not modified" in str(e):
                        await event.answer("Ничего не изменилось")
                        return
                    logging.exception("edit_media failed")
            try:
                await msg.delete()
            except Exception:
                logging.exception("delete failed")
            await msg.answer_photo(media, caption=caption, reply_markup=kb)
        await event.answer()
    else:
        await event.answer_photo(media, caption=caption, reply_markup=kb)


async def show_graph(event):
    now = int(time.time())
    rows = db.execute(
        "SELECT ts, total FROM history WHERE ts>=? ORDER BY ts", (now - 86400,)
    ).fetchall()
    kb = screen_kb("graph")
    if len(rows) < 2:
        await render(event, GRAPH_EMPTY, kb, True)
        return
    pts = [(r["ts"], r["total"]) for r in rows]
    peak = max(pts, key=lambda p: p[1])
    low = min(pts, key=lambda p: p[1])
    avg = sum(v for _, v in pts) // len(pts)
    caption = (
        "<b>Общий онлайн за 24 часа</b>\n"
        f"Сейчас: {pts[-1][1]}\n"
        f"Пик: {peak[1]} ({datetime.fromtimestamp(peak[0], MSK):%H:%M} МСК)\n"
        f"Минимум: {low[1]} ({datetime.fromtimestamp(low[0], MSK):%H:%M} МСК)\n"
        f"Среднее: {avg}"
    )
    try:
        png = await asyncio.to_thread(make_graph, pts)
    except Exception:
        logging.exception("graph failed (установлен ли matplotlib?)")
        await render(event, caption, kb, True)  # без картинки хотя бы цифры
        return
    await send_graph(event, png, caption, kb)


# ---------- экраны ----------


async def build_screen(screen: str, uid: int, chat: int, force: bool = False):
    """Возвращает (текст, клавиатура, с_фото)."""
    if screen == "menu":
        return MENU_TEXT, screen_kb("menu", refresh=False, admin=(uid == ADMIN_ID)), True
    if screen == "help":
        return HELP_TEXT, screen_kb("help", refresh=False), True
    if screen == "biz":
        return BIZ_TEXT, biz_kb(), True
    if screen == "msg":
        text, kb = build_alerts(chat, uid)
        return text, kb, True
    if screen == "dg":
        text, kb = build_digest_screen(chat)
        return text, kb, True
    if screen == "invite":
        text, kb = build_invite(uid)
        return text, kb, True
    if screen == "admin":
        if uid != ADMIN_ID:
            return MENU_TEXT, screen_kb("menu", refresh=False), True
        return build_admin(), screen_kb("admin"), False

    if screen not in ("top", "free", "fav"):
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

    if screen == "free":
        pairs = [(n, s) for n, s in enumerate(servers, 1) if num(s.get("online")) > 0]
        pairs = sorted(pairs or list(enumerate(servers, 1)), key=lambda p: num(p[1].get("online")))[:10]
        lines = [fmt_num_line(n, s) for n, s in pairs]
        text = render_list("Самые свободные серверы:", lines, f"Общий онлайн: {total}", limit)
        return text, screen_kb("free"), photo

    if screen == "fav":
        by_id = {num(s.get("id")): s for s in servers if s.get("id") is not None}
        mine = [by_id[i] for i in sorted(get_favs(uid)) if i in by_id]
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
    if screen == "graph":
        await show_graph(event)
        return
    text, markup, photo = await build_screen(screen, uid_of(event), chat_of(event), force)
    await render(event, text, markup, photo)


def card_kb(n: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [btn("↻ Обновить", f"sr:{n}"), btn("☰ Все серверы", "nav:online")],
        [btn("← Назад", "nav:menu")],
    ])


async def show_search(event, q: str):
    try:
        servers = await fetch_servers()
    except Exception:
        logging.exception("fetch failed")
        await render(event, ERROR_TEXT, screen_kb("online"), True)
        return
    found = find_servers(servers, q)
    if not found:
        text = f"Сервер «{html.escape(q)}» не найден. Попробуй номер из списка /online."
        await render(event, text, screen_kb("online", refresh=False), True)
        return
    if len(found) == 1:
        n, s = found[0]
        await render(event, server_card(n, s), card_kb(n), True)
        return
    lines = [fmt_num_line(n, s) for n, s in found[:20]]
    text = render_list(f"Найдено серверов: {len(found)}", lines, "Нажми на сервер ниже", CAPTION_LIMIT)
    btns = [btn(f"{n} {s.get('name', '?')}", f"sr:{n}") for n, s in found[:12]]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([btn("← Назад", "nav:menu")])
    await render(event, text, InlineKeyboardMarkup(inline_keyboard=rows), True)


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


# ---------- фоновые проверки ----------


def build_digest_text(chat: int, servers: list[dict], now: datetime) -> str:
    by_id = {num(s.get("id")): s for s in servers if s.get("id") is not None}
    mine = [by_id[i] for i in sorted(get_favs(chat)) if i in by_id]
    stamp = now.strftime("%d.%m %H:%M")
    total = total_online(servers)
    if mine:
        lines = [fmt_line(i, s) for i, s in enumerate(mine, 1)]
        title = f"Сводка {stamp} МСК: избранное"
    else:
        best = sorted(servers, key=lambda s: num(s.get("online")), reverse=True)[:5]
        lines = [fmt_line(i, s) for i, s in enumerate(best, 1)]
        title = f"Сводка {stamp} МСК: топ-5"
    return render_list(title, lines, f"Общий онлайн: {total}", CAPTION_LIMIT)


async def safe_push(chat: int, text: str, kb: InlineKeyboardMarkup | None) -> bool:
    try:
        await push(chat, text, kb)
        return True
    except TelegramForbiddenError:
        purge_chat(chat)  # пользователь заблокировал бота
    except Exception:
        logging.exception("push failed")
    return False


async def check_alerts(servers: list[dict]):
    alerts = db.execute("SELECT * FROM alerts").fetchall()
    if not alerts:
        return
    by_key = {str(server_key(s)): s for s in servers}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("☰ Все серверы", "nav:online"), btn("◉ Уведомления", "nav:msg")],
    ])
    for a in alerts:
        s = by_key.get(a["skey"])
        if s is None:
            continue
        cur = num(s.get("online"))
        hit = alert_hit(a["op"], cur, a["val"])
        word = "ниже" if a["op"] == "-" else "выше"
        text = (
            "<b>Уведомление</b>\n"
            f"{html.escape(str(s.get('name', a['name'])))}: онлайн {cur}, это {word} {a['val']}."
        )
        if a["repeat"]:
            if hit and a["armed"]:
                if await safe_push(a["chat"], text, kb):
                    run("UPDATE alerts SET armed=0 WHERE id=?", (a["id"],))
            elif not hit and not a["armed"]:
                run("UPDATE alerts SET armed=1 WHERE id=?", (a["id"],))
        elif hit:
            if await safe_push(a["chat"], text, kb):
                run("DELETE FROM alerts WHERE id=?", (a["id"],))


async def check_x2(servers: list[dict]):
    cur = {str(server_key(s)): bool(s.get("x2")) for s in servers}
    prev = dict(_x2_state)
    _x2_state.clear()
    _x2_state.update(cur)
    if not prev:  # первый проход — только запоминаем состояние
        return
    subs = db.execute("SELECT * FROM x2subs").fetchall()
    if not subs:
        return
    by_key = {str(server_key(s)): s for s in servers}
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("☰ Все серверы", "nav:online"), btn("◉ Уведомления", "nav:msg")],
    ])
    for sub in subs:
        k = sub["skey"]
        if k in prev and k in cur and prev[k] != cur[k]:
            s = by_key[k]
            state = "включён" if cur[k] else "выключен"
            text = (
                "<b>x2</b>\n"
                f"{html.escape(str(s.get('name', sub['name'])))}: x2 {state}. "
                f"Онлайн: {num(s.get('online'))}/{num(s.get('max_online'))}."
            )
            await safe_push(sub["chat"], text, kb)


async def check_digests(servers: list[dict]):
    now = datetime.now(MSK)
    today = now.strftime("%Y-%m-%d")
    rows = db.execute(
        "SELECT chat FROM digest WHERE hour=? AND COALESCE(last_date, '') <> ?",
        (now.hour, today),
    ).fetchall()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [btn("☰ Все серверы", "nav:online"), btn("◔ Сводка", "nav:dg")],
    ])
    for r in rows:
        chat = r["chat"]
        text = build_digest_text(chat, servers, now)
        if await safe_push(chat, text, kb):
            run("UPDATE digest SET last_date=? WHERE chat=?", (today, chat))


async def watch_loop():
    last_hist = 0.0
    while True:
        try:
            servers = await fetch_servers(force=True)
        except Exception:
            logging.exception("watch fetch failed")
            await asyncio.sleep(WATCH_EVERY)
            continue

        now = time.time()
        if now - last_hist >= HISTORY_INTERVAL:
            total = total_online(servers)
            if total > 0:
                try:
                    db.execute("INSERT INTO history VALUES (?, ?)", (int(now), total))
                    db.execute("DELETE FROM history WHERE ts < ?", (int(now) - 7 * 86400,))
                    db.commit()
                    last_hist = now
                except Exception:
                    logging.exception("history failed")

        for step in (check_alerts, check_x2, check_digests):
            try:
                await step(servers)
            except Exception:
                logging.exception("%s failed", step.__name__)
        await asyncio.sleep(WATCH_EVERY)


# ---------- команды ----------

_ALERT_RE = re.compile(
    r"^(.+?)\s+([+\-−–<>])\s*(\d+)(?:\s+(повтор\w*|repeat|всегда|\*))?\s*$", re.I
)


async def resolve_server(m: Message, query: str, kb: InlineKeyboardMarkup):
    """Находит один сервер по номеру/названию. Если не вышло, сам отвечает и возвращает None."""
    try:
        servers = await fetch_servers()
    except Exception:
        logging.exception("fetch failed")
        await send_screen(m, "Не удалось получить список серверов, попробуй позже.", kb, True)
        return None
    found = find_servers(servers, query)
    if not found:
        text = f"Сервер «{html.escape(query)}» не найден. Номер можно взять из /online."
        await send_screen(m, text, kb, True)
        return None
    if len(found) > 1:
        await send_screen(m, ambiguous(found), kb, True)
        return None
    return found[0]


@dp.message(CommandStart())
async def cmd_start(m: Message, command: CommandObject, is_new: bool = False):
    arg = (command.args or "").strip()
    if is_new and arg.startswith("ref_") and arg[4:].isdigit():
        ref, uid = int(arg[4:]), uid_of(m)
        if ref != uid and db.execute("SELECT 1 FROM users WHERE user_id=?", (ref,)).fetchone():
            run("UPDATE users SET referrer=? WHERE user_id=?", (ref, uid))
            limit, refs = alert_limit(ref)
            try:
                await bot.send_message(
                    ref,
                    "По твоей ссылке пришёл новый пользователь.\n"
                    f"Приглашено: {refs}. Лимит уведомлений: {limit}.",
                )
            except Exception:
                logging.exception("referral notify failed")
    await show_screen(m, "online")


@dp.message(Command("online"))
@dp.message(F.text.func(lambda t: t.strip().lower() == "онлайн"))
async def cmd_online(m: Message, command: CommandObject | None = None):
    arg = (command.args or "").strip() if command else ""
    if arg:
        await show_search(m, arg)
    else:
        await show_screen(m, "online")


@dp.message(Command("menu"))
async def cmd_menu(m: Message):
    await show_screen(m, "menu")


@dp.message(Command("top"))
async def cmd_top(m: Message):
    await show_screen(m, "top")


@dp.message(Command("free"))
async def cmd_free(m: Message):
    await show_screen(m, "free")


@dp.message(Command("fav"))
async def cmd_fav(m: Message):
    await show_screen(m, "fav")


@dp.message(Command("graph"))
async def cmd_graph(m: Message):
    await show_screen(m, "graph")


@dp.message(Command("help"))
async def cmd_help(m: Message):
    await show_screen(m, "help")


@dp.message(Command("invite"))
async def cmd_invite(m: Message):
    await show_screen(m, "invite")


@dp.message(Command("admin"))
async def cmd_admin(m: Message):
    if uid_of(m) != ADMIN_ID:
        return  # для остальных команды как будто нет
    await show_screen(m, "admin")


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

    query, sign, val_s, rep = mt.groups()
    op = "-" if sign in "-−–<" else "+"
    val = int(val_s)
    repeat = bool(rep)
    word = "меньше" if op == "-" else "больше"

    found = await resolve_server(m, query, kb)
    if not found:
        return
    n, s = found
    name = str(s.get("name", "?"))
    safe = html.escape(name)
    cur = num(s.get("online"))
    hit = alert_hit(op, cur, val)

    if hit and not repeat:
        text = (
            f"Уже выполнено: на {safe} сейчас {cur}, это {word} {val}. "
            "Задай другой порог или добавь «повтор»."
        )
        await send_screen(m, text, kb, True)
        return

    chat, uid = chat_of(m), uid_of(m)
    skey = str(server_key(s))
    mine = db.execute("SELECT * FROM alerts WHERE chat=?", (chat,)).fetchall()
    if any(a["skey"] == skey and a["op"] == op and a["val"] == val for a in mine):
        await send_screen(m, "Такое уведомление уже есть.", kb, True)
        return
    limit, _ = alert_limit(uid)
    if len(mine) >= limit:
        text = (
            f"Лимит уведомлений: {limit}. Удали ненужные в разделе «Уведомления» "
            "или пригласи друзей: /invite."
        )
        await send_screen(m, text, kb, True)
        return

    run(
        "INSERT INTO alerts (chat, skey, name, op, val, repeat, armed) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (chat, skey, name, op, val, int(repeat), 0 if hit else 1),
    )
    if repeat:
        text = f"Готово. Буду сообщать каждый раз, когда онлайн {safe} станет {word} {val}. Сейчас: {cur}."
        if hit:
            text += " Условие уже выполняется, сообщу, когда оно выполнится снова."
    else:
        text = f"Готово. Сообщу один раз, когда онлайн {safe} станет {word} {val}. Сейчас: {cur}."
    await send_screen(m, text, kb, True)


@dp.message(Command("x2"))
async def cmd_x2(m: Message, command: CommandObject):
    arg = (command.args or "").strip()
    if not arg:
        await show_screen(m, "msg")
        return

    kb = screen_kb("msg", refresh=False)
    found = await resolve_server(m, arg, kb)
    if not found:
        return
    n, s = found
    name = str(s.get("name", "?"))
    safe = html.escape(name)
    chat, skey = chat_of(m), str(server_key(s))

    existing = db.execute(
        "SELECT id FROM x2subs WHERE chat=? AND skey=?", (chat, skey)
    ).fetchone()
    if existing:
        run("DELETE FROM x2subs WHERE id=?", (existing["id"],))
        text = f"Отключено: больше не сообщаю про x2 на {safe}."
    else:
        count = db.execute("SELECT COUNT(*) FROM x2subs WHERE chat=?", (chat,)).fetchone()[0]
        if count >= MAX_X2:
            await send_screen(m, f"Лимит подписок на x2: {MAX_X2}. Удали ненужные в «Уведомлениях».", kb, True)
            return
        run("INSERT INTO x2subs (chat, skey, name) VALUES (?, ?, ?)", (chat, skey, name))
        now_x2 = "включён" if s.get("x2") else "не включён"
        text = f"Готово. Сообщу, когда на {safe} включат или выключат x2. Сейчас x2 {now_x2}."
    await send_screen(m, text, kb, True)


@dp.message(Command("digest"))
async def cmd_digest(m: Message, command: CommandObject):
    arg = (command.args or "").strip().lower()
    if not arg:
        await show_screen(m, "dg")
        return
    chat = chat_of(m)
    kb = screen_kb("dg", refresh=False)
    if arg in ("off", "выкл", "стоп", "0ff"):
        run("DELETE FROM digest WHERE chat=?", (chat,))
        await send_screen(m, "Ежедневная сводка выключена.", kb, True)
        return
    hour_s = arg.split(":")[0]
    if hour_s.isdigit() and 0 <= int(hour_s) <= 23:
        hour = int(hour_s)
        set_digest(chat, hour)
        now = datetime.now(MSK)
        when = "завтра" if now.hour >= hour else "сегодня"
        await send_screen(m, f"Готово. Первая сводка придёт {when} в {hour:02d}:00 МСК.", kb, True)
        return
    await send_screen(m, "Укажи час от 0 до 23, например /digest 9, или /digest off.", kb, True)


# ---------- инлайн-кнопки ----------


@dp.callback_query(F.data.startswith("nav:"))
async def cb_nav(c: CallbackQuery):
    await show_screen(c, c.data.split(":", 1)[1])


@dp.callback_query(F.data.startswith("rf:"))
async def cb_refresh(c: CallbackQuery):
    await show_screen(c, c.data.split(":", 1)[1], force=True)


@dp.callback_query(F.data.startswith("sr:"))
async def cb_server(c: CallbackQuery):
    n = num(c.data.split(":")[1])
    try:
        servers = await fetch_servers(force=True)
    except Exception:
        logging.exception("fetch failed")
        await c.answer("Не удалось получить данные", show_alert=True)
        return
    if not 1 <= n <= len(servers):
        await c.answer("Такого сервера нет", show_alert=True)
        return
    await render(c, server_card(n, servers[n - 1]), card_kb(n), True)


@dp.callback_query(F.data.startswith("bc:"))
async def cb_biz_cat(c: CallbackQuery):
    _, ci, page = c.data.split(":")
    await show_biz_cat(c, num(ci), num(page))


@dp.callback_query(F.data.startswith("ad:"))
async def cb_alert_del(c: CallbackQuery):
    run("DELETE FROM alerts WHERE id=? AND chat=?", (num(c.data.split(":")[1]), chat_of(c)))
    await show_screen(c, "msg")


@dp.callback_query(F.data.startswith("xd:"))
async def cb_x2_del(c: CallbackQuery):
    run("DELETE FROM x2subs WHERE id=? AND chat=?", (num(c.data.split(":")[1]), chat_of(c)))
    await show_screen(c, "msg")


@dp.callback_query(F.data.startswith("dg:"))
async def cb_digest(c: CallbackQuery):
    arg = c.data.split(":", 1)[1]
    chat = chat_of(c)
    if arg == "off":
        run("DELETE FROM digest WHERE chat=?", (chat,))
    elif arg.isdigit() and 0 <= int(arg) <= 23:
        set_digest(chat, int(arg))
    await show_screen(c, "dg")


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
        BotCommand(command="online", description="Онлайн серверов"),
        BotCommand(command="top", description="Топ по онлайну"),
        BotCommand(command="free", description="Самые свободные серверы"),
        BotCommand(command="fav", description="Избранное"),
        BotCommand(command="graph", description="График онлайна"),
        BotCommand(command="business", description="Бизнес на карте"),
        BotCommand(command="message", description="Уведомление об онлайне"),
        BotCommand(command="x2", description="Уведомление про x2"),
        BotCommand(command="digest", description="Ежедневная сводка"),
        BotCommand(command="invite", description="Пригласить друзей"),
        BotCommand(command="help", description="Помощь"),
    ])


async def main():
    global _session, BOT_USERNAME
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    if not PHOTO_PATH.exists():
        logging.warning("brtools.png не найден: %s — ответы пойдут без фото", PHOTO_PATH)
    task = None
    try:
        try:
            BOT_USERNAME = (await bot.get_me()).username
        except Exception:
            logging.exception("get_me failed")
        try:
            await set_commands()
        except Exception:
            logging.exception("set_my_commands failed")
        task = asyncio.create_task(watch_loop())
        await dp.start_polling(bot)
    finally:
        if task:
            task.cancel()
        await _session.close()


if __name__ == "__main__":
    asyncio.run(main())
