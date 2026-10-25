"""Real reviewed repository installation and rendering, using disposable local data."""

import asyncio
import json
import shutil
import uuid
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

from soulbot.services.config_service import Settings
from soulbot.services.media_service import ImagePayload, MediaError, MediaStore
from soulbot.services.meme_extensions import checksum, sources
from soulbot.services.meme_service import MemeClient, MemeService
from soulbot.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


async def running(manager):
    async with asyncio.timeout(1800):
        while not manager.status()["running"]:
            if manager.phase == "failed":
                raise RuntimeError(manager.message)
            if manager.extension_error:
                raise RuntimeError(manager.extension_error)
            await asyncio.sleep(0.2)


async def main():
    output = ROOT / "output" / "extension-live"
    home = output / "home"
    service_root = home / "third_party" / "meme-generator"
    service_root.mkdir(parents=True, exist_ok=True)
    base = ROOT / "third_party" / "meme-generator"
    environment = service_root / ".venv"
    if not environment.exists():
        environment.symlink_to(base / ".venv", target_is_directory=True)
    shutil.copytree(base / "fonts", service_root / "fonts", dirs_exist_ok=True)
    shutil.copy2(base / "ready.json", service_root / "ready.json")
    settings = Settings.model_validate(
        {
            "meme": {
                "enabled": True,
                "base_url": "http://127.0.0.1:2234",
                "auto_install": False,
                "max_generations_per_minute": 120,
            }
        }
    )
    config = SimpleNamespace(current=settings)
    db = Database(output / "data" / "soulbot.db")
    manager = MemeService(home, config, db.audit, db)
    store = MediaStore(home, db)
    # Large archives were downloaded from the pinned official commits during review.
    for name in ("emoji", "tudou"):
        reviewed = ROOT / "third_party" / "extension-review" / (name + ".zip")
        expected = sources()[name]["sha256"]
        target = manager.extensions.root / "cache" / (expected + ".zip")
        if reviewed.exists() and not target.exists():
            assert checksum(reviewed) == expected
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(reviewed, target)
    buffer = BytesIO()
    Image.new("RGB", (96, 96), "pink").save(buffer, format="PNG")
    avatar = ImagePayload(buffer.getvalue(), "image/png")
    examples = {
        "contrib": ("operator_generator", ["SoulBot"]),
        "emoji": ("dog_ear_hat", []),
        "tudou": ("qixiong", []),
    }
    results = {}
    try:
        for name, (key, texts) in examples.items():
            for source, profile in settings.meme.extensions.items():
                profile.enabled = source == name
                profile.max_generations_per_minute = 120
            print("Installing verified source: " + name, flush=True)
            await manager.install_extension(name)
            await running(manager)
            report = manager.runtime()["extensions"][name]
            if report["error_count"]:
                raise RuntimeError(json.dumps(report, ensure_ascii=False))
            assert key in report["loaded_ids"]
            assert (await manager.client.info(key))["soulbot_source"] == name
            image = store.save(
                await manager.client.generate(key, texts, [avatar]), "extension_acceptance"
            )
            results[name] = {
                "commit": sources()[name]["commit"],
                "loaded": len(report["loaded_ids"]),
                "conflicts": len(report["conflicts"]),
                "example": key,
                "image": image,
            }
        await manager.close()
        for profile in settings.meme.extensions.values():
            profile.enabled = True
        await manager.begin()
        await running(manager)
        combined = manager.runtime()
        results["combined"] = {
            "templates": (await manager.client.health())["templates"],
            "sources": {
                key: {
                    "loaded": len(value["loaded_ids"]),
                    "conflicts": len(value["conflicts"]),
                    "errors": value["error_count"],
                }
                for key, value in combined["extensions"].items()
            },
        }
        settings.meme.disabled_ids = ["operator_generator"]
        try:
            await manager.client.info("operator_generator")
        except MediaError:
            results["disabled_id_rejected"] = True
        else:
            raise AssertionError("Disabled ID was not enforced")
        settings.meme.disabled_ids = []
        isolated_quota = Database(output / "data" / ("quota-" + uuid.uuid4().hex + ".db"))
        quota_client = MemeClient(config, service_root, isolated_quota)
        origin = combined["origins"]["dog_ear_hat"]
        settings.meme.extensions[origin].max_generations_per_minute = 1
        await quota_client.generate("dog_ear_hat", [], [avatar])
        restarted_client = MemeClient(config, service_root, isolated_quota)
        try:
            await restarted_client.generate("dog_ear_hat", [], [avatar])
        except MediaError:
            results["persistent_source_rate_rejected"] = True
        else:
            raise AssertionError("Repository rate limit was not enforced after client restart")
        (output / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(results, ensure_ascii=False, indent=2))
    finally:
        await manager.close()


if __name__ == "__main__":
    asyncio.run(main())
