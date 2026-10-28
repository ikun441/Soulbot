"""Scoped weekly facts, explicit paid prose polishing, and independently reviewed deliveries."""

import asyncio
import json
import sqlite3
from datetime import date, timedelta
from uuid import uuid4

from pydantic import UUID4, Field

from soulbot.core.scheduler import JobCreate
from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import Model


class PolishDiary(Model):
    request_id: UUID4
    revision: int = Field(ge=0, strict=True)


class WeeklyDiary(Model):
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


class DiaryDelivery(WeeklyDiary):
    kind: str = Field(pattern=r"^(daily|weekly)$")


class DiaryPeriods:
    def __init__(self, services):
        self.s = services

    @staticmethod
    def week(day):
        value = date.fromisoformat(day)
        start = value - timedelta(days=value.weekday())
        return start.isoformat(), (start + timedelta(days=7)).isoformat()

    def generate_week(self, day, *, automatic=False):
        diary, cfg = self.s.diary, self.s.config.current
        start, end = self.week(day)
        if not cfg.diary.enabled or date.fromisoformat(end) > diary.today():
            raise ValueError("周摘要仅使用已经结束的自然周")
        created = 0
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT d.* FROM diaries d LEFT JOIN diary_preferences p ON p.scope=d.scope WHERE day>=? AND day<? AND is_final=1 AND COALESCE(p.enabled,1)=1 ORDER BY d.scope,day",
                (start, end),
            ).fetchall()
            scopes = {}
            for row in rows:
                scopes.setdefault(row["scope"], []).append(row)
            for scope, items in scopes.items():
                if (
                    automatic
                    and db.execute(
                        "SELECT 1 FROM diary_summary_deleted WHERE scope=? AND week_start=?",
                        (scope, start),
                    ).fetchone()
                ):
                    continue
                if not automatic:
                    db.execute(
                        "DELETE FROM diary_summary_deleted WHERE scope=? AND week_start=?",
                        (scope, start),
                    )
                if len(items) < 2:
                    continue
                # Do not mix diaries created under different timezone boundaries.
                zones = {row["timezone"] for row in items}
                if len(zones) != 1:
                    continue
                metrics = (
                    "total",
                    "chat_replies",
                    "text_chats",
                    "media_chats",
                    "proactive_messages",
                    "qzone_posts",
                )
                counts = {
                    key: sum(json.loads(row["facts_json"]).get(key, 0) for row in items)
                    for key in metrics
                }
                counts["days"] = len(items)
                text = f"本周有 {len(items)} 天留下了确认活动，共 {counts['total']} 次活动：{counts['chat_replies']} 次交流、{counts['proactive_messages']} 次主动联系、{counts['qzone_posts']} 次空间发布。这里只回顾这段独立会话的事实计数。"
                changed = db.execute(
                    "INSERT OR IGNORE INTO diary_summaries(scope,week_start,week_end,timezone,source_ids,facts_json,role_text,created_at) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        scope,
                        start,
                        end,
                        next(iter(zones)),
                        json.dumps([r["id"] for r in items]),
                        json.dumps(counts),
                        text,
                        diary.clock(),
                    ),
                ).rowcount
                created += changed
        if created:
            self.s.db.audit("diary_weekly_generated")
        return {"created": created, "week_start": start, "week_end": end}

    def summaries(self):
        with self.s.db.connect() as db:
            rows = db.execute(
                "SELECT * FROM diary_summaries ORDER BY week_start DESC,id DESC LIMIT 100"
            ).fetchall()
        return {
            "items": [
                {
                    **dict(row),
                    "facts": json.loads(row["facts_json"]),
                    "source_ids": json.loads(row["source_ids"]),
                }
                for row in rows
            ]
        }

    async def polish(self, key, data):
        s = self.s
        item = await asyncio.to_thread(s.diary.get, key)
        if not s.config.current.diary.enabled or item["revision"] != data.revision:
            raise StateConflict("日记已变化或功能已关闭，请刷新后核对")
        with s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute(
                "SELECT 1 FROM diary_polish_runs WHERE created_at>?", (s.diary.clock() - 60,)
            ).fetchone():
                raise StateConflict("每分钟只允许一次润色，请先核对最近结果")
            try:
                db.execute(
                    "INSERT INTO diary_polish_runs VALUES (?,?,?,'running',?)",
                    (str(data.request_id), key, data.revision, s.diary.clock()),
                )
            except sqlite3.IntegrityError:
                raise StateConflict(
                    "本次修订已尝试润色，请先检查日记与用量；不会重复计费"
                ) from None
        try:
            result = await s.llm.complete(
                [
                    {
                        "role": "system",
                        "content": "你是日记角色表达编辑。只把用户提供的只读活动计数改写为简短中文角色表达，最多500字；不得编造对话、地点、人物、行动或主观意识，不得声称网页、私聊内容或未提供经历。不要输出标识符。",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "name": s.config.current.soulbot.name,
                                "day": item["day"],
                                "facts": item["facts"],
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                purpose="diary_polish",
                output_limit=512,
            )
            text = result.text.strip()
            if not text or len(text) > 2000 or result.tool_calls:
                raise ValueError("模型没有返回有效角色表达")
            with s.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute("SELECT revision,scope FROM diaries WHERE id=?", (key,)).fetchone()
                pref = db.execute(
                    "SELECT enabled FROM diary_preferences WHERE scope=?", (item["scope"],)
                ).fetchone()
                if (
                    not row
                    or row[0] != data.revision
                    or not s.config.current.diary.enabled
                    or (pref and not pref[0])
                ):
                    raise StateConflict("生成期间日记或授权已改变，未覆盖现有内容")
                db.execute(
                    "UPDATE diaries SET role_text=?,role_source='model',revision=revision+1,updated_at=? WHERE id=?",
                    (text, s.diary.clock(), key),
                )
                db.execute(
                    "UPDATE diary_polish_runs SET status='completed' WHERE request_id=?",
                    (str(data.request_id),),
                )
            s.db.audit("diary_polished")
            return s.diary.get(key)
        except BaseException:
            with s.db.connect() as db:
                db.execute(
                    "UPDATE diary_polish_runs SET status='failed' WHERE request_id=?",
                    (str(data.request_id),),
                )
            raise

    def delivery(self, kind, day):
        cfg, diary = self.s.config.current, self.s.diary
        bot, owner = cfg.diary.delivery_bot_id, cfg.personality.owner_qq
        if not cfg.diary.enabled or not bot or not owner or cfg.automation.paused:
            raise ValueError("请启用日记、设置推送机器人及主人，并恢复自动化")
        if date.fromisoformat(day) >= diary.today():
            raise ValueError("摘要只推送已结束的日期")
        start, end = (
            self.week(day)
            if kind == "weekly"
            else (day, (date.fromisoformat(day) + timedelta(days=1)).isoformat())
        )
        if date.fromisoformat(end) > diary.today():
            raise ValueError("请选择已结束的周期")
        with self.s.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            scopes = (f"{bot}:p:{owner}", f"qzone:{bot}")
            # Owner delivery never aggregates other users' private or group diaries.
            if kind == "weekly":
                rows = db.execute(
                    "SELECT facts_json FROM diary_summaries WHERE scope IN (?,?) AND week_start=?",
                    (*scopes, start),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT facts_json FROM diaries WHERE scope IN (?,?) AND day=? AND is_final=1",
                    (*scopes, day),
                ).fetchall()
            rows = [r for r in rows]
            if not rows:
                raise ValueError("该周期没有主人的私聊或机器人空间活动摘要")
            total = sum(json.loads(r[0])["total"] for r in rows)
            text = f"【SoulBot 本地{'周' if kind == 'weekly' else '日'}摘要】{start}：主人与机器人的已确认活动共 {total} 次。仅汇总你的私聊和机器人的空间活动，不包含其他会话内容。"
            old = db.execute(
                "SELECT payload_json FROM diary_deliveries WHERE kind=? AND period=? AND bot_id=? AND user_id=?",
                (kind, start, bot, owner),
            ).fetchone()
            if old:
                payload = json.loads(old[0])
            else:
                payload = JobCreate(
                    request_id=uuid4(),
                    bot_id=bot,
                    user_id=owner,
                    content=text,
                    earliest_at=diary.clock(),
                    latest_at=diary.clock() + 86400,
                ).model_dump(mode="json")
                db.execute(
                    "INSERT INTO diary_deliveries VALUES (?,?,?,?,?)",
                    (kind, start, bot, owner, json.dumps(payload)),
                )
        # This creates a real review queue item. Generation and sending remain independent.
        return self.s.scheduler.create(JobCreate.model_validate(payload))

    async def tick(self):
        cfg, diary = self.s.config.current, self.s.diary
        if cfg.automation.paused or not cfg.diary.enabled:
            return
        yesterday = (diary.today() - timedelta(days=1)).isoformat()
        last_week = (diary.today() - timedelta(days=7)).isoformat()
        if cfg.diary.weekly_enabled:
            await asyncio.to_thread(self.generate_week, last_week, automatic=True)
        if cfg.diary.delivery_mode != "manual":
            try:
                await asyncio.to_thread(
                    self.delivery,
                    cfg.diary.delivery_mode,
                    last_week if cfg.diary.delivery_mode == "weekly" else yesterday,
                )
            except ValueError:
                # No eligible activity/consent means no queued message, never substitute another user.
                return
