import asyncio
import html
import logging
import os
import time

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, LinkPreviewOptions, Message

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv("API_URL", "https://blackrussia.online/api/gameservers/")
CACHE_TTL = int(os.getenv("CACHE_TTL", "15"))  # секунд
PHOTO_PATH = os.getenv("PHOTO_PATH", "brtools.png")
PHOTO_URL = os.getenv("PHOTO_URL", "")  # Прямая ссылка на фото (позволяет прикрепить фото к длинному тексту в 1 сообщение)

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


@dp.message(CommandStart())
@dp.message(Command("online"))
@dp.message(F.text.func(lambda t: t.strip().lower() == "онлайн"))
async def send_online(m: Message):
    try:
        servers = await fetch_servers()
        text = build_text(servers)

        # Вариант 1: Задана ссылка на фото (PHOTO_URL).
        # Telegram прикрепляет фото сверху большим баннером В ОДНОМ сообщении без лимита на 1024 символа.
        if PHOTO_URL:
            await m.answer(
                f'<a href="{PHOTO_URL}">&#8203;</a>' + text,
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    url=PHOTO_URL,
                    prefer_large_media=True,
                    show_above_text=True,
                ),
            )
        # Вариант 2: Локальный файл brtools.png.
        elif os.path.exists(PHOTO_PATH):
            if len(text) <= 1024:
                # Если текст влезает в лимит Telegram (до 1024 символов) — отправляем фото с подписью
                await m.answer_photo(FSInputFile(PHOTO_PATH), caption=text)
            else:
                # Текст серверов длиннее 1024 символов. Чтобы фото не пропадало, отправляем фото и следом текст:
                await m.answer_photo(FSInputFile(PHOTO_PATH))
                await m.answer(text)
        # Вариант 3: Фото нет вообще — отправляем только текст
        else:
            await m.answer(text)
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
