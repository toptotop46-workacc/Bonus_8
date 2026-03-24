#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Работа с API портала Soneium: проверка выполнения квестов Season 8.
Endpoint: https://portal.soneium.org/api/profile/bonus-dapp?address=0x...

Season 8 dapp IDs (проверено 2026-03-12):
  startale_8  — Startale GM + Passkey
  kami_8      — Kami puzzle pieces
  nekocat_8   — NekoCat GMeow + Food
  pressa_8    — Press A Unique NFT
"""

from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, List, Optional

import requests
from tqdm import tqdm

from modules.proxy_utils import (
    build_proxy_chain,
    is_proxy_auth_error,
    proxy_dict_to_url,
    to_proxy_dict,
)

SONEIUM_BONUS_URL = "https://portal.soneium.org/api/profile/bonus-dapp"

# Точные ID дапп для Season 8
DAPP_STARTALE = "startale_8"
DAPP_KAMI     = "kami_8"
DAPP_NEKOCAT  = "nekocat_8"
DAPP_PRESSA   = "pressa_8"

_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/144.0.0.0 Safari/537.36",
]


def _get_headers() -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "ru,ru-RU;q=0.9,en-US;q=0.8,en;q=0.7",
        "dnt": "1",
        "priority": "u=1, i",
        "referer": "https://portal.soneium.org/en/profile/",
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "user-agent": random.choice(_USER_AGENTS),
    }


def get_bonus_dapp_data(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> Optional[List[Any]]:
    """
    Запрашивает список dapp-объектов для адреса.
    Возвращает список или None при ошибке.
    """
    url = f"{SONEIUM_BONUS_URL}?address={address}"
    proxy_chain = build_proxy_chain(proxy_dict_to_url(proxies), proxy_pool or [])
    if not proxy_chain:
        proxy_chain = [None]

    last_error: Optional[Exception] = None
    for idx, proxy_url in enumerate(proxy_chain, start=1):
        proxy_dict = to_proxy_dict(proxy_url)
        try:
            r = requests.get(url, headers=_get_headers(), proxies=proxy_dict, timeout=20)
            r.raise_for_status()
            data = r.json()
            if isinstance(data, list):
                return data
            return None
        except Exception as exc:
            last_error = exc
            if proxy_url and is_proxy_auth_error(exc) and idx < len(proxy_chain):
                continue
            return None

    if last_error:
        return None
    return None


def _get_dapp(data: List[Any], dapp_id: str) -> Optional[dict]:
    """Возвращает dapp-объект по точному ID."""
    for item in data:
        if item.get("id") == dapp_id:
            return item
    return None


def _find_quest_in_dapp(dapp: dict, text: str) -> Optional[dict]:
    """Ищет квест по вхождению текста в description (case-insensitive)."""
    for q in (dapp.get("quests") or []):
        desc = (q.get("description") or "").lower()
        if text.lower() in desc:
            return q
    return None


# ── Startale (Season 8: startale_8) ────────────────────────────────────────

def check_startale_passkey_quest_done(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> Optional[bool]:
    """«Set up Passkey or social recovery» → True/False/None."""
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_STARTALE)
    if dapp is None:
        return False
    q = _find_quest_in_dapp(dapp, "passkey") or _find_quest_in_dapp(dapp, "social recovery")
    return bool(q.get("isDone")) if q else False


def check_startale_gm_5_done(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> Optional[bool]:
    """«Send Daily GM 5 times» → True/False/None."""
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_STARTALE)
    if dapp is None:
        return False
    q = _find_quest_in_dapp(dapp, "send daily gm") or _find_quest_in_dapp(dapp, "daily gm")
    return bool(q.get("isDone")) if q else False


def get_startale_gm_progress(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> tuple[int, int]:
    """Возвращает (completed, required) прогресс по GM квесту."""
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return (0, 5)
    dapp = _get_dapp(data, DAPP_STARTALE)
    if dapp is None:
        return (0, 5)
    q = _find_quest_in_dapp(dapp, "send daily gm") or _find_quest_in_dapp(dapp, "daily gm")
    if q is None:
        return (0, 5)
    return (int(q.get("completed", 0)), int(q.get("required", 5)))


# ── Kami (Season 8: kami_8) ────────────────────────────────────────────────

def check_kami_week_done(
    address: str,
    week: int,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> Optional[bool]:
    """Проверяет выполнение Kami puzzle piece для конкретной недели → True/False/None."""
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_KAMI)
    if dapp is None:
        return False
    q = _find_quest_in_dapp(dapp, f"week {week}")
    return bool(q.get("isDone")) if q else False


def check_kami_done(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> Optional[bool]:
    """Проверяет выполнены ли ВСЕ Kami quests → True/False/None."""
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_KAMI)
    if dapp is None:
        return False
    quests = dapp.get("quests") or []
    if not quests:
        return False
    return all(bool(q.get("isDone")) for q in quests)


def get_kami_progress(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> list[dict]:
    """
    Возвращает список квестов Kami с прогрессом:
    [{"desc": "Mint week 1 puzzle piece", "isDone": False, "completed": 0, "required": 1}, ...]
    """
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return []
    dapp = _get_dapp(data, DAPP_KAMI)
    if dapp is None:
        return []
    return [
        {
            "desc": q.get("description", ""),
            "isDone": bool(q.get("isDone")),
            "completed": int(q.get("completed", 0)),
            "required": int(q.get("required", 1)),
        }
        for q in (dapp.get("quests") or [])
    ]


# ── NekoCat (Season 8: nekocat_8) ──────────────────────────────────────────

def check_nekocat_gmeow_done(address: str, proxies: Optional[dict] = None) -> Optional[bool]:
    """«Check-in with GMeow Calendar 10 Times» → True/False/None."""
    data = get_bonus_dapp_data(address, proxies)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_NEKOCAT)
    if dapp is None:
        return False
    q = _find_quest_in_dapp(dapp, "gmeow") or _find_quest_in_dapp(dapp, "check-in")
    return bool(q.get("isDone")) if q else False


def check_nekocat_food_done(address: str, proxies: Optional[dict] = None) -> Optional[bool]:
    """«Mint food 5 times from the Food Shop» → True/False/None."""
    data = get_bonus_dapp_data(address, proxies)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_NEKOCAT)
    if dapp is None:
        return False
    q = _find_quest_in_dapp(dapp, "food")
    return bool(q.get("isDone")) if q else False


def get_nekocat_progress(address: str, proxies: Optional[dict] = None) -> dict:
    """Возвращает прогресс NekoCat: {gmeow: (completed, required), food: (completed, required)}."""
    data = get_bonus_dapp_data(address, proxies)
    result = {"gmeow": (0, 10), "food": (0, 5)}
    if data is None:
        return result
    dapp = _get_dapp(data, DAPP_NEKOCAT)
    if dapp is None:
        return result
    q_gmeow = _find_quest_in_dapp(dapp, "gmeow") or _find_quest_in_dapp(dapp, "check-in")
    q_food = _find_quest_in_dapp(dapp, "food")
    if q_gmeow:
        result["gmeow"] = (int(q_gmeow.get("completed", 0)), int(q_gmeow.get("required", 10)))
    if q_food:
        result["food"] = (int(q_food.get("completed", 0)), int(q_food.get("required", 5)))
    return result


# ── Press A (Season 8: pressa_8) ───────────────────────────────────────────

def check_press_a_done(
    address: str,
    proxies: Optional[dict] = None,
    proxy_pool: Optional[list[str]] = None,
) -> Optional[bool]:
    """«Mint 1 Unique-grade NFT» → True/False/None."""
    data = get_bonus_dapp_data(address, proxies, proxy_pool=proxy_pool)
    if data is None:
        return None
    dapp = _get_dapp(data, DAPP_PRESSA)
    if dapp is None:
        return False
    q = _find_quest_in_dapp(dapp, "unique")
    return bool(q.get("isDone")) if q else False


# ── Batch fetch ─────────────────────────────────────────────────────────────

def parse_account_status(data: Optional[List[Any]]) -> dict:
    """
    Парсит все поля статуса из уже загруженного portal data (один запрос на кошелёк).
    Возвращает dict: gm, gm_required, passkey_done, kami_done,
                     gmeow, gmeow_required, food, food_required, press_a_done.
    """
    result = {
        "gm": 0, "gm_required": 5,
        "passkey_done": False,
        "kami_done": False,
        "kami_weeks": [],
        "gmeow": 0, "gmeow_required": 10,
        "food": 0, "food_required": 5,
        "press_a_done": False,
    }
    if not data:
        return result

    startale = _get_dapp(data, DAPP_STARTALE)
    if startale:
        q_gm = _find_quest_in_dapp(startale, "daily gm")
        if q_gm:
            result["gm"] = int(q_gm.get("completed", 0))
            result["gm_required"] = int(q_gm.get("required", 5))
        q_pk = _find_quest_in_dapp(startale, "passkey") or _find_quest_in_dapp(startale, "social recovery")
        if q_pk:
            result["passkey_done"] = bool(q_pk.get("isDone"))

    kami = _get_dapp(data, DAPP_KAMI)
    if kami:
        quests = kami.get("quests") or []
        result["kami_done"] = bool(quests) and all(bool(q.get("isDone")) for q in quests)
        result["kami_weeks"] = [bool(q.get("isDone")) for q in quests]

    nekocat = _get_dapp(data, DAPP_NEKOCAT)
    if nekocat:
        q_gmeow = _find_quest_in_dapp(nekocat, "gmeow") or _find_quest_in_dapp(nekocat, "check-in")
        q_food = _find_quest_in_dapp(nekocat, "food")
        if q_gmeow:
            result["gmeow"] = int(q_gmeow.get("completed", 0))
            result["gmeow_required"] = int(q_gmeow.get("required", 10))
        if q_food:
            result["food"] = int(q_food.get("completed", 0))
            result["food_required"] = int(q_food.get("required", 5))

    pressa = _get_dapp(data, DAPP_PRESSA)
    if pressa:
        q = _find_quest_in_dapp(pressa, "unique")
        result["press_a_done"] = bool(q.get("isDone")) if q else False

    return result


def fetch_portal_data_batch(
    addresses: list[str],
    proxy_urls: list[Optional[str]],
    batch_size: int = 50,
) -> dict[str, Optional[List[Any]]]:
    """
    Параллельно запрашивает portal data для всех адресов батчами по batch_size.
    Каждому запросу назначается случайный прокси из proxy_urls.
    Возвращает dict: address → raw portal data (или None при ошибке).
    """
    results: dict[str, Optional[List[Any]]] = {}

    def _fetch_one(address: str) -> tuple[str, Optional[List[Any]]]:
        proxy_url = random.choice(proxy_urls) if proxy_urls else None
        proxy_dict = {"http": proxy_url, "https": proxy_url} if proxy_url else None
        return address, get_bonus_dapp_data(address, proxy_dict, proxy_pool=proxy_urls)

    # Обрабатываем батчами чтобы не открывать сотни соединений разом
    with tqdm(total=len(addresses), desc="Portal статус", unit="wallet",
              bar_format="{l_bar}{bar}| {n_fmt}/{total_fmt} [{elapsed}<{remaining}]",
              colour="cyan") as pbar:
        for start in range(0, len(addresses), batch_size):
            chunk = addresses[start:start + batch_size]
            with ThreadPoolExecutor(max_workers=len(chunk)) as executor:
                futures = {executor.submit(_fetch_one, addr): addr for addr in chunk}
                for future in as_completed(futures):
                    addr, data = future.result()
                    results[addr] = data
                    pbar.update(1)

    return results
