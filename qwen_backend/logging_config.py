"""
Central logging configuration for the backend.

This file is intentionally not named logging.py because that would shadow
Python's standard-library logging module.
"""
from __future__ import annotations

import logging
import logging.config
import os

from dotenv import load_dotenv

load_dotenv()


def configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()

    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "standard": {
                "format": "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            }
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "formatter": "standard",
                "level": level,
            }
        },
        "root": {
            "handlers": ["console"],
            "level": level,
        },
    })


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
