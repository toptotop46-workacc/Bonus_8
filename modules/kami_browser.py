# -*- coding: utf-8 -*-
"""
Kami (Puzzle Pieces) — только браузерный флоу.
Проверка портала → при нехватке USDC.E добор через LI.FI → AdsPower + Playwright → kamiunlimited.com → покупка в UI.
Логин: email из firstmail_accounts.txt + OTP из письма (firstmail-py). См. docs/KAMI_FIRSTMAIL_LOGIN.md.
"""

from __future__ import annotations

import asyncio
import random
import re
from pathlib import Path
from typing import List, Optional, Tuple

from modules import logger, db
from modules.portal_api import check_kami_week_done, get_kami_progress
from modules.web3_utils import get_w3, get_account, erc20_balance_of, close_web3_provider
from modules.lifi_swap import swap_eth_to_token

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIRSTMAIL_ACCOUNTS_FILE = PROJECT_ROOT / "firstmail_accounts.txt"

RABBY_EXTENSION_ID = "acmacodkjbdgmoleebolmdjonilkdbch"

# Селекторы kamiunlimited.com (диалог Sign up / Login → Verify → 6 полей OTP → Welcome username)
KAMI_SELECTOR_LOGIN_BUTTON = 'button:has-text("Login")'
KAMI_SELECTOR_EMAIL_INPUT = 'input[placeholder="Email address"], [placeholder="Email address"]'
KAMI_SELECTOR_DIALOG_VERIFY = 'dialog[name="Verify"], [role="dialog"]:has-text("Enter the verification code")'
KAMI_OTP_INPUTS_COUNT = 6
KAMI_USERNAME_MIN_LEN = 3
KAMI_USERNAME_MAX_LEN = 20
# Контейнер галочки terms (SVG viewBox="0 0 17 18" в группе) — клик по нему как по чекбоксу
KAMI_SELECTOR_TERMS_GROUP = '[class*="group"]:has(svg[viewBox="0 0 17 18"])'

# Покупка: клик по карточке товара (переход на страницу продукта), не по кнопке Buy Now
KAMI_SELECTOR_CARD = 'img[alt="card1"]'
KAMI_SELECTOR_CARD_FALLBACK = 'a[href*="/product/"] img.object-cover, [class*="object-cover"]'

# Страница продукта /product/294 — только кнопка "Add to Cart" (не "View in Marketplace" с data-tour="edit-profile")
KAMI_SELECTOR_ADD_TO_CART = 'button:has-text("Add to Cart"):not([disabled])'
KAMI_SELECTOR_ADD_TO_CART_FALLBACK = 'button:has-text("Add to cart"):not([disabled])'
KAMI_ADD_TO_CART_SELECTORS = [
    'button:has-text("Add to Cart"):not([disabled])',
    'button:has-text("Add to cart"):not([disabled])',
    'button:has-text("ADD TO CART"):not([disabled])',
]

# Корзина /cart: чекбокс выбора товара (тот же паттерн SVG, что и terms)
KAMI_SELECTOR_CART_ITEM_CHECKBOX = '[class*="group"]:has(svg[viewBox="0 0 17 18"])'
KAMI_SELECTOR_WALLET_CONNECT_BTN = 'button:has-text("Wallet Connect")'

# Модалка выбора кошелька: MetaMask (для Rabby)
KAMI_SELECTOR_METAMASK_BTN = 'button:has-text("MetaMask")'

# Rabby popup: Ignore all, затем Connect
KAMI_SELECTOR_RABBY_IGNORE_ALL = 'span.underline:has-text("Ignore all")'
KAMI_SELECTOR_RABBY_CONNECT = 'button.ant-btn-primary span:has-text("Connect")'
# Rabby подпись/подтверждение: сначала Ignore all (если есть), затем Sign, затем Confirm
KAMI_RABBY_IGNORE_ALL_TEXTS = ["Ignore all", "ignore all"]
KAMI_RABBY_SIGN_BTN = 'button:has-text("Sign")'
KAMI_RABBY_CONFIRM_BTN = 'button:has-text("Confirm")'
# На главной после подтверждения: ждём исчезновения "Transaction processing"
KAMI_TRANSACTION_PROCESSING_TEXT = "Transaction processing"

# После подключения: кнопка оплаты (внутри вложена кнопка Disconnect — кликаем по span с текстом "Pay with")
KAMI_SELECTOR_PAY_WITH = 'button[aria-label="Wallet connected"]'
KAMI_PAY_WITH_SELECTORS = [
    'button[aria-label="Wallet connected"] span:has-text("Pay with")',
    'button[aria-label="Wallet connected"]:has-text("Pay with")',
    'button[aria-label="Wallet connected"]',
    'button[title="Wallet connected"]',
    'button:has-text("Pay with")',
]


async def _ensure_usdce_balance(
    rpc_url: str,
    private_key: str,
    address: str,
    cfg: dict,
    proxy_dict: Optional[dict],
    lifi_api_key: Optional[str],
) -> None:
    """
    Проверяет баланс USDC.E. Если меньше нужной суммы и включён LI.FI — делает своп ETH → USDC.E.
    Логика как в старой Kami: один или два свопа с учётом gas reserve и случайной суммы в [eth_min, eth_max].
    """
    def _usdc_fmt(raw: int) -> str:
        v = raw / 1e6
        s = f"{v:.6f}".rstrip("0").rstrip(".")
        return s if s else "0"

    token = cfg.get("kami_week1_payment_token")
    amount = int(cfg.get("kami_week1_payment_amount", 1_000_000))
    if not token or amount <= 0:
        logger.warning("Kami: не заданы kami_week1_payment_token/amount, пропуск добора USDC.E")
        return

    proxy_str = None
    if proxy_dict:
        proxy_str = proxy_dict.get("https") or proxy_dict.get("http")
    w3 = get_w3(rpc_url, proxy=proxy_str, disable_ssl=cfg.get("disable_ssl", True))
    account = get_account(private_key)
    addr_cs = account.address
    try:
        balance = await erc20_balance_of(w3, token, addr_cs)
        if balance >= amount:
            logger.info(f"[{addr_cs}] USDC.E достаточно: {_usdc_fmt(balance)} >= {_usdc_fmt(amount)}, добор не нужен")
            return

        if not cfg.get("kami_lifi_enabled") or not lifi_api_key:
            logger.warning(
                f"[{addr_cs}] USDC.E не хватает ({_usdc_fmt(balance)} < {_usdc_fmt(amount)}), "
                "LI.FI выключен или нет API ключа — продолжаем без добора"
            )
            return

        eth_min = float(cfg.get("kami_lifi_eth_min", 0.0007))
        eth_max = float(cfg.get("kami_lifi_eth_max", 0.0015))
        gas_reserve = float(cfg.get("kami_lifi_gas_reserve_eth", 0.0005))

        eth_balance_wei = await w3.eth.get_balance(addr_cs)
        eth_balance = eth_balance_wei / 1e18
        if eth_balance <= gas_reserve:
            logger.warning(f"[{addr_cs}] Недостаточно ETH для LI.FI (остаток {eth_balance:.6f}, резерв {gas_reserve})")
            return

        max_swap_eth = eth_balance - gas_reserve
        swap_eth = random.uniform(eth_min, eth_max)
        if swap_eth > max_swap_eth:
            swap_eth = max_swap_eth
        if swap_eth <= 0:
            return
        swap_eth_wei = int(swap_eth * 1e18)

        for attempt in range(2):
            try:
                await swap_eth_to_token(
                    w3, account, addr_cs, token, swap_eth_wei,
                    lifi_api_key=lifi_api_key, proxy=proxy_str,
                )
                balance = await erc20_balance_of(w3, token, addr_cs)
                if balance >= amount:
                    logger.info(f"[{addr_cs}] USDC.E после добора: {_usdc_fmt(balance)} >= {_usdc_fmt(amount)}")
                    return
                # Вторая попытка: снова проверяем доступный ETH и свопим в пределах [min, max]
                if attempt == 0:
                    eth_balance_wei = await w3.eth.get_balance(addr_cs)
                    eth_balance = eth_balance_wei / 1e18
                    max_swap_eth = eth_balance - gas_reserve
                    if max_swap_eth <= 0:
                        break
                    swap_eth = random.uniform(eth_min, eth_max)
                    if swap_eth > max_swap_eth:
                        swap_eth = max_swap_eth
                    swap_eth_wei = int(swap_eth * 1e18)
                    if swap_eth_wei <= 0:
                        break
            except Exception as e:
                logger.warning(f"[{addr_cs}] LI.FI попытка {attempt + 1}/2: {e}")
                if attempt == 0:
                    eth_balance_wei = await w3.eth.get_balance(addr_cs)
                    eth_balance = eth_balance_wei / 1e18
                    max_swap_eth = eth_balance - gas_reserve
                    if max_swap_eth <= 0:
                        break
                    swap_eth = min(eth_max * 0.5, max_swap_eth)
                    swap_eth_wei = int(swap_eth * 1e18)
                    if swap_eth_wei <= 0:
                        break
        logger.warning(f"[{addr_cs}] USDC.E после LI.FI всё ещё может быть недостаточно — продолжаем в браузер")
    finally:
        await close_web3_provider(w3)


# ── Firstmail: пул аккаунтов и OTP ─────────────────────────────────────────────

def load_firstmail_pool(file_path: Optional[Path] = None) -> List[Tuple[str, str]]:
    """
    Загружает пул firstmail из файла (строки email:password).
    Возвращает список пар (email, password). Пустые строки и # — пропуск.
    """
    path = file_path or FIRSTMAIL_ACCOUNTS_FILE
    if not path.exists():
        logger.warning("Kami: firstmail_accounts.txt не найден, пул пуст")
        return []
    pairs: List[Tuple[str, str]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                email, _, password = line.partition(":")
                email, password = email.strip(), password.strip()
                if email and password:
                    pairs.append((email, password))
    return pairs


def _extract_otp_from_text(text: str) -> Optional[str]:
    """Извлекает первый код 4–8 цифр из текста (тема/тело письма)."""
    if not text:
        return None
    m = re.search(r"\b(\d{4,8})\b", text)
    return m.group(1) if m else None


async def get_otp_for_kami(email: str, password: str, timeout_sec: float = 90) -> Optional[str]:
    """
    Получает OTP от kamiunlimited: сначала проверяет текущий ящик (письмо могло прийти до подключения),
    затем при необходимости ждёт новое письмо. Возвращает код 4–8 цифр или None.
    """
    try:
        from firstmail import (
            FirstMail,
            FirstMailAuthError,
            FirstMailConnectionError,
            FirstMailTimeoutError,
            FirstMailError,
        )
    except ImportError:
        logger.error("firstmail-py не установлен: pip install firstmail-py")
        return None

    try:
        async with FirstMail(
            email,
            password,
            use_ssl=True,
            timeout=25.0,
        ) as client:
            # 1) Сначала проверить текущий ящик (письмо могло прийти до вызова get_otp)
            code = await client.get_otp_code()
            if code and code.isdigit():
                logger.info("Kami OTP: получен из текущего ящика firstmail")
                return code

            # 2) Fallback: последние письма — парсим вручную (на случай нестандартного формата)
            for msg in (await client.get_all_mail(limit=5)) or []:
                for part in (msg.subject, msg.body, (msg.html_body or "")):
                    code = _extract_otp_from_text(part or "")
                    if code:
                        logger.info("Kami OTP: извлечён из письма (subject/body)")
                        return code

            # 3) Ждать новое письмо и извлечь OTP
            code = await client.get_otp_code(timeout=timeout_sec)
            if code and code.isdigit():
                logger.info("Kami OTP: получен после ожидания нового письма")
                return code

            # 4) Если get_otp_code в wait mode вернул None — разбираем последнее письмо вручную
            last = await client.get_last_mail()
            if last:
                for part in (last.subject, last.body, (last.html_body or "")):
                    code = _extract_otp_from_text(part or "")
                    if code:
                        logger.info("Kami OTP: извлечён из последнего письма после ожидания")
                        return code
    except FirstMailAuthError as e:
        logger.warning(f"Firstmail: неверный логин/пароль для {email}: {e}")
        return None
    except FirstMailConnectionError as e:
        logger.warning(f"Firstmail: ошибка подключения к серверу для {email}: {e}")
        return None
    except FirstMailTimeoutError as e:
        logger.warning(f"Firstmail: таймаут ожидания OTP для {email}: {e}")
        return None
    except FirstMailError as e:
        logger.warning(f"Firstmail OTP для {email}: {e}")
        return None
    except Exception as e:
        logger.warning(f"Firstmail OTP для {email}: {type(e).__name__}: {e}")
        return None

    return None


_FIRST_NAMES = [
    "alex", "mike", "chris", "james", "jake", "ryan", "adam", "kyle", "josh",
    "nick", "ben", "sam", "matt", "tom", "john", "david", "mark", "scott",
    "luke", "eric", "evan", "brad", "cole", "drew", "sean", "seth", "zach",
    "liam", "noah", "owen", "anna", "kate", "emma", "lily", "sara", "amy",
    "mia", "zoe", "ava", "ivy", "jade", "lisa", "nina", "nora", "rose",
    "leo", "max", "ian", "ray", "dan", "rob", "joe", "tim", "ken", "glen",
]
_WORDS = [
    "crypto", "moon", "defi", "nft", "eth", "chain", "labs", "dev", "web3",
    "dao", "vault", "node", "alpha", "degen", "hodl", "whale", "trade", "earn",
    "stake", "swap", "build", "real", "wild", "dark", "neon", "cool", "fast",
    "star", "cash", "gold", "iron", "rock", "wolf", "bear", "bull", "fox",
    "byte", "mint", "drop", "rare", "epic", "fire", "ice", "dust", "peak",
    "flux", "gear", "grid", "hack", "hub", "jet", "key", "link", "loop",
]


def generate_kami_username() -> str:
    """Генерирует человекоподобный username 6–16 символов (имя + разделитель + слово + цифры)."""
    name = random.choice(_FIRST_NAMES)
    word = random.choice(_WORDS)
    sep = random.choice(["_", "."])
    # Суффикс: 2-4 цифры или пусто (имитация реального никнейма)
    suffix = random.choice([
        "",
        str(random.randint(10, 99)),
        str(random.randint(1990, 2006)),
        str(random.randint(100, 999)),
    ])
    username = f"{name}{sep}{word}{suffix}"
    if len(username) > KAMI_USERNAME_MAX_LEN:
        username = username[:KAMI_USERNAME_MAX_LEN]
    return username


async def _kami_handle_username_modal_if_present(
    page,  # playwright.async_api.Page
    eoa_address: str,
    cfg: dict,
) -> bool:
    """
    Если показан диалог «Welcome to KAMI. Please choose a username» — заполняет username (из db или
    сгенерированный), принимает terms, нажимает Let's Go!, сохраняет username в quest_results.json.
    Возвращает True если модалка была обработана или не появилась, False при ошибке.
    """
    from playwright.async_api import Page

    if not isinstance(page, Page):
        return False

    timeout_ms = int(cfg.get("kami_login_wait_timeout_sec", 60) * 1000)
    try:
        welcome = page.get_by_role("dialog", name="Welcome to KAMI.")
        await welcome.wait_for(state="visible", timeout=3000)
        logger.info("Kami: модалка username (Welcome to KAMI), заполняю…")
    except Exception:
        return True  # модалки нет — уже есть username

    try:
        acc = db.get_account_info(eoa_address) or {}
        username = acc.get("kami_username") or None
        if not username or len(username) < KAMI_USERNAME_MIN_LEN or len(username) > KAMI_USERNAME_MAX_LEN:
            username = generate_kami_username()
        username_input = welcome.get_by_placeholder("Enter username")
        await username_input.scroll_into_view_if_needed(timeout=5000)
        await asyncio.sleep(random.uniform(0.2, 0.4))
        await username_input.fill(username, timeout=timeout_ms)
        await asyncio.sleep(random.uniform(0.4, 0.7))
        # Галочка terms: клик по контейнеру SVG-галочки (как человек — по области чекбокса)
        # Селектор: SVG viewBox="0 0 17 18" в группе; кликаем родителя/контейнер с class "group"
        terms_clicked = False
        try:
            # Контейнер с классом group, содержащий иконку галочки (Kami использует group-data-[selected])
            terms_container = welcome.locator(KAMI_SELECTOR_TERMS_GROUP).first
            await terms_container.scroll_into_view_if_needed(timeout=5000)
            await asyncio.sleep(random.uniform(0.15, 0.3))
            await terms_container.hover(timeout=5000)
            await asyncio.sleep(random.uniform(0.2, 0.4))
            await terms_container.click(timeout=5000)
            terms_clicked = True
        except Exception:
            pass
        if not terms_clicked:
            checkbox = welcome.get_by_role("checkbox")
            await checkbox.scroll_into_view_if_needed(timeout=5000)
            await asyncio.sleep(0.2)
            try:
                await checkbox.hover(timeout=5000)
                await asyncio.sleep(random.uniform(0.15, 0.35))
                await checkbox.check(timeout=5000)
            except Exception:
                try:
                    await checkbox.click(force=True, timeout=5000)
                except Exception:
                    try:
                        await checkbox.focus()
                        await asyncio.sleep(0.2)
                        await page.keyboard.press("Space")
                    except Exception:
                        await welcome.evaluate("""() => {
                            const cb = document.querySelector('input[type="checkbox"]');
                            if (cb) { cb.checked = true; cb.dispatchEvent(new Event('change', { bubbles: true })); }
                        }""")
        await asyncio.sleep(random.uniform(0.25, 0.5))
        # Модалка «Allow notification(s)» может перекрывать Let's Go — закрываем при наличии
        try:
            notif = page.get_by_text(re.compile(r"Allow\s+notification", re.I))
            if await notif.first.is_visible(timeout=800):
                dismissed = False
                for label in ["Block", "Don't allow", "Not now", "No thanks", "Later", "No", "Cancel"]:
                    btn = page.get_by_role("button", name=re.compile(re.escape(label), re.I))
                    if await btn.first.is_visible(timeout=500):
                        await btn.first.click(timeout=3000)
                        logger.info("Kami: закрыта модалка уведомлений (Allow notification)")
                        dismissed = True
                        break
                if not dismissed:
                    for text in ["Block", "Don't allow", "Not now"]:
                        el = page.get_by_text(re.compile(re.escape(text), re.I)).first
                        if await el.is_visible(timeout=300):
                            await el.click(timeout=3000)
                            logger.info("Kami: закрыта модалка уведомлений (Allow notification)")
                            break
                await asyncio.sleep(0.3)
        except Exception:
            pass
        # Кнопка Let's Go!: human-like — дождаться enabled, hover, пауза, click
        lets_go = welcome.get_by_role("button", name="Let's Go!")
        await lets_go.wait_for(state="visible", timeout=5000)
        for _ in range(25):
            try:
                if await lets_go.is_enabled():
                    break
            except Exception:
                pass
            await asyncio.sleep(0.2)
        await lets_go.scroll_into_view_if_needed(timeout=5000)
        await asyncio.sleep(0.2)
        await lets_go.hover(timeout=5000)
        await asyncio.sleep(random.uniform(0.2, 0.4))
        await lets_go.click(timeout=5000)
        db.upsert_account(eoa_address, kami_username=username)
        logger.info(f"Kami: username '{username}' сохранён в quest_results.json")
        await asyncio.sleep(1)
        try:
            await welcome.wait_for(state="hidden", timeout=10000)
        except Exception:
            pass
        logger.info("Kami: модалка username завершена, сессия сохранена")
        return True
    except Exception as e:
        logger.warning(f"Kami модалка username: {e}")
        return False


async def _kami_login_with_firstmail(
    page,  # playwright.async_api.Page
    email: str,
    password: str,
    cfg: dict,
) -> bool:
    """
    Выполняет логин на kamiunlimited.com: Login → email → ожидание Verify → OTP из firstmail → ввод кода.
    page — Playwright async Page (уже открыта страница коллекции, например /collection/410).
    Возвращает True при успешном входе, False при ошибке.
    """
    from playwright.async_api import Page

    if not isinstance(page, Page):
        logger.error("_kami_login_with_firstmail: ожидается playwright.async_api.Page")
        return False

    timeout_ms = int(cfg.get("kami_login_wait_timeout_sec", 60) * 1000)
    login_btn_wait_ms = min(25000, timeout_ms)  # до 25 с ждём появления кнопки Login

    try:
        # Дождаться полной загрузки страницы, чтобы кнопка Login успела отрендериться
        try:
            await page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(1)

        logger.info("Kami логин: ищу кнопку Login…")
        # Кнопка Login — ждём появления, затем human-like: scroll → hover → пауза → click (с повторами)
        login_btn = page.get_by_role("button", name="Login")
        await login_btn.wait_for(state="visible", timeout=login_btn_wait_ms)
        logger.info("Kami логин: кнопка Login найдена, кликаю…")
        await login_btn.scroll_into_view_if_needed(timeout=10000)
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await login_btn.hover(timeout=10000)
        await asyncio.sleep(random.uniform(0.2, 0.45))
        # Повторные попытки клика: страница может ещё догружаться
        for attempt in range(3):
            try:
                await login_btn.click(timeout=10000)
                break
            except Exception as e:
                if attempt < 2:
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue
                raise
        await asyncio.sleep(random.uniform(0.8, 1.5))

        logger.info("Kami логин: ввожу email…")
        # Поле email в диалоге Sign up / Login
        email_input = page.get_by_placeholder("Email address")
        await email_input.wait_for(state="visible", timeout=timeout_ms)
        await email_input.scroll_into_view_if_needed(timeout=5000)
        await asyncio.sleep(0.2)
        await email_input.fill(email)
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        logger.info("Kami логин: жду диалог Verify и OTP из firstmail…")
        # Диалог Verify с 6 полями кода
        verify_dialog = page.get_by_role("dialog", name="Verify")
        await verify_dialog.wait_for(state="visible", timeout=timeout_ms)
        await asyncio.sleep(random.uniform(2, 4))

        code = await get_otp_for_kami(
            email, password,
            timeout_sec=cfg.get("kami_login_wait_timeout_sec", 60),
        )
        if not code or not code.isdigit():
            logger.warning("Kami: не удалось получить OTP из firstmail")
            return False

        # 6 полей OTP — human-like: небольшая пауза между вводом цифр
        digits = (code.strip() + "000000")[:KAMI_OTP_INPUTS_COUNT]
        otp_inputs = verify_dialog.get_by_role("textbox")
        count = await otp_inputs.count()
        for i in range(min(count, KAMI_OTP_INPUTS_COUNT)):
            if i < len(digits):
                inp = otp_inputs.nth(i)
                await inp.scroll_into_view_if_needed(timeout=3000)
                await asyncio.sleep(random.uniform(0.05, 0.15))
                await inp.fill(digits[i], timeout=5000)
                await asyncio.sleep(random.uniform(0.08, 0.2))

        await asyncio.sleep(random.uniform(0.2, 0.4))
        await page.keyboard.press("Enter")
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Успех: диалог исчезает или появляется кнопка/меню пользователя
        try:
            await verify_dialog.wait_for(state="hidden", timeout=10000)
        except Exception:
            pass
        logger.info("Kami: логин по OTP выполнен")
        return True

    except Exception as e:
        logger.warning(f"Kami логин (firstmail): {e}")
        return False


async def _kami_import_wallet(cdp_endpoint: str, private_key: str, password: str = "Password123") -> None:
    """Импорт кошелька в Rabby для Kami: без новой вкладки — используется существующая, затем закрывается."""
    from playwright.async_api import async_playwright

    logger.info("Kami: импорт кошелька в Rabby…")
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
            # Kami: не открываем новую вкладку — берём любую существующую
            for p in context.pages:
                page = p
                break
            if not page:
                page = await context.new_page()
            await page.goto(setup_url)
            try:
                await page.wait_for_load_state("load", timeout=15000)
            except Exception:
                pass
            await asyncio.sleep(2)
            # Если браузер открыл расширение в новой вкладке — работаем с той, где Rabby
            for p in context.pages:
                if RABBY_EXTENSION_ID in p.url:
                    page = p
                    break

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
        await page.wait_for_selector("text=Imported Successfully", timeout=60000)
        logger.info("Kami: кошелёк импортирован в Rabby")
        await page.close()
        # Закрыть вкладки Rabby и Bitwarden, но оставить минимум одну вкладку — иначе браузер закроется
        to_close = [
            p for p in list(context.pages)
            if RABBY_EXTENSION_ID in p.url or "bitwarden.com/browser-start" in p.url
        ]
        if len(to_close) >= len(context.pages):
            to_close = to_close[:-1]
        for p in to_close:
            await p.close()
            if "bitwarden.com/browser-start" in p.url:
                logger.info("Вкладка bitwarden.com/browser-start закрыта")
    finally:
        await playwright.stop()


async def _run_kami_browser_async(
    cdp_endpoint: str,
    private_key: str,
    eoa_address: str,
    cfg: dict,
    *,
    firstmail_email: Optional[str] = None,
    firstmail_password: str = "",
) -> None:
    """Импорт кошелька в Rabby → открыть коллекцию → логин (firstmail) → username → покупка."""
    from playwright.async_api import async_playwright

    collection_url = cfg.get("kami_collection_url") or "https://www.kamiunlimited.com/collection/410"
    pk_with_0x = private_key if private_key.startswith("0x") else "0x" + private_key

    await _kami_import_wallet(cdp_endpoint, pk_with_0x)
    await asyncio.sleep(1)

    logger.info("Kami: подключение к браузеру по CDP…")
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
            logger.info("Kami: создана новая вкладка")

        logger.info("Kami: переход на коллекцию…")
        await page.goto(collection_url, wait_until="load", timeout=60000)
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2, 4))

        if firstmail_email and firstmail_password:
            logger.info("Kami: логин (firstmail)…")
            logged_in = await _kami_login_with_firstmail(page, firstmail_email, firstmail_password, cfg)
            if not logged_in:
                logger.warning("Kami: OTP не получен, авторизация не выполнена. Без входа покупка невозможна — закрываю браузер.")
                return
            await asyncio.sleep(1)
            await _kami_handle_username_modal_if_present(page, eoa_address, cfg)
            await asyncio.sleep(0.5)
        else:
            logger.warning("Kami: нет firstmail — пропуск логина (нужен уже залогиненный аккаунт)")

        logger.info("Kami: флоу покупки (продукт → корзина → Pay with)…")
        await _kami_purchase_flow(page, context, cfg)
    finally:
        await playwright.stop()


async def _kami_purchase_flow(page, context, cfg: dict) -> bool:
    """
    Карточка → продукт → Add to Cart → /cart → чекбокс → Wallet Connect → MetaMask (Rabby) → Pay with.
    Возвращает True при успехе.
    """
    base_url = (cfg.get("kami_collection_url") or "https://www.kamiunlimited.com/collection/410").rstrip("/").replace("/collection/410", "")
    timeout = int(cfg.get("kami_purchase_wait_timeout_sec", 120) * 1000)
    purchase_ok = False

    try:
        logger.info("Kami покупка: закрываю модалки (Escape)")
        for _ in range(3):
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.25)
        await asyncio.sleep(random.uniform(0.5, 1.0))

        # 1) Перейти на страницу продукта напрямую.
        product_url = (cfg.get("kami_product_url") or f"{base_url}/product/294").strip()
        logger.info("Kami покупка: переход на страницу продукта…")
        await page.goto(product_url, wait_until="load", timeout=60000)
        try:
            await page.wait_for_url("**/product/**", timeout=20000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2, 3.5))

        logger.info("Kami покупка: ищу кнопку Add to Cart…")
        # 2) Add to Cart — пробуем несколько селекторов (сайт может менять атрибуты/текст)
        add_btn = None
        per_selector_ms = 6000
        for sel in KAMI_ADD_TO_CART_SELECTORS:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=per_selector_ms)
                add_btn = loc
                break
            except Exception:
                continue
        if not add_btn:
            # Последняя попытка: кнопка с текстом "add to cart" (без учёта регистра), только не disabled
            add_btn = page.locator("button:not([disabled])").filter(has_text=re.compile(r"add\s*to\s*cart", re.I)).first
            await add_btn.wait_for(state="visible", timeout=8000)
        # Human-like клик: scroll → пауза → hover → пауза → click (как для Login / Let's Go)
        await add_btn.scroll_into_view_if_needed(timeout=8000)
        await asyncio.sleep(random.uniform(0.3, 0.6))
        await add_btn.hover(timeout=8000)
        await asyncio.sleep(random.uniform(0.25, 0.5))
        await add_btn.click(timeout=8000)
        logger.info("Kami покупка: Add to Cart нажата, жду 5 с")
        await asyncio.sleep(5)

        logger.info("Kami покупка: переход в корзину /cart…")
        # 3) Переход в корзину
        await page.goto(base_url + "/cart", wait_until="load", timeout=30000)
        try:
            await page.wait_for_load_state("load", timeout=15000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(2, 3))

        logger.info("Kami покупка: чекбокс товара в корзине…")
        # 4) Чекбокс выбора товара в корзине (круглая кнопка) — кликаем только если ещё не нажат (data-selected != true)
        cart_checkbox = page.locator(KAMI_SELECTOR_CART_ITEM_CHECKBOX).first
        await cart_checkbox.wait_for(state="visible", timeout=15000)
        await cart_checkbox.scroll_into_view_if_needed()
        await asyncio.sleep(0.2)
        is_selected = (
            await cart_checkbox.get_attribute("data-selected") == "true"
            or await cart_checkbox.get_attribute("aria-checked") == "true"
        )
        if is_selected:
            logger.info("Kami покупка: чекбокс товара уже выбран, пропуск клика")
        else:
            await cart_checkbox.click(timeout=8000)
        await asyncio.sleep(random.uniform(1.0, 1.8))

        logger.info("Kami покупка: нажимаю Wallet Connect…")
        # 5) Wallet Connect
        wallet_btn = page.locator(KAMI_SELECTOR_WALLET_CONNECT_BTN).first
        await wallet_btn.wait_for(state="visible", timeout=10000)
        await wallet_btn.click(timeout=8000)
        await asyncio.sleep(1.5)

        logger.info("Kami покупка: выбираю MetaMask (Rabby)…")
        # 6) MetaMask (Rabby) в модалке
        metamask_btn = page.locator(KAMI_SELECTOR_METAMASK_BTN).first
        await metamask_btn.wait_for(state="visible", timeout=10000)
        async with context.expect_page(timeout=20000) as popup_info:
            await metamask_btn.click(timeout=8000)
        rabby_popup = await popup_info.value
        logger.info("Kami покупка: открыт попап Rabby")
        await rabby_popup.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            await rabby_popup.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(1)
        # Ignore all (если есть)
        try:
            ignore_all = rabby_popup.locator(KAMI_SELECTOR_RABBY_IGNORE_ALL).first
            await ignore_all.wait_for(state="visible", timeout=3000)
            await ignore_all.click(timeout=5000)
            await asyncio.sleep(0.5)
        except Exception:
            pass
        connect_btn = rabby_popup.locator(KAMI_SELECTOR_RABBY_CONNECT).first
        await connect_btn.wait_for(state="visible", timeout=15000)
        await connect_btn.click(timeout=8000)
        logger.info("Kami покупка: Rabby Connect нажат, закрываю попап")
        await asyncio.sleep(2)
        await rabby_popup.close()
        await asyncio.sleep(1.5)

        # 7) Pay with 0x... — клик через mouse по координатам кнопки
        try:
            await page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        await asyncio.sleep(random.uniform(1.5, 2.5))

        logger.info("Kami покупка: ищу кнопку Pay with…")
        pay_btn = None
        for sel in KAMI_PAY_WITH_SELECTORS:
            try:
                loc = page.locator(sel).first
                await loc.wait_for(state="visible", timeout=8000)
                pay_btn = loc
                logger.info("Kami покупка: кнопка Pay with найдена")
                break
            except Exception:
                continue
        if not pay_btn:
            pay_btn = page.get_by_role("button", name="Wallet connected")
            await pay_btn.wait_for(state="visible", timeout=8000)
            logger.info("Kami покупка: кнопка Pay with найдена (get_by_role)")
        await page.bring_to_front()
        await asyncio.sleep(0.3)
        await pay_btn.scroll_into_view_if_needed()
        await asyncio.sleep(1)

        outer_btn = page.locator('button[aria-label="Wallet connected"]').first
        await outer_btn.wait_for(state="visible", timeout=5000)
        page_ids_before = {id(p) for p in context.pages}

        # Только симуляция мыши (move → пауза → down → up), без JS — сайт может игнорировать нетrusted клики
        box = await outer_btn.bounding_box()
        if not box or not box.get("width") or not box.get("height"):
            logger.warning("Kami покупка: bounding_box недоступен, пробую locator.click(force=True)")
            await pay_btn.click(timeout=8000, force=True)
        else:
            cx = box["x"] + box["width"] * 0.25
            cy = box["y"] + box["height"] / 2
            logger.info("Kami покупка: клик Pay with (мышь: move → пауза → down → up)")
            await page.mouse.move(cx, cy)
            await asyncio.sleep(0.4)
            await page.mouse.down()
            await asyncio.sleep(0.08)
            await page.mouse.up()
        await asyncio.sleep(0.5)

        # Ждём появления новой вкладки (попап Rabby) — до 30 с; через 5 с повторный клик мышью
        sign_popup = None
        for attempt in range(30):
            await asyncio.sleep(1)
            for p in context.pages:
                try:
                    if id(p) not in page_ids_before and not p.is_closed():
                        sign_popup = p
                        logger.info("Kami покупка: открыт попап подписи (новая вкладка)")
                        break
                except Exception:
                    pass
            if sign_popup:
                break
            if attempt == 5:
                logger.info("Kami покупка: повторный клик Pay with (мышь)")
                box2 = await outer_btn.bounding_box()
                if box2 and box2.get("width") and box2.get("height"):
                    cx2 = box2["x"] + box2["width"] * 0.25
                    cy2 = box2["y"] + box2["height"] / 2
                    await page.mouse.move(cx2, cy2)
                    await asyncio.sleep(0.3)
                    await page.mouse.down()
                    await asyncio.sleep(0.08)
                    await page.mouse.up()
        if not sign_popup:
            for p in context.pages:
                if p is not page and not p.is_closed():
                    try:
                        if "chrome-extension://" in (p.url or ""):
                            sign_popup = p
                            logger.info("Kami покупка: использую вкладку расширения как попап")
                            break
                    except Exception:
                        pass

        if not sign_popup:
            raise RuntimeError("Не открылся попап подписи Rabby после клика Pay with")

        await sign_popup.wait_for_load_state("domcontentloaded", timeout=15000)
        try:
            await sign_popup.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        # Дождаться полной загрузки окна кошелька (кнопка Sign = контент транзакции отрисован)
        logger.info("Kami покупка: жду полной загрузки окна Rabby…")
        sign_btn_ready = sign_popup.locator(KAMI_RABBY_SIGN_BTN).first
        await sign_btn_ready.wait_for(state="visible", timeout=20000)
        await asyncio.sleep(1.0)  # дать дорисоваться тексту Simulation Results / деталям транзакции

        async def _rabby_ignore_sign_confirm(popup_page, label: str) -> None:
            try:
                ignore_all = popup_page.get_by_role("link", name=re.compile(r"Ignore all", re.I)).first
                await ignore_all.wait_for(state="visible", timeout=3000)
                await ignore_all.click(timeout=5000)
                logger.info(f"Kami покупка: нажато Ignore all ({label})")
                await asyncio.sleep(0.5)
            except Exception:
                try:
                    ignore_all = popup_page.locator('span.underline:has-text("Ignore all"), button:has-text("Ignore all")').first
                    await ignore_all.wait_for(state="visible", timeout=2000)
                    await ignore_all.click(timeout=5000)
                    logger.info(f"Kami покупка: нажато Ignore all ({label})")
                    await asyncio.sleep(0.5)
                except Exception:
                    pass
            logger.info(f"Kami покупка: Sign в Rabby ({label})…")
            sign_btn = popup_page.locator(KAMI_RABBY_SIGN_BTN).first
            await sign_btn.wait_for(state="visible", timeout=15000)
            await sign_btn.click(timeout=8000)
            await asyncio.sleep(1.5)
            logger.info(f"Kami покупка: Confirm в Rabby ({label})…")
            confirm_btn = popup_page.locator(KAMI_RABBY_CONFIRM_BTN).first
            try:
                await confirm_btn.wait_for(state="visible", timeout=10000)
                await confirm_btn.click(timeout=8000)
            except Exception:
                confirm_btn = popup_page.get_by_role("button", name=re.compile(r"Confirm", re.I)).first
                await confirm_btn.wait_for(state="visible", timeout=8000)
                await confirm_btn.click(timeout=8000)
            await asyncio.sleep(2)

        # Определить: покупка (только в ней есть Simulation Results) или апрув
        content = ""
        try:
            content = await sign_popup.content()
        except Exception:
            pass
        is_purchase = "Simulation Results" in content
        if is_purchase:
            logger.info("Kami покупка: окно Rabby — транзакция покупки (Simulation Results)")
        else:
            logger.info("Kami покупка: окно Rabby — апрув (ожидаю окно покупки после подписания)")

        await _rabby_ignore_sign_confirm(sign_popup, "первое окно")

        # Если это был апрув — ждём появления окна покупки (Simulation Results только в покупке) в том же попапе или новой вкладке
        if not is_purchase:
            logger.info("Kami покупка: жду окно транзакции покупки (Simulation Results)…")
            purchase_popup = None
            for _ in range(35):
                await asyncio.sleep(1)
                try:
                    content_next = await sign_popup.content()
                    if "Simulation Results" in content_next:
                        purchase_popup = sign_popup
                        break
                except Exception:
                    pass
                for p in context.pages:
                    if p is sign_popup or p is page:
                        continue
                    try:
                        if p.is_closed():
                            continue
                        c = await p.content()
                        if "Simulation Results" in c:
                            purchase_popup = p
                            break
                    except Exception:
                        pass
                if purchase_popup:
                    break
            if purchase_popup:
                logger.info("Kami покупка: окно покупки открыто, подписываю…")
                await asyncio.sleep(0.5)
                await _rabby_ignore_sign_confirm(purchase_popup, "покупка")
                if purchase_popup is not sign_popup:
                    await purchase_popup.close()
            await sign_popup.close()
        else:
            await sign_popup.close()

        # Ждём исчезновения "Transaction processing" на главной странице перед завершением
        logger.info("Kami покупка: жду исчезновения «Transaction processing» на странице…")
        try:
            processing_el = page.get_by_text(KAMI_TRANSACTION_PROCESSING_TEXT)
            await processing_el.wait_for(state="visible", timeout=15000)
            await processing_el.wait_for(state="hidden", timeout=120000)
            logger.info("Kami покупка: «Transaction processing» исчез")
        except Exception:
            await asyncio.sleep(5)
        purchase_ok = True
        logger.info("Kami: покупка отправлена, проверь портал")
    except Exception as e:
        logger.warning(f"Kami покупка: {e}")
    return purchase_ok


def _sync_kami_weeks_to_db(eoa_address: str, proxy_dict: Optional[dict]) -> None:
    """Читает прогресс Kami из портала и сохраняет в quest_results.json."""
    try:
        quests = get_kami_progress(eoa_address, proxies=proxy_dict)
        if not quests:
            return
        weeks_done = [q["isDone"] for q in quests]
        kwargs: dict = {"kami_done": bool(weeks_done) and all(weeks_done)}
        if len(weeks_done) > 0:
            kwargs["kami_week1_done"] = weeks_done[0]
        if len(weeks_done) > 1:
            kwargs["kami_week2_done"] = weeks_done[1]
        if len(weeks_done) > 2:
            kwargs["kami_week3_done"] = weeks_done[2]
        db.upsert_account(eoa_address, **kwargs)
        status = " | ".join(f"w{i+1}={'ok' if d else '-'}" for i, d in enumerate(weeks_done))
        logger.info(f"[{eoa_address}] Kami db обновлён: {status}")
    except Exception as e:
        logger.warning(f"[{eoa_address}] Kami sync db: {e}")


def run_kami_browser_for_account(
    adspower_api_key: str,
    eoa_address: str,
    private_key: str,
    proxy_dict: Optional[dict],
    cfg: dict,
    *,
    lifi_api_key: Optional[str] = None,
    firstmail_email: Optional[str] = None,
    firstmail_password: Optional[str] = None,
) -> None:
    """
    Один аккаунт: проверка портала → добор USDC.E через LI.FI при нехватке → браузер (AdsPower + Playwright).
    """
    # 1) Проверка портала — week1 уже выполнен?
    if proxy_dict:
        done = check_kami_week_done(eoa_address, week=1, proxies=proxy_dict)
    else:
        done = check_kami_week_done(eoa_address, week=1)
    if done is True:
        logger.info(f"[{eoa_address}] Kami week1 уже выполнен по порталу, пропуск")
        _sync_kami_weeks_to_db(eoa_address, proxy_dict)
        return
    if done is None:
        logger.warning(f"[{eoa_address}] Не удалось проверить портал, продолжаем")

    # 2) Добор USDC.E через LI.FI при нехватке (логика как в старой Kami)
    rpc_url = cfg.get("rpc_url") or "https://soneium-rpc.publicnode.com"
    asyncio.run(
        _ensure_usdce_balance(
            rpc_url, private_key, eoa_address, cfg, proxy_dict, lifi_api_key,
        )
    )

    # 3) Браузерный флоу: AdsPower + Playwright
    from modules.startale_gm import (
        _create_profile,
        _start_browser,
        _stop_browser,
        _delete_profile,
        _get_cdp_endpoint,
    )

    profile_id = None
    try:
        profile_id = _create_profile(adspower_api_key)
        browser_data = _start_browser(adspower_api_key, profile_id)
        cdp = _get_cdp_endpoint(browser_data)
        if not cdp:
            raise RuntimeError("AdsPower не вернул CDP endpoint")
        logger.info(f"[{eoa_address}] Браузер запущен, выполняю флоу Kami...")
        asyncio.run(
            _run_kami_browser_async(
                cdp, private_key, eoa_address, cfg,
                firstmail_email=firstmail_email,
                firstmail_password=firstmail_password or "",
            )
        )
        _sync_kami_weeks_to_db(eoa_address, proxy_dict)
    except Exception as e:
        logger.warning(f"Kami browser: {e}")
    finally:
        if profile_id:
            try:
                logger.info("Kami: остановка браузера, удаление профиля AdsPower")
                _stop_browser(adspower_api_key, profile_id)
                _delete_profile(adspower_api_key, profile_id)
            except Exception as e:
                logger.warning(f"Kami cleanup: {e}")
