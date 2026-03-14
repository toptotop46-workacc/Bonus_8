#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Логгер по стандарту CLAUDE.md:
  YYYY-MM-DD HH:MM:SS | LEVEL   | Message
Цвета: INFO=белый, WARNING=оранжевый, SUCCESS=зелёный, ERROR=красный.
"""

from __future__ import annotations

import sys
from datetime import datetime

RESET = "\033[0m"
COLORS = {
    "INFO":    "\033[97m",
    "WARNING": "\033[38;5;214m",
    "SUCCESS": "\033[92m",
    "ERROR":   "\033[91m",
    "DEBUG":   "\033[90m",
}


def log(level: str, message: str) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    color = COLORS.get(level, RESET)
    print(f"{color}{ts} | {level:<7} | {message}{RESET}", file=sys.stderr)
    sys.stderr.flush()


def info(message: str) -> None:
    log("INFO", message)


def warning(message: str) -> None:
    log("WARNING", message)


def success(message: str) -> None:
    log("SUCCESS", message)


def error(message: str) -> None:
    log("ERROR", message)


def debug(message: str) -> None:
    log("DEBUG", message)
