"""
Kalshi API request signing (RSA-PSS + SHA256).

See: https://docs.kalshi.com/getting_started/quick_start_authenticated_requests
"""

from __future__ import annotations

import base64
from pathlib import Path
from urllib.parse import urlparse

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def load_private_key(key_path: str | Path) -> rsa.RSAPrivateKey:
    """Load an RSA private key from a PEM file (Kalshi .key download)."""
    path = Path(key_path).expanduser()
    if not path.is_file():
        raise FileNotFoundError(f"Kalshi private key not found: {path}")
    data = path.read_bytes()
    private_key = serialization.load_pem_private_key(
        data,
        password=None,
        backend=default_backend(),
    )
    if not isinstance(private_key, rsa.RSAPrivateKey):
        raise ValueError(f"Kalshi private key must be an RSA private key: {path}")
    return private_key


def sign_path_for_url(base_url: str, endpoint: str) -> str:
    """
    Full URL path used in the signature, e.g. /trade-api/v2/portfolio/balance.
    Query strings are stripped before signing.
    """
    endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
    full = f"{base_url.rstrip('/')}{endpoint}"
    return urlparse(full).path.split("?")[0]


def sign_request(
    private_key: rsa.RSAPrivateKey,
    timestamp_ms: str,
    method: str,
    sign_path: str,
) -> str:
    """Create base64 RSA-PSS signature for Kalshi authenticated requests."""
    path_without_query = sign_path.split("?")[0]
    message = f"{timestamp_ms}{method.upper()}{path_without_query}".encode("utf-8")
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("utf-8")


def auth_headers(
    private_key: rsa.RSAPrivateKey,
    api_key_id: str,
    timestamp_ms: str,
    method: str,
    sign_path: str,
) -> dict[str, str]:
    """Build KALSHI-ACCESS-* headers for an authenticated request."""
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": sign_request(
            private_key, timestamp_ms, method, sign_path
        ),
    }
