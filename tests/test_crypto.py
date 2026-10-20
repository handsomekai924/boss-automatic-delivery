"""手机号 AES 加密测试，对拍登录页 `je()` 的 `re(phone, atob(E))`。

锁死密钥来源、AES-128-CBC + Pkcs7、`base64(iv ‖ ciphertext)` 三件事。
黄金样本由 CryptoJS 同参数算出，改实现前先想清楚要不要动它。
"""

from __future__ import annotations

import base64

import pytest

from boss_login.crypto import (
    ACCOUNT_AES_KEY,
    decrypt_account,
    encrypt_account,
)

GOLDEN_TOKEN = "MDEyMzQ1Njc4OWFiY2RlZup/CGILT5m101zxL7vapOc="
GOLDEN_IV = b"0123456789abcdef"


class TestAccountAes:
    """账号 AES 加解密与 CryptoJS 黄金样本对拍。"""

    def test_key_is_the_decoded_e_constant(self):
        """验密钥等于 `atob("clRwXUJBK1VKK0k0IWFbbQ==")`，16 字节 AES-128。"""
        assert ACCOUNT_AES_KEY == base64.b64decode("clRwXUJBK1VKK0k0IWFbbQ==")
        assert ACCOUNT_AES_KEY == b"rTp]BA+UJ+I4!a[m"
        assert len(ACCOUNT_AES_KEY) == 16

    def test_matches_cryptojs_layout(self):
        """验 `encrypt_account("13800138000", iv=GOLDEN_IV)` 的 CryptoJS 布局。"""
        token = encrypt_account("13800138000", iv=GOLDEN_IV)
        assert token == GOLDEN_TOKEN

        raw = base64.b64decode(token)
        assert raw[:16] == GOLDEN_IV, "IV 必须原样放在密文前面"
        assert len(raw) % 16 == 0, "Pkcs7 之后整块"

    def test_round_trip(self):
        """验加解密可逆，且同号不同 IV 密文不同。"""
        token = encrypt_account("13800138000")
        assert decrypt_account(token) == "13800138000"
        assert decrypt_account(GOLDEN_TOKEN) == "13800138000"
        assert encrypt_account("13800138000") != token

    def test_empty_stays_empty(self):
        """验空串入参对齐 `re()`：直接回空串。"""
        assert encrypt_account("") == ""
        assert decrypt_account("") == ""

    def test_bad_iv_rejected(self):
        """验 IV 长度不对时抛 `ValueError`。"""
        with pytest.raises(ValueError):
            encrypt_account("13800138000", iv=b"short")

    def test_garbage_token_rejected(self):
        """验无法解出明文的密文被拒绝。"""
        with pytest.raises(Exception):
            decrypt_account(base64.b64encode(b"only-iv-no-ct!!!!").decode())
