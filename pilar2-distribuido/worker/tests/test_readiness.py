"""Readiness del worker: a quién le manda tráfico el Service de un minero.

Cada minero desplegado por el API tiene un Service con nombre estable, que es
la dirección con la que los miembros de su equipo le piden trabajo cuando
coordina. El Service enruta sólo a pods listos, y la única respuesta negativa
tiene que ser la de un ``pool-coordinator`` sin el lease de su pool: si hay dos
pods del mismo minero a la vez (un rollout), los mineros van al que manda.

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


def test_el_coordinador_con_el_lease_esta_listo(manager):
    manager._mode = "pool-coordinator"
    manager._worker = SimpleNamespace(is_leader=True)
    assert manager.readiness() == (True, {"mode": "pool-coordinator",
                                          "pool_leader": True})


def test_el_coordinador_sin_el_lease_no_recibe_trafico(manager):
    manager._mode = "pool-coordinator"
    manager._worker = SimpleNamespace(is_leader=False)
    listo, detalle = manager.readiness()
    assert listo is False
    assert detalle["pool_leader"] is False


def test_coordinador_todavia_sin_arrancar_no_esta_listo(manager):
    # Entre fijar el modo y construir el PoolCoordinator no hay nadie que
    # atienda el HTTP del pool: mandarle mineros sería mandarlos al vacío.
    manager._mode = "pool-coordinator"
    manager._worker = None
    assert manager.readiness()[0] is False
