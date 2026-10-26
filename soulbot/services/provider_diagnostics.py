"""In-memory provider test state, bound to a non-reversible settings fingerprint."""

import hashlib
import json
from datetime import datetime, timezone


class ProviderDiagnostics:
    def __init__(self):
        self.records = {}

    @staticmethod
    def fingerprint(settings, secret=None):
        payload = {
            "settings": settings.model_dump(mode="json")
            if hasattr(settings, "model_dump")
            else settings,
            "secret": secret,
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def record(self, name, settings, secret=None, *, ok, error_kind=None):
        self.records[name] = {
            "fingerprint": self.fingerprint(settings, secret),
            "tested_at": datetime.now(timezone.utc).isoformat(),
            "ok": ok,
            "error_kind": error_kind,
        }

    def status(self, name, settings, secret=None, *, enabled, configured):
        if not enabled:
            self.records.pop(name, None)
            state = "disabled"
        elif not configured:
            self.records.pop(name, None)
            state = "unconfigured"
        else:
            current = self.records.get(name)
            if current and current["fingerprint"] != self.fingerprint(settings, secret):
                self.records.pop(name, None)
                current = None
            if not current:
                state = "not_tested"
            else:
                state = "available" if current["ok"] else "failed"
        current = self.records.get(name) if state in {"available", "failed"} else None
        return {
            "state": state,
            "last_test_at": current["tested_at"] if current else None,
            "last_error_kind": current["error_kind"] if current else None,
        }
