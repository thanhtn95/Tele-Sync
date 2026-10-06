"""Human-readable text for Telegram service messages (joins, pins, title changes...)."""
from __future__ import annotations

from typing import Any

_SIMPLE = {
    "MessageActionChatCreate": "created the group",
    "MessageActionChannelCreate": "created the channel",
    "MessageActionChatDeletePhoto": "removed the group photo",
    "MessageActionChatEditPhoto": "changed the group photo",
    "MessageActionChatJoinedByLink": "joined via invite link",
    "MessageActionChatJoinedByRequest": "was accepted to the group",
    "MessageActionPinMessage": "pinned a message",
    "MessageActionHistoryClear": "cleared history",
    "MessageActionChatMigrateTo": "upgraded the group to a supergroup",
    "MessageActionChannelMigrateFrom": "group was upgraded to a supergroup",
    "MessageActionScreenshotTaken": "took a screenshot",
    "MessageActionContactSignUp": "joined Telegram",
    "MessageActionGroupCall": "video chat",
    "MessageActionSetMessagesTTL": "changed the auto-delete timer",
    "MessageActionTopicCreate": "created a topic",
}


def service_text(action: dict[str, Any], actor: str | None) -> str:
    kind = action.get("_", "")
    who = actor or "Someone"
    if kind == "MessageActionChatEditTitle":
        return f'{who} changed the name to "{action.get("title", "")}"'
    if kind == "MessageActionChatAddUser":
        return f"{who} added {len(action.get('users') or [])} member(s)" if action.get("users") else f"{who} joined"
    if kind == "MessageActionChatDeleteUser":
        return f"{who} removed a member"
    if kind == "MessageActionPhoneCall":
        dur = action.get("duration")
        return f"Call ({dur}s)" if dur else "Call"
    if kind in _SIMPLE:
        return f"{who} {_SIMPLE[kind]}"
    # Fallback: "MessageActionFooBar" -> "foo bar"
    words = "".join(f" {c.lower()}" if c.isupper() else c for c in kind.removeprefix("MessageAction"))
    return f"{who}:{words}" if words else who
