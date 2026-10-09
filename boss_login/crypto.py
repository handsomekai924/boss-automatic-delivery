"""手机号 AES 加密 —— 复刻登录页 ``je()`` 中间件里的 ``re(phone, atob(E))``。

CryptoJS 默认参数下的 **AES-128-CBC + Pkcs7**，随机 16 字节 IV，输出
``base64(iv ‖ ciphertext)``::

    key        = Utf8.parse(atob("clRwXUJBK1VKK0k0IWFbbQ=="))
    iv         = WordArray.random(16)
    ciphertext = AES.encrypt(Utf8.parse(plaintext), key, {iv}).ciphertext
    return     = Base64.stringify(iv.concat(ciphertext))

密钥是写死的 16 个 ASCII 字节（``atob`` 之后全 < 0x80，``Utf8.parse`` 不改变它）。

**只做协议对齐**：这层只是把手机号按线上形状装进表单，不涉及任何风控绕过。
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


def encrypt_account(phone: str, *, iv: bytes | None = None) -> str:
    """把手机号加密成 ``encryptedAccount``。

    :param phone: 明文手机号（会被 UTF-8 编码，线上是纯数字，等同 ASCII）
    :param iv:    可注入的 16 字节 IV，默认 ``os.urandom(16)``。给测试对拍用。
    :returns:     ``base64(iv ‖ ciphertext)``，与 CryptoJS 的输出同形
    """
    if not phone:
        return ""
    iv = iv if iv is not None else os.urandom(_IV_SIZE)
    if len(iv) != _IV_SIZE:
        raise ValueError(f"IV 必须是 {_IV_SIZE} 字节，收到 {len(iv)}")

    padder = sym_padding.PKCS7(_BLOCK_BITS).padder()
    padded = padder.update(phone.encode("utf-8")) + padder.finalize()
    encryptor = Cipher(algorithms.AES(ACCOUNT_AES_KEY), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def decrypt_account(token: str) -> str:
    """:func:`encrypt_account` 的逆运算。

    线上用不到（服务端解），放这里是因为本地假服务端要从 ``encryptedAccount``
    还原出手机号才能记账，顺带也能对拍加密是否与 CryptoJS 一致。
    """
    if not token:
        return ""
    raw = base64.b64decode(token)
    if len(raw) <= _IV_SIZE:
        raise ValueError("encryptedAccount 太短，缺 IV 或密文")
    iv, ciphertext = raw[:_IV_SIZE], raw[_IV_SIZE:]
    decryptor = Cipher(algorithms.AES(ACCOUNT_AES_KEY), modes.CBC(iv)).decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()
    unpadder = sym_padding.PKCS7(_BLOCK_BITS).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode("utf-8")
