"""Web Push (VAPID) helper: key generation, subscription storage, and sending.

This module is best-effort: it will generate a VAPID keypair if missing (requires
'cryptography'), store subscriptions in a local JSON file, and send pushes using
'pywebpush' when available.

Files created:
- catalyst_scanner/vapid.json  -- stores private_key_pem and public_key (base64url)
- catalyst_scanner/push_subscriptions.json -- array of subscription objects
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict, Any

log = logging.getLogger("scanner.push")

ROOT = Path(__file__).resolve().parent
VAPID_PATH = ROOT / "vapid.json"
SUBS_PATH = ROOT / "push_subscriptions.json"


def _b64url(b: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def ensure_vapid_keys(subject: str = "mailto:you@example.com") -> Dict[str, str]:
    """Ensure a VAPID keypair exists and return {'private_key_pem','public_key_b64','subject'}"""
    if VAPID_PATH.exists():
        try:
            return json.loads(VAPID_PATH.read_text())
        except Exception:
            log.exception("failed reading vapid.json, regenerating")

    try:
        # Generate an EC P-256 keypair and export PEMs and public key as base64url
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives import serialization
    except Exception:
        log.exception("cryptography not available; cannot generate VAPID keys")
        # Return a placeholder so callers can proceed but sending will fail
        out = {"private_key_pem": "", "public_key_b64": "", "subject": subject}
        VAPID_PATH.write_text(json.dumps(out))
        return out

    private_key = ec.generate_private_key(ec.SECP256R1())
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")

    pub = private_key.public_key()
    nums = pub.public_numbers()
    x = nums.x.to_bytes(32, "big")
    y = nums.y.to_bytes(32, "big")
    uncompressed = b"\x04" + x + y
    public_b64 = _b64url(uncompressed)

    out = {"private_key_pem": private_pem, "public_key_b64": public_b64, "subject": subject}
    try:
        VAPID_PATH.write_text(json.dumps(out))
    except Exception:
        log.exception("unable to write vapid.json")
    return out


def load_subscriptions() -> list:
    if SUBS_PATH.exists():
        try:
            return json.loads(SUBS_PATH.read_text()) or []
        except Exception:
            log.exception("failed to read subscriptions file; starting fresh")
            return []
    return []


def save_subscriptions(subs: list) -> None:
    try:
        SUBS_PATH.write_text(json.dumps(subs))
    except Exception:
        log.exception("failed to write subscriptions file")


def add_subscription(sub: Dict[str, Any]) -> bool:
    subs = load_subscriptions()
    # dedupe by endpoint
    for s in subs:
        if s.get("endpoint") == sub.get("endpoint"):
            return False
    subs.append(sub)
    save_subscriptions(subs)
    return True


def remove_subscription(endpoint: str) -> bool:
    subs = load_subscriptions()
    new = [s for s in subs if s.get("endpoint") != endpoint]
    if len(new) == len(subs):
        return False
    save_subscriptions(new)
    return True


def send_webpush_to_subscription(sub: Dict[str, Any], data: str | bytes) -> bool:
    """Send a webpush to a single subscription using pywebpush (best-effort).
    Returns True on success, False otherwise.
    """
    try:
        from pywebpush import webpush, WebPushException
    except Exception:
        log.debug("pywebpush not installed; cannot send web push")
        return False

    vapid = ensure_vapid_keys()
    if not vapid.get("private_key_pem"):
        log.warning("VAPID private key not available; cannot send push")
        return False

    try:
        webpush(
            subscription_info=sub,
            data=(data if isinstance(data, str) else data.decode("utf-8")),
            vapid_private_key=vapid["private_key_pem"],
            vapid_claims={"sub": vapid.get("subject", "mailto:you@example.com")},
        )
        return True
    except Exception as e:
        # pywebpush raises WebPushException usually; log and return False
        log.exception("webpush failed: %s", e)
        return False


def send_push_to_all(payload: Dict[str, Any]) -> int:
    """Send payload to all stored subscriptions. Returns number of successes."""
    subs = load_subscriptions()
    if not subs:
        log.debug("no subscriptions to send")
        return 0
    successes = 0
    data = json.dumps(payload)
    for s in subs:
        try:
            ok = send_webpush_to_subscription(s, data)
            if ok:
                successes += 1
        except Exception:
            log.exception("send to subscription failed")
    return successes
