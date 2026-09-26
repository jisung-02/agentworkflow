"""Verify signed requests before parsing channel payloads."""

import hashlib
import hmac
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey


def verify_slack(
    signing_secret: str,
    timestamp: str,
    signature: str,
    body: bytes,
    now: float | None = None,
) -> bool:
    if not timestamp.isascii() or not timestamp.isdecimal():
        return False
    current = time.time() if now is None else now
    if abs(current - int(timestamp)) > 300:
        return False
    base = b"v0:" + timestamp.encode("ascii") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest("v0=" + digest, signature)


def verify_discord(public_key_hex: str, timestamp: str, signature_hex: str, body: bytes) -> bool:
    try:
        key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex))
        key.verify(bytes.fromhex(signature_hex), timestamp.encode("utf-8") + body)
    except (ValueError, InvalidSignature):
        return False
    return True
