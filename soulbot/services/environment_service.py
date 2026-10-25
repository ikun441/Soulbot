"""Read project-local dotenv values without exporting credentials to child processes."""

import os
from io import StringIO
from pathlib import Path
from threading import RLock

from dotenv import dotenv_values
from dotenv.parser import parse_stream

from soulbot.services.config_service import atomic_write

USERNAME = "SOULBOT_WEBUI_USERNAME"
PASSWORD = "SOULBOT_WEBUI_PASSWORD"
PASSWORD_HASH = "SOULBOT_WEBUI_PASSWORD_HASH"
REVERSE_WS = "SOULBOT_ONEBOT_REVERSE_WS_URL"
ACCOUNT_KEYS = {USERNAME, PASSWORD, PASSWORD_HASH}


class EnvironmentError(ValueError):
    """Contains only fixed, user-readable text; never raw configuration values."""


class EnvironmentService:
    def __init__(self, root: Path):
        self.path = root / ".env"
        self.lock = RLock()

    def text(self):
        if self.path.is_symlink():
            raise EnvironmentError(".env 必须是项目目录中的普通文件，不能使用符号链接")
        if not self.path.exists():
            return ""
        if not self.path.is_file():
            raise EnvironmentError(".env 必须是普通文本文件")
        if self.path.stat().st_size > 1024 * 1024:
            raise EnvironmentError(".env 超过 1MB，请检查配置文件大小")
        text = self.path.read_text(encoding="utf-8-sig")
        if any(binding.error for binding in parse_stream(StringIO(text))):
            raise EnvironmentError(".env 语法有误，请检查引号是否成对；配置值不会显示在日志中")
        self.path.chmod(0o600)
        return text

    def values(self):
        with self.lock:
            # Passwords can contain ${...}; do not interpolate them or modify os.environ.
            return dotenv_values(stream=StringIO(self.text()), interpolate=False)

    def get(self, name):
        return os.environ.get(name) or self.values().get(name) or ""

    @staticmethod
    def process_managed_account():
        return any(os.environ.get(name) for name in ACCOUNT_KEYS)

    def update(self, changes):
        if not changes.keys() <= ACCOUNT_KEYS | {REVERSE_WS}:
            raise ValueError("不支持的环境配置名称")
        with self.lock:
            original = self.text()
            kept = "".join(
                binding.original.string
                for binding in parse_stream(StringIO(original))
                if binding.key not in changes
            )
            if kept and not kept.endswith("\n"):
                kept += "\n"
            for key, value in changes.items():
                if value is not None:
                    escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                    kept += f"{key}='{escaped}'\n"
            if kept != original:
                atomic_write(self.path, kept)
