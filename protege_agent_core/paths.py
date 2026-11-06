"""Agent-scoped persistence path builders."""

import re
from pathlib import Path


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_segment(value: str, name: str) -> str:
    if not _SAFE_SEGMENT.fullmatch(value):
        raise ValueError(f"{name} must contain only letters, digits, underscores, or hyphens")
    return value


def session_path(
    agent_id: str,
    user_id: str,
    conversation_id: str,
    storage_root: str | Path = ".",
) -> Path:
    """Build a path for an agent-scoped JSON session."""
    agent_id = _validate_segment(agent_id, "agent_id")
    user_id = _validate_segment(user_id, "user_id")
    conversation_id = _validate_segment(conversation_id, "conversation_id")
    return Path(storage_root) / "sessions" / agent_id / user_id / f"{conversation_id}.json"


def conversation_path(
    agent_id: str,
    user_id: str,
    conversation_id: str,
    storage_root: str | Path = ".",
) -> Path:
    """Build a path for an agent-scoped conversation JSON file."""
    agent_id = _validate_segment(agent_id, "agent_id")
    user_id = _validate_segment(user_id, "user_id")
    conversation_id = _validate_segment(conversation_id, "conversation_id")
    return Path(storage_root) / "conversations" / agent_id / user_id / f"{conversation_id}.json"