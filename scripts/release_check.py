"""Version and secret-file guard. Scans tracked/staged sources without printing matches."""

import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from soulbot import VERSION, __version__  # noqa: E402


def main():
    assert re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", VERSION)
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert metadata["tool"]["setuptools"]["dynamic"]["version"]["attr"] == "soulbot.__version__"
    assert f"## {VERSION} - " in (ROOT / "docs/version.md").read_text()
    assert (
        f"SoulBot {VERSION}" in (ROOT / "soulbot/web/static/js/app.js").read_text()
        or "B.version" in (ROOT / "soulbot/web/static/js/app.js").read_text()
    )
    forbidden = {".env", "config.yaml", ".venv", "data", "logs", "emoji", "third_party", "output"}
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode().split("\0")
    patterns = [re.compile(rb"gh[pousr]_[A-Za-z0-9]{20,}"), re.compile(rb"sk-[A-Za-z0-9_-]{24,}")]
    errors = []
    for name in filter(None, paths):
        path = Path(name)
        if (
            path.parts[0] in forbidden
            or path.name.startswith(".env.")
            and path.name != ".env.example"
        ):
            errors.append(f"敏感运行路径: {name}")
        data = (ROOT / path).read_bytes()
        if any(pattern.search(data) for pattern in patterns):
            errors.append(f"疑似密钥: {name}")
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(1)
    print(
        f"版本 {VERSION}（包 {__version__}）与 {len(list(filter(None, paths)))} 个受版本管理文件检查通过"
    )


if __name__ == "__main__":
    main()
