"""/api/events manda un comentario SSE cuando no hay eventos.

ingress-nginx corta el stream si el upstream pasa 60 s sin escribir; sin el
latido, la conexión moría cada minuto con ERR_HTTP2_PROTOCOL_ERROR.
"""

from __future__ import annotations

import asyncio

from voxchain_api.main import SSE_HEARTBEAT_SECONDS, sse_messages


def test_latido_muy_por_debajo_del_timeout_del_proxy():
    assert SSE_HEARTBEAT_SECONDS < 60 / 2


def test_sin_eventos_manda_un_comentario():
    async def primero():
        stream = sse_messages(asyncio.Queue(), heartbeat=0.01)
        return await stream.__anext__()

    mensaje = asyncio.run(primero())
    # Comentario SSE: empieza con ':' y cierra con línea en blanco, así
    # EventSource lo descarta sin disparar ningún evento en el cliente.
    assert mensaje.startswith(":")
    assert mensaje.endswith("\n\n")


def test_los_eventos_pasan_tal_cual_y_el_latido_sigue():
    async def tres():
        cola: asyncio.Queue = asyncio.Queue()
        evento = 'event: block\ndata: {"height": 1}\n\n'
        await cola.put(evento)
        stream = sse_messages(cola, heartbeat=0.01)
        return evento, [await stream.__anext__() for _ in range(2)]

    evento, recibidos = asyncio.run(tres())
    assert recibidos[0] == evento
    assert recibidos[1].startswith(":")
