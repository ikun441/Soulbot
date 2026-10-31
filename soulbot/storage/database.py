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
            if version > 17:
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
            if version < 3:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS emoji_index (
                        id TEXT PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
                        category TEXT NOT NULL, mtime REAL NOT NULL,
                        tags TEXT NOT NULL DEFAULT '[]', aliases TEXT NOT NULL DEFAULT '[]');
                    CREATE TABLE IF NOT EXISTS media_artifacts (
                        id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL,
                        width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL,
                        source TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL);
                    UPDATE metadata SET value='3' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 4:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    ALTER TABLE media_artifacts ADD COLUMN scope TEXT NOT NULL DEFAULT 'admin';
                    UPDATE metadata SET value='4' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 5:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS audio_artifacts (
                        id TEXT PRIMARY KEY, filename TEXT NOT NULL, mime TEXT NOT NULL,
                        format TEXT NOT NULL, sample_rate INTEGER NOT NULL, channels INTEGER NOT NULL,
                        duration REAL NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
                        source TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
                        scope TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS tts_usage (
                        id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL,
                        model TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
                        chars INTEGER NOT NULL, cost_cny REAL, created_at REAL NOT NULL,
                        error_kind TEXT);
                    CREATE INDEX IF NOT EXISTS tts_usage_day ON tts_usage(day, created_at);
                    UPDATE metadata SET value='5' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 6:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS asr_usage (
                        id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL,
                        model TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
                        seconds REAL NOT NULL, cost_cny REAL, created_at REAL NOT NULL,
                        error_kind TEXT);
                    CREATE INDEX IF NOT EXISTS asr_usage_day ON asr_usage(day,created_at);
                    UPDATE metadata SET value='6' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 7:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS image_usage (
                        id TEXT PRIMARY KEY, day TEXT NOT NULL, provider TEXT NOT NULL,
                        model TEXT NOT NULL, purpose TEXT NOT NULL, status TEXT NOT NULL,
                        images INTEGER NOT NULL, cost_cny REAL, created_at REAL NOT NULL,
                        error_kind TEXT);
                    CREATE INDEX IF NOT EXISTS image_usage_day ON image_usage(day,created_at);
                    UPDATE metadata SET value='7' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 8:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS emotion_state (
                        id INTEGER PRIMARY KEY CHECK(id=1), values_json TEXT NOT NULL,
                        state TEXT NOT NULL, updated_at REAL NOT NULL,
                        state_since REAL NOT NULL, last_interaction_at REAL,
                        revision INTEGER NOT NULL DEFAULT 0, policy_key TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS emotion_history (
                        id INTEGER PRIMARY KEY, created_at REAL NOT NULL,
                        previous_state TEXT NOT NULL, state TEXT NOT NULL,
                        reason TEXT NOT NULL, values_json TEXT NOT NULL);
                    CREATE TABLE IF NOT EXISTS relationships (
                        id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        group_id TEXT NOT NULL DEFAULT '', display_name TEXT NOT NULL DEFAULT '',
                        affinity INTEGER NOT NULL DEFAULT 0 CHECK(affinity BETWEEN -100 AND 100),
                        automatic INTEGER NOT NULL DEFAULT 1,
                        interactions INTEGER NOT NULL DEFAULT 0, last_interaction_at REAL,
                        last_reward_at REAL, reward_day TEXT NOT NULL DEFAULT '',
                        daily_gain INTEGER NOT NULL DEFAULT 0, updated_at REAL NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(bot_id,group_id,user_id));
                    CREATE INDEX IF NOT EXISTS relationships_user ON relationships(user_id,id);
                    CREATE TABLE IF NOT EXISTS social_observations (
                        key TEXT PRIMARY KEY, created_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS social_observations_time
                        ON social_observations(created_at);
                    UPDATE metadata SET value='8' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 9:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS memories (
                        id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        group_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL, content TEXT NOT NULL, source TEXT NOT NULL,
                        importance INTEGER NOT NULL CHECK(importance BETWEEN 1 AND 5),
                        confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                        long_term_allowed INTEGER NOT NULL DEFAULT 0,
                        status TEXT NOT NULL CHECK(status IN ('candidate','approved','rejected','archived')),
                        fingerprint TEXT NOT NULL UNIQUE, expires_at REAL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS memory_scope ON memories(bot_id,group_id,user_id,status);
                    CREATE TABLE IF NOT EXISTS memory_preferences (
                        scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
                    UPDATE metadata SET value='9' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 10:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS proactive_contacts (
                        id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        opted_in INTEGER NOT NULL DEFAULT 0, user_paused INTEGER NOT NULL DEFAULT 0,
                        admin_blocked INTEGER NOT NULL DEFAULT 0,
                        quiet_start TEXT NOT NULL DEFAULT '00:00', quiet_end TEXT NOT NULL DEFAULT '08:00',
                        authorized_at REAL, revoked_at REAL, updated_at REAL NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 0, UNIQUE(bot_id,user_id));
                    CREATE TABLE IF NOT EXISTS proactive_jobs (
                        id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
                        bot_id TEXT NOT NULL, user_id TEXT NOT NULL, content TEXT NOT NULL,
                        earliest_at REAL NOT NULL, latest_at REAL NOT NULL, run_at REAL NOT NULL,
                        status TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL, reviewed_at REAL, approved_by INTEGER,
                        next_check_at REAL NOT NULL DEFAULT 0, lease_until REAL,
                        revision INTEGER NOT NULL DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS proactive_jobs_due
                        ON proactive_jobs(status,next_check_at,run_at);
                    CREATE TABLE IF NOT EXISTS proactive_usage (
                        job_id TEXT PRIMARY KEY, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        day TEXT NOT NULL, created_at REAL NOT NULL, status TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS proactive_usage_scope
                        ON proactive_usage(bot_id,user_id,day,created_at);
                    CREATE TABLE IF NOT EXISTS proactive_job_events (
                        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, status TEXT NOT NULL,
                        reason TEXT NOT NULL, created_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS proactive_job_history ON proactive_job_events(job_id,id);
                    UPDATE metadata SET value='10' WHERE key='schema_version';
                    COMMIT;
                """)
            if version < 11:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS diary_events (
                        id TEXT PRIMARY KEY, scope TEXT NOT NULL, bot_id TEXT NOT NULL,
                        user_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL CHECK(kind IN ('chat','proactive')),
                        has_text INTEGER NOT NULL, has_media INTEGER NOT NULL,
                        occurred_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS diary_events_day ON diary_events(occurred_at,scope);
                    CREATE TABLE IF NOT EXISTS diary_preferences (
                        scope TEXT PRIMARY KEY, enabled INTEGER NOT NULL);
                    CREATE TABLE IF NOT EXISTS diaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL, bot_id TEXT NOT NULL,
                        user_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
                        day TEXT NOT NULL, timezone TEXT NOT NULL,
                        starts_at REAL NOT NULL, ends_at REAL NOT NULL,
                        title TEXT NOT NULL, facts_json TEXT NOT NULL, role_text TEXT NOT NULL,
                        role_source TEXT NOT NULL, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
                        UNIQUE(scope,day));
                    CREATE INDEX IF NOT EXISTS diaries_day ON diaries(day,id);
                    CREATE TABLE IF NOT EXISTS diary_runs (
                        day TEXT PRIMARY KEY, timezone TEXT NOT NULL, completed_at REAL NOT NULL,
                        created_count INTEGER NOT NULL, skipped_scopes INTEGER NOT NULL);
                    UPDATE metadata SET value='11' WHERE key='schema_version';
                    COMMIT;
                """)

            if version < 12:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS qzone_drafts (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        request_id TEXT UNIQUE NOT NULL, input_hash TEXT NOT NULL,
                        bot_id TEXT NOT NULL, content TEXT NOT NULL, audience TEXT NOT NULL,
                        source TEXT NOT NULL, status TEXT NOT NULL, reason TEXT NOT NULL,
                        created_at REAL NOT NULL, updated_at REAL NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 0,
                        approved_hash TEXT, approved_owner TEXT, approved_at REAL,
                        lease_until REAL, tid TEXT);
                    CREATE INDEX IF NOT EXISTS qzone_drafts_status ON qzone_drafts(status,id);
                    CREATE TABLE IF NOT EXISTS qzone_attempts (
                        draft_id INTEGER PRIMARY KEY, bot_id TEXT NOT NULL,
                        created_at REAL NOT NULL, status TEXT NOT NULL);
                    CREATE INDEX IF NOT EXISTS qzone_attempts_time ON qzone_attempts(created_at);
                    CREATE TABLE IF NOT EXISTS qzone_events (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, draft_id INTEGER NOT NULL,
                        status TEXT NOT NULL, reason TEXT NOT NULL, created_at REAL NOT NULL);
                    UPDATE metadata SET value='12' WHERE key='schema_version';
                    COMMIT;
                """)

            if version < 13:
                final_column = (
                    ""
                    if "is_final" in {r[1] for r in db.execute("PRAGMA table_info(diaries)")}
                    else "ALTER TABLE diaries ADD COLUMN is_final INTEGER NOT NULL DEFAULT 1;"
                )
                db.executescript(
                    """
                    BEGIN IMMEDIATE;
                    ALTER TABLE diary_events RENAME TO diary_events_old;
                    CREATE TABLE diary_events (
                        id TEXT PRIMARY KEY, scope TEXT NOT NULL, bot_id TEXT NOT NULL,
                        user_id TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '',
                        kind TEXT NOT NULL CHECK(kind IN ('chat','proactive','qzone')),
                        has_text INTEGER NOT NULL, has_media INTEGER NOT NULL,
                        occurred_at REAL NOT NULL);
                    INSERT INTO diary_events SELECT * FROM diary_events_old;
                    DROP TABLE diary_events_old;
                    CREATE INDEX diary_events_day ON diary_events(occurred_at,scope);
                """
                    + final_column
                    + """
                    CREATE TABLE IF NOT EXISTS diary_qzone_seen (draft_id INTEGER PRIMARY KEY, captured_at REAL NOT NULL);
                    UPDATE metadata SET value='13' WHERE key='schema_version';
                    COMMIT;
                """
                )

            if version < 14:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS search_usage (
                        id TEXT PRIMARY KEY, day TEXT NOT NULL, purpose TEXT NOT NULL,
                        status TEXT NOT NULL, cost_cny REAL, created_at REAL NOT NULL);
                    CREATE INDEX IF NOT EXISTS search_usage_time ON search_usage(day,created_at);
                    CREATE TABLE IF NOT EXISTS exploration_notes (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, topic TEXT NOT NULL,
                        results_json TEXT NOT NULL, created_at REAL NOT NULL);
                    CREATE TABLE IF NOT EXISTS diary_summaries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT, scope TEXT NOT NULL,
                        week_start TEXT NOT NULL, week_end TEXT NOT NULL, timezone TEXT NOT NULL,
                        source_ids TEXT NOT NULL, facts_json TEXT NOT NULL,
                        role_text TEXT NOT NULL, created_at REAL NOT NULL,
                        UNIQUE(scope,week_start));
                    CREATE TABLE IF NOT EXISTS diary_polish_runs (
                        request_id TEXT PRIMARY KEY, diary_id INTEGER NOT NULL,
                        revision INTEGER NOT NULL, status TEXT NOT NULL,
                        created_at REAL NOT NULL, UNIQUE(diary_id,revision));
                    CREATE TABLE IF NOT EXISTS diary_deliveries (
                        kind TEXT NOT NULL, period TEXT NOT NULL, bot_id TEXT NOT NULL,
                        user_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                        PRIMARY KEY(kind,period,bot_id,user_id));
                    UPDATE metadata SET value='14' WHERE key='schema_version';
                    COMMIT;
                """)

            if version < 15:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS proactive_draft_runs (
                        day TEXT NOT NULL, bot_id TEXT NOT NULL, user_id TEXT NOT NULL,
                        status TEXT NOT NULL, created_at REAL NOT NULL,
                        PRIMARY KEY(day,bot_id,user_id));
                    CREATE TABLE IF NOT EXISTS tool_approvals (
                        id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, name TEXT NOT NULL,
                        arguments_json TEXT NOT NULL, context_json TEXT NOT NULL, owner TEXT NOT NULL,
                        status TEXT NOT NULL, result_json TEXT, created_at REAL NOT NULL,
                        updated_at REAL NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                    CREATE INDEX IF NOT EXISTS tool_approvals_state ON tool_approvals(status,created_at);
                    CREATE TABLE IF NOT EXISTS diary_summary_deleted (
                        scope TEXT NOT NULL, week_start TEXT NOT NULL, PRIMARY KEY(scope,week_start));
                    UPDATE metadata SET value='15' WHERE key='schema_version';
                    COMMIT;
                """)

            if version < 16:
                additions = {
                    "memories": {
                        "proposed_target_id": "INTEGER",
                        "proposed_target_revision": "INTEGER",
                        "evidence": "TEXT NOT NULL DEFAULT ''",
                    },
                    "relationships": {
                        "daily_loss": "INTEGER NOT NULL DEFAULT 0",
                        "last_feedback_at": "REAL",
                    },
                    "exploration_notes": {
                        "synthesis_json": "TEXT NOT NULL DEFAULT '{}'",
                        "reviewed": "INTEGER NOT NULL DEFAULT 0",
                    },
                }
                changes = []
                for table, fields in additions.items():
                    present = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
                    changes.extend(
                        f"ALTER TABLE {table} ADD COLUMN {name} {kind};"
                        for name, kind in fields.items()
                        if name not in present
                    )
                db.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + "\n".join(changes)
                    + """
                    CREATE TABLE IF NOT EXISTS reflection_jobs (
                        id TEXT PRIMARY KEY, scope TEXT NOT NULL, bot_id TEXT NOT NULL,
                        user_id TEXT NOT NULL, group_id TEXT NOT NULL,
                        message_id TEXT NOT NULL, input_text TEXT NOT NULL,
                        status TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
                        started_at REAL, day TEXT, result_json TEXT NOT NULL DEFAULT '{}');
                    CREATE INDEX IF NOT EXISTS reflection_due ON reflection_jobs(status,created_at);
                    CREATE TABLE IF NOT EXISTS social_feedback (
                        event_key TEXT NOT NULL, kind TEXT NOT NULL, scope TEXT NOT NULL,
                        signal TEXT NOT NULL, delta REAL NOT NULL, created_at REAL NOT NULL,
                        PRIMARY KEY(event_key,kind));
                    UPDATE metadata SET value='16' WHERE key='schema_version';
                    COMMIT;
                """
                )

            if version < 17:
                db.executescript("""
                    BEGIN IMMEDIATE;
                    CREATE TABLE IF NOT EXISTS plugin_settings (
                        id TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
                        values_json TEXT NOT NULL DEFAULT '{}', policy TEXT NOT NULL DEFAULT 'approval',
                        approved_hash TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0);
                    UPDATE metadata SET value='17' WHERE key='schema_version';
                    COMMIT;
                """)
