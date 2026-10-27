from enum import StrEnum


class Permission(StrEnum):
    DENY = "deny"
    AUTO = "auto"
    APPROVAL = "approval"


class PermissionEngine:
    """Missing policies deny; approval grants are supplied only by trusted executors."""

    def __init__(self, policies=None):
        self.policies = policies or {}

    def require(self, name, *, approved=False):
        policy = self.policies.get(name, Permission.DENY)
        if policy != Permission.AUTO and not (policy == Permission.APPROVAL and approved is True):
            raise PermissionError("操作被禁止" if policy == Permission.DENY else "操作需要主人审批")
