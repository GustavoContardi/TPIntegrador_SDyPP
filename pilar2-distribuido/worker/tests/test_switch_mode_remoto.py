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
        self._pool_election_handler = None

    def connect(self):
        pass

    def close(self):
        self._consumiendo = False

    def on_worker_command(self, worker_id, handler):
        self._handler = handler

    def on_challenge(self, handler):
        pass

    def on_pool_election(self, pool_id, handler):
        self._pool_election_handler = handler

    def publish_pool_election(self, pool_id, msg):
        pass

    def unsubscribe_pool_election(self):
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


def test_pool_worker_arranca_dos_hilos(manager):
    """El bucle de minado y el de consumo tienen que ser hilos distintos.

    El bucle del pool-worker bloquea mientras mina un fragmento; si comparte
    hilo con el consumo de RabbitMQ, el worker deja de escuchar mientras trabaja.
    """
    m, _ = manager
    m.switch_mode("pool-worker", "http://coordinador-inexistente:9001")

    assert m._thread is not None and m._thread.is_alive()
    assert m._worker_thread is not None and m._worker_thread.is_alive()
    assert m._thread is not m._worker_thread


def test_pool_worker_sigue_escuchando_comandos(manager):
    """Regresión: un minero dentro de un equipo tiene que poder salir de él.

    En modo pool-worker el hilo del worker corría el bucle de minado y nadie
    corría ``start_consuming``, así que el consumidor de ``worker.command``
    quedaba registrado pero nunca activo. La consecuencia práctica: el backend
    publicaba la orden de volver a competitivo, nadie la consumía, y el minero
    seguía pidiéndole fragmentos a su coordinador para siempre — sin más salida
    que reiniciar el contenedor.
    """
    m, buses = manager
    m.switch_mode("pool-worker", "http://coordinador-inexistente:9001")

    # Cambiar de modo recrea la mensajería, así que el bus vigente es el último.
    bus = buses[-1]
    assert _esperar(lambda: bus.hilo_io is not None), (
        "nadie arrancó el loop de consumo en modo pool-worker"
    )

    bus.publish_worker_command("gustavo10", {"type": "switch_mode",
                                             "mode": "standalone"})
    assert _esperar(lambda: m.get_status()["mode"] == "standalone"), (
        f"sigue en {m.get_status()['mode']}; errores: {bus.errores}"
    )


def test_salir_de_pool_auto_detiene_el_bully(manager):
    """Al cambiar de modo el bully tiene que soltar lo que arrancó por dentro.

    El bully levanta un coordinator o un pool-worker según gane o pierda la
    elección; si no se lo detiene, esos hilos sobreviven al cambio de modo y el
    worker termina minando en dos modos a la vez.
    """
    m, _ = manager
    m.switch_mode("pool-auto")
    bully = m._bully
    assert bully is not None

    m.switch_mode("standalone")
    assert bully._running is False
    assert m._bully is None
    assert m.get_status()["bully_state"] is None


class TestDireccionAnunciada:
    """La dirección que el worker publica es lo que arma un equipo.

    El backend se la entrega a quien se une al equipo, así que si sale mal el
    minero apunta a un host que no existe y el pool nunca reparte nada.
    """

    def test_worker_address_explicita_gana(self, monkeypatch):
        monkeypatch.setenv("WORKER_ADDRESS", "http://mi-servicio:9001/")
        m = WorkerManager("w", has_gpu=False)
        assert m.address == "http://mi-servicio:9001"

    def test_sin_worker_address_usa_la_ip_del_pod(self, monkeypatch):
        monkeypatch.delenv("WORKER_ADDRESS", raising=False)
        monkeypatch.setenv("MY_POD_IP", "10.42.0.7")
        m = WorkerManager("w", has_gpu=False)
        assert m.address == "http://10.42.0.7:9001"

    def test_ultimo_recurso_el_hostname(self, monkeypatch):
        monkeypatch.delenv("WORKER_ADDRESS", raising=False)
        monkeypatch.delenv("MY_POD_IP", raising=False)
        monkeypatch.setattr(worker_main.socket, "gethostname", lambda: "contenedor-x")
        m = WorkerManager("w", has_gpu=False)
        assert m.address == "http://contenedor-x:9001"

    def test_el_coordinator_publica_su_direccion_como_pool_url(self, monkeypatch):
        """Antes se armaba con el worker_id, que casi nunca resuelve."""
        monkeypatch.setenv("WORKER_ADDRESS", "http://10.42.0.7:9001")
        m = WorkerManager("pool-coordinator-1", has_gpu=False)
        m._mode = "pool-coordinator"
        estado = m.get_status()
        assert estado["pool_url"] == "http://10.42.0.7:9001"
        assert estado["address"] == "http://10.42.0.7:9001"
