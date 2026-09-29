"""Opciones del cliente de Redis para sobrevivir a un failover detrás de HAProxy."""

from __future__ import annotations

from redis.exceptions import ConnectionError, ReadOnlyError, TimeoutError

from common.redis import create_redis
from common.storage.redis_store import connect_redis


def _opciones(cliente) -> dict:
    return cliente.connection_pool.connection_kwargs


def test_reintenta_conexiones_cortadas_y_masters_degradados():
    opciones = _opciones(connect_redis("redis://:clave@redis:6379/0"))

    # ReadOnlyError: la conexión quedó en una réplica. redis-py la corta antes
    # de reintentar, así que la reconexión vuelve a pasar por HAProxy.
    assert set(opciones["retry_on_error"]) >= {ConnectionError, TimeoutError, ReadOnlyError}
    # Con 8 reintentos y tope de 2 s el cliente espera unos 11 s: más que un
    # failover de Sentinel (5 s de down-after + elección + chequeo de HAProxy).
    assert opciones["retry"]._retries == 8
    assert opciones["health_check_interval"] == 15


def test_los_argumentos_explicitos_ganan():
    opciones = _opciones(connect_redis("redis://redis:6379/0", health_check_interval=0))

    assert opciones["health_check_interval"] == 0


def test_create_redis_usa_las_mismas_opciones():
    opciones = _opciones(create_redis("redis://:clave@redis:6379/0"))

    assert ReadOnlyError in opciones["retry_on_error"]
