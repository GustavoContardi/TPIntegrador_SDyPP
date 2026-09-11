"""Dificultad dinámica: `n` en función del cómputo vivo de la red.

**Qué resuelve.** Con `n` fijo, el esfuerzo de promulgar depende de cuánto
cómputo haya conectado: con la red chica una ley cuesta minutos y con la red
grande, milisegundos. El objetivo acá es el inverso — que **promulgar cueste
siempre lo mismo en tiempo**, `DIFFICULTY_TARGET_SECONDS`, sin importar cuántos
mineros se registren. `n` deja de ser una constante y pasa a ser la variable que
absorbe los cambios de población.

**Cómo se mide la red, que es la parte que importa.** El error natural es sumar
todos los mineros. Está mal, porque los modos no agregan igual:

  - Los **standalone son redundantes entre sí**: cada uno barre ``[0, espacio)``
    desde 0 con el mismo ``partial_hash_base``, así que todos encuentran *el
    mismo* nonce (``standalone_worker.py``). Diez standalone no hacen la red diez
    veces más rápida: hacen diez veces el mismo trabajo y gana el más rápido.
  - Los **pools sí agregan**, porque el coordinador fragmenta el espacio y cada
    miembro barre un tramo distinto (``pool_coordinator/coordinator.py``).
  - Y **dos pools distintos vuelven a ser redundantes entre sí**: cada uno
    fragmenta el espacio completo por su cuenta.

Entonces la velocidad de la red es el **máximo entre buscadores independientes**
—cada pool con la suma de sus miembros, cada standalone solo—, no la suma de
todo. Quien sella la ventana es el más rápido de esa lista.

**Por qué no es gameable como el mecanismo que AGENT.md 11.2 descartaba.** Aquel
se basaba en el *comportamiento* de los actores (tiempos de resolución), y por
eso se podía manipular haciendo vencer ventanas a propósito. Éste mide el
*cómputo declarado y vivo*, que es un hecho observable, no una conducta. Quedan
dos vectores y los dos están cerrados acá:

  - **Bajar `n` apagando mineros.** Una coalición podría conectar cómputo, dejar
    que `n` suba, desconectarlo en bloque para que baje, y promulgar barato con
    su hardware real intacto. Por eso `n` **sube en el acto y baja con
    histéresis** (``DifficultyRatchet``): hay que sostener la red chica durante
    varias ventanas seguidas para que ceda.
  - **Inflar `n` para expulsar a los chicos.** Subir la dificultad no le sirve a
    un atacante —le cuesta a él también— pero sí puede dejar fuera al minero
    individual. Para eso está el techo, y para eso `n` se acota además por el
    espacio de nonces (ver ``max_n_for_space``).
"""

from __future__ import annotations

import math

# Objetivo: cuántos segundos debería tardar la red en promulgar una ley. Es la
# constante que el sistema sostiene moviendo `n`.
DIFFICULTY_TARGET_SECONDS = 30.0

# Piso y techo de `n`. El piso evita que una medición rota deje las leyes
# gratis; el techo, que las deje imposibles. El techo efectivo es el menor entre
# éste y el que permite el espacio de nonces.
N_ZEROS_MIN = 3
N_ZEROS_MAX = 9

# Cuántas ventanas seguidas hay que medir una red más chica antes de bajar `n`.
# Subir es inmediato. La asimetría es deliberada: bajar es lo único que le sirve
# a un atacante, así que es lo único que se hace despacio.
#
# El valor es un compromiso: más alto encarece el ataque de apagar cómputo en
# bloque, pero deja a la red penalizada más tiempo cuando la caída es genuina
# (mineros que se van de verdad). Con 3, una contracción real se absorbe en pocos
# minutos y el atacante igual tiene que sostener la red chica durante tres
# ventanas completas, con una sola medición alta reiniciando la racha.
DIFFICULTY_DECAY_WINDOWS = 3

# Probabilidad de que exista solución dentro del espacio de nonces. El número de
# intentos hasta el primer acierto es geométrico: con un espacio de k veces los
# intentos esperados, la probabilidad es 1 - e^-k.
SPACE_COVERAGE = 0.99


def expected_attempts(n_zeros: int) -> int:
    """Intentos esperados hasta el primer hash con `n_zeros` ceros hex."""
    return 16 ** n_zeros


def hashrate_of(worker: dict, hps_cpu: float, hps_gpu: float) -> float:
    """Cómputo de un minero: el medido si lo reporta, si no el estimado por recurso.

    El medido es preferible —refleja la máquina real— pero un minero recién
    conectado todavía no minó nada, y hasta que lo haga hay que suponerle algo o
    no contaría para la dificultad.
    """
    medido = worker.get("hashrate_hps")
    try:
        if medido and float(medido) > 0:
            return float(medido)
    except (TypeError, ValueError):
        pass
    base = hps_gpu if worker.get("has_gpu") else hps_cpu
    try:
        capacidad = max(1, int(worker.get("capacity", 1) or 1))
    except (TypeError, ValueError):
        capacidad = 1
    return base * capacidad


def searchers(workers: list[dict], teams: list[dict],
              hps_cpu: float, hps_gpu: float) -> list[tuple[str, float]]:
    """Buscadores independientes y su cómputo agregado, de mayor a menor.

    Un pool es un buscador (sus miembros fragmentan, así que suman). Cada
    standalone es un buscador suelto. Ver el porqué en el docstring del módulo.
    """
    vivos = {w.get("worker_id"): w for w in workers if w.get("worker_id")}
    resultado: list[tuple[str, float]] = []
    en_pool: set[str] = set()

    for equipo in teams:
        miembros = [equipo.get("coordinator_worker_id"), *equipo.get("members", [])]
        presentes = [vivos[m] for m in miembros if m in vivos]
        en_pool.update(m for m in miembros if m in vivos)
        if presentes:
            total = sum(hashrate_of(w, hps_cpu, hps_gpu) for w in presentes)
            resultado.append((f"equipo:{equipo.get('team_id')}", total))

    for wid, w in vivos.items():
        if wid not in en_pool:
            resultado.append((f"standalone:{wid}", hashrate_of(w, hps_cpu, hps_gpu)))

    return sorted(resultado, key=lambda x: -x[1])


def effective_hashrate(workers: list[dict], teams: list[dict],
                       hps_cpu: float, hps_gpu: float) -> float:
    """Velocidad de la red: la del buscador independiente más rápido."""
    lista = searchers(workers, teams, hps_cpu, hps_gpu)
    return lista[0][1] if lista else 0.0


def max_n_for_space(nonce_space: int, coverage: float = SPACE_COVERAGE) -> int:
    """Mayor `n` que el espacio de nonces puede sostener.

    Se dimensiona sobre la **derogación** (`n+1`), que es el caso caro: si el
    espacio sólo cubriera la promulgación, derogar vencería siempre y el sistema
    quedaría con leyes que no se pueden dar de baja. Es un techo duro — subir `n`
    por encima de esto rompe el sistema aunque la red tenga el cómputo.
    """
    if nonce_space <= 0:
        return N_ZEROS_MAX
    k = -math.log(1.0 - coverage)
    # k * 16^(n+1) <= espacio  ⇒  n <= log16(espacio / k) - 1
    return max(N_ZEROS_MIN, int(math.floor(math.log(nonce_space / k, 16))) - 1)


def nonce_space_for(n_zeros: int, coverage: float = SPACE_COVERAGE) -> int:
    """Espacio de nonces mínimo para que una derogación (`n+1`) tenga solución."""
    k = -math.log(1.0 - coverage)
    return int(math.ceil(k * expected_attempts(n_zeros + 1)))


def difficulty_for(hashrate: float, *,
                   target_seconds: float = DIFFICULTY_TARGET_SECONDS,
                   nonce_space: int = 0,
                   n_min: int = N_ZEROS_MIN,
                   n_max: int = N_ZEROS_MAX) -> int:
    """`n` que hace que promulgar tarde ~`target_seconds` con este cómputo.

    Redondea **hacia abajo**: pasarse de `n` encarece por 16, y es preferible una
    ventana que se sella más rápido que el objetivo a una que vence sin sellar.

    Sin cómputo medible devuelve el piso: una red vacía no justifica dificultad,
    y si además dejáramos `n` alto, el primer minero en volver no podría sellar
    nada.
    """
    techo = min(n_max, max_n_for_space(nonce_space)) if nonce_space else n_max
    if hashrate <= 0 or target_seconds <= 0:
        return n_min
    n = int(math.floor(math.log(hashrate * target_seconds, 16)))
    return max(n_min, min(n, techo))


class DifficultyRatchet:
    """Suaviza el ajuste: sube en el acto, baja sólo tras varias mediciones bajas.

    La asimetría es la defensa. Bajar `n` es lo único que le sirve a un atacante
    —promulgar barato—, y la forma barata de lograrlo es conectar cómputo y
    apagarlo de golpe. Con histéresis, esa maniobra exige sostener la red chica
    durante `decay_windows` ventanas seguidas, tiempo durante el cual el resto de
    la red puede reaccionar. Subir, en cambio, no beneficia a nadie en
    particular, así que no hay razón para demorarlo.

    **El estado se persiste** (`state()` / `restore()`, que el NCT guarda en
    Redis). Sin eso, reiniciar el NCT era una forma de saltearse la histéresis:
    el sucesor arrancaba sin memoria y adoptaba la primera medición tal cual, así
    que bastaba con provocar un failover después de apagar el cómputo para que
    `n` bajara de una. Que la ventana en curso sí se pierda en la caída
    (AGENT.md 4) no aplica acá: la dificultad no es estado de *una* ventana sino
    del sistema, y debe sobrevivir a quién la esté coordinando.
    """

    def __init__(self, decay_windows: int = DIFFICULTY_DECAY_WINDOWS):
        self.decay_windows = decay_windows
        self.current: int | None = None
        self._low_streak = 0

    def state(self) -> dict:
        """Estado serializable, para que sobreviva al failover del NCT."""
        return {"current": self.current, "low_streak": self._low_streak}

    def restore(self, state: dict | None) -> None:
        """Recupera el estado guardado. Un estado ausente o corrupto se ignora.

        Ignorar en vez de fallar es deliberado: si la persistencia se rompe, el
        mecanismo degrada a su versión en memoria —que funciona— en lugar de
        impedir que se abran ventanas.
        """
        if not state:
            return
        try:
            actual = state.get("current")
            self.current = int(actual) if actual not in (None, "") else None
            self._low_streak = int(state.get("low_streak") or 0)
        except (TypeError, ValueError):
            pass

    def update(self, measured: int) -> int:
        """Incorpora una medición y devuelve el `n` a usar en esta ventana."""
        if self.current is None:
            self.current = measured
            return self.current

        if measured > self.current:
            self.current = measured
            self._low_streak = 0
        elif measured < self.current:
            self._low_streak += 1
            if self._low_streak >= self.decay_windows:
                # Baja de a un cero por vez, no de golpe al valor medido: un
                # salto grande hacia abajo por una medición rara abarata el
                # sistema entero de una.
                self.current -= 1
                self._low_streak = 0
        else:
            self._low_streak = 0

        return self.current
