"""手机号 AES 加密 —— 与登录页 ``je()`` 中间件的 ``re(phone, atob(E))`` 对拍。

这一层看着像「随手加个密」，但表单里叫 ``encryptedAccount`` 还是 ``phone``
是服务端说了算的：键名或密文形状不对，``send/smsCodeV2`` 不会认。
所以这里锁死三件事：密钥来源、AES-128-CBC + Pkcs7、``base64(iv ‖ ciphertext)``。

黄金样本是拿 CryptoJS 同参数算出来的，改实现前先想清楚要不要动它。
"""

from __future__ import annotations

import base64

import pytest

from boss_login.crypto import (
    ACCOUNT_AES_KEY,
    decrypt_account,
    encrypt_account,
)

#: encrypt_account("13800138000", iv=b"0123456789abcdef") —— 见 test_matches_cryptojs_layout
GOLDEN_TOKEN = "MDEyMzQ1Njc4OWFiY2RlZup/CGILT5m101zxL7vapOc="
GOLDEN_IV = b"0123456789abcdef"


class TestAccountAes:
    def test_key_is_the_decoded_e_constant(self):
        """``atob("clRwXUJBK1VKK0k0IWFbbQ==")`` —— 16 字节，AES-128。"""
        assert ACCOUNT_AES_KEY == base64.b64decode("clRwXUJBK1VKK0k0IWFbbQ==")
        assert ACCOUNT_AES_KEY == b"rTp]BA+UJ+I4!a[m"
        assert len(ACCOUNT_AES_KEY) == 16

    def test_matches_cryptojs_layout(self):
        """CryptoJS 默认 AES：CBC + Pkcs7，输出 Base64(iv.concat(ciphertext))。"""
        token = encrypt_account("13800138000", iv=GOLDEN_IV)
        assert token == GOLDEN_TOKEN

        raw = base64.b64decode(token)
        assert raw[:16] == GOLDEN_IV, "IV 必须原样放在密文前面"
        assert len(raw) % 16 == 0, "Pkcs7 之后整块"

    def test_round_trip(self):
        token = encrypt_account("13800138000")
        assert decrypt_account(token) == "13800138000"
        assert decrypt_account(GOLDEN_TOKEN) == "13800138000"
        # 同号不同 IV，密文必须不一样（IV 是随机的）
        assert encrypt_account("13800138000") != token

    def test_empty_stays_empty(self):
        """``re()`` 在明文或密钥为空时直接回空串，这里对齐。"""
        assert encrypt_account("") == ""
        assert decrypt_account("") == ""

    def test_bad_iv_rejected(self):
        with pytest.raises(ValueError):
            encrypt_account("13800138000", iv=b"short")

    def test_garbage_token_rejected(self):
        with pytest.raises(Exception):
            decrypt_account(base64.b64encode(b"only-iv-no-ct!!!!").decode())
