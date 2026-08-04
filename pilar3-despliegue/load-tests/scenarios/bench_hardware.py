#!/usr/bin/env python3
"""Techo de cómputo de la máquina: el minero suelto, sin nada del sistema.

Es la referencia contra la que se juzga el escalado del pool (ver
`graficos.py`, constante TECHO_HW). Sin este número, un speedup de 2,77x con 4
mineros no se puede interpretar: no se sabe cuánto de lo que falta para 4x es
culpa del diseño distribuido y cuánto de que la CPU no da más.

Corre el mismo `brute_force.py` que usan los workers, en N procesos paralelos,
y mide nonces por segundo agregados.

Detalles que importan para que el número sea válido:
  - Prefijo inalcanzable (8 caracteres): si el minero encontrara una solución
    cortaría antes de barrer el rango y el tiempo no sería comparable. El costo
    por hash no cambia, porque la comparación corta en el primer dígito que no
    coincide.
  - Mediana de varias repeticiones: la primera corrida suele salir alta (turbo
    en frío) y cualquier proceso de fondo hunde una corrida suelta.
  - Correr con la máquina descargada. En particular, sin contenedores
    levantándose o bajándose: eso arruinó la primera medición que hicimos.

Uso:
  python3 bench_hardware.py [--procesos 1,2,4] [--nonces 3000000] [--repes 3]
"""

import argparse
import os
import subprocess
import sys
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
MINERO = os.path.normpath(
    os.path.join(AQUI, "..", "..", "..", "pilar1-minero", "cpu", "src", "brute_force.py"))
PREFIJO_INALCANZABLE = "ffffffff"


def una_corrida(paralelas: int, nonces: int) -> float:
    procs = [
        subprocess.Popen(
            [sys.executable, MINERO, f"base{i}", PREFIJO_INALCANZABLE, "0", str(nonces)],
            stdout=subprocess.DEVNULL,
        )
        for i in range(paralelas)
    ]
    inicio = time.perf_counter()
    for p in procs:
        p.wait()
    return nonces * paralelas / (time.perf_counter() - inicio)


def main() -> None:
    p = argparse.ArgumentParser(description="Techo de cómputo del minero CPU")
    p.add_argument("--procesos", default="1,2,4")
    p.add_argument("--nonces", type=int, default=3_000_000)
    p.add_argument("--repes", type=int, default=3)
    args = p.parse_args()

    if not os.path.exists(MINERO):
        raise SystemExit(f"no encuentro el minero en {MINERO}")

    print(f"Minero: {MINERO}")
    print(f"{args.nonces:,} nonces por proceso · mediana de {args.repes} repeticiones")
    print(f"CPUs visibles: {os.cpu_count()}\n")

    resultados = {}
    for n in [int(x) for x in args.procesos.split(",")]:
        vals = sorted(una_corrida(n, args.nonces) for _ in range(args.repes))
        resultados[n] = vals[len(vals) // 2]
        muestras = ", ".join(f"{v/1000:.0f}k" for v in vals)
        print(f"  {n} proceso(s): [{muestras}] → mediana {resultados[n]:,.0f} H/s")

    base = resultados[min(resultados)]
    print(f"\n{'procesos':>9} {'H/s':>12} {'speedup':>9}")
    for n, v in resultados.items():
        print(f"{n:>9} {v:>12,.0f} {v/base:>8.2f}x")
    print("\nPara graficos.py:")
    print("TECHO_HW =", {k: int(v) for k, v in resultados.items()})


if __name__ == "__main__":
    main()
