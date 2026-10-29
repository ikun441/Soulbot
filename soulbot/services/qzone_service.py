"""Public-input drafts with configurable review and bounded autonomous publication."""

import asyncio
import hashlib
import json
import random
import re
import time
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator

from soulbot.core.permissions import Permission, PermissionEngine
from soulbot.core.social_state import StateConflict
from soulbot.providers.llm.base import ProviderError
from soulbot.services.config_service import Model

AUDIENCES = {"friends": 4, "self": 64, "public": 1}
STATUS = {
    "generating": "正在起草",
    "generation_failed": "起草未完成",
    "pending_approval": "待审批",
    "approved": "已审批",
    "rejected": "已拒绝",
    "cancelled": "已撤销",
    "publishing": "正在发布",
    "published": "平台已确认",
    "uncertain": "结果不确定，请人工核对空间",
}
EDITABLE = {"pending_approval", "approved", "rejected"}


class DraftCreate(Model):
    request_id: UUID
    bot_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    content: str = Field(min_length=1, max_length=2000)
    audience: Literal["friends", "self", "public"] = "friends"
    mode: Literal["manual", "ai"] = "manual"

    @field_validator("content")
    @classmethod
    def clean(cls, value):
        if not value.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise ValueError("请填写正常的公开文字")
        return value.strip()


class DraftEdit(Model):
    revision: int = Field(ge=0, strict=True)
    content: str = Field(min_length=1, max_length=2000)
    audience: Literal["friends", "self", "public"]
    clean = field_validator("content")(DraftCreate.clean.__func__)


class DraftReview(Model):
    revision: int = Field(ge=0, strict=True)
    action: Literal["approve", "reject", "cancel"]
    public_confirmed: bool = Field(default=False, strict=True)


class DraftPublish(Model):
    revision: int = Field(ge=0, strict=True)


def privacy_issues(content):
    """Heuristics, not a guarantee. No private database is consulted or copied."""
    rules = {
        "疑似账号、手机号或长数字标识": r"\d{5,}",
        "疑似邮箱": r"[\w.+-]+@[\w.-]+\.[a-zA-Z]{2,}",
        "疑似密钥或凭据": r"(?i)(?:\b(?:sk|ghp|gho)-?[a-z0-9_]{12,}|api[_ -]?key\s*[:=]|access[_ -]?token\s*[:=]|password\s*[:=]|密码\s*[:：=])",
        "疑似私人来源标记": r"(?i)(?:私聊记录|私人记忆|私人日记|聊天记录摘录|BEGIN (?:RSA |OPENSSH )?PRIVATE KEY)",
        "带凭据或本机地址的链接": r"(?i)(?:https?://[^\s/]+@|https?://(?:localhost|127\.|192\.168\.|10\.\d|172\.(?:1[6-9]|2\d|3[01])\.)|[?&](?:token|key|password)=)",
    }
    return [label for label, pattern in rules.items() if re.search(pattern, content)]


class QZoneService:
    def __init__(self, services, clock=time.time):
        self.s, self.clock = services, clock
        self.permissions = PermissionEngine({"publish_qzone": Permission.APPROVAL})
        self.task = None
        self.closed = False
        self.tick_lock = asyncio.Lock()

    @staticmethod
    def digest(value):
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def content_hash(self, row):
        return self.digest([row["bot_id"], row["content"], row["audience"]])

    def record(self, db, key, status, reason=""):
        db.execute(
            "INSERT INTO qzone_events(draft_id,status,reason,created_at) VALUES (?,?,?,?)",
            (key, status, reason, self.clock()),
        )

    def recover(self, db):
        for row in db.execute(
            "SELECT id,status FROM qzone_drafts WHERE status IN ('generating','publishing') AND lease_until<=?",
            (self.clock(),),
        ).fetchall():
            status = "uncertain" if row["status"] == "publishing" else "generation_failed"
            db.execute(
                "UPDATE qzone_drafts SET status=?,reason='interrupted',revision=revision+1,updated_at=? WHERE id=?",
                (status, self.clock(), row["id"]),
            )
            db.execute(
                "UPDATE qzone_attempts SET status='uncertain' WHERE draft_id=?", (row["id"],)
            )
            self.record(db, row["id"], status, "interrupted")

    @staticmethod
    def row(db, key):
        row = db.execute("SELECT * FROM qzone_drafts WHERE id=?", (key,)).fetchone()
        if row is None:
            raise LookupError
        return row

    def public(self, row):
        item = dict(row)
        for name in ("input_hash", "approved_hash", "lease_until"):
            item.pop(name, None)
        item["status_label"] = (
            "可直接发布"
            if item["status"] == "pending_approval"
            and not self.s.config.current.qzone.require_approval
            else STATUS[item["status"]]
        )
        item["approval_required"] = self.s.config.current.qzone.require_approval
        item["privacy_issues"] = self.content_issues(item["content"])
        item["approval_expired"] = bool(
            item["approved_at"] is not None and item["approved_at"] + 86400 <= self.clock()
        )
        return item

    def get(self, key):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            item = self.public(self.row(db, key))
            item["events"] = [
                dict(r)
                for r in db.execute(
                    "SELECT status,reason,created_at FROM qzone_events WHERE draft_id=? ORDER BY id",
                    (key,),
                )
            ]
            return item

    def listing(self, offset=0, limit=30):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            return {
                "total": db.execute("SELECT COUNT(*) FROM qzone_drafts").fetchone()[0],
                "items": [
                    self.public(r)
                    for r in db.execute(
                        "SELECT * FROM qzone_drafts ORDER BY id DESC LIMIT ? OFFSET ?",
                        (limit, offset),
                    )
                ],
            }

    async def create(self, data, *, autonomous=False, from_chat=False):
        fingerprint = self.digest(data.model_dump(mode="json"))
        if from_chat and (autonomous or data.mode != "manual"):
            raise ValueError("QQ 工具只能保存已经提供正文的草稿")
        source = (
            "model_chat_draft"
            if from_chat
            else "autonomous_public_topic"
            if autonomous
            else "ai_public_topic"
            if data.mode == "ai"
            else "manual_public_input"
        )
        if data.mode == "ai" and self.content_issues(data.content):
            raise ValueError("公开主题包含疑似私人信息，请先移除")
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            old = db.execute(
                "SELECT * FROM qzone_drafts WHERE request_id=?", (str(data.request_id),)
            ).fetchone()
            if old:
                if old["input_hash"] != fingerprint:
                    raise StateConflict("同一请求标识不能用于不同草稿")
                return self.public(old)
            if (
                db.execute(
                    "SELECT COUNT(*) FROM qzone_drafts WHERE status IN ('generating','pending_approval','approved')"
                ).fetchone()[0]
                >= 200
            ):
                raise ValueError("请先处理待完成草稿（最多 200 条）")
            if (
                data.mode == "ai"
                and db.execute(
                    "SELECT 1 FROM qzone_drafts WHERE source IN ('ai_public_topic','autonomous_public_topic') AND created_at>?",
                    (self.clock() - 60,),
                ).fetchone()
            ):
                raise ValueError("AI 起草每分钟最多一次")
            status = "generating" if data.mode == "ai" else "pending_approval"
            key = db.execute(
                """INSERT INTO qzone_drafts(request_id,input_hash,bot_id,content,audience,source,status,reason,created_at,updated_at,lease_until)
                VALUES (?,?,?,?,?,?,?,'',?,?,?)""",
                (
                    str(data.request_id),
                    fingerprint,
                    data.bot_id,
                    "" if data.mode == "ai" else data.content,
                    data.audience,
                    source,
                    status,
                    self.clock(),
                    self.clock(),
                    self.clock() + 120 if data.mode == "ai" else None,
                ),
            ).lastrowid
            self.record(db, key, status)
        if data.mode == "ai":
            try:
                async with asyncio.timeout(90):
                    result = await self.s.llm.complete(
                        [
                            {
                                "role": "system",
                                "content": "仅根据用户提供的公开主题写一条适合 QQ 空间的中文草稿，最多 600 字。不要编造角色实际经历、引用私聊、添加账号或个人隐私。不调用工具。仅返回正文，必须由管理员审批后才可能发布。",
                            },
                            {"role": "user", "content": data.content},
                        ],
                        purpose="qzone_autonomous" if autonomous else "qzone_draft",
                        output_limit=512,
                    )
                content = DraftCreate.clean(result.text)
                if len(content) > 2000 or result.tool_calls:
                    raise ValueError("模型未返回受支持的纯文字草稿")
                with self.s.db.connect() as db:
                    changed = db.execute(
                        "UPDATE qzone_drafts SET content=?,status='pending_approval',lease_until=NULL,updated_at=?,revision=revision+1 WHERE id=? AND status='generating'",
                        (content, self.clock(), key),
                    ).rowcount
                    if changed:
                        self.record(db, key, "pending_approval")
            except BaseException as error:
                with self.s.db.connect() as db:
                    changed = db.execute(
                        "UPDATE qzone_drafts SET status='generation_failed',reason='generation_failed',updated_at=?,revision=revision+1 WHERE id=? AND status='generating'",
                        (self.clock(), key),
                    ).rowcount
                    if changed:
                        self.record(db, key, "generation_failed", "generation_failed")
                if isinstance(error, (asyncio.CancelledError, ProviderError)):
                    raise
                if not isinstance(error, Exception):
                    raise
                raise ValueError("AI 起草未完成，请检查模型状态后创建新草稿") from None
        self.s.db.audit("qzone_draft_created")
        return self.get(key)

    def edit(self, key, data):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = self.row(db, key)
            if row["revision"] != data.revision or row["status"] not in EDITABLE:
                raise StateConflict("草稿已变化或无法编辑，请刷新")
            db.execute(
                "UPDATE qzone_drafts SET content=?,audience=?,status='pending_approval',reason='',approved_hash=NULL,approved_owner=NULL,approved_at=NULL,updated_at=?,revision=revision+1 WHERE id=?",
                (data.content, data.audience, self.clock(), key),
            )
            self.record(db, key, "pending_approval", "edited")
        self.s.db.audit("qzone_draft_edited")
        return self.get(key)

    def review(self, key, data):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            row = self.row(db, key)
            if row["revision"] != data.revision or row["status"] not in EDITABLE:
                raise StateConflict("草稿已变化或无法审批，请刷新")
            owner = self.s.config.current.personality.owner_qq
            if data.action == "approve" and (
                not owner or not data.public_confirmed or self.content_issues(row["content"])
            ):
                raise ValueError("请设置主人 QQ、移除隐私提示并确认正文可公开")
            status = {"approve": "approved", "reject": "rejected", "cancel": "cancelled"}[
                data.action
            ]
            approved = status == "approved"
            db.execute(
                "UPDATE qzone_drafts SET status=?,reason='',approved_hash=?,approved_owner=?,approved_at=?,updated_at=?,revision=revision+1 WHERE id=?",
                (
                    status,
                    self.content_hash(row) if approved else None,
                    owner if approved else None,
                    self.clock() if approved else None,
                    self.clock(),
                    key,
                ),
            )
            self.record(db, key, status)
        self.s.db.audit("qzone_draft_reviewed", status)
        return self.get(key)

    def quota(self, db):
        policy = self.s.config.current.qzone
        last = db.execute("SELECT MAX(created_at) FROM qzone_attempts").fetchone()[0]
        recent = [
            r[0]
            for r in db.execute(
                "SELECT created_at FROM qzone_attempts WHERE created_at>? ORDER BY created_at DESC",
                (self.clock() - 7 * 86400,),
            )
        ]
        next_at = (last + policy.min_interval_hours * 3600) if last is not None else 0
        if len(recent) >= policy.max_posts_per_week:
            next_at = max(next_at, recent[policy.max_posts_per_week - 1] + 7 * 86400)
        return {"used_last_seven_days": len(recent), "next_allowed_at": max(next_at, self.clock())}

    def content_issues(self, content):
        issues = privacy_issues(content)
        memory = getattr(self.s, "memory", None)
        if memory and memory.private_echo(content):
            issues.append("包含其它会话记忆原文")
        return issues

    def status(self):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            quota = self.quota(db)
        transport = self.s.qq_transport
        return {
            "enabled": self.s.config.current.qzone.enabled,
            "bots": transport.connected() if transport else [],
            "min_interval_hours": self.s.config.current.qzone.min_interval_hours,
            "max_posts_per_week": self.s.config.current.qzone.max_posts_per_week,
            "approval_required": self.s.config.current.qzone.require_approval,
            "autonomous_enabled": self.s.config.current.qzone.autonomous_enabled,
            "auto_bot_id": self.s.config.current.qzone.auto_bot_id,
            "auto_audience": self.s.config.current.qzone.auto_audience,
            "autonomous_state": json.loads(self.s.db.get("qzone_auto_state") or "{}"),
            "autonomous_wait_reason": self.auto_reason(),
            "approval_lifetime_hours": 24,
            **quota,
        }

    async def capability(self, bot_id):
        transport = self.s.qq_transport
        if (
            not self.s.config.current.qq.enabled
            or transport is None
            or bot_id not in transport.connected()
        ):
            return {
                "supported": False,
                "state": "disconnected",
                "message": "SoulBot 尚未连接此 QQ；草稿和审批仍可使用",
            }
        if not hasattr(transport, "qzone_capability"):
            return {
                "supported": False,
                "state": "unsupported",
                "message": "当前适配层未提供已核实的空间发布能力",
            }
        try:
            async with asyncio.timeout(16):
                return await transport.qzone_capability(bot_id)
        except Exception:
            return {
                "supported": False,
                "state": "unverified",
                "message": "只读检测未完成，请检查 NapCat 登录和连接",
            }

    def allowed(self, row, *, autonomous=False):
        cfg = self.s.config.current
        if not (
            cfg.qzone.enabled
            and cfg.qq.enabled
            and cfg.personality.owner_qq
            and row["content"]
            and not self.content_issues(row["content"])
        ):
            return False
        if (
            autonomous
            and cfg.behavior.quiet_hours
            and datetime.fromtimestamp(self.clock(), ZoneInfo(cfg.soulbot.timezone)).hour < 8
        ):
            return False
        if autonomous and not (
            not cfg.automation.paused
            and cfg.qzone.autonomous_enabled
            and not cfg.qzone.require_approval
            and row["source"] == "autonomous_public_topic"
            and row["bot_id"] == cfg.qzone.auto_bot_id
            and row["audience"] == cfg.qzone.auto_audience
        ):
            return False
        approved = (
            row["approved_owner"] == cfg.personality.owner_qq
            and row["approved_at"] is not None
            and row["approved_at"] + 86400 > self.clock()
            and row["approved_hash"] == self.content_hash(row)
        )
        if cfg.qzone.require_approval and not approved:
            return False
        try:
            # Explicit configuration grants auto authority; never forge a human review.
            if cfg.qzone.require_approval:
                self.permissions.require("publish_qzone", approved=approved)
            else:
                if self.permissions.policies.get("publish_qzone") == Permission.DENY:
                    return False
                PermissionEngine({"publish_qzone": Permission.AUTO}).require("publish_qzone")
            return True
        except PermissionError:
            return False

    async def publish(self, key, data, *, autonomous=False):
        # Check local authorization before any adapter request, and again after probing.
        initial = self.get(key)
        auto_policy = self.auto_policy_key() if autonomous else None
        with self.s.db.connect() as db:
            row = self.row(db, key)
            if (
                row["revision"] != data.revision
                or row["status"] not in {"approved", "pending_approval"}
                or not self.allowed(row, autonomous=autonomous)
            ):
                raise StateConflict("请检查发布开关、主人 QQ、隐私提示和当前审批模式")
        transport = self.s.qq_transport
        capability = await self.capability(initial["bot_id"])
        if not capability.get("supported") or not capability.get("connection"):
            raise ValueError("未确认当前账号的已核实接口能力，已保留草稿")
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.recover(db)
            row = self.row(db, key)
            if (
                row["revision"] != data.revision
                or row["status"] not in {"approved", "pending_approval"}
                or not self.allowed(row, autonomous=autonomous)
                or transport is not self.s.qq_transport
                or (autonomous and auto_policy != self.auto_policy_key())
            ):
                raise StateConflict("权限、连接或草稿已变化，请刷新后重新审批")
            if self.quota(db)["next_allowed_at"] > self.clock():
                raise StateConflict(
                    "未到下次发布窗口：至少间隔 72 小时、滚动 7 天最多两次；不确定结果也计入"
                )
            now = self.clock()
            db.execute(
                "INSERT INTO qzone_attempts VALUES (?,?,?,'reserved')", (key, row["bot_id"], now)
            )
            db.execute(
                "UPDATE qzone_drafts SET status='publishing',lease_until=?,updated_at=?,revision=revision+1 WHERE id=?",
                (now + 90, now, key),
            )
            self.record(
                db,
                key,
                "publishing",
                "autonomous_policy"
                if autonomous
                else "auto_policy"
                if not self.s.config.current.qzone.require_approval
                else "",
            )
            payload = dict(row)

        def guard():
            with self.s.db.connect() as db:
                current = self.row(db, key)
                return (
                    transport is self.s.qq_transport
                    and current["status"] == "publishing"
                    and current["lease_until"] > self.clock()
                    and self.allowed(current, autonomous=autonomous)
                    and (not autonomous or auto_policy == self.auto_policy_key())
                )

        status, tid = "uncertain", None
        try:
            async with asyncio.timeout(30):
                result = await transport.qzone_publish(
                    payload["bot_id"],
                    payload["content"],
                    AUDIENCES[payload["audience"]],
                    capability["connection"],
                    guard,
                )
            if (
                isinstance(result, dict)
                and isinstance(result.get("tid"), str)
                and re.fullmatch(r"[a-zA-Z0-9_-]{1,200}", result["tid"])
            ):
                status, tid = "published", result["tid"]
        except Exception:
            pass  # The platform may have accepted the request; never expose upstream text.
        finally:
            # Every dispatched/cancelled/unknown attempt is terminal. No automatic retry.
            with self.s.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                changed = db.execute(
                    "UPDATE qzone_drafts SET status=?,tid=?,reason=?,updated_at=?,revision=revision+1 WHERE id=? AND status='publishing'",
                    (status, tid, "" if tid else "check_platform", self.clock(), key),
                ).rowcount
                if changed:
                    db.execute("UPDATE qzone_attempts SET status=? WHERE draft_id=?", (status, key))
                    self.record(db, key, status, "" if tid else "check_platform")
                else:
                    status = "uncertain"
            self.s.db.audit("qzone_publish_result", status)
        return self.get(key)

    def auto_policy_key(self):
        cfg = self.s.config.current
        return self.digest(
            [cfg.qzone.model_dump(), cfg.personality.owner_qq, cfg.automation.paused]
        )

    def auto_reason(self):
        cfg = self.s.config.current
        if cfg.automation.paused or not cfg.qzone.enabled or not cfg.qzone.autonomous_enabled:
            return "disabled"
        if cfg.qzone.require_approval:
            return "approval_required"
        if not cfg.personality.owner_qq or not cfg.qq.enabled:
            return "not_configured"
        if (
            cfg.behavior.quiet_hours
            and datetime.fromtimestamp(self.clock(), ZoneInfo(cfg.soulbot.timezone)).hour < 8
        ):
            return "quiet_hours"
        if any(self.content_issues(topic) for topic in cfg.qzone.auto_topics):
            return "private_topic"
        transport = self.s.qq_transport
        if not transport or cfg.qzone.auto_bot_id not in transport.connected():
            return "disconnected"
        with self.s.db.connect() as db:
            if self.quota(db)["next_allowed_at"] > self.clock():
                return "quota"
        return ""

    def auto_state(self, *, lease_id=None, **changes):
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            state = json.loads(
                (
                    db.execute("SELECT value FROM metadata WHERE key='qzone_auto_state'").fetchone()
                    or ["{}"]
                )[0]
            )
            if lease_id is not None and state.get("request_id") != lease_id:
                return
            state.update(changes)
            db.execute(
                "INSERT INTO metadata VALUES ('qzone_auto_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(state),),
            )

    async def tick(self):
        if self.tick_lock.locked():
            return
        async with self.tick_lock:
            if self.auto_reason():
                return
            now = self.clock()
            with self.s.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                state = json.loads(
                    (
                        db.execute(
                            "SELECT value FROM metadata WHERE key='qzone_auto_state'"
                        ).fetchone()
                        or ["{}"]
                    )[0]
                )
                if state.get("lease_until", 0) > now or state.get("next_check_at", 0) > now:
                    return
                state = {
                    "lease_until": now + 180,
                    "next_check_at": now + 600 + random.uniform(0, 60),
                    "reason": "evaluating",
                    "request_id": str(uuid4()),
                    "last_check_at": now,
                }
                db.execute(
                    "INSERT INTO metadata VALUES ('qzone_auto_state',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(state),),
                )
            try:
                cfg = self.s.config.current.qzone.model_copy(deep=True)
                policy = self.auto_policy_key()
                capability = await self.capability(cfg.auto_bot_id)
                if not capability.get("supported"):
                    self.auto_state(lease_id=state["request_id"], reason="unverified_capability")
                    return
                lease = json.loads(self.s.db.get("qzone_auto_state") or "{}")
                if (
                    self.auto_reason()
                    or policy != self.auto_policy_key()
                    or lease.get("request_id") != state["request_id"]
                    or lease.get("lease_until", 0) <= self.clock()
                ):
                    self.auto_state(lease_id=state["request_id"], reason="policy_changed")
                    return
                topic = cfg.auto_topics[int(now // 86400) % len(cfg.auto_topics)]
                row = await self.create(
                    DraftCreate(
                        request_id=state["request_id"],
                        bot_id=cfg.auto_bot_id,
                        content=topic,
                        audience=cfg.auto_audience,
                        mode="ai",
                    ),
                    autonomous=True,
                )
                self.auto_state(lease_id=state["request_id"], draft_id=row["id"])
                lease = json.loads(self.s.db.get("qzone_auto_state") or "{}")
                if (
                    self.auto_reason()
                    or policy != self.auto_policy_key()
                    or lease.get("request_id") != state["request_id"]
                    or lease.get("lease_until", 0) <= self.clock()
                ):
                    self.auto_state(lease_id=state["request_id"], reason="policy_changed")
                    return
                row = await self.publish(
                    row["id"], DraftPublish(revision=row["revision"]), autonomous=True
                )
                self.auto_state(
                    lease_id=state["request_id"], reason=row["status"], completed_at=self.clock()
                )
            except asyncio.CancelledError:
                self.auto_state(lease_id=state["request_id"], reason="interrupted")
                raise
            except ProviderError as error:
                self.auto_state(
                    lease_id=state["request_id"],
                    reason=error.kind
                    if error.kind in {"budget", "unknown_cost", "not_configured"}
                    else "generation_failed",
                )
            except Exception:
                self.auto_state(lease_id=state["request_id"], reason="deferred")
                self.s.db.audit("qzone_autonomous_deferred")
            finally:
                self.auto_state(lease_id=state["request_id"], lease_until=0)

    async def startup(self):
        if self.task and not self.task.done():
            return
        self.closed = False

        async def loop():
            while not self.closed:
                try:
                    await self.tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self.s.db.audit("qzone_autonomous_deferred")
                await asyncio.sleep(30)

        self.task = asyncio.create_task(loop())

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
            self.task = None
