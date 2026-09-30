import json
import logging
from typing import Any

from grocery_agent.models.common import utc_now


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields: dict[str, Any] = {
            "timestamp": utc_now().isoformat(),
            "level": record.levelname,
            "event": record.getMessage(),
            "logger": record.name,
        }
        fields.update(getattr(record, "fields", {}))
        if record.exc_info:
            fields["traceback"] = self.formatException(record.exc_info)
        return json.dumps(fields, ensure_ascii=False, default=str)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=level, handlers=[handler], force=True)
