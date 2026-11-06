import json
import logging
import queue
import threading
import time

import httpx
from dotenv import load_dotenv

from .utils import env_flag, env_int, resolve_secret, utc_now

load_dotenv()

logger = logging.getLogger(__name__)
logger.propagate = False

SUMO_HTTP_SOURCE_URL = resolve_secret(None, "SUMO_HTTP_SOURCE_URL")
ENABLE_SUMOLOGIC = env_flag("ENABLE_SUMOLOGIC", False)
SUMO_SOURCE_CATEGORY = resolve_secret(None, "SUMO_SOURCE_CATEGORY")
SUMO_SOURCE_NAME = resolve_secret(None, "SUMO_SOURCE_NAME")
SUMO_BATCH_SIZE = env_int("SUMO_BATCH_SIZE", 50)
SUMO_FLUSH_INTERVAL_SECONDS = env_int("SUMO_FLUSH_INTERVAL_SECONDS", 2)


class SumoLogicPublisher:
    def __init__(self, max_queue_size: int = 10_000):
        self._items: queue.Queue[str | None] = queue.Queue(maxsize=max_queue_size)
        self._stop = threading.Event()
        headers = {"Content-Type": "application/json"}
        if SUMO_SOURCE_CATEGORY:
            headers["X-Sumo-Category"] = SUMO_SOURCE_CATEGORY
        if SUMO_SOURCE_NAME:
            headers["X-Sumo-Name"] = SUMO_SOURCE_NAME
        self._client = httpx.Client(timeout=10, headers=headers)
        self._thread = threading.Thread(
            target=self._run,
            name="sumologic-publisher",
            daemon=True,
        )
        self._thread.start()

    def enqueue(self, line: str) -> bool:
        try:
            self._items.put_nowait(line)
            return True
        except queue.Full:
            logger.warning("Sumo Logic publish queue is full; dropping log line")
            return False

    def close(self) -> None:
        self._stop.set()

        try:
            self._items.put_nowait(None)
        except queue.Full:
            pass

        self._thread.join(timeout=10)
        if self._thread.is_alive():
            logger.warning("Sumo Logic publisher thread did not stop within timeout")
        self._client.close()

    def _run(self) -> None:
        batch: list[str] = []
        deadline = time.monotonic() + SUMO_FLUSH_INTERVAL_SECONDS

        while not self._stop.is_set():
            remaining = max(0.0, deadline - time.monotonic())
            try:
                item = self._items.get(timeout=remaining or SUMO_FLUSH_INTERVAL_SECONDS)
            except queue.Empty:
                item = "__timeout__"

            if item is None:
                self._flush(batch)
                batch = []
                break

            if item != "__timeout__":
                batch.append(item)

            if len(batch) >= SUMO_BATCH_SIZE or time.monotonic() >= deadline:
                self._flush(batch)
                batch = []
                deadline = time.monotonic() + SUMO_FLUSH_INTERVAL_SECONDS

        self._flush(batch)

    def _flush(self, batch: list[str]) -> None:
        if not batch:
            return

        try:
            response = self._client.post(SUMO_HTTP_SOURCE_URL, content="\n".join(batch))
            response.raise_for_status()
        except Exception:
            logger.warning("Sumo Logic batch send failed; dropping %d line(s)", len(batch))


_publisher: SumoLogicPublisher | None = None
_publisher_lock = threading.Lock()


def start_publisher() -> SumoLogicPublisher | None:
    global _publisher

    if not ENABLE_SUMOLOGIC or not SUMO_HTTP_SOURCE_URL:
        return None

    if _publisher is None:
        with _publisher_lock:
            if _publisher is None:
                _publisher = SumoLogicPublisher()

    return _publisher


def stop_publisher() -> None:
    global _publisher

    if _publisher is not None:
        _publisher.close()
        _publisher = None


class SumoLogicHandler(logging.Handler):
    enabled: bool = False

    def __init__(self, level=0):
        super().__init__(level)
        self.enabled = ENABLE_SUMOLOGIC and bool(SUMO_HTTP_SOURCE_URL)

    def emit(self, record: logging.LogRecord) -> None:
        if not self.enabled:
            return

        publisher = start_publisher()
        if publisher is None:
            return

        try:
            self.format(record=record)
            line = json.dumps(
                {
                    "message": record.message,
                    "level": record.levelname,
                    "logger": record.name,
                    "DateTime": utc_now(),
                }
            )
            publisher.enqueue(line)
        except Exception:
            logger.warning("Failed to enqueue log record for Sumo Logic")
