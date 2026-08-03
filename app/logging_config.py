import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(level: str, filename: str) -> None:
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s event=%(message)s"
    )
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if filename:
        path = Path(filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(path, maxBytes=5_000_000, backupCount=5))
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=getattr(logging, level.upper(), logging.INFO), handlers=handlers)
