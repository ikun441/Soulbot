"""Real optional Chromium setup and PNG acceptance, without changing bot settings."""

import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from soulbot.app import build_services, create_web_app
from soulbot.services.config_service import Settings
from soulbot.services.media_service import MediaError, MediaStore
from soulbot.services.meme_service import InstanceLock
from soulbot.services.render_service import HTMLRenderService
from soulbot.storage.database import Database
from soulbot.tools.html_render import CardRequest
from soulbot.tools.registry import ToolContext

ROOT = Path(__file__).resolve().parents[1]


async def main(install):
    output = ROOT / "output" / "render-live"
    db = Database(output / "data" / "acceptance.db")
    settings = Settings()
    settings.html_render.enabled = True
    renderer = HTMLRenderService(ROOT, SimpleNamespace(current=settings), db.audit)
    media = MediaStore(output, db)
    try:
        if install:
            await renderer.begin_install()
            previous = ""
            while renderer.task and not renderer.task.done():
                message = renderer.status()["message"]
                if previous != message:
                    print(message, flush=True)
                    previous = message
                await asyncio.sleep(0.5)
            await renderer.task
        if not renderer.ready():
            raise RuntimeError(renderer.status()["message"])
        examples = [
            CardRequest(
                title="今天的小小收获",
                body="把值得记住的瞬间，轻轻放在这里。\n一步一步，也是一种前进。",
                footer="SoulBot · 真实 Chromium 渲染",
            ),
            CardRequest(
                template="quote",
                theme="lilac",
                title="留一句话给自己",
                body="慢慢来，生活里的细节也值得认真。",
                footer="文字引用卡",
            ),
            CardRequest(
                template="list",
                theme="mint",
                title="接下来的小计划",
                items=["完成 HTML 卡片能力", "让每个按钮都连接真实后端", "继续推进语音与多模态"],
                footer="SoulBot · 开发中的日常",
            ),
            CardRequest(
                title="文本边界检查",
                body='<script>throw Error("should never run")</script>\n<img src="file:///etc/passwd">\n<div style="display:none">这些标签应按文字显示。</div>',
            ),
        ]
        results = []
        for example in examples:
            result = media.save(await renderer.render(example), "html_render_acceptance")
            assert result["mime"] == "image/png" and result["width"] == 800
            results.append({"template": example.template, "title": example.title, **result})
        settings.html_render.max_height = 400
        try:
            await renderer.render(CardRequest(title="高度限制", body="很多行文字\n" * 80))
        except MediaError as error:
            assert "高度" in str(error)
            results.append({"height_limit_rejected": True})
        else:
            raise AssertionError("Oversized card was not rejected")
        settings.html_render.enabled = False
        try:
            await renderer.render(examples[0])
        except MediaError:
            results.append({"disabled_rejected": True})
        else:
            raise AssertionError("Disabled rendering was not rejected")
        (output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await renderer.close()
        assert not renderer.processes and not renderer.jobs


def verify_api():
    output = ROOT / "output" / "render-live"
    root = output / "api"
    services = build_services(root)
    services.renderer.root = ROOT / "data" / "html_render"
    services.renderer.lock = InstanceLock(services.renderer.root / "install.lock")
    settings = services.config.current.model_copy(deep=True)
    settings.html_render.enabled = True
    services.config.save(settings)
    app = create_web_app(root, services)
    with TestClient(app, headers={"X-Soulbot-Request": "1"}) as client:
        account = {"username": "render_acceptance", "password": "isolated-render-password-123"}
        if not client.get("/api/bootstrap").json()["has_admin"]:
            assert client.post("/api/auth/setup", json=account).status_code == 200
        else:
            assert client.post("/api/auth/login", json=account).status_code == 200
        assert client.get("/api/render/status").json()["ready"]
        data = {
            "template": "note",
            "title": "真实 API 卡片",
            "body": "通过认证接口生成 PNG 并读取私有产物。",
        }
        result = client.post("/api/render/generate", json=data)
        assert result.status_code == 200, result.text
        artifact = result.json()
        assert client.get(artifact["url"]).content.startswith(b"\x89PNG")
        context = ToolContext(scope="private:render-acceptance")
        tool = client.portal.call(services.tools.execute, "render_html", data, context)
        key = tool["attachment"].split(":", 1)[1]
        assert services.media.read(key, context.scope).mime == "image/png"
        try:
            services.media.read(key, "group:different-scope")
        except MediaError:
            pass
        else:
            raise AssertionError("Card leaked across scopes")
        assert not services.renderer.processes
    results = json.loads((output / "result.json").read_text())
    results.append({"authenticated_api_png": artifact, "real_tool_scope_isolation": True})
    (output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print("真实认证 API 出图、受保护读取、真实 render_html 工具和会话隔离均通过。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--install",
        action="store_true",
        help="Download Chromium into the project's private data directory",
    )
    asyncio.run(main(parser.parse_args().install))
    verify_api()
