#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Soneium Score Season 8 — Bonus Tasks Bot

Запуск: python main.py
Интерактивное меню выбора модулей (стрелки + пробел + Enter).

Файлы конфигурации:
  keys.txt      — приватные ключи (один на строку)
  proxy.txt     — прокси (опционально, формат: http://user:pass@host:port или host:port)
  config.toml   — настройки (rpc_url, delays, threads, ...)
"""

from __future__ import annotations

import random
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import questionary

import toml
from web3 import Web3

from modules import logger, db
from modules.portal_api import (
    fetch_portal_data_batch,
    parse_account_status,
    check_nekocat_gmeow_done,
    check_press_a_done,
)
from modules.proxy_utils import load_proxies_from_file, to_proxy_dict

PROJECT_ROOT = Path(__file__).resolve().parent

SEASON_START = datetime(2026, 3, 11, tzinfo=timezone.utc)


# ── Config ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    cfg_path = PROJECT_ROOT / "config.toml"
    if not cfg_path.exists():
        logger.error(f"config.toml не найден: {cfg_path}")
        sys.exit(1)
    return toml.load(cfg_path)


# ── Wallets & Proxies ────────────────────────────────────────────────────────

def load_wallets() -> list[tuple[str, str]]:
    """Возвращает список (private_key, eoa_address). Поддерживает keys.enc и keys.txt."""
    from modules.crypto_utils import load_keys_plaintext
    plaintext = load_keys_plaintext(PROJECT_ROOT)
    result = []
    for line in plaintext.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.match(r"^0x[a-fA-F0-9]{64}$", line):
            pk = line
        elif re.match(r"^[a-fA-F0-9]{64}$", line):
            pk = "0x" + line
        else:
            logger.warning(f"Неверный формат ключа, пропуск: {line[:10]}...")
            continue
        try:
            addr = Web3.to_checksum_address(Web3().eth.account.from_key(pk).address)
            result.append((pk, addr))
        except Exception as e:
            logger.warning(f"Ошибка получения адреса из ключа: {e}")
    if not result:
        logger.error("Нет валидных приватных ключей")
        sys.exit(1)
    return result


def load_proxies() -> list[Optional[str]]:
    """
    Загружает прокси из proxy.txt.
    Основной формат: IP:PORT:LOGIN:PASSWORD.
    Также поддерживаются URL-прокси.
    """
    return load_proxies_from_file(PROJECT_ROOT / "proxy.txt")


def load_adspower_key() -> Optional[str]:
    path = PROJECT_ROOT / "adspower_api_key.txt"
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def match_proxy(proxies: list[Optional[str]], index: int) -> Optional[str]:
    """Матчит прокси 1:1 с кошельком по индексу, или None если прокси нет."""
    if not proxies:
        return None
    return proxies[index % len(proxies)]


# ── Status ──────────────────────────────────────────────────────────────────

def show_status(wallets: list[tuple[str, str]], proxies: list[Optional[str]]) -> None:
    """Выводит таблицу прогресса по всем кошелькам."""
    addresses = [addr for _, addr in wallets]

    portal_cache = fetch_portal_data_batch(addresses, [p for p in proxies if p])

    header = f"{'#':<4} {'Адрес':<44} {'GM':<6} {'Passkey':<8} {'Kami w1':<9} {'Kami w2':<9} {'Kami w3':<9} {'GMeow':<8} {'Food':<6} {'PressA':<8}"
    separator = "-" * len(header)
    print(header)
    print(separator)
    for i, (pk, addr) in enumerate(wallets):
        info = db.get_account_info(addr) or {}
        status = parse_account_status(portal_cache.get(addr))

        gm      = f"{status['gm']}/{status['gm_required']}"
        passkey = "ok" if status["passkey_done"] or info.get("passkey_done") else "-"
        weeks = status["kami_weeks"] + [False] * 3  # pad до 3 элементов
        k = ["ok" if weeks[i] else "-" for i in range(3)]
        press_a = "ok" if status["press_a_done"] or info.get("press_a_done") else "-"
        gmeow_cnt = max(status["gmeow"], info.get("nekocat_gmeow_count", 0))
        food_cnt  = max(status["food"],  info.get("nekocat_food_count",  0))
        gmeow   = f"{gmeow_cnt}/{status['gmeow_required']}"
        food    = f"{food_cnt}/{status['food_required']}"

        print(f"{i+1:<4} {addr:<44} {gm:<6} {passkey:<8} {k[0]:<9} {k[1]:<9} {k[2]:<9} {gmeow:<8} {food:<6} {press_a:<8}")
    print(separator)
    print(f"Всего кошельков: {len(wallets)}")


# ── LiFi key loader ──────────────────────────────────────────────────────────

def load_lifi_key(cfg: dict) -> Optional[str]:
    """Читает LI.FI API ключ из config.toml или из lifi_api_key.txt."""
    path = PROJECT_ROOT / "lifi_api_key.txt"
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    return line
    key = cfg.get("lifi_api_key", "").strip()
    return key if key else None


# ── Portal pre-check helper ───────────────────────────────────────────────────

def _is_already_done(module_key: str, addr: str, portal_statuses: dict) -> bool:
    """
    Проверяет завершённость квеста.
    Источник истины: portal_statuses (батч-ответ). Fallback: db.
    """
    status = portal_statuses.get(addr)   # dict из parse_account_status или None
    acc = db.get_account_info(addr) or {}

    if module_key == "press_a":
        if status and status.get("press_a_done"):
            return True
        return bool(acc.get("press_a_done"))

    if module_key == "startale_gm":
        if status and status.get("gm", 0) >= status.get("gm_required", 5):
            return True
        return bool(acc.get("gm_done"))

    if module_key == "kami":
        if status:
            week_idx = max(0, (datetime.now(timezone.utc) - SEASON_START).days // 7)
            kami_weeks = status.get("kami_weeks", [])
            if week_idx < len(kami_weeks) and kami_weeks[week_idx]:
                return True
        return bool(acc.get("kami_done"))

    if module_key == "nekocat":
        if status:
            if (status.get("gmeow", 0) >= status.get("gmeow_required", 10) and
                    status.get("food", 0) >= status.get("food_required", 5)):
                return True
        return (
            acc.get("nekocat_gmeow_count", 0) >= 10
            and acc.get("nekocat_food_count", 0) >= 5
        )

    return False


# ── Single-task dispatcher ────────────────────────────────────────────────────

def _run_single_task(
    module_key: str,
    i_orig: int,
    pk: str,
    addr: str,
    proxies: list,
    cfg: dict,
    adspower_key: Optional[str],
    firstmail_pool: list,
    lifi_key: Optional[str],
    portal_statuses: dict,
) -> bool:
    """Выполняет один модуль для одного кошелька. True = запущено, False = пропущено."""
    if _is_already_done(module_key, addr, portal_statuses):
        logger.info(f"[{addr[:8]}] {module_key}: квест уже выполнен, пропуск")
        return False

    proxy = match_proxy(proxies, i_orig)

    if module_key == "startale_gm":
        if not adspower_key:
            logger.error(f"[{addr[:8]}] startale_gm требует AdsPower, пропуск")
            return True
        if i_orig >= len(firstmail_pool):
            logger.error(f"[{addr[:8]}] Нет firstmail #{i_orig+1}, пропуск")
            return True
        email, password = firstmail_pool[i_orig]
        proxy_dict = to_proxy_dict(proxy)
        logger.info(f"[{addr[:8]}] Startale GM (email: {email})")
        from modules.startale_gm import run_gm_for_account
        run_gm_for_account(pk, addr, adspower_key, proxy=proxy_dict,
                           firstmail_email=email, firstmail_password=password)

    elif module_key == "kami":
        if not adspower_key:
            logger.error(f"[{addr[:8]}] kami требует AdsPower, пропуск")
            return True
        if i_orig >= len(firstmail_pool):
            logger.error(f"[{addr[:8]}] Нет firstmail #{i_orig+1}, пропуск")
            return True
        email, password = firstmail_pool[i_orig]
        proxy_dict = to_proxy_dict(proxy)
        logger.info(f"[{addr[:8]}] Kami (email: {email})")
        from modules.kami_browser import run_kami_browser_for_account
        run_kami_browser_for_account(
            adspower_key, addr, pk, proxy_dict, cfg,
            lifi_api_key=lifi_key,
            firstmail_email=email,
            firstmail_password=password,
        )

    elif module_key == "nekocat":
        rpc = cfg.get("rpc_url", "https://soneium-rpc.publicnode.com")
        from modules.nekocat import run_nekocat_for_account
        run_nekocat_for_account(
            pk, addr, rpc, proxy=proxy,
            disable_ssl=cfg.get("disable_ssl", True),
            gas_multiplier=cfg.get("gas_limit_multiplier", 1.2),
            action_delay_min=cfg.get("action_delay_min", 5),
            action_delay_max=cfg.get("action_delay_max", 30),
        )

    elif module_key == "press_a":
        rpc = cfg.get("rpc_url", "https://soneium-rpc.publicnode.com")
        from modules.press_a import run_press_a_for_account
        run_press_a_for_account(
            pk, addr, rpc, proxy=proxy,
            disable_ssl=cfg.get("disable_ssl", True),
            gas_multiplier=cfg.get("gas_limit_multiplier", 1.2),
            lifi_api_key=lifi_key,
            config=cfg,
        )

    return True


# ── Menu ─────────────────────────────────────────────────────────────────────

MODULES = [
    ("Startale Daily GM",      "startale_gm"),
    ("Kami Puzzle Mint",       "kami"),
    ("NekoCat (GMeow + Food)", "nekocat"),
    ("Press A Gacha",          "press_a"),
]

ALL_KEY  = "__all__"
STAT_KEY = "__status__"


def show_banner() -> None:
    print("\033[96m")
    print("  ╔══════════════════════════════════════╗")
    print("  ║        Soneium Score — Bonus 8       ║")
    print("  ╚══════════════════════════════════════╝")
    print("\033[0m")


def ask_modules() -> list[str]:
    """
    Интерактивный выбор модулей стрелками + пробел + Enter.
    Возвращает список ключей выбранных модулей (или [ALL_KEY] / [STAT_KEY]).
    """
    choices = [
        questionary.Choice("Все модули подряд",    value=ALL_KEY),
        questionary.Choice("Показать статус",      value=STAT_KEY),
        questionary.Separator("─── Отдельные модули ───"),
    ] + [questionary.Choice(label, value=key) for label, key in MODULES]

    selected = questionary.checkbox(
        "Выбери действие (↑↓ — навигация, пробел — выбор, Enter — запуск):",
        choices=choices,
        style=questionary.Style([
            ("highlighted", "fg:cyan bold"),
            ("selected",    "fg:green"),
            ("pointer",     "fg:cyan bold"),
        ]),
    ).ask()

    if selected is None:          # Ctrl+C
        print("\nОтменено.")
        sys.exit(0)

    if not selected:
        logger.warning("Ничего не выбрано. Запусти снова и выбери хотя бы один пункт.")
        sys.exit(0)

    return selected


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    show_banner()

    selected = ask_modules()

    cfg = load_config()
    wallets = load_wallets()
    wallets_ordered = list(wallets)   # порядок из keys.txt, не меняется ни при каких условиях
    proxies = load_proxies()
    adspower_key = load_adspower_key()
    db.init_db()

    logger.info(f"Загружено кошельков: {len(wallets)}, прокси: {len(proxies)}")

    run_all    = ALL_KEY  in selected
    run_status = STAT_KEY in selected

    if run_status:
        show_status(wallets_ordered, proxies)
        if not run_all and set(selected) <= {STAT_KEY}:
            return

    MODULE_ORDER = ("startale_gm", "kami", "nekocat", "press_a")
    selected_modules = [m for m in MODULE_ORDER if run_all or m in selected]

    if not selected_modules:
        logger.warning("Нет модулей для выполнения.")
        return

    # Загружаем firstmail_pool один раз (нужен для startale/kami)
    firstmail_pool: list = []
    if any(m in ("startale_gm", "kami") for m in selected_modules):
        from modules.kami_browser import load_firstmail_pool
        firstmail_pool = load_firstmail_pool()
        if not firstmail_pool:
            logger.warning("firstmail_accounts.txt пуст — задачи startale_gm/kami будут пропущены")

    lifi_key = load_lifi_key(cfg)

    # Батч-запрос к порталу для pre-check "уже выполнено"
    logger.info("Запрос статусов с портала...")
    portal_raw = fetch_portal_data_batch(
        [addr for _, addr in wallets_ordered],
        [p for p in proxies if p],
    )
    portal_statuses = {
        addr: parse_account_status(portal_raw.get(addr))
        for _, addr in wallets_ordered
    }

    # Строим плоский список задач и перемешиваем (anti-sybil)
    tasks = [
        (module_key, i_orig, pk, addr)
        for module_key in selected_modules
        for i_orig, (pk, addr) in enumerate(wallets_ordered)
    ]
    random.shuffle(tasks)

    logger.info(
        f"Всего задач: {len(tasks)} "
        f"({len(wallets_ordered)} кошельков × {len(selected_modules)} модулей), "
        f"выполняем в случайном порядке"
    )

    for idx, (module_key, i_orig, pk, addr) in enumerate(tasks):
        logger.info(f"[{idx+1}/{len(tasks)}] {addr} — {module_key} (ключ #{i_orig+1})")
        ran = _run_single_task(
            module_key, i_orig, pk, addr, proxies, cfg,
            adspower_key, firstmail_pool, lifi_key,
            portal_statuses,
        )
        if ran and idx < len(tasks) - 1:
            delay = random.uniform(cfg.get("delay_min", 30), cfg.get("delay_max", 180))
            logger.info(f"Пауза {delay:.0f}с...")
            time.sleep(delay)

    logger.success("Готово! Запусти снова и выбери «Показать статус» чтобы проверить прогресс.")


if __name__ == "__main__":
    main()
