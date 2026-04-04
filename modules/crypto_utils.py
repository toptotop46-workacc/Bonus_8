#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Шифрование/дешифрование keys.txt → keys.enc.

Алгоритм: AES-256-GCM с ключом из PBKDF2-HMAC-SHA256 (200 000 итераций).
Формат файла keys.enc:
  [16 байт magic] [32 байта salt] [12 байт nonce] [ciphertext + 16 байт GCM-tag]
"""
from __future__ import annotations

import getpass
import os
import sys

import questionary

from modules import logger

MAGIC = b"SONEIUM_ENC_V1\n"  # 16 байт
SALT_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16
KDF_ITERATIONS = 200_000


def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 → 32-байтный ключ AES-256."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=KDF_ITERATIONS,
    )
    return kdf.derive(password.encode("utf-8"))


def encrypt_keys(plaintext: str, password: str) -> bytes:
    """Шифрует строку (содержимое keys.txt) и возвращает байты для записи в keys.enc."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    salt = os.urandom(SALT_LEN)
    nonce = os.urandom(NONCE_LEN)
    key = _derive_key(password, salt)
    ct_with_tag = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return MAGIC + salt + nonce + ct_with_tag


def decrypt_keys(data: bytes, password: str) -> str:
    """
    Дешифрует содержимое keys.enc и возвращает plaintext.
    Raises ValueError при неверном пароле или повреждённом файле.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.exceptions import InvalidTag

    if not data.startswith(MAGIC):
        raise ValueError("Неизвестный формат файла (неверный magic)")

    offset = len(MAGIC)
    salt = data[offset: offset + SALT_LEN]
    offset += SALT_LEN
    nonce = data[offset: offset + NONCE_LEN]
    offset += NONCE_LEN
    ct_with_tag = data[offset:]

    key = _derive_key(password, salt)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ct_with_tag, None)
    except InvalidTag:
        raise ValueError("Неверный пароль или файл повреждён")

    return plaintext.decode("utf-8")


def prompt_password_new() -> str:
    """Запрашивает новый пароль дважды (с подтверждением)."""
    while True:
        pwd = getpass.getpass("  Введите пароль для шифрования: ")
        if not pwd:
            print("  Пароль не может быть пустым, попробуйте снова.")
            continue
        pwd2 = getpass.getpass("  Подтвердите пароль: ")
        if pwd != pwd2:
            print("  Пароли не совпадают, попробуйте снова.")
            continue
        return pwd


def prompt_password() -> str:
    """Запрашивает пароль для расшифровки."""
    return getpass.getpass("  Пароль для keys.enc: ")


def offer_encryption(keys_path, enc_path) -> bool:
    """
    Предлагает пользователю зашифровать keys.txt.
    Возвращает True если файл был зашифрован.
    """
    print()
    logger.warning("Обнаружен незашифрованный keys.txt.")
    do_encrypt = questionary.confirm(
        "Зашифровать приватные ключи в keys.enc (рекомендуется)?",
        default=True,
    ).ask()

    if not do_encrypt:
        return False

    plaintext = keys_path.read_text(encoding="utf-8")
    password = prompt_password_new()

    logger.info("Шифрование... (может занять пару секунд)")
    encrypted = encrypt_keys(plaintext, password)
    enc_path.write_bytes(encrypted)
    logger.success(f"Ключи зашифрованы → {enc_path.name}")

    delete_plain = questionary.confirm(
        f"Удалить {keys_path.name} (оригинал)?",
        default=True,
    ).ask()
    if delete_plain:
        keys_path.unlink()
        logger.success(f"{keys_path.name} удалён.")
    else:
        logger.warning(f"{keys_path.name} оставлен. Убедитесь, что он не попадёт в git или облако!")

    return True


def load_keys_plaintext(project_root) -> str:
    """
    Возвращает plaintext содержимое ключей.
    Логика:
      1. keys.enc существует → запросить пароль → расшифровать
      2. keys.txt существует → предложить шифрование → вернуть plaintext
      3. Ни того ни другого → sys.exit(1)
    """
    from pathlib import Path
    keys_path = Path(project_root) / "keys.txt"
    enc_path  = Path(project_root) / "keys.enc"

    if enc_path.exists():
        for attempt in range(3):
            if attempt > 0:
                logger.warning(f"Попытка {attempt + 1}/3...")
            try:
                password = prompt_password()
                plaintext = decrypt_keys(enc_path.read_bytes(), password)
                logger.success("Ключи успешно расшифрованы.")
                return plaintext
            except ValueError as e:
                logger.error(str(e))
        logger.error("Превышено число попыток ввода пароля.")
        sys.exit(1)

    if keys_path.exists():
        offer_encryption(keys_path, enc_path)
        # После шифрования читаем из enc если оно появилось, иначе из plaintext
        if enc_path.exists():
            return load_keys_plaintext(project_root)
        return keys_path.read_text(encoding="utf-8")

    logger.error("Не найден ни keys.txt, ни keys.enc")
    sys.exit(1)
