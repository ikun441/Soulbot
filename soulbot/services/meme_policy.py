"""One persistent rolling minute quota shared by UI previews and AI generation."""

import json
import threading
import time

from soulbot.services.media_service import MediaError


class MemeRateLimiter:
    def __init__(self, config, db=None, clock=time.time):
        self.config, self.db, self.clock = config, db, clock
        self.lock, self.entries = threading.Lock(), []

    def check(self, entries, source, now, kind):
        entries = [entry for entry in entries if entry["at"] > now - 60]
        settings = self.config.current.meme
        if len(entries) >= settings.max_generations_per_minute:
            raise MediaError("表情包生成速度已达全局每分钟上限，请稍后再试")
        profile = settings.extensions.get(source)
        if (
            profile
            and sum(entry["source"] == source for entry in entries)
            >= profile.max_generations_per_minute
        ):
            raise MediaError("此扩展仓库的生成速度已达每分钟上限，请稍后再试")
        return [*entries, {"at": now, "source": source, "kind": kind}]

    def reserve(self, source, kind="generate"):
        now = self.clock()
        if self.db is None:
            with self.lock:
                self.entries = self.check(self.entries, source, now, kind)
            return
        with self.db.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key='meme_rate_window'"
            ).fetchone()
            try:
                entries = json.loads(row[0]) if row else []
                if not isinstance(entries, list):
                    raise ValueError
                entries = self.check(entries, source, now, kind)
            except (ValueError, TypeError, KeyError):
                raise MediaError("表情生成限额记录无法读取，请检查本机数据库") from None
            connection.execute(
                "INSERT OR REPLACE INTO metadata VALUES ('meme_rate_window',?)",
                (json.dumps(entries),),
            )
