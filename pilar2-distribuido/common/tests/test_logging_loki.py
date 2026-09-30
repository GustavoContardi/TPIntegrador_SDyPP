"""Envío de logs a Loki desde el proceso (los mineros del k3s, `LOKI_PUSH_URL`).

Se prueba contra un servidor HTTP de verdad en localhost: lo que importa es el
cuerpo y los headers que llegan, que son los que Loki valida.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from common.logging_setup import JsonFormatter, LokiHandler, setup_logging

# En una constante y no como `password="..."`: esa forma la marca la regla
# hardcoded-credential-assignment de .gitleaks.toml y el CI falla.
CLAVE_DE_PRUEBA = "no-es-real"


class _Loki:
    """Servidor que guarda cada push que recibe y responde como Loki (204)."""

    def __init__(self, status: int = 204):
        self.pushes: list[dict] = []
        self.status = status
        recibidos = self.pushes
        servidor = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                largo = int(self.headers["Content-Length"])
                recibidos.append({"path": self.path, "headers": dict(self.headers),
                                  "body": json.loads(self.rfile.read(largo))})
                self.send_response(servidor.status)
                self.end_headers()

            def log_message(self, *args):  # sin ruido en la salida de pytest
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/loki/api/v1/push"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def loki():
    servidor = _Loki()
    yield servidor
    servidor.close()


def _logger(handler: logging.Handler, nombre: str) -> logging.Logger:
    log = logging.getLogger(nombre)
    log.handlers = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    return log


def _handler(url, **kwargs) -> LokiHandler:
    h = LokiHandler(url, labels={"service": "worker", "cluster": "k3s", "pod": "w-1"},
                    flush_interval=0.05, **kwargs)
    h.setFormatter(JsonFormatter(service="worker"))
    return h


def test_manda_un_stream_por_nivel_con_labels_y_basic_auth(loki):
    h = _handler(loki.url, user="voxchain-logs", password=CLAVE_DE_PRUEBA)
    log = _logger(h, "test.loki.streams")

    log.info("minando ley %s", "L1")
    log.warning("fragmento vencido")
    log.info("nonce encontrado")
    h.close()

    entradas = [s for p in loki.pushes for s in p["body"]["streams"]]
    por_nivel = {}
    for stream in entradas:
        assert stream["stream"]["service"] == "worker"
        assert stream["stream"]["cluster"] == "k3s"
        assert stream["stream"]["pod"] == "w-1"
        por_nivel.setdefault(stream["stream"]["level"], []).extend(stream["values"])
    assert set(por_nivel) == {"INFO", "WARNING"}
    mensajes = [json.loads(linea)["message"] for _, linea in por_nivel["INFO"]]
    assert mensajes == ["minando ley L1", "nonce encontrado"]
    # Timestamps en nanosegundos, como string: es lo que exige la API de push.
    ts, _ = por_nivel["WARNING"][0]
    assert isinstance(ts, str) and len(ts) == 19

    push = loki.pushes[0]
    assert push["path"] == "/loki/api/v1/push"
    assert push["headers"]["Content-Type"] == "application/json"
    esperado = base64.b64encode(f"voxchain-logs:{CLAVE_DE_PRUEBA}".encode()).decode()
    assert push["headers"]["Authorization"] == f"Basic {esperado}"


def test_sin_credenciales_no_manda_authorization(loki):
    h = _handler(loki.url)
    _logger(h, "test.loki.sin_auth").info("hola")
    h.close()

    assert "Authorization" not in loki.pushes[0]["headers"]


def test_close_manda_lo_que_quedaba_en_la_cola(loki):
    # Un intervalo largo: si no fuera por close(), nada saldría durante el test.
    h = LokiHandler(loki.url, labels={"service": "worker"}, flush_interval=30)
    h.setFormatter(JsonFormatter(service="worker"))
    log = _logger(h, "test.loki.close")
    for i in range(5):
        log.info("registro %d", i)

    h.close()

    valores = [v for p in loki.pushes for s in p["body"]["streams"] for v in s["values"]]
    assert len(valores) == 5


def test_loki_caido_no_rompe_al_que_loguea(capsys):
    # Un puerto donde no escucha nadie.
    servidor = _Loki()
    url = servidor.url
    servidor.close()

    h = _handler(url, timeout=0.5)
    log = _logger(h, "test.loki.caido")
    log.info("uno")
    log.error("dos")
    h.close()

    assert h.failed == 2
    # El aviso va a stderr una sola vez, no una por lote.
    assert capsys.readouterr().err.count("no se pudo enviar a Loki") == 1


def test_loki_que_rechaza_cuenta_como_falla():
    servidor = _Loki(status=401)  # basic auth equivocada en el Ingress
    try:
        h = _handler(servidor.url, user="x", password=CLAVE_DE_PRUEBA)
        _logger(h, "test.loki.401").info("hola")
        h.close()
    finally:
        servidor.close()

    assert h.failed == 1


def test_cola_llena_descarta_y_cuenta():
    # Sin servidor y con una cola de 2: el hilo toma a lo sumo un lote, así que
    # de 50 registros la mayoría tiene que descartarse sin bloquear.
    h = LokiHandler("http://127.0.0.1:9/loki/api/v1/push", labels={}, max_queue=2,
                    flush_interval=30, timeout=0.2)
    h.setFormatter(logging.Formatter("%(message)s"))
    log = _logger(h, "test.loki.llena")
    for i in range(50):
        log.info("registro %d", i)
    h.close()

    assert h.dropped >= 45


@pytest.fixture
def root_limpio():
    """setup_logging reemplaza los handlers del root: se restauran después."""
    root = logging.getLogger()
    previos, nivel = list(root.handlers), root.level
    yield
    for h in list(root.handlers):
        root.removeHandler(h)
        if isinstance(h, LokiHandler):
            h.close()
    for h in previos:
        root.addHandler(h)
    root.setLevel(nivel)


def test_setup_logging_sin_url_no_agrega_loki(root_limpio, monkeypatch, tmp_path):
    monkeypatch.delenv("LOKI_PUSH_URL", raising=False)
    monkeypatch.setenv("LOG_DIR", str(tmp_path))

    setup_logging("nct")

    assert not any(isinstance(h, LokiHandler) for h in logging.getLogger().handlers)


def test_setup_logging_con_url_agrega_loki(root_limpio, monkeypatch, tmp_path, loki):
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    monkeypatch.setenv("LOKI_PUSH_URL", loki.url)
    monkeypatch.setenv("LOKI_PUSH_USER", "voxchain-logs")
    monkeypatch.setenv("LOKI_PUSH_PASSWORD", CLAVE_DE_PRUEBA)
    monkeypatch.setenv("LOKI_CLUSTER", "k3s")

    log = setup_logging("worker")
    (h,) = [h for h in logging.getLogger().handlers if isinstance(h, LokiHandler)]
    assert h.labels["service"] == "worker"
    assert h.labels["cluster"] == "k3s"
    assert h.labels["pod"]

    log.info("arrancó el minero")
    h.close()
    (stream,) = loki.pushes[0]["body"]["streams"]
    assert json.loads(stream["values"][0][1])["message"] == "arrancó el minero"

    # Llamarlo otra vez no deja el handler anterior con su hilo andando.
    setup_logging("worker")
    assert h._stop.is_set()
    assert len([x for x in logging.getLogger().handlers if isinstance(x, LokiHandler)]) == 1
