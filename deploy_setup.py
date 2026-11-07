"""One-time setup for the Vercel + Supabase deployment. Run locally:

  .venv/bin/python deploy_setup.py secrets   # adds WEBHOOK_SECRET / CRON_SECRET to .env
  .venv/bin/python deploy_setup.py db        # creates tables and the video storage bucket
  .venv/bin/python deploy_setup.py webhook   # points Telegram at PUBLIC_URL, menu button, commands
  .venv/bin/python deploy_setup.py warm      # Supabase pings the bot every 4 min: no slow cold starts
  .venv/bin/python deploy_setup.py check     # shows what Telegram knows about the webhook
"""
import asyncio
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv, set_key

ENV = Path(__file__).parent / ".env"


def cmd_secrets() -> None:
    load_dotenv(ENV)
    import os

    for name in ("WEBHOOK_SECRET", "CRON_SECRET"):
        if os.getenv(name):
            print(f"{name} уже есть в .env")
        else:
            set_key(str(ENV), name, secrets.token_urlsafe(32), quote_mode="never")
            print(f"{name} добавлен в .env")


async def cmd_db() -> None:
    from core import config, services

    if not config.DATABASE_URL.startswith(("postgres://", "postgresql://")):
        sys.exit("DATABASE_URL в .env должен быть строкой подключения Supabase (postgresql://...)")
    db = services.db()  # creates the tables
    print("Таблицы готовы")
    if not (config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY):
        sys.exit("Нет SUPABASE_URL / SUPABASE_SERVICE_KEY в .env")
    await services.storage().create_bucket()
    print(f"Хранилище «{config.STORAGE_BUCKET}» готово")
    print("Кружков в базе:", db.one("SELECT COUNT(*) AS n FROM circles")["n"])


async def cmd_webhook() -> None:
    from aiogram.types import MenuButtonWebApp, WebAppInfo

    from core import config, services
    from core.handlers import COMMANDS

    if not config.PUBLIC_URL.startswith("https://"):
        sys.exit("Впиши в .env PUBLIC_URL=https://<твой-проект>.vercel.app")
    if not config.WEBHOOK_SECRET:
        sys.exit("Нет WEBHOOK_SECRET: сначала запусти  deploy_setup.py secrets")
    bot = services.bot()
    await bot.set_webhook(
        f"{config.PUBLIC_URL}/api/telegram",
        secret_token=config.WEBHOOK_SECRET,
        max_connections=1,  # one update at a time: the bot's state never races
        allowed_updates=["message", "callback_query"],
    )
    await bot.set_my_commands(COMMANDS)
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(text="Галерея", web_app=WebAppInfo(url=config.PUBLIC_URL + "/"))
    )
    print("Webhook:", f"{config.PUBLIC_URL}/api/telegram")
    await cmd_check(bot)
    await bot.session.close()


def cmd_warm() -> None:
    """pg_cron job in Supabase that pings the bot every 4 minutes so Vercel keeps it warm."""
    from core import config, services

    if not config.PUBLIC_URL.startswith("https://"):
        sys.exit("Нет PUBLIC_URL в .env")
    db = services.db()
    db.ex("CREATE EXTENSION IF NOT EXISTS pg_cron")
    db.ex("CREATE EXTENSION IF NOT EXISTS pg_net")
    url = f"{config.PUBLIC_URL}/api/ping"
    db.ex("SELECT cron.schedule('keep-bot-warm', '*/4 * * * *', ?)", (f"SELECT net.http_get('{url}')",))
    print("Задача keep-bot-warm:", db.one("SELECT schedule, command FROM cron.job WHERE jobname = 'keep-bot-warm'"))


async def cmd_check(bot=None) -> None:
    from core import services

    own = bot is None
    bot = bot or services.bot()
    info = await bot.get_webhook_info()
    print("URL:", info.url or "(не задан)")
    print("Ожидают обработки:", info.pending_update_count)
    if info.last_error_message:
        print("Последняя ошибка:", info.last_error_message)
    if own:
        await bot.session.close()


if __name__ == "__main__":
    load_dotenv(ENV)
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "secrets":
        cmd_secrets()
    elif what == "db":
        asyncio.run(cmd_db())
    elif what == "webhook":
        asyncio.run(cmd_webhook())
    elif what == "warm":
        cmd_warm()
    elif what == "check":
        asyncio.run(cmd_check())
    else:
        print(__doc__)
