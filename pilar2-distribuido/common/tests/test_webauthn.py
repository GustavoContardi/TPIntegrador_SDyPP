"""Firmas con passkey (WebAuthn): lo que verifica common.identity.verify."""

import hashlib
import json

import pytest

pytest.importorskip("cryptography", reason="requiere cryptography")

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

from common import config  # noqa: E402
from common.identity import generate_private_key, proposal_message, public_key_b64, verify  # noqa: E402
from common.identity.webauthn import b64url, encode_envelope  # noqa: E402

RP_ID = "voxchain.test"
ORIGIN = "https://voxchain.test"


@pytest.fixture(autouse=True)
def dominio(monkeypatch):
    monkeypatch.setattr(config, "WEBAUTHN_RP_IDS", [RP_ID])
    monkeypatch.setattr(config, "WEBAUTHN_ORIGINS", [ORIGIN])


def assertion(key, message: bytes, *, rp_id=RP_ID, origin=ORIGIN, flags=0x05,
              type_="webauthn.get", challenge=None, extra_client=None):
    """Arma una aserción como la de un autenticador real (WebAuthn §6.1, §5.8.1)."""
    auth_data = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + (0).to_bytes(4, "big")
    client = {"type": type_,
              "challenge": challenge or b64url(hashlib.sha256(message).digest()),
              "origin": origin, "crossOrigin": False, **(extra_client or {})}
    client_json = json.dumps(client, separators=(",", ":")).encode()
    sig = key.sign(auth_data + hashlib.sha256(client_json).digest(), ec.ECDSA(hashes.SHA256()))
    return encode_envelope(auth_data, client_json, sig)


@pytest.fixture
def ident():
    key = generate_private_key()
    return key, public_key_b64(key)


def msg(pub):
    return proposal_message(pub, "promulgacion", "h", "L1", "2026-09-26T00:00:00+00:00")


def test_firma_con_passkey_valida(ident):
    key, pub = ident
    assert verify(pub, msg(pub), assertion(key, msg(pub)))


def test_mensaje_distinto_falla(ident):
    """El challenge ata la firma al mensaje: no sirve para otra propuesta."""
    key, pub = ident
    otro = proposal_message(pub, "promulgacion", "h", "L2", "2026-09-26T00:00:00+00:00")
    assert not verify(pub, otro, assertion(key, msg(pub)))


def test_otra_clave_falla(ident):
    key, pub = ident
    otra = generate_private_key()
    assert not verify(pub, msg(pub), assertion(otra, msg(pub)))


def test_otro_rp_id_falla(ident):
    """Una passkey de otro sitio no vale acá aunque la firma sea correcta."""
    key, pub = ident
    assert not verify(pub, msg(pub), assertion(key, msg(pub), rp_id="phishing.test"))


def test_otro_origen_falla(ident):
    key, pub = ident
    assert not verify(pub, msg(pub), assertion(key, msg(pub), origin="https://evil.test"))


def test_cross_origin_falla(ident):
    key, pub = ident
    env = assertion(key, msg(pub), extra_client={"crossOrigin": True})
    assert not verify(pub, msg(pub), env)


@pytest.mark.parametrize("flags", [0x00, 0x01, 0x04])
def test_sin_presencia_o_sin_verificacion_falla(ident, flags):
    """Hace falta UP y UV: alguien presente y verificado (huella o PIN)."""
    key, pub = ident
    assert not verify(pub, msg(pub), assertion(key, msg(pub), flags=flags))


def test_tipo_create_falla(ident):
    """Una aserción de registro (webauthn.create) no es una firma."""
    key, pub = ident
    assert not verify(pub, msg(pub), assertion(key, msg(pub), type_="webauthn.create"))


@pytest.mark.parametrize("sobre", ["wa1.", "wa1.a.b", "wa1.!!.!!.!!", "wa1.a.b.c.d"])
def test_sobre_malformado_no_lanza(ident, sobre):
    _, pub = ident
    assert verify(pub, msg(pub), sobre) is False


def test_firma_cruda_sigue_funcionando(ident):
    """Las firmas P1363 de siempre (contraseña, CLI, mineros) no cambian."""
    from common.identity import sign
    key, pub = ident
    assert verify(pub, msg(pub), sign(key, msg(pub)))
