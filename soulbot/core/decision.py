import random
import time
from datetime import datetime
from zoneinfo import ZoneInfo


class DecisionEngine:
    def __init__(self, db, conversation, clock=time.time, rng=random.random):
        self.db, self.conversation, self.clock, self.rng = db, conversation, clock, rng

    @staticmethod
    def group_policy(settings, event):
        return settings.behavior.group_policies.get(event.group_id)

    def access(self, settings, event):
        if (
            not settings.qq.enabled
            or event.user_id == event.bot_id
            or event.is_bot
            or event.user_id in settings.behavior.ignored_bot_ids
        ):
            return False
        if not event.group_id:
            return settings.behavior.private_chat
        policy = self.group_policy(settings, event)
        return not (
            event.group_id in settings.behavior.group_blocklist
            or (
                settings.behavior.group_allowlist
                and event.group_id not in settings.behavior.group_allowlist
            )
            or (policy and not policy.enabled)
        )

    def mode(self, settings, event):
        policy = self.group_policy(settings, event)
        return policy.mode if policy else settings.behavior.group_mode

    def state(self, event):
        with self.db.connect() as db:
            db.execute("INSERT OR IGNORE INTO group_state(scope) VALUES (?)", (event.group_scope,))
            return dict(
                db.execute(
                    "SELECT * FROM group_state WHERE scope=?", (event.group_scope,)
                ).fetchone()
            )

    def quiet(self, settings):
        return (
            settings.behavior.quiet_hours
            and datetime.fromtimestamp(self.clock(), ZoneInfo(settings.soulbot.timezone)).hour < 8
        )

    def classify(self, settings, event, roll=True):
        if not self.access(settings, event) or not event.text.strip() or len(event.text) > 12000:
            return None
        if not event.group_id:
            return "direct"
        state = self.state(event)
        if state["muted_until"] > self.clock() or self.mode(settings, event) == "silent":
            return None
        words = [settings.soulbot.name, *settings.behavior.wake_words]
        wake = any(event.text.strip().casefold().startswith(word.casefold()) for word in words)
        if event.directed or event.reply_target == "bot" or wake:
            with self.db.connect() as db:
                db.execute(
                    "UPDATE group_state SET unanswered=0 WHERE scope=?", (event.group_scope,)
                )
            return "direct"
        if event.mentions_other or event.reply_target == "other":
            return None
        elapsed = self.clock() - self.conversation.last_reply(event.scope)
        if (
            0 <= elapsed <= settings.behavior.followup_window_seconds
            and event.text.strip().startswith(("你", "那你", "继续", "再说", "还有吗"))
        ):
            return "followup"
        policy = self.group_policy(settings, event)
        mode = self.mode(settings, event)
        if (
            self.quiet(settings)
            or not settings.behavior.proactive_group_enabled
            or not policy
            or not policy.allow_proactive
            or mode not in {"natural", "active"}
        ):
            return None
        if state["pause_until"] > self.clock():
            return None
        return (
            "proactive" if not roll or self.rng() < (0.08 if mode == "natural" else 0.20) else None
        )

    def claim(self, settings, event, trigger):
        now = self.clock()
        day = datetime.fromtimestamp(now, ZoneInfo(settings.soulbot.timezone)).date().isoformat()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO group_state(scope) VALUES (?)", (event.group_scope,))
            row = db.execute(
                "SELECT * FROM group_state WHERE scope=?", (event.group_scope,)
            ).fetchone()
            if now - row["last_call"] < settings.behavior.reply_cooldown_seconds:
                return False
            if trigger == "proactive":
                count = row["proactive_count"] if row["day"] == day else 0
                if (
                    row["pause_until"] > now
                    or now - row["last_proactive"] < settings.behavior.proactive_interval_seconds
                    or count >= settings.behavior.max_proactive_per_day
                ):
                    return False
                db.execute(
                    "UPDATE group_state SET last_proactive=?,day=?,proactive_count=? WHERE scope=?",
                    (now, day, count + 1, event.group_scope),
                )
            db.execute("UPDATE group_state SET last_call=? WHERE scope=?", (now, event.group_scope))
        return True

    def proactive_sent(self, event):
        with self.db.connect() as db:
            db.execute(
                "UPDATE group_state SET unanswered=unanswered+1 WHERE scope=?", (event.group_scope,)
            )
            db.execute(
                "UPDATE group_state SET pause_until=?,unanswered=0 WHERE scope=? AND unanswered>=3",
                (self.clock() + 900, event.group_scope),
            )

    def mute(self, event, seconds):
        with self.db.connect() as db:
            db.execute("INSERT OR IGNORE INTO group_state(scope) VALUES (?)", (event.group_scope,))
            db.execute(
                "UPDATE group_state SET muted_until=? WHERE scope=?",
                (self.clock() + seconds if seconds else 0, event.group_scope),
            )

    def reserve_send(self, settings):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT value FROM metadata WHERE key='last_send_at'").fetchone()
            delay = (
                max(
                    0, float(row[0]) + settings.behavior.global_send_interval_seconds - self.clock()
                )
                if row
                else 0
            )
            if delay == 0:
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES ('last_send_at',?)",
                    (str(self.clock()),),
                )
            return delay
