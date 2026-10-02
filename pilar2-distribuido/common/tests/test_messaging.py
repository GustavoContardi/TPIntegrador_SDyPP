"""Tests del bus en memoria (la implementación RabbitMQ se cubre en e2e con broker).

Al final hay tests del ruteo de publicaciones entre hilos de RabbitMQMessaging,
que sí se pueden verificar sin broker con dobles de conexión y canal.
"""

import threading

from common.messaging import InMemoryBus
from common.messaging.rabbitmq import RabbitMQMessaging


def test_inmemory_dispatch_a_un_handler():
    bus = InMemoryBus()
    recibidos = []
    bus.on_proposal(recibidos.append)
    bus.publish_proposal({"law_id": "L1"})
    assert recibidos == [{"law_id": "L1"}]


def test_inmemory_dispatch_a_multiples_suscriptores_del_topic():
    bus = InMemoryBus()
    a, b = [], []
    bus.on_challenge(a.append)
    bus.on_challenge(b.append)
    bus.publish_challenge({"voting_window_id": "W1"})
    assert a == b == [{"voting_window_id": "W1"}]


def test_flujos_independientes_no_se_cruzan():
    bus = InMemoryBus()
    props, nonces = [], []
    bus.on_proposal(props.append)
    bus.on_nonce_response(nonces.append)
    bus.publish_nonce_response({"nonce": 5})
    assert props == []
    assert nonces == [{"nonce": 5}]


def test_internos_tareas_y_keepalive():
    bus = InMemoryBus()
    tareas, kas = [], []
    bus.on_task(tareas.append)
    bus.on_keepalive(kas.append)
    bus.publish_task({"range_min": 0, "range_max": 100})
    bus.publish_keepalive({"worker_id": "w1", "capacity": 1})
    assert tareas == [{"range_min": 0, "range_max": 100}]
    assert kas == [{"worker_id": "w1", "capacity": 1}]


# -- publicación entre hilos (RabbitMQMessaging) ---------------------------
#
# Regresión: el Pool Coordinator publica el nonce ganador desde el hilo del
# auto-minero y desde el del servidor HTTP, no desde el que consume. Hacer
# basic_publish directo desde ahí revienta la BlockingConnection de pika
# ("tx buffer size underflow") y mata al worker apenas gana una ventana.


class _FakeChannel:
    def __init__(self):
        self.publicados = []

    def basic_publish(self, **kwargs):
        self.publicados.append(kwargs)


class _FakeConnection:
    def __init__(self):
        self.diferidos = []

    def add_callback_threadsafe(self, cb):
        self.diferidos.append(cb)


def _messaging_conectado(consuming: bool):
    m = RabbitMQMessaging("amqp://x/")
    m._ch = _FakeChannel()
    m._conn = _FakeConnection()
    m._consuming = consuming
    m._io_thread = threading.current_thread()
    return m


def test_publicar_desde_el_hilo_dueno_va_directo():
    m = _messaging_conectado(consuming=True)
    m.publish_nonce_response({"nonce": 1})
    assert len(m._ch.publicados) == 1
    assert m._conn.diferidos == []


def test_publicar_desde_otro_hilo_se_difiere_al_hilo_dueno():
    m = _messaging_conectado(consuming=True)

    hilo = threading.Thread(target=m.publish_nonce_response, args=({"nonce": 2},))
    hilo.start()
    hilo.join()

    # No se tocó el canal desde el otro hilo: quedó encolado.
    assert m._ch.publicados == []
    assert len(m._conn.diferidos) == 1

    # Y al correr el callback (lo hace el hilo dueño) sí se publica.
    m._conn.diferidos[0]()
    assert len(m._ch.publicados) == 1


def test_publicador_puro_no_difiere_aunque_cambie_de_hilo():
    # El API sólo publica (nunca llama a start_consuming): no hay hilo dueño
    # con el que competir, así que diferir sólo agregaría una publicación que
    # nadie va a ejecutar.
    m = _messaging_conectado(consuming=False)

    hilo = threading.Thread(target=m.publish_proposal, args=({"law_id": "L1"},))
    hilo.start()
    hilo.join()

    assert len(m._ch.publicados) == 1
    assert m._conn.diferidos == []



# -- aviso de ventana cerrada (flujo 2, routing key aparte) ------------------

def test_inmemory_el_cierre_no_llega_al_handler_del_desafio():
    bus = InMemoryBus()
    desafios, cierres = [], []
    bus.on_challenge(desafios.append)
    bus.on_window_closed(cierres.append)
    bus.publish_window_closed({"voting_window_id": "W1", "result": "success"})
    assert desafios == []
    assert cierres == [{"voting_window_id": "W1", "result": "success"}]


def test_rabbit_el_cierre_sale_por_el_exchange_del_desafio():
    """Mismo exchange que el desafío (es el flujo 2), otra routing key."""
    from common.messaging import CIERRE_ROUTING_KEY, EXCHANGE_DESAFIO

    m = _messaging_conectado(consuming=False)
    m.publish_window_closed({"voting_window_id": "W1", "result": "success"})
    (pub,) = m._ch.publicados
    assert pub["exchange"] == EXCHANGE_DESAFIO
    assert pub["routing_key"] == CIERRE_ROUTING_KEY


def test_rabbit_un_solo_consumidor_reparte_por_routing_key():
    import json
    from types import SimpleNamespace

    from common.messaging import CIERRE_ROUTING_KEY, DESAFIO_ROUTING_KEY

    class _Canal:
        def __init__(self):
            self.acks = []

        def basic_ack(self, delivery_tag):
            self.acks.append(delivery_tag)

    m = RabbitMQMessaging("amqp://x/")
    desafios, cierres = [], []
    m.on_challenge(desafios.append)
    m.on_window_closed(cierres.append)
    callback = m._wrap_desafio()
    canal = _Canal()

    def entregar(routing_key, payload, tag):
        callback(canal, SimpleNamespace(routing_key=routing_key, delivery_tag=tag),
                 None, json.dumps(payload).encode())

    entregar(DESAFIO_ROUTING_KEY, {"voting_window_id": "W1"}, 1)
    entregar(CIERRE_ROUTING_KEY, {"voting_window_id": "W1", "result": "success"}, 2)

    assert desafios == [{"voting_window_id": "W1"}]
    assert cierres == [{"voting_window_id": "W1", "result": "success"}]
    assert canal.acks == [1, 2]
