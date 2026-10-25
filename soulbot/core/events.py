from dataclasses import dataclass


@dataclass(frozen=True)
class ChatEvent:
    bot_id: str
    user_id: str
    message_id: str
    text: str
    group_id: str | None = None
    directed: bool = False
    reply_target: str = "none"
    mentions_other: bool = False
    is_bot: bool = False
    role: str = "member"

    @property
    def scope(self):
        return (
            f"{self.bot_id}:g:{self.group_id}:u:{self.user_id}"
            if self.group_id
            else f"{self.bot_id}:p:{self.user_id}"
        )

    @property
    def group_scope(self):
        return f"{self.bot_id}:g:{self.group_id}" if self.group_id else self.scope

    @property
    def key(self):
        return f"{self.scope}:{self.message_id}"
