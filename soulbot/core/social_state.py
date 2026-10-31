"""Shared transaction helpers for simulated social state; no message content is stored."""

import hashlib


class StateConflict(ValueError):
    """A newer interaction or administrator edit superseded this revision."""


def observe_once(db, event, kind, now):
    db.execute("DELETE FROM social_observations WHERE created_at<?", (now - 172800,))
    key = hashlib.sha256(f"{kind}:{event.key}".encode()).hexdigest()
    return bool(
        db.execute("INSERT OR IGNORE INTO social_observations VALUES (?,?)", (key, now)).rowcount
    )


def feedback_job_valid(db, event, job_id, now):
    if job_id is None:
        return True
    return (
        db.execute(
            "SELECT 1 FROM reflection_jobs WHERE id=? AND scope=? AND status='running' AND expires_at>?",
            (job_id, event.scope, now),
        ).fetchone()
        is not None
    )
