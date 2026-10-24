import hashlib
import hmac
import secrets
import sqlite3
import time

from fastapi import HTTPException

ITERATIONS = 600000


def hash_password(password):
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), ITERATIONS).hex()
    return f"{ITERATIONS}${salt}${digest}"


def verify_password(password, stored):
    count, salt, expected = stored.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(count)).hex()
    return hmac.compare_digest(actual, expected)


class AuthService:
    def __init__(self, database):
        self.db = database
        self.dummy_hash = hash_password(secrets.token_hex(32))

    def has_admin(self):
        with self.db.connect() as db:
            return db.execute("SELECT 1 FROM admins").fetchone() is not None

    def setup(self, username, password):
        hashed = hash_password(password)
        try:
            with self.db.connect() as db:
                db.execute("INSERT INTO admins VALUES (1,?,?)", (username, hashed))
        except sqlite3.IntegrityError:
            raise HTTPException(409, "管理员已创建，请登录继续配置") from None
        self.db.audit("admin_created")

    def login(self, username, password, client):
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
