import asyncio
import math
import random
import time
import aiohttp
import json
from pathlib import Path
import argparse
import re
import sys
from dataclasses import dataclass, field
from collections import deque
import shutil
import unicodedata
from richer import Color, Live, render_panel, sparkline, strip_ansi

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from disscrape.client import APIError, DiscordClient


FILES = {
    "display": "names/display.txt",
    "bio": "bio/bio.txt",
    "status": "status/status.txt",
    "pronouns": "pronouns/pronouns.txt",
}


def line_value(value):
    return str(value or "").replace("\\", "\\\\").replace("\r", "\\r").replace("\n", "\\n")


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.records = {}
        self.errors = []
        self.counts = dict.fromkeys(FILES, 0)

    def prepare(self):
        for name in [*FILES.values(), "avatars/.gitkeep"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name in FILES.values():
                path.touch(exist_ok=True)

    def merge(self, member, profile):
        user = member["user"] | (profile.get("user") or {})
        uid = str(user["id"])
        if not uid.isdecimal():
            raise ValueError("Invalid user ID")
        details = profile.get("user_profile") or {}
        guild_details = profile.get("guild_member_profile") or {}
        activities = profile.get("activities") or (profile.get("guild_member") or {}).get("activities") or []
        status = next((a.get("state") for a in activities if a.get("type") == 4), "")
        record = self.records.setdefault(uid, {})
        values = {
            "display": member.get("nick") or user.get("global_name") or user.get("username"),
            "bio": guild_details.get("bio") or details.get("bio") or user.get("bio"),
            "status": status,
            "pronouns": guild_details.get("pronouns") or details.get("pronouns") or user.get("pronouns"),
        }
        for key, value in values.items():
            if value and not record.get(key):
                record[key] = value
                self.counts[key] += 1
        return uid, user.get("avatar")

    def error(self, scope, error):
        self.errors.append({"scope": scope, "error": str(error)})

    def _flush(self):
        for key, name in FILES.items():
            path = self.root / name

            existing = {}
            for line in path.read_text(encoding="utf-8").splitlines():

                uid, sep, value = line.partition(": ")
                if sep and uid.isdecimal():
                    existing[uid] = value
                elif line.strip():

                    pass
            for uid, record in self.records.items():
                if record.get(key):
                    existing[uid] = line_value(record[key])

            text = "".join(f"{value}\n" for _, value in sorted(existing.items(), key=lambda x: int(x[0])))
            temporary = path.with_suffix(".tmp")
            temporary.write_text(text, encoding="utf-8")
            temporary.replace(path)
        if self.errors:
            path = self.root / "errors.jsonl"
            with path.open("a", encoding="utf-8") as file:
                for error in self.errors:
                    file.write(json.dumps(error, ensure_ascii=False) + "\n")
            self.errors.clear()

    async def flush(self):
        await asyncio.to_thread(self._flush)

    async def avatar(self, uid, data, ext="png"):
        def write():
            target = self.root / "avatars" / f"{uid}.{ext}"
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(target)
        await asyncio.to_thread(write)


@dataclass
class Progress:
    server: str = "loading servers"
    servers: int = 0
    entries: int = 0
    errors: int = 0
    message: str = "starting"
    avatars: int = 0
    duplicates: int = 0
    requests: int = 0
    rate_limits: int = 0
    page: int = 0
    page_done: int = 0
    page_size: int = 0
    discovered: int = 0
    finished: bool = False
    outcome: str = ""
    started: float = field(default_factory=time.monotonic)
    stopped: float = 0
    wait_until: float = 0
    sampled: float = field(default_factory=time.monotonic)
    sample_entries: int = 0
    rates: deque = field(default_factory=lambda: deque([0.0] * 24, maxlen=24))
    latency: deque = field(default_factory=lambda: deque(maxlen=30))
    events: deque = field(default_factory=lambda: deque(maxlen=40))
    counts: dict = field(default_factory=lambda: dict.fromkeys(FILES, 0))

    def note(self, message, level="info"):
        self.message = message
        self.events.append((time.strftime("%H:%M:%S"), level, message))
        if message.startswith("Rate limited"):
            match = re.search(r"waiting ([\d.]+)s", message)
            if match:
                self.wait_until = time.monotonic() + float(match[1])

    def response(self, status, elapsed):
        self.requests += 1
        self.latency.append(elapsed * 1000)
        if status == 429:
            self.rate_limits += 1

    def sample(self):
        now = time.monotonic()
        elapsed = now - self.sampled
        if elapsed >= 1:
            self.rates.append((self.entries - self.sample_entries) / elapsed)
            self.sample_entries, self.sampled = self.entries, now

    def render(self, width=None, height=None):
        size = shutil.get_terminal_size((100, 30))
        width = min(118, width or size.columns - 2)
        height = height or size.lines
        ink, dim = "#E6EDF7", "#75839B"
        accent, amber, red = "#A49BFF", "#F2BE75", "#F28E9C"
        paint = lambda text, color: Color.from_hex(color) + str(text) + Color.RESET

        def clean(text, limit):
            result, cells = "", 0
            for char in str(text):
                if unicodedata.category(char).startswith("C"):
                    continue
                step = 0 if unicodedata.combining(char) else (2 if unicodedata.east_asian_width(char) in "WF" else 1)
                if cells + step > max(0, limit - 1):
                    return result + "…"
                result, cells = result + char, cells + step
            return result

        def rule(char="─"):
            return paint(char * width, "#2A3347")

        def kv(label, value, value_color=None):
            val = paint(str(value), value_color or ink)
            lbl = paint(label, dim)
            gap = " " * max(1, width - len(label) - len(strip_ansi(str(value))))
            return lbl + gap + val

        elapsed = max(0, (self.stopped or time.monotonic()) - self.started)
        h, m, s = int(elapsed) // 3600, int(elapsed) // 60 % 60, int(elapsed) % 60
        duration = f"{h}h {m}m {s}s" if h else f"{m}m {s}s" if m else f"{s}s"
        waiting = max(0, self.wait_until - time.monotonic())
        rate = sum(list(self.rates)[-5:]) / min(5, len(self.rates)) if self.rates else 0
        latency = f"{sum(self.latency) / len(self.latency):.0f}ms" if self.latency else "—"
        state = self.outcome.lower() if self.outcome else ("waiting" if waiting else "running")
        state_color = red if self.outcome == "FAILED" else amber if waiting or (self.errors and self.finished) else "#6DE4BF"
        counts = self.counts


        if width < 60 or height < 10:
            return "\n".join([
                paint("disscrape", accent) + "  " + paint(state, state_color),
                paint(clean(self.server, width - 2), dim),
                f"{self.entries:,} members  {self.avatars:,} avatars  {self.errors} errors",
            ])

        srv = clean(self.server, width - 20)
        srv_detail = f"{self.page_done:,} of {self.page_size:,} in chunk" if self.page_size else ""
        progress_frac = self.page_done / self.page_size if self.page_size else 0
        bar_w = max(10, width - 4)
        filled = int(bar_w * progress_frac)
        bar = paint("▪" * filled, accent) + paint("▪" * (bar_w - filled), "#2A3347")

        rows = [
            "",
            "  " + paint("disscrape", accent) + "  " + paint(state, state_color),
            "",
            rule(),
            "",
            "  " + kv("members found", f"{self.entries:,}", accent),
            "  " + kv("avatars saved", f"{self.avatars:,}"),
            "  " + kv("names", f"{counts['display']:,}"),
            "  " + kv("bios", f"{counts['bio']:,}"),
            "  " + kv("statuses", f"{counts['status']:,}"),
            "  " + kv("pronouns", f"{counts['pronouns']:,}"),
            "",
            rule(),
            "",
            "  " + kv("server", srv),
            "  " + kv("servers done", f"{self.servers} of {self.discovered}"),
            "  " + kv("chunk progress", srv_detail or "—"),
            "",
            "  " + bar,
            "",
            rule(),
            "",
            "  " + kv("rate", f"{rate:.1f}/s"),
            "  " + kv("latency", latency),
            "  " + kv("requests", f"{self.requests:,}"),
            "  " + kv("errors", str(self.errors), red if self.errors else dim),
            "  " + kv("elapsed", duration),
            "",
        ]

        if height >= len(rows) + 4:
            history = sparkline(list(self.rates), color=Color.from_hex(accent), show_stats=False)
            rows += ["  " + history, ""]

        event_budget = max(0, height - len(rows) - 3)
        if event_budget > 0:
            rows.append(rule())
            events = list(self.events)[-event_budget:]
            for stamp, level, message in events:
                color = red if level == "error" else amber if level == "warn" else dim
                rows.append("  " + paint(stamp + "  ", "#2A3347") + paint(clean(message, width - 14), color))
            rows.append("")

        rows.append("  " + paint("ctrl+c to stop", "#2A3347"))
        return "\n".join(rows)


def credential(path):
    lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    if len(lines) != 1:
        raise ValueError("input/tokens.txt must contain exactly one credential")
    return lines[0]


async def collect(root, plain=False):
    token = await asyncio.to_thread(credential, root / "input" / "tokens.txt")
    store = Store(root / "output")
    await asyncio.to_thread(store.prepare)
    progress = Progress()
    progress.counts = store.counts
    progress.note("Opening session")
    seen = set()
    live = None if plain or not sys.stdout.isatty() else Live(progress.render(), refresh_rate=8)

    def update(message=None):
        if message:
            if message.startswith("Rate limited"):
                progress.note(message, "warn")
            else:
                progress.message = message

    async def refresh():
        while True:
            progress.sample()
            if live:
                live.update(progress.render())
            await asyncio.sleep(.125)

    def failed(scope, error):
        progress.errors += 1
        store.error(scope, error)
        progress.note(f"{scope} · {error}", "error")

    if live:
        live.start()
    ticker = asyncio.create_task(refresh())
    try:
        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=60)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with DiscordClient(session, token, update, observe=progress.response) as client:
                async for guilds in client.pages("/users/@me/guilds", 200):
                    progress.discovered += len(guilds)
                    for guild in guilds:
                        gid = str(guild["id"])
                        if not gid.isdecimal():
                            raise ValueError("Invalid guild ID")
                        progress.server = guild.get("name", gid)
                        progress.page = progress.page_done = progress.page_size = 0
                        progress.note(f"Opening {progress.server}")
                        update("Reading members")
                        avatar_sem = asyncio.Semaphore(20)
                        profile_sem = asyncio.Semaphore(10)

                        async def fetch_avatar(uid, avatar):
                            async with avatar_sem:
                                try:
                                    result = await client.get(f"/avatars/{uid}/{avatar}", cdn=True)
                                    data, ctype = result if isinstance(result, tuple) else (result, "image/png")
                                    ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
                                    ext = ext_map.get(ctype, "png")
                                    await store.avatar(uid, data, ext)
                                    progress.avatars += 1
                                except (APIError, ValueError) as error:
                                    failed(f"avatar:{uid}", error)

                        async def fetch_profile(member_snapshot, uid):
                            async with profile_sem:
                                try:
                                    prof = await client.get_profile(uid, gid)
                                    prof["activities"] = member_snapshot.get("activities", [])
                                    store.merge(member_snapshot, prof)
                                except APIError as error:
                                    if error.status == 401:
                                        raise
                                    failed(f"profile:{uid}", error)


                        live_tasks: set[asyncio.Task] = set()

                        def _track(t):
                            live_tasks.add(t)
                            t.add_done_callback(live_tasks.discard)

                        try:
                            async for members in client.pages(f"/guilds/{gid}/members", 1000):
                                progress.page += 1
                                progress.page_done = 0
                                progress.page_size = len(members)
                                for member in members:
                                    uid = str(member["user"]["id"])
                                    if not uid.isdecimal():
                                        raise ValueError("Invalid member ID")
                                    if uid in seen:
                                        store.merge(member, {})
                                        progress.duplicates += 1
                                        progress.page_done += 1
                                        continue

                                    profile = {"activities": member.get("activities", [])}
                                    uid, avatar = store.merge(member, profile)

                                    _track(asyncio.create_task(fetch_profile(member, uid)))
                                    if avatar and re.fullmatch(r"[a-zA-Z0-9_-]+", avatar):
                                        _track(asyncio.create_task(fetch_avatar(uid, avatar)))
                                    seen.add(uid)
                                    progress.entries = len(seen)
                                    progress.page_done += 1
                                await store.flush()
                            if live_tasks:
                                await asyncio.gather(*live_tasks, return_exceptions=True)
                            progress.note(f"Page {progress.page} saved · {progress.entries:,} unique entries")
                        except APIError as error:
                            if error.status == 401:
                                raise
                            failed(f"guild:{gid}", error)
                        progress.servers += 1
                        await store.flush()
        progress.finished = True
        progress.outcome = "PARTIAL" if progress.errors else "COMPLETE"
        progress.note("Finished" if not progress.errors else "Finished with request failures", "warn" if progress.errors else "info")
        return 0 if not progress.errors else 2
    except asyncio.CancelledError:
        progress.outcome = "STOPPED"
        progress.note("Interrupted · saving collected entries", "warn")
        raise
    except Exception:
        progress.outcome = "FAILED"
        progress.note("Session failed · saving collected entries", "error")
        raise
    finally:
        try:
            await store.flush()
        finally:
            progress.stopped = time.monotonic()
            ticker.cancel()
            try:
                await ticker
            except asyncio.CancelledError:
                pass
            if live:
                live.update(progress.render())
                await asyncio.sleep(.15)
                live.stop()
            print(f"DisScrape: {progress.entries} entries, {progress.servers} servers, {progress.errors} errors")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Async Discord profile collector")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parent, help="Project directory containing input/ and output/")
    parser.add_argument("--plain", action="store_true", help="Disable live terminal rendering")
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


if __name__ == "__main__":
    main()
