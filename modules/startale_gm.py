#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Startale Daily GM + Passkey — браузерная автоматизация через AdsPower + Playwright.
Адаптация из examples/Startale_2fa_GM для Season 8.

Паттерн (точно как в примере):
  run_gm_for_account() — синхронная функция
    asyncio.run(_import_wallet(...))                            ← импорт кошелька
    asyncio.run(_connect_startale(..., do_passkey=True/False))  ← подключение + (опц.) passkey
    asyncio.run(_do_gm_on_existing(...))                        ← GM (если нужен)
  Каждый шаг — изолированный playwright инстанс через asyncio.run().
"""

from __future__ import annotations

import asyncio
import random
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests
from web3 import Web3

from modules import logger, db
from modules.portal_api import (
    check_startale_gm_5_done,
    check_startale_passkey_quest_done,
    get_startale_gm_progress,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PROXY_FILE = PROJECT_ROOT / "proxy.txt"

PORTAL_URL = "https://portal.soneium.org/"
PROFILE_MAPPING_URL = "https://portal.soneium.org/api/profile/mapping"
STARTALE_LOGIN_URL = "https://app.startale.com/log-in"
STARTALE_APP_URL = "https://app.startale.com/"
RABBY_EXTENSION_ID = "acmacodkjbdgmoleebolmdjonilkdbch"
BITWARDEN_EXTENSION_ID = "nngceckbapebfimnlniiiahkandclblb"

# Временная почта
MAILTM_BASE = "https://api.mail.tm"
MAILTM_PASSWORD = "BitwardenTemp1"

# Bitwarden
BITWARDEN_MASTER_PASSWORD = "Password1234!@#45"
BITWARDEN_MASTER_HINT = "startale"
BITWARDEN_VERIFY_LINK_RE = re.compile(
    r"https://vault\.bitwarden\.com/redirect-connector\.html#finish-signup\?[^\s\"'<>]+"
)

NEXT_GM_TEXT_SELECTOR = "div.relative.z-10 p.text-sm.text-zinc-900"
WAIT_FOR_GM_DATA_SEC = 10
FALLBACK_GM_COOLDOWN_MINUTES = 60


# ── Proxy helper ─────────────────────────────────────────────────────────────

def _load_random_proxy() -> Optional[dict]:
    """Загружает proxy.txt и возвращает случайный прокси для requests (или None)."""
    if not PROXY_FILE.exists():
        return None
    lines = []
    with open(PROXY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                parts = line.split(":")
                if len(parts) >= 4:
                    ip, port, user, password = parts[0], parts[1], parts[2], ":".join(parts[3:])
                    proxy_url = f"http://{user}:{password}@{ip}:{port}"
                    lines.append({"http": proxy_url, "https": proxy_url})
    return random.choice(lines) if lines else None


# ── AdsPower helpers ─────────────────────────────────────────────────────────

def _adspower_request(api_key: str, method: str, endpoint: str, data: Optional[dict] = None, port: int = 50325) -> dict:
    url = f"http://local.adspower.net:{port}{endpoint}"
    params = {"api_key": api_key}
    if method.upper() == "GET":
        r = requests.get(url, params=params, timeout=30)
    else:
        r = requests.post(url, params=params, json=data or {}, timeout=30)
    r.raise_for_status()
    result = r.json()
    if result.get("code") != 0:
        raise ValueError(result.get("msg", "Ошибка API AdsPower"))
    return result


def _create_profile(api_key: str) -> str:
    name = f"s8_gm_{int(time.time())}_{uuid.uuid4().hex[:6]}"
    result = _adspower_request(api_key, "POST", "/api/v2/browser-profile/create", {
        "name": name,
        "group_id": "0",
        "fingerprint_config": {
            "automatic_timezone": "1",
            "language_switch": "0",
            "language": ["en-US", "en"],
            "webrtc": "disabled",
            "os": "win",
            "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        },
        "proxyid": "random",
    })
    profile_id = result.get("data", {}).get("profile_id")
    if not profile_id:
        raise ValueError("AdsPower не вернул profile_id")
    return profile_id


def _start_browser(api_key: str, profile_id: str) -> dict:
    """Запускает браузер. --disable-features=WebAuthenticationUseNativeWinApi нужен чтобы
    Windows Hello не перехватывал WebAuthn при создании passkey — вместо него откроется Bitwarden."""
    result = _adspower_request(api_key, "POST", "/api/v2/browser-profile/start", {
        "profile_id": profile_id,
        "launch_args": ["--disable-features=WebAuthenticationUseNativeWinApi"],
    })
    return result.get("data", {})


def _stop_browser(api_key: str, profile_id: str) -> None:
    try:
        _adspower_request(api_key, "POST", "/api/v2/browser-profile/stop", {"profile_id": profile_id})
    except Exception as e:
        logger.warning(f"Остановка браузера: {e}")


def _delete_profile(api_key: str, profile_id: str) -> None:
    for key in ("profile_id", "Profile_id"):
        try:
            _adspower_request(api_key, "POST", "/api/v2/browser-profile/delete", {key: [profile_id]})
            return
        except Exception:
            continue
    logger.warning("Не удалось удалить профиль")


def _get_cdp_endpoint(browser_info: dict) -> Optional[str]:
    ws_data = browser_info.get("ws")
    if isinstance(ws_data, dict):
        cdp = ws_data.get("puppeteer")
        if cdp:
            return cdp
    for _, value in browser_info.items():
        if isinstance(value, str) and value.startswith("ws://"):
            return value
        if isinstance(value, dict):
            cdp = value.get("puppeteer") or value.get("ws")
            if isinstance(cdp, str) and cdp.startswith("ws://"):
                return cdp
    return None


def check_smart_account_exists(eoa_address: str, proxies: Optional[dict] = None) -> bool:
    """Проверяет через API profile/mapping, есть ли смарт-аккаунт."""
    url = f"{PROFILE_MAPPING_URL}?eoaAddress={eoa_address}"
    headers = {
        "accept": "application/json, text/plain, */*",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }
    try:
        r = requests.get(url, headers=headers, proxies=proxies, timeout=15)
        if r.status_code == 404:
            return False
        return r.ok
    except Exception as e:
        logger.warning(f"Проверка profile/mapping не удалась: {e}")
        return False


# ── GM timing helpers ────────────────────────────────────────────────────────

def _format_dt(dt: datetime) -> str:
    return dt.strftime("%d.%m.%Y %H:%M UTC")


def parse_next_gm_available(text: str) -> Optional[datetime]:
    if not text or "Next GM available in" not in text:
        return None
    part = text.split("Next GM available in", 1)[-1].strip()
    d = h = m = 0
    for match in re.finditer(r"(\d+)\s*([dhm])", part, re.I):
        val = int(match.group(1))
        unit = match.group(2).lower()
        if unit == "d":
            d = val
        elif unit == "h":
            h = val
        elif unit == "m":
            m = val
    if d == 0 and h == 0 and m == 0:
        return None
    return datetime.now(timezone.utc) + timedelta(days=d, hours=h, minutes=m)


# ── Passkey helpers ──────────────────────────────────────────────────────────

async def _human_like_click(page, locator, timeout: int = 15000) -> None:
    """Эмулирует клик мышью как у человека: движение к случайной точке внутри элемента."""
    await locator.wait_for(state="attached", timeout=timeout)
    try:
        await locator.scroll_into_view_if_needed(timeout=timeout)
    except Exception:
        pass
    await locator.wait_for(state="visible", timeout=timeout)
    box = await locator.bounding_box()
    if not box or box.get("width", 0) <= 0 or box.get("height", 0) <= 0:
        await locator.click(timeout=timeout)
        return
    padding_w = max(2, box["width"] * 0.2)
    padding_h = max(2, box["height"] * 0.2)
    x = box["x"] + padding_w + random.uniform(0, max(1, box["width"] - 2 * padding_w))
    y = box["y"] + padding_h + random.uniform(0, max(1, box["height"] - 2 * padding_h))
    await page.mouse.move(x, y)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await page.mouse.down()
    await asyncio.sleep(random.uniform(0.02, 0.08))
    await page.mouse.up()


def get_disposable_email(proxies: Optional[dict] = None) -> str:
    """Создаёт временный аккаунт mail.tm и возвращает email."""
    r = requests.get(
        f"{MAILTM_BASE}/domains",
        headers={"Accept": "application/json"},
        proxies=proxies,
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and "hydra:member" in data:
        domains = data["hydra:member"]
    elif isinstance(data, list):
        domains = data
    else:
        raise ValueError("Неверный ответ mail.tm/domains")
    if not domains:
        raise ValueError("Нет доступных доменов mail.tm")
    domain = domains[0].get("domain") if isinstance(domains[0], dict) else domains[0]
    local = f"startale_{uuid.uuid4().hex[:12]}"
    address = f"{local}@{domain}"
    create = requests.post(
        f"{MAILTM_BASE}/accounts",
        json={"address": address, "password": MAILTM_PASSWORD},
        headers={"Content-Type": "application/json"},
        proxies=proxies,
        timeout=15,
    )
    create.raise_for_status()
    return address


def fetch_verification_link_from_inbox(
    email: str,
    timeout_seconds: int = 120,
    poll_interval: int = 8,
    proxies: Optional[dict] = None,
) -> Optional[str]:
    """Опрашивает mail.tm: ждёт письмо и извлекает ссылку подтверждения Bitwarden."""
    if "@" not in email:
        return None
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            token_r = requests.post(
                f"{MAILTM_BASE}/token",
                json={"address": email, "password": MAILTM_PASSWORD},
                headers={"Content-Type": "application/json"},
                proxies=proxies,
                timeout=15,
            )
            token_r.raise_for_status()
            token = token_r.json().get("token")
            if not token:
                raise ValueError("Нет token в ответе")
            msg_list = requests.get(
                f"{MAILTM_BASE}/messages",
                headers={"Authorization": f"Bearer {token}"},
                proxies=proxies,
                timeout=15,
            )
            msg_list.raise_for_status()
            messages = msg_list.json()
            if isinstance(messages, dict) and "hydra:member" in messages:
                messages = messages["hydra:member"]
            elif not isinstance(messages, list):
                messages = []
            for msg in messages:
                msg_id = msg.get("id") if isinstance(msg, dict) else msg
                if not msg_id:
                    continue
                read_r = requests.get(
                    f"{MAILTM_BASE}/messages/{msg_id}",
                    headers={"Authorization": f"Bearer {token}"},
                    proxies=proxies,
                    timeout=15,
                )
                read_r.raise_for_status()
                data = read_r.json()
                html = data.get("html")
                if isinstance(html, list):
                    body = " ".join(html)
                else:
                    body = str(html or data.get("htmlBody") or data.get("body") or data.get("text") or "")
                match = BITWARDEN_VERIFY_LINK_RE.search(body)
                if match:
                    return match.group(0).rstrip("&>'\"")
        except Exception as e:
            logger.info(f"Опрос почты: {e}")
        time.sleep(poll_interval)
    return None


async def fetch_verification_link_from_firstmail(
    email: str,
    password: str,
    timeout: int = 120,
    check_interval: float = 8.0,
) -> Optional[str]:
    """Ждёт письмо от Bitwarden через firstmail IMAP и извлекает ссылку подтверждения."""
    from firstmail import FirstMail, FirstMailTimeoutError
    async with FirstMail(email, password) as client:
        try:
            msg = await client.wait_for_sender(
                "bitwarden", timeout=timeout, check_interval=check_interval
            )
        except FirstMailTimeoutError:
            return None
        body = msg.html_body or msg.body or ""
        m = BITWARDEN_VERIFY_LINK_RE.search(body)
        return m.group(0) if m else None


async def _unbind_passkey(page) -> bool:
    """
    Отвязывает passkey через UI (меню → Remove passkey).
    Возвращает True при успехе, False при ошибке.
    """
    try:
        menu_btn = (
            page.locator("div.rounded-xl.border.border-zinc-200")
            .filter(has_text="Passkey [ID:")
            .locator("button[aria-haspopup='menu']")
            .first
        )
        await _human_like_click(page, menu_btn, timeout=30000)
        await asyncio.sleep(0.3)
        remove_menuitem = page.get_by_role("menuitem", name=re.compile(r"Remove\s+passkey", re.IGNORECASE))
        await _human_like_click(page, remove_menuitem.first, timeout=20000)
        await asyncio.sleep(0.3)
        confirm_btn = page.get_by_role("dialog").get_by_role("button", name=re.compile(r"Remove\s+passkey", re.IGNORECASE))
        await _human_like_click(page, confirm_btn.first, timeout=20000)
        await asyncio.sleep(5)
        await page.get_by_text("No passkeys yet").wait_for(state="visible", timeout=30000)
        logger.success("Passkey отвязан (на странице «No passkeys yet»)")
        return True
    except Exception as e:
        logger.warning(f"Ошибка отвязки passkey через UI: {e}")
        return False


PASSKEY_POLL_TIMEOUT_SEC = 600  # 10 мин — при превышении всё равно пытаемся отвязать


async def _wait_quest_done_then_unbind_passkey(page, wallet_address: str, interval_sec: int = 12) -> None:
    """Опрашивает портал каждые interval_sec сек; при засчитывании квеста отвязывает passkey."""
    logger.info(f"Ожидание засчитывания passkey-квеста (опрос каждые {interval_sec} сек)...")
    deadline = time.time() + PASSKEY_POLL_TIMEOUT_SEC
    while time.time() < deadline:
        try:
            proxies = _load_random_proxy()
            done = await asyncio.to_thread(check_startale_passkey_quest_done, wallet_address, proxies)
            if done:
                break
        except Exception as e:
            logger.warning(f"Ошибка опроса портала: {e}")
        await asyncio.sleep(interval_sec)
    else:
        raise TimeoutError(
            f"Портал не засчитал квест за {PASSKEY_POLL_TIMEOUT_SEC}с. "
            "Отвязываем passkey, чтобы не оставить привязанным (почта одноразовая)."
        )
    logger.success("Passkey-квест засчитан. Отвязываем passkey...")
    if not await _unbind_passkey(page):
        raise RuntimeError("Не удалось отвязать passkey через UI")


# ── GM UI helpers ─────────────────────────────────────────────────────────────

async def _get_next_gm_text_from_page(page) -> Optional[str]:
    locs = page.locator(NEXT_GM_TEXT_SELECTOR).filter(has_text="Next GM available in")
    n = await locs.count()
    for i in range(n):
        el = locs.nth(i)
        in_dialog = await el.evaluate("el => !!el.closest('[role=\"dialog\"]')")
        if not in_dialog:
            return await el.text_content()
    if n > 0:
        return await locs.first.text_content()
    return None


async def _get_next_gm_text_from_modal(page) -> Optional[str]:
    # Диалог после GM может иметь разные заголовки:
    # раньше было "GM sent!", сейчас при 10/10 GM — "You got 1 STAR Point!".
    # Ищем любой диалог, где есть текст про следующий GM / STAR Point.
    dialog = page.locator('[role="dialog"]').filter(
        has_text=re.compile(r"(GM sent!|Next GM available in|STAR Point)", re.IGNORECASE)
    )
    for selector in ["p.text-sm.text-zinc-900", "p"]:
        try:
            el = dialog.locator(selector).filter(has_text="Next GM available in")
            await el.first.wait_for(state="visible", timeout=3000)
            text = await el.first.text_content()
            if text and "Next GM available in" in text:
                return text
        except Exception:
            continue
    try:
        el = dialog.get_by_text("Next GM available in")
        await el.first.wait_for(state="visible", timeout=3000)
        return await el.first.text_content()
    except Exception:
        return None


# ── Шаг 1: импорт кошелька в Rabby ──────────────────────────────────────────

async def _import_wallet(cdp_endpoint: str, private_key: str, password: str = "Password123") -> None:
    """Импортирует кошелёк в Rabby по CDP. Отдельный playwright инстанс."""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
        if not browser.contexts:
            raise RuntimeError("Нет контекстов в браузере")
        context = browser.contexts[0]
        setup_url = f"chrome-extension://{RABBY_EXTENSION_ID}/index.html#/new-user/guide"
        page = None
        for p in context.pages:
            if RABBY_EXTENSION_ID in p.url or ("chrome-extension://" in p.url and "rabby" in p.url.lower()):
                page = p
                if "#/new-user/guide" not in p.url:
                    await page.goto(setup_url)
                    await asyncio.sleep(2)
                break
        if not page:
            page = await context.new_page()
            await page.goto(setup_url)
            await asyncio.sleep(3)

        await page.wait_for_selector('span:has-text("I already have an address")', timeout=60000)
        await page.click('span:has-text("I already have an address")')
        await asyncio.sleep(0.5)
        seed_phrase_card = page.locator('div.rabby-ItemWrapper-rabby--mylnj7').filter(has_text="Seed phrase or private key")
        await seed_phrase_card.wait_for(state="visible", timeout=60000)
        await seed_phrase_card.click()
        await asyncio.sleep(0.5)
        await page.wait_for_selector('div.pills-switch__item:has-text("Private Key")', timeout=60000)
        await page.click('div.pills-switch__item:has-text("Private Key")')
        await asyncio.sleep(0.5)
        await page.wait_for_selector("#privateKey", timeout=60000)
        await page.fill("#privateKey", private_key)
        await asyncio.sleep(0.3)
        await page.wait_for_selector('button.ant-btn-primary:has-text("Next"):not([disabled])', timeout=60000)
        await page.click('button.ant-btn-primary:has-text("Next"):not([disabled])')
        await asyncio.sleep(0.5)
        await page.wait_for_selector("#password", timeout=60000)
        await page.fill("#password", password)
        await page.wait_for_selector("#confirmPassword", timeout=60000)
        await page.fill("#confirmPassword", password)
        await asyncio.sleep(0.3)
        await page.wait_for_selector('button.ant-btn-primary:has-text("Confirm"):not([disabled])', timeout=60000)
        await page.click('button.ant-btn-primary:has-text("Confirm"):not([disabled])')
        import_success = asyncio.create_task(page.wait_for_selector("text=Imported Successfully", timeout=60000))
        address_imported = asyncio.create_task(page.wait_for_selector("text=Address Imported", timeout=60000))
        done, pending = await asyncio.wait(
            [import_success, address_imported],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()
            try:
                await t
            except asyncio.CancelledError:
                pass
        logger.success("Кошелёк импортирован в Rabby")
        await page.close()
        # Закрыть вкладку Bitwarden browser-start, если открылась
        for p in context.pages:
            if "bitwarden.com/browser-start" in p.url:
                await p.close()
                logger.info("Вкладка bitwarden.com/browser-start закрыта")
                break
    finally:
        await playwright.stop()


# ── Шаг 2: portal flow (новый аккаунт без смарт-аккаунта) ───────────────────

async def _open_portal(cdp_endpoint: str, eoa_address: str) -> None:
    """Открывает portal.soneium.org, подключает Rabby, создаёт смарт-аккаунт."""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
        if not browser.contexts:
            raise RuntimeError("Нет контекстов в браузере")
        context = browser.contexts[0]
        page = None
        for p in context.pages:
            if not p.url.startswith("chrome-extension://"):
                page = p
                break
        if not page:
            page = await context.new_page()

        await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
        logger.success(f"Открыта страница: {PORTAL_URL}")
        await asyncio.sleep(2)

        connect_wallet_btn = page.get_by_test_id("connect-wallet-button")
        await connect_wallet_btn.wait_for(state="visible", timeout=20000)
        await connect_wallet_btn.scroll_into_view_if_needed()
        await asyncio.sleep(1)
        async with context.expect_page(timeout=35000) as popup_info:
            await connect_wallet_btn.evaluate("el => el.click()")
        popup_page = await popup_info.value
        await popup_page.wait_for_load_state("domcontentloaded", timeout=30000)
        logger.success('Нажата "Connect Wallet", открыт popup Startale')

        connect_btn = popup_page.get_by_role("button", name="Connect a wallet")
        await connect_btn.wait_for(state="visible", timeout=30000)
        await connect_btn.click()
        logger.success('В popup нажата "Connect a wallet"')
        await asyncio.sleep(2)

        rabby_btn = popup_page.get_by_role("button", name="Rabby")
        await rabby_btn.wait_for(state="visible", timeout=30000)
        async with context.expect_page() as wallet_popup_info:
            await rabby_btn.click()
        wallet_popup = await wallet_popup_info.value
        await wallet_popup.wait_for_load_state("domcontentloaded", timeout=15000)
        logger.success("Открыто popup окно кошелька Rabby")

        connect_btn_wallet = wallet_popup.get_by_role("button", name="Connect")
        await connect_btn_wallet.wait_for(state="visible", timeout=30000)
        await connect_btn_wallet.click()
        logger.success("Нажата Connect в popup кошелька")

        sign_popup = await context.wait_for_event("page", timeout=30000)
        await sign_popup.wait_for_load_state("domcontentloaded", timeout=15000)
        sign_btn = sign_popup.get_by_role("button", name="Sign")
        await sign_btn.wait_for(state="visible", timeout=30000)
        await sign_btn.click()
        logger.success("Нажата Sign")
        await asyncio.sleep(1)

        confirm_btn = sign_popup.get_by_role("button", name="Confirm")
        await confirm_btn.wait_for(state="visible", timeout=30000)
        await confirm_btn.click()
        logger.success("Нажата Confirm")
        await asyncio.sleep(1)

        approve_btn = popup_page.get_by_role("button", name="Approve")
        await approve_btn.wait_for(state="visible", timeout=30000)
        await approve_btn.click()
        logger.success("В popup нажата Approve")
        await asyncio.sleep(1)

        # Проверяем смарт-аккаунт через API страницы
        await page.bring_to_front()
        mapping_url = f"{PROFILE_MAPPING_URL}?eoaAddress={eoa_address}"
        need_gasless = True
        try:
            response = await page.request.get(mapping_url)
            need_gasless = response.status == 404
        except Exception as e:
            logger.warning(f"Проверка profile/mapping не удалась: {e}, выполняем Try gasless")

        if need_gasless:
            logger.info("Смарт-аккаунт не создан, нажимаем Try gasless action")
            if "portal.soneium.org" not in page.url:
                await page.goto(PORTAL_URL, wait_until="domcontentloaded", timeout=60000)
            else:
                await page.reload(wait_until="domcontentloaded", timeout=60000)
            welcome_modal = page.locator('[role="dialog"][aria-labelledby="welcome-back-modal-title"]')
            await welcome_modal.wait_for(state="visible", timeout=30000)
            try_gasless_btn = page.get_by_role("button", name="Try gasless action")
            await try_gasless_btn.wait_for(state="visible", timeout=10000)
            async with context.expect_page(timeout=15000) as startale_popup_info:
                await try_gasless_btn.click()
            logger.success('Нажата "Try gasless action"')
            startale_popup = await startale_popup_info.value
            await startale_popup.wait_for_load_state("domcontentloaded", timeout=15000)
            approve_gasless = startale_popup.get_by_role("button", name="Approve")
            await approve_gasless.wait_for(state="visible", timeout=30000)
            await approve_gasless.click()
            logger.success("Approve gasless-транзакции")
        else:
            logger.info("Смарт-аккаунт уже создан, пропускаем Try gasless")
    finally:
        await playwright.stop()


# ── Шаг 3: connect + passkey (смарт-аккаунт уже есть) ───────────────────────

async def _connect_startale(
    cdp_endpoint: str,
    eoa_address: str,
    *,
    do_passkey: bool,
    firstmail_email: Optional[str] = None,
    firstmail_password: Optional[str] = None,
) -> None:
    """Открывает app.startale.com/log-in, подключает Rabby.
    Если do_passkey=True — выполняет Bitwarden + passkey flow.
    Если firstmail_email/password заданы — использует их вместо mail.tm.
    """
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
        if not browser.contexts:
            raise RuntimeError("Нет контекстов в браузере")
        context = browser.contexts[0]
        page = None
        for p in context.pages:
            if not p.url.startswith("chrome-extension://"):
                page = p
                break
        if not page:
            page = await context.new_page()

        await page.goto(STARTALE_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        logger.success(f"Открыта страница: {STARTALE_LOGIN_URL}")

        connect_btn = page.get_by_role("button", name="Connect a wallet")
        await connect_btn.wait_for(state="visible", timeout=30000)
        await connect_btn.click()
        logger.success('Нажата "Connect a wallet"')
        await asyncio.sleep(2)

        rabby_btn = page.get_by_role("button", name="Rabby")
        await rabby_btn.wait_for(state="visible", timeout=30000)
        async with context.expect_page() as wallet_popup_info:
            await rabby_btn.click()
        wallet_popup = await wallet_popup_info.value
        await wallet_popup.wait_for_load_state("domcontentloaded", timeout=15000)
        logger.success("Открыто popup Rabby")

        connect_btn_wallet = wallet_popup.get_by_role("button", name="Connect")
        await connect_btn_wallet.wait_for(state="visible", timeout=30000)
        await connect_btn_wallet.click()
        logger.success("Нажата Connect в popup")

        sign_popup = await context.wait_for_event("page", timeout=30000)
        await sign_popup.wait_for_load_state("domcontentloaded", timeout=15000)
        sign_btn = sign_popup.get_by_role("button", name="Sign")
        await sign_btn.wait_for(state="visible", timeout=30000)
        await sign_btn.click()
        logger.success("Нажата Sign")
        await asyncio.sleep(1)

        confirm_btn = sign_popup.get_by_role("button", name="Confirm")
        await confirm_btn.wait_for(state="visible", timeout=30000)
        await confirm_btn.click()
        logger.success("Нажата Confirm")
        await asyncio.sleep(1)

        try:
            approve_btn = page.get_by_role("button", name="Approve")
            await approve_btn.wait_for(state="visible", timeout=10000)
            await approve_btn.click()
            logger.success("Нажата Approve на log-in")
        except Exception:
            pass
        await asyncio.sleep(1)

        await page.goto(STARTALE_APP_URL, wait_until="domcontentloaded", timeout=60000)
        logger.success(f"Открыта страница {STARTALE_APP_URL}")

        if not do_passkey:
            logger.info("Passkey не требуется — пропускаем шаги Bitwarden/passkey")
            return

        # ── Passkey flow ──────────────────────────────────────────────────────
        # Клик по иконке кошелька → Settings
        wallet_icon = page.locator('img[alt="Wallet"]')
        await wallet_icon.wait_for(state="visible", timeout=45000)
        await wallet_icon.click()
        await asyncio.sleep(0.5)
        settings_btn = page.locator('span.text-zinc-950').filter(has_text="Settings")
        await settings_btn.wait_for(state="visible", timeout=30000)
        await settings_btn.click()
        logger.success("Открыт раздел Settings")

        # Открыть расширение Bitwarden в новой вкладке
        extension_url = f"chrome-extension://{BITWARDEN_EXTENSION_ID}/popup/index.html"
        ext_page = await context.new_page()
        await ext_page.goto(extension_url, wait_until="domcontentloaded", timeout=45000)
        logger.success("Открыто расширение Bitwarden")
        await asyncio.sleep(1)

        # Создать аккаунт Bitwarden
        create_btn = ext_page.get_by_text("Create account", exact=False)
        await create_btn.first.wait_for(state="visible", timeout=45000)
        await create_btn.first.click()
        await asyncio.sleep(1)

        await ext_page.wait_for_selector("#register-start_form_input_email", timeout=45000)
        if firstmail_email and firstmail_password:
            bitwarden_email = firstmail_email
            logger.info(f"Bitwarden: используем firstmail {bitwarden_email}")
        else:
            proxies = _load_random_proxy()
            if proxies:
                logger.info("Запросы к mail.tm через прокси из proxy.txt")
            bitwarden_email = get_disposable_email(proxies)
            logger.info(f"Bitwarden: временный email mail.tm {bitwarden_email}")
        await ext_page.fill("#register-start_form_input_email", bitwarden_email)
        await asyncio.sleep(0.3)

        continue_btn = ext_page.locator('button[type="submit"]').filter(has_text="Continue")
        await continue_btn.wait_for(state="visible", timeout=20000)
        await continue_btn.click()
        logger.success("Bitwarden: введён email, нажато Continue")

        logger.info("Ожидание письма с подтверждением (до 2 мин)...")
        if firstmail_email and firstmail_password:
            verification_link = await fetch_verification_link_from_firstmail(
                firstmail_email, firstmail_password, timeout=120, check_interval=8.0
            )
        else:
            verification_link = await asyncio.to_thread(
                fetch_verification_link_from_inbox, bitwarden_email, 120, 8, proxies
            )
        if not verification_link:
            logger.warning("Ссылка подтверждения Bitwarden не получена — passkey пропущен")
            await ext_page.close()
            return

        await ext_page.goto(verification_link, wait_until="domcontentloaded", timeout=90000)
        logger.success("Переход по ссылке подтверждения Bitwarden")
        await asyncio.sleep(1)

        # Установить мастер-пароль
        await ext_page.wait_for_selector("#input-password-form_new-password", timeout=60000)
        await ext_page.fill("#input-password-form_new-password", BITWARDEN_MASTER_PASSWORD)
        await ext_page.fill("#input-password-form_new-password-confirm", BITWARDEN_MASTER_PASSWORD)
        await ext_page.fill("#input-password-form_new-password-hint", BITWARDEN_MASTER_HINT[:50])
        await asyncio.sleep(0.3)
        create_acc_btn = ext_page.locator('button[type="submit"]').filter(has_text="Create account")
        await create_acc_btn.wait_for(state="visible", timeout=20000)
        await create_acc_btn.click()
        logger.success("Bitwarden: введён мастер-пароль, нажато Create account")
        await ext_page.wait_for_selector("text=Bitwarden extension is installed!", timeout=60000)
        logger.success("Bitwarden: расширение установлено")
        await ext_page.close()

        # Логин в Bitwarden
        ext_page = await context.new_page()
        login_url = f"chrome-extension://{BITWARDEN_EXTENSION_ID}/popup/index.html#/login"
        await ext_page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        logger.success("Bitwarden: открыта страница входа")
        await asyncio.sleep(0.5)
        email_input = ext_page.locator('input[type="email"]').first
        await email_input.wait_for(state="visible", timeout=30000)
        await email_input.fill(bitwarden_email)
        await ext_page.get_by_role("button", name="Continue").click()
        await asyncio.sleep(0.5)
        await ext_page.wait_for_selector('input[type="password"]', timeout=30000)
        await ext_page.fill('input[type="password"]', BITWARDEN_MASTER_PASSWORD)
        await ext_page.get_by_role("button", name="Log in with master password").click()
        await ext_page.wait_for_selector("text=Your vault is empty", timeout=60000)
        logger.success("Bitwarden: вход выполнен, vault пустой")
        await ext_page.close()

        # На Startale Settings → кнопка Passkey
        passkey_btn = page.locator('button').filter(has_text="Passkey")
        await passkey_btn.first.wait_for(state="visible", timeout=30000)
        await passkey_btn.first.click()
        logger.success("Нажата кнопка Passkey на Startale")
        await asyncio.sleep(1)

        # New passkey → Bitwarden popout
        new_passkey_btn = page.locator('button').filter(has_text="New passkey")
        await new_passkey_btn.first.wait_for(state="visible", timeout=45000)
        await _human_like_click(page, new_passkey_btn.first)
        logger.success("Нажата кнопка New passkey")

        # Ждём попап Bitwarden с Fido2Popout в URL
        bitwarden_popup = None
        deadline = time.time() + 30
        while time.time() < deadline:
            for p in context.pages:
                u = p.url or ""
                if "uilocation=popout" in u and "Fido2Popout" in u:
                    bitwarden_popup = p
                    break
            if bitwarden_popup:
                break
            await asyncio.sleep(0.5)
        if not bitwarden_popup:
            raise RuntimeError("Не найдено окно Bitwarden passkey (Fido2Popout)")

        await bitwarden_popup.wait_for_load_state("domcontentloaded", timeout=45000)
        save_btn_re = re.compile(
            r"(Сохранить\s+passkey.*новый\s+логин|Save\s+passkey\s+as\s+new\s+login)",
            re.IGNORECASE,
        )
        save_login_btn = bitwarden_popup.get_by_role("button", name=save_btn_re).first
        await _human_like_click(bitwarden_popup, save_login_btn, timeout=45000)
        logger.success("Нажата «Save passkey as new login» в Bitwarden")
        await asyncio.sleep(1)

        # Ждём засчитывания квеста, затем отвязываем passkey
        passkey_removed = False
        try:
            await _wait_quest_done_then_unbind_passkey(page, eoa_address, interval_sec=12)
            passkey_removed = True
        except Exception as e:
            logger.warning(f"Ошибка при опросе портала/отвязке passkey: {e}")
            # Защита: passkey уже привязан, почта одноразовая — при закрытии браузера
            # отвязать будет невозможно. Пытаемся отвязать сейчас, пока страница открыта.
            logger.info("Попытка отвязать passkey при ошибке (чтобы не оставить привязанным)...")
            try:
                passkey_removed = await _unbind_passkey(page)
            except Exception as e2:
                logger.warning(f"Не удалось отвязать passkey: {e2}")
            db.upsert_account(eoa_address, passkey_remove_failed=not passkey_removed)
        finally:
            if passkey_removed:
                db.upsert_account(eoa_address, passkey_done=True)
            else:
                # Квест мог засчитаться, но отвязка не прошла — всё равно помечаем done
                db.upsert_account(eoa_address, passkey_done=True, passkey_remove_failed=True)
                logger.warning("Passkey засчитан, но не отвязан — требуется ручное удаление")
    finally:
        await playwright.stop()


# ── Шаг 4: GM на уже подключённом браузере ───────────────────────────────────

async def _do_gm_on_existing(cdp_endpoint: str, eoa_address: str) -> None:
    """Подключается к уже запущенному браузеру, открывает app.startale.com и делает GM."""
    from playwright.async_api import async_playwright

    playwright = await async_playwright().start()
    try:
        browser = await playwright.chromium.connect_over_cdp(cdp_endpoint)
        if not browser.contexts:
            raise RuntimeError("Нет контекстов в браузере")
        context = browser.contexts[0]
        page = None
        for p in context.pages:
            if not p.url.startswith("chrome-extension://"):
                page = p
                break
        if not page:
            page = await context.new_page()

        await page.goto(STARTALE_APP_URL, wait_until="domcontentloaded", timeout=60000)
        logger.success(f"Открыта страница {STARTALE_APP_URL}")
        await _do_gm(page, eoa_address)
    finally:
        await playwright.stop()


# ── GM действие ───────────────────────────────────────────────────────────────

async def _do_gm(page, eoa_address: str) -> None:
    """Выполняет GM на открытой странице app.startale.com."""
    await asyncio.sleep(WAIT_FOR_GM_DATA_SEC)

    # Проверяем cooldown
    try:
        text = await _get_next_gm_text_from_page(page)
        if text and "Next GM available in" in text:
            next_at = parse_next_gm_available(text)
            if next_at:
                db.upsert_account(eoa_address, next_gm_available_at=next_at)
                logger.info(f"GM cooldown, следующий: {_format_dt(next_at)}")
            return
    except Exception:
        pass

    # Кликаем Send GM back
    try:
        send_btn = page.get_by_role("button", name="Send GM back")
        await send_btn.wait_for(state="visible", timeout=15000)
        await send_btn.click(timeout=10000)
        logger.success('Нажата "Send GM back"')

        await page.locator("h2:has-text('GM sent!')").wait_for(state="visible", timeout=120000)
        logger.success('Модальное окно "GM sent!" появилось')

        try:
            text = await _get_next_gm_text_from_modal(page)
            next_at = parse_next_gm_available(text or "") if text else None
            if next_at:
                db.upsert_account(eoa_address, next_gm_available_at=next_at)
                logger.success(f"Следующий GM: {_format_dt(next_at)}")
            else:
                fallback = datetime.now(timezone.utc) + timedelta(minutes=FALLBACK_GM_COOLDOWN_MINUTES)
                db.upsert_account(eoa_address, next_gm_available_at=fallback)
                logger.warning(f"Время GM не распознано, fallback: {_format_dt(fallback)}")
        except Exception:
            fallback = datetime.now(timezone.utc) + timedelta(minutes=FALLBACK_GM_COOLDOWN_MINUTES)
            db.upsert_account(eoa_address, next_gm_available_at=fallback)
            logger.warning(f"Ошибка чтения модалки, fallback: {_format_dt(fallback)}")
    except Exception as e:
        logger.warning(f"Кнопка Send GM back не найдена: {e}")


# ── Public API ────────────────────────────────────────────────────────────────

def run_gm_for_account(
    private_key: str,
    eoa_address: str,
    adspower_api_key: str,
    proxy: Optional[dict] = None,
    firstmail_email: Optional[str] = None,
    firstmail_password: Optional[str] = None,
) -> bool:
    """
    Запускает passkey и/или GM для одного аккаунта через AdsPower.
    Паттерн точно как в примере: отдельный asyncio.run() для каждого шага.
    firstmail_email/password — постоянный email для Bitwarden (вместо разового mail.tm).
    """
    db.init_db()

    # ── Проверка passkey ──────────────────────────────────────────────────────
    acc = db.get_account_info(eoa_address)
    passkey_done = bool((acc or {}).get("passkey_done"))
    if not passkey_done:
        api_passkey = check_startale_passkey_quest_done(eoa_address, proxy)
        if api_passkey is True:
            passkey_done = True
            db.upsert_account(eoa_address, passkey_done=True)

    # ── Проверка GM ───────────────────────────────────────────────────────────
    gm_5_done = check_startale_gm_5_done(eoa_address, proxy)
    if gm_5_done is True:
        db.upsert_account(eoa_address, gm_done=True)

    gm_needed = (gm_5_done is not True) and db.is_gm_needed_now(eoa_address)
    if not gm_needed and gm_5_done is not True:
        rec = db.get_account_info(eoa_address)
        next_at_str = (rec or {}).get("next_gm_available_at", "?")
        logger.info(f"[{eoa_address}] GM cooldown до {next_at_str}")

    # ── Пропуск если оба выполнены ────────────────────────────────────────────
    if passkey_done and not gm_needed:
        if gm_5_done is True:
            logger.info(f"[{eoa_address}] Passkey и GM уже выполнены, пропуск")
        else:
            logger.info(f"[{eoa_address}] Passkey выполнен, GM на cooldown, пропуск")
        return True

    cur, req = get_startale_gm_progress(eoa_address, proxy)
    logger.info(f"[{eoa_address}] GM прогресс: {cur}/{req} | passkey: {passkey_done} | запускаем браузер...")

    profile_id = None
    try:
        profile_id = _create_profile(adspower_api_key)
        logger.info(f"[{eoa_address}] Профиль создан: {profile_id}")
        browser_info = _start_browser(adspower_api_key, profile_id)
        time.sleep(5)

        cdp = _get_cdp_endpoint(browser_info)
        if not cdp:
            raise RuntimeError("Не удалось получить CDP endpoint от AdsPower")
        logger.info(f"[{eoa_address}] CDP: {cdp}")

        # Шаг 1: импорт кошелька
        asyncio.run(_import_wallet(cdp, private_key))

        # Шаг 2: подключение к Startale + (опц.) passkey
        has_smart = check_smart_account_exists(eoa_address, proxy)
        db.upsert_account(eoa_address, smart_account_created=has_smart)

        if has_smart:
            logger.info(f"[{eoa_address}] Смарт-аккаунт есть → log-in flow")
            asyncio.run(_connect_startale(
                cdp, eoa_address, do_passkey=not passkey_done,
                firstmail_email=firstmail_email, firstmail_password=firstmail_password,
            ))
        else:
            logger.info(f"[{eoa_address}] Смарт-аккаунт не создан → portal flow")
            asyncio.run(_open_portal(cdp, eoa_address))
            db.upsert_account(eoa_address, smart_account_created=True)
            # После portal flow passkey ещё не выполнен — подключаемся через log-in
            if not passkey_done:
                asyncio.run(_connect_startale(
                    cdp, eoa_address, do_passkey=True,
                    firstmail_email=firstmail_email, firstmail_password=firstmail_password,
                ))

        # Шаг 3: GM (если нужен)
        if gm_needed:
            asyncio.run(_do_gm_on_existing(cdp, eoa_address))

        if firstmail_email and not passkey_done:
            db.upsert_account(eoa_address, passkey_email=firstmail_email)
        logger.success(f"[{eoa_address}] Сессия завершена")
        return True
    except Exception as e:
        logger.error(f"[{eoa_address}] Ошибка: {e}")
        return False
    finally:
        if profile_id:
            _stop_browser(adspower_api_key, profile_id)
            _delete_profile(adspower_api_key, profile_id)
