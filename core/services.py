"""Shared objects: database, video storage, bot. Created lazily and reused while
a serverless instance stays warm."""
import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from core import config
from core.db import DB
from core.storage import LocalStorage, SupabaseStorage

_db: DB | None = None
_storage = None
_bot: Bot | None = None
_bot_loop = None
_bot_pinned = False


def db() -> DB:
    global _db
    if _db is None:
        _db = DB(config.DATABASE_URL)
    return _db


def storage():
    global _storage
    if _storage is None:
        if config.SUPABASE_URL and config.SUPABASE_SERVICE_KEY:
            _storage = SupabaseStorage(config.SUPABASE_URL, config.SUPABASE_SERVICE_KEY, config.STORAGE_BUCKET)
        else:
            _storage = LocalStorage("media")
    return _storage


def bot() -> Bot:
    """The bot's HTTP session is tied to an event loop; make a new bot if the loop changed."""
    global _bot, _bot_loop
    loop = asyncio.get_running_loop()
    if _bot is None or (_bot_loop is not loop and not _bot_pinned):
        from core.handlers import TrackSent  # avoid an import cycle

        _bot = Bot(config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        _bot.session.middleware(TrackSent())
        _bot_loop = loop
    return _bot


def override(db_obj=None, storage_obj=None, bot_obj=None) -> None:
    """For tests."""
    global _db, _storage, _bot, _bot_loop
    if db_obj is not None:
        _db = db_obj
    if storage_obj is not None:
        _storage = storage_obj
    global _bot_pinned
    if bot_obj is not None:
        _bot = bot_obj
        _bot_pinned = True
