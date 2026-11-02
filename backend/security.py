"""Passwords, sessions and exchange credentials; secrets never reach API responses."""
import base64
import hashlib
import hmac
import os
import secrets


def hash_password(password: str) -> str:
    if len(password) < 10:
        raise ValueError("密码至少 10 位")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1)
        return hmac.compare_digest(actual.hex(), expected)
    except (ValueError, TypeError):
        return False


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def cipher():
    from cryptography.fernet import Fernet
    key = os.environ.get("MK_MASTER_KEY")
    if not key:
        raise ValueError("实盘凭证需要 MK_MASTER_KEY；请使用 Fernet.generate_key() 生成并独立保管")
    try:
        if len(base64.urlsafe_b64decode(key)) != 32:
            raise ValueError()
        return Fernet(key.encode())
    except Exception as exc:
        raise ValueError("MK_MASTER_KEY 必须为 32 字节的 Fernet 密钥") from exc


def encrypt_secret(value: str) -> str:
    return cipher().encrypt(value.encode()).decode()


def decrypt_secret(value: str) -> str:
    return cipher().decrypt(value.encode()).decode()
