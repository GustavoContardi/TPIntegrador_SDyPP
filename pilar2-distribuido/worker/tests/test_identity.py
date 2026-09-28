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


    def test_con_dos_tokens_prueba_el_segundo_si_el_primero_ya_se_uso(
            self, tmp_path, monkeypatch):
        """Las réplicas de un coordinador montan el mismo Secret.

        El primer token lo gasta una; la otra recibe 401 y tiene que probar el
        de la réplica en vez de rendirse y minar como anónima.
        """
        import io
        import json
        import urllib.error
        import urllib.request

        monkeypatch.setenv("WORKER_ENROLL_TOKEN", "usado")
        monkeypatch.setenv("WORKER_ENROLL_TOKEN_REPLICA", "de-la-replica")
        monkeypatch.setenv("VOXCHAIN_API_URL", "http://api:8000")
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        probados = []

        class Resp:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def fake_urlopen(req, timeout=None):
            token = json.loads(req.data)["enrollment_token"]
            probados.append(token)
            if token == "usado":
                raise urllib.error.HTTPError(req.full_url, 401, "ya usado", {},
                                             io.BytesIO(b"ya usado"))
            return Resp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert enroll("minero-7", signer) is True
        assert probados == ["usado", "de-la-replica"]
        assert "WORKER_ENROLL_TOKEN_REPLICA" not in os.environ

    def test_un_error_que_no_es_401_no_prueba_el_otro_token(self, tmp_path, monkeypatch):
        import io
        import urllib.error
        import urllib.request

        monkeypatch.setenv("WORKER_ENROLL_TOKEN", "t1")
        monkeypatch.setenv("WORKER_ENROLL_TOKEN_REPLICA", "t2")
        monkeypatch.setenv("VOXCHAIN_API_URL", "http://api:8000")
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        intentos = []

        def fake_urlopen(req, timeout=None):
            intentos.append(req)
            raise urllib.error.HTTPError(req.full_url, 404, "no existe", {},
                                         io.BytesIO(b"no existe"))

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        assert enroll("minero-7", signer) is False
        # 404 = el minero no está registrado: el otro token no lo cambia.
        assert len(intentos) == 1


class TestTokensDelVolumen:
    """En los pods que despliega el API los tokens llegan como archivos.

    El Secret montado como volumen se actualiza con el pod vivo; una variable de
    entorno, no. Es lo que deja enrolarse a un pod que reemplaza a otro.
    """

    def test_lee_los_dos_archivos_y_los_prefiere_al_entorno(self, tmp_path, monkeypatch):
        from worker_pkg.identity import enrollment_tokens

        (tmp_path / "enrollment-token").write_text("del-volumen\n")
        (tmp_path / "enrollment-token-replica").write_text("de-la-replica")
        monkeypatch.setenv("WORKER_ENROLL_TOKEN_DIR", str(tmp_path))
        monkeypatch.setenv("WORKER_ENROLL_TOKEN", "del-entorno")
        monkeypatch.delenv("WORKER_ENROLL_TOKEN_REPLICA", raising=False)
        assert enrollment_tokens() == ["del-volumen", "de-la-replica", "del-entorno"]

    def test_un_archivo_que_falta_no_es_un_error(self, tmp_path, monkeypatch):
        from worker_pkg.identity import enrollment_tokens

        (tmp_path / "enrollment-token").write_text("unico")
        monkeypatch.setenv("WORKER_ENROLL_TOKEN_DIR", str(tmp_path))
        monkeypatch.delenv("WORKER_ENROLL_TOKEN", raising=False)
        monkeypatch.delenv("WORKER_ENROLL_TOKEN_REPLICA", raising=False)
        assert enrollment_tokens() == ["unico"]

    def test_relee_el_volumen_en_cada_llamada(self, tmp_path, monkeypatch):
        # El API repone el token con el pod corriendo: leerlo una sola vez al
        # arrancar sería volver al problema de la variable de entorno.
        from worker_pkg.identity import enrollment_tokens

        archivo = tmp_path / "enrollment-token"
        archivo.write_text("gastado")
        monkeypatch.setenv("WORKER_ENROLL_TOKEN_DIR", str(tmp_path))
        monkeypatch.delenv("WORKER_ENROLL_TOKEN", raising=False)
        monkeypatch.delenv("WORKER_ENROLL_TOKEN_REPLICA", raising=False)
        assert enrollment_tokens() == ["gastado"]
        archivo.write_text("repuesto")
        assert enrollment_tokens() == ["repuesto"]

    def test_reintenta_hasta_quedar_enrolado(self, tmp_path, monkeypatch):
        import threading

        from worker_pkg import identity

        intentos = []
        # Falla dos veces (tokens gastados, el nuevo todavía no llegó) y a la
        # tercera pasa.
        monkeypatch.setattr(identity, "enroll",
                            lambda *_a: intentos.append(1) or len(intentos) >= 3)
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        assert identity.enroll_until_bound("minero-7", signer, first_wait=0.01,
                                           max_wait=0.02) is True
        assert len(intentos) == 3

    def test_se_puede_cortar_el_ciclo(self, tmp_path, monkeypatch):
        import threading

        from worker_pkg import identity

        monkeypatch.setattr(identity, "enroll", lambda *_a: False)
        signer = WorkerSigner(str(tmp_path / "node-key.pem"))
        stop = threading.Event()
        stop.set()
        assert identity.enroll_until_bound("minero-7", signer, stop=stop) is False
