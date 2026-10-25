import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
from threading import RLock

from fastapi import HTTPException
from pydantic import Field, SecretStr

from soulbot.services.config_service import Model
from soulbot.services.environment_service import PASSWORD, PASSWORD_HASH, USERNAME, EnvironmentError

ITERATIONS = 600000


class AdminIdentity(Model):
    username: str = Field(min_length=3, max_length=40, pattern=r"^[a-zA-Z0-9_\-]+$")


class AdminAccount(AdminIdentity):
    password: SecretStr = Field(min_length=12, max_length=128)


class AccountChange(AdminIdentity):
    current_password: SecretStr = Field(min_length=1, max_length=128)
    password: SecretStr | None = Field(default=None, min_length=12, max_length=128)


def valid_password_hash(stored):
    if not isinstance(stored, str) or not re.fullmatch(
        r"[0-9]{6,7}\$[a-f0-9]{32}\$[a-f0-9]{64}", stored
    ):
        return False
    return 100000 <= int(stored.split("$", 1)[0]) <= 2000000


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"{ITERATIONS}${salt}${digest}"


def verify_password(password, stored):
    if not valid_password_hash(stored):
        return False
    count, salt, expected = stored.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(count)).hex()
    return hmac.compare_digest(actual, expected)


class AuthService:
    def __init__(self, database, environment=None):
        self.db = database
        self.environment = environment
        self.lock = RLock()
        self.dummy_hash = hash_password(secrets.token_hex(32))
        if environment:
            self.apply_environment()

    def apply_environment(self):
        values = self.environment.values()
        username = os.environ.get(USERNAME) or values.get(USERNAME) or ""
        plain = os.environ.get(PASSWORD) or ""
        encoded = os.environ.get(PASSWORD_HASH) or ""
        if not plain and not encoded:
            plain = values.get(PASSWORD) or ""
            encoded = values.get(PASSWORD_HASH) or ""
        with self.lock, self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            admin = db.execute("SELECT * FROM admins WHERE id=1").fetchone()
            if not username and not plain and not encoded:
                if admin:
                    self.persist(admin["username"], admin["password_hash"])
                return
            if not username or not (plain or encoded):
                raise EnvironmentError(
                    ".env 管理员配置不完整：请同时设置 SOULBOT_WEBUI_USERNAME 与 SOULBOT_WEBUI_PASSWORD（或 PASSWORD_HASH）"
                )
            try:
                AdminIdentity(username=username)
                if plain:
                    AdminAccount(username=username, password=plain)
            except ValueError:
                raise EnvironmentError(
                    ".env 管理员配置无效：用户名须为 3—40 位字母/数字/下划线/短横线，密码须为 12—128 位"
                ) from None
            if plain:
                encoded = (
                    admin["password_hash"]
                    if admin and verify_password(plain, admin["password_hash"])
                    else hash_password(plain)
                )
            if not valid_password_hash(encoded):
                raise EnvironmentError(
                    "SOULBOT_WEBUI_PASSWORD_HASH 格式无效；可改填 SOULBOT_WEBUI_PASSWORD 后重启生成"
                )
            self.persist(username, encoded)
            if not admin or (admin["username"], admin["password_hash"]) != (username, encoded):
                db.execute("INSERT OR REPLACE INTO admins VALUES (1,?,?)", (username, encoded))
                db.execute("DELETE FROM sessions")
                db.execute("DELETE FROM auth_attempts")
                db.execute("INSERT INTO audit(action,detail) VALUES ('admin_env_applied','')")

    def persist(self, username, encoded):
        if self.environment:
            self.environment.update({USERNAME: username, PASSWORD_HASH: encoded, PASSWORD: None})

    def account(self):
        with self.db.connect() as db:
            row = db.execute("SELECT username FROM admins WHERE id=1").fetchone()
        return {
            "username": row[0] if row else "",
            "managed_by_process": bool(
                self.environment and self.environment.process_managed_account()
            ),
            "stored_in_env": bool(
                self.environment and self.environment.values().get(PASSWORD_HASH)
            ),
        }

    def has_admin(self):
        with self.db.connect() as db:
            return db.execute("SELECT 1 FROM admins").fetchone() is not None

    def setup(self, username, password):
        hashed = hash_password(password)
        try:
            with self.lock, self.db.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM admins").fetchone():
                    raise HTTPException(409, "管理员已创建，请登录继续配置")
                self.persist(username, hashed)
                db.execute("INSERT INTO admins VALUES (1,?,?)", (username, hashed))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "管理员已创建，请登录继续配置") from None
        self.db.audit("admin_created")

    def login(self, username, password, client):
        with self.lock:
            self.authenticate(username, password, client)
            return self.session()

    def authenticate(self, username, password, client):
        now = time.time()
        with self.db.connect() as db:
            attempt = db.execute("SELECT * FROM auth_attempts WHERE client=?", (client,)).fetchone()
            if attempt and attempt["reset_at"] > now and attempt["count"] >= 10:
                raise HTTPException(429, "登录尝试过多，请15分钟后重试")
            db.execute(
                """INSERT INTO auth_attempts VALUES (?,1,?)
                ON CONFLICT(client) DO UPDATE SET
                count=CASE WHEN reset_at<=? THEN 1 ELSE count+1 END,
                reset_at=CASE WHEN reset_at<=? THEN excluded.reset_at ELSE reset_at END""",
                (client, now + 900, now, now),
            )
            admin = db.execute("SELECT * FROM admins WHERE id=1").fetchone()
        valid = verify_password(password, admin["password_hash"] if admin else self.dummy_hash)
        if (
            not admin
            or not valid
            or not hmac.compare_digest(username.encode(), admin["username"].encode())
        ):
            raise HTTPException(401, "用户名或密码不正确")
        with self.db.connect() as db:
            db.execute("DELETE FROM auth_attempts WHERE client=?", (client,))
        return admin

    def change_account(self, username, password, current_password, client):
        if self.environment and self.environment.process_managed_account():
            raise HTTPException(409, "管理员账户由进程环境变量管理，请修改环境变量并重启")
        with self.lock:
            with self.db.connect() as db:
                admin = db.execute("SELECT * FROM admins WHERE id=1").fetchone()
            if not admin:
                raise HTTPException(409, "请先创建管理员")
            try:
                self.authenticate(admin["username"], current_password, client)
            except HTTPException as exc:
                if exc.status_code == 401:
                    raise HTTPException(403, "当前密码不正确，请重新输入") from None
                raise
            encoded = hash_password(password) if password is not None else admin["password_hash"]
            self.persist(username, encoded)
            with self.db.connect() as db:
                db.execute(
                    "UPDATE admins SET username=?, password_hash=? WHERE id=1", (username, encoded)
                )
                db.execute("DELETE FROM sessions")
                db.execute("INSERT INTO audit(action,detail) VALUES ('admin_account_updated','')")
            return self.session()

    def session(self):
        token = secrets.token_urlsafe(32)
        with self.db.connect() as db:
            db.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
            db.execute(
                "INSERT INTO sessions VALUES (?,?)", (self.digest(token), time.time() + 43200)
            )
        return token

    @staticmethod
    def digest(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def valid(self, token):
        if not token:
            return False
        with self.db.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM sessions WHERE token_hash=? AND expires>?",
                    (self.digest(token), time.time()),
                ).fetchone()
                is not None
            )

    def logout(self, token):
        with self.db.connect() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.digest(token or ""),))
