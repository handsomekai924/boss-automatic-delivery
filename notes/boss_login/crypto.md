# `boss_login.crypto` 协议笔记

> 摘自 `boss_login/crypto.py` 的模块文档字符串 / `#` 注释。

## 一句话

AES 加密 —— 复刻登录页的 ``re(plain, atob(E))``。

## 细节

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

### `ACCOUNT_AES_KEY`

``atob("clRwXUJBK1VKK0k0IWFbbQ==")`` —— 16 字节，AES-128。
