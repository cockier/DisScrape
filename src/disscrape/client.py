import asyncio
import contextlib
import random
import uuid

import aiohttp

API = "https://discord.com/api/v10"
CDN = "https://cdn.discordapp.com"
GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"


class APIError(Exception):
    def __init__(self, status, path):
        self.status = status
        super().__init__(f"Discord {status}: {path}")


class DiscordClient:

    def __init__(self, session, token, notify=lambda _: None, observe=lambda *_: None):
        self.session = session
        self.token = token.strip()
        self.notify = notify
        self.observe = observe
        self.ws = None
        self.reader = None
        self.heartbeat = None
        self.events = asyncio.Queue(maxsize=0)
        self.sequence = None
        self.acked = True
        self._guild_meta = {}

    async def __aenter__(self):
        try:
            self.ws = await self.session.ws_connect(GATEWAY, max_msg_size=32 * 1024 * 1024)
            hello = await self.ws.receive_json(timeout=30)
            if hello.get("op") != 10:
                raise APIError(0, "Gateway did not send HELLO")
            interval = hello["d"]["heartbeat_interval"] / 1000
            self.reader = asyncio.create_task(self._read())
            self.heartbeat = asyncio.create_task(self._heartbeat(interval))
            await self.ws.send_json({"op": 2, "d": {
                "token": self.token,
                "capabilities": 16381,
                "properties": {
                    "os": "Windows",
                    "browser": "Chrome",
                    "device": "",
                    "system_locale": "en-US",
                    "browser_user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                    "browser_version": "124.0.0.0",
                    "os_version": "10",
                    "referrer": "",
                    "referring_domain": "",
                    "referrer_current": "",
                    "referring_domain_current": "",
                    "release_channel": "stable",
                    "client_build_number": 294168,
                    "client_event_source": None,
                },
                "presence": {
                    "status": "online",
                    "since": 0,
                    "activities": [],
                    "afk": False,
                },
                "compress": False,
                "client_state": {
                    "guild_versions": {},
                    "highest_last_message_id": "0",
                    "read_state_version": 0,
                    "user_guild_settings_version": -1,
                    "user_settings_version": -1,
                    "private_channels_version": "0",
                    "api_code_version": 0,
                },
            }})
            return self
        except BaseException:
            await self.__aexit__(None, None, None)
            raise

    async def __aexit__(self, *args):
        for task in (self.reader, self.heartbeat):
            if task:
                task.cancel()
        for task in (self.reader, self.heartbeat):
            if task:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self.ws:
            await self.ws.close()

    async def _heartbeat(self, interval):
        await asyncio.sleep(random.random() * interval)
        while True:
            if not self.acked:
                await self.ws.close()
                return
            self.acked = False
            await self.ws.send_json({"op": 1, "d": self.sequence})
            await asyncio.sleep(interval)

    async def _read(self):
        try:
            async for message in self.ws:
                if message.type != aiohttp.WSMsgType.TEXT:
                    raise APIError(0, "Gateway returned a non-text frame")
                payload = message.json()
                if payload.get("s") is not None:
                    self.sequence = payload["s"]
                op = payload["op"]
                if op == 11:
                    self.acked = True
                elif op == 1:
                    await self.ws.send_json({"op": 1, "d": self.sequence})
                elif op in (7, 9):
                    raise APIError(0, "Gateway requested reconnect; rerun to collect again")
                elif op == 0:
                    await self.events.put((payload["t"], payload["d"]))
            raise APIError(self.ws.close_code or 0,
                           "Gateway disconnected; check token and rerun")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            await self.events.put(error)

    async def _event(self, timeout=90):
        try:
            event = await asyncio.wait_for(self.events.get(), timeout)
        except asyncio.TimeoutError:
            raise APIError(0, "Gateway event timeout") from None
        if isinstance(event, Exception):
            raise event
        return event

    async def pages(self, path, limit):
        if path == "/users/@me/guilds":
            while True:
                name, data = await self._event(timeout=120)
                if name == "READY":
                    raw_guilds = data["guilds"]
                    break

            def _guild_name(g):
                return (g.get("properties") or {}).get("name") or g.get("name")

            full = [g for g in raw_guilds if isinstance(g, dict) and _guild_name(g)]
            stubs = {str(g["id"]) for g in raw_guilds if isinstance(g, dict) and not _guild_name(g)}

            guilds = []
            for g in full:
                gid = str(g["id"])
                channels = g.get("channels") or []
                text_cids = [str(c["id"]) for c in channels
                             if isinstance(c, dict) and c.get("type") == 0]
                any_cids = [str(c["id"]) for c in channels if isinstance(c, dict)]
                self._guild_meta[gid] = {
                    "lazy": bool(g.get("lazy")),
                    "channels": text_cids or any_cids,
                }
                guilds.append({"id": gid, "name": _guild_name(g)})

            while stubs:
                name, data = await self._event(timeout=120)
                if not isinstance(data, dict):
                    continue
                gid = str(data.get("id", ""))
                if name in ("GUILD_CREATE", "GUILD_DELETE") and gid in stubs:
                    stubs.remove(gid)
                    if name == "GUILD_CREATE" and not data.get("unavailable"):
                        channels = data.get("channels") or []
                        text_cids = [str(c["id"]) for c in channels
                                     if isinstance(c, dict) and c.get("type") == 0]
                        self._guild_meta[gid] = {
                            "lazy": bool(data.get("lazy")),
                            "channels": text_cids or [str(c["id"]) for c in channels if isinstance(c, dict)],
                        }
                        guilds.append({"id": gid, "name": data.get("name", gid)})

            yield guilds
            return

        gid = path.split("/")[2]
        meta = self._guild_meta.get(gid, {})

        channels = meta.get("channels") or []
        if meta.get("lazy") and channels:

            async for chunk in self._members_lazy(gid, channels[0]):
                yield chunk
            return


        nonce = uuid.uuid4().hex
        await self.ws.send_json({"op": 8, "d": {
            "guild_id": gid,
            "query": "",
            "limit": 0,
            "presences": True,
            "nonce": nonce,
        }})
        received = set()
        while True:
            name, data = await self._event()
            if (name != "GUILD_MEMBERS_CHUNK"
                    or data.get("nonce") != nonce
                    or str(data.get("guild_id")) != gid):
                continue
            index = data["chunk_index"]
            if index in received:
                continue
            received.add(index)
            presences = {str(p["user"]["id"]): p for p in data.get("presences", [])}
            members = data["members"]
            for member in members:
                member["activities"] = presences.get(
                    str(member["user"]["id"]), {}
                ).get("activities", [])
            yield members
            if len(received) == data["chunk_count"]:
                return

    async def _members_lazy(self, gid, channel_id):
        BATCH = 100
        offset = 0
        total = None

        while total is None or offset < total:
            await self.ws.send_json({"op": 14, "d": {
                "guild_id": gid,
                "typing": True,
                "threads": False,
                "activities": True,
                "members": [],
                "channels": {channel_id: [[offset, offset + BATCH - 1]]},
            }})

            members = []

            for _ in range(30):
                try:
                    name, data = await self._event(timeout=8)
                except APIError:
                    return
                if name != "GUILD_MEMBER_LIST_UPDATE":
                    continue
                if str(data.get("guild_id")) != gid:
                    continue
                if total is None:
                    total = data.get("member_count") or 0
                for op in data.get("ops", []):
                    if op.get("op") == "SYNC":
                        for item in op.get("items", []):
                            if "member" in item:
                                members.append(item["member"])
                if members:
                    break

            if not members:
                return

            yield members
            offset += BATCH
            await asyncio.sleep(0.05)

    async def get_profile(self, uid, gid):
        path = f"/users/{uid}/profile?guild_id={gid}&with_mutual_guilds=false"
        headers = {
            "Authorization": self.token,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "X-Super-Properties": "",
        }
        for attempt in range(5):
            try:
                async with self.session.get(API + path, headers=headers) as resp:
                    self.observe(resp.status, 0)
                    if resp.status == 200:
                        return await resp.json()
                    if resp.status == 429:
                        retry = float((await resp.json()).get("retry_after", 2 ** attempt))
                        await asyncio.sleep(min(retry, 30))
                        continue
                    if resp.status in (401, 403):
                        raise APIError(resp.status, path)
                    if resp.status == 404:
                        return {}
                    raise APIError(resp.status, path)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 4:
                    raise APIError(0, path) from None
                await asyncio.sleep(2 ** attempt)
        raise APIError(0, path)

    async def get(self, path, *, cdn=False, params=None):
        if cdn and path.startswith("/avatars/"):
            url = CDN + path
        else:
            raise ValueError("Use get_profile() for API calls")
        for attempt in range(4):
            try:
                async with self.session.get(url, allow_redirects=True) as response:
                    self.observe(response.status, 0)
                    if response.status == 200:
                        return await response.read(), response.content_type
                    if response.status == 429:
                        await asyncio.sleep(2 ** attempt)
                        continue
                    if response.status < 500:
                        raise APIError(response.status, path)
            except (aiohttp.ClientError, asyncio.TimeoutError):
                if attempt == 3:
                    raise APIError(0, path) from None
            await asyncio.sleep(2 ** attempt)
        raise APIError(0, path)
