"""Health endpoint: qué cuenta como sano y qué no.

Regresión del despliegue del 2026-08-04: el NCT standby respondía 503 y su
readinessProbe fallaba para siempre, porque el chequeo exigía que *todos* los
valores del status fueran ``"ok"`` y el standby reporta su rol
(``"nct": "standby"``) en el mismo diccionario. Un rol no es un diagnóstico.
"""

from __future__ import annotations

import json
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
