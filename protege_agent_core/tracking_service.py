import json
import logging
import os
import queue
import threading
from dataclasses import dataclass
from typing import Any

import pika
from dotenv import load_dotenv

from .utils import get_queue_connection_string

load_dotenv()

logger = logging.getLogger(__name__)
logger.setLevel(logging.ERROR)
AGENT_EXCHANGE = os.getenv("RABBIT_EXCHANGE", "")
AGENT_APP_KEY= os.getenv("AGENT_APP_KEY", "Agent")
AGENT_CATEGORY = os.getenv("AGENT_CATEGORY", "AI Agent")

@dataclass
class PublishItem:
    message: dict[str, Any]
    exchange: str


class RabbitPublisher:
    def __init__(self, max_queue_size: int = 10_000):
        if not AGENT_EXCHANGE:
            logger.info("No exchange configured, in prod you should configure RABBIT_EXCHANGE for tracking ")
            return

        self._items: queue.Queue[PublishItem | None] = queue.Queue(
            maxsize=max_queue_size
        )
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name="rabbit-publisher",
            daemon=True,
        )
        self._thread.start()

    def publish(
        self,
        message: dict[str, Any],
        custom_exchange: str | None = None,
    ) -> bool:
        if not AGENT_EXCHANGE:
            logger.info("No exchange configured, skipping this")
            return
        
        if message:
            if "AppKey" not in message:
                message["AppKey"] = AGENT_APP_KEY
            if "Category" not in message:
                message["Category"] = AGENT_CATEGORY
        item = PublishItem(
            message=message,
            exchange=custom_exchange or AGENT_EXCHANGE,
        )

        try:
            self._items.put_nowait(item)
            logger.info("RabbitMQ message queued exchange=%s", item.exchange)
            return True
        except queue.Full:
            logger.warning("RabbitMQ publish queue is full; dropping telemetry")
            return False

    def close(self) -> None:
        if not AGENT_EXCHANGE:
            logger.info("No exchange configured, skipping this")
            return
        
        self._stop.set()

        try:
            self._items.put_nowait(None)
        except queue.Full:
            pass

        self._thread.join(timeout=10)
        if self._thread.is_alive():
            logger.warning("RabbitMQ publisher thread did not stop within timeout")

    def _run(self) -> None:
        connection = None
        channel = None

        try:
            if not AGENT_EXCHANGE:
                logger.info("No exchanged configured. Enable for prod")
                return
            while not self._stop.is_set():
                item = self._items.get()

                if item is None:
                    break

                while not self._stop.is_set():
                    try:
                        if connection is None or connection.is_closed:
                            connection = self._connect()
                            channel = connection.channel()
                            logger.info("RabbitMQ publisher connected")

                        channel.basic_publish(
                            exchange=item.exchange,
                            routing_key="",
                            body=json.dumps(item.message),
                            properties=pika.BasicProperties(
                                delivery_mode=2
                            ),
                        )
                        
                        break

                    except Exception:
                        self._close_connection(connection)
                        connection = None
                        channel = None
                        if self._stop.wait(timeout=1):
                            break

        finally:
            self._close_connection(connection)

    def _connect(self):
        if not AGENT_EXCHANGE:
            logger.info("No exchange configured, skipping this")
            return
        
        parts = self._connection_parts()
        logger.info(
            "Connecting to RabbitMQ host=%s exchange=%s",
            parts.get("host", ""),
            AGENT_EXCHANGE,
        )

        connection_parameters = {
            "host": parts["host"],
            "virtual_host": parts.get("virtualHost", "/"),
            "heartbeat": 60,
            "blocked_connection_timeout": 30,
        }
        if parts.get("username"):
            connection_parameters["credentials"] = pika.PlainCredentials(
                parts["username"],
                parts.get("password", ""),
            )

        return pika.BlockingConnection(
            pika.ConnectionParameters(**connection_parameters)
        )

    def _connection_parts(self) -> dict[str, str]:
        connection_string = get_queue_connection_string()
        return dict(
            item.split("=", 1)
            for item in connection_string.split(";")
            if "=" in item
        )

    def _close_connection(self, connection) -> None:
        try:
            if connection is not None and connection.is_open:
                connection.close()
        except Exception:
            logger.exception("RabbitMQ connection close failed")


_publisher: RabbitPublisher | None = None
_publisher_lock = threading.Lock()


def start_publisher() -> RabbitPublisher:
    global _publisher

    if _publisher is None:
        with _publisher_lock:
            if _publisher is None:
                _publisher = RabbitPublisher()

    return _publisher


def stop_publisher() -> None:
    global _publisher

    if _publisher is not None:
        _publisher.close()
        _publisher = None


def publish_message(
    message: dict[str, Any],
    custom_exchange: str | None = None,
) -> bool:
    publisher = start_publisher()
    published = publisher.publish(message, custom_exchange)
    if not published:
        logger.warning("RabbitMQ publisher queue is full")
        
    return published