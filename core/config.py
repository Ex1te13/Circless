"""Settings from environment variables (.env locally, Project Settings → Environment Variables on Vercel)."""
import os
import re

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ALLOWED_USERS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ALLOWED_USERS", "")) if x.strip()}

# Supabase Postgres (use the "Transaction pooler" connection string, port 6543).
# A plain file path means a local SQLite database (used by tests).
DATABASE_URL = os.getenv("DATABASE_URL", "local.db")

# Supabase Storage for the gallery videos
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
STORAGE_BUCKET = os.getenv("STORAGE_BUCKET", "circles")

# Public https address of the deployment, e.g. https://circles.vercel.app
_vercel_host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL", "")
PUBLIC_URL = (os.getenv("PUBLIC_URL") or (f"https://{_vercel_host}" if _vercel_host else "")).rstrip("/")

# Telegram sends this in every webhook call so nobody else can post fake updates
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Vercel Cron sends "Authorization: Bearer <CRON_SECRET>"
CRON_SECRET = os.getenv("CRON_SECRET", "")

# Local testing only: open the gallery in a normal browser as this user, without Telegram.
# Never set it on Vercel.
DEV_USER = 0 if os.getenv("VERCEL") else int(os.getenv("GALLERY_DEV_USER", "0") or 0)
