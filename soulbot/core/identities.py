"""Platform account IDs are authoritative; nicknames never confer owner privileges."""

import re
import time


class IdentityDirectory:
    def __init__(self, services):
        self.s = services

    @staticmethod
    def name(value):
        return "".join(c for c in str(value or "") if ord(c) >= 32)[:100]

    def observe(self, event):
        if not all(
            re.fullmatch(r"[1-9][0-9]{4,11}", str(value)) for value in (event.bot_id, event.user_id)
        ):
            return
        with self.s.db.connect() as db:
            db.execute(
                "INSERT INTO identity_accounts VALUES (?,?,?,?) ON CONFLICT(bot_id,user_id) DO UPDATE SET nickname=CASE WHEN excluded.nickname<>'' THEN excluded.nickname ELSE nickname END,updated_at=excluded.updated_at",
                (event.bot_id, event.user_id, self.name(event.user_name), time.time()),
            )
            if event.group_id:
                db.execute(
                    "INSERT INTO identity_groups VALUES (?,?,?,?) ON CONFLICT(bot_id,group_id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE name END,updated_at=excluded.updated_at",
                    (event.bot_id, event.group_id, self.name(event.group_name), time.time()),
                )
                db.execute(
                    "INSERT INTO identity_members VALUES (?,?,?,?) ON CONFLICT(bot_id,group_id,user_id) DO UPDATE SET card=excluded.card",
                    (event.bot_id, event.group_id, event.user_id, self.name(event.group_card)),
                )

    def context(self, event):
        owner = self.s.config.current.personality.owner_qq

        def user(qq):
            with self.s.db.connect() as db:
                row = db.execute(
                    "SELECT nickname FROM identity_accounts WHERE bot_id=? AND user_id=?",
                    (event.bot_id, qq),
                ).fetchone()
                card = db.execute(
                    "SELECT card FROM identity_members WHERE bot_id=? AND group_id=? AND user_id=?",
                    (event.bot_id, event.group_id or "", qq),
                ).fetchone()
                relation = db.execute(
                    "SELECT display_name,affinity FROM relationships WHERE bot_id=? AND user_id=? AND group_id=?",
                    (event.bot_id, qq, event.group_id or ""),
                ).fetchone()
            return {
                "qq": qq,
                "nickname": row[0] if row else "",
                "group_card": card[0] if card else "",
                "alias": relation[0] if relation else "",
                "affinity": relation[1] if relation else None,
                "is_owner": bool(owner and qq == owner),
            }

        with self.s.db.connect() as db:
            group = db.execute(
                "SELECT name FROM identity_groups WHERE bot_id=? AND group_id=?",
                (event.bot_id, event.group_id or ""),
            ).fetchone()
        return {
            "bot_qq": event.bot_id,
            "speaker": user(event.user_id),
            "owner_qq": owner,
            "group": {"id": event.group_id, "name": group[0] if group else ""},
            "mentioned": [user(qq) for qq in event.mentioned_users[:7]],
        }

    def listing(self, query=""):
        with self.s.db.connect() as db:
            rows = db.execute(
                """SELECT a.bot_id,a.user_id,a.nickname,m.group_id,m.card,g.name AS group_name,r.display_name AS alias,r.affinity FROM identity_accounts a
                LEFT JOIN identity_members m ON a.bot_id=m.bot_id AND a.user_id=m.user_id
                LEFT JOIN identity_groups g ON m.bot_id=g.bot_id AND m.group_id=g.group_id
                LEFT JOIN relationships r ON r.bot_id=a.bot_id AND r.user_id=a.user_id AND r.group_id=COALESCE(m.group_id,'')
                WHERE instr(a.user_id,?)>0 OR instr(a.nickname,?)>0 OR instr(COALESCE(m.group_id,''),?)>0 ORDER BY a.updated_at DESC LIMIT 200""",
                (query, query, query),
            ).fetchall()
        return {
            "items": [
                dict(row, is_owner=row["user_id"] == self.s.config.current.personality.owner_qq)
                for row in rows
            ]
        }
