"""Versioned stdlib scrypt, with strictly bounded verification parameters."""

import hashlib
import secrets

N, R, P = 2**17, 8, 1
MAXMEM = 160 * 1024 * 1024


def password_bytes(password: str) -> bytes:
    value = password.encode("utf-8")
    if not 15 <= len(password) <= 1024 or len(value) > 4096:
        raise ValueError("password must contain 15..1024 characters and at most 4096 UTF-8 bytes")
    return value


def hash_password(password: str) -> str:
    value = password_bytes(password)
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(value, salt=salt, n=N, r=R, p=P, maxmem=MAXMEM, dklen=32)
    return f"scrypt$v1${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    # Unknown users and malformed hashes still perform the same bounded KDF.
    valid = False
    salt, expected = bytes(16), bytes(32)
    try:
        parts = (encoded or "").split("$")
        if len(parts) == 7 and parts[:5] == ["scrypt", "v1", str(N), str(R), str(P)]:
            if len(parts[5]) == 32 and len(parts[6]) == 64:
                salt, expected = bytes.fromhex(parts[5]), bytes.fromhex(parts[6])
                valid = len(salt) == 16 and len(expected) == 32
        value = password_bytes(password)
    except (ValueError, UnicodeError):
        value = b"invalid password"
        valid = False
    actual = hashlib.scrypt(value, salt=salt, n=N, r=R, p=P, maxmem=MAXMEM, dklen=32)
    return secrets.compare_digest(actual, expected) and valid
