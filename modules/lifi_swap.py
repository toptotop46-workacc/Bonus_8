#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LI.FI swap на Soneium (chain 1868):
  swap_eth_to_token   — ETH -> произвольный токен (в т.ч. USDC.E для Kami)
  swap_eth_to_usdsc   — ETH -> USDSC (обёртка над swap_eth_to_token)
  swap_usdsc_to_eth   — USDSC -> ETH (с ERC-20 approve если нужен)
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

import aiohttp
from web3 import AsyncWeb3

from modules import logger
from modules.web3_utils import get_nonce, send_tx, send_contract_tx

LIFI_QUOTE_URL = "https://li.quest/v1/quote"
SONEIUM_CHAIN_ID = 1868
ETH_ZERO_ADDR = "0x0000000000000000000000000000000000000000"
USDSC_ADDRESS = "0x3f99231dD03a9F0E7e3421c92B7b90fbe012985a"

ERC20_ABI = json.loads("""[
  {"inputs":[{"type":"address","name":"owner"},{"type":"address","name":"spender"}],
   "name":"allowance","outputs":[{"type":"uint256"}],"stateMutability":"view","type":"function"},
  {"inputs":[{"type":"address","name":"spender"},{"type":"uint256","name":"amount"}],
   "name":"approve","outputs":[{"type":"bool"}],"stateMutability":"nonpayable","type":"function"}
]""")


def _parse_int(val) -> int:
    if val is None:
        return 0
    if isinstance(val, int):
        return val
    s = str(val)
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)
    return int(s)


async def _get_session(proxy: Optional[str]):
    """Возвращает (session, proxy_url) с учётом SOCKS."""
    connector = None
    proxy_url = None
    if proxy:
        if proxy.startswith("socks"):
            from aiohttp_socks import ProxyConnector
            connector = ProxyConnector.from_url(proxy)
        else:
            proxy_url = proxy
    return aiohttp.ClientSession(connector=connector), proxy_url


async def _lifi_quote(
    params: dict,
    lifi_api_key: Optional[str],
    proxy: Optional[str],
) -> dict:
    """GET /v1/quote → возвращает весь ответ."""
    headers = {}
    if lifi_api_key:
        headers["x-lifi-api-key"] = lifi_api_key

    session, proxy_url = await _get_session(proxy)
    async with session:
        async with session.get(
            LIFI_QUOTE_URL,
            params=params,
            headers=headers,
            proxy=proxy_url,
            ssl=False,
        ) as resp:
            if resp.status != 200:
                text = await resp.text()
                raise RuntimeError(f"LI.FI API ошибка {resp.status}: {text[:300]}")
            return await resp.json()


async def _build_and_send(
    w3: AsyncWeb3,
    account,
    addr_cs: str,
    tx_req: dict,
    action: str,
) -> str:
    """Формирует EIP-1559 tx из LI.FI transactionRequest и отправляет."""
    chain_id = await w3.eth.chain_id
    nonce = await get_nonce(w3, addr_cs)

    tx = {
        "from": addr_cs,
        "to": AsyncWeb3.to_checksum_address(tx_req["to"]),
        "data": tx_req.get("data", "0x"),
        "value": _parse_int(tx_req.get("value", 0)),
        "nonce": nonce,
        "chainId": chain_id,
    }

    if "maxFeePerGas" in tx_req:
        tx["maxFeePerGas"] = _parse_int(tx_req["maxFeePerGas"])
        tx["maxPriorityFeePerGas"] = _parse_int(
            tx_req.get("maxPriorityFeePerGas", tx_req["maxFeePerGas"])
        )
        tx["type"] = 2
    elif "gasPrice" in tx_req:
        tx["gasPrice"] = _parse_int(tx_req["gasPrice"])
    else:
        latest = await w3.eth.get_block("latest")
        base_fee = int(latest["baseFeePerGas"])
        prio = await w3.eth.max_priority_fee
        tx["maxPriorityFeePerGas"] = prio
        tx["maxFeePerGas"] = prio + base_fee * 2
        tx["type"] = 2

    gas_raw = tx_req.get("gasLimit") or tx_req.get("gas")
    if gas_raw:
        tx["gas"] = _parse_int(gas_raw)
    else:
        estimated = await w3.eth.estimate_gas(
            {"from": tx["from"], "to": tx["to"],
             "data": tx["data"], "value": tx["value"]}
        )
        tx["gas"] = int(estimated * 1.3)

    return await send_tx(w3, account, tx, action=action)


# ── Публичные функции ─────────────────────────────────────────────────────────

async def swap_eth_to_token(
    w3: AsyncWeb3,
    account,
    addr_cs: str,
    to_token_address: str,
    eth_amount_wei: int,
    lifi_api_key: Optional[str] = None,
    proxy: Optional[str] = None,
    slippage: float = 0.02,
) -> int:
    """
    ETH -> произвольный токен на Soneium через LI.FI.
    to_token_address — адрес целевого токена (например USDC.E).
    Возвращает ожидаемое кол-во токена (raw, decimals зависят от токена).
    """
    to_token = AsyncWeb3.to_checksum_address(to_token_address)
    params = {
        "fromChain": str(SONEIUM_CHAIN_ID),
        "toChain": str(SONEIUM_CHAIN_ID),
        "fromToken": ETH_ZERO_ADDR,
        "toToken": to_token,
        "fromAmount": str(eth_amount_wei),
        "fromAddress": addr_cs,
        "slippage": str(slippage),
        "order": "CHEAPEST",
    }

    logger.info(
        f"[{addr_cs}] LI.FI: {eth_amount_wei/1e18:.6f} ETH -> token {to_token[:10]}..."
    )
    data = await _lifi_quote(params, lifi_api_key, proxy)

    step = data.get("step") or data
    tx_req = step.get("transactionRequest")
    if not tx_req:
        raise RuntimeError(f"LI.FI: нет transactionRequest: {str(data)[:300]}")

    estimate = step.get("estimate", {})
    to_amount = int(estimate.get("toAmount", 0))
    logger.info(f"[{addr_cs}] LI.FI: ожидаем ~{to_amount} (raw)")

    tx_hash = await _build_and_send(
        w3, account, addr_cs, tx_req,
        action=f"[{addr_cs}] LI.FI ETH->token",
    )
    logger.success(f"[{addr_cs}] ETH->token swap: {tx_hash}")
    await asyncio.sleep(5)
    return to_amount


async def swap_eth_to_usdsc(
    w3: AsyncWeb3,
    account,
    addr_cs: str,
    eth_amount_wei: int,
    lifi_api_key: Optional[str] = None,
    proxy: Optional[str] = None,
    slippage: float = 0.02,
) -> int:
    """
    ETH -> USDSC через LI.FI.
    Возвращает ожидаемое кол-во USDSC (raw, 6 decimals).
    """
    return await swap_eth_to_token(
        w3, account, addr_cs,
        to_token_address=USDSC_ADDRESS,
        eth_amount_wei=eth_amount_wei,
        lifi_api_key=lifi_api_key,
        proxy=proxy,
        slippage=slippage,
    )


async def swap_usdsc_to_eth(
    w3: AsyncWeb3,
    account,
    addr_cs: str,
    usdsc_amount: int,
    lifi_api_key: Optional[str] = None,
    proxy: Optional[str] = None,
    slippage: float = 0.02,
    gas_multiplier: float = 1.2,
) -> int:
    """
    USDSC -> ETH через LI.FI.
    Перед свапом выставляет approve если нужен.
    Возвращает ожидаемое кол-во ETH в wei.
    """
    params = {
        "fromChain": str(SONEIUM_CHAIN_ID),
        "toChain":   str(SONEIUM_CHAIN_ID),
        "fromToken": USDSC_ADDRESS,
        "toToken":   ETH_ZERO_ADDR,
        "fromAmount": str(usdsc_amount),
        "fromAddress": addr_cs,
        "slippage": str(slippage),
        "order": "CHEAPEST",
    }

    logger.info(
        f"[{addr_cs}] LI.FI: {usdsc_amount/1e6:.4f} USDSC -> ETH"
    )
    data = await _lifi_quote(params, lifi_api_key, proxy)

    step = data.get("step") or data
    tx_req = step.get("transactionRequest")
    if not tx_req:
        raise RuntimeError(f"LI.FI: нет transactionRequest: {str(data)[:300]}")

    estimate = step.get("estimate", {})
    to_amount = int(estimate.get("toAmount", 0))
    approval_addr = estimate.get("approvalAddress") or step.get("action", {}).get("toContractAddress")
    logger.info(f"[{addr_cs}] LI.FI: ожидаем ~{to_amount/1e18:.6f} ETH")

    # ── ERC-20 approve если нужен ─────────────────────────────────────────────
    if approval_addr:
        spender = AsyncWeb3.to_checksum_address(approval_addr)
        usdsc_contract = w3.eth.contract(
            address=AsyncWeb3.to_checksum_address(USDSC_ADDRESS),
            abi=ERC20_ABI,
        )
        allowance = await usdsc_contract.functions.allowance(addr_cs, spender).call()
        if allowance < usdsc_amount:
            logger.info(
                f"[{addr_cs}] Approve USDSC для LI.FI router "
                f"({usdsc_amount/1e6:.4f} USDSC)..."
            )
            approve_tx = await send_contract_tx(
                w3, account,
                usdsc_contract.functions.approve(spender, usdsc_amount),
                value=0,
                action=f"[{addr_cs}] USDSC approve",
                gas_multiplier=gas_multiplier,
            )
            logger.success(f"[{addr_cs}] Approve tx: {approve_tx}")
            await asyncio.sleep(3)

    tx_hash = await _build_and_send(
        w3, account, addr_cs, tx_req,
        action=f"[{addr_cs}] LI.FI USDSC->{to_amount/1e18:.6f}ETH",
    )
    logger.success(f"[{addr_cs}] USDSC->ETH swap: {tx_hash}")
    await asyncio.sleep(5)
    return to_amount
