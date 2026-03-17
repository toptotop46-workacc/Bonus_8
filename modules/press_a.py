#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Press A — USDSC bootstrap + Shell/Stone loop для получения Unique NFT.

Стратегия:
  Фаза 1 (Bootstrap, однократно):
    checkIn → approvals → купить USDSC если < 0.25 →
    наминтить 200 Rug через requestGachaByUSDSC(0, batch) → продать все

  Фаза 2 (основной цикл, только тикеты):
    Повторять до Unique: spin all Shell → spin all Stone → продать NFT

Контракты:
  Gacha:       0xf1Be6F9d4ff40Cac47C620E058535451596a5aBD
  SaleUpgrade: 0xF3d98F2B3403723d8417FeA42b4E166d599ea3f9
  Vault:       0xFc31B05318dcc603A6DAB21E519aCC639b205fb4  (spender для permit)
  USDSC:       0x3f99231dD03a9F0E7e3421c92B7b90fbe012985a  (6 decimals, EIP-2612)
  GameNFT:     0x3B8BA2A8e7374C9Ff9BfD2ceC84CB115D1F2D4ee

Тиры:
  0 = Rug    (Free/USDSC, Unique 0%, no VRF)
  1 = Ape    (Stone ticket, Unique 0.1%, no VRF)
  2 = Hodler (Shell ticket, Unique 4%, Pyth VRF)
  3 = Alpha  ($0.20, Unique 11%)
  4 = Degen  ($1.00, Unique 40%)
  5 = LFG    ($6.00, Unique 50%)

Grade encoding: grade = tokenId // 300_000
  0=Common, 1=Rare, 2=Epic, 3=Unique, 4=Legendary, 5=Degendary
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Optional, Any

import requests

from modules import logger, db
from modules.web3_utils import get_w3, get_account, send_contract_tx, close_web3_provider
from modules.portal_api import check_press_a_done

# ── Адреса контрактов ─────────────────────────────────────────────────────────

GACHA_CONTRACT        = "0xf1Be6F9d4ff40Cac47C620E058535451596a5aBD"
VAULT_ADDRESS         = "0xFc31B05318dcc603A6DAB21E519aCC639b205fb4"
USDSC_ADDRESS         = "0x3f99231dD03a9F0E7e3421c92B7b90fbe012985a"
GAME_NFT_CONTRACT     = "0x3B8BA2A8e7374C9Ff9BfD2ceC84CB115D1F2D4ee"
SALE_UPGRADE_CONTRACT = "0xF3d98F2B3403723d8417FeA42b4E166d599ea3f9"

# ── Константы ─────────────────────────────────────────────────────────────────

APE_TIER_INDEX    = 1  # Stone ticket → Ape (no VRF, 0.1% Unique)
HODLER_TIER_INDEX = 2  # Shell ticket → Hodler (Pyth VRF, 4% Unique)
ALPHA_TIER_INDEX  = 3  # Shell ticket → Alpha (Pyth VRF, 11% Unique)
DEGEN_TIER_INDEX  = 4  # Gold ticket  → Degen (Pyth VRF, 40% Unique)
LFG_TIER_INDEX    = 5  # Gold ticket  → LFG (Pyth VRF, 50% Unique)

USDSC_MIN_BALANCE = 250_000   # 0.25 USDSC — порог ниже которого докупаем

RUG_USDSC_BATCH_COST  = 10_000   # 0.01 USDSC за batch из 10 NFT — уточнить!
RUG_USDSC_SINGLE_COST = 1_000    # 0.001 USDSC за 1 NFT — уточнить!

ENTROPY_FEE_WEI = 8_000_000_000_000   # 0.000008 ETH — Pyth VRF fee (tier 2+)
SALE_ETH_VALUE  = 8_000_000_000_000   # 0.000008 ETH — VRF fee для SaleUpgrade

GRADE_UNIQUE = 3
GRADE_NAMES = {
    0: "Common",
    1: "Rare",
    2: "Epic",
    3: "Unique",
    4: "Legendary",
    5: "Degendary",
}
G = 300_000  # grade = tokenId // G

TRANSFER_SINGLE_TOPIC = "0xc3d58168c5ae7397731d063d5bbf3d657854427343f4c083240f7aacaa2d0f62"

VRF_WAIT_TIMEOUT  = 180
VRF_POLL_INTERVAL = 10

TOKEN_STONE = 1_800_000
TOKEN_SHELL = 1_800_001
TOKEN_GOLD  = 1_800_002

INITIAL_TICKET_TOKEN_ID = 1_800_000

MAX_SELL_BATCH = 200

BLOCKSCOUT_API = "https://soneium.blockscout.com/api/v2"

# Defaults (переопределяются из config.toml)
DEFAULT_USDSC_MIN_RAW = 250_000    # 0.25 USDSC
DEFAULT_USDSC_MAX_RAW = 1_000_000  # 1.00 USDSC
DEFAULT_RUG_TARGET    = 200
DEFAULT_MAX_CYCLES    = 100

# ── ABI ───────────────────────────────────────────────────────────────────────

GACHA_ABI = json.loads(
    """
[
  {
    "inputs": [{"type": "uint8", "name": "_tierIndex"},
               {"type": "bool",  "name": "_isBatch"}],
    "name": "requestGachaByTicket",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function"
  },
  {
    "inputs": [{"type": "uint8",   "name": "_tierIndex"},
               {"type": "bool",   "name": "_isBatch"},
               {"type": "uint256","name": "_deadline"},
               {"type": "bytes",  "name": "_permitSig"}],
    "name": "requestGachaByUSDSC",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function"
  },
  {
    "inputs": [],
    "name": "getGachaResult",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [],
    "name": "checkIn",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [{"type": "address", "name": "user"}],
    "name": "canCheckIn",
    "outputs": [{"type": "bool", "name": ""}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [{"type": "address", "name": ""}],
    "name": "gachaStates",
    "outputs": [
      {"type": "uint8",   "name": "status"},
      {"type": "uint8",   "name": "tierIndex"},
      {"type": "bool",    "name": "isBatch"},
      {"type": "uint256", "name": "randomSeed"}
    ],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [{"type": "address", "name": ""}],
    "name": "checkInStates",
    "outputs": [
      {"type": "uint32", "name": "lastCheckInDay"},
      {"type": "uint32", "name": "checkInCount"}
    ],
    "stateMutability": "view",
    "type": "function"
  }
]
"""
)

USDSC_ABI = json.loads(
    """
[
  {
    "inputs": [{"type": "address", "name": "account"}],
    "name": "balanceOf",
    "outputs": [{"type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [{"type": "address", "name": "owner"}],
    "name": "nonces",
    "outputs": [{"type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [],
    "name": "eip712Domain",
    "outputs": [
      {"type": "bytes1",  "name": "fields"},
      {"type": "string",  "name": "name"},
      {"type": "string",  "name": "version"},
      {"type": "uint256", "name": "chainId"},
      {"type": "address", "name": "verifyingContract"},
      {"type": "bytes32", "name": "salt"},
      {"type": "uint256[]","name": "extensions"}
    ],
    "stateMutability": "view",
    "type": "function"
  }
]
"""
)

SALE_ABI = json.loads(
    """
[
  {
    "inputs": [
      {"name": "_tokenIds", "type": "uint256[]"},
      {"name": "_amounts", "type": "uint256[]"}
    ],
    "name": "requestSale",
    "outputs": [],
    "stateMutability": "payable",
    "type": "function"
  },
  {
    "inputs": [],
    "name": "getSaleResult",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  },
  {
    "inputs": [{"name": "user", "type": "address"}],
    "name": "saleStates",
    "outputs": [
      {"name": "status", "type": "uint8"},
      {"name": "amountsByGrade", "type": "uint16[6]"},
      {"name": "randomSeed", "type": "uint256"}
    ],
    "stateMutability": "view",
    "type": "function"
  }
]
"""
)

GAME_NFT_ABI = json.loads(
    """
[
  {
    "inputs": [
      {"name": "account", "type": "address"},
      {"name": "id", "type": "uint256"}
    ],
    "name": "balanceOf",
    "outputs": [{"name": "", "type": "uint256"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "account", "type": "address"},
      {"name": "operator", "type": "address"}
    ],
    "name": "isApprovedForAll",
    "outputs": [{"name": "", "type": "bool"}],
    "stateMutability": "view",
    "type": "function"
  },
  {
    "inputs": [
      {"name": "operator", "type": "address"},
      {"name": "approved", "type": "bool"}
    ],
    "name": "setApprovalForAll",
    "outputs": [],
    "stateMutability": "nonpayable",
    "type": "function"
  }
]
"""
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fmt_usdsc(amount: float) -> str:
    return f"{amount:.6f}".rstrip("0").rstrip(".") or "0"


def _fmt_eth(amount: float) -> str:
    return f"{amount:.8f}".rstrip("0").rstrip(".") or "0"


def _get_gas_cost_from_receipt(receipt: dict) -> int:
    """gasUsed × effectiveGasPrice из receipt (возвращает 0 если поля нет)."""
    gas_used = int(receipt.get("gasUsed", 0))
    eff_price = int(receipt.get("effectiveGasPrice", 0))
    return gas_used * eff_price


def _inc_press_a_stats(
    eoa_address: str,
    *,
    spins: int = 0,
    usdsc_spent: int = 0,   # raw USDSC (6 decimals)
    eth_vrf_wei: int = 0,   # ETH отправленный как value (VRF fee)
    eth_gas_wei: int = 0,   # ETH потраченный на газ
) -> None:
    """
    Обновляет статистику Press A в quest_results.json:
      press_a_usdsc_spent — суммарный USDSC в USD-единицах (6 decimals → float)
      press_a_eth_spent   — суммарный ETH (VRF + газ) в ETH-единицах
    """
    acc = db.get_account_info(eoa_address) or {}
    new_spins = acc.get("press_a_spins_count", 0) + spins
    new_usdsc = float(acc.get("press_a_usdsc_spent", 0) or 0) + usdsc_spent / 1_000_000
    new_eth   = float(acc.get("press_a_eth_spent",   0) or 0) + (eth_vrf_wei + eth_gas_wei) / 1e18
    db.upsert_account(
        eoa_address,
        press_a_spins_count=new_spins,
        press_a_usdsc_spent=_fmt_usdsc(new_usdsc),
        press_a_eth_spent=_fmt_eth(new_eth),
    )


async def _action_delay(min_s: float = 8.0, max_s: float = 20.0) -> None:
    delay = random.uniform(min_s, max_s)
    logger.info(f"Пауза {delay:.0f}с между транзакциями...")
    await asyncio.sleep(delay)


def _decode_grade(token_id: int) -> int:
    return token_id // G


def _parse_all_grades_from_receipt(receipt: dict) -> list[int]:
    """Находит все TransferSingle события и возвращает список grades."""
    grades: list[int] = []
    logs = receipt.get("logs") or []
    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        t0_hex = t0.hex() if hasattr(t0, "hex") else str(t0)
        if not t0_hex.startswith("0x"):
            t0_hex = "0x" + t0_hex
        if t0_hex.lower() != TRANSFER_SINGLE_TOPIC.lower():
            continue
        data = log.get("data", b"")
        raw = (
            bytes(data)
            if hasattr(data, "__iter__") and not isinstance(data, (str, bytes))
            else (bytes.fromhex(str(data).replace("0x", "")) if isinstance(data, str) else bytes(data))
        )
        if len(raw) >= 32:
            token_id = int.from_bytes(raw[0:32], "big")
            grades.append(_decode_grade(token_id))
    return grades


def _parse_token_id_from_receipt(receipt: dict) -> Optional[int]:
    """Читает tokenId из первого события TransferSingle в receipt."""
    logs = receipt.get("logs") or []
    for log in logs:
        topics = log.get("topics") or []
        if not topics:
            continue
        t0 = topics[0]
        t0_hex = t0.hex() if hasattr(t0, "hex") else str(t0)
        if not t0_hex.startswith("0x"):
            t0_hex = "0x" + t0_hex
        if t0_hex.lower() == TRANSFER_SINGLE_TOPIC.lower():
            data = log.get("data", b"")
            raw = (
                bytes(data)
                if hasattr(data, "__iter__") and not isinstance(data, (str, bytes))
                else (bytes.fromhex(str(data).replace("0x", "")) if isinstance(data, str) else bytes(data))
            )
            if len(raw) >= 32:
                return int.from_bytes(raw[0:32], "big")
    return None


async def _get_receipt(w3, tx_hash: str):
    """Возвращает receipt по tx_hash или None при ошибке."""
    try:
        await asyncio.sleep(3)
        tx_bytes = bytes.fromhex(tx_hash[2:] if tx_hash.startswith("0x") else tx_hash)
        return await w3.eth.get_transaction_receipt(tx_bytes)
    except Exception as e:
        logger.warning(f"Не удалось получить receipt: {e}")
        return None


async def _get_gacha_status(gacha, addr_cs: str) -> int:
    try:
        state = await gacha.functions.gachaStates(addr_cs).call()
        return int(state[0])
    except Exception as e:
        logger.warning(f"gachaStates ошибка: {e}")
        return 0


async def _wait_vrf(gacha, addr_cs: str) -> bool:
    logger.info(f"[{addr_cs[:8]}] Ожидание VRF (до {VRF_WAIT_TIMEOUT}с)...")
    elapsed = 0
    while elapsed < VRF_WAIT_TIMEOUT:
        await asyncio.sleep(VRF_POLL_INTERVAL)
        elapsed += VRF_POLL_INTERVAL
        status = await _get_gacha_status(gacha, addr_cs)
        if status == 2:
            logger.success(f"[{addr_cs[:8]}] VRF callback получен (status=2)")
            return True
        if status == 0:
            logger.info(f"[{addr_cs[:8]}] VRF уже обработан (status=0)")
            return False
        logger.info(f"[{addr_cs[:8]}] VRF ожидание... ({elapsed}/{VRF_WAIT_TIMEOUT}с)")
    logger.warning(f"[{addr_cs[:8]}] VRF timeout")
    return False


async def _get_usdsc_balance(usdsc, addr_cs: str) -> int:
    try:
        return await usdsc.functions.balanceOf(addr_cs).call()
    except Exception as e:
        logger.warning(f"[{addr_cs[:8]}] USDSC balanceOf ошибка: {e}")
        return 0


async def _get_eip712_domain(usdsc) -> dict:
    info = await usdsc.functions.eip712Domain().call()
    return {
        "name": info[1],
        "version": info[2],
        "chainId": info[3],
        "verifyingContract": info[4],
    }


def _sign_usdsc_permit(
    account,
    domain: dict,
    nonce: int,
    cost: int,
    deadline: int,
    addr_cs: str,
) -> bytes:
    """Подписывает EIP-2612 permit для USDSC. Spender = Vault."""
    from eth_account.messages import encode_typed_data

    msg = encode_typed_data(
        domain_data=domain,
        message_types={
            "Permit": [
                {"name": "owner",    "type": "address"},
                {"name": "spender",  "type": "address"},
                {"name": "value",    "type": "uint256"},
                {"name": "nonce",    "type": "uint256"},
                {"name": "deadline", "type": "uint256"},
            ]
        },
        message_data={
            "owner":    addr_cs,
            "spender":  VAULT_ADDRESS,
            "value":    cost,
            "nonce":    nonce,
            "deadline": deadline,
        },
    )
    signed = account.sign_message(msg)
    return bytes(signed.signature)


async def _get_gacha_result_with_grade(
    w3, account, gacha, addr_cs: str, gas_multiplier: float
) -> tuple[Optional[int], int]:
    """
    Вызывает getGachaResult().
    Возвращает (grade, gas_cost_wei). grade=None если tokenId не найден.
    """
    tx_hash = await send_contract_tx(
        w3, account,
        gacha.functions.getGachaResult(),
        value=0,
        action=f"[{addr_cs[:8]}] getGachaResult",
        gas_multiplier=gas_multiplier,
    )
    receipt = await _get_receipt(w3, tx_hash)
    gas_cost = _get_gas_cost_from_receipt(dict(receipt)) if receipt else 0
    if receipt:
        token_id = _parse_token_id_from_receipt(dict(receipt))
        if token_id is not None:
            grade = _decode_grade(token_id)
            grade_name = GRADE_NAMES.get(grade, str(grade))
            logger.info(
                f"[{addr_cs[:8]}] getGachaResult: tokenId={token_id} "
                f"→ grade={grade} ({grade_name})"
            )
            return grade, gas_cost
    logger.warning(f"[{addr_cs[:8]}] getGachaResult: tokenId не найден в receipt")
    return None, gas_cost


async def _get_sale_status(sale_contract, addr_cs: str) -> int:
    try:
        state = await sale_contract.functions.saleStates(addr_cs).call()
        return int(state[0])
    except Exception:
        return 0


async def _wait_sale_vrf(sale_contract, addr_cs: str) -> bool:
    logger.info(f"[{addr_cs[:8]}] Ожидание Sale VRF (до {VRF_WAIT_TIMEOUT}с)...")
    await asyncio.sleep(15)  # дать контракту время перейти status 0→1
    elapsed = 15
    while elapsed < VRF_WAIT_TIMEOUT:
        await asyncio.sleep(VRF_POLL_INTERVAL)
        elapsed += VRF_POLL_INTERVAL
        status = await _get_sale_status(sale_contract, addr_cs)
        if status == 2:
            return True
        if status == 0:
            logger.info(f"[{addr_cs[:8]}] Sale VRF: status=0 (VRF уже обработан или не начат)")
            return False
        logger.info(f"[{addr_cs[:8]}] Sale VRF ожидание... ({elapsed}/{VRF_WAIT_TIMEOUT}с)")
    return False


async def _resolve_pending_sale(
    w3, account, sale, addr_cs: str, eoa_address: str, gas_multiplier: float
) -> bool:
    """
    Разрешает pending состояние SaleUpgrade.
    - status==1: ждёт VRF, затем getSaleResult
    - status==2: вызывает getSaleResult
    - status==0: ничего не делать
    Возвращает True если была разрешена pending продажа.
    """
    status = await _get_sale_status(sale, addr_cs)
    if status == 0:
        # status=0 может быть ложным — пробуем getSaleResult через симуляцию
        try:
            result_hash = await send_contract_tx(
                w3, account,
                sale.functions.getSaleResult(),
                value=0,
                action=f"[{addr_cs[:8]}] getSaleResult (status=0 probe)",
                gas_multiplier=gas_multiplier,
            )
            result_receipt = await _get_receipt(w3, result_hash)
            result_gas = _get_gas_cost_from_receipt(dict(result_receipt)) if result_receipt else 0
            _inc_press_a_stats(eoa_address, eth_gas_wei=result_gas)
            logger.success(f"[{addr_cs[:8]}] Pending sale разрешена (status=0 probe)")
            return True
        except Exception:
            return False  # симуляция упала — продажи действительно нет
    if status == 1:
        logger.info(f"[{addr_cs[:8]}] Pending sale VRF (status=1), ждём...")
        if not await _wait_sale_vrf(sale, addr_cs):
            logger.warning(f"[{addr_cs[:8]}] Sale VRF timeout при resolve")
            return False
        # after VRF — status should be 2
    # status == 2: VRF ready
    logger.info(f"[{addr_cs[:8]}] Sale VRF ready (status=2), getSaleResult...")
    try:
        result_hash = await send_contract_tx(
            w3, account,
            sale.functions.getSaleResult(),
            value=0,
            action=f"[{addr_cs[:8]}] getSaleResult (resolve pending)",
            gas_multiplier=gas_multiplier,
        )
        result_receipt = await _get_receipt(w3, result_hash)
        result_gas = _get_gas_cost_from_receipt(dict(result_receipt)) if result_receipt else 0
        _inc_press_a_stats(eoa_address, eth_gas_wei=result_gas)
        logger.success(f"[{addr_cs[:8]}] Pending sale разрешена")
    except Exception as e:
        logger.warning(f"[{addr_cs[:8]}] getSaleResult (pending resolve) ошибка: {e}")
    return True


def _batch_for_sell(inv: list[tuple[int, int]], max_total: int = MAX_SELL_BATCH) -> list[tuple[int, int]]:
    result: list[tuple[int, int]] = []
    total = 0
    for tid, amt in inv:
        take = min(amt, max_total - total)
        if take > 0:
            result.append((tid, take))
            total += take
        if total >= max_total:
            break
    return result


def _get_on_chain_item_inventory(address: str, proxy: Optional[str] = None) -> list[tuple[int, int]]:
    """Возвращает балансы item-NFT (tokenId < INITIAL_TICKET_TOKEN_ID) через Blockscout."""
    from web3 import Web3

    addr_cs = Web3.to_checksum_address(address)
    url = (
        f"{BLOCKSCOUT_API}/addresses/{addr_cs}/token-transfers"
        f"?type=ERC-1155&token={GAME_NFT_CONTRACT}"
    )
    proxies = {"http": proxy, "https": proxy} if proxy else None
    balances: dict[int, int] = {}

    try:
        next_params: Optional[dict[str, Any]] = None
        while True:
            req_url = url
            if next_params:
                req_url = f"{url}&" + "&".join(f"{k}={v}" for k, v in next_params.items())
            resp = requests.get(req_url, proxies=proxies, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            items = data.get("items") or []

            for item in items:
                total_obj = item.get("total") or {}
                token_id = int(total_obj.get("token_id") or 0)
                value = int(total_obj.get("value") or item.get("value", 0) or 0)
                from_addr = (item.get("from") or {}).get("hash", "")
                to_addr   = (item.get("to")   or {}).get("hash", "")

                if token_id < INITIAL_TICKET_TOKEN_ID:
                    if to_addr and to_addr.lower() == addr_cs.lower():
                        balances[token_id] = balances.get(token_id, 0) + value
                    if from_addr and from_addr.lower() == addr_cs.lower():
                        balances[token_id] = balances.get(token_id, 0) - value

            next_params = data.get("next_page_params")
            if not next_params:
                break

        return [(tid, amt) for tid, amt in balances.items() if amt > 0]
    except Exception as e:
        logger.warning(f"[{address}] Blockscout item inventory failed: {e}")
        return []


async def _ensure_game_nft_approvals(
    w3, account, addr_cs: str, gas_multiplier: float
) -> None:
    from web3 import AsyncWeb3

    game_nft = w3.eth.contract(
        address=AsyncWeb3.to_checksum_address(GAME_NFT_CONTRACT), abi=GAME_NFT_ABI
    )
    for operator, name in [
        (SALE_UPGRADE_CONTRACT, "SaleUpgrade"),
        (GACHA_CONTRACT,        "Gacha"),
    ]:
        try:
            approved = await game_nft.functions.isApprovedForAll(addr_cs, operator).call()
            if not approved:
                logger.info(f"[{addr_cs[:8]}] setApprovalForAll({name})...")
                await send_contract_tx(
                    w3, account,
                    game_nft.functions.setApprovalForAll(
                        AsyncWeb3.to_checksum_address(operator), True
                    ),
                    value=0,
                    action=f"[{addr_cs[:8]}] setApprovalForAll({name})",
                    gas_multiplier=gas_multiplier,
                )
                await _action_delay()
        except Exception as e:
            logger.warning(f"[{addr_cs[:8]}] setApprovalForAll({name}): {e}")


async def _calc_eth_for_usdsc(
    target_usdsc_raw: int,
    lifi_api_key: Optional[str],
    proxy: Optional[str],
) -> int:
    """Вычисляет сколько wei нужно чтобы получить target_usdsc_raw USDSC через LI.FI."""
    from modules.lifi_swap import _lifi_quote

    REF_ETH = 1_000_000_000_000_000  # 0.001 ETH — референсная сумма для котировки
    params = {
        "fromChain": "1868",
        "toChain":   "1868",
        "fromToken": "0x0000000000000000000000000000000000000000",
        "toToken":   USDSC_ADDRESS,
        "fromAmount": str(REF_ETH),
        "fromAddress": "0x0000000000000000000000000000000000000001",
        "slippage": "0.02",
        "order": "CHEAPEST",
    }
    quote = await _lifi_quote(params, lifi_api_key, proxy)
    step = quote.get("step") or quote
    estimate = step.get("estimate", {})
    ref_usdsc = int(estimate.get("toAmount", 0))
    if ref_usdsc <= 0:
        raise RuntimeError("LI.FI: не удалось получить котировку ETH→USDSC")
    eth_needed = int(REF_ETH * target_usdsc_raw / ref_usdsc)
    logger.info(
        f"LI.FI котировка: {REF_ETH/1e18:.4f} ETH → {ref_usdsc/1e6:.4f} USDSC, "
        f"нужно {eth_needed/1e18:.6f} ETH для {target_usdsc_raw/1e6:.4f} USDSC"
    )
    return eth_needed


async def _mint_rug_usdsc(
    w3,
    account,
    addr_cs: str,
    eoa_address: str,
    gacha,
    usdsc,
    current_count: int,
    target: int,
    gas_multiplier: float,
    lifi_api_key: Optional[str],
    proxy: Optional[str],
) -> bool:
    """
    Минтит Rug NFT через USDSC (requestGachaByUSDSC) до target штук.
    Возвращает True если по пути выпал Unique.
    """
    need = target - current_count
    logger.info(
        f"[{addr_cs[:8]}] Rug bootstrap: нужно {need} NFT "
        f"(текущих {current_count}, цель {target})"
    )

    domain = await _get_eip712_domain(usdsc)

    while need > 0:
        use_batch = need >= 10

        if use_batch:
            # USDSC batch (10 NFT за раз)
            cost  = RUG_USDSC_BATCH_COST
            count = 10
            deadline   = int(time.time()) + 3600
            nonce      = await usdsc.functions.nonces(addr_cs).call()
            permit_sig = _sign_usdsc_permit(account, domain, nonce, cost, deadline, addr_cs)
            logger.info(
                f"[{addr_cs[:8]}] Rug USDSC batch(10), cost={cost/1e6:.3f} USDSC, нужно ещё {need}"
            )
            try:
                tx_hash = await send_contract_tx(
                    w3, account,
                    gacha.functions.requestGachaByUSDSC(0, True, deadline, permit_sig),
                    value=0,
                    action=f"[{addr_cs[:8]}] requestGachaByUSDSC(Rug,batch)",
                    gas_multiplier=gas_multiplier,
                )
                receipt = await _get_receipt(w3, tx_hash)
                gas_cost = _get_gas_cost_from_receipt(dict(receipt)) if receipt else 0
                _inc_press_a_stats(eoa_address, spins=count, usdsc_spent=cost, eth_gas_wei=gas_cost)
                if receipt:
                    grades = _parse_all_grades_from_receipt(dict(receipt))
                    if GRADE_UNIQUE in grades:
                        logger.success(f"[{addr_cs[:8]}] UNIQUE выпал при Rug bootstrap!")
                        return True
            except Exception as e:
                logger.warning(f"[{addr_cs[:8]}] Rug USDSC batch ошибка: {e} — пропуск батча")
                await _action_delay()
                continue
        else:
            # Free Path (need < 10) — requestGachaByTicket(0, False), value=0, без USDSC
            count = 1
            logger.info(f"[{addr_cs[:8]}] Rug Free mint (need<10), нужно ещё {need}")
            try:
                tx_hash = await send_contract_tx(
                    w3, account,
                    gacha.functions.requestGachaByTicket(0, False),
                    value=0,
                    action=f"[{addr_cs[:8]}] requestGachaByTicket(Rug,free)",
                    gas_multiplier=gas_multiplier,
                )
                receipt = await _get_receipt(w3, tx_hash)
                gas_cost = _get_gas_cost_from_receipt(dict(receipt)) if receipt else 0
                _inc_press_a_stats(eoa_address, spins=count, eth_gas_wei=gas_cost)
                if receipt:
                    grades = _parse_all_grades_from_receipt(dict(receipt))
                    if GRADE_UNIQUE in grades:
                        logger.success(f"[{addr_cs[:8]}] UNIQUE выпал при Rug Free mint!")
                        return True
            except Exception as e:
                logger.warning(f"[{addr_cs[:8]}] Rug Free mint ошибка: {e} — пропуск")
                await _action_delay()
                continue

        need -= count
        await _action_delay()

    logger.info(f"[{addr_cs[:8]}] Rug bootstrap завершён, Unique не выпал")
    return False


async def _spin_all_shell(
    w3,
    account,
    addr_cs: str,
    eoa_address: str,
    gacha,
    game_nft,
    gas_multiplier: float,
) -> bool:
    """
    Крутит все Shell тикеты (tier=2, Hodler, Pyth VRF, 4% Unique).
    Batch по 10 если >= 10 (экономия VRF), иначе по одному.
    Возвращает True если выпал Unique.
    """
    shell_bal = await game_nft.functions.balanceOf(addr_cs, TOKEN_SHELL).call()
    if shell_bal == 0:
        return False
    logger.info(f"[{addr_cs[:8]}] Shell спины: {shell_bal} Shell")

    while shell_bal > 0:
        use_batch = shell_bal >= 10
        count = 10 if use_batch else 1
        logger.info(
            f"[{addr_cs[:8]}] Shell spin: {'batch(10)' if use_batch else 'single(1)'}, "
            f"осталось Shell={shell_bal}"
        )
        try:
            req_hash = await send_contract_tx(
                w3, account,
                gacha.functions.requestGachaByTicket(HODLER_TIER_INDEX, use_batch),
                value=ENTROPY_FEE_WEI,
                action=f"[{addr_cs[:8]}] requestGachaByTicket(Shell,{'batch' if use_batch else 'single'})",
                gas_multiplier=gas_multiplier,
            )
            req_receipt = await _get_receipt(w3, req_hash)
            req_gas = _get_gas_cost_from_receipt(dict(req_receipt)) if req_receipt else 0
            _inc_press_a_stats(eoa_address, spins=count, eth_vrf_wei=ENTROPY_FEE_WEI, eth_gas_wei=req_gas)

            if await _wait_vrf(gacha, addr_cs):
                result_tx = await send_contract_tx(
                    w3, account,
                    gacha.functions.getGachaResult(),
                    value=0,
                    action=f"[{addr_cs[:8]}] getGachaResult(Shell,{'batch' if use_batch else 'single'})",
                    gas_multiplier=gas_multiplier,
                )
                result_receipt = await _get_receipt(w3, result_tx)
                result_gas = _get_gas_cost_from_receipt(dict(result_receipt)) if result_receipt else 0
                _inc_press_a_stats(eoa_address, eth_gas_wei=result_gas)
                if result_receipt:
                    grades = _parse_all_grades_from_receipt(dict(result_receipt))
                    if grades:
                        grade_names = [GRADE_NAMES.get(g, str(g)) for g in grades]
                        logger.info(f"[{addr_cs[:8]}] Shell grades: {grade_names}")
                    if GRADE_UNIQUE in grades:
                        logger.success(f"[{addr_cs[:8]}] UNIQUE выпал при Shell спине!")
                        return True
        except Exception as e:
            logger.warning(f"[{addr_cs[:8]}] Shell spin ошибка: {e} — пропуск")

        shell_bal -= count
        await _action_delay()

    return False


async def _spin_all_stone(
    w3,
    account,
    addr_cs: str,
    eoa_address: str,
    gacha,
    game_nft,
    gas_multiplier: float,
) -> bool:
    """
    Крутит все Stone тикеты (tier=1, Ape, no VRF, 0.1% Unique).
    Batch по 10 если >= 10, иначе по одному.
    Возвращает True если выпал Unique.
    """
    stone_bal = await game_nft.functions.balanceOf(addr_cs, TOKEN_STONE).call()
    if stone_bal == 0:
        return False
    logger.info(f"[{addr_cs[:8]}] Stone спины: {stone_bal} Stone")

    while stone_bal > 0:
        use_batch = stone_bal >= 10
        count = 10 if use_batch else 1
        logger.info(
            f"[{addr_cs[:8]}] Stone spin: {'batch(10)' if use_batch else 'single(1)'}, "
            f"осталось Stone={stone_bal}"
        )
        try:
            tx_hash = await send_contract_tx(
                w3, account,
                gacha.functions.requestGachaByTicket(APE_TIER_INDEX, use_batch),
                value=0,
                action=f"[{addr_cs[:8]}] requestGachaByTicket(Stone,{'batch' if use_batch else 'single'})",
                gas_multiplier=gas_multiplier,
            )

            receipt = await _get_receipt(w3, tx_hash)
            gas_cost = _get_gas_cost_from_receipt(dict(receipt)) if receipt else 0
            _inc_press_a_stats(eoa_address, spins=count, eth_gas_wei=gas_cost)

            if receipt:
                grades = _parse_all_grades_from_receipt(dict(receipt))
                if grades:
                    grade_names = [GRADE_NAMES.get(g, str(g)) for g in grades]
                    logger.info(f"[{addr_cs[:8]}] Stone grades: {grade_names}")
                if GRADE_UNIQUE in grades:
                    logger.success(f"[{addr_cs[:8]}] UNIQUE выпал при Stone спине!")
                    return True
        except Exception as e:
            logger.warning(f"[{addr_cs[:8]}] Stone spin ошибка: {e} — пропуск")

        stone_bal -= count
        await _action_delay()

    return False


async def _spin_by_tickets(
    w3,
    account,
    addr_cs: str,
    eoa_address: str,
    *,
    gacha,
    tier_index: int,
    is_batch: bool,
    count_spins: int,
    vrf_required: bool,
    gas_multiplier: float,
    label: str,
) -> bool:
    """
    Делает спин(ы) за тикеты через requestGachaByTicket.
    Возвращает True если выпал Unique.
    """
    value = ENTROPY_FEE_WEI if vrf_required else 0
    try:
        req_hash = await send_contract_tx(
            w3, account,
            gacha.functions.requestGachaByTicket(tier_index, is_batch),
            value=value,
            action=f"[{addr_cs[:8]}] requestGachaByTicket({label},{'batch' if is_batch else 'single'})",
            gas_multiplier=gas_multiplier,
        )
        req_receipt = await _get_receipt(w3, req_hash)
        req_gas = _get_gas_cost_from_receipt(dict(req_receipt)) if req_receipt else 0
        _inc_press_a_stats(
            eoa_address,
            spins=count_spins,
            eth_vrf_wei=value,
            eth_gas_wei=req_gas,
        )

        if not vrf_required:
            if req_receipt:
                grades = _parse_all_grades_from_receipt(dict(req_receipt))
                if grades:
                    grade_names = [GRADE_NAMES.get(g, str(g)) for g in grades]
                    logger.info(f"[{addr_cs[:8]}] {label} grades: {grade_names}")
                if GRADE_UNIQUE in grades:
                    logger.success(f"[{addr_cs[:8]}] UNIQUE выпал при {label} спине!")
                    return True
            return False

        # VRF path (tier 2+)
        if await _wait_vrf(gacha, addr_cs):
            result_hash = await send_contract_tx(
                w3, account,
                gacha.functions.getGachaResult(),
                value=0,
                action=f"[{addr_cs[:8]}] getGachaResult({label},{'batch' if is_batch else 'single'})",
                gas_multiplier=gas_multiplier,
            )
            result_receipt = await _get_receipt(w3, result_hash)
            result_gas = _get_gas_cost_from_receipt(dict(result_receipt)) if result_receipt else 0
            _inc_press_a_stats(eoa_address, eth_gas_wei=result_gas)
            if result_receipt:
                grades = _parse_all_grades_from_receipt(dict(result_receipt))
                if grades:
                    grade_names = [GRADE_NAMES.get(g, str(g)) for g in grades]
                    logger.info(f"[{addr_cs[:8]}] {label} grades: {grade_names}")
                if GRADE_UNIQUE in grades:
                    logger.success(f"[{addr_cs[:8]}] UNIQUE выпал при {label} спине!")
                    return True
        return False
    except Exception as e:
        logger.warning(f"[{addr_cs[:8]}] {label} spin ошибка: {e} — пропуск")
        return False


async def _pre_spin_best_tickets(
    w3,
    account,
    addr_cs: str,
    eoa_address: str,
    *,
    gacha,
    game_nft,
    gas_multiplier: float,
) -> bool:
    """
    Пытается получить Unique до «тяжёлого» флоу, тратя имеющиеся тикеты по приоритету:
      Gold → Shell → Stone (LFG/Degen/Alpha/Hodler/Ape).
    Возвращает True если выпал Unique.
    """
    try:
        gold = int(await game_nft.functions.balanceOf(addr_cs, TOKEN_GOLD).call())
        shell = int(await game_nft.functions.balanceOf(addr_cs, TOKEN_SHELL).call())
        stone = int(await game_nft.functions.balanceOf(addr_cs, TOKEN_STONE).call())
    except Exception as e:
        logger.warning(f"[{addr_cs[:8]}] balanceOf тикетов ошибка: {e}")
        return False

    if gold <= 0 and shell <= 0 and stone <= 0:
        return False

    logger.info(f"[{addr_cs[:8]}] Тикеты перед циклом: Gold={gold}, Shell={shell}, Stone={stone}")

    # Внутренний цикл: пробуем тратить тикеты, пока есть подходящие комбинации
    while True:
        # Gold
        if gold >= 10:
            if await _spin_by_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                tier_index=LFG_TIER_INDEX,
                is_batch=True,
                count_spins=10,
                vrf_required=True,
                gas_multiplier=gas_multiplier,
                label="Gold/LFG",
            ):
                return True
            gold -= 10
            await _action_delay()
            continue
        if gold >= 1:
            if await _spin_by_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                tier_index=DEGEN_TIER_INDEX,
                is_batch=False,
                count_spins=1,
                vrf_required=True,
                gas_multiplier=gas_multiplier,
                label="Gold/Degen",
            ):
                return True
            gold -= 1
            await _action_delay()
            continue

        # Shell
        if shell >= 5:
            if await _spin_by_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                tier_index=ALPHA_TIER_INDEX,
                is_batch=False,
                count_spins=1,
                vrf_required=True,
                gas_multiplier=gas_multiplier,
                label="Shell/Alpha",
            ):
                return True
            shell -= 5
            await _action_delay()
            continue
        if shell >= 1:
            if await _spin_by_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                tier_index=HODLER_TIER_INDEX,
                is_batch=False,
                count_spins=1,
                vrf_required=True,
                gas_multiplier=gas_multiplier,
                label="Shell/Hodler",
            ):
                return True
            shell -= 1
            await _action_delay()
            continue

        # Stone
        if stone >= 10:
            if await _spin_by_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                tier_index=APE_TIER_INDEX,
                is_batch=True,
                count_spins=10,
                vrf_required=False,
                gas_multiplier=gas_multiplier,
                label="Stone/Ape",
            ):
                return True
            stone -= 10
            await _action_delay()
            continue
        if stone >= 1:
            if await _spin_by_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                tier_index=APE_TIER_INDEX,
                is_batch=False,
                count_spins=1,
                vrf_required=False,
                gas_multiplier=gas_multiplier,
                label="Stone/Ape",
            ):
                return True
            stone -= 1
            await _action_delay()
            continue

        break

    return False


async def _sell_all_items(
    w3,
    account,
    addr_cs: str,
    eoa_address: str,
    sale,
    gas_multiplier: float,
    proxy: Optional[str],
) -> None:
    """Продаёт все item-NFT (tokenId < INITIAL_TICKET_TOKEN_ID) через SaleUpgrade."""
    inventory = await asyncio.to_thread(_get_on_chain_item_inventory, eoa_address, proxy)
    inventory = [(tid, amt) for tid, amt in inventory if tid < INITIAL_TICKET_TOKEN_ID]

    if not inventory:
        logger.info(f"[{addr_cs[:8]}] Нет item-NFT для продажи")
        return

    total_items = sum(a for _, a in inventory)
    logger.info(f"[{addr_cs[:8]}] Продажа {total_items} item-NFT...")

    while inventory:
        batch = _batch_for_sell(inventory, MAX_SELL_BATCH)
        if not batch:
            break

        by_id = dict(inventory)
        for tid, amt in batch:
            by_id[tid] = by_id.get(tid, 0) - amt
        inventory = [(t, a) for t, a in by_id.items() if a > 0]

        token_ids = [t for t, _ in batch]
        amounts   = [a for _, a in batch]
        total_sell = sum(a for _, a in batch)

        logger.info(f"[{addr_cs[:8]}] requestSale {total_sell} шт ({len(batch)} типов)...")
        try:
            # Разрешить любую pending продажу перед новой
            await _resolve_pending_sale(w3, account, sale, addr_cs, eoa_address, gas_multiplier)

            sale_hash = await send_contract_tx(
                w3, account,
                sale.functions.requestSale(token_ids, amounts),
                value=SALE_ETH_VALUE,
                action=f"[{addr_cs[:8]}] requestSale",
                gas_multiplier=gas_multiplier,
            )
            sale_receipt = await _get_receipt(w3, sale_hash)
            sale_gas = _get_gas_cost_from_receipt(dict(sale_receipt)) if sale_receipt else 0
            _inc_press_a_stats(eoa_address, eth_vrf_wei=SALE_ETH_VALUE, eth_gas_wei=sale_gas)

            if not await _wait_sale_vrf(sale, addr_cs):
                # status=0 или реальный timeout — пробуем getSaleResult через симуляцию
                logger.warning(f"[{addr_cs[:8]}] Sale VRF: status=0/timeout — fallback getSaleResult")
                try:
                    result_hash = await send_contract_tx(
                        w3, account,
                        sale.functions.getSaleResult(),
                        value=0,
                        action=f"[{addr_cs[:8]}] getSaleResult (fallback)",
                        gas_multiplier=gas_multiplier,
                    )
                    result_receipt = await _get_receipt(w3, result_hash)
                    result_gas = _get_gas_cost_from_receipt(dict(result_receipt)) if result_receipt else 0
                    _inc_press_a_stats(eoa_address, eth_gas_wei=result_gas)
                except Exception as e:
                    logger.warning(f"[{addr_cs[:8]}] getSaleResult fallback ошибка: {e} — прерываем продажи")
                    break
                # getSaleResult прошёл → продажа разрешена, продолжаем цикл
                continue

            result_hash = await send_contract_tx(
                w3, account,
                sale.functions.getSaleResult(),
                value=0,
                action=f"[{addr_cs[:8]}] getSaleResult",
                gas_multiplier=gas_multiplier,
            )
            result_receipt = await _get_receipt(w3, result_hash)
            result_gas = _get_gas_cost_from_receipt(dict(result_receipt)) if result_receipt else 0
            _inc_press_a_stats(eoa_address, eth_gas_wei=result_gas)
        except Exception as e:
            logger.warning(f"[{addr_cs[:8]}] Продажа ошибка: {e} — прерываем продажи")
            break
        await _action_delay()


# ── Основная async сессия ─────────────────────────────────────────────────────

async def _run_press_a_session(
    private_key: str,
    eoa_address: str,
    rpc_url: str,
    proxy: Optional[str] = None,
    disable_ssl: bool = True,
    gas_multiplier: float = 1.2,
    lifi_api_key: Optional[str] = None,
    config: Optional[dict] = None,
) -> bool:
    """
    Полный запуск Press A: bootstrap + основной цикл.
    Возвращает True если Unique NFT получен.
    """
    from web3 import AsyncWeb3

    cfg = config or {}
    usdsc_min_raw = cfg.get("press_a_usdsc_min_raw", DEFAULT_USDSC_MIN_RAW)
    usdsc_max_raw = cfg.get("press_a_usdsc_max_raw", DEFAULT_USDSC_MAX_RAW)
    rug_target    = cfg.get("press_a_rug_target",    DEFAULT_RUG_TARGET)
    max_cycles    = cfg.get("press_a_max_cycles",    DEFAULT_MAX_CYCLES)
    checkin_only  = bool(cfg.get("press_a_checkin_only", False))

    w3 = get_w3(rpc_url, proxy=proxy, disable_ssl=disable_ssl)
    try:
        account  = get_account(private_key)
        addr_cs  = AsyncWeb3.to_checksum_address(eoa_address)

        gacha    = w3.eth.contract(address=AsyncWeb3.to_checksum_address(GACHA_CONTRACT),        abi=GACHA_ABI)
        sale     = w3.eth.contract(address=AsyncWeb3.to_checksum_address(SALE_UPGRADE_CONTRACT), abi=SALE_ABI)
        game_nft = w3.eth.contract(address=AsyncWeb3.to_checksum_address(GAME_NFT_CONTRACT),     abi=GAME_NFT_ABI)
        usdsc    = w3.eth.contract(address=AsyncWeb3.to_checksum_address(USDSC_ADDRESS),         abi=USDSC_ABI)

        # ── Режим: только checkIn (без bootstrap/spin/sell) ───────────────────
        if checkin_only:
            try:
                done_api = await asyncio.to_thread(check_press_a_done, eoa_address)
                if done_api is True:
                    logger.info(f"[{eoa_address[:8]}] Press A Unique уже получен (portal) — checkIn не требуется")
                    return True
            except Exception as e:
                logger.warning(f"[{eoa_address[:8]}] Portal check_press_a_done ошибка: {e} — продолжаем checkIn")

            try:
                can_check = await gacha.functions.canCheckIn(addr_cs).call()
                if can_check:
                    await send_contract_tx(
                        w3, account,
                        gacha.functions.checkIn(),
                        value=0,
                        action=f"[{eoa_address[:8]}] checkIn",
                        gas_multiplier=gas_multiplier,
                    )
                    ci_state = await gacha.functions.checkInStates(addr_cs).call()
                    logger.success(f"[{eoa_address[:8]}] checkIn выполнен: count={ci_state[1]}")
                else:
                    ci_state = await gacha.functions.checkInStates(addr_cs).call()
                    logger.info(f"[{eoa_address[:8]}] checkIn сегодня выполнен (count={ci_state[1]})")
            except Exception as e:
                logger.warning(f"[{eoa_address[:8]}] checkIn ошибка: {e}")

            return True

        # ── Обработка pending VRF из предыдущего запуска ─────────────────────
        status = await _get_gacha_status(gacha, addr_cs)
        if status == 1:
            logger.info(f"[{eoa_address[:8]}] VRF callback ещё не пришёл, ждём...")
            if await _wait_vrf(gacha, addr_cs):
                status = 2
            else:
                logger.warning(f"[{eoa_address[:8]}] VRF timeout, повторить позже")
                return False
        if status == 2:
            logger.info(f"[{eoa_address[:8]}] VRF fulfilled, вызываем getGachaResult...")
            grade, gas_cost = await _get_gacha_result_with_grade(w3, account, gacha, addr_cs, gas_multiplier)
            _inc_press_a_stats(eoa_address, eth_gas_wei=gas_cost)
            if grade == GRADE_UNIQUE:
                logger.success(f"[{eoa_address[:8]}] UNIQUE NFT получен через pending VRF!")
                db.upsert_account(eoa_address, press_a_done=True)
                return True
            await _action_delay()

        # ── Обработка pending Sale VRF из предыдущего запуска ────────────────
        try:
            if await _resolve_pending_sale(w3, account, sale, addr_cs, eoa_address, gas_multiplier):
                await _action_delay()
        except Exception as e:
            logger.warning(f"[{eoa_address[:8]}] Resolve pending sale ошибка: {e}")

        # ── 1. checkIn ────────────────────────────────────────────────────────
        try:
            can_check = await gacha.functions.canCheckIn(addr_cs).call()
            if can_check:
                await send_contract_tx(
                    w3, account,
                    gacha.functions.checkIn(),
                    value=0,
                    action=f"[{eoa_address[:8]}] checkIn",
                    gas_multiplier=gas_multiplier,
                )
                ci_state = await gacha.functions.checkInStates(addr_cs).call()
                logger.success(f"[{eoa_address[:8]}] checkIn выполнен: count={ci_state[1]}")
                await _action_delay()
            else:
                ci_state = await gacha.functions.checkInStates(addr_cs).call()
                logger.info(f"[{eoa_address[:8]}] checkIn уже выполнен (count={ci_state[1]})")
        except Exception as e:
            logger.warning(f"[{eoa_address[:8]}] checkIn ошибка: {e}")

        # ── 2. Approvals (нужны для списания тикетов/Sale) ────────────────────
        await _ensure_game_nft_approvals(w3, account, addr_cs, gas_multiplier)

        # ── 2.1 Pre-spin (Gold → Shell → Stone) ──────────────────────────────
        try:
            if await _pre_spin_best_tickets(
                w3, account, addr_cs, eoa_address,
                gacha=gacha,
                game_nft=game_nft,
                gas_multiplier=gas_multiplier,
            ):
                db.upsert_account(eoa_address, press_a_done=True)
                return True
        except Exception as e:
            logger.warning(f"[{eoa_address[:8]}] Pre-spin тикетов ошибка: {e} — продолжаем основной цикл")

        # ── 3. USDSC check + swap ─────────────────────────────────────────────
        usdsc_bal = await _get_usdsc_balance(usdsc, addr_cs)
        logger.info(f"[{eoa_address[:8]}] USDSC баланс: {usdsc_bal/1e6:.4f}")

        if usdsc_bal < USDSC_MIN_BALANCE:
            target_usdsc = random.randint(usdsc_min_raw, usdsc_max_raw)
            logger.info(
                f"[{eoa_address[:8]}] USDSC < 0.25, покупаем {target_usdsc/1e6:.4f} USDSC"
            )
            if lifi_api_key:
                try:
                    eth_needed = await _calc_eth_for_usdsc(target_usdsc, lifi_api_key, proxy)
                    from modules.lifi_swap import swap_eth_to_usdsc
                    await swap_eth_to_usdsc(
                        w3, account, addr_cs,
                        eth_amount_wei=eth_needed,
                        lifi_api_key=lifi_api_key,
                        proxy=proxy,
                    )
                    usdsc_bal = await _get_usdsc_balance(usdsc, addr_cs)
                    logger.info(f"[{eoa_address[:8]}] После свапа USDSC: {usdsc_bal/1e6:.4f}")
                except Exception as e:
                    logger.warning(f"[{eoa_address[:8]}] LI.FI swap ошибка: {e}")
            else:
                logger.warning(f"[{eoa_address[:8]}] Нет LI.FI API ключа, пропускаем покупку USDSC")

        # ── 4. Rug bootstrap mint ─────────────────────────────────────────────
        inv = await asyncio.to_thread(_get_on_chain_item_inventory, eoa_address, proxy)
        current_count = sum(amt for tid, amt in inv if tid < INITIAL_TICKET_TOKEN_ID)
        logger.info(f"[{eoa_address[:8]}] Текущий баланс item-NFT: {current_count}")

        if current_count < rug_target:
            usdsc_bal = await _get_usdsc_balance(usdsc, addr_cs)
            if usdsc_bal > 0:
                try:
                    if await _mint_rug_usdsc(
                        w3, account, addr_cs, eoa_address,
                        gacha, usdsc,
                        current_count, rug_target,
                        gas_multiplier, lifi_api_key, proxy,
                    ):
                        db.upsert_account(eoa_address, press_a_done=True)
                        return True
                except Exception as e:
                    logger.warning(f"[{eoa_address[:8]}] Bootstrap mint ошибка: {e} — продолжаем")
            else:
                logger.warning(f"[{eoa_address[:8]}] USDSC = 0, пропускаем Rug bootstrap")

        # ── 5. Первая продажа (после bootstrap) ───────────────────────────────
        try:
            await _sell_all_items(w3, account, addr_cs, eoa_address, sale, gas_multiplier, proxy)
        except Exception as e:
            logger.warning(f"[{eoa_address[:8]}] Первая продажа ошибка: {e} — продолжаем")

        # ── 6. Основной цикл (только тикеты) ─────────────────────────────────
        for cycle in range(max_cycles):
            logger.info(f"[{eoa_address[:8]}] Цикл {cycle+1}/{max_cycles}")

            try:
                if await _spin_all_shell(
                    w3, account, addr_cs, eoa_address, gacha, game_nft, gas_multiplier
                ):
                    db.upsert_account(eoa_address, press_a_done=True)
                    return True
            except Exception as e:
                logger.warning(f"[{eoa_address[:8]}] Shell spin ошибка: {e}")

            try:
                if await _spin_all_stone(
                    w3, account, addr_cs, eoa_address, gacha, game_nft, gas_multiplier
                ):
                    db.upsert_account(eoa_address, press_a_done=True)
                    return True
            except Exception as e:
                logger.warning(f"[{eoa_address[:8]}] Stone spin ошибка: {e}")

            try:
                await _sell_all_items(w3, account, addr_cs, eoa_address, sale, gas_multiplier, proxy)
            except Exception as e:
                logger.warning(f"[{eoa_address[:8]}] Продажа ошибка: {e}")

            done = await asyncio.to_thread(check_press_a_done, eoa_address)
            if done:
                db.upsert_account(eoa_address, press_a_done=True)
                return True

        logger.warning(f"[{eoa_address[:8]}] Уникальный NFT не получен за {max_cycles} циклов")
        return False

    finally:
        await close_web3_provider(w3)


# ── Public API ────────────────────────────────────────────────────────────────

def run_press_a_for_account(
    private_key: str,
    eoa_address: str,
    rpc_url: str,
    proxy: Optional[str] = None,
    disable_ssl: bool = True,
    gas_multiplier: float = 1.2,
    lifi_api_key: Optional[str] = None,
    config: Optional[dict] = None,
) -> bool:
    """
    Запускает Press A для аккаунта.
    True = Unique уже есть или успешно получен.
    False = ошибка или нужен следующий запуск.
    """
    db.init_db()

    done_api = check_press_a_done(eoa_address)
    if done_api is True:
        db.upsert_account(eoa_address, press_a_done=True)
        logger.info(f"[{eoa_address[:8]}] Press A Unique уже получен (portal)")
        return True

    acc = db.get_account_info(eoa_address) or {}
    checkin_only = bool((config or {}).get("press_a_checkin_only", False))
    if (not checkin_only) and acc.get("press_a_done"):
        logger.info(f"[{eoa_address[:8]}] Press A Unique уже получен (db)")
        return True

    logger.info(f"[{eoa_address[:8]}] Запуск Press A сессии...")
    proxy_url = f"http://{proxy}" if proxy and not proxy.startswith("http") else proxy

    try:
        got_unique = asyncio.run(
            _run_press_a_session(
                private_key,
                eoa_address,
                rpc_url,
                proxy=proxy_url,
                disable_ssl=disable_ssl,
                gas_multiplier=gas_multiplier,
                lifi_api_key=lifi_api_key,
                config=config,
            )
        )
        if got_unique and (not checkin_only):
            db.upsert_account(eoa_address, press_a_done=True)
            logger.success(f"[{eoa_address[:8]}] Press A выполнен!")
        elif checkin_only:
            logger.info(f"[{eoa_address[:8]}] checkIn-only: выполнено")
        else:
            logger.info(f"[{eoa_address[:8]}] Unique не выпал, запустить снова")
        return got_unique
    except Exception as e:
        logger.error(f"[{eoa_address[:8]}] Press A ошибка: {e}")
        return False
