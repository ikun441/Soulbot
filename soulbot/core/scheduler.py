"""Persistent approval queue for bounded private messages; no LLM-generated approvals."""

import asyncio
import hashlib
import json
import random
import time
from datetime import datetime
from typing import Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

from pydantic import UUID4, Field, field_validator, model_validator

from soulbot.core.budget import BudgetService
from soulbot.core.consent import in_quiet_window
from soulbot.core.events import ChatEvent
from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import Model

STATUSES = {
    "pending_approval": "待审批",
    "scheduled": "已安排",
    "waiting": "条件等待",
    "running": "执行中",
    "sent": "已确认发送",
    "uncertain": "发送未确认",
    "cancelled": "已取消",
    "rejected": "已拒绝",
    "expired": "已过期",
}
REASONS = {
    "created": "等待管理员审批",
    "approved": "已审批，等待时间窗口",
    "rejected": "管理员拒绝",
    "cancelled": "管理员取消",
    "consent_revoked": "用户撤销授权",
    "window_expired": "发送窗口已结束",
    "global_disabled": "主动私聊总开关已关闭",
    "qq_disabled": "QQ 私聊接入已关闭",
    "no_consent": "用户未授权或已撤销",
    "user_paused": "用户免打扰已开启",
    "admin_blocked": "管理员已停用此用户的主动联系",
    "quiet_hours": "当前处于免打扰时段",
    "daily_user_limit": "该用户今日主动联系额度已用尽",
    "daily_total_limit": "今日全局主动额度已用尽",
    "cooldown": "尚未达到主动联系最短间隔",
    "budget": "Token 或费用预算已用尽",
    "unknown_cost": "存在未知费用，暂停可选主动行为",
    "offline": "对应 QQ 机器人尚未连接",
    "dispatching": "已预留额度，正在发送",
    "confirmed": "平台确认发送",
    "unconfirmed": "未得到发送确认，禁止自动重试",
    "interrupted": "执行中断，禁止自动重试",
}


class JobCreate(Model):
    request_id: UUID4
    bot_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    user_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    content: str = Field(min_length=1, max_length=1000)
    earliest_at: float = Field(ge=0, allow_inf_nan=False)
    latest_at: float = Field(ge=0, allow_inf_nan=False)

    @field_validator("content")
    @classmethod
    def text(cls, value):
        if not value.strip() or any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
            raise ValueError("草稿不能为空或包含控制字符")
        return value.strip()

    @model_validator(mode="after")
    def window(self):
        if not 10 <= self.latest_at - self.earliest_at <= 7 * 86400:
            raise ValueError("发送窗口长度须在 10 秒到 7 天之间")
        if self.bot_id == self.user_id:
            raise ValueError("不能主动联系机器人自己")
        return self


class JobReview(Model):
    revision: int = Field(ge=0, strict=True)
    action: Literal["approve", "reject", "cancel"]


class ProactiveScheduler:
    def __init__(self, services, clock=time.time, rng=random.uniform):
        self.s = services
        self.db, self.config = services.db, services.config
        self.clock, self.rng = clock, rng
        self.lock = asyncio.Lock()
        self.task = None
        self.closed = False

    def transition(self, db, key, status, reason):
        old = db.execute("SELECT status,reason FROM proactive_jobs WHERE id=?", (key,)).fetchone()
        if old and (old["status"] != status or old["reason"] != reason):
            now = self.clock()
            db.execute(
                "UPDATE proactive_jobs SET status=?,reason=?,updated_at=?,revision=revision+1 WHERE id=?",
                (status, reason, now, key),
            )
            db.execute(
                "INSERT INTO proactive_job_events(job_id,status,reason,created_at) VALUES (?,?,?,?)",
                (key, status, reason, now),
            )

    def expire(self, db):
        now = self.clock()
        for row in db.execute(
            "SELECT id FROM proactive_jobs WHERE status IN ('pending_approval','scheduled','waiting') AND latest_at<=?",
            (now,),
        ).fetchall():
            self.transition(db, row["id"], "expired", "window_expired")
        for row in db.execute(
            "SELECT id FROM proactive_jobs WHERE status='running' AND lease_until<=?", (now,)
        ).fetchall():
            self.transition(db, row["id"], "uncertain", "interrupted")
            db.execute("UPDATE proactive_usage SET status='uncertain' WHERE job_id=?", (row["id"],))

    def public(self, row):
        item = dict(row)
        item["status_label"] = STATUSES[item["status"]]
        item["reason_label"] = REASONS[item["reason"]]
        item.pop("fingerprint", None)
        return item

    def listing(self, status="all", offset=0, limit=30):
        clause = "" if status == "all" else " WHERE status=?"
        args = [] if status == "all" else [status]
        with self.db.connect() as db:
            self.expire(db)
            total = db.execute("SELECT COUNT(*) FROM proactive_jobs" + clause, args).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM proactive_jobs"
                + clause
                + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                [*args, limit, offset],
            ).fetchall()
            items = []
            for row in rows:
                item = self.public(row)
                item["history"] = [
                    {
                        **dict(h),
                        "status_label": STATUSES[h["status"]],
                        "reason_label": REASONS[h["reason"]],
                    }
                    for h in db.execute(
                        "SELECT status,reason,created_at FROM proactive_job_events WHERE job_id=? ORDER BY id DESC LIMIT 20",
                        (row["id"],),
                    )
                ]
                items.append(item)
        return {
            "items": items,
            "total": total,
            "enabled": self.config.current.proactive.enabled,
            "statuses": STATUSES,
        }

    def create(self, data: JobCreate):
        now = self.clock()
        payload = data.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            old = db.execute(
                "SELECT * FROM proactive_jobs WHERE request_id=?", (str(data.request_id),)
            ).fetchone()
            if old:
                if old["fingerprint"] != digest:
                    raise StateConflict("此请求已用于其它草稿，请刷新后重建")
                return {"item": self.public(old), "duplicate": True}
            if data.latest_at <= now or data.earliest_at > now + 7 * 86400:
                raise ValueError("发送窗口已过期或开始时间超过 7 天")
            contact = db.execute(
                "SELECT opted_in FROM proactive_contacts WHERE bot_id=? AND user_id=?",
                (data.bot_id, data.user_id),
            ).fetchone()
            if not contact or not contact[0]:
                raise ValueError("用户必须先通过 QQ 私聊明确授权")
            if (
                db.execute(
                    "SELECT COUNT(*) FROM proactive_jobs WHERE status IN ('pending_approval','scheduled','waiting','running')"
                ).fetchone()[0]
                >= 200
            ):
                raise ValueError("队列已有 200 个未结束任务，请先处理")
            key = uuid4().hex
            run_at = data.earliest_at + self.rng(
                0,
                min(
                    self.config.current.proactive.jitter_max_seconds,
                    data.latest_at - data.earliest_at - 1,
                ),
            )
            db.execute(
                """INSERT INTO proactive_jobs(id,request_id,fingerprint,bot_id,user_id,content,
                earliest_at,latest_at,run_at,status,reason,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'pending_approval','created',?,?)""",
                (
                    key,
                    str(data.request_id),
                    digest,
                    data.bot_id,
                    data.user_id,
                    data.content,
                    data.earliest_at,
                    data.latest_at,
                    run_at,
                    now,
                    now,
                ),
            )
            db.execute(
                "INSERT INTO proactive_job_events(job_id,status,reason,created_at) VALUES (?,'pending_approval','created',?)",
                (key, now),
            )
            item = self.public(
                db.execute("SELECT * FROM proactive_jobs WHERE id=?", (key,)).fetchone()
            )
        self.db.audit("proactive_job_created")
        return {"item": item, "duplicate": False}

    def review(self, key, data: JobReview):
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            row = db.execute("SELECT * FROM proactive_jobs WHERE id=?", (key,)).fetchone()
            if not row:
                raise LookupError("任务不存在")
            if row["revision"] != data.revision:
                raise StateConflict("任务状态已改变，请刷新后核对")
            if data.action in {"approve", "reject"} and row["status"] != "pending_approval":
                raise StateConflict("只有待审批草稿可以通过或拒绝")
            if data.action == "cancel" and row["status"] not in {
                "pending_approval",
                "scheduled",
                "waiting",
            }:
                raise StateConflict("任务已执行或结束，无法取消；发送未确认的任务不会自动重试")
            if data.action == "approve":
                contact = db.execute(
                    "SELECT opted_in,admin_blocked FROM proactive_contacts WHERE bot_id=? AND user_id=?",
                    (row["bot_id"], row["user_id"]),
                ).fetchone()
                if not contact or not contact["opted_in"] or contact["admin_blocked"]:
                    raise StateConflict("用户已撤销或管理员停用，无法审批发送")
                db.execute(
                    "UPDATE proactive_jobs SET reviewed_at=?,approved_by=1 WHERE id=?",
                    (self.clock(), key),
                )
                self.transition(db, key, "scheduled", "approved")
            else:
                state = "rejected" if data.action == "reject" else "cancelled"
                self.transition(db, key, state, state)
            result = self.public(
                db.execute("SELECT * FROM proactive_jobs WHERE id=?", (key,)).fetchone()
            )
        self.db.audit("proactive_job_reviewed", data.action)
        return result

    def reason(self, db, row, *, exclude=None):
        now = self.clock()
        settings = self.config.current
        policy = settings.proactive
        if not policy.enabled:
            return "global_disabled"
        if not settings.qq.enabled or not settings.behavior.private_chat:
            return "qq_disabled"
        if row["user_id"] in settings.behavior.ignored_bot_ids:
            return "admin_blocked"
        contact = db.execute(
            "SELECT * FROM proactive_contacts WHERE bot_id=? AND user_id=?",
            (row["bot_id"], row["user_id"]),
        ).fetchone()
        if not contact or not contact["opted_in"]:
            return "no_consent"
        if contact["admin_blocked"]:
            return "admin_blocked"
        if contact["user_paused"]:
            return "user_paused"
        if in_quiet_window(
            now, settings.soulbot.timezone, contact["quiet_start"], contact["quiet_end"]
        ) or (
            settings.behavior.quiet_hours
            and datetime.fromtimestamp(now, ZoneInfo(settings.soulbot.timezone)).hour < 8
        ):
            return "quiet_hours"
        day = datetime.fromtimestamp(now, ZoneInfo(settings.soulbot.timezone)).date().isoformat()
        excluded = exclude or ""
        count = db.execute(
            "SELECT COUNT(*) FROM proactive_usage WHERE day=? AND bot_id=? AND user_id=? AND job_id<>?",
            (day, row["bot_id"], row["user_id"], excluded),
        ).fetchone()[0]
        if count >= policy.max_per_user_day:
            return "daily_user_limit"
        if (
            db.execute(
                "SELECT COUNT(*) FROM proactive_usage WHERE day=? AND job_id<>?", (day, excluded)
            ).fetchone()[0]
            >= policy.max_daily_total
        ):
            return "daily_total_limit"
        last = db.execute(
            "SELECT MAX(created_at) FROM proactive_usage WHERE bot_id=? AND user_id=? AND job_id<>?",
            (row["bot_id"], row["user_id"], excluded),
        ).fetchone()[0]
        if last is not None and now - last < policy.min_interval_minutes * 60:
            return "cooldown"
        cost, unknown = BudgetService.cost_totals(db, day)
        tokens = db.execute(
            "SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM llm_usage WHERE day=?", (day,)
        ).fetchone()[0]
        if cost >= settings.behavior.daily_cost_cny or tokens >= settings.behavior.daily_tokens:
            return "budget"
        if unknown:
            return "unknown_cost"
        return None

    def claim(self, key, connected):
        now = self.clock()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self.expire(db)
            row = db.execute("SELECT * FROM proactive_jobs WHERE id=?", (key,)).fetchone()
            if (
                not row
                or row["status"] not in {"scheduled", "waiting"}
                or row["run_at"] > now
                or row["next_check_at"] > now
            ):
                return None
            reason = self.reason(db, row)
            if reason is None and row["bot_id"] not in connected:
                reason = "offline"
            if reason:
                self.transition(db, key, "waiting", reason)
                db.execute(
                    "UPDATE proactive_jobs SET next_check_at=? WHERE id=?",
                    (now + self.config.current.proactive.evaluation_interval_seconds, key),
                )
                return None
            if db.execute("SELECT 1 FROM proactive_usage WHERE job_id=?", (key,)).fetchone():
                self.transition(db, key, "uncertain", "interrupted")
                return None
            day = (
                datetime.fromtimestamp(now, ZoneInfo(self.config.current.soulbot.timezone))
                .date()
                .isoformat()
            )
            db.execute(
                "INSERT INTO proactive_usage VALUES (?,?,?,?,?,'reserved')",
                (key, row["bot_id"], row["user_id"], day, now),
            )
            db.execute("UPDATE proactive_jobs SET lease_until=? WHERE id=?", (now + 90, key))
            self.transition(db, key, "running", "dispatching")
            return dict(row)

    def allowed_event(self, event):
        if event.group_id or not event.message_id.startswith("proactive:"):
            return False
        key = event.message_id.removeprefix("proactive:")
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM proactive_jobs WHERE id=?", (key,)).fetchone()
            if not row or row["status"] != "running" or row["approved_by"] != 1:
                return False
            if (event.bot_id, event.user_id) != (row["bot_id"], row["user_id"]):
                return False
            if (
                not row["run_at"] <= self.clock() < row["latest_at"]
                or row["lease_until"] <= self.clock()
            ):
                return False
            usage = db.execute("SELECT day FROM proactive_usage WHERE job_id=?", (key,)).fetchone()
            day = (
                datetime.fromtimestamp(self.clock(), ZoneInfo(self.config.current.soulbot.timezone))
                .date()
                .isoformat()
            )
            if not usage or usage["day"] != day:
                return False
            return self.reason(db, row, exclude=key) is None

    def finish(self, key, sent, reason=None):
        confirmed = None
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM proactive_jobs WHERE id=?", (key,)).fetchone()
            if row and row["status"] == "running":
                status = "sent" if sent else "uncertain"
                self.transition(db, key, status, reason or ("confirmed" if sent else "unconfirmed"))
                db.execute("UPDATE proactive_usage SET status=? WHERE job_id=?", (status, key))
                if sent:
                    confirmed = ChatEvent(row["bot_id"], row["user_id"], key, "")
        self.db.audit("proactive_job_finished", "sent" if sent else "uncertain")
        diary = getattr(self.s, "diary", None)
        if confirmed and diary:
            try:
                diary.record(confirmed, kind="proactive", source_key=key)
            except Exception:
                self.db.audit("diary_capture_failed")

    async def tick(self):
        async with self.lock:

            def due():
                with self.db.connect() as db:
                    self.expire(db)
                    return [
                        row[0]
                        for row in db.execute(
                            "SELECT id FROM proactive_jobs WHERE status IN ('scheduled','waiting') AND run_at<=? AND next_check_at<=? ORDER BY next_check_at,run_at,id LIMIT 20",
                            (self.clock(), self.clock()),
                        )
                    ]

            for key in await asyncio.to_thread(due):
                if self.closed:
                    return
                connected = self.s.qq_transport.connected() if self.s.qq_transport else []
                row = await asyncio.to_thread(self.claim, key, connected)
                if not row:
                    continue
                event = ChatEvent(
                    row["bot_id"], row["user_id"], "proactive:" + key, "", directed=True
                )
                try:
                    async with asyncio.timeout(30):
                        sent = await self.s.agent.executor.send(
                            event, row["content"], "scheduled_private"
                        )
                    await asyncio.to_thread(self.finish, key, sent)
                except asyncio.CancelledError:
                    await asyncio.shield(asyncio.to_thread(self.finish, key, False, "interrupted"))
                    raise
                except Exception:
                    await asyncio.to_thread(self.finish, key, False)

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
                    await asyncio.to_thread(self.db.audit, "proactive_scheduler_failed")
                await asyncio.sleep(self.config.current.proactive.evaluation_interval_seconds)

        self.task = asyncio.create_task(loop(), name="soulbot-proactive-scheduler")

    async def close(self):
        self.closed = True
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
