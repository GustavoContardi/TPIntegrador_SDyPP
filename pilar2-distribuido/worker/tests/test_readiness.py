"""Readiness del worker: a quién le manda tráfico el Service de un minero.

Cada minero desplegado por el API tiene un Service con nombre estable, que es
la dirección con la que los miembros de su equipo le piden trabajo cuando
coordina. El Service enruta sólo a pods listos, y la única respuesta negativa
tiene que ser la de un ``pool-coordinator`` en espera: otra réplica del mismo
minero tiene el lease de su pool, y los mineros van a ésa.

Cualquier otro modo responde listo: no hay tráfico que dirigir, y marcarlo
NotReady sólo ensuciaría el estado del Deployment.
"""

from __future__ import annotations

import importlib.util
import pathlib
from types import SimpleNamespace

import pytest

# Igual que en test_switch_mode_remoto: `main` a secas resuelve al del NCT.
_RUTA_MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
_spec = importlib.util.spec_from_file_location("worker_main_readiness", _RUTA_MAIN)
worker_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker_main)
WorkerManager = worker_main.WorkerManager


@pytest.fixture
def manager():
    return WorkerManager("gustavo10", has_gpu=False)


@pytest.mark.parametrize("modo", ["idle", "standalone", "pool-worker", "pool-auto"])
def test_los_modos_que_no_coordinan_un_equipo_estan_listos(manager, modo):
    manager._mode = modo
    listo, detalle = manager.readiness()
    assert listo is True
    assert detalle["mode"] == modo


def test_el_coordinador_que_manda_esta_listo(manager):
    manager._mode = "pool-coordinator"
    manager._worker = SimpleNamespace(is_leader=True, standby=False)
    listo, detalle = manager.readiness()
    assert listo is True
    assert detalle["standby"] is False


def test_la_replica_en_espera_no_recibe_trafico(manager):
    # Otro pod del mismo minero tiene el lease: los mineros van a ése.
    manager._mode = "pool-coordinator"
    manager._worker = SimpleNamespace(is_leader=False, standby=True)
    listo, detalle = manager.readiness()
    assert listo is False
    assert detalle["standby"] is True


def test_sin_lease_pero_sin_nadie_mas_sigue_listo(manager):
    # Todavía no ganó la elección, o no puede leer Redis: no sabe de otra
    # réplica. Sacarlo del Service dejaría al equipo sin coordinador por una
    # falla de observación.
    manager._mode = "pool-coordinator"
    manager._worker = SimpleNamespace(is_leader=False, standby=False)
    assert manager.readiness()[0] is True


def test_coordinador_todavia_sin_arrancar_no_esta_listo(manager):
    # Entre fijar el modo y construir el PoolCoordinator no hay nadie que
    # atienda el HTTP del pool: mandarle mineros sería mandarlos al vacío.
    manager._mode = "pool-coordinator"
    manager._worker = None
    assert manager.readiness()[0] is False


def test_el_id_de_instancia_distingue_replicas_del_mismo_minero(manager, monkeypatch):
    # Mismo WORKER_ID (el env del template), hostname distinto (el pod).
    monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "worker-dep-x-abc12")
    a = manager.instance_id
    monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "worker-dep-x-def34")
    b = manager.instance_id
    assert a != b
    assert a.startswith("gustavo10@") and b.startswith("gustavo10@")
