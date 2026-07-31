"""Generates a VAPID key pair in the format expected by .env (one-off)."""
import base64

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


key = ec.generate_private_key(ec.SECP256R1())
private = key.private_numbers().private_value.to_bytes(32, "big")
public = key.public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
)

print("VAPID_PRIVATE_KEY=" + b64u(private))
print("VAPID_PUBLIC_KEY=" + b64u(public))
