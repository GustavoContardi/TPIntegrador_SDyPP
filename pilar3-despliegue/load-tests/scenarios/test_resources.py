#!/usr/bin/env python3
"""Prueba de escalado: N transacciones con M recursos (checklist §4 y §7).

Propone N leyes de una vez y mide el tiempo de pared hasta que las N quedan
selladas en la cadena. El NCT abre una ventana de votación por vez, así que las
leyes se sellan en serie y el total es casi tiempo de minado puro: es la métrica
que se mueve al agregar mineros.

Este script NO cambia la cantidad de mineros (eso lo hace run_scaling.sh vía
`docker compose --scale`); `--miners` es sólo la etiqueta de la fila del CSV.

Uso:
  python3 test_resources.py --api-url http://localhost:8000 --miners 2 --laws 10

Salida:
  --output        CSV resumen, una fila por configuración (soporta --append).
  --detail-output CSV con el tiempo de sellado de cada ley (para desvíos y
                  barras de error en el informe).
"""

import argparse
import csv
import json
import os
import statistics
import time
import uuid
from urllib.error import URLError
from urllib.request import Request, urlopen

API_URL = "http://localhost:8000"


def _req(method: str, path: str, data: bytes | None = None, timeout: float = 30.0) -> dict:
    req = Request(f"{API_URL}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=timeout) as resp:
            return {"status": resp.status, "body": json.loads(resp.read().decode())}
    except URLError as e:
        return {"error": str(e)}
    except json.JSONDecodeError as e:
        return {"error": f"respuesta no-JSON: {e}"}


def propose(text: str) -> str | None:
    """Publica una ley y devuelve su law_id.

    Cada propuesta usa un author_pubkey distinto: el API aplica cooldown por
    autor y devolvería 429 si se reusara el mismo.
    """
    payload = json.dumps({
        "text": text,
        "author_pubkey": f"pk-scale-{uuid.uuid4().hex[:12]}",
    }).encode()
    result = _req("POST", "/api/laws", payload)
    if "error" in result:
        print(f"    error al proponer: {result['error']}")
        return None
    law_id = result.get("body", {}).get("law_id")
    if not law_id:
        print(f"    respuesta sin law_id: {result.get('body')}")
    return law_id


def sealed_nonces() -> dict[str, int]:
    """law_id -> nonce ganador de los bloques ya sellados.

    El nonce importa además de como identificador: los mineros barren el espacio
    desde 0, así que el nonce ganador es (aproximadamente) la cantidad de nonces
    que hubo que probar para esa ley. Sumarlos da el trabajo total realizado, y
    dividido por el tiempo da la capacidad de cómputo efectiva del pool.
    """
    chain = _req("GET", "/api/chain", timeout=60)
    blocks = chain.get("body")
    if not isinstance(blocks, list):
        return {}
    return {b["law_id"]: int(b.get("nonce", 0))
            for b in blocks
            if isinstance(b, dict) and b.get("law_id")}


def run(miners: int, laws: int, n_zeros: int, timeout: float,
        poll: float) -> tuple[dict, list[dict]]:
    print(f"  Proponiendo {laws} leyes...", flush=True)

    # Lo ya sellado antes de empezar no cuenta (la cadena puede venir con
    # bloques de una corrida anterior si no se levantó el stack de cero).
    already = set(sealed_nonces())

    start = time.monotonic()
    pending: dict[str, None] = {}
    for i in range(laws):
        law_id = propose(f"Ley de escalado {i + 1}/{laws} ({uuid.uuid4().hex[:8]})")
        if law_id:
            pending[law_id] = None

    if not pending:
        raise SystemExit("no se pudo proponer ninguna ley: ¿está el API arriba?")
    if len(pending) < laws:
        print(f"    ojo: sólo entraron {len(pending)}/{laws} propuestas")

    print(f"  Esperando sellado (timeout {timeout:.0f}s)", end="", flush=True)
    seal_times: dict[str, float] = {}
    nonces: dict[str, int] = {}
    while len(seal_times) < len(pending) and time.monotonic() - start < timeout:
        time.sleep(poll)
        for law_id, nonce in sealed_nonces().items():
            if law_id in already:
                continue
            if law_id in pending and law_id not in seal_times:
                seal_times[law_id] = time.monotonic() - start
                nonces[law_id] = nonce
                print(".", end="", flush=True)
    total = time.monotonic() - start
    timed_out = len(pending) - len(seal_times)
    print(" ✓" if not timed_out else f" TIMEOUT ({timed_out} sin sellar)")

    ordered = sorted(seal_times.items(), key=lambda kv: kv[1])
    # Tiempo por ley = hueco entre sellados consecutivos. Como el NCT sella de a
    # una ventana por vez, cada hueco es el minado de esa ley.
    per_law = []
    previous = 0.0
    for idx, (law_id, at) in enumerate(ordered, start=1):
        per_law.append({
            "miners": miners,
            "n_zeros": n_zeros,
            "seq": idx,
            "law_id": law_id,
            "sealed_at_s": round(at, 3),
            "time_to_seal_s": round(at - previous, 3),
            "nonce": nonces.get(law_id, 0),
        })
        previous = at

    deltas = [r["time_to_seal_s"] for r in per_law]
    sealed = len(ordered)
    # Trabajo total = suma de los nonces ganadores. A diferencia del tiempo por
    # ley (exponencial, con desvío del orden de la media), este cociente casi no
    # tiene varianza: es la medida limpia de capacidad para comparar M vs 2xM.
    total_nonces = sum(nonces.values())
    summary = {
        "miners": miners,
        "n_zeros": n_zeros,
        "laws": len(pending),
        "sealed": sealed,
        "timed_out": timed_out,
        # Con leyes sin sellar el total es el timeout, no una medida de
        # capacidad: se marca -1 para que no entre en los gráficos.
        "total_time_s": round(total, 3) if not timed_out else -1,
        "mean_time_per_law_s": round(statistics.mean(deltas), 3) if deltas else -1,
        "stdev_time_per_law_s": (round(statistics.stdev(deltas), 3)
                                 if len(deltas) > 1 else 0.0),
        "throughput_laws_min": (round(sealed / total * 60, 2)
                                if sealed and total > 0 else 0.0),
        "total_nonces": total_nonces,
        "hashrate_nonces_s": (round(total_nonces / total) if total > 0 else 0),
    }
    return summary, per_law


def write_csv(path: str, rows: list[dict], append: bool) -> None:
    if not rows:
        return
    exists = append and os.path.exists(path) and os.path.getsize(path) > 0
    with open(path, "a" if append else "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        if not exists:
            w.writeheader()
        w.writerows(rows)


def main() -> None:
    global API_URL
    p = argparse.ArgumentParser(description="Escalado: N transacciones con M recursos")
    p.add_argument("--api-url", default=API_URL)
    p.add_argument("--miners", type=int, required=True,
                   help="mineros activos (etiqueta; incluye al pool-coordinator)")
    p.add_argument("--laws", type=int, default=10, help="N transacciones")
    p.add_argument("--n-zeros", type=int, default=6,
                   help="dificultad configurada en el stack (etiqueta)")
    p.add_argument("--timeout", type=float, default=1800.0)
    p.add_argument("--poll", type=float, default=1.0)
    p.add_argument("--output", default="resultados_recursos.csv")
    p.add_argument("--detail-output", default="")
    p.add_argument("--append", action="store_true")
    args = p.parse_args()
    API_URL = args.api_url

    print(f"=== {args.laws} transacciones con {args.miners} minero(s), "
          f"n_zeros={args.n_zeros} ===")
    summary, per_law = run(args.miners, args.laws, args.n_zeros,
                           args.timeout, args.poll)

    write_csv(args.output, [summary], args.append)
    if args.detail_output:
        write_csv(args.detail_output, per_law, args.append)

    print(f"  total={summary['total_time_s']}s  "
          f"media/ley={summary['mean_time_per_law_s']}s "
          f"(σ={summary['stdev_time_per_law_s']})  "
          f"throughput={summary['throughput_laws_min']} leyes/min")
    print(f"  trabajo={summary['total_nonces']:,} nonces  →  "
          f"{summary['hashrate_nonces_s']:,} nonces/s")
    print(f"  → {args.output}")


if __name__ == "__main__":
    main()
