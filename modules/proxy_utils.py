#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Утилиты для нормализации и валидации proxy.txt.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

from modules import logger

_PLACEHOLDER_TOKENS = {
    "ip",
    "host",
    "port",
    "proxy",
}


def is_proxy_auth_error(exc_or_text: object) -> bool:
    text = str(exc_or_text).lower()
    return "proxy authentication required" in text or (
        "407" in text and "proxy" in text
    )


def _looks_like_placeholder(value: Optional[str]) -> bool:
    if value is None:
        return False
    token = value.strip().lower().strip("<>[]{}()")
    return token in _PLACEHOLDER_TOKENS


def _validate_host(host: str) -> Optional[str]:
    if not host:
        return "пустой host"
    if " " in host:
        return "host содержит пробелы"
    if _looks_like_placeholder(host):
        return "host выглядит как шаблон"
    return None


def _validate_port(port: str | int | None) -> Optional[str]:
    if port is None:
        return "не указан port"
    raw = str(port).strip()
    if _looks_like_placeholder(raw):
        return "port выглядит как шаблон"
    try:
        num = int(raw)
    except ValueError:
        return "port не является числом"
    if not 1 <= num <= 65535:
        return "port вне диапазона 1..65535"
    return None


def _build_http_proxy(
    host: str,
    port: str,
    username: Optional[str] = None,
    password: Optional[str] = None,
) -> tuple[Optional[str], Optional[str]]:
    host_error = _validate_host(host)
    if host_error:
        return None, host_error

    port_error = _validate_port(port)
    if port_error:
        return None, port_error

    if username is None and password is None:
        return f"http://{host}:{int(port)}", None

    if not username:
        return None, "не указан логин прокси"
    if password is None or password == "":
        return None, "не указан пароль прокси"

    return (
        f"http://{quote(username, safe='')}:{quote(password, safe='')}@{host}:{int(port)}",
        None,
    )


def parse_proxy_line(raw_line: str) -> tuple[Optional[str], Optional[str]]:
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None, None

    if "://" in line:
        try:
            parsed = urlsplit(line)
        except ValueError as exc:
            return None, f"некорректный URL: {exc}"

        if not parsed.scheme:
            return None, "не указана схема прокси"
        if parsed.hostname is None:
            return None, "не указан host прокси"
        if parsed.port is None:
            return None, "не указан port прокси"

        host_error = _validate_host(parsed.hostname)
        if host_error:
            return None, host_error

        port_error = _validate_port(parsed.port)
        if port_error:
            return None, port_error

        username = parsed.username
        password = parsed.password
        if username is not None or password is not None:
            if not username:
                return None, "не указан логин прокси"
            if password is None or password == "":
                return None, "не указан пароль прокси"
            auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
        else:
            auth = ""

        netloc = f"{auth}{parsed.hostname}:{parsed.port}"
        return urlunsplit(
            (parsed.scheme, netloc, parsed.path or "", parsed.query or "", parsed.fragment or "")
        ), None

    parts = line.split(":", 3)
    if len(parts) == 2:
        return _build_http_proxy(parts[0], parts[1])
    if len(parts) == 4:
        return _build_http_proxy(parts[0], parts[1], parts[2], parts[3])
    return None, "поддерживаются только host:port, host:port:user:pass или URL"


def load_proxies_from_file(path: Path) -> list[str]:
    if not path.exists():
        return []

    proxies: list[str] = []
    with open(path, "r", encoding="utf-8") as fh:
        for line_no, line in enumerate(fh, start=1):
            proxy_url, error = parse_proxy_line(line)
            if proxy_url:
                proxies.append(proxy_url)
            elif error:
                logger.warning(f"[{path.name}:{line_no}] Прокси пропущен: {error}")
    return proxies


def to_proxy_dict(proxy_url: Optional[str]) -> Optional[dict]:
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def proxy_dict_to_url(proxy_dict: Optional[dict]) -> Optional[str]:
    if not proxy_dict:
        return None
    return proxy_dict.get("https") or proxy_dict.get("http")


def build_proxy_chain(initial_proxy: Optional[str], proxy_pool: list[str]) -> list[str]:
    chain: list[str] = []
    if initial_proxy:
        chain.append(initial_proxy)
    for proxy_url in proxy_pool:
        if proxy_url and proxy_url not in chain:
            chain.append(proxy_url)
    return chain
