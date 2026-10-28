"""Consent-gated memory candidates, review and retrieval within a single QQ scope."""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from typing import Literal

from pydantic import Field, field_validator, model_validator

from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import Model

MemoryStatus = Literal["candidate", "approved", "rejected", "archived"]
MemoryKind = Literal["conversation", "person", "event", "relationship", "self", "diary"]


class MemoryFields(Model):
    content: str = Field(min_length=1, max_length=1000)
    source: str = Field(default="管理员整理", min_length=1, max_length=120)
    kind: MemoryKind = "person"
    importance: int = Field(default=3, ge=1, le=5, strict=True)
    confidence: float = Field(default=0.7, ge=0, le=1, allow_inf_nan=False)
    long_term_allowed: bool = False
    expires_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @field_validator("content", "source")
    @classmethod
    def clean_text(cls, value):
        if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
            raise ValueError("不能包含控制字符")
        value = " ".join(value.split())
        if not value:
            raise ValueError("内容不能为空")
        return value


class MemoryCreate(MemoryFields):
    bot_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    user_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    group_id: str = Field(default="", pattern=r"^([1-9][0-9]{4,11})?$")


class MemoryEdit(MemoryFields):
    revision: int = Field(ge=0, strict=True)
    status: MemoryStatus = "candidate"

    @model_validator(mode="after")
    def consent(self):
        if self.status == "approved" and not self.long_term_allowed:
            raise ValueError("审核通过前必须确认已获准长期保存")
        return self


def fingerprint(bot_id, user_id, group_id, content):
    content = " ".join(unicodedata.normalize("NFKC", content).casefold().split())
    return hashlib.sha256(json.dumps([bot_id, group_id, user_id, content]).encode()).hexdigest()


class MemoryEngine:
    def __init__(self, config, db, clock=time.time):
        self.config, self.db, self.clock = config, db, clock

    @staticmethod
    def row(row):
        data = dict(row)
        data["long_term_allowed"] = bool(data["long_term_allowed"])
        data["visibility"] = "group" if data["group_id"] else "private"
        return data

    def expire(self, db):
        db.execute(
            """UPDATE memories SET status='archived',revision=revision+1,updated_at=?
            WHERE expires_at IS NOT NULL AND expires_at<=? AND status IN ('candidate','approved')""",
            (self.clock(), self.clock()),
        )

    def listing(self, query="", status="all", offset=0, limit=30):
        where, args = [], []
        if status != "all":
            where.append("status=?")
            args.append(status)
        if query:
            where.append(
                "(instr(content,?)>0 OR instr(source,?)>0 OR instr(user_id,?)>0 OR instr(group_id,?)>0)"
            )
            args.extend([query] * 4)
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.db.connect() as db:
            self.expire(db)
            total = db.execute("SELECT COUNT(*) FROM memories" + clause, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM memories"
                + clause
                + " ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
        return {"items": [self.row(row) for row in rows], "total": total}

    def create(self, data: MemoryCreate, *, capture_scope=None):
        values = data.model_dump()
        now = self.clock()
        values["expires_at"] = data.expires_at if data.expires_at is not None else now + 30 * 86400
        digest = fingerprint(data.bot_id, data.user_id, data.group_id, data.content)
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            if capture_scope is not None:
                preference = db.execute(
                    "SELECT enabled FROM memory_preferences WHERE scope=?", (capture_scope,)
                ).fetchone()
                if not self.config.current.memory.enabled or (preference and not preference[0]):
                    raise ValueError("当前会话记忆已关闭")
            old = db.execute("SELECT * FROM memories WHERE fingerprint=?", (digest,)).fetchone()
            if old:
                return {"item": self.row(old), "duplicate": True}
            if (
                db.execute(
                    "SELECT COUNT(*) FROM memories WHERE bot_id=? AND user_id=? AND group_id=? AND status='candidate'",
                    (data.bot_id, data.user_id, data.group_id),
                ).fetchone()[0]
                >= 50
            ):
                raise ValueError("本会话已有 50 条待审核候选，请先处理或删除")
            columns = ",".join(values)
            placeholders = ",".join("?" for _ in values)
            cursor = db.execute(
                f"INSERT INTO memories({columns},status,fingerprint,created_at,updated_at) VALUES ({placeholders},'candidate',?,?,?)",
                [*values.values(), digest, now, now],
            )
            item = self.row(
                db.execute("SELECT * FROM memories WHERE id=?", (cursor.lastrowid,)).fetchone()
            )
        self.db.audit("memory_created")
        return {"item": item, "duplicate": False}

    def edit(self, key, data: MemoryEdit):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            old = db.execute("SELECT * FROM memories WHERE id=?", (key,)).fetchone()
            if not old:
                raise LookupError("记忆不存在")
            if old["revision"] != data.revision:
                raise StateConflict("记忆已变化，请刷新后重新核对")
            if (
                data.status == "approved"
                and data.expires_at is not None
                and data.expires_at <= self.clock()
            ):
                raise ValueError("请先调整已过期的记忆有效期")
            digest = fingerprint(old["bot_id"], old["user_id"], old["group_id"], data.content)
            values = data.model_dump(exclude={"revision"})
            if data.status == "candidate" and data.expires_at is None:
                values["expires_at"] = self.clock() + 30 * 86400
            assignment = ",".join(f"{key}=?" for key in values)
            try:
                db.execute(
                    f"UPDATE memories SET {assignment},fingerprint=?,updated_at=?,revision=revision+1 WHERE id=?",
                    [*values.values(), digest, self.clock(), key],
                )
            except sqlite3.IntegrityError:
                raise StateConflict("本会话已有相同记忆，请编辑现有条目") from None
            item = self.row(db.execute("SELECT * FROM memories WHERE id=?", (key,)).fetchone())
        self.db.audit("memory_edited", data.status)
        return item

    def delete(self, key, revision):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM memories WHERE id=?", (key,)).fetchone()
            if not row:
                raise LookupError("记忆不存在")
            if row[0] != revision:
                raise StateConflict("记忆已变化，请刷新后重新核对")
            db.execute("DELETE FROM memories WHERE id=?", (key,))
        self.db.audit("memory_deleted")

    def permitted(self, event):
        if not self.config.current.memory.enabled:
            return False
        with self.db.connect() as db:
            row = db.execute(
                "SELECT enabled FROM memory_preferences WHERE scope=?", (event.scope,)
            ).fetchone()
        return row is None or bool(row[0])

    def preference(self, event, enabled):
        with self.db.connect() as db:
            db.execute(
                "INSERT INTO memory_preferences VALUES (?,?) ON CONFLICT(scope) DO UPDATE SET enabled=excluded.enabled",
                (event.scope, int(enabled)),
            )
        self.db.audit("memory_preference_changed")

    def forget(self, event):
        with self.db.connect() as db:
            count = db.execute(
                "DELETE FROM memories WHERE bot_id=? AND group_id=? AND user_id=?",
                (event.bot_id, event.group_id or "", event.user_id),
            ).rowcount
        self.db.audit("memory_forgotten")
        return count

    def capture(self, event, content):
        if not self.permitted(event):
            raise ValueError("本会话记忆已关闭。管理员启用功能后，可发送 /记忆 开启 恢复本会话。")
        return self.create(
            MemoryCreate(
                bot_id=event.bot_id,
                user_id=event.user_id,
                group_id=event.group_id or "",
                content=content,
                source="QQ 用户明确请求；消息 ID：" + event.message_id[:60],
                long_term_allowed=True,
            ),
            capture_scope=event.scope,
        )

    def recall(self, query="", offset=0, limit=10):
        """Internal cross-scope recall explicitly enabled by the administrator; paginated, never grants rights."""
        if not self.config.current.memory.enabled or not self.config.current.memory.global_context:
            return {"items": [], "total": 0}
        with self.db.connect() as db:
            self.expire(db)
            rows = db.execute(
                """SELECT m.* FROM memories m
                LEFT JOIN memory_preferences p ON p.scope=(CASE WHEN m.group_id='' THEN m.bot_id||':p:'||m.user_id ELSE m.bot_id||':g:'||m.group_id||':u:'||m.user_id END)
                WHERE m.status='approved' AND m.long_term_allowed=1
                AND (m.expires_at IS NULL OR m.expires_at>?) AND COALESCE(p.enabled,1)=1
                ORDER BY m.importance DESC,m.confidence DESC,m.updated_at DESC""",
                (self.clock(),),
            ).fetchall()
        terms = set(
            re.findall(
                r"[a-z0-9]+|[\u4e00-\u9fff]{1,2}", unicodedata.normalize("NFKC", query).casefold()
            )
        )
        ranked = sorted(
            rows,
            key=lambda row: sum(
                term in unicodedata.normalize("NFKC", row["content"]).casefold() for term in terms
            ),
            reverse=True,
        )
        return {
            "total": len(ranked),
            "items": [
                {
                    "id": r["id"],
                    "content": r["content"],
                    "kind": r["kind"],
                    "importance": r["importance"],
                    "confidence": r["confidence"],
                    "visibility": "group" if r["group_id"] else "private",
                    "source_scope": f"{r['bot_id']}:g:{r['group_id']}:u:{r['user_id']}"
                    if r["group_id"]
                    else f"{r['bot_id']}:p:{r['user_id']}",
                }
                for r in ranked[offset : offset + limit]
            ],
        }

    def action_context(self, query=""):
        policy = self.config.current.memory
        result = []
        for item in self.recall(query, limit=policy.max_items)["items"]:
            if len(json.dumps([*result, item], ensure_ascii=False)) <= policy.max_chars:
                result.append(item)
        return result

    def private_echo(self, text, scope=None):
        """Additional literal disclosure guard; semantic paraphrases still require model privacy rules."""
        normalized = "".join(unicodedata.normalize("NFKC", text).casefold().split())
        with self.db.connect() as db:
            rows = db.execute(
                "SELECT bot_id,user_id,group_id,content FROM memories WHERE long_term_allowed=1 AND status='approved'"
            ).fetchall()
        for row in rows:
            source = (
                f"{row['bot_id']}:g:{row['group_id']}:u:{row['user_id']}"
                if row["group_id"]
                else f"{row['bot_id']}:p:{row['user_id']}"
            )
            if source == scope:
                continue
            content = "".join(unicodedata.normalize("NFKC", row["content"]).casefold().split())
            if len(content) >= 8 and content in normalized:
                return True
        return False

    def retrieve(self, event):
        if not self.permitted(event):
            return []
        policy = self.config.current.memory
        with self.db.connect() as db:
            self.expire(db)
            rows = db.execute(
                """SELECT content,kind,importance,confidence,updated_at FROM memories
                WHERE bot_id=? AND group_id=? AND user_id=? AND status='approved'
                AND long_term_allowed=1 AND (expires_at IS NULL OR expires_at>?)
                ORDER BY importance DESC,confidence DESC,updated_at DESC LIMIT 200""",
                (event.bot_id, event.group_id or "", event.user_id, self.clock()),
            ).fetchall()

        def terms(text):
            text = unicodedata.normalize("NFKC", text).casefold()
            words = set(re.findall(r"[a-z0-9]+", text))
            words.update(
                text[i : i + 2]
                for i in range(len(text) - 1)
                if all("\u4e00" <= c <= "\u9fff" for c in text[i : i + 2])
            )
            return words

        query = terms(event.text)
        rows = sorted(
            rows,
            key=lambda r: (
                len(query & terms(r["content"])),
                r["importance"],
                r["confidence"],
                r["updated_at"],
            ),
            reverse=True,
        )
        result = []
        for row in rows:
            item = {key: row[key] for key in ("content", "kind", "importance", "confidence")}
            candidate = [*result, item]
            if len(json.dumps(candidate, ensure_ascii=False)) > policy.max_chars:
                continue
            result = candidate
            if len(result) >= policy.max_items:
                break
        return result
