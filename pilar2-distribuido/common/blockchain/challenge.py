"""Desafío de gobierno de VoxChain y verificación de nonce.

El "string base" que se hashea no es una transacción monetaria, es el desafío
de gobierno serializado (AGENT.md 5, Pilar 1):

    base = law_id + text_hash + voting_window_id + action

Resolver el desafío = encontrar un ``nonce`` tal que ``md5(base + str(nonce))``
empiece con ``n`` ceros (promulgar) o ``n+1`` ceros (derogar).

El hashing concreto lo realiza el minero de Pilar 1 (GPU CUDA o fallback CPU);
acá sólo se reconstruye el mismo cómputo para *verificar* la solución recibida.
La función MD5 y la convención de "prefijo de n caracteres '0'" son idénticas a
las de ``pilar1-minero/cpu/src/brute_force.py`` para que el puente sea exacto.
"""

from __future__ import annotations

import hashlib
import math

ACTION_PROMULGACION = "promulgacion"
ACTION_DEROGACION = "derogacion"
VALID_ACTIONS = (ACTION_PROMULGACION, ACTION_DEROGACION)


def build_partial_hash_base(law_id: str, text_hash: str,
                            voting_window_id: str, action: str) -> str:
    """Construye el ``partial_hash_base`` del desafío (AGENT.md 7.2).

    Orden fijo y sin separadores: ``law_id + text_hash + voting_window_id +
    action``. Es el dato que el NCT publica en ``desafio_activo`` y el que el
    minero concatena con el nonce.
    """
    if action not in VALID_ACTIONS:
        raise ValueError(f"action inválida: {action!r}; esperado {VALID_ACTIONS}")
    return f"{law_id}{text_hash}{voting_window_id}{action}"


def n_zeros_for_action(n: int, action: str) -> int:
    """Dificultad fija (AGENT.md 3.6): ``n`` para promulgar, ``n+1`` para derogar.

    Prohibido el ajuste dinámico por carga de red (AGENT.md 10): ``n`` es un
    parámetro de configuración, no una variable de runtime.
    """
    if action == ACTION_PROMULGACION:
        return n
    if action == ACTION_DEROGACION:
        return n + 1
    raise ValueError(f"action inválida: {action!r}")


def prefix_for_zeros(n_zeros: int) -> str:
    """Puente desafío → minero: ``n`` ceros ⇒ prefijo de ``n`` caracteres '0'."""
    if n_zeros < 0:
        raise ValueError("n_zeros no puede ser negativo")
    return "0" * n_zeros


def compute_hash(partial_hash_base: str, nonce: int) -> str:
    """MD5 hex de ``partial_hash_base + str(nonce)`` (misma convención que el minero)."""
    return hashlib.md5(f"{partial_hash_base}{nonce}".encode()).hexdigest()


def verify_nonce(partial_hash_base: str, nonce: int, n_zeros_required: int):
    """Verifica un nonce contra el desafío.

    Devuelve ``(ok: bool, hash_hex: str)``. ``ok`` es True si el hash empieza
    con exactamente ``n_zeros_required`` caracteres '0'. El hash siempre se
    devuelve para poder sellarlo/loguearlo aunque no sea válido.
    """
    hash_hex = compute_hash(partial_hash_base, nonce)
    ok = hash_hex.startswith(prefix_for_zeros(n_zeros_required))
    return ok, hash_hex


def espacio_insuficiente(n_zeros: int, nonce_space: int,
                         cobertura: float = 0.95) -> str | None:
    """Avisa si ``nonce_space`` es chico para la dificultad; ``None`` si está bien.

    ``n`` no es una perilla suelta: el espacio de nonces tiene que alcanzar para
    que exista solución dentro del rango que el minero barre. Se dimensiona sobre
    la **derogación** (``n+1``), que es el caso caro; si sólo cubriera la
    promulgación, derogar vencería siempre.

    El número de intentos hasta el primer acierto es geométrico, así que con un
    espacio de ``k`` veces los intentos esperados la probabilidad de encontrarlo
    es ``1 - e^-k``.

    Existe porque el modo de fallo es mudo: con ``N_ZEROS`` alto y el espacio
    viejo, el sistema arranca sin quejarse, las ventanas vencen sin sellar, y en
    los logs parece falta de mineros. El NCT lo consulta al arrancar y antes de
    abrir cada ventana, porque la config puede cambiar con el sistema andando.
    """
    esperados = 16 ** (n_zeros + 1)
    prob = 1.0 - math.exp(-nonce_space / esperados) if esperados else 1.0
    if prob >= cobertura:
        return None
    k = -math.log(1.0 - cobertura)
    return (f"NONCE_SPACE={nonce_space:,} es chico para n={n_zeros}: una derogación "
            f"(n+1={n_zeros + 1}) espera {esperados:,} intentos y sólo se encontraría "
            f"el {prob:.1%} de las veces. Hace falta {math.ceil(k * esperados):,} "
            f"para {cobertura:.0%}. Recalculalo con "
            f"Ver la tabla de calibración en pilar3-despliegue/README.md")


def solve_mini_challenge(seed: str, n_zeros: int, max_iter: int = 10_000_000) -> int | None:
    """Encuentra el primer nonce tal que hash(seed+nonce) tenga ``n_zeros`` ceros.

    Compartido por el bully del NCT y la elección del pool coordinator para que
    ambos usen exactamente el mismo algoritmo de PoW.
    """
    prefix = prefix_for_zeros(n_zeros)
    for nonce in range(max_iter):
        if compute_hash(seed, nonce).startswith(prefix):
            return nonce
    return None
