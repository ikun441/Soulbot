"""Adapter executed only by the isolated meme-generator interpreter.

No upstream code is vendored or changed. Its localstore paths and Skia font
collection are configured before the upstream server is imported.
"""

import argparse
import asyncio
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import sys
from pathlib import Path

VERSION = "0.1.14"
OFFICIAL = f"https://raw.githubusercontent.com/MemeCrafters/meme-generator/v{VERSION}/"


def configure(root, port):
    distribution = importlib.metadata.distribution("meme-generator")
    if distribution.version != VERSION:
        raise RuntimeError("Unexpected meme-generator version")
    source = distribution.locate_file("meme_generator/dirs.py")
    spec = importlib.util.spec_from_file_location("meme_generator.dirs", source)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    for kind in ("CACHE", "CONFIG", "DATA"):
        directory = root / kind.lower()
        directory.mkdir(parents=True, exist_ok=True)
        setattr(module, f"BASE_{kind}_DIR", directory)
    (root / "config" / "config.toml").write_text(
        f'[server]\nhost = "127.0.0.1"\nport = {port}\n'
        '[log]\nlog_level = "WARNING"\n'
        '[resource]\nresource_url = "https://raw.githubusercontent.com/MemeCrafters/meme-generator/"\n',
        encoding="utf-8",
    )
    return Path(distribution.locate_file("meme_generator"))


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".partial")
    temporary.write_bytes(content)
    temporary.chmod(0o600)
    os.replace(temporary, path)


async def prepare(root, package):
    import httpx

    async def download(client, relative, maximum=64 * 1024 * 1024):
        for attempt in range(2):
            try:
                async with client.stream("GET", OFFICIAL + relative) as response:
                    response.raise_for_status()
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        content.extend(chunk)
                        if len(content) > maximum:
                            raise ValueError("Resource is too large")
                return bytes(content)
            except httpx.HTTPError:
                if attempt:
                    raise
                await asyncio.sleep(0.5)

    async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
        raw = await download(client, "resources/resource_list.json", 2 * 1024 * 1024)
        entries = json.loads(raw)
        if not isinstance(entries, list) or not 1 <= len(entries) <= 10000:
            raise ValueError("Invalid resource manifest")
        semaphore = asyncio.Semaphore(6)
        completed = 0

        async def one(entry):
            nonlocal completed
            relative = Path(entry["path"])
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Unsafe resource path")
            target = (package / "memes" / relative).resolve()
            if not target.is_relative_to((package / "memes").resolve()):
                raise ValueError("Unsafe resource target")
            expected = entry["hash"]
            async with semaphore:
                if not target.is_file() or hashlib.md5(target.read_bytes()).hexdigest() != expected:
                    content = await download(client, "meme_generator/memes/" + relative.as_posix())
                    if hashlib.md5(content).hexdigest() != expected:
                        raise ValueError("Resource checksum mismatch")
                    write(target, content)
            completed += 1
            if completed % 100 == 0 or completed == len(entries):
                print(f"resources {completed}/{len(entries)}", flush=True)

        await asyncio.gather(*(one(entry) for entry in entries))
        font = root / "fonts" / "NotoSansSC-Regular.ttf"
        if not font.exists():
            write(font, await download(client, "resources/fonts/NotoSansSC-Regular.ttf"))
        import skia

        if skia.Typeface.MakeFromFile(str(font)) is None:
            raise ValueError("Invalid font resource")
        write(
            root / "ready.json",
            json.dumps(
                {
                    "version": VERSION,
                    "resources": len(entries),
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "font_sha256": hashlib.sha256(font.read_bytes()).hexdigest(),
                }
            ).encode(),
        )
        print("resources verified", flush=True)


def serve(root):
    import skia
    from pil_utils.text2image import font_collection

    fonts = skia.textlayout.TypefaceFontProvider()
    font = skia.Typeface.MakeFromFile(str(root / "fonts" / "NotoSansSC-Regular.ttf"))
    if font is None:
        raise ValueError("Chinese font is not ready")
    fonts.registerTypeface(font)
    font_collection.setDefaultFontManager(fonts, font.getFamilyName())
    from meme_generator.app import run_server

    run_server()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("prepare", "run"))
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--port", type=int, default=2233)
    args = parser.parse_args()
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    package = configure(root, args.port)
    if args.mode == "prepare":
        asyncio.run(prepare(root, package))
    else:
        if json.loads((root / "ready.json").read_text())["version"] != VERSION:
            raise ValueError("Run preparation first")
        serve(root)


if __name__ == "__main__":
    main()
