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

# Если есть ссылка на баннер, бот прикрепит фото прямо сверху сообщения в 1 пузырь.
# Если оставить пустым, бот сам автоматически загрузит brtools.png при старте.
PHOTO_URL = os.getenv("PHOTO_URL", "")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()

_cache = {"ts": 0.0, "data": None}
_lock = asyncio.Lock()
_session: aiohttp.ClientSession | None = None


async def get_photo_url() -> str:
    """Возвращает прямую ссылку на фото. Если её нет, автоматически выгружает brtools.png на хостинг."""
    global PHOTO_URL
    if PHOTO_URL:
        return PHOTO_URL
    if not os.path.exists(PHOTO_PATH) or not _session:
        return ""

    for host in ["catbox", "0x0"]:
        try:
            data = aiohttp.FormData()
            if host == "catbox":
                data.add_field("reqtype", "fileupload")
                with open(PHOTO_PATH, "rb") as f:
                    data.add_field("fileToUpload", f.read(), filename="brtools.png")
                url = "https://catbox.moe/user/api.php"
            else:
                with open(PHOTO_PATH, "rb") as f:
                    data.add_field("file", f.read(), filename="brtools.png")
                url = "https://0x0.st"

            async with _session.post(url, data=data, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    link = (await resp.text()).strip()
                    if link.startswith("http"):
                        PHOTO_URL = link
                        logging.info("Баннер успешно загружен: %s", PHOTO_URL)
                        return PHOTO_URL
        except Exception as e:
            logging.warning("Не удалось автоматически загрузить баннер на %s: %s", host, e)
    return ""


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
        photo_url = await get_photo_url()

        # Прикрепляем фото прямо к сообщению (баннер сверху текста, ОДНО сообщение)
        if photo_url:
            await m.answer(
                f'<a href="{photo_url}">&#8203;</a>' + text,
                link_preview_options=LinkPreviewOptions(
                    is_disabled=False,
                    url=photo_url,
                    prefer_large_media=True,
                    show_above_text=True,
                ),
            )
        elif os.path.exists(PHOTO_PATH):
            if len(text) <= 1024:
                await m.answer_photo(FSInputFile(PHOTO_PATH), caption=text)
            else:
                await m.answer_photo(FSInputFile(PHOTO_PATH))
                await m.answer(text)
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
