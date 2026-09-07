#!/usr/bin/env python3
"""Limpia mineros fantasma y equipos huérfanos del estado en Redis.

Registrar un minero desde la UI lo **anota**; encenderlo es otra cosa (ver
`docs/workers.md`). Probar la pantalla de Minería deja entonces un rastro de
registros sin proceso detrás, y equipos cuyo coordinador no existe. Nada de eso
rompe el sistema —la UI los muestra honestamente como "Sin arrancar"— pero
ensucia una demo.

Un **minero fantasma** es uno registrado que no reporta estado: la clave
`worker:status:<id>` vive 15 s, así que si no está, no hay proceso detrás.

Un **equipo huérfano** es uno cuyo coordinador es un fantasma. Sin coordinador
vivo no reparte trabajo, y sus miembros quedan pidiéndole fragmentos a un HTTP
que no atiende.

Por defecto sólo muestra qué borraría. Para borrar de verdad: `--apply`.

    python scripts/limpiar_registros.py                # ver
    python scripts/limpiar_registros.py --apply        # borrar
    python scripts/limpiar_registros.py --all --apply  # borrar TODO lo dinámico

Los mineros del docker-compose (`worker-1`, `worker-2`, `pool-coordinator-1`) no
se tocan nunca: no están en `registered_workers`, existen porque el compose los
levanta.
"""

from __future__ import annotations

import argparse
import os
import sys

# Permite correrlo como `python scripts/limpiar_registros.py` desde pilar2-distribuido/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.redaction import redact_url  # noqa: E402
from common.redis import create_redis  # noqa: E402


def _claves_de_minero(worker_id: str) -> list[str]:
    return [
        f"worker:owner:{worker_id}",
        f"worker:pubkey:{worker_id}",
        f"worker:status:{worker_id}",
        f"worker:desired_mode:{worker_id}",
        f"worker:team:{worker_id}",
        f"pool:policy:{worker_id}",
        f"pool:health:{worker_id}",
    ]


def _claves_de_equipo(r, team_id: str) -> list[str]:
    claves = [f"team:{team_id}", f"team:members:{team_id}"]
    dueno = r.hget(f"team:{team_id}", "owner")
    if dueno:
        # Sin borrar este índice, quien fundó el equipo no puede fundar otro:
        # el backend lo tomaría como que ya tiene uno.
        claves.append(f"team:owner:{dueno}")
    return claves


def relevar(r, todo: bool = False) -> tuple[list[str], list[str]]:
    """Devuelve ``(mineros_a_borrar, equipos_a_borrar)``."""
    registrados = set(r.smembers("registered_workers"))
    vivos = {k.split("worker:status:", 1)[1] for k in r.scan_iter("worker:status:*")}

    mineros = sorted(registrados if todo else registrados - vivos)

    equipos = []
    for team_id in sorted(r.smembers("teams")):
        datos = r.hgetall(f"team:{team_id}")
        if not datos:
            equipos.append(team_id)  # id suelto en el set, sin hash detrás
            continue
        coordinador = datos.get("coordinator_worker_id", "")
        if todo or coordinador not in vivos:
            equipos.append(team_id)
    return mineros, equipos


def borrar(r, mineros: list[str], equipos: list[str]) -> int:
    borradas = 0
    for team_id in equipos:
        # Primero los miembros: si el equipo desaparece antes, no hay de dónde
        # sacar la lista y quedan `worker:team:*` apuntando a la nada.
        for miembro in r.smembers(f"team:members:{team_id}"):
            borradas += r.delete(f"worker:team:{miembro}")
        for clave in _claves_de_equipo(r, team_id):
            borradas += r.delete(clave)
        borradas += r.srem("teams", team_id)

    for worker_id in mineros:
        for clave in _claves_de_minero(worker_id):
            borradas += r.delete(clave)
        borradas += r.srem("registered_workers", worker_id)
    return borradas


def _conectar(url: str):
    """Cliente de Redis, o un mensaje entendible si no hay Redis del otro lado.

    redis-py conecta perezosamente: sin este ping, el primer comando explotaba
    con veinte líneas de traceback y un ``ConnectionRefusedError`` al final, que
    no le dice a nadie lo único que importa — que el sistema está apagado.
    """
    import redis as redis_pkg

    cliente = create_redis(url)
    try:
        cliente.ping()
    except redis_pkg.exceptions.RedisError as exc:
        destino = redact_url(url)
        raise SystemExit(
            f"No hay un Redis escuchando en {destino} ({type(exc).__name__}).\n\n"
            "  El estado de mineros y equipos vive ahí, así que sin Redis no hay\n"
            "  nada que limpiar. Levantá el sistema y volvé a intentar:\n\n"
            "      ./run.sh demo\n\n"
            "  (`./run.sh limpiar` lo arranca solo si hace falta; si estás\n"
            "  corriendo este script a mano, arrancalo vos.)"
        ) from exc
    return cliente


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="borrar de verdad (por defecto sólo muestra)")
    ap.add_argument("--all", dest="todo", action="store_true",
                    help="borrar TODOS los registros dinámicos y equipos, "
                         "estén corriendo o no")
    ap.add_argument("--redis-url", default=os.getenv("REDIS_URL",
                                                     "redis://localhost:6379/0"))
    args = ap.parse_args()

    r = _conectar(args.redis_url)
    vivos = {k.split("worker:status:", 1)[1] for k in r.scan_iter("worker:status:*")}

    if not vivos and not args.todo:
        # Sin nadie reportando, "fantasma" deja de distinguir: o el sistema está
        # apagado, o efectivamente no queda ninguno. Pedir --all obliga a decir
        # cuál de las dos cosas es, en vez de barrer con todo por accidente.
        print("Ningún minero está reportando estado.\n"
              "  Si el sistema está apagado, levantalo primero (./run.sh demo):\n"
              "  con todo caído no se puede distinguir un fantasma de uno vivo.\n"
              "  Si querés barrer con todo igual, agregá --all.")
        return 1

    mineros, equipos = relevar(r, todo=args.todo)

    if not mineros and not equipos:
        print("No hay nada para limpiar.")
        return 0

    print(f"EQUIPOS a borrar ({len(equipos)}):")
    for team_id in equipos:
        datos = r.hgetall(f"team:{team_id}")
        miembros = sorted(r.smembers(f"team:members:{team_id}"))
        print(f"  {datos.get('name', team_id)!r}  [{team_id}]")
        print(f"    coordinador: {datos.get('coordinator_worker_id', '?')}")
        print(f"    miembros:    {', '.join(miembros) or '(ninguno)'}")

    print(f"\nMINEROS a borrar ({len(mineros)}):")
    for worker_id in mineros:
        print(f"  {worker_id}")

    if not args.apply:
        print("\n(dry-run) Nada se borró. Agregá --apply para hacerlo.")
        return 0

    borradas = borrar(r, mineros, equipos)
    print(f"\nListo: {len(equipos)} equipo(s) y {len(mineros)} minero(s) borrados "
          f"({borradas} claves de Redis).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
