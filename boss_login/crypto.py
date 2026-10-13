"""AES 加密 —— 复刻登录页的 ``re(plain, atob(E))``。

来源是按需加载的 webpack chunk ``user-login.5398946e.js``（zhipin-sign/v5330）。
登录页里 ``re`` 被用了两次，明文不同、密钥和算法完全一样::

    je():        phone → encryptedAccount      （短信登录的表单）
    scanJump():  fingerprint → fp              （扫码登录 dispatcher 的 query）

``re(plaintext, key)`` 是 CryptoJS 默认参数下的 AES：

    key        = Utf8.parse(atob("clRwXUJBK1VKK0k0IWFbbQ=="))
    iv         = WordArray.random(16)
    ciphertext = AES.encrypt(Utf8.parse(plaintext), key, {iv}).ciphertext
    return     = Base64.stringify(iv.concat(ciphertext))

也就是 **AES-128-CBC + Pkcs7**，随机 16 字节 IV，输出 ``base64(iv ‖ ciphertext)``。
密钥是写死的 16 个 ASCII 字节（``atob`` 之后全是 < 0x80，所以 Utf8.parse 不改变它）。

**只做协议对齐**：这层只是把字符串按线上形状装进请求，不涉及任何风控绕过。
"""

from __future__ import annotations

import base64
import os

from cryptography.hazmat.primitives import padding as sym_padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

#: ``atob("clRwXUJBK1VKK0k0IWFbbQ==")`` —— 16 字节，AES-128
ACCOUNT_AES_KEY: bytes = base64.b64decode("clRwXUJBK1VKK0k0IWFbbQ==")

_BLOCK_BITS = 128
_IV_SIZE = 16


def encrypt_text(plaintext: str, *, iv: bytes | None = None) -> str:
    """登录页那个 ``re(plain, atob(E))``：AES-128-CBC + Pkcs7，``base64(iv‖ct)``。

    :param plaintext: 明文（UTF-8 编码）
    :param iv:        可注入的 16 字节 IV，默认 ``os.urandom(16)``。给测试对拍用。
    :returns:         ``base64(iv ‖ ciphertext)``，与 CryptoJS 的输出同形
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
    """把手机号加密成 ``encryptedAccount``。:func:`encrypt_text` 的别名。"""
    return encrypt_text(phone, iv=iv)


def decrypt_text(token: str) -> str:
    """:func:`encrypt_text` 的逆运算。

    线上用不到（服务端解），放这里是因为本地假服务端要从 ``encryptedAccount``
    还原出手机号才能记账，顺带也能对拍加密是否与 CryptoJS 一致。
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
    """:func:`encrypt_account` 的逆运算。:func:`decrypt_text` 的别名。"""
    return decrypt_text(token)
