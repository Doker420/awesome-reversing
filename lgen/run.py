"""
Единый скрипт запуска LGen (Telegram Bot + FastAPI REST API)

Запуск:
    python run.py

Опции:
    python run.py --all          # Запустить и бота, и API сервер (по умолчанию)
    python run.py --bot          # Запустить только Telegram бота
    python run.py --api          # Запустить только REST API сервер
    python run.py --port 8080    # Указать свой порт для API
"""

import sys
import os
import argparse
import asyncio
import logging
import uvicorn
from contextlib import suppress

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("LGen-Runner")


async def run_bot():
    """Запуск Telegram-бота."""
    logger.info("🤖 Запуск Telegram-бота...")
    from bot import bot, dp, on_startup, on_shutdown

    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


async def run_api(host: str = "0.0.0.0", port: int = 8000):
    """Запуск FastAPI REST API сервера."""
    logger.info(f"🌐 Запуск REST API на http://{host}:{port} (Доки: http://localhost:{port}/docs)...")
    from api_server import app

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main():
    parser = argparse.ArgumentParser(description="LGen Unified Runner")
    parser.add_argument("--bot", action="store_true", help="Запустить только Telegram-бота")
    parser.add_argument("--api", action="store_true", help="Запустить только REST API")
    parser.add_argument("--all", action="store_true", help="Запустить бота и API одновременно (по умолчанию)")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="Хост для API сервера (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8000, help="Порт для API сервера (default: 8000)")

    args = parser.parse_args()

    # Создаём базовые каталоги, если их нет
    os.makedirs("cookies", exist_ok=True)
    os.makedirs("generated", exist_ok=True)

    tasks = []

    if args.bot:
        tasks.append(run_bot())
    elif args.api:
        tasks.append(run_api(host=args.host, port=args.port))
    else:
        # По умолчанию запускаем оба сервиса
        logger.info("🚀 Запуск всех сервисов (Telegram Bot + REST API)...")
        tasks.append(run_bot())
        tasks.append(run_api(host=args.host, port=args.port))

    try:
        await asyncio.gather(*tasks)
    except (KeyboardInterrupt, asyncio.CancelledError):
        logger.info("🛑 Остановка сервисов по сигналу завершения...")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Выход завершён.")
        sys.exit(0)
