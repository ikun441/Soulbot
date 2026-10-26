"""Persistent global emotion simulation with deterministic, bounded transitions."""

import hashlib
import json
import time

from soulbot.core.social_state import StateConflict, observe_once
from soulbot.services.config_service import EmotionValues

STATES = {
    "tired": "疲惫",
    "withdrawn": "低落",
    "excited": "活跃",
    "curious": "好奇",
    "content": "愉快",
    "calm": "平静",
}


def choose_state(values, policy, previous="calm", since=0, now=0, force=False):
    high, low, gap = policy.high_threshold, policy.low_threshold, policy.hysteresis
    gates = {
        "tired": values["fatigue"] >= high or values["energy"] <= low,
        "withdrawn": values["valence"] <= low or values["sociability"] <= low,
        "excited": values["energy"] >= high and values["valence"] >= high - gap,
        "curious": values["curiosity"] >= high,
        "content": values["valence"] >= high,
        "calm": True,
    }
    candidate = next(state for state, matches in gates.items() if matches)
    if force or previous not in STATES:
        return candidate
    if now - since < policy.min_state_seconds:
        return previous
    retained = {
        "tired": values["fatigue"] > high - gap or values["energy"] < low + gap,
        "withdrawn": values["valence"] < low + gap or values["sociability"] < low + gap,
        "excited": values["energy"] > high - gap and values["valence"] > high - 2 * gap,
        "curious": values["curiosity"] > high - gap,
        "content": values["valence"] > high - gap,
        "calm": False,
    }
    order = list(STATES)
    if retained[previous] and order.index(candidate) > order.index(previous):
        return previous
    return candidate


class EmotionEngine:
    def __init__(self, config, db, clock=time.time):
        self.config, self.db, self.clock = config, db, clock

    def _load(self, db, policy, now):
        row = db.execute("SELECT * FROM emotion_state WHERE id=1").fetchone()
        if not row:
            values = policy.baseline.model_dump()
            state = choose_state(values, policy, force=True)
            db.execute(
                "INSERT INTO emotion_state VALUES (1,?,?,?,?,NULL,0,?)",
                (json.dumps(values), state, now, now, self._policy_key(policy)),
            )
            row = db.execute("SELECT * FROM emotion_state WHERE id=1").fetchone()
        result = dict(row)
        result["values"] = json.loads(result.pop("values_json"))
        return result

    @staticmethod
    def _policy_key(policy):
        return hashlib.sha256(policy.model_dump_json().encode()).hexdigest()

    @staticmethod
    def _history(db, data, previous, reason, now):
        db.execute(
            "INSERT INTO emotion_history (created_at,previous_state,state,reason,values_json) "
            "VALUES (?,?,?,?,?)",
            (now, previous, data["state"], reason, json.dumps(data["values"])),
        )
        db.execute(
            "DELETE FROM emotion_history WHERE id NOT IN "
            "(SELECT id FROM emotion_history ORDER BY id DESC LIMIT 512)"
        )

    @staticmethod
    def _save(db, data):
        db.execute(
            "UPDATE emotion_state SET values_json=?,state=?,updated_at=?,state_since=?,"
            "last_interaction_at=?,revision=?,policy_key=? WHERE id=1",
            (
                json.dumps(data["values"]),
                data["state"],
                data["updated_at"],
                data["state_since"],
                data["last_interaction_at"],
                data["revision"],
                data["policy_key"],
            ),
        )

    def _advance(self, db, data, policy, now):
        policy_key = self._policy_key(policy)
        changed = data["policy_key"] != policy_key
        if changed:
            data["updated_at"] = now
            data["policy_key"] = policy_key
            data["revision"] += 1
        if policy.enabled and policy.automatic:
            elapsed = max(0, now - data["updated_at"])
            factor = 2 ** (-elapsed / (policy.recovery_half_life_minutes * 60))
            baseline = policy.baseline.model_dump()
            data["values"] = {
                key: baseline[key] + (value - baseline[key]) * factor
                for key, value in data["values"].items()
            }
            previous = data["state"]
            data["state"] = choose_state(
                data["values"], policy, previous, data["state_since"], now, force=changed
            )
            if previous != data["state"]:
                data["state_since"] = now
                data["revision"] += 1
                self._history(db, data, previous, "recovery", now)
        # Frozen/disabled intervals do not accumulate recovery to apply on re-enable.
        data["updated_at"] = max(now, data["updated_at"])

    def snapshot(self):
        policy, now = self.config.current.emotion, self.clock()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            data = self._load(db, policy, now)
            self._advance(db, data, policy, now)
            self._save(db, data)
        return self._public(data, policy)

    @staticmethod
    def _public(data, policy):
        return {
            **{key: value for key, value in data.items() if key not in {"id", "policy_key"}},
            "state_label": STATES[data["state"]],
            "enabled": policy.enabled,
            "automatic": policy.automatic,
            "values": {key: round(value, 3) for key, value in data["values"].items()},
        }

    def history(self, limit=40):
        with self.db.connect() as db:
            rows = db.execute(
                "SELECT * FROM emotion_history ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        result = []
        for row in reversed(rows):
            item = dict(row)
            item["values"] = json.loads(item.pop("values_json"))
            result.append(item)
        return result

    def edit(self, values, revision, *, reset=False):
        policy, now = self.config.current.emotion, self.clock()
        checked = EmotionValues.model_validate(values).model_dump()
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            data = self._load(db, policy, now)
            if data["revision"] != revision:
                raise StateConflict("情绪已被新的互动或编辑更新，请刷新后再保存")
            previous = data["state"]
            data["values"] = checked
            data["state"] = choose_state(checked, policy, force=True)
            if previous != data["state"]:
                data["state_since"] = now
            data["updated_at"] = now
            data["policy_key"] = self._policy_key(policy)
            data["revision"] += 1
            self._save(db, data)
            self._history(db, data, previous, "reset" if reset else "manual", now)
            db.execute(
                "INSERT INTO audit(action,detail) VALUES ('emotion_edited',?)",
                ("reset" if reset else "manual",),
            )
        return self._public(data, policy)

    def interact(self, event):
        policy, now = self.config.current.emotion, self.clock()
        if not policy.enabled or not policy.automatic:
            return
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            data = self._load(db, policy, now)
            self._advance(db, data, policy, now)
            if not observe_once(db, event, "emotion", now):
                self._save(db, data)
                return
            last = data["last_interaction_at"]
            if last is not None and now - last < policy.interaction_cooldown_seconds:
                self._save(db, data)
                return
            previous = data["state"]
            deltas = {
                "valence": 0.5,
                "energy": -0.5,
                "curiosity": 0.3,
                "sociability": 0.4,
                "fatigue": 1,
            }
            data["values"] = {
                key: max(0, min(100, value + deltas[key])) for key, value in data["values"].items()
            }
            data["state"] = choose_state(data["values"], policy, previous, data["state_since"], now)
            if previous != data["state"]:
                data["state_since"] = now
            data["last_interaction_at"] = now
            data["revision"] += 1
            self._save(db, data)
            self._history(db, data, previous, "interaction", now)
