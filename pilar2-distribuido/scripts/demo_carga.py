#!/usr/bin/env python3
"""Inyector de carga para la demo en vivo: N leyes con ritmo controlado.

Pensado para correr durante la defensa del TP con Grafana proyectado. No es un
test con SLOs (para eso está `tests/stress/`): su único objetivo es que el
sistema tenga trabajo real y sostenido mientras se muestran los paneles.

Por qué el ritmo importa
------------------------
El NCT abre **una ventana por vez** (`coordinator.maybe_open_window`) y, con
N_ZEROS=4, un worker sella en fracciones de segundo. Si se mandan las 500 leyes
de golpe, la cadena se drena en un par de minutos y los paneles muestran un
pico seguido de una línea plana el resto de la exposición. Todos los paneles
del dashboard usan `rate(...[1m])`, así que lo que se ve lindo es un caudal
sostenido con escalones, no una ráfaga.

Perfiles
--------
    escalera  (default)  calentamiento → bajo → medio → ráfaga → medio → bajada
    constante            ritmo uniforme a lo largo de toda la duración
    rafaga               todo lo más rápido posible (para mostrar absorción de picos)

Uso
---
    # Demo típica: 500 leyes repartidas en 15 minutos, con tráfico de lectura
    python scripts/demo_carga.py --api-url https://voxchain.<ip>.sslip.io

    # Más corta y más intensa
    python scripts/demo_carga.py --total 500 --duracion 6

    # Ráfaga pura (chequear cómo absorbe un pico)
    python scripts/demo_carga.py --perfil rafaga --total 500

    # Sumar derogaciones (n+1 ceros, ventana más larga: da variedad al dashboard)
    python scripts/demo_carga.py --derogaciones 5

Ctrl-C corta en cualquier momento e imprime el resumen igual.

Sólo usa la biblioteca estándar: se puede correr desde cualquier notebook sin
instalar nada ni levantar el venv del proyecto.
"""

from __future__ import annotations

import argparse
import json
import random
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

# ── Colores ────────────────────────────────────────────────────────────────
B, DIM, G, R, Y, C, M, X = (
    "\033[1m", "\033[2m", "\033[92m", "\033[91m",
    "\033[93m", "\033[96m", "\033[95m", "\033[0m",
)


# ── Generación de textos de ley plausibles ─────────────────────────────────
# Textos variados y creíbles: durante la demo se ven en el frontend y en la
# cadena, y quedan mucho mejor que "stress-a1b2c3".

_MATERIAS = [
    "el transporte público de pasajeros", "las energías renovables",
    "la conectividad de banda ancha en zonas rurales", "la salud mental comunitaria",
    "la producción de software nacional", "la gestión integral de residuos",
    "el acceso al agua potable", "la educación técnica y profesional",
    "la protección de humedales", "la movilidad sustentable urbana",
    "el trabajo remoto en la administración pública", "la soberanía alimentaria",
    "la preservación del patrimonio histórico", "la economía del conocimiento",
    "el uso responsable de datos personales", "la eficiencia energética edilicia",
]

_MEDIDAS = [
    "se crea un régimen de promoción por el término de diez años",
    "se establece un fondo fiduciario de asistencia técnica",
    "se declara la emergencia sectorial por veinticuatro meses",
    "se dispone la creación de un registro nacional de carácter público",
    "se otorgan beneficios fiscales a las cooperativas del sector",
    "se instituye un programa federal de capacitación permanente",
    "se fija un cupo mínimo de participación para pequeños productores",
    "se ordena la elaboración de un plan estratégico quinquenal",
]

_ORGANOS = [
    "el Poder Ejecutivo Nacional", "la autoridad de aplicación que se designe",
    "el Ministerio competente en la materia", "el organismo descentralizado creado al efecto",
]


def texto_de_ley(numero: int) -> str:
    """Devuelve un texto de ley único (el sufijo evita la reproposición idéntica)."""
    materia = random.choice(_MATERIAS)
    medida = random.choice(_MEDIDAS)
    organo = random.choice(_ORGANOS)
    return (
        f"PROYECTO DE LEY Nº {numero}\n\n"
        f"Artículo 1º — Declárase de interés público {materia} en todo el "
        f"territorio nacional.\n\n"
        f"Artículo 2º — A los fines del artículo precedente, {medida}, cuya "
        f"reglamentación estará a cargo de {organo}.\n\n"
        f"Artículo 3º — Los gastos que demande el cumplimiento de la presente "
        f"se imputarán a las partidas presupuestarias correspondientes al "
        f"ejercicio en curso.\n\n"
        f"Artículo 4º — Comuníquese al Poder Ejecutivo Nacional.\n\n"
        f"[ref. {uuid.uuid4().hex[:12]}]"
    )


# ── Cliente HTTP mínimo ────────────────────────────────────────────────────

class Api:
    def __init__(self, base_url: str, inseguro: bool = False, timeout: float = 15.0):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.ctx = ssl._create_unverified_context() if inseguro else None

    def _abrir(self, req: urllib.request.Request):
        if self.ctx is not None:
            return urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx)
        return urllib.request.urlopen(req, timeout=self.timeout)

    def get(self, path: str):
        """GET que devuelve el JSON, o None ante cualquier fallo."""
        try:
            with self._abrir(urllib.request.Request(self.base + path)) as r:
                cuerpo = r.read()
            return json.loads(cuerpo) if cuerpo else None
        except Exception:
            return None

    def post_ley(self, payload: dict) -> tuple[int, str]:
        """POST /api/laws. Devuelve (status, detalle). status 0 = error de red."""
        req = urllib.request.Request(
            self.base + "/api/laws",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._abrir(req) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace")[:160]
        except Exception as e:
            return 0, f"{type(e).__name__}: {e}"


# ── Estado compartido ──────────────────────────────────────────────────────

@dataclass
class Estado:
    enviadas: int = 0
    ok: int = 0
    cooldown: int = 0          # 429
    errores: int = 0
    lecturas: int = 0
    etapa: str = "-"
    ritmo_objetivo: float = 0.0
    cadena: int = 0
    cola: int = 0
    ventana: str = "-"
    inicio: float = field(default_factory=time.monotonic)
    cadena_inicial: int = 0
    detalle_error: str = ""
    corriendo: bool = True
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def registrar(self, status: int, detalle: str) -> None:
        with self._lock:
            self.enviadas += 1
            if status == 200:
                self.ok += 1
            elif status == 429:
                self.cooldown += 1
            else:
                self.errores += 1
                if detalle:
                    self.detalle_error = detalle


# ── Hilos auxiliares ───────────────────────────────────────────────────────

def hilo_muestreo(api: Api, est: Estado) -> None:
    """Consulta el estado de la cadena cada 3 s (barato, no ensucia las métricas)."""
    while est.corriendo:
        cadena = api.get("/api/chain")
        cola = api.get("/api/laws/queue")
        ventana = api.get("/api/windows/active")
        if cadena is not None:
            est.cadena = len(cadena)
        if cola is not None:
            est.cola = len(cola)
        est.ventana = (ventana or {}).get("voting_window_id", "-")
        time.sleep(3.0)


_RUTAS_LECTURA = [
    ("/api/chain", 3),
    ("/api/laws", 3),
    ("/api/laws/queue", 2),
    ("/api/laws/next", 2),
    ("/api/windows/active", 2),
    ("/api/health", 1),
    ("/api/accounts", 1),
    ("/api/workers/status", 1),
]


def hilo_lector(api: Api, est: Estado) -> None:
    """Tráfico de lectura de fondo.

    Puebla los paneles 'HTTP Requests/s por ruta' y 'Requests por método HTTP',
    que con sólo POSTs mostrarían una única serie.
    """
    rutas = [r for r, peso in _RUTAS_LECTURA for _ in range(peso)]
    while est.corriendo:
        api.get(random.choice(rutas))
        with est._lock:
            est.lecturas += 1
        time.sleep(random.uniform(0.6, 1.6))


def hilo_estado(est: Estado, total: int) -> None:
    """Línea de estado en vivo, se reescribe sobre sí misma."""
    giro = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    i = 0
    while est.corriendo:
        t = time.monotonic() - est.inicio
        real = est.enviadas / t if t > 0 else 0.0
        nuevos = max(0, est.cadena - est.cadena_inicial)
        linea = (
            f"\r  {C}{giro[i % len(giro)]}{X} {B}{est.enviadas:>4}/{total}{X} leyes  "
            f"{DIM}│{X} {G}ok {est.ok}{X}  {Y}429 {est.cooldown}{X}  {R}err {est.errores}{X}  "
            f"{DIM}│{X} {est.ritmo_objetivo:>4.1f}/s obj  {real:>4.1f}/s real  "
            f"{DIM}│{X} cadena {M}{est.cadena}{X} {DIM}(+{nuevos}){X}  cola {est.cola}  "
            f"{DIM}│{X} {C}{est.etapa}{X}  {DIM}{int(t)}s{X}   "
        )
        sys.stdout.write(linea)
        sys.stdout.flush()
        i += 1
        time.sleep(0.2)


# ── Perfiles de carga ──────────────────────────────────────────────────────
# Cada etapa: (nombre, fracción del total, peso de ritmo relativo).

PERFILES: dict[str, list[tuple[str, float, float]]] = {
    "escalera": [
        ("calentamiento", 0.06, 1.0),
        ("ritmo bajo",    0.14, 2.0),
        ("ritmo medio",   0.24, 4.0),
        ("RÁFAGA",        0.32, 12.0),
        ("ritmo medio",   0.16, 4.0),
        ("enfriamiento",  0.08, 1.0),
    ],
    "constante": [("ritmo constante", 1.0, 1.0)],
}


def planificar(perfil: str, total: int, duracion_s: float) -> list[tuple[str, int, float]]:
    """Traduce un perfil a etapas concretas: (nombre, cantidad, leyes/segundo).

    Escala los pesos con un factor k tal que la suma de las duraciones de las
    etapas dé exactamente `duracion_s`:  t_i = n_i / (k·w_i)  ⇒  k = Σ(f_i/w_i)·total/T
    """
    etapas = PERFILES[perfil]
    k = sum(f / w for _, f, w in etapas) * total / duracion_s

    plan: list[tuple[str, int, float]] = []
    asignadas = 0
    for idx, (nombre, frac, peso) in enumerate(etapas):
        # La última etapa absorbe el redondeo para que el total sea exacto.
        n = total - asignadas if idx == len(etapas) - 1 else round(total * frac)
        asignadas += n
        if n > 0:
            plan.append((nombre, n, k * peso))
    return plan


# ── Envío ──────────────────────────────────────────────────────────────────

# Áreas de gobierno con las que la demo reparte la carga. Que las leyes caigan
# en categorías distintas es lo que hace visible el efecto de los equipos con
# agenda: si todas fueran "general", ningún equipo dejaría de aportar cómputo y
# la demo no mostraría la diferencia.
CATEGORIAS = (
    "economia", "salud", "educacion", "seguridad",
    "ambiente", "infraestructura", "derechos", "general",
)


def enviar_ley(api: Api, est: Estado, numero: int) -> None:
    """Propone una ley con autor único (autor distinto ⇒ nunca cae en cooldown)."""
    payload = {
        "text": texto_de_ley(numero),
        "author_pubkey": f"pk-demo-{numero:04d}-{uuid.uuid4().hex[:8]}",
        "action": "promulgacion",
        # Round-robin en vez de aleatorio: dos corridas de la demo con el mismo
        # total reparten igual, y eso hace comparables sus resultados.
        "category": CATEGORIAS[numero % len(CATEGORIAS)],
    }
    est.registrar(*api.post_ley(payload))


def enviar_derogacion(api: Api, est: Estado, law_id: str) -> None:
    """Propone derogar una ley ya promulgada (exige n+1 ceros y ventana más larga)."""
    payload = {
        "law_id": law_id,
        "text": f"Derógase en todos sus términos la ley {law_id}. "
                f"[ref. {uuid.uuid4().hex[:12]}]",
        "author_pubkey": f"pk-derog-{uuid.uuid4().hex[:8]}",
        "action": "derogacion",
    }
    est.registrar(*api.post_ley(payload))


def leyes_promulgadas(api: Api, cantidad: int) -> list[str]:
    leyes = api.get("/api/laws") or []
    ids = [l["law_id"] for l in leyes
           if isinstance(l, dict) and l.get("status") == "promulgated" and l.get("law_id")]
    random.shuffle(ids)
    return ids[:cantidad]


def correr_plan(api: Api, est: Estado, pool: ThreadPoolExecutor,
                plan: list[tuple[str, int, float]], desde: int) -> int:
    """Ejecuta el plan respetando el ritmo de cada etapa. Devuelve el próximo número."""
    numero = desde
    for nombre, cantidad, ritmo in plan:
        est.etapa = nombre
        est.ritmo_objetivo = ritmo
        intervalo = 1.0 / ritmo
        proxima = time.monotonic()
        for _ in range(cantidad):
            if not est.corriendo:
                return numero
            proxima += intervalo
            pool.submit(enviar_ley, api, est, numero)
            numero += 1
            espera = proxima - time.monotonic()
            if espera > 0:
                time.sleep(espera)
    return numero


# ── Resumen final ──────────────────────────────────────────────────────────

def imprimir_resumen(api: Api, est: Estado, total: int) -> None:
    transcurrido = time.monotonic() - est.inicio
    # Un respiro para que el NCT termine de drenar lo que quedó en vuelo, y
    # relectura fresca: el hilo de muestreo puede tener valores de hace 3 s.
    time.sleep(2.0)
    cadena_final = api.get("/api/chain")
    if cadena_final is not None:
        est.cadena = len(cadena_final)
    cola_final = api.get("/api/laws/queue")
    if cola_final is not None:
        est.cola = len(cola_final)
    sellados = max(0, est.cadena - est.cadena_inicial)

    print(f"\n\n{C}{B}{'─' * 62}{X}")
    print(f"{B}  RESUMEN{X}")
    print(f"{C}{'─' * 62}{X}")
    print(f"  Propuestas enviadas   {B}{est.enviadas}{X} / {total}")
    print(f"  Aceptadas (200)       {G}{est.ok}{X}")
    if est.cooldown:
        print(f"  Rechazadas (429)      {Y}{est.cooldown}{X} {DIM}(cooldown del autor){X}")
    if est.errores:
        print(f"  Errores               {R}{est.errores}{X}  {DIM}{est.detalle_error}{X}")
    print(f"  Lecturas de fondo     {est.lecturas}")
    print(f"  Duración              {transcurrido:.0f} s")
    print(f"  Throughput propuestas {est.enviadas / transcurrido:.2f}/s"
          if transcurrido > 0 else "")
    print(f"  Cadena                {est.cadena_inicial} → {M}{est.cadena}{X} "
          f"bloques {DIM}(+{sellados} sellados){X}")
    if est.cola:
        print(f"  Cola pendiente        {Y}{est.cola}{X} {DIM}(el NCT sigue drenando){X}")
    print(f"{C}{'─' * 62}{X}\n")


# ── main ───────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Inyecta N leyes con ritmo controlado para la demo en Grafana",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--api-url", default="http://localhost:8000",
                    help="URL base del API (Ingress de GKE o localhost)")
    ap.add_argument("--total", type=int, default=500, help="cantidad de leyes (default 500)")
    ap.add_argument("--perfil", choices=["escalera", "constante", "rafaga"],
                    default="escalera", help="forma de la curva de carga")
    ap.add_argument("--duracion", type=float, default=15.0,
                    help="minutos sobre los que repartir la carga (no aplica a 'rafaga')")
    ap.add_argument("--lectores", type=int, default=3,
                    help="hilos de tráfico GET de fondo (0 para desactivar)")
    ap.add_argument("--derogaciones", type=int, default=0,
                    help="derogaciones a intercalar al final (n+1 ceros, ventana larga)")
    ap.add_argument("--concurrencia", type=int, default=12,
                    help="hilos de envío HTTP")
    ap.add_argument("--insecure", action="store_true",
                    help="no validar el certificado TLS")
    args = ap.parse_args()

    api = Api(args.api_url, inseguro=args.insecure)

    print(f"""
{C}{B}╔══════════════════════════════════════════════════════════╗
║        VoxChain — Inyector de carga para la demo         ║
╚══════════════════════════════════════════════════════════╝{X}""")

    salud = api.get("/api/health")
    if salud is None:
        print(f"  {R}✗{X} no se pudo contactar {args.api_url}/api/health")
        print(f"  {DIM}¿URL correcta? ¿Hace falta --insecure?{X}\n")
        return 1
    estado_salud = "  ".join(
        f"{G if v == 'ok' else Y}{k}={v}{X}" for k, v in salud.items())
    print(f"\n  {G}✓{X} API viva   {estado_salud}")

    est = Estado()
    est.cadena_inicial = len(api.get("/api/chain") or [])
    est.cadena = est.cadena_inicial
    print(f"  {DIM}cadena inicial: {est.cadena_inicial} bloques{X}")

    if args.perfil == "rafaga":
        plan = [("RÁFAGA (sin pausa)", args.total, float("inf"))]
        print(f"  {DIM}perfil: ráfaga — {args.total} leyes lo más rápido posible{X}\n")
    else:
        plan = planificar(args.perfil, args.total, args.duracion * 60)
        print(f"  {DIM}perfil: {args.perfil} — {args.total} leyes en "
              f"~{args.duracion:.0f} min{X}")
        for nombre, n, ritmo in plan:
            print(f"    {DIM}· {nombre:<16} {n:>4} leyes  a {ritmo:>5.2f}/s  "
                  f"≈{n / ritmo:>4.0f}s{X}")
        print()

    hilos = [threading.Thread(target=hilo_muestreo, args=(api, est), daemon=True)]
    hilos += [threading.Thread(target=hilo_lector, args=(api, est), daemon=True)
              for _ in range(args.lectores)]
    hilos.append(threading.Thread(target=hilo_estado, args=(est, args.total), daemon=True))
    for h in hilos:
        h.start()

    est.inicio = time.monotonic()
    pool = ThreadPoolExecutor(max_workers=args.concurrencia)
    try:
        if args.perfil == "rafaga":
            est.etapa = "RÁFAGA"
            est.ritmo_objetivo = 0.0
            list(pool.map(lambda n: enviar_ley(api, est, n), range(1, args.total + 1)))
            numero = args.total + 1
        else:
            numero = correr_plan(api, est, pool, plan, desde=1)

        if args.derogaciones > 0 and est.corriendo:
            est.etapa = "derogaciones"
            objetivos = leyes_promulgadas(api, args.derogaciones)
            if not objetivos:
                est.etapa = "derogaciones (sin leyes promulgadas todavía)"
            for law_id in objetivos:
                if not est.corriendo:
                    break
                pool.submit(enviar_derogacion, api, est, law_id)
                time.sleep(2.0)   # la derogación exige n+1 ceros: no la apuremos

        pool.shutdown(wait=True)
    except KeyboardInterrupt:
        print(f"\n\n  {Y}⚠{X} interrumpido — cerrando envíos en vuelo...")
        pool.shutdown(wait=False, cancel_futures=True)
    finally:
        est.corriendo = False
        time.sleep(0.3)

    imprimir_resumen(api, est, args.total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
