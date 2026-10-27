"""NapCat QZone native action contract, verified against the pinned upstream release."""

import asyncio

# Verify a new upstream release's action, registration and receipt before extending.
# https://github.com/NapNeko/NapCatQQ/blob/v4.18.38/packages/napcat-onebot/action/extends/SendQzoneMsg.ts
VERIFIED_NAPCAT_VERSIONS = frozenset({"4.18.38"})


async def probe_qzone(bot, bot_id):
    """Only read-only calls. Version contract support is not proof a post will succeed."""
    async with asyncio.timeout(15):
        version = await bot.call_api("get_version_info")
        status = await bot.call_api("get_status")
        login = await bot.call_api("get_login_info")
    if not isinstance(version, dict) or not isinstance(status, dict) or not isinstance(login, dict):
        return {"supported": False, "state": "unverified", "message": "NapCat 只读响应格式无法确认"}
    if (
        str(login.get("user_id")) != bot_id
        or status.get("online") is not True
        or status.get("good") is not True
    ):
        return {"supported": False, "state": "offline", "message": "QQ 未在线或账号身份不匹配"}
    supported = (
        version.get("app_name") == "NapCat.Onebot"
        and version.get("app_version") in VERIFIED_NAPCAT_VERSIONS
    )
    return {
        "supported": supported,
        "state": "verified_contract" if supported else "unverified_version",
        "message": "账号在线，接口版本已核实；最终发布仍以平台 tid 回执为准"
        if supported
        else "当前适配器或版本未核实，保留草稿；不会用发布动作探测能力",
        "version": version["app_version"] if supported else None,
        "connection": bot if supported else None,
    }
