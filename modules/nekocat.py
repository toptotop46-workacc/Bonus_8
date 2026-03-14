#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
NekoCat — два действия:
  1. GMeow Check-in: signGMeow (10 раз за сезон, 1 раз в день) — без кота
  2. Food: заглушка — только после покупки кота за 0.00375 ETH
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Optional

from web3 import AsyncWeb3

from modules import logger, db
from modules.web3_utils import get_w3, get_account, send_contract_tx, close_web3_provider
from modules.portal_api import check_nekocat_gmeow_done, get_nekocat_progress

# ── GMeow (NekoActivityReward) ─────────────────────────────────────────────

NEKOCAT_CHECKIN_ADDRESS = "0xfF3aC835a193Cc08543256e24508b42248A63A26"

NEKOCAT_CHECKIN_ABI = json.loads("""
[
  {"inputs":[{"name":"message","type":"string"},{"name":"dayNumber","type":"uint256"},{"name":"currentStreak","type":"uint256"}],"name":"signGMeow","outputs":[],"stateMutability":"nonpayable","type":"function"},
  {"inputs":[{"name":"user","type":"address"}],"name":"getGMeowStats","outputs":[{"name":"currentStreak","type":"uint256"},{"name":"totalSigned","type":"uint256"},{"name":"totalPaw","type":"uint256"},{"name":"firstSignDate","type":"uint256"},{"name":"canSignToday","type":"bool"}],"stateMutability":"view","type":"function"}
]
""")

# Food — покупка еды через контракт NekoCatFood
NEKOCAT_FOOD_ADDRESS = "0x0BBabFDe7B628AE2b0e9eC80Ac239BbcE650ea21"

# Минимальный ABI для NekoCatFood (mint еды)
NEKOCAT_FOOD_ABI = json.loads("""
[
  {
    "inputs": [
      {"name": "foodTypeId", "type": "uint256"},
      {"name": "amount", "type": "uint256"}
    ],
    "name": "batchMintFood",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function"
  }
]
""")

# Цена за 1 еду (из успешной транзакции 0x31d2...324fe): 0.000025 ETH
FOOD_PRICE_WEI = 25_000_000_000_000

# Целевые счётчики за сезон
GMEOW_TARGET = 10
FOOD_TARGET = 5


async def _do_gmeow_checkin(
    private_key: str,
    eoa_address: str,
    rpc_url: str,
    proxy: Optional[str] = None,
    disable_ssl: bool = True,
    gas_multiplier: float = 1.2,
) -> Optional[str]:
    """
    Выполняет GMeow sign-in (signGMeow).
    Возвращает tx hash или None если canSignToday=false (уже сделан сегодня).
    """
    w3 = get_w3(rpc_url, proxy=proxy, disable_ssl=disable_ssl)
    account = get_account(private_key)
    try:
        contract = w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(NEKOCAT_CHECKIN_ADDRESS),
            abi=NEKOCAT_CHECKIN_ABI,
        )

        addr_cs = AsyncWeb3.to_checksum_address(eoa_address)
        stats = await contract.functions.getGMeowStats(addr_cs).call()
        current_streak = stats[0]
        can_sign_today = stats[4]

        if not can_sign_today:
            return None

        day_number = (int(time.time()) // 86400) % 30 + 1
        fn = contract.functions.signGMeow("GM", day_number, current_streak)

        return await send_contract_tx(
            w3, account, fn,
            value=0,
            action=f"[{eoa_address}] NekoCat GMeow signGMeow",
            gas_multiplier=gas_multiplier,
        )
    finally:
        await close_web3_provider(w3)


async def _buy_food_once(
    private_key: str,
    eoa_address: str,
    rpc_url: str,
    proxy: Optional[str] = None,
    disable_ssl: bool = True,
    gas_multiplier: float = 1.2,
    food_type_id: int = 0,
    amount: int = 1,
) -> Optional[str]:
    """
    Покупает NekoCat Food через контракт NekoCatFood.batchMintFood.
    Возвращает tx hash или None при ошибке до отправки tx.
    """
    w3 = get_w3(rpc_url, proxy=proxy, disable_ssl=disable_ssl)
    account = get_account(private_key)
    try:
        contract = w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(NEKOCAT_FOOD_ADDRESS),
            abi=NEKOCAT_FOOD_ABI,
        )
        addr_cs = AsyncWeb3.to_checksum_address(eoa_address)
        logger.info(f"[{addr_cs}] NekoCat Food mint: type={food_type_id}, amount={amount}")

        fn = contract.functions.batchMintFood(int(food_type_id), int(amount))
        tx = await send_contract_tx(
            w3,
            account,
            fn,
            value=FOOD_PRICE_WEI,
            action=f"[{eoa_address}] NekoCat Food batchMintFood",
            gas_multiplier=gas_multiplier,
        )
        return tx
    finally:
        await close_web3_provider(w3)


def run_nekocat_for_account(
    private_key: str,
    eoa_address: str,
    rpc_url: str,
    proxy: Optional[str] = None,
    disable_ssl: bool = True,
    gas_multiplier: float = 1.2,
    action_delay_min: float = 5.0,
    action_delay_max: float = 30.0,
) -> bool:
    """
    Выполняет NekoCat задания:
    - GMeow check-in если счётчик < 10
    - Food Shop mint если счётчик < 5
    Возвращает True при успехе или пропуске, False при ошибке.
    """
    db.init_db()
    import time

    proxy_url = f"http://{proxy}" if proxy and not proxy.startswith("http") else proxy

    # Получаем прогресс из portal
    progress = get_nekocat_progress(eoa_address)
    gmeow_cur, gmeow_req = progress["gmeow"]
    food_cur, food_req = progress["food"]

    # Синхронизируем с db
    acc = db.get_account_info(eoa_address) or {}
    db_gmeow = acc.get("nekocat_gmeow_count", 0)
    db_food = acc.get("nekocat_food_count", 0)
    # Берём максимум из portal и db
    gmeow_count = max(gmeow_cur, db_gmeow)
    food_count = max(food_cur, db_food)

    overall_ok = True

    # ── GMeow Check-in ────────────────────────────────────────────────────
    if gmeow_count >= GMEOW_TARGET:
        logger.info(f"[{eoa_address}] NekoCat GMeow выполнен ({gmeow_count}/{GMEOW_TARGET}), пропуск")
    else:
        gmeow_done_api = check_nekocat_gmeow_done(eoa_address)
        if gmeow_done_api is True:
            db.upsert_account(eoa_address, nekocat_gmeow_count=GMEOW_TARGET)
            logger.info(f"[{eoa_address}] NekoCat GMeow уже выполнен (portal), пропуск")
        else:
            logger.info(f"[{eoa_address}] NekoCat GMeow ({gmeow_count}/{GMEOW_TARGET})...")
            try:
                tx = asyncio.run(_do_gmeow_checkin(
                    private_key, eoa_address, rpc_url,
                    proxy=proxy_url, disable_ssl=disable_ssl, gas_multiplier=gas_multiplier,
                ))
                if tx is None:
                    logger.info(f"[{eoa_address}] GMeow: уже сделан сегодня (canSignToday=false), пропуск")
                else:
                    gmeow_count += 1
                    db.upsert_account(eoa_address, nekocat_gmeow_count=gmeow_count)
                    logger.success(f"[{eoa_address}] GMeow sign-in #{gmeow_count}: {tx}")
            except Exception as e:
                logger.error(f"[{eoa_address}] GMeow ошибка: {e}")
                overall_ok = False

    # Пауза между действиями
    if food_count < FOOD_TARGET:
        delay = random.uniform(action_delay_min, action_delay_max)
        time.sleep(delay)

    # ── Food mint ──────────────────────────────────────────────────────────
    if food_count >= FOOD_TARGET:
        logger.info(f"[{eoa_address}] NekoCat Food выполнен ({food_count}/{FOOD_TARGET}), пропуск")
    else:
        logger.info(
            f"[{eoa_address}] NekoCat Food ({food_count}/{FOOD_TARGET}) — "
            f"минтим еду через NekoCatFood без кота"
        )
        try:
            tx = asyncio.run(
                _buy_food_once(
                    private_key,
                    eoa_address,
                    rpc_url,
                    proxy=proxy_url,
                    disable_ssl=disable_ssl,
                    gas_multiplier=gas_multiplier,
                    food_type_id=0,
                    amount=1,
                )
            )
            if tx:
                food_count += 1
                db.upsert_account(eoa_address, nekocat_food_count=food_count)
                logger.success(f"[{eoa_address}] NekoCat Food mint #{food_count}: {tx}")
            else:
                logger.warning(f"[{eoa_address}] NekoCat Food: mint не выполнен")
                overall_ok = False
        except Exception as e:
            logger.error(f"[{eoa_address}] NekoCat Food ошибка: {e}")
            overall_ok = False

    return overall_ok
