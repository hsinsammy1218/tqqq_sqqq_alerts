from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any


def format_utc_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def setup_logger(level_name: str) -> logging.Logger:
    level = getattr(logging, level_name.upper(), logging.INFO)
    logs_dir = Path("logs")
    logs_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("tqqq_alert_bot")
    logger.setLevel(level)
    logger.propagate = False
    if logger.handlers:
        logger.handlers.clear()

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:  # noqa: D401
            payload: dict[str, Any] = {
                "timestamp": format_utc_z(datetime.now(timezone.utc)),
                "level": record.levelname,
                "message": record.getMessage(),
            }
            extra = getattr(record, "event", None)
            if isinstance(extra, dict):
                payload.update(extra)
            return json.dumps(payload, ensure_ascii=True)

    file_handler = TimedRotatingFileHandler(
        filename=logs_dir / "bot.log",
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
        utc=True,
    )
    file_handler.setFormatter(JsonFormatter())
    logger.addHandler(file_handler)
    return logger


def log_event(logger: logging.Logger, level: int, message: str, **event: Any) -> None:
    logger.log(level, message, extra={"event": event})
