"""AES 加解密，对齐登录页 `re(plain, atob(E))` 的输出形状。

算法为 AES-128-CBC + Pkcs7，随机 16 字节 IV，结果为 `base64(iv ‖ ciphertext)`。

Attributes:
    ACCOUNT_AES_KEY: AES-128 密钥，16 字节。
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

ACCOUNT_AES_KEY: bytes = base64.b64decode("clRwXUJBK1VKK0k0IWFbbQ==")

_BLOCK_BITS = 128
_IV_SIZE = 16


def encrypt_text(plaintext: str, *, iv: bytes | None = None) -> str:
    """AES-128-CBC + Pkcs7 加密，输出 `base64(iv ‖ ciphertext)`。

    Args:
        plaintext: 明文，按 UTF-8 编码。
        iv: 可注入的 16 字节 IV，缺省随机；供测试对拍。长度不符时抛 `ValueError`。

    Returns:
        与 CryptoJS 同形的密文字符串；空明文返回空串。
    """
    if not plaintext:
        return ""
    iv = iv if iv is not None else os.urandom(_IV_SIZE)
    if len(iv) != _IV_SIZE:
        raise ValueError(f"IV 必须是 {_IV_SIZE} 字节，收到 {len(iv)}")

    padder = sym_padding.PKCS7(_BLOCK_BITS).padder()
    padded = padder.update(plaintext.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(ACCOUNT_AES_KEY), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def encrypt_account(phone: str, *, iv: bytes | None = None) -> str:
    """把手机号加密成 `encryptedAccount`；`encrypt_text` 的别名。"""
    return encrypt_text(phone, iv=iv)


def decrypt_text(token: str) -> str:
    """`encrypt_text` 的逆运算；格式非法时抛 `ValueError`。

    线上用不到（服务端解），供本地假服务端还原手机号、对拍加密结果。
    """
    if not token:
        return ""
    raw = base64.b64decode(token)
    if len(raw) <= _IV_SIZE:
        raise ValueError("密文太短，缺 IV 或密文")
    iv, ciphertext = raw[:_IV_SIZE], raw[_IV_SIZE:]
    decryptor = Cipher(algorithms.AES(ACCOUNT_AES_KEY), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(_BLOCK_BITS).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")


def decrypt_account(token: str) -> str:
    """`encrypt_account` 的逆运算；`decrypt_text` 的别名。"""
    return decrypt_text(token)
