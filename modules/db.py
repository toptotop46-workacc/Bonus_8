#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
JSON-хранилище состояния аккаунтов: quest_results.json
Поля Season 8:
  - passkey_done, passkey_remove_failed
  - gm_done, next_gm_available_at, smart_account_created
  - kami_done, kami_last_mint_at
  - nekocat_gmeow_count, nekocat_food_count
  - press_a_done
  - updated_at
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
JSON_PATH = PROJECT_ROOT / "quest_results.json"


def _read_data() -> dict[str, Any]:
    data: dict[str, Any] = {}
    if JSON_PATH.exists():
        try:
            with open(JSON_PATH, "r", encoding="utf-8") as f:
                raw = f.read().strip()
            if raw:
                data = json.loads(raw)
            if not isinstance(data, dict):
                data = {}
        except (json.JSONDecodeError, ValueError):
            data = {}
    # Поддержка старого формата с ключом "accounts"
    if "accounts" in data and isinstance(data["accounts"], dict):
        data = {k: v for k, v in data["accounts"].items() if isinstance(k, str) and k.startswith("0x")}
    elif any(not k.startswith("0x") for k in data if isinstance(k, str)):
        data = {k: v for k, v in data.items() if isinstance(k, str) and k.startswith("0x")}
    return data


def _write_data(data: dict[str, Any]) -> None:
    with open(JSON_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def init_db() -> None:
    if not JSON_PATH.exists():
        _write_data({})


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_account_info(eoa_address: str) -> Optional[dict]:
    """Возвращает запись по адресу или None."""
    init_db()
    data = _read_data()
    if eoa_address not in data:
        return None
    rec = dict(data[eoa_address])
    # Нормализуем булевы поля
    for field in ("passkey_done", "passkey_remove_failed", "gm_done",
                  "smart_account_created", "kami_done",
                  "kami_week1_done", "kami_week2_done", "kami_week3_done",
                  "press_a_done"):
        rec[field] = bool(rec.get(field, False))
    for field in ("nekocat_gmeow_count", "nekocat_food_count", "press_a_spins_count"):
        rec[field] = int(rec.get(field, 0))
    return rec


def upsert_account(
    eoa_address: str,
    *,
    passkey_done: Optional[bool] = None,
    passkey_remove_failed: Optional[bool] = None,
    passkey_email: Optional[str] = None,
    gm_done: Optional[bool] = None,
    next_gm_available_at: Optional[datetime] = None,
    smart_account_created: Optional[bool] = None,
    kami_done: Optional[bool] = None,
    kami_week1_done: Optional[bool] = None,
    kami_week2_done: Optional[bool] = None,
    kami_week3_done: Optional[bool] = None,
    kami_username: Optional[str] = None,
    kami_last_mint_at: Optional[datetime] = None,
    nekocat_gmeow_count: Optional[int] = None,
    nekocat_food_count: Optional[int] = None,
    press_a_done: Optional[bool] = None,
    press_a_spins_count: Optional[int] = None,
    press_a_usdsc_spent: Optional[str] = None,
    press_a_eth_spent: Optional[str] = None,
) -> None:
    """Создаёт или обновляет запись. Поля None не изменяются."""
    init_db()
    data = _read_data()
    now = _now_utc()
    if eoa_address not in data:
        data[eoa_address] = {
            "passkey_done": False,
            "passkey_remove_failed": False,
            "passkey_email": None,
            "gm_done": False,
            "next_gm_available_at": None,
            "smart_account_created": False,
            "kami_done": False,
            "kami_week1_done": False,
            "kami_week2_done": False,
            "kami_week3_done": False,
            "kami_username": None,
            "kami_last_mint_at": None,
            "nekocat_gmeow_count": 0,
            "nekocat_food_count": 0,
            "press_a_done": False,
            "press_a_spins_count": 0,
            "press_a_usdsc_spent": "0",
            "press_a_eth_spent": "0",
            "updated_at": now,
        }
    rec = data[eoa_address]
    if passkey_done is not None:
        rec["passkey_done"] = bool(passkey_done)
    if passkey_remove_failed is not None:
        rec["passkey_remove_failed"] = bool(passkey_remove_failed)
    if passkey_email is not None:
        rec["passkey_email"] = str(passkey_email)
    if gm_done is not None:
        rec["gm_done"] = bool(gm_done)
    if next_gm_available_at is not None:
        rec["next_gm_available_at"] = next_gm_available_at.isoformat()
    if smart_account_created is not None:
        rec["smart_account_created"] = bool(smart_account_created)
    if kami_done is not None:
        rec["kami_done"] = bool(kami_done)
    if kami_week1_done is not None:
        rec["kami_week1_done"] = bool(kami_week1_done)
    if kami_week2_done is not None:
        rec["kami_week2_done"] = bool(kami_week2_done)
    if kami_week3_done is not None:
        rec["kami_week3_done"] = bool(kami_week3_done)
    if kami_username is not None:
        rec["kami_username"] = str(kami_username)
    if kami_last_mint_at is not None:
        rec["kami_last_mint_at"] = kami_last_mint_at.isoformat()
    if nekocat_gmeow_count is not None:
        rec["nekocat_gmeow_count"] = int(nekocat_gmeow_count)
    if nekocat_food_count is not None:
        rec["nekocat_food_count"] = int(nekocat_food_count)
    if press_a_done is not None:
        rec["press_a_done"] = bool(press_a_done)
    if press_a_spins_count is not None:
        rec["press_a_spins_count"] = int(press_a_spins_count)
    if press_a_usdsc_spent is not None:
        rec["press_a_usdsc_spent"] = str(press_a_usdsc_spent)
    if press_a_eth_spent is not None:
        rec["press_a_eth_spent"] = str(press_a_eth_spent)
    rec["updated_at"] = now
    _write_data(data)


def get_all_addresses() -> list[str]:
    return list(_read_data().keys())


def is_gm_needed_now(eoa_address: str) -> bool:
    """True если для аккаунта пора отправить GM (нет записи или время вышло)."""
    rec = get_account_info(eoa_address)
    if rec is None:
        return True
    if rec.get("gm_done"):
        return False
    next_at_str = rec.get("next_gm_available_at")
    if next_at_str is None:
        return True
    try:
        next_at = datetime.fromisoformat(next_at_str.replace("Z", "+00:00"))
        return next_at <= datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True


def is_kami_needed_this_week(eoa_address: str) -> bool:
    """True если kami_done=False или с последнего минта прошло >7 дней."""
    rec = get_account_info(eoa_address)
    if rec is None:
        return True
    if rec.get("kami_done"):
        return False
    last_str = rec.get("kami_last_mint_at")
    if last_str is None:
        return True
    try:
        from datetime import timedelta
        last_at = datetime.fromisoformat(last_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) - last_at > timedelta(days=7)
    except (ValueError, TypeError):
        return True


def get_accounts_due_for_gm(known_addresses: list[str]) -> list[str]:
    """Адреса, для которых пора GM."""
    init_db()
    data = _read_data()
    now_utc = datetime.now(timezone.utc)
    due = []
    for addr in known_addresses:
        rec = data.get(addr)
        if rec is None:
            due.append(addr)
            continue
        if rec.get("gm_done"):
            continue
        next_at_str = rec.get("next_gm_available_at")
        if next_at_str is None:
            due.append(addr)
            continue
        try:
            next_at = datetime.fromisoformat(next_at_str.replace("Z", "+00:00"))
            if next_at <= now_utc:
                due.append(addr)
        except (ValueError, TypeError):
            due.append(addr)
    return due
