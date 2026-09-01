"""
Kalshi API request signing.

Kalshi authenticates every request (including WebSocket upgrades and,
notably, PUBLIC market-data channels) using an RSA-PSS signature over:

    timestamp_ms + HTTP_METHOD + path

where `path` is the request path WITHOUT the query string, and the
signature is base64-encoded and sent in headers alongside your key id and
timestamp.

Common failure mode: including query parameters in the signed string.
Always sign the bare path only.
"""

import base64
import time
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

import config


class KalshiSigner:
    def __init__(self, key_id: str | None = None, private_key_path: str | None = None):
        self.key_id = key_id or config.KALSHI_API_KEY_ID
        key_path = private_key_path or config.KALSHI_PRIVATE_KEY_PATH

        if not self.key_id or not key_path:
            raise ValueError(
                "Missing Kalshi credentials. Set KALSHI_API_KEY_ID and "
                "KALSHI_PRIVATE_KEY_PATH environment variables. "
                "Generate these from the Kalshi dashboard (use the DEMO "
                "environment's key while testing)."
            )

        self._private_key = self._load_private_key(key_path)

    @staticmethod
    def _load_private_key(path: str) -> rsa.RSAPrivateKey:
        key_bytes = Path(path).read_bytes()
        key = serialization.load_pem_private_key(key_bytes, password=None)
        if not isinstance(key, rsa.RSAPrivateKey):
            raise TypeError("Kalshi private key must be an RSA key.")
        return key

    def sign(self, method: str, path: str) -> dict[str, str]:
        """
        Returns the headers needed to authenticate a REST request or a
        WebSocket upgrade.

        `path` must be the bare path (no scheme, host, or query string),
        e.g. "/trade-api/v2/markets" or "/trade-api/ws/v2".
        """
        timestamp_ms = str(int(time.time() * 1000))
        message = f"{timestamp_ms}{method.upper()}{path}".encode("utf-8")

        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        signature_b64 = base64.b64encode(signature).decode("utf-8")

        return {
            "KALSHI-ACCESS-KEY": self.key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature_b64,
        }
