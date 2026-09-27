"""Firmas hechas con passkey (WebAuthn), verificadas sin estado del lado servidor.

Una identidad con passkey tiene su clave privada en el autenticador del
dispositivo (Secure Enclave, TPM, una llave física o el gestor de passkeys) y
**cada firma exige un gesto del usuario** —huella, cara o PIN—. Es lo que ni la
clave no extraíble (nivel 1) ni la contraseña (nivel 2) podían dar: un XSS no
puede firmar en silencio, porque no hay nada en la página que firme.

El precio es que WebAuthn no firma el mensaje tal cual. El autenticador firma
``authenticatorData || SHA-256(clientDataJSON)``, y el mensaje entra por el
``challenge`` que va adentro de ``clientDataJSON``. Acá el challenge es
``SHA-256(mensaje canónico)``, así que verificar una firma con passkey es:

1. ``clientDataJSON`` dice ``type == "webauthn.get"``, ``challenge ==
   base64url(SHA-256(mensaje))`` y un ``origin`` permitido.
2. ``authenticatorData`` empieza con ``SHA-256(rpId)`` de un rpId permitido y
   trae las banderas **UP** (hubo alguien) y **UV** (se verificó quién: huella o
   PIN).
3. La firma ECDSA P-256 (en DER, como la entrega el autenticador) valida contra
   la pubkey sobre ``authenticatorData || SHA-256(clientDataJSON)``.

No hay challenge emitido por el servidor, a diferencia del WebAuthn de un login:
la frescura y el anti-replay ya los dan los mensajes firmados (timestamp o
``created_at`` + ``sig:used``), igual que con cualquier otra firma del sistema.
Tampoco se controla el contador de firmas: las passkeys sincronizadas lo
reportan siempre en cero, y seguirlo exigiría estado por credencial.

Formato en el cable (campo ``signature`` o cabecera ``X-Signature``, igual que
las demás firmas)::

    wa1.<authenticatorData>.<clientDataJSON>.<firma DER>     (base64url sin padding)

El prefijo es lo que permite que ``common.identity.verify`` distinga una firma
con passkey de una firma cruda P1363 (base64 estándar, que nunca empieza así).
"""

from __future__ import annotations

import base64
import hashlib
import json

PREFIX = "wa1."

_FLAG_UP = 0x01  # user present
_FLAG_UV = 0x04  # user verified


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def encode_envelope(authenticator_data: bytes, client_data_json: bytes,
                    signature_der: bytes) -> str:
    return PREFIX + ".".join(b64url(x) for x in
                             (authenticator_data, client_data_json, signature_der))


def verify_assertion(pubkey_b64: str, message: bytes, envelope: str,
                     rp_ids: list[str], origins: list[str]) -> bool:
    """Verifica una firma con passkey sobre ``message``. Nunca lanza."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        if not envelope.startswith(PREFIX):
            return False
        parts = envelope[len(PREFIX):].split(".")
        if len(parts) != 3:
            return False
        auth_data, client_json, sig_der = (_b64url_decode(p) for p in parts)

        client = json.loads(client_json)
        expected_challenge = b64url(hashlib.sha256(message).digest())
        if (client.get("type") != "webauthn.get"
                or client.get("challenge") != expected_challenge
                or client.get("origin") not in origins
                or client.get("crossOrigin") is True):
            return False

        # authenticatorData: rpIdHash (32) | flags (1) | signCount (4) | ...
        if len(auth_data) < 37:
            return False
        rp_hashes = {hashlib.sha256(rp.encode()).digest() for rp in rp_ids}
        if auth_data[:32] not in rp_hashes:
            return False
        flags = auth_data[32]
        if not (flags & _FLAG_UP) or not (flags & _FLAG_UV):
            return False

        pub = load_der_public_key(base64.b64decode(pubkey_b64))
        if not isinstance(pub, ec.EllipticCurvePublicKey):
            return False
        signed = auth_data + hashlib.sha256(client_json).digest()
        try:
            pub.verify(sig_der, signed, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
    except Exception:
        return False
