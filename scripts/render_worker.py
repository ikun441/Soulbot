"""Owned production renderer, not an interactive browser or arbitrary HTML endpoint."""

import argparse
import asyncio
import base64
import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pydantic import Field  # noqa: E402

from soulbot.services.config_service import Model  # noqa: E402
from soulbot.tools.html_render import FONT_SHA256, CardRequest, card_html  # noqa: E402


class RenderJob(Model):
    card: CardRequest
    width: int = Field(ge=480, le=1200)
    max_height: int = Field(ge=400, le=2400)


async def run(root, probe):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            channel="chromium",
            headless=True,
            chromium_sandbox=True,
            timeout=10000,
            args=[
                "--disable-background-networking",
                "--disable-component-update",
                "--disable-sync",
                "--host-resolver-rules=MAP * ~NOTFOUND",
            ],
        )
        try:
            if probe:
                executable = Path(playwright.chromium.executable_path).resolve()
                if not executable.is_relative_to((root / "browsers").resolve()):
                    raise ValueError("browser")
                sys.stdout.write(
                    json.dumps(
                        {
                            "version": version("playwright"),
                            "executable": str(executable),
                            "browser_version": browser.version,
                        }
                    )
                )
                return
            job = RenderJob.model_validate_json(sys.stdin.buffer.read(32769))
            font_path = root / "NotoSansSC-Regular.ttf"
            if font_path.is_symlink():
                raise ValueError("font")
            font = font_path.read_bytes()
            if hashlib.sha256(font).hexdigest() != FONT_SHA256:
                raise ValueError("font")
            context = await browser.new_context(
                java_script_enabled=False,
                offline=True,
                service_workers="block",
                accept_downloads=False,
                viewport={"width": job.width, "height": job.max_height},
                device_scale_factor=1,
                locale="zh-CN",
            )
            try:
                await context.route("**/*", lambda route: route.abort())
                page = await context.new_page()
                page.set_default_timeout(10000)
                await page.set_content(
                    card_html(job.card, base64.b64encode(font).decode()), wait_until="load"
                )
                # Fixed controller expression; no model code is evaluated.
                await page.evaluate("document.fonts.ready")
                target = page.locator("#card")
                bounds = await target.bounding_box()
                if not bounds or bounds["height"] > job.max_height:
                    raise ValueError("height")
                image = await target.screenshot(
                    type="png", animations="disabled", omit_background=True
                )
                sys.stdout.buffer.write(image)
            finally:
                await context.close()
        finally:
            await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--probe", action="store_true")
    arguments = parser.parse_args()
    try:
        asyncio.run(run(arguments.root.resolve(), arguments.probe))
    except Exception as error:
        code = (
            str(error)
            if isinstance(error, ValueError) and str(error) in {"height", "font", "browser"}
            else "runtime"
        )
        sys.stderr.write(json.dumps({"error": code}))
        raise SystemExit(1) from None
