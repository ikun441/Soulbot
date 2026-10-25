import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Database:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1');
                INSERT OR IGNORE INTO metadata VALUES ('wizard_step', '0');
                INSERT OR IGNORE INTO metadata VALUES ('initialized', 'false');
                CREATE TABLE IF NOT EXISTS admins (
                    id INTEGER PRIMARY KEY CHECK(id=1), username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS auth_attempts (
                    client TEXT PRIMARY KEY, count INTEGER NOT NULL, reset_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    action TEXT NOT NULL, detail TEXT NOT NULL);
            """)
        self.migrate()
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return row[0] if row else None

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (key, str(value)))

    def audit(self, action, detail=""):
        with self.connect() as db:
            db.execute("INSERT INTO audit(action,detail) VALUES (?,?)", (action, detail))

    def migrate(self):
        with self.connect() as db:
            version = int(
                db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
            )
            if version > 2:
                raise ValueError("数据库来自更高版本，请升级 SoulBot 后再启动")
            if version < 2:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS llm_usage (
                        id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL,
                        model TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
                        input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,
                        reasoning_tokens INTEGER, estimated INTEGER NOT NULL DEFAULT 1,
                        cost_cny REAL, created_at REAL NOT NULL, error_kind TEXT);
                    CREATE INDEX IF NOT EXISTS usage_day ON llm_usage(day);
                    CREATE TABLE IF NOT EXISTS chat_context (
                        id INTEGER PRIMARY KEY, scope TEXT NOT NULL, role TEXT NOT NULL,
                        content TEXT NOT NULL, expires REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS context_scope ON chat_context(scope, id);
                    CREATE TABLE IF NOT EXISTS seen_events (
                        key TEXT PRIMARY KEY, expires REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS conversation_state (
                        scope TEXT PRIMARY KEY, last_reply REAL NOT NULL DEFAULT 0);
                    CREATE TABLE IF NOT EXISTS group_state (
                        scope TEXT PRIMARY KEY, last_call REAL NOT NULL DEFAULT 0,
                        last_proactive REAL NOT NULL DEFAULT 0, day TEXT NOT NULL DEFAULT '',
                        proactive_count INTEGER NOT NULL DEFAULT 0,
                        unanswered INTEGER NOT NULL DEFAULT 0, pause_until REAL NOT NULL DEFAULT 0,
                        muted_until REAL NOT NULL DEFAULT 0);
                    UPDATE metadata SET value='2' WHERE key='schema_version';
                    COMMIT;
                """)
