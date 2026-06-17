"""
Enterprise WeChat / WeChat Customer Service — Message Encryption/Decryption.

Protocol (from official docs):
  - EncodingAESKey: 43-char base64 string. Actual AES key = Base64.decode(AESKey + "=")
  - Ciphertext structure: random(16 bytes) + msg_len(4 bytes, big-endian) + msg + corp_id
  - AES-256-CBC with PKCS#7 padding, IV = first 16 bytes of AES key
  - Signature: SHA1(sort(token, timestamp, nonce, echostr_or_encrypt))

Ref: https://developer.work.weixin.qq.com/document/path/90238
"""

import base64
import hashlib
import random
import string
import struct
import time
from typing import Tuple

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad


def _decode_aes_key(encoding_aes_key: str) -> bytes:
    """Convert the 43-char EncodingAESKey to a 32-byte AES key."""
    return base64.b64decode(encoding_aes_key + "=")


def _calc_signature(token: str, timestamp: str, nonce: str, encrypt: str) -> str:
    """Calculate SHA1 signature for verification."""
    params = sorted([token, timestamp, nonce, encrypt])
    raw = "".join(params)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def verify_signature(token: str, timestamp: str, nonce: str,
                     encrypt: str, msg_signature: str) -> bool:
    """Verify that the incoming request signature matches."""
    return _calc_signature(token, timestamp, nonce, encrypt) == msg_signature


def decrypt_message(encoding_aes_key: str, encrypted: str) -> Tuple[int, str, str]:
    """
    Decrypt an encrypted message from WeChat.

    Returns:
        Tuple of (status_code, plaintext_message, corp_id)
        status_code = 0 means success
    """
    aes_key = _decode_aes_key(encoding_aes_key)
    cipher = AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16])

    try:
        ciphertext = base64.b64decode(encrypted)
        plaintext = cipher.decrypt(ciphertext)

        # Remove PKCS#7 padding
        pad_len = plaintext[-1]
        if pad_len < 1 or pad_len > 32:
            return (-40001, "", "")
        plaintext = plaintext[:-pad_len]

        # Parse: random(16) + msg_len(4) + msg + corp_id
        if len(plaintext) < 20:
            return (-40002, "", "")

        # random = plaintext[:16]
        msg_len = struct.unpack("!I", plaintext[16:20])[0]
        msg = plaintext[20:20 + msg_len].decode("utf-8")
        corp_id = plaintext[20 + msg_len:].decode("utf-8")

        return (0, msg, corp_id)
    except Exception:
        return (-40003, "", "")


def encrypt_message(encoding_aes_key: str, msg: str, corp_id: str) -> str:
    """
    Encrypt a reply message for WeChat.

    Returns:
        Base64-encoded encrypted string.
    """
    aes_key = _decode_aes_key(encoding_aes_key)

    # Build: random(16) + msg_len(4) + msg_bytes + corp_id_bytes
    random_bytes = bytes(random.getrandbits(8) for _ in range(16))
    msg_bytes = msg.encode("utf-8")
    msg_len = struct.pack("!I", len(msg_bytes))
    corp_id_bytes = corp_id.encode("utf-8")

    plaintext = random_bytes + msg_len + msg_bytes + corp_id_bytes
    plaintext = pad(plaintext, 32)  # AES block size = 32 for 256-bit key? No, AES block is always 16 bytes

    cipher = AES.new(aes_key, AES.MODE_CBC, iv=aes_key[:16])
    ciphertext = cipher.encrypt(plaintext)
    return base64.b64encode(ciphertext).decode("utf-8")


def generate_echostr_reply(encoding_aes_key: str, echostr: str, corp_id: str) -> str:
    """Decrypt echostr for URL verification (GET request)."""
    _, plaintext, _ = decrypt_message(encoding_aes_key, echostr)
    return plaintext


# ============================================================
# Access token cache (simple in-memory)
# ============================================================
_access_token: dict = {"value": "", "expires_at": 0}


async def get_access_token(corp_id: str, app_secret: str) -> str:
    """
    Get a cached or fresh access_token for calling WeChat APIs.
    Token is valid for 7200 seconds; we refresh 5 min before expiry.
    """
    import httpx

    now = time.time()
    if _access_token["value"] and _access_token["expires_at"] > now + 300:
        return _access_token["value"]

    url = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
    params = {"corpid": corp_id, "corpsecret": app_secret}

    async with httpx.AsyncClient() as client:
        resp = await client.get(url, params=params, timeout=10)
        data = resp.json()

    if data.get("errcode") != 0:
        raise RuntimeError(f"Failed to get access_token: {data}")

    _access_token["value"] = data["access_token"]
    _access_token["expires_at"] = now + data.get("expires_in", 7200)
    return _access_token["value"]
