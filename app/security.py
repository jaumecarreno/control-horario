"""Security helpers."""

from __future__ import annotations

from werkzeug.security import check_password_hash, generate_password_hash


def hash_secret(raw_value: str) -> str:
    return generate_password_hash(raw_value, method="pbkdf2:sha256", salt_length=16)


def verify_secret(secret_hash: str, raw_value: str) -> bool:
    return check_password_hash(secret_hash, raw_value)

