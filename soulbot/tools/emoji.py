import hashlib
import json
import os
import re
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from threading import RLock

from soulbot.services.media_service import MAX_IMAGE_BYTES, ImagePayload, MediaError, inspect_image

EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class EmojiLibrary:
    def __init__(self, root, config, db):
        self.root, self.config, self.db = root / "emoji", config, db
        if self.root.is_symlink():
            raise MediaError("emoji 目录不能是符号链接")
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        for category in ("开心", "难过", "生气", "整活", "其他"):
            (self.root / category).mkdir(exist_ok=True)

    def require(self):
        if not self.config.current.emoji.enabled:
            raise MediaError("Emoji 功能已关闭")

    def path(self, relative):
        path = self.root / relative
        if (
            self.root.is_symlink()
            or path.is_symlink()
            or not path.resolve().is_relative_to(self.root.resolve())
        ):
            raise MediaError("表情路径超出库目录")
        return path

    def refresh(self):
        self.require()
        entries = []
        recursive = self.config.current.emoji.recursive
        with self.lock:
            for directory, folders, files in os.walk(self.root, followlinks=False):
                folders[:] = (
                    sorted(
                        name
                        for name in folders
                        if not name.startswith(".") and not (Path(directory) / name).is_symlink()
                    )
                    if recursive
                    else []
                )
                for name in sorted(files):
                    path = Path(directory) / name
                    if (
                        name.startswith(".")
                        or path.suffix.lower() not in EXTENSIONS
                        or path.is_symlink()
                    ):
                        continue
                    try:
                        relative = path.relative_to(self.root).as_posix()
                        path = self.path(relative)
                        stat = path.stat()
                        if not 0 < stat.st_size <= MAX_IMAGE_BYTES:
                            continue
                        key = hashlib.sha256(relative.encode()).hexdigest()[:32]
                        entries.append(
                            (key, relative, name, Path(relative).parent.as_posix(), stat.st_mtime)
                        )
                    except (OSError, MediaError):
                        continue
            with self.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                old = {row[0] for row in db.execute("SELECT id FROM emoji_index")}
                for row in entries:
                    db.execute(
                        "INSERT INTO emoji_index(id,path,name,category,mtime) VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET path=excluded.path,name=excluded.name,category=excluded.category,mtime=excluded.mtime",
                        row,
                    )
                    old.discard(row[0])
                db.executemany("DELETE FROM emoji_index WHERE id=?", [(key,) for key in old])
        return {"total": len(entries), "message": "表情索引已更新"}

    def public(self, row):
        item = dict(row)
        item["name"] = item["name"][: self.config.current.emoji.max_filename_chars]
        item["tags"], item["aliases"] = json.loads(item["tags"]), json.loads(item["aliases"])
        item["url"] = "/api/emoji/" + item["id"] + "/image"
        item.pop("path")
        return item

    def get(self, key):
        self.require()
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise MediaError("表情标识无效")
        with self.db.connect() as db:
            row = db.execute("SELECT * FROM emoji_index WHERE id=?", (key,)).fetchone()
        if not row:
            raise MediaError("表情不存在，请重新索引")
        return dict(row)

    def browse(self, offset=0, limit=100):
        self.require()
        options = self.config.current.emoji
        if not options.browse_enabled:
            raise MediaError("分页浏览已关闭，仍可以关键词检索")
        offset, limit = max(0, offset), min(max(1, limit), options.max_files)
        with self.db.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM emoji_index").fetchone()[0]
            rows = db.execute(
                "SELECT * FROM emoji_index ORDER BY category,name,id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {
            "items": [self.public(row) for row in rows],
            "offset": offset,
            "limit": limit,
            "total": total,
        }

    def search(self, query, limit=12):
        self.require()
        query = query.strip().casefold()[:100]
        if not query:
            return {"items": [], "total": 0}
        with self.db.connect() as db:
            rows = db.execute("SELECT * FROM emoji_index").fetchall()
        scored = []
        for row in rows:
            values = [
                row["name"].casefold(),
                row["category"].casefold(),
                *[v.casefold() for v in json.loads(row["tags"]) + json.loads(row["aliases"])],
            ]
            score = max(
                (1.0 if query in value else SequenceMatcher(None, query, value).ratio())
                for value in values
            )
            if score >= 0.4:
                scored.append((score, row))
        scored.sort(key=lambda pair: (-pair[0], pair[1]["path"]))
        maximum = min(max(1, limit), self.config.current.emoji.max_candidates)
        return {"items": [self.public(row) for _, row in scored[:maximum]], "total": len(scored)}

    def read(self, key):
        row = self.get(key)
        try:
            path = self.path(row["path"])
            if path.stat().st_size > MAX_IMAGE_BYTES:
                raise MediaError("表情图片过大")
            with path.open("rb") as stream:
                content = stream.read(MAX_IMAGE_BYTES + 1)
        except OSError:
            raise MediaError("表情文件已删除或无法访问，请重新索引") from None
        _, mime, _, _ = inspect_image(content)
        return ImagePayload(content, mime)

    def update(self, key, tags, aliases):
        self.get(key)
        with self.db.connect() as db:
            db.execute(
                "UPDATE emoji_index SET tags=?,aliases=? WHERE id=?",
                (
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(aliases, ensure_ascii=False),
                    key,
                ),
            )
        return self.public(self.get(key))

    def upload(self, filename, content, category):
        self.require()
        if (
            not filename
            or Path(filename).name != filename
            or any(c in filename for c in ("/", "\\", "\x00"))
            or filename.startswith(".")
            or len(filename) > 120
        ):
            raise MediaError("文件名无效，请使用普通图片文件名")
        if not re.fullmatch(r"[\w\-\u4e00-\u9fff ]{1,40}", category):
            raise MediaError("分类只能使用中文、字母、数字、空格、下划线或短横线")
        if Path(filename).suffix.lower() not in EXTENSIONS:
            raise MediaError("请选择 PNG/JPG/GIF/WebP 图片")
        extension, _, _, _ = inspect_image(content)
        with self.lock:
            directory = self.path(category)
            directory.mkdir(exist_ok=True)
            path = directory / (Path(filename).stem + "." + extension)
            if path.exists():
                path = path.with_name(path.stem + "-" + uuid.uuid4().hex[:8] + path.suffix)
            with path.open("xb") as stream:
                os.chmod(path, 0o600)
                stream.write(content)
            relative = path.relative_to(self.root).as_posix()
            self.refresh()
        return self.public(self.get(hashlib.sha256(relative.encode()).hexdigest()[:32]))
