"""Signed requests between a relay and the central server (Ed25519).

The relay keeps its private key; the server only ever stores the public key, so a
leaked database gives nobody a usable key. Each request is signed over its path,
a timestamp, a one-off nonce and a hash of the body, so a captured request can't
be replayed or altered.

    X-PC-Key:   <key id the server issued at pairing>
    X-PC-Time:  <unix seconds>
    X-PC-Nonce: <16 random bytes, base64url>
    X-PC-Sig:   base64url(Ed25519 over "POST\\n<path>\\n<time>\\n<nonce>\\n<sha256(body) hex>")
"""

import base64
import hashlib
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

HEADER_KEY = "X-PC-Key"
HEADER_TIME = "X-PC-Time"
HEADER_NONCE = "X-PC-Nonce"
HEADER_SIG = "X-PC-Sig"
SIGNED_HEADERS = (HEADER_KEY, HEADER_TIME, HEADER_NONCE, HEADER_SIG)


def b64e(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64d(text):
    if not isinstance(text, str) or len(text) > 200:
        raise ValueError("bad base64")
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(method, path, timestamp, nonce, body):
    return f"{method}\n{path}\n{timestamp}\n{nonce}\n{hashlib.sha256(body).hexdigest()}".encode()


def new_private_key():
    return Ed25519PrivateKey.generate()


def private_bytes(key):
    return key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                             serialization.NoEncryption())


def private_from_bytes(raw):
    return Ed25519PrivateKey.from_private_bytes(raw)


def public_bytes(key):
    """The 32-byte public key, from a private or public key."""
    if isinstance(key, Ed25519PrivateKey):
        key = key.public_key()
    return key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def public_from_bytes(raw):
    if len(raw) != 32:
        raise ValueError("an Ed25519 public key is 32 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def sign_headers(private_key, key_id, path, body, method="POST", now=None):
    """Headers for a signed request."""
    timestamp = str(int(time.time() if now is None else now))
    nonce = b64e(secrets.token_bytes(16))
    sig = private_key.sign(canonical(method, path, timestamp, nonce, body))
    return {HEADER_KEY: key_id, HEADER_TIME: timestamp, HEADER_NONCE: nonce, HEADER_SIG: b64e(sig)}


def verify(public_key_bytes, method, path, timestamp, nonce, body, sig_b64):
    """True if the signature is good. Never raises on bad input."""
    try:
        public_from_bytes(public_key_bytes).verify(b64d(sig_b64), canonical(method, path, timestamp, nonce, body))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
