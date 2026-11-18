import asyncio
import ipaddress
import os
import re
import socket
from collections import defaultdict, deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import aiohttp
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup

TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("Set BOT_TOKEN in your environment (see .env.example).")

bot = Bot(token=TOKEN)
dp = Dispatcher()

keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🔎 Проверить домен или IP")], [KeyboardButton(text="🛡 Конфиденциальность"), KeyboardButton(text="ℹ️ Помощь")]],
    resize_keyboard=True,
)
# In-memory rate limiting only; no query history is persisted.
recent_requests: dict[int, deque[float]] = defaultdict(deque)
MAX_REQUESTS_PER_MINUTE = 5


def normalize_target(text: str) -> str | None:
    candidate = text.strip()
    if candidate.startswith(("http://", "https://")):
        candidate = urlparse(candidate).hostname or ""
    candidate = candidate.rstrip(".").lower()
    if len(candidate) > 253 or not candidate:
        return None
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    if not re.fullmatch(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", candidate):
        return None
    return candidate


def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return ip.is_global


async def resolve_domain(domain: str) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        records = await asyncio.wait_for(loop.getaddrinfo(domain, None, type=socket.SOCK_STREAM), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return []
    return sorted({record[4][0] for record in records})


async def public_ip_details(ip: str) -> str:
    if not is_public_ip(ip):
        return f"{ip} — частный или служебный IP-адрес; публичная геоинформация не запрашивалась."
    timeout = aiohttp.ClientTimeout(total=7)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://ipwho.is/{ip}") as response:
                data = await response.json(content_type=None)
        if not data.get("success", False):
            return f"{ip} — публичный IP; подробности недоступны."
        parts = [ip]
        location = ", ".join(filter(None, [data.get("city"), data.get("region"), data.get("country")]))
        if location:
            parts.append(f"Регион: {location}")
        connection = data.get("connection") or {}
        if connection.get("isp"):
            parts.append(f"Провайдер: {connection['isp']}")
        parts.append("Источник: ipwho.is")
        return "\n".join(parts)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return f"{ip} — публичный IP; сервис справочной информации временно недоступен."


@dp.message(CommandStart())
async def start(message: Message) -> None:
    await message.answer(
        "Привет! Я бесплатный бот для базовой проверки доменов и публичных IP-адресов.\n\n"
        "Отправьте домен (например, example.com), IP или URL. Я покажу DNS-адреса и, для публичных IP, общедоступную справочную информацию.\n\n"
        "Я не ищу людей, телефоны, документы, аккаунты или данные из утечек и не сканирую порты. Запросы не сохраняются.",
        reply_markup=keyboard,
    )


@dp.message(Command("help"))
@dp.message(lambda message: message.text == "ℹ️ Помощь")
async def help_message(message: Message) -> None:
    await message.answer(
        "Как пользоваться: отправьте домен (example.com), публичный IP или URL.\n"
        "Доступны команды /start, /help и /privacy. Бесплатно, без подписки.\n"
        "Проверяются только DNS и базовая публичная информация об IP; данные о частных лицах не ищутся."
    )


@dp.message(Command("privacy"))
@dp.message(lambda message: message.text == "🛡 Конфиденциальность")
async def privacy(message: Message) -> None:
    await message.answer(
        "Конфиденциальность: бот не сохраняет историю запросов или профиль пользователя. "
        "Временный лимит запросов хранится только в памяти и сбрасывается при перезапуске. "
        "Для справки о публичном IP адрес передаётся сервису ipwho.is. Не отправляйте секреты или персональные данные."
    )


@dp.message(lambda message: message.text == "🔎 Проверить домен или IP")
async def prompt_target(message: Message) -> None:
    await message.answer("Отправьте домен, публичный IP или URL для проверки.")


@dp.message()
async def lookup(message: Message) -> None:
    if not message.text:
        await message.answer("Отправьте текстовый домен, IP или URL.")
        return
    target = normalize_target(message.text)
    if not target:
        await message.answer("Не удалось распознать домен или IP. Пример: example.com или https://example.com")
        return

    now = datetime.now(timezone.utc).timestamp()
    queue = recent_requests[message.from_user.id]
    while queue and now - queue[0] >= 60:
        queue.popleft()
    if len(queue) >= MAX_REQUESTS_PER_MINUTE:
        await message.answer("Слишком много запросов. Попробуйте через минуту.")
        return
    queue.append(now)

    if not isinstance(ipaddress.ip_address(target) if _is_ip(target) else None, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        addresses = await resolve_domain(target)
        if not addresses:
            await message.answer(f"Для {target} DNS-адреса не найдены.")
            return
        lines = [f"Домен: {target}", "DNS-адреса:"]
        for address in addresses[:8]:
            lines.append("• " + await public_ip_details(address))
        await message.answer("\n".join(lines))
    else:
        await message.answer(await public_ip_details(target))


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return False


async def main() -> None:
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
