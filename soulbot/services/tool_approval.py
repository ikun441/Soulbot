"""Single-use, owner-bound tool proposals. Approval never automatically sends to QQ."""

import hashlib
import json
import time
from dataclasses import asdict
from uuid import uuid4

from soulbot.core.events import ChatEvent
from soulbot.core.social_state import StateConflict
from soulbot.tools.registry import ToolContext


class ToolApprovals:
    def __init__(self, services, clock=time.time):
        self.s, self.clock = services, clock

    def recover(self, db):
        db.execute(
            "UPDATE tool_approvals SET status='expired',revision=revision+1 WHERE status='pending' AND created_at<?",
            (self.clock() - 86400,),
        )
        db.execute(
            "UPDATE tool_approvals SET status='uncertain',revision=revision+1 WHERE status='running' AND updated_at<?",
            (self.clock() - 900,),
        )

    def enqueue(self, name, arguments, context):
        owner = self.s.config.current.personality.owner_qq
        if not owner or context.scope is None:
            raise PermissionError("工具审批需要配置主人和有效会话")
        data = arguments.model_dump(mode="json")
        raw_context = {
            "scope": context.scope,
            "purpose": context.purpose,
            "event": asdict(context.event) if context.event else None,
        }
        digest = hashlib.sha256(
            json.dumps([name, data, raw_context, owner], sort_keys=True).encode()
        ).hexdigest()
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            old = db.execute(
                "SELECT id FROM tool_approvals WHERE fingerprint=? AND status='pending'", (digest,)
            ).fetchone()
            if old:
                key = old[0]
            else:
                if (
                    db.execute(
                        "SELECT COUNT(*) FROM tool_approvals WHERE status IN ('pending','running')"
                    ).fetchone()[0]
                    >= 100
                ):
                    raise PermissionError("工具待审批队列已满")
                key = uuid4().hex
                db.execute(
                    "INSERT INTO tool_approvals(id,fingerprint,name,arguments_json,context_json,owner,status,created_at,updated_at) VALUES (?,?,?,?,?,?,'pending',?,?)",
                    (
                        key,
                        digest,
                        name,
                        json.dumps(data),
                        json.dumps(raw_context),
                        owner,
                        self.clock(),
                        self.clock(),
                    ),
                )
        self.s.db.audit("tool_approval_created", name)
        return {
            "approval_id": key,
            "status": "pending",
            "message": "已进入主人审批队列，工具尚未执行，不要声称已完成。审批只生成本地结果，不自动向 QQ 发送。",
        }

    def listing(self):
        with self.s.db.connect() as db:
            self.recover(db)
            rows = db.execute(
                "SELECT id,name,arguments_json,status,result_json,created_at,revision FROM tool_approvals ORDER BY created_at DESC LIMIT 100"
            ).fetchall()
        return {
            "items": [
                {
                    **dict(row),
                    "arguments": json.loads(row["arguments_json"]),
                    "result": json.loads(row["result_json"]) if row["result_json"] else None,
                }
                for row in rows
            ]
        }

    async def review(self, key, revision, approve):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            row = db.execute("SELECT * FROM tool_approvals WHERE id=?", (key,)).fetchone()
            if not row:
                raise LookupError
            if (
                row["revision"] != revision
                or row["status"] != "pending"
                or row["owner"] != self.s.config.current.personality.owner_qq
            ):
                raise StateConflict("审批已变化、过期或主人已更换")
            if approve and getattr(self.s.config.current.tools, row["name"], "deny") != "approval":
                raise StateConflict("工具权限已改变，请取消并重新提出操作")
            db.execute(
                "UPDATE tool_approvals SET status=?,updated_at=?,revision=revision+1 WHERE id=?",
                ("running" if approve else "rejected", self.clock(), key),
            )
        if not approve:
            return {"ok": True, "status": "rejected"}
        try:
            context = json.loads(row["context_json"])
            event = ChatEvent(**context["event"]) if context["event"] else None
            result = await self.s.tools.execute(
                row["name"],
                json.loads(row["arguments_json"]),
                ToolContext(
                    scope=context["scope"],
                    purpose="explicit",
                    event=event,
                    approved_tools=frozenset({row["name"]}),
                ),
            )
            with self.s.db.connect() as db:
                db.execute(
                    "UPDATE tool_approvals SET status='completed',result_json=?,updated_at=?,revision=revision+1 WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), self.clock(), key),
                )
            self.s.db.audit("tool_approval_completed", row["name"])
            return {"ok": True, "status": "completed", "result": result}
        except BaseException:
            with self.s.db.connect() as db:
                db.execute(
                    "UPDATE tool_approvals SET status='uncertain',updated_at=?,revision=revision+1 WHERE id=?",
                    (self.clock(), key),
                )
            self.s.db.audit("tool_approval_uncertain", row["name"])
            raise
