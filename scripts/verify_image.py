"""One real cloud request using configured project quotas, never sends QQ."""

import asyncio
import json
from pathlib import Path

from soulbot.core.budget import BudgetService
from soulbot.providers.registry import ProviderRegistry
from soulbot.services.config_service import ConfigService
from soulbot.services.image_service import ImageGenerationService
from soulbot.services.media_service import MediaStore
from soulbot.services.secret_service import SecretService
from soulbot.storage.database import Database

ROOT = Path(__file__).resolve().parents[1]


async def main():
    config, secrets = ConfigService(ROOT), SecretService(ROOT)
    settings = config.current.image_generation
    if not settings.enabled or not settings.model.strip() or not secrets.get("image_api_key"):
        print(
            json.dumps(
                {
                    "cloud_live_test": "not_run",
                    "reason": "请先在 WebUI 启用并填写独立生图配置；没有使用替代图片或模拟结果",
                },
                ensure_ascii=False,
            )
        )
        return
    db = Database(ROOT / "data" / "soulbot.db")
    registry = ProviderRegistry()
    service = ImageGenerationService(config, secrets, registry, BudgetService(db), db)
    try:
        image = await service.generate(
            "A small pink glass greenhouse under a soft blue sky, watercolor illustration, no text.",
            "acceptance",
        )
        artifact = MediaStore(ROOT, db).save(image.data, "image_generation")
        result = {"cloud_live_test": "passed", "artifact": artifact, "qq_live_test": "not_run"}
        output = ROOT / "output" / "image-live"
        output.mkdir(parents=True, exist_ok=True)
        (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        await service.close()
        await registry.close()


if __name__ == "__main__":
    asyncio.run(main())
