"""Identidad propia del minero (AGENT.md 3.1).

Lo que estos tests protegen es que un minero pueda firmar **sin que nadie le
entregue una clave privada**. Antes la identidad del minero era literalmente la
del ciudadano que lo registró, subida al backend en el alta y montada como
Secret del pod; el que tuviera acceso al clúster podía votar como esa persona.
"""

from __future__ import annotations

import os
import stat

import pytest

pytest.importorskip("cryptography", reason="requiere cryptography")

from worker_pkg.identity import WorkerSigner, enroll  # noqa: E402


class TestLaClaveNaceEnElNodo:
    def test_genera_su_par_si_no_existe_el_archivo(self, tmp_path):
        path = tmp_path / "keys" / "node-key.pem"
        signer = WorkerSigner(str(path))

        assert signer.enabled
        assert signer.pubkey
        assert path.exists()
        assert path.read_bytes().startswith(b"-----BEGIN PRIVATE KEY-----")

    def test_el_pem_no_es_legible_por_otros(self, tmp_path):
        """0600 desde su creación, no ajustado después.

        Crear con permisos laxos y corregirlos a continuación deja una ventana
        en la que otro proceso del contenedor puede leer la clave.
        """
        path = tmp_path / "node-key.pem"
        WorkerSigner(str(path))
        modo = stat.S_IMODE(os.stat(path).st_mode)
        assert modo == 0o600

    def test_reusa_la_clave_existente(self, tmp_path):
        path = tmp_path / "node-key.pem"
        primero = WorkerSigner(str(path))
        segundo = WorkerSigner(str(path))
        assert primero.pubkey == segundo.pubkey

    def test_sin_path_configurado_no_firma(self):
        signer = WorkerSigner("")
        assert not signer.enabled
        assert signer.identity("minero-7") == "minero-7"
        assert signer.sign_nonce("w1", 1, "minero-7") is None

    def test_la_firma_verifica_contra_su_propia_pubkey(self, tmp_path):
        from common.identity import nonce_message, verify

        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        winner = signer.identity("minero-7")
        sig = signer.sign_nonce("w1", 42, winner)

        assert winner == signer.pubkey
        assert verify(signer.pubkey, nonce_message("w1", 42, winner), sig)


class TestEnrolamiento:
    def test_sin_token_no_intenta_enrolar(self, tmp_path, monkeypatch):
        """Un minero levantado a mano sin token mina igual, como anónimo."""
        monkeypatch.delenv("WORKER_ENROLL_TOKEN", raising=False)
        monkeypatch.setenv("VOXCHAIN_API_URL", "http://api:8000")
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        assert enroll("minero-7", signer) is False

    def test_un_fallo_de_red_no_tumba_el_minero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WORKER_ENROLL_TOKEN", "t0ken")
        monkeypatch.setenv("VOXCHAIN_API_URL", "http://api-que-no-existe:8000")
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))

        import urllib.request

        def explota(*_a, **_kw):
            raise OSError("sin ruta al host")

        monkeypatch.setattr(urllib.request, "urlopen", explota)
        assert enroll("minero-7", signer) is False

    def test_enrolar_manda_la_pubkey_y_quema_el_token_del_entorno(self, tmp_path, monkeypatch):
        import json
        import urllib.request

        monkeypatch.setenv("WORKER_ENROLL_TOKEN", "t0ken")
        monkeypatch.setenv("VOXCHAIN_API_URL", "http://api:8000/")
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        enviado = {}

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            enviado["url"] = req.full_url
            enviado["body"] = json.loads(req.data)
            return Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

        assert enroll("minero-7", signer) is True
        assert enviado["url"] == "http://api:8000/api/workers/enroll"
        assert enviado["body"] == {"worker_id": "minero-7",
                                  "node_pubkey": signer.pubkey,
                                  "enrollment_token": "t0ken"}
        # El token es de un solo uso y ya se quemó: dejarlo en el entorno sólo
        # sirve para que un volcado o un subproceso lo arrastre.
        assert "WORKER_ENROLL_TOKEN" not in os.environ
