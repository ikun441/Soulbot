"""Cross-platform, idempotent launcher. All packages live in the project .venv."""

import hashlib
import os
import subprocess
import sys
import venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise SystemExit("需要 Python 3.11–3.13。请安装后重试，详见 start.md。")
    os.chdir(ROOT)
    python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not python.exists():
        print("正在创建项目专用 .venv，不会全局安装依赖……", flush=True)
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    dependencies = ROOT / "requirements.txt"
    lockfile = ROOT / "requirements.lock"
    fingerprint = hashlib.sha256(
        dependencies.read_bytes() + (lockfile.read_bytes() if lockfile.exists() else b"")
    ).hexdigest()
    stamp = ROOT / ".venv" / ".soulbot-dependencies"
    if not stamp.exists() or stamp.read_text() != fingerprint:
        print("正在安装/检查依赖，首次启动可能需要几分钟……", flush=True)
        command = [str(python), "-m", "pip", "install", "-r", str(dependencies)]
        if lockfile.exists():
            command += ["-c", str(lockfile)]
        subprocess.run(command, check=True)
        stamp.write_text(fingerprint)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    print("meme-generator 按 WebUI 配置在项目隔离目录准备和启动，不安装系统服务。", flush=True)
    if "--check" in sys.argv:
        subprocess.run([str(python), str(ROOT / "main.py"), "--check"], check=True)
        return
    raise SystemExit(subprocess.call([str(python), str(ROOT / "main.py")]))


if __name__ == "__main__":
    try:
        main()
    except (subprocess.CalledProcessError, OSError):
        print("启动准备失败。请检查网络、Python 安装和目录权限，详见 start.md。")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        raise SystemExit(0) from None
