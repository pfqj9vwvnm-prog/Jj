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
API_URL = os.getenv("API_URL", "https://blackrussia.online/api/gameservers/")
IMAGE_URL = "https://raw.githubusercontent.com/pfqj9vwvnm-prog/Jj/main/brtools.png"
CACHE_TTL = int(os.getenv("CACHE_TTL", "15"))

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None


async def fetch_servers() -> list[dict]:
    async with _lock:
        if _cache["data"] is not None and time.monotonic() - _cache["ts"] < CACHE_TTL:
            return _cache["data"]

        try:
            async with _session.get(API_URL) as response:
                response.raise_for_status()
                data = await response.json(content_type=None)
        except Exception:
            if _cache["data"] is not None:
                logging.exception("API request failed; using cached data")
                return _cache["data"]
            raise

        data = [
            server for server in data
            if server.get("publish_status", "published") == "published"
        ]
        data.sort(key=lambda server: server.get("id", 0))
        _cache.update(ts=time.monotonic(), data=data)
        return data


def build_caption(servers: list[dict]) -> str:
    """Build one compact photo caption within Telegram's 1024-character limit."""
    total = sum(int(server.get("online") or 0) for server in servers)
    header = f"<b>Онлайн серверов:</b>\n"
    footer = f"\n<b>Общий онлайн: {total}</b>"

    lines = []
    for server in servers:
        name = html.escape(str(server.get("name", "?")))
        online = int(server.get("online") or 0)
        maximum = int(server.get("max_online") or 0)
        multiplier = " x2" if server.get("x2") else ""
        lines.append(f"{name}: {online}/{maximum}{multiplier}")

    # Reserve space for the heading, total and a possible "more servers" line.
    selected = []
    for index, line in enumerate(lines):
        remaining = len(lines) - index - 1
        more = f"\n… и ещё {remaining} сервер(ов)" if remaining else ""
        candidate = header + "\n".join(selected + [line]) + more + footer
        if len(candidate) > 1000:
            break
        selected.append(line)

    remaining = len(lines) - len(selected)
    more = f"\n… и ещё {remaining} сервер(ов)" if remaining else ""
    caption = header + "\n".join(selected) + more + footer

    # Defensive fallback for unusually long server names.
    if len(caption) > 1024:
        caption = f"<b>Онлайн серверов</b>\n<b>Общий онлайн: {total}</b>"
    return caption


async def send_online(message: Message):
    try:
        servers = await fetch_servers()
        caption = build_caption(servers)

        # Download the image first, then send photo and online in ONE Telegram message.
        async with _session.get(IMAGE_URL) as response:
            response.raise_for_status()
            image_bytes = await response.read()

        photo = BufferedInputFile(image_bytes, filename="brtools.png")
        await message.answer_photo(photo=photo, caption=caption)

    except Exception:
        logging.exception("Could not send photo with online")
        try:
            # Fallback only if the image cannot be downloaded/sent.
            await message.answer(build_caption(await fetch_servers()))
        except Exception:
            logging.exception("Could not get server online")
            await message.answer("Не удалось получить онлайн, попробуй позже.")


@dp.message(CommandStart())
async def start(message: Message):
    await send_online(message)


@dp.message(Command("online"))
@dp.message(F.text.func(lambda text: text.strip().lower() == "онлайн"))
async def online(message: Message):
    await send_online(message)


async def main():
    global _session
    _session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=20),
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
    )
    try:
        await dp.start_polling(bot)
    finally:
        await _session.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
