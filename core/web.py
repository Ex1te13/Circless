"""The whole deployment as one ASGI app (Vercel finds it through index.py in the project root).

  POST /api/telegram              Telegram webhook (header X-Telegram-Bot-Api-Secret-Token)
  GET  /api/cron                  daily: keeps Supabase awake, prunes old data
  GET  /                          gallery page (webapp/index.html)
  GET  /api/data                  tags + circles of the current user
  POST /api/order                 {"newest_first": bool}
  POST /api/circles/{id}/tags     {"tags": [...]} - replaces the circle's tags
  POST /api/circles/{id}/delete
  POST /api/circles/{id}/send     bot sends the circle to the chat (to forward it to a friend)
  GET  /media/{v|t}/{id}?u&e&s    video / preview: copies it to storage once, redirects to a signed link

Gallery API calls are authenticated with Telegram's signed initData (header X-Init-Data).
<video>/<img> can't send headers, so media links carry a short-lived HMAC signature instead.
"""
import hashlib
import hmac
import io
import json
import logging
import time
from pathlib import Path
from urllib.parse import parse_qsl

from aiogram.types import Update
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Route

from core import config, services, timing
from core.handlers import DELETE_WINDOW, build_dispatcher, remove_circles, send_circle
from core.storage import LocalStorage, media_path

log = logging.getLogger("web")
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

INDEX = Path(__file__).resolve().parent.parent / "webapp" / "index.html"
INIT_MAX_AGE = 7 * 24 * 3600   # how long an opened Mini App session stays valid
MEDIA_LINK_TTL = 12 * 3600     # our signed /media links
STORAGE_LINK_TTL = 3600        # Supabase signed links we redirect to
dp = build_dispatcher()


def check_init_data(init_data: str, token: str) -> dict | None:
    """Validate Telegram WebApp initData. Returns the user dict or None."""
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    if time.time() - int(pairs.get("auth_date", 0) or 0) > INIT_MAX_AGE:
        return None
    try:
        return json.loads(pairs["user"])
    except (KeyError, ValueError):
        return None


def _media_key() -> bytes:
    return hashlib.sha256(b"media:" + config.BOT_TOKEN.encode()).digest()


def sign(kind: str, cid: int, uid: int, exp: int) -> str:
    return hmac.new(_media_key(), f"{kind}:{cid}:{uid}:{exp}".encode(), hashlib.sha256).hexdigest()[:32]


def media_url(kind: str, cid: int, uid: int) -> str:
    exp = int(time.time()) + MEDIA_LINK_TTL
    return f"media/{kind}/{cid}?u={uid}&e={exp}&s={sign(kind, cid, uid, exp)}"


def gallery_user(request: Request) -> int:
    """User id from initData, or raise 401/403."""
    if config.DEV_USER:
        uid = config.DEV_USER
    else:
        user = check_init_data(request.headers.get("x-init-data", ""), config.BOT_TOKEN)
        if not user:
            raise PermissionError(401)
        uid = int(user["id"])
    if config.ALLOWED_USERS and uid not in config.ALLOWED_USERS:
        raise PermissionError(403)
    return uid


def api(handler):
    """Gallery API endpoint: authenticates and turns errors into status codes."""
    async def wrapper(request: Request) -> Response:
        try:
            uid = gallery_user(request)
        except PermissionError as e:
            return PlainTextResponse("not allowed", status_code=e.args[0])
        try:
            return await handler(request, uid)
        except LookupError:
            return PlainTextResponse("not found", status_code=404)
    return wrapper


def circle_or_404(request: Request, uid: int):
    circle = services.db().get_circle(uid, int(request.path_params["id"]))
    if not circle:
        raise LookupError
    return circle


# ---------------------------------------------------------------- telegram
async def telegram_webhook(request: Request) -> Response:
    if not config.WEBHOOK_SECRET or not hmac.compare_digest(
        request.headers.get("x-telegram-bot-api-secret-token", ""), config.WEBHOOK_SECRET
    ):
        return PlainTextResponse("forbidden", status_code=403)
    t0 = time.perf_counter()
    acc = timing.start()
    bot = services.bot()
    kind = "?"
    try:
        body = await request.json()
        kind = next((k for k in ("message", "callback_query") if k in body), "?")
        update = Update.model_validate(body, context={"bot": bot})
        await dp.feed_update(bot, update)
    except Exception:
        # answer 200 anyway: otherwise Telegram retries the same update forever and the queue stalls
        log.exception("update failed")
    log.info("update %s: %s", kind, timing.summary(acc, time.perf_counter() - t0))
    return PlainTextResponse("ok")


async def ping(_: Request) -> Response:
    """Called every few minutes by a pg_cron job in Supabase so Vercel keeps the function warm
    (a cold start re-imports aiogram and takes seconds)."""
    return PlainTextResponse("pong")


async def cron(request: Request) -> Response:
    if config.CRON_SECRET and request.headers.get("authorization") != f"Bearer {config.CRON_SECRET}":
        return PlainTextResponse("forbidden", status_code=403)
    db = services.db()
    db.ping()
    db.prune_messages(int(time.time()) - DELETE_WINDOW)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------- gallery
async def index(_: Request) -> Response:
    return FileResponse(INDEX, headers={"Cache-Control": "no-cache"})


@api
async def data(request: Request, uid: int) -> Response:
    db = services.db()
    circles = db.gallery_circles(uid)
    for c in circles:
        c["video"] = media_url("v", c["id"], uid)
        c["thumb"] = media_url("t", c["id"], uid) if c.pop("has_thumb") else None
    tags = [{"id": t["id"], "name": t["name"], "count": t["cnt"]} for t in db.list_tags(uid)]
    return JSONResponse({"circles": circles, "tags": tags, "newest_first": db.newest_first(uid)})


@api
async def set_order(request: Request, uid: int) -> Response:
    body = await request.json()
    services.db().set_newest_first(uid, bool(body.get("newest_first")))
    return JSONResponse({"ok": True})


@api
async def set_tags(request: Request, uid: int) -> Response:
    circle = circle_or_404(request, uid)
    body = await request.json()
    names = [str(n) for n in body.get("tags", [])][:50]
    return JSONResponse({"tags": services.db().set_circle_tags(uid, circle["id"], names)})


@api
async def delete(request: Request, uid: int) -> Response:
    circle = circle_or_404(request, uid)
    await remove_circles(uid, [circle["id"]])
    return JSONResponse({"ok": True})


@api
async def send(request: Request, uid: int) -> Response:
    circle = circle_or_404(request, uid)
    await send_circle(services.bot(), uid, circle)
    return JSONResponse({"ok": True})


async def media(request: Request) -> Response:
    kind = request.path_params["kind"]
    cid = int(request.path_params["id"])
    q = request.query_params
    if kind not in ("v", "t"):
        return PlainTextResponse("not found", status_code=404)
    try:
        uid, exp = int(q["u"]), int(q["e"])
    except (KeyError, ValueError):
        return PlainTextResponse("forbidden", status_code=403)
    if exp < time.time() or not hmac.compare_digest(sign(kind, cid, uid, exp), q.get("s", "")):
        return PlainTextResponse("forbidden", status_code=403)
    db = services.db()
    circle = db.get_circle(uid, cid)
    file_id = circle and (circle["file_id"] if kind == "v" else circle["thumb_file_id"])
    if not file_id:
        return PlainTextResponse("not found", status_code=404)
    storage = services.storage()
    path = media_path(circle, kind)
    if not circle["stored_video" if kind == "v" else "stored_thumb"]:
        # first view: copy the file from Telegram into storage
        try:
            buf = await services.bot().download(file_id, destination=io.BytesIO())
            await storage.upload(path, buf.getvalue(), "video/mp4" if kind == "v" else "image/jpeg")
        except Exception:
            log.exception("copy %s to storage failed", path)
            return PlainTextResponse("can't fetch file", status_code=502)
        db.mark_stored(cid, kind)
    url = await storage.signed_url(path, STORAGE_LINK_TTL)
    return RedirectResponse(url, status_code=302, headers={"Cache-Control": "private, max-age=3000"})


async def local_file(request: Request) -> Response:
    """Only for LocalStorage (tests/local runs); Supabase serves files itself in production."""
    storage = services.storage()
    if not isinstance(storage, LocalStorage):
        return PlainTextResponse("not found", status_code=404)
    p = (storage.folder / request.path_params["path"]).resolve()
    if storage.folder.resolve() not in p.parents or not p.is_file():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(p)


app = Starlette(routes=[
    Route("/api/telegram", telegram_webhook, methods=["POST"]),
    Route("/api/cron", cron, methods=["GET"]),
    Route("/api/ping", ping, methods=["GET"]),
    Route("/", index),
    Route("/api/data", data),
    Route("/api/order", set_order, methods=["POST"]),
    Route("/api/circles/{id:int}/tags", set_tags, methods=["POST"]),
    Route("/api/circles/{id:int}/delete", delete, methods=["POST"]),
    Route("/api/circles/{id:int}/send", send, methods=["POST"]),
    Route("/media/{kind:str}/{id:int}", media),
    Route("/files/{path:path}", local_file),
])
