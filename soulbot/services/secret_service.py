import json
from pathlib import Path
from threading import RLock

from soulbot.services.config_service import atomic_write
from soulbot.services.environment_service import EnvironmentService

NAMES = {
    "llm_api_key": "SOULBOT_LLM_API_KEY",
    "onebot_access_token": "SOULBOT_ONEBOT_ACCESS_TOKEN",
    "llm_fallback_1": "SOULBOT_LLM_FALLBACK_1",
    "llm_fallback_2": "SOULBOT_LLM_FALLBACK_2",
    "tts_api_key": "SOULBOT_TTS_API_KEY",
    "asr_api_key": "SOULBOT_ASR_API_KEY",
}


class SecretService:
    def __init__(self, root: Path, environment=None):
        self.path = root / "data" / "secrets.json"
        self.environment = environment or EnvironmentService(root)
        self.lock = RLock()

    def values(self):
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, name):
        env_name = NAMES[name]
        return self.environment.get(env_name) or self.values().get(name, "")

    def save(self, changes: dict[str, str]):
        if not changes.keys() <= NAMES.keys():
            raise ValueError("不支持的密钥名称")
        with self.lock:
            values = self.values()
            values.update(changes)
            atomic_write(self.path, json.dumps(values))

    def status(self):
        return {name: bool(self.get(name)) for name in NAMES}
