from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    platform: str
    chat_id: int
    msg_id: str
    text: str
    date: float
    chat_title: str = ""
    chat_link: str = ""
    msg_link: str = ""
    author_id: int | None = None
    author_name: str = ""
    author_username: str = ""
    author_link: str = ""
    reply_ctx: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    is_lead: bool
    confidence: int = 0
    work_type: str = ""
    subject: str = ""
    university: str = ""
    deadline: str = ""
    budget: str = ""
    summary: str = ""
    reply: str = ""
    source: str = "ai"

    def as_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)
