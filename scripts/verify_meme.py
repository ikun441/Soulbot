"""Opt-in real upstream acceptance; never substitutes fixtures for a service.

Run after preparation: PYTHONPATH=. .venv/bin/python scripts/verify_meme.py
Uses the project third_party environment and isolated output/m2-live data.
"""

import asyncio
import json
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from soulbot.services.config_service import Settings
from soulbot.services.media_service import ImagePayload, MediaError, MediaStore
from soulbot.services.meme_service import MemeService
from soulbot.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


async def wait_running(manager, minimum_restarts=0):
    async with asyncio.timeout(120):
        while True:
            status = manager.status()
            if status["phase"] == "failed":
                raise RuntimeError(status["message"])
            if status["running"] and status["restarts"] >= minimum_restarts:
                return
            await asyncio.sleep(0.2)


async def main():
    settings = Settings.model_validate(
        {"meme": {"enabled": True, "auto_install": False, "base_url": "http://127.0.0.1:2234"}}
    )
    config = SimpleNamespace(current=settings)
    output = ROOT / "output" / "m2-live"
    db = Database(output / "data" / "soulbot.db")
    store = MediaStore(output, db)
    manager = MemeService(ROOT, config, db.audit, db)
    duplicate = MemeService(ROOT, config, db.audit, db)
    try:
        await manager.begin(install=True)
        await wait_running(manager)
        try:
            await duplicate.begin()
        except MediaError:
            pass
        else:
            raise AssertionError("Duplicate service lock was not enforced")
        health = await manager.client.health()
        info = await manager.client.info("raise_sign")
        assert info["params_type"]["min_texts"] == 1
        jpeg = store.save(
            await manager.client.generate("raise_sign", ["你好，SoulBot"], []), "live_meme"
        )
        preview = store.save(await manager.client.preview("raise_sign"), "live_preview")
        source = BytesIO()
        Image.new("RGB", (96, 96), "pink").save(source, format="PNG")
        gif = store.save(
            await manager.client.generate(
                "petpet", [], [ImagePayload(source.getvalue(), "image/png")]
            ),
            "live_meme",
        )
        assert jpeg["mime"] == "image/jpeg" and gif["mime"] == "image/gif"
        try:
            await manager.client.generate("raise_sign", [], [])
        except MediaError:
            pass
        else:
            raise AssertionError("Template input count was not enforced")
        old_process = manager.process
        old_process.kill()  # Only the process created by this acceptance run.
        await wait_running(manager, minimum_restarts=1)
        assert manager.process.pid != old_process.pid
        result = {
            "third_party": "meme-generator",
            "health": health,
            "jpeg": jpeg,
            "preview": preview,
            "gif": gif,
            "duplicate_lock": True,
            "invalid_input_rejected": True,
            "crash_restart": True,
        }
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await duplicate.close()
        await manager.close()
    assert manager.process is None and manager.lock.stream is None
    print("Owned service stopped; core data and external processes untouched.")


if __name__ == "__main__":
    asyncio.run(main())
