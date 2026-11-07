"""Where gallery videos live so the browser can play them.

Telegram file links contain the bot token, so they must never reach the browser.
Each circle is copied once from Telegram into a private Supabase Storage bucket and
the gallery gets short-lived signed links (Supabase serves them with Range support,
which iOS needs to play video).
"""
import time
from pathlib import Path

import aiohttp


class StorageError(Exception):
    pass


class SupabaseStorage:
    def __init__(self, url: str, service_key: str, bucket: str):
        self.base = f"{url}/storage/v1"
        self.bucket = bucket
        self.headers = {"apikey": service_key}
        if not service_key.startswith("sb_"):
            # legacy service_role JWT goes in Authorization too; new sb_secret_ keys only in apikey
            self.headers["Authorization"] = f"Bearer {service_key}"

    async def _req(self, method: str, path: str, **kw):
        async with aiohttp.ClientSession(headers=self.headers) as s:
            async with s.request(method, self.base + path, **kw) as r:
                body = await r.text()
                if r.status >= 300:
                    raise StorageError(f"{method} {path}: {r.status} {body[:200]}")
                return await r.json(content_type=None) if body else None

    async def create_bucket(self) -> None:
        try:
            await self._req("POST", "/bucket", json={"id": self.bucket, "name": self.bucket, "public": False})
        except StorageError as e:
            if "already exists" not in str(e).lower() and "409" not in str(e):
                raise

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        await self._req(
            "POST", f"/object/{self.bucket}/{path}", data=data,
            headers={"Content-Type": content_type, "x-upsert": "true", "Cache-Control": "31536000"},
        )

    async def signed_url(self, path: str, expires: int) -> str:
        res = await self._req("POST", f"/object/sign/{self.bucket}/{path}", json={"expiresIn": expires})
        return self.base + res["signedURL"]

    async def delete(self, paths: list[str]) -> None:
        if paths:
            await self._req("DELETE", f"/object/{self.bucket}", json={"prefixes": paths})


class LocalStorage:
    """Files in a folder, served by the web app under /files (tests and local runs)."""

    def __init__(self, folder: str | Path):
        self.folder = Path(folder)

    async def create_bucket(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)

    async def upload(self, path: str, data: bytes, content_type: str) -> None:
        p = self.folder / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    async def signed_url(self, path: str, expires: int) -> str:
        return f"/files/{path}?exp={int(time.time()) + expires}"

    async def delete(self, paths: list[str]) -> None:
        for path in paths:
            (self.folder / path).unlink(missing_ok=True)


def media_path(circle, kind: str) -> str:
    return f"{circle['user_id']}/{circle['file_unique_id']}.{'mp4' if kind == 'v' else 'jpg'}"
