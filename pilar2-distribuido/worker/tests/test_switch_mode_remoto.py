"""Cambio de modo por comando remoto (exchange ``worker_command``).

El callback de RabbitMQ corre en el mismo hilo que ``start_consuming``, que es
justo el hilo que ``switch_mode`` tiene que parar y joinear. Aplicar el cambio
inline reventaba con ``RuntimeError: cannot join current thread``, el wrapper
del consumidor se lo tragaba y el worker seguía en su modo viejo mientras el
backend devolvía 200.

El camino del admin HTTP nunca lo sufrió porque ya venía de otro hilo, así que
sólo fallaba con los workers del k3s, los únicos para los que el backend no
tiene fallback HTTP.
"""

from __future__ import annotations

import importlib.util
import pathlib
import threading
import time

import pytest

from common.messaging.base import Messaging

# `main` a secas resuelve al del nct-coordinator, que va antes en el pythonpath
# del pytest.ini. Se carga por ruta para quedarse con el del worker.
_RUTA_MAIN = pathlib.Path(__file__).resolve().parents[1] / "main.py"
_spec = importlib.util.spec_from_file_location("worker_main_bajo_test", _RUTA_MAIN)
worker_main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(worker_main)
WorkerManager = worker_main.WorkerManager


class BusDeUnHilo(Messaging):
    """Bus que entrega los comandos desde el hilo de ``start_consuming``.

    Es lo que hace pika: por eso un ``InMemoryBus`` que despacha de forma
    síncrona desde el publicador no reproduce el fallo.
    """

    def __init__(self):
        self._handler = None
        self._pendientes = []
        self._consumiendo = False
        self.hilo_io = None
        self.errores = []

    def connect(self):
        pass

    def close(self):
        self._consumiendo = False

    def on_worker_command(self, worker_id, handler):
        self._handler = handler

    def on_challenge(self, handler):
        pass

    def publish_worker_command(self, worker_id, command):
        self._pendientes.append(command)

    def publish_nonce_response(self, solution):
        pass

    def publish_keepalive(self, keepalive):
        pass

    def consumed_queues(self):
        return set()

    def unsubscribe(self, stream):
        pass

    def start_consuming(self, tick=None, tick_interval=1.0):
        self.hilo_io = threading.current_thread()
        self._consumiendo = True
        while self._consumiendo:
            while self._pendientes:
                try:
                    self._handler(self._pendientes.pop(0))
                except Exception as exc:  # lo mismo que hace _wrap: loguear y seguir
                    self.errores.append(exc)
            if tick:
                tick()
            time.sleep(0.01)


@pytest.fixture
def manager(monkeypatch):
    buses = []

    def fabrica(_url):
        bus = BusDeUnHilo()
        buses.append(bus)
        return bus

    monkeypatch.setattr(worker_main, "build_rabbitmq", fabrica)
    # El reporte de estado a Redis no hace falta acá y necesitaría un servidor.
    monkeypatch.setattr(WorkerManager, "_redis_report_loop", lambda self: None)

    m = WorkerManager("gustavo10", has_gpu=False)
    m.start("standalone")
    _esperar(lambda: buses and buses[0].hilo_io is not None)
    yield m, buses
    m.stop()


def _esperar(cond, timeout=5.0):
    limite = time.time() + timeout
    while time.time() < limite:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_el_comando_llega_en_el_hilo_de_consumo(manager):
    """Si esto deja de ser cierto, el test de abajo ya no prueba nada."""
    m, buses = manager
    assert buses[0].hilo_io is m._thread


def test_switch_mode_remoto_cambia_el_modo(manager):
    m, buses = manager
    assert m.get_status()["mode"] == "standalone"

    buses[0].publish_worker_command("gustavo10", {
        "type": "switch_mode",
        "mode": "pool-worker",
        "pool_url": "http://worker-pool-coordinator:9001",
    })

    assert _esperar(lambda: m.get_status()["mode"] == "pool-worker"), (
        f"sigue en {m.get_status()['mode']}; errores del consumidor: {buses[0].errores}"
    )
    assert m.get_status()["pool_url"] == "http://worker-pool-coordinator:9001"


def test_el_consumidor_no_ve_excepciones(manager):
    """El fallo original quedaba enterrado en un log, no en la respuesta."""
    m, buses = manager
    buses[0].publish_worker_command("gustavo10", {
        "type": "switch_mode", "mode": "pool-auto",
    })
    _esperar(lambda: m.get_status()["mode"] == "pool-auto")
    assert buses[0].errores == []


def test_switch_mode_directo_sigue_andando(manager):
    """El camino del admin HTTP (otro hilo) no debe haberse roto."""
    m, _ = manager
    m.switch_mode("standalone")
    assert m.get_status()["mode"] == "standalone"
