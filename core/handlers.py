"""Bot logic: saving circles, tagging, listings, clean-up.

Runs on Vercel, where every Telegram update is a separate short-lived call, so nothing is
kept in memory between messages: the tagging prompt, the open batch etc. live in user_state.
Telegram delivers updates one at a time (webhook max_connections=1), so they never race.
"""
import asyncio
import html
import logging
import re
import time
from contextvars import ContextVar
from datetime import date, datetime, timedelta, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    WebAppInfo,
)

from core import config, services, timing
from core.db import normalize_tag
from core.storage import media_path

PAGE_SIZE = 10          # circles sent per page
BATCH_WINDOW = 120      # circles forwarded within this many seconds join one batch
TAGS_IN_KEYBOARD = 40   # max tag buttons in the tagging keyboard
SEND_DELAY = 0.05       # small pause between sent circles (flood limits are handled via RetryAfter)
DELETE_WINDOW = 47 * 3600  # Telegram lets bots delete messages only for 48 hours
TZ = timezone(timedelta(hours=3))  # dates in listings are shown in Moscow time

log = logging.getLogger("circles")


async def _await(aw):
    return await aw


async def together(*aws, return_exceptions: bool = False):
    """Run several Telegram calls at once (aiogram's method objects aren't plain coroutines)."""
    return await asyncio.gather(*(_await(a) for a in aws), return_exceptions=return_exceptions)


class _DB:
    """`db.method()` → the shared database (created on first use)."""

    def __getattr__(self, name):
        return getattr(services.db(), name)


db = _DB()
router = Router()
if config.ALLOWED_USERS:
    router.message.filter(F.from_user.id.in_(config.ALLOWED_USERS))
    router.callback_query.filter(F.from_user.id.in_(config.ALLOWED_USERS))


# ---------------------------------------------------------------- message tracking
# Every message in the bot chat is remembered so it can be cleaned up later.
# Messages sent while a circle listing is being shown get that listing's group id.
view_group: ContextVar[str | None] = ContextVar("view_group", default=None)


class TrackSent(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        t0 = time.perf_counter()
        try:
            result = await make_request(bot, method)
        finally:
            timing.add("tg", time.perf_counter() - t0)
        if isinstance(result, Message):
            db.track_message(result.chat.id, result.message_id, view_group.get())
        return result


async def track_incoming(handler, event: Message, data):
    db.track_message(event.chat.id, event.message_id)
    return await handler(event, data)


router.message.outer_middleware(track_incoming)


async def delete_many(bot: Bot, chat_id: int, ids: list[int]) -> None:
    for i in range(0, len(ids), 100):  # API limit per call
        try:
            await bot.delete_messages(chat_id, ids[i:i + 100])
        except TelegramBadRequest as e:
            log.warning("delete_messages failed: %s", e)


# ---------------------------------------------------------------- state helpers
def get_current(uid: int) -> tuple[str, int, int] | None:
    st = db.state(uid)
    return (st["cur_target"], st["cur_chat"], st["cur_msg"]) if st["cur_target"] else None


def set_current(uid: int, target: str, chat_id: int, msg_id: int) -> None:
    db.set_state(uid, cur_target=target, cur_chat=chat_id, cur_msg=msg_id)


def clear_current(uid: int, only: str | None = None) -> None:
    cur = get_current(uid)
    if cur and (only is None or cur[0] == only):
        db.set_state(uid, cur_target=None, cur_chat=None, cur_msg=None)


async def remove_circles(uid: int, circle_ids: list[int]) -> None:
    """Delete circles from the database and their copies from the video storage."""
    rows = db.get_circles(uid, circle_ids)
    for c in rows:
        db.delete_circle(uid, c["id"])
    paths = [media_path(c, k) for c in rows for k in ("v", "t")]
    try:
        await services.storage().delete(paths)
    except Exception as e:  # leftovers in storage are harmless
        log.warning("storage delete failed: %s", e)


# A "target" is what a tagging keyboard edits: "b<batch_id>" or "c<circle_id>".
def target_circles(user_id: int, target: str) -> list[int]:
    kind, num = target[0], int(target[1:])
    if kind == "b":
        return db.batch_circle_ids(user_id, num)
    circle = db.get_circle(user_id, num)
    return [circle["id"]] if circle else []


def split_tags(text: str) -> list[str]:
    out: list[str] = []
    for p in re.split(r"[,\n;]+", text):
        name = normalize_tag(p)
        if name and name not in out:
            out.append(name)
    return out


def fmt_tags(names: list[str]) -> str:
    return ", ".join(f"<b>{html.escape(n)}</b>" for n in names) if names else "<i>нет</i>"


# ---------------------------------------------------------------- keyboards
def tagging_view(user_id: int, target: str) -> tuple[str, InlineKeyboardMarkup] | None:
    circle_ids = target_circles(user_id, target)
    if not circle_ids:
        return None
    on = db.tags_on_all(circle_ids)
    tags = db.list_tags(user_id)
    head = f"📥 Сохранено кружков: <b>{len(circle_ids)}</b>" if target.startswith("b") else "🏷 Теги кружка"
    on_names = [t["name"] for t in tags if t["id"] in on]
    text = (
        f"{head}\nТеги: {fmt_tags(on_names)}\n\n"
        "Нажимай на теги или напиши новые через запятую, например:\n"
        "<code>поездка в армению, горы</code>"
    )
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for t in tags[:TAGS_IN_KEYBOARD]:
        mark = "✅ " if t["id"] in on else ""
        row.append(InlineKeyboardButton(text=f"{mark}{t['name']}", callback_data=f"tg:{target}:{t['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([
        InlineKeyboardButton(text="✔️ Готово", callback_data=f"ok:{target}"),
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"rm:{target}"),
    ])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def order_label(newest: bool) -> str:
    return "сначала новые" if newest else "сначала старые"


def tags_list_view(user_id: int) -> tuple[str, InlineKeyboardMarkup]:
    total, untagged = db.count_circles(user_id)
    tags = [t for t in db.list_tags(user_id) if t["cnt"] > 0]
    if total == 0:
        return "Пока пусто. Перешли мне кружок, и я его сохраню.", InlineKeyboardMarkup(inline_keyboard=[])
    rows = [[InlineKeyboardButton(text=f"{t['name']} · {t['cnt']}", callback_data=f"sh:{t['id']}:0")] for t in tags]
    extra = [InlineKeyboardButton(text=f"Все · {total}", callback_data="sh:all:0")]
    if untagged:
        extra.append(InlineKeyboardButton(text=f"Без тегов · {untagged}", callback_data="sh:none:0"))
    rows.append(extra)
    rows.append([InlineKeyboardButton(text=f"🔃 Порядок: {order_label(db.newest_first(user_id))}", callback_data="ord")])
    text = f"🗂 Тегов: <b>{len(tags)}</b>, кружков: <b>{total}</b>\nВыбери, что показать:"
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def circle_kb(circle_id: int) -> InlineKeyboardMarkup:
    names = db.circle_tag_names(circle_id)
    label = "🏷 " + (", ".join(names) if names else "добавить теги")
    if len(label) > 45:
        label = label[:42] + "…"
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=label, callback_data=f"ed:{circle_id}"),
        InlineKeyboardButton(text="🗑", callback_data=f"dl:{circle_id}"),
    ]])


def confirm_delete_kb(circle_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🗑 Да, удалить", callback_data=f"dly:{circle_id}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"dln:{circle_id}"),
    ]])


# ---------------------------------------------------------------- helpers
async def safe_edit(msg: Message, text: str, kb: InlineKeyboardMarkup | None) -> None:
    try:
        await msg.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise


async def send_prompt(bot: Bot, user_id: int, chat_id: int, target: str) -> None:
    view = tagging_view(user_id, target)
    if not view:
        return
    msg = await bot.send_message(chat_id, view[0], reply_markup=view[1])
    set_current(user_id, target, chat_id, msg.message_id)


async def refresh_prompt(bot: Bot, user_id: int) -> None:
    cur = get_current(user_id)
    if not cur:
        return
    target, chat_id, msg_id = cur
    view = tagging_view(user_id, target)
    if not view:
        return
    for attempt in range(2):
        try:
            await bot.edit_message_text(view[0], chat_id=chat_id, message_id=msg_id, reply_markup=view[1])
            return
        except TelegramRetryAfter as e:  # many circles forwarded at once → many edits
            if attempt == 0:
                await asyncio.sleep(min(e.retry_after, 5))
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                await send_prompt(bot, user_id, chat_id, target)  # prompt gone: send a fresh one
            return


async def send_circle(bot: Bot, chat_id: int, circle) -> None:
    for _ in range(3):
        try:
            await bot.send_video_note(chat_id, circle["file_id"], reply_markup=circle_kb(circle["id"]))
            return
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)


MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]


def circle_day(circle) -> date:
    """Day the circle was recorded: original send date if forwarded, else when it was saved."""
    return datetime.fromtimestamp(circle["orig_date"] or circle["added_at"], TZ).date()


def fmt_day(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def parse_show_key(user_id: int, key: str) -> tuple[list[int] | None, bool, str] | None:
    """key -> (tag_ids, untagged, title)."""
    if key == "all":
        return None, False, "все кружки"
    if key == "none":
        return None, True, "без тегов"
    ids = [int(x) for x in key.lstrip("m").split("-")]
    names = []
    for tid in ids:
        t = db.get_tag(user_id, tid)
        if not t:
            return None
        names.append(t["name"])
    return ids, False, " + ".join(names)


def base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while n:
        n, r = divmod(n, 36)
        out = digits[r] + out
    return out or "0"


async def show_page(
    bot: Bot, user_id: int, chat_id: int, key: str, offset: int, newest: bool | None = None,
    grp: str | None = None, trigger: Message | None = None,
) -> None:
    """Send one page of a listing. All its messages share `grp`, so "🧹" removes them together."""
    grp = grp or base36(time.time_ns() // 1_000_000)  # short: callback data is limited to 64 bytes
    if trigger:
        db.track_message(trigger.chat.id, trigger.message_id, grp)
    token = view_group.set(grp)
    try:
        await _show_page(bot, user_id, chat_id, key, offset, newest, grp)
    finally:
        view_group.reset(token)


async def _show_page(bot: Bot, user_id: int, chat_id: int, key: str, offset: int, newest: bool | None, grp: str):
    parsed = parse_show_key(user_id, key)
    if not parsed:
        await bot.send_message(chat_id, "Такого тега больше нет. /tags")
        return
    tag_ids, untagged, title = parsed
    if newest is None:
        newest = db.newest_first(user_id)
    rows, total = db.find_circles(user_id, tag_ids, untagged, offset, PAGE_SIZE, newest)
    if total == 0:
        await bot.send_message(chat_id, f"По запросу «{html.escape(title)}» ничего нет.")
        return
    if offset == 0:
        await bot.send_message(chat_id, f"▶️ <b>{html.escape(title)}</b> · {total} · {order_label(newest)}")
    # Circles can't carry captions, so a date header is sent before each new day.
    last_day = None
    if offset > 0:
        prev, _ = db.find_circles(user_id, tag_ids, untagged, offset - 1, 1, newest)
        if prev:
            last_day = circle_day(prev[0])
    for c in rows:
        day = circle_day(c)
        if day != last_day:
            await bot.send_message(chat_id, f"📅 <b>{fmt_day(day)}</b>")
            last_day = day
        await send_circle(bot, chat_id, c)
        await asyncio.sleep(SEND_DELAY)
    shown = offset + len(rows)
    buttons = []
    if shown < total:
        buttons.append([InlineKeyboardButton(
            text=f"Ещё ({total - shown}) ▶️", callback_data=f"sh:{key}:{shown}:{'d' if newest else 'a'}:{grp}"
        )])
    buttons.append([InlineKeyboardButton(text="🧹 Убрать подборку из чата", callback_data=f"cl:{grp}")])
    await bot.send_message(
        chat_id, f"Показано {offset + 1}–{shown} из {total} · {html.escape(title)}",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


# ---------------------------------------------------------------- commands
HELP = (
    "Я храню твои кружки и раскладываю их по тегам.\n\n"
    "<b>Как сохранить</b>\n"
    "Перешли мне кружок (или сразу несколько), потом выбери теги кнопками "
    "или напиши их через запятую.\n\n"
    "<b>Как смотреть</b>\n"
    "/gallery или кнопка «Галерея» у поля ввода — галерея, где кружки можно листать свайпом\n"
    "/tags — все теги, нажми на тег, и я пришлю его кружки\n"
    "/show армения, горы — кружки, у которых есть <i>все</i> эти теги\n"
    "/all — все кружки\n"
    "/untagged — кружки без тегов\n"
    "/order — сначала старые / сначала новые (или кнопка 🔃 в /tags)\n\n"
    "<b>Чистота в чате</b>\n"
    "Под каждой подборкой есть кнопка 🧹, она убирает эту подборку из чата.\n"
    "/clear — очистить весь чат за последние 48 часов. Кружки в хранилище остаются.\n\n"
    "<b>Управление</b>\n"
    "Под каждым присланным кружком есть кнопки: 🏷 — поменять теги, 🗑 — удалить кружок.\n"
    "/rename старое -> новое — переименовать тег (если новое уже есть, теги сольются)\n"
    "/deltag имя — удалить тег (кружки останутся)"
)

COMMANDS = [
    BotCommand(command="gallery", description="Галерея кружков"),
    BotCommand(command="tags", description="Все теги"),
    BotCommand(command="show", description="Кружки по тегам: /show армения, горы"),
    BotCommand(command="all", description="Все кружки"),
    BotCommand(command="untagged", description="Кружки без тегов"),
    BotCommand(command="order", description="Сначала старые / сначала новые"),
    BotCommand(command="clear", description="Очистить чат (кружки останутся в хранилище)"),
    BotCommand(command="rename", description="Переименовать тег: /rename старое -> новое"),
    BotCommand(command="deltag", description="Удалить тег"),
    BotCommand(command="help", description="Как пользоваться"),
]


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message) -> None:
    await message.answer(HELP)


@router.message(Command("tags"))
async def cmd_tags(message: Message) -> None:
    text, kb = tags_list_view(message.from_user.id)
    await message.answer(text, reply_markup=kb)


@router.message(Command("all"))
async def cmd_all(message: Message) -> None:
    await show_page(message.bot, message.from_user.id, message.chat.id, "all", 0, trigger=message)


@router.message(Command("untagged"))
async def cmd_untagged(message: Message) -> None:
    await show_page(message.bot, message.from_user.id, message.chat.id, "none", 0, trigger=message)


@router.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    """Delete everything in the bot chat from the last 48 hours. Saved circles stay in storage."""
    ids = db.pop_messages(message.chat.id, None, int(time.time()) - DELETE_WINDOW)
    clear_current(message.from_user.id)
    await delete_many(message.bot, message.chat.id, sorted(set(ids) | {message.message_id}))


@router.message(Command("show"))
async def cmd_show(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    names = split_tags(command.args or "")
    if not names:
        await message.answer("Напиши теги через запятую: <code>/show армения, горы</code>")
        return
    ids = []
    for n in names:
        t = db.find_tag(uid, n)
        if not t:
            await message.answer(f"Тега «{html.escape(n)}» нет. Все теги: /tags")
            return
        ids.append(t["id"])
    await show_page(message.bot, uid, message.chat.id, "m" + "-".join(map(str, ids)), 0, trigger=message)


@router.message(Command("rename"))
async def cmd_rename(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    parts = re.split(r"\s*(?:->|→|=>)\s*", command.args or "", maxsplit=1)
    if len(parts) != 2:
        await message.answer("Формат: <code>/rename старое -> новое</code>")
        return
    old = db.find_tag(uid, parts[0])
    if not old:
        await message.answer(f"Тега «{html.escape(normalize_tag(parts[0]))}» нет.")
        return
    if not db.rename_tag(uid, old["id"], parts[1]):
        await message.answer("Новое имя пустое.")
        return
    await message.answer(f"Готово: {fmt_tags([old['name']])} → {fmt_tags([normalize_tag(parts[1])])}")


@router.message(Command("deltag"))
async def cmd_deltag(message: Message, command: CommandObject) -> None:
    uid = message.from_user.id
    t = db.find_tag(uid, command.args or "")
    if not t:
        await message.answer("Формат: <code>/deltag имя</code> (такого тега нет)")
        return
    db.delete_tag(uid, t["id"])
    await message.answer(f"Тег {fmt_tags([t['name']])} удалён, кружки остались.")


@router.message(Command("order"))
async def cmd_order(message: Message) -> None:
    uid = message.from_user.id
    db.set_newest_first(uid, not db.newest_first(uid))
    await message.answer(f"🔃 Порядок теперь: <b>{order_label(db.newest_first(uid))}</b>")


@router.message(Command("gallery"))
async def cmd_gallery(message: Message) -> None:
    if not config.PUBLIC_URL:
        await message.answer("Галерея не настроена: не задан PUBLIC_URL.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🎞 Открыть галерею", web_app=WebAppInfo(url=config.PUBLIC_URL + "/"))
    ]])
    await message.answer("Все твои кружки, с тегами и датами:", reply_markup=kb)


@router.message(Command("done"))
async def cmd_done(message: Message) -> None:
    clear_current(message.from_user.id)
    await message.answer("Ок.")


# ---------------------------------------------------------------- circles in
@router.message(F.video_note)
async def on_video_note(message: Message) -> None:
    """Circles forwarded together go into one batch with one tagging prompt.
    The first circle sends the prompt, the next ones update its counter."""
    uid = message.from_user.id
    vn = message.video_note
    origin = message.forward_origin
    orig_date = int((origin.date if origin else message.date).timestamp())
    now = int(time.time())

    st = db.state(uid)
    batch = st["open_batch"] if st["open_batch"] and now - (st["open_batch_at"] or 0) < BATCH_WINDOW else None
    if batch is None:
        batch = db.new_batch(uid)
    thumb = vn.thumbnail.file_id if vn.thumbnail else None
    db.add_circle(uid, vn.file_id, vn.file_unique_id, vn.duration, orig_date, batch, thumb)
    db.set_state(uid, open_batch=batch, open_batch_at=now)

    target = f"b{batch}"
    if st["cur_target"] == target:
        await refresh_prompt(message.bot, uid)
    else:
        await send_prompt(message.bot, uid, message.chat.id, target)


@router.message(F.video | F.animation)
async def on_video(message: Message) -> None:
    await message.answer("Это обычное видео, а я храню только кружки 🙂")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    uid = message.from_user.id
    cur = get_current(uid)
    if not cur:
        await message.answer("Перешли мне кружок, а потом напиши теги. Посмотреть сохранённое: /tags")
        return
    circle_ids = target_circles(uid, cur[0])
    names = split_tags(message.text)
    if not circle_ids or not names:
        return
    db.add_tag_to(circle_ids, [db.get_or_create_tag(uid, n) for n in names])
    # keep the chat tidy (the prompt shows the result) - both calls at once to save a round trip
    await together(message.delete(), refresh_prompt(message.bot, uid), return_exceptions=True)


# ---------------------------------------------------------------- buttons
@router.callback_query(F.data.startswith("tg:"))
async def cb_toggle(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    _, target, tag_id = cq.data.split(":")
    tag_id = int(tag_id)
    circle_ids = target_circles(uid, target)
    if not circle_ids or not db.get_tag(uid, tag_id):
        await cq.answer("Уже не актуально", show_alert=True)
        return
    if tag_id in db.tags_on_all(circle_ids):
        db.remove_tag_from(circle_ids, tag_id)
    else:
        db.add_tag_to(circle_ids, tag_id)
    set_current(uid, target, cq.message.chat.id, cq.message.message_id)
    text, kb = tagging_view(uid, target)
    await together(cq.answer(), safe_edit(cq.message, text, kb))


@router.callback_query(F.data.startswith("ok:"))
async def cb_done(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    target = cq.data.split(":")[1]
    circle_ids = target_circles(uid, target)
    clear_current(uid, only=target)
    if target == f"b{db.state(uid)['open_batch']}":
        db.set_state(uid, open_batch=None, open_batch_at=None)  # next circles start a new batch
    on = db.tags_on_all(circle_ids)
    names = [t["name"] for t in db.list_tags(uid) if t["id"] in on]
    await together(cq.answer(), safe_edit(cq.message, f"✅ Сохранено кружков: {len(circle_ids)}. Теги: {fmt_tags(names)}", None))


@router.callback_query(F.data.startswith("rm:"))
async def cb_remove(cq: CallbackQuery) -> None:
    target = cq.data.split(":")[1]
    n = len(target_circles(cq.from_user.id, target))
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"Да, удалить ({n})", callback_data=f"rmy:{target}"),
        InlineKeyboardButton(text="Отмена", callback_data=f"rmn:{target}"),
    ]])
    await together(cq.answer(), safe_edit(cq.message, f"Удалить {n} кружк(а/ов) из хранилища? Из чатов с друзьями они не пропадут.", kb))


@router.callback_query(F.data.startswith("rmy:"))
async def cb_remove_yes(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    target = cq.data.split(":")[1]
    ids = target_circles(uid, target)
    await remove_circles(uid, ids)
    clear_current(uid, only=target)
    await safe_edit(cq.message, f"🗑 Удалено: {len(ids)}", None)
    await cq.answer()


@router.callback_query(F.data.startswith("rmn:"))
async def cb_remove_no(cq: CallbackQuery) -> None:
    target = cq.data.split(":")[1]
    view = tagging_view(cq.from_user.id, target)
    if view:
        await safe_edit(cq.message, *view)
    await cq.answer()


@router.callback_query(F.data.startswith("ed:"))
async def cb_edit(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    circle_id = int(cq.data.split(":")[1])
    if not db.get_circle(uid, circle_id):
        await cq.answer("Этот кружок удалён", show_alert=True)
        return
    await together(cq.answer(), send_prompt(cq.bot, uid, cq.message.chat.id, f"c{circle_id}"))


# Delete straight from a shown circle: 🗑 → confirm → circle and its message disappear.
@router.callback_query(F.data.startswith("dl:"))
async def cb_delete_ask(cq: CallbackQuery) -> None:
    circle_id = int(cq.data.split(":")[1])
    if not db.get_circle(cq.from_user.id, circle_id):
        await cq.answer("Этот кружок уже удалён", show_alert=True)
        return
    await together(cq.answer("Удалить этот кружок из хранилища?"),
                         cq.message.edit_reply_markup(reply_markup=confirm_delete_kb(circle_id)))


@router.callback_query(F.data.startswith("dly:"))
async def cb_delete_yes(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    circle_id = int(cq.data.split(":")[1])
    await remove_circles(uid, [circle_id])
    clear_current(uid, only=f"c{circle_id}")
    async def drop_message():
        try:
            await cq.message.delete()
        except TelegramBadRequest:
            # Telegram doesn't let bots delete messages older than 48h: just drop the buttons
            await cq.message.edit_reply_markup(reply_markup=None)
    await together(cq.answer("🗑 Удалено"), drop_message())


@router.callback_query(F.data.startswith("dln:"))
async def cb_delete_no(cq: CallbackQuery) -> None:
    circle_id = int(cq.data.split(":")[1])
    if db.get_circle(cq.from_user.id, circle_id):
        await cq.message.edit_reply_markup(reply_markup=circle_kb(circle_id))
    await cq.answer()


@router.callback_query(F.data.startswith("sh:"))
async def cb_show(cq: CallbackQuery) -> None:
    parts = cq.data.split(":")
    key, offset = parts[1], int(parts[2])
    # "more" buttons remember the order the list was started in
    newest = {"d": True, "a": False}.get(parts[3]) if len(parts) > 3 else None
    grp = parts[4] if len(parts) > 4 else None
    first = [cq.answer()]
    if offset > 0:
        # remove the buttons of the page footer we just pressed; the new footer gets them
        first.append(safe_edit(cq.message, cq.message.html_text, None))
    await together(*first)
    await show_page(cq.bot, cq.from_user.id, cq.message.chat.id, key, offset, newest, grp)


@router.callback_query(F.data.startswith("cl:"))
async def cb_clean(cq: CallbackQuery) -> None:
    grp = cq.data.split(":")[1]
    ids = db.pop_messages(cq.message.chat.id, grp, int(time.time()) - DELETE_WINDOW)
    await together(cq.answer("🧹 Убрано"), delete_many(cq.bot, cq.message.chat.id, sorted(set(ids) | {cq.message.message_id})))


@router.callback_query(F.data == "ord")
async def cb_order(cq: CallbackQuery) -> None:
    uid = cq.from_user.id
    db.set_newest_first(uid, not db.newest_first(uid))
    text, kb = tags_list_view(uid)
    await together(cq.answer(f"Порядок: {order_label(db.newest_first(uid))}"), safe_edit(cq.message, text, kb))


# ---------------------------------------------------------------- strangers
# Only reached by users not in ALLOWED_USERS: tell them their id so the owner can add it.
denied = Router()


@denied.message()
async def on_denied(message: Message) -> None:
    await message.answer(f"Это личное хранилище, доступ закрыт.\nТвой id: <code>{message.from_user.id}</code>")


@denied.callback_query()
async def on_denied_cb(cq: CallbackQuery) -> None:
    await cq.answer("Доступ закрыт", show_alert=True)


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(router)
    dp.include_router(denied)
    return dp
