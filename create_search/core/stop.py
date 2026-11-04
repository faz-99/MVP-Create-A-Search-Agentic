"""Stop requests, file-based as in agentv4: PUT /stop touches a file the loop polls."""

from pathlib import Path

from create_search.config import STORAGE_PATH


def stop_key(user_id: str, run_id: str) -> Path:
    return Path(STORAGE_PATH) / "stops" / user_id / run_id / ".stop"


def request_stop(key: Path) -> None:
    key.parent.mkdir(parents=True, exist_ok=True)
    key.touch(exist_ok=True)


def is_stop_requested(key: Path | None) -> bool:
    return key is not None and key.exists()


def clear_stop(key: Path | None) -> None:
    if key is not None and key.exists():
        key.unlink()
