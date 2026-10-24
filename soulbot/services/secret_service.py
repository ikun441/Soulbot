import json
import os
from pathlib import Path
from threading import RLock

from dotenv import dotenv_values

from soulbot.services.config_service import atomic_write

NAMES = {"llm_api_key": "SOULBOT_LLM_API_KEY", "onebot_access_token": "SOULBOT_ONEBOT_ACCESS_TOKEN"}


class SecretService:
    def __init__(self, root: Path):
        self.path = root / "data" / "secrets.json"
        self.environment = dotenv_values(root / ".env")
        self.lock = RLock()

    def values(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, name):
        env_name = NAMES[name]
        return (
            os.environ.get(env_name)
            or self.environment.get(env_name)
            or self.values().get(name, "")
        )

    def save(self, changes: dict[str, str]):
        if not changes.keys() <= NAMES.keys():
            raise ValueError("不支持的密钥名称")
        with self.lock:
            values = self.values()
            values.update(changes)
            atomic_write(self.path, json.dumps(values))

    def status(self):
        return {name: bool(self.get(name)) for name in NAMES}
