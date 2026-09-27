"""Respaldo cifrado de identidad: el formato que comparten el navegador y el CLI."""

import base64
import json

import pytest

pytest.importorskip("cryptography", reason="requiere cryptography")

from common.identity import (  # noqa: E402
    BackupError,
    decrypt_backup,
    encrypt_backup,
    generate_private_key,
    proposal_message,
    public_key_b64,
    sign,
    verify,
)
import common.identity.backup as backup  # noqa: E402

PASS = "una frase larga de prueba"


@pytest.fixture(autouse=True)
def pocas_iteraciones(monkeypatch):
    """600k iteraciones por test harían lenta la suite; el formato es el mismo."""
    monkeypatch.setattr(backup, "ITERATIONS", backup.MIN_ITERATIONS)


def test_ida_y_vuelta_firma_con_la_misma_identidad():
    key = generate_private_key()
    record = encrypt_backup(key, PASS, username="Ana")
    assert record["format"] == "voxchain-identity" and record["v"] == 1
    assert record["username"] == "Ana"

    restored = decrypt_backup(json.dumps(record), PASS)
    pub = public_key_b64(restored)
    assert pub == public_key_b64(key) == record["pubkey"]
    msg = proposal_message(pub, "promulgacion", "h", "L1", "t0")
    assert verify(pub, msg, sign(restored, msg))


def test_contraseña_equivocada():
    record = encrypt_backup(generate_private_key(), PASS)
    with pytest.raises(BackupError, match="contraseña"):
        decrypt_backup(record, PASS + "x")


def test_contraseña_normalizada_nfc():
    """'á' compuesta y descompuesta son la misma contraseña (como en el navegador)."""
    compuesta, descompuesta = "contraseña ácida", "contraseña ácida"
    record = encrypt_backup(generate_private_key(), compuesta)
    decrypt_backup(record, descompuesta)


def test_pubkey_cambiada_no_descifra():
    """La pubkey es AAD: no se puede hacer pasar el respaldo de uno por el de otro."""
    record = encrypt_backup(generate_private_key(), PASS)
    record["pubkey"] = public_key_b64(generate_private_key())
    with pytest.raises(BackupError):
        decrypt_backup(record, PASS)


def test_cifrado_alterado_no_descifra():
    record = encrypt_backup(generate_private_key(), PASS)
    raw = bytearray(base64.b64decode(record["wrapped"]))
    raw[0] ^= 1
    record["wrapped"] = base64.b64encode(bytes(raw)).decode()
    with pytest.raises(BackupError):
        decrypt_backup(record, PASS)


@pytest.mark.parametrize("iterations", [1000, 50_000_000, "600000"])
def test_iteraciones_fuera_de_rango(iterations):
    record = encrypt_backup(generate_private_key(), PASS)
    record["kdf"]["iterations"] = iterations
    with pytest.raises(BackupError, match="iteraciones"):
        decrypt_backup(record, PASS)


@pytest.mark.parametrize("texto", ["no es json", "{}", '{"format": "otra-cosa", "v": 1}'])
def test_no_es_un_respaldo(texto):
    with pytest.raises(BackupError):
        decrypt_backup(texto, PASS)


def test_contraseña_corta_no_cifra():
    with pytest.raises(BackupError):
        encrypt_backup(generate_private_key(), "corta")


# Generado por el frontend real (Chrome, identity.service.ts) con la contraseña
# de abajo: es lo que garantiza que Web Crypto y este módulo hablan el mismo
# formato (PBKDF2 → AES-GCM con la pubkey como AAD, tag al final). Es una
# identidad de prueba descartable.
VECTOR_NAVEGADOR = {
    "format": "voxchain-identity", "v": 1,
    "pubkey": "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE7alYbB6QqxULFfpq1gAvv1Y8bhjgBcN24Gqt6YqMwh7jw5lBciX0WWSwjRKb35fRfNMD/bHBolAAGJLAcAMHqQ==",
    "username": "Cifrada",
    "kdf": {"name": "PBKDF2", "hash": "SHA-256", "iterations": 600000,
            "salt": "rTF6BEESzcIrYc7t03hvFg=="},
    "cipher": {"name": "AES-GCM", "iv": "/PNGLLJcYNO6bpKv"},
    "wrapped": "twFU+ynid61FNqcxOHaOtE3/+nJQHo15yf+HqwXQiPTGVt7OGTnDiB26K9RavW29vekyYipuaC1XH0Ko482k7lUoKx8BWxpCkif0lHOOKT9/hRtfR5zNQAgEXiVQDR5weWUaJiCizkMRcGElCjEmvKpjRr7agWV8eaDBtjItBA1llsBXPfxVWtPLVHk9V5jqdnAPi3h/3Hn4Vg==",
}


def test_respaldo_del_navegador_se_descifra_en_python():
    key = decrypt_backup(VECTOR_NAVEGADOR, "frase de prueba uno")
    assert public_key_b64(key) == VECTOR_NAVEGADOR["pubkey"]
    with pytest.raises(BackupError):
        decrypt_backup(VECTOR_NAVEGADOR, "frase de prueba dos")
