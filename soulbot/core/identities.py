"""Platform account IDs are authoritative; nicknames never confer owner privileges."""

import re
import time


class IdentityDirectory:
    def __init__(self, services):
        self.s = services

    @staticmethod
    def name(value):
        return "".join(c for c in str(value or "") if ord(c) >= 32)[:100]

    @staticmethod
    def valid_qq(value):
        return bool(re.fullmatch(r"[1-9][0-9]{4,11}", str(value)))

    def remember_name(self, db, bot, user, group, kind, name, now):
        name = self.name(name)
        if not name:
            return
        db.execute(
            "INSERT INTO identity_aliases(bot_id,user_id,group_id,kind,name,first_seen,last_seen) VALUES (?,?,?,?,?,?,?) ON CONFLICT(bot_id,user_id,group_id,kind,name) DO UPDATE SET last_seen=MAX(last_seen,excluded.last_seen)",
            (bot, user, group, kind, name, now, now),
        )
        db.execute(
            "DELETE FROM identity_aliases WHERE bot_id=? AND user_id=? AND group_id=? AND kind=? AND id NOT IN (SELECT id FROM identity_aliases WHERE bot_id=? AND user_id=? AND group_id=? AND kind=? ORDER BY last_seen DESC,id DESC LIMIT 8)",
            (bot, user, group, kind, bot, user, group, kind),
        )

    def upsert_user(self, db, bot, user, nickname, now, remark=None):
        if not self.valid_qq(user):
            return False
        old = db.execute(
            "SELECT * FROM identity_accounts WHERE bot_id=? AND user_id=?", (bot, user)
        ).fetchone()
        name = self.name(nickname) or (old["nickname"] if old else "")
        note = self.name(remark) if remark is not None else old["remark"] if old else ""
        if old:
            self.remember_name(db, bot, user, "", "nickname", old["nickname"], old["updated_at"])
            self.remember_name(db, bot, user, "", "remark", old["remark"], old["updated_at"])
        self.remember_name(db, bot, user, "", "nickname", name, now)
        self.remember_name(db, bot, user, "", "remark", note, now)
        db.execute(
            "INSERT INTO identity_accounts(bot_id,user_id,nickname,updated_at,remark) VALUES (?,?,?,?,?) ON CONFLICT(bot_id,user_id) DO UPDATE SET nickname=excluded.nickname,remark=excluded.remark,updated_at=excluded.updated_at",
            (bot, user, name, now, note),
        )
        return True

    def upsert_group(self, db, bot, group, name, now):
        if not self.valid_qq(group):
            return False
        db.execute(
            "INSERT INTO identity_groups VALUES (?,?,?,?) ON CONFLICT(bot_id,group_id) DO UPDATE SET name=CASE WHEN excluded.name<>'' THEN excluded.name ELSE name END,updated_at=excluded.updated_at",
            (bot, group, self.name(name), now),
        )
        return True

    def upsert_member(self, db, bot, group, user, card, now):
        if not self.valid_qq(user) or not self.valid_qq(group):
            return False
        old = db.execute(
            "SELECT card FROM identity_members WHERE bot_id=? AND group_id=? AND user_id=?",
            (bot, group, user),
        ).fetchone()
        if old:
            self.remember_name(db, bot, user, group, "card", old[0], now)
        self.remember_name(db, bot, user, group, "card", card, now)
        db.execute(
            "INSERT INTO identity_members VALUES (?,?,?,?) ON CONFLICT(bot_id,group_id,user_id) DO UPDATE SET card=excluded.card",
            (bot, group, user, self.name(card)),
        )
        return True

    def observe(self, event):
        if not all(self.valid_qq(value) for value in (event.bot_id, event.user_id)):
            return
        now = time.time()
        with self.s.db.connect() as db:
            self.upsert_user(db, event.bot_id, event.user_id, event.user_name, now)
            if event.group_id:
                self.upsert_group(db, event.bot_id, event.group_id, event.group_name, now)
                self.upsert_member(
                    db, event.bot_id, event.group_id, event.user_id, event.group_card, now
                )

    def history(self, bot, user, group=None):
        with self.s.db.connect() as db:
            rows = db.execute(
                "SELECT id,kind,group_id,name,first_seen,last_seen FROM identity_aliases WHERE bot_id=? AND user_id=?"
                + (" AND (group_id='' OR group_id=?)" if group is not None else "")
                + " ORDER BY last_seen DESC,id DESC LIMIT 100",
                (bot, user, group) if group is not None else (bot, user),
            ).fetchall()
        return {"items": [dict(row) for row in rows]}

    def context(self, event):
        owner = self.s.config.current.personality.owner_qq

        def user(qq):
            with self.s.db.connect() as db:
                row = db.execute(
                    "SELECT nickname,remark,is_friend,friends_synced_at FROM identity_accounts WHERE bot_id=? AND user_id=?",
                    (event.bot_id, qq),
                ).fetchone()
                card = db.execute(
                    "SELECT card FROM identity_members WHERE bot_id=? AND group_id=? AND user_id=?",
                    (event.bot_id, event.group_id or "", qq),
                ).fetchone()
                relation = db.execute(
                    "SELECT display_name,affinity FROM relationships WHERE bot_id=? AND user_id=?",
                    (event.bot_id, qq),
                ).fetchone()
            return {
                "qq": qq,
                "nickname": row[0] if row else "",
                "group_card": card[0] if card else "",
                "remark": row[1] if row else "",
                "is_friend": bool(row[2]) if row and row[2] is not None else None,
                "friendship_checked_at": row[3] if row else None,
                "known_names": [
                    {"kind": item["kind"], "name": item["name"]}
                    for item in self.history(event.bot_id, qq, event.group_id or "")["items"][:5]
                ],
                "alias": relation[0]
                if relation and self.s.config.current.relationships.enabled
                else "",
                "affinity": relation[1]
                if relation and self.s.config.current.relationships.enabled
                else None,
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

    def listing(self, query="", offset=0, limit=100):
        where = """ FROM identity_accounts a
                LEFT JOIN identity_members m ON a.bot_id=m.bot_id AND a.user_id=m.user_id
                LEFT JOIN identity_groups g ON m.bot_id=g.bot_id AND m.group_id=g.group_id
                LEFT JOIN relationships r ON r.bot_id=a.bot_id AND r.user_id=a.user_id
                WHERE instr(a.user_id,?)>0 OR instr(a.nickname,?)>0 OR instr(a.remark,?)>0 OR instr(COALESCE(m.group_id,''),?)>0 OR instr(COALESCE(m.card,''),?)>0 OR EXISTS(SELECT 1 FROM identity_aliases h WHERE h.bot_id=a.bot_id AND h.user_id=a.user_id AND instr(h.name,?)>0)"""
        args = (query,) * 6
        with self.s.db.connect() as db:
            total = db.execute("SELECT COUNT(*)" + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT a.bot_id,a.user_id,a.nickname,a.remark,a.is_friend,a.friends_synced_at,m.group_id,m.card,g.name AS group_name,r.display_name AS alias,r.affinity"
                + where
                + " ORDER BY a.updated_at DESC,a.user_id,m.group_id LIMIT ? OFFSET ?",
                (*args, limit, offset),
            ).fetchall()
        return {
            "items": [
                dict(row, is_owner=row["user_id"] == self.s.config.current.personality.owner_qq)
                for row in rows
            ],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def forget_name(self, bot, user, key, last_seen):
        from soulbot.core.social_state import StateConflict

        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT last_seen FROM identity_aliases WHERE id=? AND bot_id=? AND user_id=?",
                (key, bot, user),
            ).fetchone()
            if not row:
                raise LookupError("历史名称已不存在")
            if row[0] != last_seen:
                raise StateConflict("该名称刚被新的平台信息更新，请刷新后确认")
            db.execute("DELETE FROM identity_aliases WHERE id=?", (key,))
        self.s.db.audit("identity_history_deleted")
