import time


class ConversationStore:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock

    def unseen(self, event):
        with self.db.connect() as db:
            db.execute("DELETE FROM seen_events WHERE expires<?", (self.clock(),))
            return (
                db.execute(
                    "INSERT OR IGNORE INTO seen_events VALUES (?,?)",
                    (event.key, self.clock() + 86400),
                ).rowcount
                == 1
            )

    def history(self, scope, turns):
        with self.db.connect() as db:
            db.execute("DELETE FROM chat_context WHERE expires<=?", (self.clock(),))
            rows = db.execute(
                "SELECT role,content FROM chat_context WHERE scope=? ORDER BY id DESC LIMIT ?",
                (scope, turns * 2),
            ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def remember(self, scope, user, assistant, settings):
        now = self.clock()
        with self.db.connect() as db:
            db.executemany(
                "INSERT INTO chat_context(scope,role,content,expires) VALUES (?,?,?,?)",
                [
                    (scope, "user", user, now + settings.behavior.context_retention_hours * 3600),
                    (
                        scope,
                        "assistant",
                        assistant,
                        now + settings.behavior.context_retention_hours * 3600,
                    ),
                ],
            )
            db.execute(
                "DELETE FROM chat_context WHERE scope=? AND id NOT IN (SELECT id FROM chat_context WHERE scope=? ORDER BY id DESC LIMIT ?)",
                (scope, scope, settings.behavior.context_turns * 2),
            )
            db.execute(
                "INSERT INTO conversation_state VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET last_reply=excluded.last_reply",
                (scope, now),
            )

    def last_reply(self, scope):
        with self.db.connect() as db:
            row = db.execute(
                "SELECT last_reply FROM conversation_state WHERE scope=?", (scope,)
            ).fetchone()
        return row[0] if row else 0

    def clear(self):
        with self.db.connect() as db:
            db.execute("DELETE FROM chat_context")
            db.execute("DELETE FROM conversation_state")
