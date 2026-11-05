"""Bounded administrator-triggered read-only QQ directory synchronization."""

import asyncio
import json
import time
from threading import Event
from uuid import uuid4

from pydantic import UUID4, Field, field_validator

from soulbot.core.social_state import StateConflict
from soulbot.services.config_service import Model


class SyncRequest(Model):
    request_id: UUID4 = Field(default_factory=uuid4)
    bot_id: str = Field(pattern=r"^[1-9][0-9]{4,11}$")
    include_friends: bool = True
    include_members: bool = True
    group_ids: list[str] = Field(default_factory=list, max_length=200)
    max_groups: int = Field(default=50, ge=1, le=200)

    @field_validator("group_ids")
    @classmethod
    def groups(cls, values):
        from soulbot.core.identities import IdentityDirectory

        if any(not IdentityDirectory.valid_qq(value) for value in values):
            raise ValueError("群号格式无效")
        return list(dict.fromkeys(values))


class IdentitySync:
    def __init__(self, services):
        self.s = services
        self.task = None
        self.lock = asyncio.Lock()
        self.stop = Event()

    def saved(self):
        return json.loads(self.s.db.get("identity_sync_state") or '{"status":"idle"}')

    def status(self):
        return {
            **self.saved(),
            "connected_bots": self.s.qq_transport.connected() if self.s.qq_transport else [],
        }

    def save(self, state):
        state["updated_at"] = time.time()
        self.s.db.set("identity_sync_state", json.dumps(state))

    def group_allowed(self, group):
        cfg = self.s.config.current
        policy = cfg.behavior.group_policies.get(group)
        return (
            group not in cfg.behavior.group_blocklist
            and (not cfg.behavior.group_allowlist or group in cfg.behavior.group_allowlist)
            and (not policy or policy.enabled)
        )

    def active(self, request):
        return (
            self.s.config.current.qq.enabled
            and self.s.qq_transport
            and request.bot_id in self.s.qq_transport.connected()
        )

    async def begin(self, request):
        async with self.lock:
            previous = self.saved()
            payload = request.model_dump(mode="json")
            if previous.get("request", {}).get("request_id") == str(request.request_id):
                if previous["request"] != payload:
                    raise StateConflict("请求编号已用于其他同步设置，请重新开始")
                return {**self.status(), "duplicate": True}
            if self.task and not self.task.done():
                raise StateConflict("已有账号同步正在运行，请等待或取消")
            if not self.active(request):
                raise ValueError("请先启用并连接所选 QQ 机器人")
            state = {
                "status": "running",
                "request": payload,
                "started_at": time.time(),
                "friends": 0,
                "groups": 0,
                "members": 0,
                "groups_done": 0,
                "failed_groups": 0,
                "skipped_groups": 0,
                "stage": "friends" if request.include_friends else "groups",
            }
            await asyncio.to_thread(self.save, state)
            self.stop = Event()
            self.task = asyncio.create_task(
                self.run(request, state, self.stop), name="soulbot-identity-sync"
            )
            self.s.db.audit("identity_sync_started")
            return {**self.status(), "duplicate": False}

    @staticmethod
    def check_list(value):
        if (
            not isinstance(value, list)
            or len(value) > 100000
            or any(not isinstance(row, dict) for row in value)
        ):
            raise ValueError("QQ名录响应格式不兼容或超过数量限制")
        return value

    def friends(self, bot, rows, stop=None):
        count, now = 0, time.time()
        directory = self.s.identities
        if any(not directory.valid_qq(row.get("user_id", "")) for row in rows):
            raise ValueError("好友响应包含无效账号，保留原映射")
        with self.s.db.connect() as db:
            for old in db.execute(
                "SELECT user_id,remark,updated_at FROM identity_accounts WHERE bot_id=? AND is_friend=1",
                (bot,),
            ).fetchall():
                directory.remember_name(
                    db, bot, old["user_id"], "", "remark", old["remark"], old["updated_at"]
                )
            db.execute(
                "UPDATE identity_accounts SET is_friend=0,remark='',friends_synced_at=? WHERE bot_id=?",
                (now, bot),
            )
            for row in rows:
                if stop is not None and stop.is_set():
                    raise InterruptedError("同步已取消")
                user = str(row.get("user_id", ""))
                if directory.upsert_user(
                    db, bot, user, row.get("nickname", ""), now, remark=row.get("remark", "")
                ):
                    count += 1
                    db.execute(
                        "UPDATE identity_accounts SET is_friend=1,friends_synced_at=? WHERE bot_id=? AND user_id=?",
                        (now, bot, user),
                    )
            if stop is not None and stop.is_set():
                raise InterruptedError("同步已取消")
        return count

    def groups(self, bot, rows, stop=None):
        now = time.time()
        with self.s.db.connect() as db:
            for row in rows:
                if stop is not None and stop.is_set():
                    raise InterruptedError("同步已取消")
                group = str(row.get("group_id", ""))
                if self.group_allowed(group):
                    self.s.identities.upsert_group(db, bot, group, row.get("group_name", ""), now)
            if stop is not None and stop.is_set():
                raise InterruptedError("同步已取消")

    def members(self, bot, group, rows, expected, stop=None):
        directory = self.s.identities
        valid = [r for r in rows if directory.valid_qq(r.get("user_id", ""))]
        complete = len(valid) == len(rows) and (type(expected) is not int or len(valid) >= expected)
        now = time.time()
        with self.s.db.connect() as db:
            # Keep older membership observations when the platform returned an incomplete list.
            if complete:
                # Retain previous cards as history before replacing the current membership set.
                for old in db.execute(
                    "SELECT user_id,card FROM identity_members WHERE bot_id=? AND group_id=?",
                    (bot, group),
                ).fetchall():
                    directory.remember_name(
                        db, bot, old["user_id"], group, "card", old["card"], now
                    )
                db.execute(
                    "DELETE FROM identity_members WHERE bot_id=? AND group_id=?", (bot, group)
                )
            for row in valid:
                if stop is not None and stop.is_set():
                    raise InterruptedError("同步已取消")
                user = str(row["user_id"])
                directory.upsert_user(db, bot, user, row.get("nickname", ""), now)
                directory.upsert_member(db, bot, group, user, row.get("card", ""), now)
            if stop is not None and stop.is_set():
                raise InterruptedError("同步已取消")
        return len(valid), complete

    async def store(self, operation, *args, stop):
        task = asyncio.create_task(asyncio.to_thread(operation, *args, stop))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            stop.set()
            await asyncio.gather(task, return_exceptions=True)
            raise

    async def run(self, request, state, stop):
        try:
            async with asyncio.timeout(600):
                transport = self.s.qq_transport
                if request.include_friends:
                    rows = self.check_list(await transport.directory_friends(request.bot_id))
                    if not self.active(request):
                        raise PermissionError
                    state["friends"] = await self.store(
                        self.friends, request.bot_id, rows, stop=stop
                    )
                    await asyncio.to_thread(self.save, state)
                state["stage"] = "groups"
                rows = self.check_list(await transport.directory_groups(request.bot_id))
                if not self.active(request):
                    raise PermissionError
                if any(not self.s.identities.valid_qq(row.get("group_id", "")) for row in rows):
                    raise ValueError("群列表包含无效群号")
                rows = list({str(row["group_id"]): row for row in rows}.values())
                chosen = [
                    row
                    for row in rows
                    if self.s.identities.valid_qq(row.get("group_id", ""))
                    and self.group_allowed(str(row["group_id"]))
                    and (not request.group_ids or str(row["group_id"]) in request.group_ids)
                ]
                await self.store(self.groups, request.bot_id, chosen, stop=stop)
                state["skipped_groups"] = len(rows) - len(chosen)
                state["groups"] = len(chosen)
                missing = set(request.group_ids) - {str(row.get("group_id")) for row in rows}
                state["failed_groups"] += len(missing)
                state["limited_groups"] = (
                    max(0, len(chosen) - request.max_groups) if request.include_members else 0
                )
                state["stage"] = "members" if request.include_members else "done"
                if request.include_members:
                    for group in chosen[: request.max_groups]:
                        if not self.active(request):
                            raise PermissionError
                        gid = str(group["group_id"])
                        if not self.group_allowed(gid):
                            state["skipped_groups"] += 1
                            continue
                        try:
                            members = self.check_list(
                                await transport.directory_members(request.bot_id, gid)
                            )
                            if not self.active(request):
                                raise PermissionError
                            if not self.group_allowed(gid):
                                state["skipped_groups"] += 1
                                continue
                            count, complete = await self.store(
                                self.members,
                                request.bot_id,
                                gid,
                                members,
                                group.get("member_count"),
                                stop=stop,
                            )
                            state["members"] += count
                            state["groups_done"] += 1
                            if not complete:
                                state["failed_groups"] += 1
                        except PermissionError:
                            raise
                        except Exception:
                            state["failed_groups"] += 1
                        await asyncio.to_thread(self.save, state)
                        await asyncio.sleep(0.2)
                if not self.active(request):
                    raise PermissionError
                state["stage"] = "done"
                state["status"] = (
                    "partial"
                    if state["failed_groups"] or state.get("limited_groups")
                    else "completed"
                )
        except asyncio.CancelledError:
            state["status"] = "cancelled"
            raise
        except (PermissionError, TimeoutError):
            state["status"] = "cancelled" if not self.active(request) else "failed"
            state["reason"] = "connection_or_timeout"
        except Exception:
            state["status"] = "failed"
            state["reason"] = "platform_response"
        finally:
            await asyncio.to_thread(self.save, state)
            self.s.db.audit("identity_sync_finished", state["status"])

    async def cancel(self, *, interrupted=False):
        async with self.lock:
            self.stop.set()
            if self.task and not self.task.done():
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)
            state = self.saved()
            if state.get("status") in {"running", "cancelled"}:
                state["status"] = "interrupted" if interrupted else "cancelled"
                await asyncio.to_thread(self.save, state)
            return self.status()

    async def startup(self):
        state = self.saved()
        if state.get("status") == "running" and (not self.task or self.task.done()):
            state["status"] = "interrupted"
            await asyncio.to_thread(self.save, state)

    async def close(self):
        await self.cancel(interrupted=True)
