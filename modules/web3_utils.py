#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Web3 хелперы для Soneium (EIP-1559 chain).
Правила:
  - nonce всегда 'pending'
  - eth_call симуляция ОБЯЗАТЕЛЬНА перед отправкой
  - gas estimate × gas_limit_multiplier
  - gas price рандомизация через config
"""

from __future__ import annotations

import asyncio
import random
from typing import Optional, Sequence, Any

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import AsyncWeb3, AsyncHTTPProvider
from web3.types import TxParams

from modules import logger

# Таймаут ожидания receipt (секунды)
TX_WAIT_TIMEOUT = 180
TX_POLL_INTERVAL = 2

# Минимальные ABI для ERC20 / ERC721 (нужны для Kami purchase flow)
ERC20_MIN_ABI: list[dict[str, Any]] = [
    {
        "name": "approve",
        "type": "function",
        "inputs": [{"name": "spender", "type": "address"}, {"name": "value", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "transfer",
        "type": "function",
        "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
    },
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
    {
        "name": "allowance",
        "type": "function",
        "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]

ERC721_MIN_ABI: list[dict[str, Any]] = [
    {
        "name": "balanceOf",
        "type": "function",
        "inputs": [{"name": "owner", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
    },
]


def get_w3(rpc_url: str, proxy: Optional[str] = None, disable_ssl: bool = True) -> AsyncWeb3:
    """Создаёт AsyncWeb3 с опциональным прокси."""
    req_kwargs: dict = {}
    if proxy:
        req_kwargs["proxy"] = proxy
    if disable_ssl:
        req_kwargs["ssl"] = False
    return AsyncWeb3(AsyncHTTPProvider(rpc_url, request_kwargs=req_kwargs))


async def close_web3_provider(w3: AsyncWeb3) -> None:
    """
    Закрывает aiohttp-сессию провайдера AsyncHTTPProvider.
    Вызывать по завершении работы с w3, чтобы не было предупреждения «Unclosed client session».
    """
    provider = getattr(w3, "provider", None)
    if provider is not None and hasattr(provider, "disconnect"):
        try:
            await provider.disconnect()
        except Exception:
            pass


def get_account(private_key: str) -> LocalAccount:
    """Возвращает eth_account из приватного ключа."""
    pk = private_key if private_key.startswith("0x") else "0x" + private_key
    return Account.from_key(pk)

def _to_checksum(addr: str) -> str:
    return AsyncWeb3.to_checksum_address(addr)


def get_erc20_contract(w3: AsyncWeb3, token_address: str, abi: Optional[Sequence[dict]] = None):
    """Возвращает контракт ERC-20 с минимальным ABI (или заданным)."""
    token = _to_checksum(token_address)
    return w3.eth.contract(address=token, abi=list(abi) if abi is not None else ERC20_MIN_ABI)


def get_erc721_contract(w3: AsyncWeb3, nft_address: str, abi: Optional[Sequence[dict]] = None):
    """Возвращает контракт ERC-721 с минимальным ABI (или заданным)."""
    nft = _to_checksum(nft_address)
    return w3.eth.contract(address=nft, abi=list(abi) if abi is not None else ERC721_MIN_ABI)


async def erc20_balance_of(w3: AsyncWeb3, token: str, owner: str) -> int:
    c = get_erc20_contract(w3, token)
    return int(await c.functions.balanceOf(_to_checksum(owner)).call())


async def erc20_allowance(w3: AsyncWeb3, token: str, owner: str, spender: str) -> int:
    c = get_erc20_contract(w3, token)
    return int(await c.functions.allowance(_to_checksum(owner), _to_checksum(spender)).call())


async def erc721_balance_of(w3: AsyncWeb3, nft: str, owner: str) -> int:
    c = get_erc721_contract(w3, nft)
    return int(await c.functions.balanceOf(_to_checksum(owner)).call())


async def erc20_approve_if_needed(
    w3: AsyncWeb3,
    account: LocalAccount,
    token: str,
    spender: str,
    amount: int,
    action: str = "",
    gas_multiplier: float = 1.2,
) -> Optional[str]:
    """
    Делает approve(spender, amount) если allowance < amount.
    Возвращает tx hash или None если approve не нужен.
    """
    owner = account.address
    current = await erc20_allowance(w3, token, owner, spender)
    if current >= amount:
        logger.info(f"{action} | approve не нужен (allowance={current} >= {amount})")
        return None
    c = get_erc20_contract(w3, token)
    fn = c.functions.approve(_to_checksum(spender), int(amount))
    return await send_contract_tx(w3, account, fn, value=0, action=f"{action} | USDC approve", gas_multiplier=gas_multiplier)


async def erc20_transfer(
    w3: AsyncWeb3,
    account: LocalAccount,
    token: str,
    to: str,
    amount: int,
    action: str = "",
    gas_multiplier: float = 1.2,
) -> str:
    """Отправляет ERC-20 transfer(to, amount)."""
    c = get_erc20_contract(w3, token)
    fn = c.functions.transfer(_to_checksum(to), int(amount))
    return await send_contract_tx(w3, account, fn, value=0, action=f"{action} | USDC transfer", gas_multiplier=gas_multiplier)


async def get_nonce(w3: AsyncWeb3, address: str) -> int:
    """Получает актуальный nonce с учётом pending транзакций."""
    return await w3.eth.get_transaction_count(
        AsyncWeb3.to_checksum_address(address), "pending"
    )


async def simulate_tx(w3: AsyncWeb3, tx: TxParams) -> None:
    """
    Симуляция транзакции через eth_call.
    Бросает исключение если транзакция зареверчена.
    В eth_call передаём только from/to/data/value — без gas и fee,
    чтобы RPC не делал баланс-чек на gas*price.
    """
    sim_tx = {
        "from": tx["from"],
        "to": tx["to"],
        "data": tx.get("data", b""),
        "value": tx.get("value", 0),
    }
    try:
        await w3.eth.call(sim_tx, block_identifier="latest")
    except Exception as e:
        raise RuntimeError(f"Симуляция транзакции упала: {e}") from e


async def build_eip1559_tx(
    w3: AsyncWeb3,
    from_addr: str,
    to: str,
    data: bytes,
    value: int = 0,
    gas_multiplier: float = 1.2,
    priority_fee_rand_min: float = 1.0,
    priority_fee_rand_max: float = 1.2,
) -> TxParams:
    """
    Строит EIP-1559 транзакцию для Soneium:
    1. Получает base fee и max priority fee
    2. Добавляет случайный offset к priority fee (антисибил)
    3. Получает nonce 'pending'
    4. Симулирует через eth_call
    5. Оценивает gas × multiplier
    """
    from_addr = AsyncWeb3.to_checksum_address(from_addr)
    to = AsyncWeb3.to_checksum_address(to)

    # Fees
    latest_block = await w3.eth.get_block("latest")
    base_fee = int(latest_block["baseFeePerGas"])
    max_priority_fee = await w3.eth.max_priority_fee
    # Рандомизация priority fee (антисибил)
    rand_mult = random.uniform(priority_fee_rand_min, priority_fee_rand_max)
    max_priority_fee = int(max_priority_fee * rand_mult)
    max_fee_per_gas = max_priority_fee + base_fee * 2

    nonce = await get_nonce(w3, from_addr)
    chain_id = await w3.eth.chain_id

    tx: TxParams = {
        "from": from_addr,
        "to": to,
        "data": data,
        "value": value,
        "nonce": nonce,
        "chainId": chain_id,
        "maxPriorityFeePerGas": max_priority_fee,
        "maxFeePerGas": max_fee_per_gas,
        "type": 2,
    }

    # Симуляция
    await simulate_tx(w3, tx)

    # Оценка газа
    try:
        estimated = await w3.eth.estimate_gas(tx)
        tx["gas"] = int(estimated * gas_multiplier)
    except Exception as e:
        raise RuntimeError(f"estimate_gas упал: {e}") from e

    return tx


async def send_tx(
    w3: AsyncWeb3,
    account: LocalAccount,
    tx: TxParams,
    action: str = "",
) -> str:
    """
    Подписывает и отправляет транзакцию. Ждёт receipt.
    Возвращает tx hash (hex string).
    """
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
    tx_hash = await w3.eth.send_raw_transaction(raw)
    tx_hex = tx_hash.hex()
    tx_hex_prefixed = tx_hex if tx_hex.startswith("0x") else "0x" + tx_hex
    explorer = f"https://soneium.blockscout.com/tx/{tx_hex_prefixed}"
    logger.info(f"{action} | Tx отправлена: {explorer}")

    # Ожидание receipt
    elapsed = 0
    while elapsed < TX_WAIT_TIMEOUT:
        try:
            receipt = await w3.eth.get_transaction_receipt(tx_hash)
            if receipt is not None:
                if receipt.get("status") == 1:
                    logger.success(f"{action} | Tx подтверждена: {explorer}")
                    return tx_hex_prefixed
                else:
                    raise RuntimeError(f"Tx зареверчена: {explorer}")
        except Exception as e:
            if "not found" not in str(e).lower():
                raise
        await asyncio.sleep(TX_POLL_INTERVAL)
        elapsed += TX_POLL_INTERVAL

    raise TimeoutError(f"Tx не подтверждена за {TX_WAIT_TIMEOUT}с: {explorer}")


async def send_contract_tx(
    w3: AsyncWeb3,
    account: LocalAccount,
    contract_func,
    value: int = 0,
    action: str = "",
    gas_multiplier: float = 1.2,
    max_retries: int = 3,
    retry_delay: float = 15.0,
) -> str:
    """
    Строит + отправляет вызов контрактной функции (contract.functions.foo().build_transaction).
    Автоматически симулирует и оценивает газ.
    При реверте транзакции повторяет попытку до max_retries раз с паузой retry_delay секунд.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_retries + 1):
        from_addr = AsyncWeb3.to_checksum_address(account.address)
        nonce = await get_nonce(w3, from_addr)
        chain_id = await w3.eth.chain_id

        latest_block = await w3.eth.get_block("latest")
        base_fee = int(latest_block["baseFeePerGas"])
        max_priority_fee = await w3.eth.max_priority_fee
        rand_mult = random.uniform(1.0, 1.2)
        max_priority_fee = int(max_priority_fee * rand_mult)
        max_fee_per_gas = max_priority_fee + base_fee * 2

        tx: TxParams = await contract_func.build_transaction({
            "from": from_addr,
            "nonce": nonce,
            "chainId": chain_id,
            "maxPriorityFeePerGas": max_priority_fee,
            "maxFeePerGas": max_fee_per_gas,
            "value": value,
            "gas": 0,
            "type": 2,
        })

        # Симуляция
        await simulate_tx(w3, tx)

        # Оценка газа
        try:
            estimated = await w3.eth.estimate_gas(tx)
            tx["gas"] = int(estimated * gas_multiplier)
        except Exception as e:
            raise RuntimeError(f"estimate_gas упал: {e}") from e

        try:
            return await send_tx(w3, account, tx, action=action)
        except RuntimeError as e:
            if "зареверчена" in str(e).lower() and attempt < max_retries:
                last_exc = e
                logger.warning(
                    f"{action} | Попытка {attempt}/{max_retries} провалилась (реверт), "
                    f"повтор через {retry_delay:.0f}с..."
                )
                await asyncio.sleep(retry_delay)
                continue
            raise

    raise last_exc  # type: ignore[misc]
