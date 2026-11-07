import asyncio
import html
import logging
import os
import time
from pathlib import Path

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "https://blackrussia.online/api/gameservers/")
CACHE_TTL = int(os.getenv("CACHE_TTL", "15"))  # секунд

PHOTO_PATH = Path(__file__).parent / "brtools.png"
CAPTION_LIMIT = 1024  # лимит Telegram на подпись к фото

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None


async def fetch_servers() -> list[dict]:
    async with _lock:
        if _cache["data"] and time.monotonic() - _cache["ts"] < CACHE_TTL:
            return _cache["data"]
        try:
            async with _session.get(API_URL) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)
        except Exception:
            if _cache["data"]:  # сайт моргнул — отдаём последние данные
                logging.exception("fetch failed, using stale cache")
                return _cache["data"]
            raise
        data = [s for s in data if s.get("publish_status", "published") == "published"]
        data.sort(key=lambda s: s.get("id", 0))
        _cache.update(ts=time.monotonic(), data=data)
        return data


def build_text(servers: list[dict]) -> str:
    lines, total = [], 0
    for i, s in enumerate(servers, 1):
        name = str(s.get("name", "?"))
        online = int(s.get("online") or 0)
        mx = int(s.get("max_online") or 0)
        total += online
        x = " x2" if s.get("x2") else ""
        lines.append(f"{i}. {name}: {online}/{mx}{x}")

    header = "Онлайн серверов:\n"
    footer = f"\nОбщий онлайн: {total}"

    # Telegram считает длину подписи по видимому тексту (без HTML-тегов).
    # Если
