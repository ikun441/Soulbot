"""Explicit template/keyword commands, independent of LLM support and mention heuristics."""

import asyncio
import json
import random
import shlex

from soulbot.services.media_service import ImagePayload, MediaError, inspect_image
from soulbot.services.qq_avatar import avatar
from soulbot.tools.registry import ToolContext

HELP = (
    "表情命令：随机表情 @群友；/meme list 关键词；/meme info 模板ID或关键词；/meme preview 模板ID；"
    '/meme make 模板ID或关键词 @群友 "文字1" "文字2"。也支持 /表情包、表情列表、表情详情、表情预览，以及直接发送“模板关键词 @群友 文字”。'
    "无@且模板需图片时使用你的头像；高级参数可在命令末尾加 --args '{\"参数\":值}'。受群权限、冷却、模板禁用及生成速率限制。"
)


async def _generate(agent, event, key, texts, extra, info=None):
    s = agent.services
    if s.tools.permissions.policy("generate_meme") == "deny":
        raise MediaError("表情生成工具已被管理员禁止")
    p = (info or await s.meme.client.info(key))["params_type"]
    targets = tuple(dict.fromkeys(event.mentioned_users))
    if not targets and p["min_images"]:
        targets = (event.user_id,)
        if p["min_images"] == 2:
            targets = tuple(dict.fromkeys((event.user_id, event.bot_id)))
    if not p["min_images"] <= len(targets) <= min(6, p["max_images"]):
        raise MediaError(
            f"此模板需要 {p['min_images']}–{p['max_images']} 张头像，请调整本次 @ 人数"
        )
    texts = texts or p["default_texts"]
    if not p["min_texts"] <= len(texts) <= p["max_texts"]:
        raise MediaError(
            f'此模板需要 {p["min_texts"]}–{p["max_texts"]} 段文字。用引号包住每段文字，例如 /meme make 模板ID @群友 "第一段" "第二段"'
        )
    references = []
    for target in targets:
        content = await avatar(target)
        item = await asyncio.to_thread(s.media.save, content, "qq_avatar", event.scope)
        references.append("media:" + item["id"])
    result = await s.tools.execute(
        "generate_meme",
        {"template": key, "texts": texts, "images": references, "args": extra},
        ToolContext(scope=event.scope, event=event),
    )
    reference = result.get("attachment", "")
    if reference.startswith("media:"):
        payload = await asyncio.to_thread(s.media.read, reference.split(":", 1)[1], event.scope)
        sent = await agent.executor.send_image(event, payload, "direct")
        return None if sent else "图片已生成，但没有获得 QQ 发送回执；未声称已发送。"
    return result.get("message", "已提交工具审批；没有生成或发送图片，请在 WebUI 查看。")


async def handle_meme_command(agent, event):
    text = event.text.strip()
    s = agent.services
    if not s:
        return False
    random_request = text in {"随机表情", "/随机表情"}
    prefix = text.split(maxsplit=1)[0].lower() if text else ""
    shortcuts = {"表情列表": "list", "表情详情": "info", "表情预览": "preview"}
    explicit = prefix in {"/meme", "/表情包", *shortcuts}
    keyword = None
    if (
        not explicit
        and not random_request
        and s.config.current.meme.enabled
        and (event.mentions_other or event.directed)
    ):
        index = await asyncio.to_thread(s.meme.client.keywords)
        aliases = {value for values in index.values() for value in values}
        aliases.update(index)
        keyword = next(
            (
                value
                for value in sorted(aliases, key=len, reverse=True)
                if text == value or text.startswith(value + " ")
            ),
            None,
        )
    if not explicit and not random_request and not keyword:
        return False
    if event.is_bot or event.user_id == event.bot_id:
        return True
    cfg = agent.config.current
    if not await asyncio.to_thread(agent.decision.access, cfg, event):
        return True
    if event.group_id:
        state = await asyncio.to_thread(agent.decision.state, event)
        if (
            agent.decision.mode(cfg, event) == "silent"
            or state["muted_until"] > agent.decision.clock()
        ):
            return True
    if not await asyncio.to_thread(agent.decision.claim, cfg, event, "direct"):
        return True
    await s.action_memory.collect(event, text, "meme_command")
    reply = HELP
    try:
        tokens = shlex.split(text)
        if keyword:
            tokens = ["/meme", "make", keyword, *shlex.split(text[len(keyword) :])]
        elif prefix in shortcuts:
            tokens = ["/meme", shortcuts[prefix], *tokens[1:]]
        action = tokens[1] if len(tokens) > 1 else "help"
        if not random_request and action in {"help", "帮助"}:
            await agent.executor.send(event, HELP, "direct")
            return True
        if not cfg.meme.enabled:
            raise MediaError("meme-generator 尚未启用。请在 WebUI“表情包工坊”启动服务并测试连接。")
        if random_request:
            if s.tools.permissions.policy("generate_meme") == "deny":
                raise MediaError("表情生成工具已被管理员禁止，请检查工具权限。")
            count = len(set(event.mentioned_users)) or 1
            keys = await s.meme.client.keys()
            chosen = None
            for key in random.sample(keys, min(16, len(keys))):
                info = await s.meme.client.info(key)
                p = info["params_type"]
                if (
                    p["min_images"] <= count <= min(6, p["max_images"])
                    and p["min_texts"] <= len(p["default_texts"]) <= p["max_texts"]
                ):
                    chosen = key, info
                    break
            if chosen is None:
                raise MediaError(
                    "随机候选中没有匹配头像数量与默认文字的模板，请减少 @ 人数或使用指定模板命令。"
                )
            reply = await _generate(agent, event, chosen[0], [], {}, chosen[1])
        elif action in {"list", "列表"}:
            query = " ".join(tokens[2:])
            items = await s.meme.client.search_templates(query)
            reply = (
                f"匹配 {len(items)} 个已启用模板，最多显示 20 个：\n"
                + "\n".join(
                    item["key"]
                    + ("（" + " / ".join(item["keywords"][:3]) + "）" if item["keywords"] else "")
                    for item in items[:20]
                )
                + "\n用 /meme info 模板ID 查看要求。"
            )
        elif action in {"info", "说明", "preview", "预览", "make", "generate", "制作"}:
            if len(tokens) < 3:
                raise MediaError("请补充模板 ID 或关键词，例如先用 /meme list 查找。")
            key = await s.meme.client.resolve_template(tokens[2])
            info = await s.meme.client.info(key)
            p = info["params_type"]
            if action in {"info", "说明"}:
                reply = f'{key}（{" / ".join(info["keywords"])}）\n需要 {p["min_images"]}–{p["max_images"]} 张图片、{p["min_texts"]}–{p["max_texts"]} 段文字；可用 /meme make {key} @群友 "文字"，高级参数支持 --args JSON。'
            elif action in {"preview", "预览"}:
                s.tools.permissions.require("generate_meme")
                content = await s.meme.client.preview(key)
                await agent.executor.send_image(
                    event, ImagePayload(content, inspect_image(content)[1]), "direct"
                )
                return True
            else:
                texts, extra = tokens[3:], {}
                if "--args" in texts:
                    index = texts.index("--args")
                    if index != len(texts) - 2:
                        raise MediaError("--args 后须为引号包住的一个 JSON 对象，放在命令末尾。")
                    extra = json.loads(texts[index + 1])
                    if not isinstance(extra, dict):
                        raise MediaError("高级参数必须是 JSON 对象")
                    texts = texts[:index]
                reply = await _generate(agent, event, key, texts, extra, info)
        else:
            reply = HELP
    except MediaError as error:
        reply = str(error)
    except PermissionError:
        reply = "表情请求被工具权限或当前会话规则拒绝，请管理员检查 generate_meme 权限；预览需要允许自动执行。"
    except (ValueError, KeyError, TypeError, TimeoutError):
        reply = (
            "命令参数或表情服务信息不兼容。请检查引号和 JSON 格式，或在 WebUI“表情包工坊”测试连接。"
        )
    if reply:
        await agent.executor.send(event, reply, "direct")
    return True
