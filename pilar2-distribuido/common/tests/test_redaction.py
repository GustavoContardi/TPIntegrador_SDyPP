"""Redacción de credenciales en URLs de conexión.

Regresión del despliegue del 2026-08-04: tanto el cliente de RabbitMQ como el de
Redis logueaban su URL de conexión completa al conectar, y esas URLs llevan la
contraseña embebida. Las credenciales de ambos servicios quedaron en texto plano
en Cloud Logging.
"""

from __future__ import annotations

import logging

from common.redaction import redact_url

def test_oculta_la_password_de_amqp():
    limpia = redact_url("amqp://voxchain:SuperSecreta123@rabbitmq:5672/")
    assert "SuperSecreta123" not in limpia
    assert limpia == "amqp://voxchain:***@rabbitmq:5672/"

def test_oculta_la_password_de_redis_sin_usuario():
    # Redis usa la forma redis://:contraseña@host, sin nombre de usuario.
    limpia = redact_url("redis://:PzVD3RYejnriq70B@34.151.249.246:6379/0")
    assert "PzVD3RYejnriq70B" not in limpia
    assert limpia == "redis://:***@34.151.249.246:6379/0"

def test_conserva_url_sin_password():
    assert redact_url("amqp://rabbitmq:5672/") == "amqp://rabbitmq:5672/"
    assert redact_url("redis://redis:6379/0") == "redis://redis:6379/0"

def test_conserva_esquema_host_puerto_y_path():
    assert redact_url("amqps://u:p@broker.example/") == "amqps://u:***@broker.example/"
    assert redact_url("redis://:p@h:6379/2") == "redis://:***@h:6379/2"

def test_create_redis_no_loguea_la_password(caplog, monkeypatch):
    import common.redis as mod

    # No queremos un Redis real: sólo nos importa qué se loguea al conectar.
    monkeypatch.setattr(mod, "create_redis", mod.create_redis)
    fake = type("FakeRedis", (), {"from_url": staticmethod(lambda *a, **k: object())})
    monkeypatch.setitem(__import__("sys").modules, "redis", type("M", (), {"Redis": fake}))

    with caplog.at_level(logging.INFO, logger="voxchain.common.redis"):
        mod.create_redis("redis://:MiClaveSecreta@redis:6379/0")

    texto = " ".join(r.getMessage() for r in caplog.records)
    assert "MiClaveSecreta" not in texto
    assert "***" in texto
