"""Merge conversation relationships into one record per bot and QQ account."""


def merge_global_relationships(db):
    db.execute("BEGIN IMMEDIATE")
    # Retain the exact old rows for inspection; full rollback uses the instance backup.
    db.execute(
        "CREATE TABLE IF NOT EXISTS relationships_before_global AS SELECT * FROM relationships"
    )
    pairs = db.execute("SELECT DISTINCT bot_id,user_id FROM relationships").fetchall()
    for bot_id, user_id in pairs:
        rows = db.execute(
            "SELECT * FROM relationships WHERE bot_id=? AND user_id=? ORDER BY updated_at DESC,revision DESC,id DESC",
            (bot_id, user_id),
        ).fetchall()
        latest = rows[0]
        # A manually frozen score takes precedence. Never sum affinity across conversations.
        chosen = next((r for r in rows if not r["automatic"]), latest)
        alias = next((r["display_name"] for r in rows if r["display_name"]), "")
        day = max(r["reward_day"] for r in rows)
        daily = [r for r in rows if r["reward_day"] == day]

        def most_recent(column):
            return max((r[column] for r in rows if r[column] is not None), default=None)

        db.execute(
            "DELETE FROM relationships WHERE bot_id=? AND user_id=? AND id<>?",
            (bot_id, user_id, latest["id"]),
        )
        db.execute(
            "UPDATE relationships SET group_id='',display_name=?,affinity=?,automatic=?,"
            "interactions=?,last_interaction_at=?,last_reward_at=?,reward_day=?,daily_gain=?,"
            "daily_loss=?,last_feedback_at=?,revision=? WHERE id=?",
            (
                alias,
                chosen["affinity"],
                int(all(r["automatic"] for r in rows)),
                sum(r["interactions"] for r in rows),
                most_recent("last_interaction_at"),
                most_recent("last_reward_at"),
                day,
                sum(r["daily_gain"] for r in daily),
                sum(r["daily_loss"] for r in daily),
                most_recent("last_feedback_at"),
                max(r["revision"] for r in rows) + int(len(rows) > 1 or bool(latest["group_id"])),
                latest["id"],
            ),
        )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS relationships_global_user ON relationships(bot_id,user_id)"
    )
    db.execute("UPDATE metadata SET value='19' WHERE key='schema_version'")
