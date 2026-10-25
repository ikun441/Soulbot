"""Download only reviewed public snapshots; never accept arbitrary URLs or commands."""

import asyncio
import hashlib
import json
import os
import shutil
import stat
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import httpx

from soulbot.services.config_service import atomic_write
from soulbot.services.media_service import MediaError

CATALOG = Path(__file__).resolve().parents[2] / "scripts" / "meme-sources.json"


def sources():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def checksum(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for content in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(content)
    return digest.hexdigest()


def unpack(archive, destination, source):
    total, paths = 0, []
    with zipfile.ZipFile(archive) as bundle:
        entries = bundle.infolist()
        if not entries or len(entries) > 50000:
            raise MediaError("扩展压缩包的文件数量无效")
        tops = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (
                path.is_absolute()
                or ".." in path.parts
                or "\\" in entry.filename
                or any(":" in part for part in path.parts)
                or stat.S_ISLNK(mode)
                or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)))
            ):
                raise MediaError("扩展压缩包含不安全路径或特殊文件，已拒绝安装")
            if not path.parts:
                continue
            tops.add(path.parts[0])
            total += entry.file_size
            if total > 3 * 1024**3 or entry.file_size > 512 * 1024**2:
                raise MediaError("扩展解压后超过大小限制")
            paths.append(entry)
        if len(tops) != 1:
            raise MediaError("扩展压缩包目录结构不符合固定版本")
        destination.mkdir(parents=True)
        for entry in paths:
            relative = PurePosixPath(entry.filename).parts[1:]
            if not relative:
                continue
            target = destination.joinpath(*relative)
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as incoming, target.open("xb") as output:
                    shutil.copyfileobj(incoming, output, 1024 * 1024)
                target.chmod(0o600)
        if (
            not (destination / source["directory"]).is_dir()
            or not (destination / "LICENSE").is_file()
        ):
            raise MediaError("扩展缺少模板目录或许可证文件")
    return total


class MemeExtensions:
    def __init__(self, root, config):
        self.root, self.config = root / "extensions", config
        self.catalog = sources()
        self.transport = None

    def installed(self, name):
        if name not in self.catalog:
            raise MediaError("未知扩展仓库，只支持页面列出的三个已验证来源")
        source = self.catalog[name]
        try:
            data = json.loads((self.root / name / "installed.json").read_text())
            target = self.root / name / data["release"]
            if (
                data["commit"] != source["commit"]
                or data["sha256"] != source["sha256"]
                or target.is_symlink()
                or not target.resolve().is_relative_to((self.root / name).resolve())
                or not (target / source["directory"]).is_dir()
            ):
                return None
            return data
        except (OSError, KeyError, TypeError, ValueError):
            return None

    def status(self, runtime=None):
        runtime = runtime or {}
        result = []
        for name, source in self.catalog.items():
            profile = self.config.current.meme.extensions.get(name)
            data = self.installed(name)
            loaded = runtime.get("extensions", {}).get(name, {})
            result.append(
                {
                    "id": name,
                    **source,
                    "url": "https://github.com/" + source["repository"],
                    "installed": bool(data),
                    "enabled": bool(profile and profile.enabled),
                    "last_load": loaded,
                }
            )
        return result

    async def install(self, name, progress):
        if name not in self.catalog:
            raise MediaError("未知扩展仓库")
        source = self.catalog[name]
        directory = self.root / name
        directory.mkdir(parents=True, exist_ok=True)
        job = uuid.uuid4().hex
        archive = directory / ("download-" + job + ".zip.partial")
        stage = directory / ("stage-" + job)
        cache = self.root / "cache" / (source["sha256"] + ".zip")
        try:
            cached = (
                cache.is_file()
                and not cache.is_symlink()
                and await asyncio.to_thread(checksum, cache) == source["sha256"]
            )
            if not cached:
                digest, size, reported = hashlib.sha256(), 0, 0
                url = f"https://codeload.github.com/{source['repository']}/zip/{source['commit']}"
                async with httpx.AsyncClient(
                    timeout=120, follow_redirects=False, transport=self.transport
                ) as client:
                    async with client.stream("GET", url) as response:
                        response.raise_for_status()
                        with archive.open("xb") as output:
                            archive.chmod(0o600)
                            async for content in response.aiter_bytes():
                                size += len(content)
                                if size > 1024**3:
                                    raise MediaError("扩展压缩包超过 1GB 限制")
                                digest.update(content)
                                output.write(content)
                                if size - reported >= 8 * 1024**2:
                                    reported = size
                                    progress(
                                        f"正在下载 {source['repository']}：{size // 1024**2} MB"
                                    )
                if digest.hexdigest() != source["sha256"]:
                    raise MediaError(
                        "扩展压缩包校验不匹配，保留原安装版本；请检查网络或更新 SoulBot 来源目录"
                    )
                cache.parent.mkdir(parents=True, exist_ok=True)
                os.replace(archive, cache)
            progress("正在解压并验证扩展文件结构")
            unpacking = asyncio.create_task(asyncio.to_thread(unpack, cache, stage, source))
            try:
                expanded = await asyncio.shield(unpacking)
            except asyncio.CancelledError:
                await unpacking
                raise
            release = source["commit"] + "-" + job[:8]
            os.replace(stage, directory / release)
            data = {
                "repository": source["repository"],
                "commit": source["commit"],
                "sha256": source["sha256"],
                "release": release,
                "expanded_bytes": expanded,
            }
            atomic_write(directory / "installed.json", json.dumps(data))
            return data
        except (httpx.HTTPError, TimeoutError):
            raise MediaError("扩展下载失败，已保留原版本；请检查网络后重试") from None
        except zipfile.BadZipFile:
            raise MediaError("扩展压缩包无效，原有版本未受影响") from None
        finally:
            archive.unlink(missing_ok=True)
            if stage.exists():
                await asyncio.to_thread(shutil.rmtree, stage)

    def worker_settings(self):
        selected = []
        for name in self.catalog:
            profile = self.config.current.meme.extensions.get(name)
            if profile and profile.enabled and (data := self.installed(name)):
                selected.append(
                    {
                        "id": name,
                        "directory": str(
                            (
                                self.root / name / data["release"] / self.catalog[name]["directory"]
                            ).resolve()
                        ),
                        "commit": data["commit"],
                        "disabled_ids": profile.disabled_ids,
                    }
                )
        return {"extensions": selected, "disabled_ids": self.config.current.meme.disabled_ids}
