"""Stamp native module imports and entry assets; --check never modifies files."""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from soulbot import VERSION  # noqa: E402

RELATIVE = re.compile(r"(?P<quote>['\"])(?P<path>\./[\w./-]+\.js)(?:\?v=v[\d.]+)?(?P=quote)")
STATIC = re.compile(
    r"(?P<quote>['\"])(?P<path>/static/(?:js|css|assets)/[\w./-]+\.(?:js|css|png|svg))(?:\?v=v[\d.]+)?(?P=quote)"
)


def stamp(text, version):
    def replace(match):
        return f"{match['quote']}{match['path']}?v={version}{match['quote']}"

    return STATIC.sub(replace, RELATIVE.sub(replace, text))


def sync(check=False):
    static = ROOT / "soulbot/web/static"
    files = [static / "index.html", *sorted((static / "js").glob("*.js"))]
    outdated = []
    for path in files:
        original = path.read_text()
        updated = stamp(original, VERSION)
        if original != updated:
            outdated.append(str(path.relative_to(ROOT)))
            if not check:
                path.write_text(updated)
    if check and outdated:
        raise ValueError("静态资源版本未同步，请先运行 .venv/bin/python scripts/web_assets.py")
    return len(outdated)


if __name__ == "__main__":
    print(f"{VERSION} 静态资源同步：{sync('--check' in sys.argv)} 个文件需要更新")
