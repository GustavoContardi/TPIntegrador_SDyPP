"""Health endpoint: qué cuenta como sano y qué no.

Regresión del despliegue del 2026-08-04: el NCT standby respondía 503 y su
readinessProbe fallaba para siempre, porque el chequeo exigía que *todos* los
valores del status fueran ``"ok"`` y el standby reporta su rol
(``"nct": "standby"``) en el mismo diccionario. Un rol no es un diagnóstico.

Regresión del 2026-08-07: cada probe del kubelet dejaba un ``BrokenPipeError``
con traceback completo en los logs de los pods del NCT.
"""

from __future__ import annotations

import json
import socket
import struct
import time
import urllib.error
import urllib.request

import pytest

from common.health import start_health_server

def _pedir(server) -> tuple[int, dict]:
    """GET /health contra el server, devolviendo (status, cuerpo)."""
    puerto = server.server_address[1]
    try:
        resp = urllib.request.urlopen(f"http://127.0.0.1:{puerto}/health", timeout=5)
        return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())

@pytest.fixture
def servidor():
    servers = []

    def crear(status, **kwargs):
        # puerto 0 = el SO asigna uno libre, así los tests no chocan entre sí
        s = start_health_server(0, lambda: status, **kwargs)
        servers.append(s)
        return s

    yield crear
    for s in servers:
        s.shutdown()

def test_todo_ok_devuelve_200(servidor):
    s = servidor({"redis": "ok", "rabbitmq": "ok"})
    assert _pedir(s)[0] == 200

def test_una_dependencia_caida_devuelve_503(servidor):
    s = servidor({"redis": "down", "rabbitmq": "ok"})
    codigo, cuerpo = _pedir(s)
    assert codigo == 503
    assert cuerpo["redis"] == "down"

def test_standby_es_sano_si_se_declara_como_valor_ok(servidor):
    # El caso del NCT follower: rol standby, dependencias sanas.
    s = servidor({"nct": "standby", "redis": "ok", "rabbitmq": "ok"},
                 ok_values=("ok", "standby"))
    codigo, cuerpo = _pedir(s)
    assert codigo == 200
    assert cuerpo["nct"] == "standby"

def test_standby_con_dependencia_caida_sigue_siendo_503(servidor):
    # Aceptar "standby" no puede tapar una falla real.
    s = servidor({"nct": "standby", "redis": "down", "rabbitmq": "ok"},
                 ok_values=("ok", "standby"))
    assert _pedir(s)[0] == 503

def test_sin_ok_values_el_standby_sigue_dando_503(servidor):
    # El default no cambia: sólo "ok" es sano si nadie declara lo contrario.
    s = servidor({"nct": "standby", "redis": "ok"})
    assert _pedir(s)[0] == 503

def test_cliente_que_corta_no_deja_traceback(servidor, capsys):
    """El kubelet lee el status y cierra sin leer el cuerpo.

    Se reproduce con SO_LINGER 0, que hace que ``close()`` mande un RST en vez
    de un cierre ordenado: el server se come el error al escribir el body.
    Antes del fix esto imprimía un traceback de BrokenPipeError por cada probe.
    """
    s = servidor({"redis": "ok"})
    puerto = s.server_address[1]

    sock = socket.create_connection(("127.0.0.1", puerto), timeout=5)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                    struct.pack("ii", 1, 0))
    sock.sendall(b"GET /health HTTP/1.0\r\n\r\n")
    sock.close()

    # el handler corre en otro hilo; darle tiempo a fallar y reportar
    time.sleep(0.5)
    assert "Traceback" not in capsys.readouterr().err

    # y el server sigue atendiendo: no se cayó el hilo ni el socket de escucha
    assert _pedir(s)[0] == 200

def test_los_errores_que_no_son_de_conexion_se_siguen_reportando(servidor, capsys):
    """El silencio es sólo para ConnectionError, no para bugs de verdad."""
    s = servidor({"redis": "ok"})
    capsys.readouterr()  # descartar lo que haya quedado del arranque

    try:
        raise ValueError("bug real")
    except ValueError:
        s.handle_error(None, ("127.0.0.1", 0))
    assert "ValueError" in capsys.readouterr().err

    try:
        raise BrokenPipeError()
    except BrokenPipeError:
        s.handle_error(None, ("127.0.0.1", 0))
    assert capsys.readouterr().err == ""
