import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import aiohttp
from richer import Live, render_panel

from .client import APIError, DiscordClient
from .storage import Store

AVATAR_EXTS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}


@dataclass
class Progress:
    server: str = "Loading servers"
    servers: int = 0
    entries: int = 0
    errors: int = 0
    message: str = "Starting"

    def render(self):
        server = "".join(c for c in self.server if c.isprintable())[:60]
        return render_panel([
            f"Server: {server}",
            f"Servers: {self.servers}   Entries: {self.entries}   Errors: {self.errors}",
            self.message,
        ], title="DisScrape")


def credential(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if not lines:
        raise ValueError("input/tokens.txt is empty")
    if len(lines) > 1:
        raise ValueError("input/tokens.txt must contain exactly one token per run")
    return lines[0]


async def collect(root, plain=False):
    token = await asyncio.to_thread(credential, root / "input" / "tokens.txt")
    store = Store(root / "output")
    await asyncio.to_thread(store.prepare)
    progress = Progress()
    seen = set()
    live = None if plain or not sys.stdout.isatty() else Live(progress.render(), refresh_rate=8)

    def update(message=None):
        if message:
            progress.message = message
        if live:
            live.update(progress.render())

    def failed(scope, error):
        progress.errors += 1
        store.error(scope, error)
        update(f"Request failed; see output/errors.jsonl ({progress.errors})")

    if live:
        live.start()
    try:
        timeout = aiohttp.ClientTimeout(total=60)
        headers = {
            "Authorization": token,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            async with DiscordClient(session, token, update) as client:
                async for guilds in client.pages("/users/@me/guilds", 200):
                    for guild in guilds:
                        gid = str(guild["id"])
                        if not gid.isdecimal():
                            raise ValueError("Invalid guild ID")
                        progress.server = guild.get("name", gid)
                        update("Reading members")
                        try:
                            async for members in client.pages(f"/guilds/{gid}/members", 1000):
                                for member in members:
                                    uid = str(member["user"]["id"])
                                    if not uid.isdecimal():
                                        raise ValueError("Invalid member ID")


                                    profile = {}
                                    if uid not in seen:
                                        try:
                                            profile = await client.get_profile(uid, gid)
                                        except APIError as error:
                                            if error.status == 401:
                                                raise
                                            failed(f"profile:{uid}", error)

                                    uid, avatar = store.merge(member, profile)

                                    if uid not in seen and avatar and re.fullmatch(r"[a-zA-Z0-9_-]+", avatar):
                                        try:
                                            data, ctype = await client.get(
                                                f"/avatars/{uid}/{avatar}", cdn=True
                                            )
                                            ext = AVATAR_EXTS.get(ctype, "png")
                                            await store.avatar(uid, data, ext)
                                        except (APIError, ValueError) as error:
                                            failed(f"avatar:{uid}", error)

                                    seen.add(uid)
                                    progress.entries = len(seen)
                                    update("Collecting members")
                                await store.flush()
                        except APIError as error:
                            if error.status == 401:
                                raise
                            failed(f"guild:{gid}", error)
                        progress.servers += 1
                        await store.flush()
        update("Finished" if not progress.errors else "Finished with errors")
        return 0 if not progress.errors else 2
    finally:
        try:
            await store.flush()
        finally:
            if live:
                live.stop()
            print(f"DisScrape: {progress.entries} entries, {progress.servers} servers, {progress.errors} errors")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Discord profile collector")
    parser.add_argument("--root", type=Path, default=Path.cwd(),
                        help="Project root containing input/ and output/")
    parser.add_argument("--plain", action="store_true", help="Disable live TUI")
    args = parser.parse_args()
    try:
        code = asyncio.run(collect(args.root.resolve(), args.plain))
    except KeyboardInterrupt:
        print("Interrupted; collected entries saved.", file=sys.stderr)
        code = 130
    except (APIError, OSError, ValueError) as error:
        print(f"DisScrape: {error}", file=sys.stderr)
        code = 1
    raise SystemExit(code)
