"""Tests for Kalshi RSA-PSS request signing."""

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from kalshi_client.auth import auth_headers, sign_path_for_url, sign_request


def _generate_test_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_sign_path_includes_trade_api_prefix():
    base = "https://api.elections.kalshi.com/trade-api/v2"
    assert sign_path_for_url(base, "/portfolio/balance") == "/trade-api/v2/portfolio/balance"
    assert sign_path_for_url(base, "/exchange/status") == "/trade-api/v2/exchange/status"


def test_sign_path_strips_query_string():
    base = "https://demo-api.kalshi.co/trade-api/v2"
    path = sign_path_for_url(base, "/portfolio/orders?limit=5")
    assert path == "/trade-api/v2/portfolio/orders"
    assert "?" not in path


def test_sign_request_verifies_with_public_key():
    private_key = _generate_test_key()
    public_key = private_key.public_key()
    message = b"1703123456789GET/trade-api/v2/portfolio/balance"
    signature_b64 = sign_request(
        private_key, "1703123456789", "GET", "/trade-api/v2/portfolio/balance"
    )
    import base64
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    public_key.verify(
        base64.b64decode(signature_b64),
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )


def test_auth_headers_contain_kalshi_access_fields():
    private_key = _generate_test_key()
    headers = auth_headers(
        private_key,
        "test-api-key-id",
        "1703123456789",
        "GET",
        "/trade-api/v2/portfolio/balance",
    )
    assert headers["KALSHI-ACCESS-KEY"] == "test-api-key-id"
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == "1703123456789"
    assert headers["KALSHI-ACCESS-SIGNATURE"]


def test_load_private_key_from_pem_file(tmp_path):
    private_key = _generate_test_key()
    pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "kalshi-test.pem"
    key_path.write_bytes(pem)

    from kalshi_client.auth import load_private_key

    loaded = load_private_key(key_path)
    assert loaded.key_size == 2048
