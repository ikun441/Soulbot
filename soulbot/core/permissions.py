from enum import StrEnum


class Permission(StrEnum):
    DENY = "deny"
    AUTO = "auto"
    APPROVAL = "approval"


class PermissionEngine:
    """Missing policy and pending approvals always deny execution."""

    def __init__(self, policies=None):
        self.policies = policies or {}

    def require(self, name):
        policy = self.policies.get(name, Permission.DENY)
        if policy != Permission.AUTO:
            raise PermissionError("操作被禁止" if policy == Permission.DENY else "操作需要主人审批")
