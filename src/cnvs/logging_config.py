import json
import logging
from datetime import datetime, timezone


_STANDARD_RECORD_FIELDS = frozenset(
    logging.makeLogRecord({}).__dict__
) | {"asctime", "message"}


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": getattr(record, "event", "log"),
        }
        payload.update(
            {
                key: value
                for key, value in record.__dict__.items()
                if key not in _STANDARD_RECORD_FIELDS
            }
        )
        return json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str)


def configure_logging(level: int) -> None:
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        root_logger.addHandler(logging.StreamHandler())
    formatter = JsonLogFormatter()
    for handler in root_logger.handlers:
        handler.setFormatter(formatter)
