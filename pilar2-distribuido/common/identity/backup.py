"""Respaldo cifrado de una identidad (el mismo formato que usa el frontend).

El navegador guarda la clave privada del ciudadano cifrada con su contraseña, y
ese mismo registro es el archivo de respaldo que el usuario descarga
(``identity.service.ts``, ``VaultRecord``). Este módulo lee y escribe ese
formato para que el CLI (``scripts/propose_law.py --backup``) pueda firmar con
una identidad creada en el navegador.

El formato es un contrato entre las dos puntas, por eso va versionado:

.. code-block:: json

    {
      "format": "voxchain-identity", "v": 1,
      "pubkey": "<SPKI DER en base64>",
      "username": "opcional",
      "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": 600000, "salt": "<b64>"},
      "cipher": {"name": "AES-GCM", "iv": "<b64, 12 bytes>"},
      "wrapped": "<b64: AES-GCM-256(PKCS#8 DER) con la tag al final>"
    }

- La clave AES sale de PBKDF2-HMAC-SHA256 sobre la contraseña **normalizada a
  NFC** (Web Crypto y Python tienen que ver los mismos bytes).
- ``pubkey`` va como dato autenticado (AAD) del cifrado: un registro al que le
  cambien la pubkey no se descifra.
- ``wrapped`` es lo que produce ``crypto.subtle.wrapKey('pkcs8', …, AES-GCM)``:
  el texto cifrado con la tag de 16 bytes pegada al final, que es exactamente
  lo que espera ``AESGCM.decrypt``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import unicodedata

FORMAT = "voxchain-identity"
VERSION = 1
ITERATIONS = 600_000
# Mismos límites que el frontend: por debajo sería trivial de forzar, por
# encima un archivo armado podría colgar al que lo abre.
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 10_000_000
MIN_PASSPHRASE = 10


class BackupError(ValueError):
    """El respaldo no tiene el formato esperado o la contraseña no corresponde."""


def _kek(passphrase: str, salt: bytes, iterations: int) -> bytes:
    normalized = unicodedata.normalize("NFC", passphrase).encode()
    return hashlib.pbkdf2_hmac("sha256", normalized, salt, iterations, dklen=32)


def encrypt_backup(private_key, passphrase: str, username: str | None = None) -> dict:
    """Cifra ``private_key`` (EC P-256 de ``cryptography``) en un respaldo v1."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import (
        Encoding, NoEncryption, PrivateFormat)

    from .signing import public_key_b64

    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"la contraseña necesita al menos {MIN_PASSPHRASE} caracteres")
    pubkey = public_key_b64(private_key)
    salt, iv = os.urandom(16), os.urandom(12)
    pkcs8 = private_key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption())
    wrapped = AESGCM(_kek(passphrase, salt, ITERATIONS)).encrypt(iv, pkcs8, pubkey.encode())
    record = {
        "format": FORMAT, "v": VERSION, "pubkey": pubkey,
        "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": ITERATIONS,
                "salt": base64.b64encode(salt).decode()},
        "cipher": {"name": "AES-GCM", "iv": base64.b64encode(iv).decode()},
        "wrapped": base64.b64encode(wrapped).decode(),
    }
    if username:
        record["username"] = username
    return record


def decrypt_backup(record: dict | str, passphrase: str):
    """Descifra un respaldo v1 y devuelve la clave privada EC.

    Además de descifrar, comprueba que la privada corresponda a la ``pubkey``
    del registro: la AAD ya lo garantiza para un registro íntegro, esto cubre un
    archivo armado a mano.
    """
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.serialization import load_der_private_key

    from .signing import public_key_b64

    if isinstance(record, str):
        try:
            record = json.loads(record)
        except json.JSONDecodeError as exc:
            raise BackupError("no es un respaldo de VoxChain (JSON inválido)") from exc
    try:
        if record["format"] != FORMAT or record["v"] != VERSION:
            raise BackupError("formato o versión de respaldo desconocidos")
        kdf, cipher = record["kdf"], record["cipher"]
        if kdf["name"] != "PBKDF2" or kdf["hash"] != "SHA-256" or cipher["name"] != "AES-GCM":
            raise BackupError("algoritmos de respaldo no soportados")
        iterations = kdf["iterations"]
        if not isinstance(iterations, int) or not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            raise BackupError("cantidad de iteraciones fuera de rango")
        pubkey = record["pubkey"]
        salt = base64.b64decode(kdf["salt"], validate=True)
        iv = base64.b64decode(cipher["iv"], validate=True)
        wrapped = base64.b64decode(record["wrapped"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, BackupError):
            raise
        raise BackupError("no es un respaldo de VoxChain") from exc

    try:
        pkcs8 = AESGCM(_kek(passphrase, salt, iterations)).decrypt(iv, wrapped, pubkey.encode())
    except InvalidTag as exc:
        raise BackupError("la contraseña no corresponde a ese respaldo") from exc

    key = load_der_private_key(pkcs8, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey) or public_key_b64(key) != pubkey:
        raise BackupError("la clave del respaldo no corresponde a su pubkey")
    return key


def load_backup(path: str, passphrase: str):
    """Lee un archivo de respaldo y devuelve la clave privada EC."""
    with open(path, encoding="utf-8") as fh:
        return decrypt_backup(fh.read(), passphrase)
