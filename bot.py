
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
from aiogram.types import Message, BufferedInputFile

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.environ["BOT_TOKEN"]
API_URL = os.getenv(
    "API_URL",
    "https://blackrussia.online/api/gameservers/"
)
IMAGE_URL = (
    "https://raw.githubusercontent.com/"
    "pfqj9vwvnm-prog/Jj/main/brtools.png"
)
CACHE_TTL = 15

bot = Bot(
    BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML)
)
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session = None


async def fetch_servers():
    async with _lock:
        if (
            _cache["data"]
            and time.monotonic() - _cache["ts"] < CACHE_TTL
        ):
            return _cache["data"]

        try:
            async with _session.get(API_URL) as r:
                r.raise_for_status()
                data = await r.json(content_type=None)

            data = [
                s for s in data
                if s.get("publish_status", "published") == "published"
            ]
            data.sort(key=lambda s: s.get("id", 0))

            _cache.update(
                ts=time.monotonic(),
                data=data
            )
            return data

        except Exception:
            logging.exception("API error")
            if _cache["data"]:
                return _cache["data"]
            raise


def build_text(servers):
    lines = []
    total = 0

    for i, s in enumerate(servers, 1):
        name = html.escape(str(s.get("name", "?")))
        online = int(s.get("online") or 0)
        maximum = int(s.get("max_online") or 0)
        total += online

        x = " x2" if s.get("x2") else ""
        lines.append(
            f"{i}. {name}: {online}/{maximum}{x}"
        )

    return (
        "<b>Онлайн серверов:</b>\n"
        f"<blockquote expandable>{chr(10).join(lines)}</blockquote>\n"
        f"<b>Общий онлайн: {total}</b>"
    )


async def send_online(m: Message):
    try:
        text = build_text(await fetch_servers())

        # Загружаем изображение сами, а не просим Telegram
        # скачивать его с GitHub.
        async with _session.get(IMAGE_URL) as response:
            response.raise_for_status()
            image_data = await response.read()

        photo = BufferedInputFile(
            image_data,
            filename="brtools.png"
        )

        if len(text) <= 1024:
            await m.answer_photo(
                photo=photo,
                caption=text
            )
        else:
            await m.answer_photo(
                photo=photo,
                caption="<b>Онлайн серверов:</b>"
            )
            await m.answer(text)

    except Exception:
        logging.exception("Photo or online error")

        try:
            await m.answer(
                build_text(await fetch_servers())
            )
        except Exception:
            logging.exception("Online error")
            await m.answer(
                "Не удалось получить онлайн, попробуй позже."
            )


@dp.message(CommandStart())
async def start(m: Message):
    await send_online(m)


@dp.message(Command("online"))
@dp.message(
    F.text.func(
        lambda t: t.strip().lower() == "онлайн"
    )
)
async def online(m: Message):
    await send_online(m)


async def main():
    global _session

    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        }
    )

    try:
        await dp.start_polling(bot)
    finally:
        await _session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
