"""Consistent private snapshots and validated restoration while the instance is stopped."""

import hashlib
import io
import json
import os
import re
import sqlite3
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath
from uuid import uuid4

import yaml

from soulbot import VERSION
from soulbot.services.config_service import ConfigService, Settings, atomic_write
from soulbot.services.secret_service import NAMES

MAX_BACKUP = 256 * 1024 * 1024


class InstanceLock:
    def __init__(self, root):
        self.path = Path(root) / "data" / "runtime.lock"
        self.stream = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.stream = self.path.open("a+b")
        self.path.chmod(0o600)
        try:
            if os.name == "nt":
                import msvcrt

                self.stream.seek(0)
                self.stream.write(b"0")
                self.stream.flush()
                self.stream.seek(0)
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            self.stream = None
            raise ValueError("此实例仍在运行，请先停止 SoulBot 再启动或恢复备份") from None
        return self

    def __exit__(self, *_):
        if self.stream:
            self.stream.close()
            self.stream = None


class BackupService:
    def __init__(self, root):
        self.root = Path(root).resolve()

    @staticmethod
    def allowed(name):
        path = PurePosixPath(name)
        if (
            name != path.as_posix()
            or path.is_absolute()
            or any(part in {"..", "."} for part in path.parts)
            or "\\" in name
            or ":" in name
        ):
            return False
        return (
            name in {"config.yaml", ".env", "data/secrets.json", "data/soulbot.db"}
            or (
                len(path.parts) > 1
                and path.parts[0] == "emoji"
                and path.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}
            )
            or (
                len(path.parts) == 3
                and path.parts[:2] == ("data", "wallpapers")
                and re.fullmatch(r"[a-f0-9]{32}\.(png|jpg|gif|webp|mp4|webm)", path.name)
            )
        )

    def snapshot(self):
        files = {}
        candidates = [self.root / name for name in ("config.yaml", ".env", "data/secrets.json")]
        for directory in (self.root / "emoji", self.root / "data/wallpapers"):
            if directory.exists():
                candidates.extend(directory.rglob("*"))
        for path in candidates:
            if path.is_symlink() or not path.resolve().is_relative_to(self.root):
                raise ValueError("备份目录包含符号链接，已停止读取")
            name = path.relative_to(self.root).as_posix()
            if path.is_file() and self.allowed(name):
                if (
                    path.stat().st_size > MAX_BACKUP
                    or sum(map(len, files.values())) + path.stat().st_size > MAX_BACKUP
                ):
                    raise ValueError("备份超过 256MB，请先缩小壁纸或表情库")
                files[name] = path.read_bytes()
        db_path = self.root / "data/soulbot.db"
        if not db_path.is_file() or db_path.is_symlink():
            raise ValueError("没有可备份的数据库")
        with tempfile.TemporaryDirectory(dir=self.root / "data", prefix="snapshot-") as temporary:
            destination = Path(temporary) / "soulbot.db"
            with (
                sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True) as source,
                sqlite3.connect(destination) as target,
            ):
                source.backup(target)
            if destination.stat().st_size + sum(map(len, files.values())) > MAX_BACKUP:
                raise ValueError("备份超过 256MB")
            files["data/soulbot.db"] = destination.read_bytes()
        manifest = {
            "format": 1,
            "version": VERSION,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in files.items()},
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            for name, data in files.items():
                archive.writestr(name, data)
        return output.getvalue()

    def validate(self, content):
        if not content or len(content) > MAX_BACKUP:
            raise ValueError("备份不能为空或超过 256MB")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                infos = archive.infolist()
                names = [item.filename for item in infos]
                if (
                    len(infos) > 10000
                    or len(set(names)) != len(names)
                    or sum(item.file_size for item in infos) > MAX_BACKUP
                ):
                    raise ValueError
                if any(
                    item.is_dir() or stat.S_ISLNK(item.external_attr >> 16) or item.flag_bits & 1
                    for item in infos
                ):
                    raise ValueError
                if "manifest.json" not in names or any(
                    name != "manifest.json" and not self.allowed(name) for name in names
                ):
                    raise ValueError
                manifest = json.loads(archive.read("manifest.json"))
                if manifest.get("format") != 1 or set(manifest["files"]) != set(names) - {
                    "manifest.json"
                }:
                    raise ValueError
                files = {name: archive.read(name) for name in names if name != "manifest.json"}
                if not {"config.yaml", "data/soulbot.db"} <= files.keys():
                    raise ValueError
                if any(
                    hashlib.sha256(data).hexdigest() != manifest["files"][name]
                    for name, data in files.items()
                ):
                    raise ValueError
            Settings.model_validate(yaml.safe_load(files["config.yaml"]))
            if "data/secrets.json" in files:
                keys = json.loads(files["data/secrets.json"])
                if (
                    not isinstance(keys, dict)
                    or not keys.keys() <= NAMES.keys()
                    or any(not isinstance(v, str) for v in keys.values())
                ):
                    raise ValueError
            with tempfile.TemporaryDirectory(
                dir=self.root / "data", prefix="validate-"
            ) as temporary:
                path = Path(temporary) / "soulbot.db"
                path.write_bytes(files["data/soulbot.db"])
                with sqlite3.connect(path) as db:
                    if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                        raise ValueError
                    version = int(
                        db.execute(
                            "SELECT value FROM metadata WHERE key='schema_version'"
                        ).fetchone()[0]
                    )
                    if not 1 <= version <= 15:
                        raise ValueError
            return files
        except (
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
            zipfile.BadZipFile,
            RuntimeError,
            sqlite3.Error,
            yaml.YAMLError,
        ):
            raise ValueError("备份格式、校验和、配置或数据库无效；未替换现有数据") from None

    def stage(self, content):
        self.validate(content)
        if (self.root / "data/restore-pending.zip").exists():
            raise ValueError("已有待恢复备份，请先取消或重启应用")
        self.write_bytes(self.root / "data/restore-pending.zip", content)
        return {
            "ok": True,
            "restart_required": True,
            "message": "备份已校验。停止并重新启动 SoulBot 后恢复；旧数据会自动备份，恢复后需重新登录并恢复自动化。",
        }

    @staticmethod
    def write_bytes(path, content):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".restore-")
        try:
            with os.fdopen(fd, "wb") as stream:
                os.chmod(temporary, 0o600)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def preserve_limits(self, files):
        """Restoring an older snapshot must not refund costs, sends or cooldowns."""
        from soulbot.storage.database import Database

        with tempfile.TemporaryDirectory(dir=self.root / "data", prefix="merge-") as temporary:
            path = Path(temporary) / "soulbot.db"
            path.write_bytes(files["data/soulbot.db"])
            migrated = Database(path)
            tables = (
                "llm_usage",
                "tts_usage",
                "asr_usage",
                "image_usage",
                "search_usage",
                "proactive_usage",
                "qzone_attempts",
                "group_state",
                "conversation_state",
                "seen_events",
                "proactive_draft_runs",
                "proactive_contacts",
                "memory_preferences",
                "diary_preferences",
                "tool_approvals",
            )
            with (
                sqlite3.connect(
                    f"{(self.root / 'data/soulbot.db').as_uri()}?mode=ro", uri=True
                ) as source,
                migrated.connect() as target,
            ):
                available = {
                    r[0]
                    for r in source.execute("SELECT name FROM sqlite_master WHERE type='table'")
                }
                for table in tables:
                    if table not in available:
                        continue
                    columns = [r[1] for r in source.execute(f"PRAGMA table_info({table})")]
                    names = ",".join(columns)
                    values = source.execute(f"SELECT {names} FROM {table}").fetchall()
                    placeholders = ",".join("?" for _ in columns)
                    target.executemany(
                        f"INSERT OR REPLACE INTO {table}({names}) VALUES ({placeholders})", values
                    )
                # Prevent replaying jobs/drafts that were already dispatched after the snapshot.
                for table, key, statuses in (
                    (
                        "proactive_jobs",
                        "id",
                        ("running", "sent", "uncertain", "cancelled", "rejected"),
                    ),
                    ("qzone_drafts", "id", ("publishing", "published", "uncertain", "rejected")),
                ):
                    if table in available:
                        placeholders = ",".join("?" for _ in statuses)
                        for (identity,) in source.execute(
                            f"SELECT {key} FROM {table} WHERE status IN ({placeholders})", statuses
                        ):
                            target.execute(
                                f"UPDATE {table} SET status='uncertain' WHERE {key}=? AND status NOT IN ('published','sent')",
                                (identity,),
                            )
                if "qzone_drafts" in available:
                    maximum = source.execute(
                        "SELECT COALESCE(MAX(id),0) FROM qzone_drafts"
                    ).fetchone()[0]
                    if target.execute(
                        "SELECT 1 FROM sqlite_sequence WHERE name='qzone_drafts'"
                    ).fetchone():
                        target.execute(
                            "UPDATE sqlite_sequence SET seq=MAX(seq,?) WHERE name='qzone_drafts'",
                            (maximum,),
                        )
                    else:
                        target.execute(
                            "INSERT INTO sqlite_sequence VALUES ('qzone_drafts',?)", (maximum,)
                        )
            # Force all merged WAL records into the standalone database bytes.
            with sqlite3.connect(path) as db:
                db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            files["data/soulbot.db"] = path.read_bytes()

    def apply_pending(self, lock):
        """Caller MUST hold InstanceLock. No running server may replace its own database."""
        if (
            not isinstance(lock, InstanceLock)
            or not lock.stream
            or lock.path.resolve() != self.root / "data/runtime.lock"
        ):
            raise ValueError("恢复前必须持有实例锁")
        pending = self.root / "data/restore-pending.zip"
        if not pending.exists():
            return False
        files = self.validate(pending.read_bytes())
        recovery = self.root / "data/backups" / ("before-restore-" + uuid4().hex + ".zip")
        self.write_bytes(recovery, self.snapshot())
        files.setdefault(".env", b"")
        files.setdefault("data/secrets.json", b"{}")
        self.preserve_limits(files)
        old = {
            name: (self.root / name).read_bytes() if (self.root / name).is_file() else None
            for name in files
        }
        # Remove WAL sidecars only after the instance lock; snapshot already contains committed WAL data.
        for suffix in ("-wal", "-shm"):
            (self.root / ("data/soulbot.db" + suffix)).unlink(missing_ok=True)
        try:
            for name, data in files.items():
                target = self.root / name
                if target.is_symlink() or not target.resolve().is_relative_to(self.root):
                    raise ValueError("恢复目标包含符号链接")
                self.write_bytes(target, data)
            config = Settings.model_validate(yaml.safe_load(files["config.yaml"]))
            config.automation.paused = True
            atomic_write(self.root / "config.yaml", ConfigService.dump(config))
            with sqlite3.connect(self.root / "data/soulbot.db") as db:
                db.execute("DELETE FROM sessions")
        except BaseException:
            for name, data in old.items():
                if data is None:
                    (self.root / name).unlink(missing_ok=True)
                else:
                    self.write_bytes(self.root / name, data)
            raise
        pending.unlink()
        return True
