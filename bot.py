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
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, LinkPreviewOptions, Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "https://blackrussia.online/api/gameservers/")
CACHE_TTL = int(os.getenv("CACHE_TTL", "15"))  # секунд

# Путь к фото в репозитории (рядом с bot.py)
PHOTO_PATH = Path(__file__).with_name("brtools.png")
# Опционально: прямая ссылка на raw-файл из GitHub, если нужно фото + длинный текст в 1 сообщении
PHOTO_URL = os.getenv("PHOTO_URL")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None
_photo_file_id: str | None = None


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
        name = html.escape(str(s.get("name", "?")))
        online = int(s.get("online") or 0)
        mx = int(s.get("max_online") or 0)
        total += online
        x = " x2" if s.get("x2") else ""
        lines.append(f"{i}. {name}: {online}/{mx}{x}")
    return (
        "<b>Онлайн серверов:</b>\n"
        f"<blockquote expandable>{chr(10).join(lines)}</blockquote>\n"
        f"<b>Общий онлайн: {total}</b>"
    )


async def send_with_photo(m: Message, text: str):
    global _photo_file_id

    # Вариант 1: Если задана прямая ссылка PHOTO_URL — отправляем одним сообщением (до 4096 символов)
    if PHOTO_URL:
        await m.answer(
            text,
            link_preview_options=LinkPreviewOptions(
                url=PHOTO_URL,
                prefer_large_media=True,
                show_above_text=True,
            ),
        )
        return

    # Вариант 2: Отправка локального файла brtools.png из репозитория
    if not PHOTO_PATH.exists():
        await m.answer(text)
        return

    photo = _photo_file_id or FSInputFile(PHOTO_PATH)
    try:
        sent = await m.answer_photo(photo=photo, caption=text)
        if sent.photo:
            _photo_file_id = sent.photo[-1].file_id
    except TelegramBadRequest as e:
        # Если текст длиннее 1024 символов (лимит подписи к фото в Telegram)
        if "caption is too long" in str(e):
            sent = await m.answer_photo(photo=photo)
            if sent.photo:
                _photo_file_id = sent.photo[-1].file_id
            await m.answer(text)
        else:
            raise


@dp.message(CommandStart())
@dp.message(Command("online"))
@dp.message(F.text.func(lambda t: t.strip().lower() == "онлайн"))
async def online(m: Message):
    try:
        text = build_text(await fetch_servers())
        await send_with_photo(m, text)
    except Exception:
        logging.exception("online failed")
        await m.answer("Не удалось получить онлайн, попробуй позже.")


async def main():
    global _session
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=10),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        await dp.start_polling(bot)
    finally:
        await _session.close()


if __name__ == "__main__":
    asyncio.run(main())
