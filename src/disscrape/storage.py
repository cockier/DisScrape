import asyncio
import json
from pathlib import Path

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

    def prepare(self):
        for name in [*FILES.values(), "avatars/.gitkeep"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if name in FILES.values():
                path.touch(exist_ok=True)

    def merge(self, member, profile):


        profile_user = profile.get("user") or {}
        user = member["user"] | profile_user
        uid = str(user["id"])
        if not uid.isdecimal():
            raise ValueError("Invalid user ID")

        user_profile = profile.get("user_profile") or {}
        guild_member_profile = profile.get("guild_member_profile") or {}
        guild_member = profile.get("guild_member") or {}


        activities = member.get("activities") or guild_member.get("activities") or []
        status = next((a.get("state") for a in activities if a.get("type") == 4), "")

        nick = member.get("nick") or guild_member.get("nick")
        record = self.records.setdefault(uid, {})
        values = {
            "display": nick or user.get("global_name") or user.get("username"),
            "bio": (guild_member_profile.get("bio")
                    or user_profile.get("bio")
                    or user.get("bio")),
            "status": status,
            "pronouns": (guild_member_profile.get("pronouns")
                         or user_profile.get("pronouns")
                         or user.get("pronouns")),
        }
        for key, value in values.items():
            if value and not record.get(key):
                record[key] = value
        return uid, user.get("avatar")

    def error(self, scope, error):
        self.errors.append({"scope": scope, "error": str(error)})

    def _flush(self):
        for key, name in FILES.items():
            path = self.root / name
            existing = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                uid, separator, value = line.partition(": ")
                if separator and uid.isdecimal():
                    existing[uid] = value
            for uid, record in self.records.items():
                if record.get(key):
                    existing[uid] = line_value(record[key])
            text = "".join(f"{uid}: {value}\n" for uid, value in sorted(existing.items(), key=lambda x: int(x[0])))
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
