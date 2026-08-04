#!/usr/bin/env python3
"""Gráficos comparativos para el informe (checklist §7).

Lee los CSV de pilar3-despliegue/load-tests/resultados/ y genera un PNG por
configuración de prueba. Pensado para embeber en el informe escrito, así que
sale en modo claro y a 200 dpi.

Uso:
  python3 graficos.py [--in DIR_CSV] [--out DIR_PNG]
"""

import argparse
import csv
import os

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import FuncFormatter

# Paleta y tinta: un solo hue para el dato, gris para el contexto/referencia.
SERIE = "#2a78d6"       # azul: lo medido
CONTEXTO = "#898781"    # gris: la referencia (techo del hardware)
TINTA = "#0b0b0b"
TINTA_2 = "#52514e"
MUTED = "#898781"
GRILLA = "#e1e0d9"
EJE = "#c3c2b7"
SUPERFICIE = "#fcfcfb"

plt.rcParams.update({
    "font.family": "sans-serif",
    "font.sans-serif": ["DejaVu Sans"],
    "figure.facecolor": SUPERFICIE,
    "axes.facecolor": SUPERFICIE,
    "axes.edgecolor": EJE,
    "axes.labelcolor": TINTA_2,
    "text.color": TINTA,
    "xtick.color": MUTED,
    "ytick.color": MUTED,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "axes.titlesize": 13,
    "axes.labelsize": 10,
    "figure.dpi": 200,
})


def leer(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def base(ax, titulo, subtitulo=None, ylabel=None):
    """Chrome común: grilla recesiva, sin marco, título arriba a la izquierda."""
    ax.set_title(titulo, loc="left", pad=18 if subtitulo else 10,
                 fontweight="bold", color=TINTA)
    if subtitulo:
        ax.text(0, 1.02, subtitulo, transform=ax.transAxes,
                fontsize=9.5, color=TINTA_2, va="bottom")
    if ylabel:
        ax.set_ylabel(ylabel, color=TINTA_2)
    ax.yaxis.grid(True, color=GRILLA, linewidth=1, zorder=0)
    ax.xaxis.grid(False)
    ax.set_axisbelow(True)
    for lado in ("top", "right", "left"):
        ax.spines[lado].set_visible(False)
    ax.spines["bottom"].set_color(EJE)
    ax.tick_params(length=0)


def guardar(fig, out, nombre):
    path = os.path.join(out, nombre)
    fig.savefig(path, bbox_inches="tight", facecolor=SUPERFICIE)
    plt.close(fig)
    print(f"  {path}")


# Escalado del mismo minero corriendo en procesos sueltos, SIN nada del sistema
# distribuido, en esta misma máquina (mediana de 3 repeticiones). Es la
# referencia honesta: sólo se comparan cocientes, porque las tasas absolutas de
# las dos mediciones no son el mismo experimento.
TECHO_HW = {1: 954180, 2: 1549091, 4: 2165728}


def g_capacidad(rows, out):
    ms = [int(r["miners"]) for r in rows]
    medido = [int(r["hashrate_nonces_s"]) for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    x = range(len(ms))
    ax.bar(x, medido, 0.5, color=SERIE, zorder=3)
    for i, m in enumerate(medido):
        ax.text(i, m + 26000, f"{m/1000:,.0f}k", ha="center", fontsize=10.5,
                color=TINTA, fontweight="bold")
        if i:
            ax.text(i, m / 2, f"×{m/medido[0]:.2f}", ha="center", va="center",
                    fontsize=11, color="#ffffff", fontweight="bold")

    ax.set_xticks(list(x))
    ax.set_xticklabels([f"M = {m}" for m in ms])
    ax.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v/1e6:.1f}M"))
    ax.set_ylim(0, max(medido) * 1.16)
    base(ax, "Capacidad de cómputo según cantidad de mineros",
         "Nonces barridos por segundo, derivado del nonce ganador de cada "
         "bloque. 10 leyes por configuración.", "nonces / s")
    guardar(fig, out, "escalado-capacidad.png")


def g_speedup(rows, out):
    ms = [int(r["miners"]) for r in rows]
    hr = [int(r["hashrate_nonces_s"]) for r in rows]
    sp = [h / hr[0] for h in hr]
    techo = [TECHO_HW[m] / TECHO_HW[1] for m in ms]

    fig, ax = plt.subplots(figsize=(7.2, 4.6))
    # Tres referencias: el ideal inalcanzable, lo que da la CPU sola, y lo
    # medido. La distancia entre las dos primeras es culpa del hardware; la
    # distancia entre las dos últimas sería culpa de la coordinación.
    ax.plot(ms, ms, linestyle=(0, (4, 3)), linewidth=2, color=EJE, zorder=2,
            label="Ideal lineal (inalcanzable)")
    ax.plot(ms, techo, linewidth=2, color=CONTEXTO, marker="s", markersize=8,
            zorder=3, label="Minero suelto, sin el sistema")
    ax.plot(ms, sp, linewidth=2, color=SERIE, marker="o", markersize=9,
            zorder=4, label="Pool VoxChain (medido)")

    for m, s in zip(ms, sp):
        ax.annotate(f"{s:.2f}x", (m, s), textcoords="offset points",
                    xytext=(0, 12), ha="center", fontsize=10,
                    color=TINTA, fontweight="bold")
    for m, t in list(zip(ms, techo))[1:]:
        ax.annotate(f"{t:.2f}x", (m, t), textcoords="offset points",
                    xytext=(0, -22), ha="center", fontsize=9.5, color=TINTA_2)

    ax.set_xticks(ms)
    ax.set_xticklabels([f"M = {m}" for m in ms])
    ax.set_ylim(0.6, max(ms) * 1.1)
    base(ax, "El límite es la CPU, no la coordinación",
         "El pool escala igual o mejor que el mismo minero en procesos "
         "sueltos: la capa distribuida no agrega costo.", "speedup (veces)")
    ax.legend(frameon=False, loc="upper left", fontsize=9.5, labelcolor=TINTA_2)
    guardar(fig, out, "escalado-speedup.png")


def g_tiempo_total(rows, out):
    ms = [int(r["miners"]) for r in rows]
    tot = [float(r["total_time_s"]) for r in rows]
    laws = rows[0]["laws"]

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    x = range(len(ms))
    ax.bar(x, tot, 0.5, color=SERIE, zorder=3)
    for i, t in enumerate(tot):
        ax.text(i, t + 4, f"{t:.0f} s", ha="center", fontsize=10,
                color=TINTA, fontweight="bold")
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"M = {m}" for m in ms])
    ax.set_ylim(0, max(tot) * 1.15)
    base(ax, f"Tiempo total para sellar {laws} transacciones",
         "Tiempo de respuesta extremo a extremo, dificultad 6.", "segundos")
    guardar(fig, out, "escalado-tiempo-total.png")


def g_dispersion(detalle, resumen, out):
    ms = sorted({int(r["miners"]) for r in detalle})
    fig, ax = plt.subplots(figsize=(7.2, 4.4))

    for i, m in enumerate(ms):
        vals = [float(r["time_to_seal_s"]) for r in detalle
                if int(r["miners"]) == m]
        # Dispersión horizontal determinista: evita que dos puntos iguales se
        # tapen sin depender del azar (el gráfico tiene que ser reproducible).
        offs = [(j % 5 - 2) * 0.045 for j in range(len(vals))]
        ax.scatter([i + o for o in offs], vals, s=44, color=SERIE, alpha=0.55,
                   linewidths=1.2, edgecolors=SUPERFICIE, zorder=3)
        media = float(next(r["mean_time_per_law_s"] for r in resumen
                           if int(r["miners"]) == m))
        ax.plot([i - 0.2, i + 0.2], [media, media], linewidth=2.5,
                color=TINTA, zorder=4)
        ax.text(i + 0.24, media, f"media {media:.1f} s", fontsize=9,
                color=TINTA_2, va="center")

    ax.set_xticks(range(len(ms)))
    ax.set_xticklabels([f"M = {m}" for m in ms])
    ax.set_xlim(-0.5, len(ms) - 0.2)
    base(ax, "Tiempo de sellado ley por ley",
         "Cada punto es una ley. La enorme dispersión es inherente a la prueba "
         "de trabajo, no ruido de medición.", "segundos")
    guardar(fig, out, "escalado-dispersion.png")


# -- pruebas previas (misma batería de carga) ------------------------------
def g_dificultad(rows, out):
    n = [int(r["n_zeros"]) for r in rows]
    t = [float(r["time_to_seal_s"]) for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    ax.plot(n, t, linewidth=2, color=SERIE, marker="o", markersize=8, zorder=3)
    for xi, yi in zip(n, t):
        ax.annotate(f"{yi:.1f} s", (xi, yi), textcoords="offset points",
                    xytext=(0, 11), ha="center", fontsize=9, color=TINTA_2)
    ax.set_yscale("log")
    ax.set_xticks(n)
    ax.set_xlabel("ceros exigidos en el prefijo", color=TINTA_2)
    base(ax, "Costo del minado según la dificultad",
         "Escala logarítmica. Por debajo de 4 ceros el tiempo lo domina el "
         "overhead del sistema, no el minado; de 4 en adelante se ve el "
         "crecimiento exponencial (×16 por cero).",
         "segundos hasta sellar (log)")
    guardar(fig, out, "dificultad.png")


def g_fragmentacion(rows, out):
    pct = [int(r["fragment_pct"]) for r in rows]
    t = [float(r["time_to_seal_s"]) for r in rows]
    tasks = [int(r["tasks_created"]) for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    x = range(len(pct))
    ax.bar(x, t, 0.5, color=SERIE, zorder=3)
    for i, (ti, tk) in enumerate(zip(t, tasks)):
        ax.text(i, ti + 0.08, f"{ti:.2f} s", ha="center", fontsize=9.5,
                color=TINTA, fontweight="bold")
    ax.set_xticks(list(x))
    # La cantidad de tareas va en el propio tick, así no pisa el rótulo del eje.
    ax.set_xticklabels([f"{p}%\n{tk} tareas" for p, tk in zip(pct, tasks)])
    ax.set_xlabel("tamaño del fragmento (% del espacio de nonces)",
                  color=TINTA_2, labelpad=12)
    ax.set_ylim(0, max(t) * 1.2)
    base(ax, "Efecto de la fragmentación del pool",
         "A dificultad 4 el tamaño del fragmento casi no incide en el tiempo.",
         "segundos hasta sellar")
    guardar(fig, out, "fragmentacion.png")


def g_bulk(rows, out):
    size = [int(r["size"]) for r in rows]
    thr = [float(r["throughput_proposals_s"]) for r in rows]

    fig, ax = plt.subplots(figsize=(7.2, 4.0))
    x = range(len(size))
    ax.bar(x, thr, 0.5, color=SERIE, zorder=3)
    for i, v in enumerate(thr):
        ax.text(i, v + 0.5, f"{v:.1f}", ha="center", fontsize=9.5,
                color=TINTA, fontweight="bold")
    ax.set_xticks(list(x))
    ax.set_xticklabels([f"{s:,}" for s in size])
    ax.set_xlabel("propuestas enviadas en el bulk", color=TINTA_2)
    ax.set_ylim(0, max(thr) * 1.18)
    base(ax, "Throughput de ingreso de propuestas",
         "Propuestas aceptadas por segundo según el tamaño del envío.",
         "propuestas / s")
    guardar(fig, out, "bulk.png")


def main():
    p = argparse.ArgumentParser()
    aca = os.path.dirname(os.path.abspath(__file__))
    p.add_argument("--in", dest="entrada",
                   default=os.path.join(aca, "..", "resultados"))
    p.add_argument("--out", dest="salida",
                   default=os.path.join(aca, "..", "..", "..",
                                        "docs", "informe", "graficos"))
    args = p.parse_args()
    os.makedirs(args.salida, exist_ok=True)

    def csv_path(n):
        return os.path.join(args.entrada, n)

    print("Generando gráficos:")
    recursos = leer(csv_path("resultados_recursos.csv"))
    detalle = leer(csv_path("resultados_recursos_detalle.csv"))
    g_capacidad(recursos, args.salida)
    g_speedup(recursos, args.salida)
    g_tiempo_total(recursos, args.salida)
    g_dispersion(detalle, recursos, args.salida)
    g_dificultad(leer(csv_path("resultados_dificultad.csv")), args.salida)
    g_fragmentacion(leer(csv_path("resultados_fragmentacion.csv")), args.salida)
    g_bulk(leer(csv_path("resultados_bulk.csv")), args.salida)
    print("Listo.")


if __name__ == "__main__":
    main()
