"""Persistent reservation before every billable attempt, including tests and fallbacks."""

import time
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo

from soulbot.providers.llm.base import ProviderError, Usage


class BudgetService:
    def __init__(self, db, clock=time.time):
        self.db, self.clock = db, clock

    def day(self, settings):
        return (
            datetime.fromtimestamp(self.clock(), ZoneInfo(settings.soulbot.timezone))
            .date()
            .isoformat()
        )

    @staticmethod
    def cost_totals(db, day):
        row = db.execute(
            "SELECT COALESCE(SUM(cost_cny),0),COALESCE(SUM(CASE WHEN cost_cny IS NULL AND status!='rejected' THEN 1 ELSE 0 END),0) FROM (SELECT cost_cny,status FROM llm_usage WHERE day=? UNION ALL SELECT cost_cny,status FROM tts_usage WHERE day=? UNION ALL SELECT cost_cny,status FROM asr_usage WHERE day=?)",
            (day, day, day),
        ).fetchone()
        return row[0], row[1]

    @staticmethod
    def price(endpoint, input_tokens, output_tokens):
        if (
            endpoint.input_price_cny_per_million is None
            or endpoint.output_price_cny_per_million is None
        ):
            return None
        return (
            input_tokens * endpoint.input_price_cny_per_million
            + output_tokens * endpoint.output_price_cny_per_million
        ) / 1000000

    def reserve(self, settings, endpoint, provider, purpose, input_tokens, output_tokens):
        day = self.day(settings)
        price = self.price(endpoint, input_tokens, output_tokens)
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT COALESCE(SUM(input_tokens+output_tokens),0) AS tokens, COALESCE(SUM(cost_cny),0) AS cost, SUM(CASE WHEN cost_cny IS NULL AND status!='rejected' THEN 1 ELSE 0 END) AS unknown FROM llm_usage WHERE day=?",
                (day,),
            ).fetchone()
            if row["tokens"] + input_tokens + output_tokens > settings.behavior.daily_tokens:
                raise ProviderError("budget", "今日 Token 额度不足，已停止模型调用")
            cost, unknown = self.cost_totals(db, day)
            if cost >= settings.behavior.daily_cost_cny or (
                price is not None and cost + price > settings.behavior.daily_cost_cny
            ):
                raise ProviderError("budget", "今日费用预算不足，已停止模型调用")
            if purpose == "proactive" and (price is None or unknown):
                raise ProviderError(
                    "unknown_cost", "存在未知费用，已暂停主动模型调用；请配置价格并核对账单"
                )
            key = uuid.uuid4().hex
            db.execute(
                "INSERT INTO llm_usage(id,day,provider,model,purpose,status,input_tokens,output_tokens,cost_cny,created_at) VALUES (?,?,?,?,?,'pending',?,?,?,?)",
                (
                    key,
                    day,
                    provider,
                    endpoint.model,
                    purpose,
                    input_tokens,
                    output_tokens,
                    price,
                    self.clock(),
                ),
            )
        return key

    def settle(self, key, endpoint, usage: Usage | None):
        with self.db.connect() as db:
            if usage is None:
                db.execute("UPDATE llm_usage SET status='estimated' WHERE id=?", (key,))
            else:
                cost = self.price(endpoint, usage.input_tokens, usage.output_tokens)
                db.execute(
                    "UPDATE llm_usage SET status='completed',input_tokens=?,output_tokens=?,reasoning_tokens=?,estimated=0,cost_cny=? WHERE id=?",
                    (usage.input_tokens, usage.output_tokens, usage.reasoning_tokens, cost, key),
                )

    def failed(self, key, error):
        with self.db.connect() as db:
            if error.uncertain:
                db.execute(
                    "UPDATE llm_usage SET status='uncertain',error_kind=? WHERE id=?",
                    (error.kind, key),
                )
            else:
                db.execute(
                    "UPDATE llm_usage SET status='rejected',input_tokens=0,output_tokens=0,cost_cny=0,error_kind=? WHERE id=?",
                    (error.kind, key),
                )

    def summary(self, settings):
        with self.db.connect() as db:
            day = self.day(settings)
            rows = [
                dict(row)
                for row in db.execute(
                    "SELECT provider,COUNT(*) AS attempts,SUM(input_tokens) AS input_tokens,SUM(output_tokens) AS output_tokens,SUM(COALESCE(reasoning_tokens,0)) AS reasoning_tokens,SUM(cost_cny) AS cost_cny,SUM(CASE WHEN cost_cny IS NULL AND status!='rejected' THEN 1 ELSE 0 END) AS unknown_cost_calls,SUM(estimated) AS estimated_calls FROM llm_usage WHERE day=? GROUP BY provider",
                    (day,),
                )
            ]
            recent = [
                dict(row)
                for row in db.execute(
                    "SELECT id,provider,model,purpose,status,input_tokens,output_tokens,reasoning_tokens,estimated,cost_cny,created_at,error_kind FROM llm_usage ORDER BY created_at DESC LIMIT 50"
                )
            ]
            speech = [
                dict(row)
                for row in db.execute(
                    "SELECT id,provider,model,purpose,status,chars,cost_cny,created_at,error_kind FROM tts_usage ORDER BY created_at DESC LIMIT 50"
                )
            ]
            today = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(chars),0) FROM tts_usage WHERE day=?", (day,)
            ).fetchone()
            cost, unknown = self.cost_totals(db, day)
            recognition = [
                dict(row)
                for row in db.execute(
                    "SELECT id,provider,model,purpose,status,seconds,cost_cny,created_at,error_kind FROM asr_usage ORDER BY created_at DESC LIMIT 50"
                )
            ]
            asr_today = db.execute(
                "SELECT COUNT(*),COALESCE(SUM(seconds),0) FROM asr_usage WHERE day=?", (day,)
            ).fetchone()
        return {
            "day": day,
            "providers": rows,
            "recent": recent,
            "token_limit": settings.behavior.daily_tokens,
            "cost_limit_cny": settings.behavior.daily_cost_cny,
            "reserved_or_used_tokens": sum(r["input_tokens"] + r["output_tokens"] for r in rows),
            "known_or_estimated_cost_cny": cost,
            "unknown_cost_calls": unknown,
            "tts": {"recent": speech, "calls": today[0], "chars": today[1]},
            "asr": {"recent": recognition, "calls": asr_today[0], "seconds": asr_today[1]},
        }
