"""Deliberación: la pausa en la que los equipos deciden si aportan cómputo.

**Qué resuelve.** El PoW sólo cuenta a favor: una ley sale si alguien la mina, y
quedarse afuera no pesaba nada salvo que nadie más pudiera minarla. En la
práctica se promulgaba casi todo. La deliberación le da peso al "no"
(AGENT.md 3.12):

1. Cuando una ley llega a su turno **no se abre la ventana**: se anuncia sólo
   la ley y su área, y los convocados —los equipos y standalone cuya agenda
   cubre esa área— tienen ``DELIBERATION_SECONDS`` para decidir si aportan.
2. La dificultad se **congela al anunciar**, calculada sobre el convocado más
   grande (``biggest``). Si ese equipo después se baja, la ley sale igual con su
   dificultad, y los que quedan tienen que resolverla con menos cómputo.
3. El plazo de la ventana también se congela: ``factor`` veces lo que tardaría
   en promedio el más grande (``window_seconds_for``). Así el veto pesa lo
   mismo sea cual sea `n`, que se mueve en saltos de ×16.
4. Al vencer la pausa (``resolve``): con algún "sí" se abre la ventana sólo
   para quienes aceptaron; sin ningún "sí" pero con algún "no" la ley se
   descarta; si nadie respondió, vuelve a la cola.
5. Un convocado puede dejar declarada una **respuesta por defecto**: lo que
   vale si no responde durante la pausa. No es una excepción a "quien no
   responde no vota": es una respuesta dada de antemano, y la de la pausa la
   pisa. Sirve para que un equipo no tenga que estar conectado en cada ley.
6. Una ley que pasa ``MAX_SILENT_DELIBERATIONS`` pausas seguidas sin ninguna
   respuesta se descarta **sin penalidad** (``OUTCOME_UNANSWERED``): no fue
   rechazada, nadie estaba. Sin este límite se reanunciaría para siempre.

**Quién no responde, no vota.** El desafío viaja con la lista de participantes
y cada minero se fija si está en ella (``participates``). No es una convención
que el minero pueda ignorar: quien no aceptó tampoco sabe el desafío hasta que
se abre, y ahí ya está en juego contra el plazo congelado.

**Qué se anuncia y qué no.** Durante la pausa se publica la ley y su área, nunca
el ``voting_window_id`` ni el ``partial_hash_base``: con ellos se podría empezar
a minar antes de que la ventana abra. Por eso el id de ventana lleva además una
parte aleatoria — el contador de ventanas es público.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

from .availability import (
    _team_policy,
    deliberates,
    policy_convokes,
    prevetoed,
)
from .categories import normalize_category
from .difficulty import hashrate_of

#: Cuánto dura la pausa entre anunciar la ley y abrir su ventana.
DELIBERATION_SECONDS = 120

#: Cuántas veces el tiempo esperado del convocado más grande dura la ventana.
#: Con 2, él sella en ~86% de los casos; uno con 1/4 de su cómputo, en ~39%; y
#: uno con 1/10, en ~18% (P = 1 - e^(-factor·fracción)).
WINDOW_DEADLINE_FACTOR = 2.0

#: Piso del plazo congelado. Con `n` chico el tiempo esperado es de segundos, y
#: la latencia de RabbitMQ y el reparto de fragmentos del pool no son
#: despreciables contra eso.
WINDOW_MIN_SECONDS = 20

ACCEPT = "accept"
REJECT = "reject"
DECISIONS = (ACCEPT, REJECT)

#: Pausas seguidas sin ninguna respuesta antes de dar a la ley por abandonada.
MAX_SILENT_DELIBERATIONS = 3

OUTCOME_OPEN = "open"
OUTCOME_DISCARD = "discard"
OUTCOME_REQUEUE = "requeue"
#: Descartada tras ``MAX_SILENT_DELIBERATIONS`` pausas sin respuesta. A
#: diferencia de ``OUTCOME_DISCARD`` no anota el texto como descartado:
#: reproponerla no paga el cooldown largo, porque nadie la juzgó.
OUTCOME_UNANSWERED = "unanswered"

KIND_TEAM = "equipo"
KIND_STANDALONE = "standalone"


@dataclass(frozen=True)
class Convocado:
    """Una unidad que decide: un equipo entero o un standalone.

    ``voter_id`` es el id del worker que recibe el desafío y decide minar: el
    coordinador del equipo o el propio standalone. Es lo que viaja en la lista
    de participantes, así que cada minero se reconoce con su propio id.
    """

    voter_id: str
    kind: str
    name: str
    owner: str
    hashrate: float
    #: Ya había vetado esta ley puntual antes de que saliera: cuenta como "no".
    prevetoed: bool = False
    team_id: str = ""
    #: Lo que vale si no responde durante la pausa: ``accept``, ``reject`` o
    #: vacío (sin respuesta, no mina).
    default_decision: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Convocado":
        return cls(voter_id=str(data.get("voter_id", "")),
                   kind=str(data.get("kind", "")),
                   name=str(data.get("name", "")),
                   owner=str(data.get("owner", "")),
                   hashrate=float(data.get("hashrate") or 0.0),
                   prevetoed=bool(data.get("prevetoed")),
                   team_id=str(data.get("team_id", "")),
                   default_decision=normalize_decision(data.get("default_decision")))


def normalize_decision(value) -> str:
    """Una respuesta por defecto válida, o vacío. Lo desconocido no inventa votos."""
    return value if value in DECISIONS else ""


def convocados(workers: list[dict], teams: list[dict], challenge: dict, *,
               hps_cpu: float, hps_gpu: float) -> list[Convocado]:
    """A quiénes se convoca a decidir sobre esta ley, de mayor a menor cómputo.

    Mismo modelo de buscadores que la dificultad (``difficulty.searchers``): un
    equipo suma a sus miembros vivos y decide como uno; cada standalone decide
    solo. Se filtra con ``policy_convokes``: la agenda y el veto a una acción
    entera excluyen, el veto a esta ley puntual no (queda como respuesta
    anticipada, ``prevetoed``).
    """
    desafio = dict(challenge)
    desafio["category"] = normalize_category(desafio.get("category"))
    law_id = desafio.get("law_id")
    vivos = {w.get("worker_id"): w for w in workers if w.get("worker_id")}
    resultado: list[Convocado] = []
    en_equipo: set[str] = set()

    for equipo in teams:
        coordinador = equipo.get("coordinator_worker_id") or ""
        miembros = [coordinador, *equipo.get("members", [])]
        presentes = [vivos[m] for m in miembros if m in vivos]
        en_equipo.update(m for m in miembros if m in vivos)
        if not presentes or not coordinador:
            continue
        politica = _team_policy(equipo)
        if not policy_convokes(politica, desafio):
            continue
        resultado.append(Convocado(
            voter_id=coordinador, kind=KIND_TEAM,
            name=equipo.get("name") or equipo.get("team_id") or coordinador,
            owner=equipo.get("owner") or "",
            hashrate=sum(hashrate_of(w, hps_cpu, hps_gpu) for w in presentes),
            prevetoed=prevetoed(politica, law_id),
            team_id=equipo.get("team_id") or "",
            default_decision=normalize_decision(equipo.get("default_decision"))))

    for wid, worker in vivos.items():
        if wid in en_equipo or not deliberates(worker):
            continue
        politica = {"categories": worker.get("categories"),
                    "rejected_actions": worker.get("rejected_actions")}
        if not policy_convokes(politica, desafio):
            continue
        resultado.append(Convocado(
            voter_id=wid, kind=KIND_STANDALONE, name=wid,
            owner=worker.get("owner") or "",
            hashrate=hashrate_of(worker, hps_cpu, hps_gpu),
            default_decision=normalize_decision(worker.get("default_decision"))))

    return sorted(resultado, key=lambda c: (-c.hashrate, c.voter_id))


def biggest(lista: list[Convocado]) -> float:
    """Cómputo del convocado más grande: sobre él se congela la dificultad."""
    return max((c.hashrate for c in lista), default=0.0)


def window_seconds_for(n_zeros_required: int, hashrate: float, *,
                       factor: float = WINDOW_DEADLINE_FACTOR,
                       minimum: float = WINDOW_MIN_SECONDS,
                       maximum: float) -> int:
    """Plazo de la ventana: ``factor`` × lo que tarda en promedio el más grande.

    Acotado a ``[minimum, maximum]``: el máximo es el plazo de config de la
    acción, que sigue siendo el techo de siempre. Sin cómputo medible (o con
    ``factor`` apagado) devuelve el máximo, que es el comportamiento previo.
    """
    if hashrate <= 0 or factor <= 0:
        return int(maximum)
    esperado = (16 ** n_zeros_required) / hashrate
    segundos = math.ceil(factor * esperado)
    return int(max(minimum, min(segundos, maximum)))


def effective_decisions(voter_ids: list[str], decisions: dict,
                        defaults: dict | None = None) -> dict:
    """Respuestas que cuentan: las dadas en la pausa y, para el resto, la por defecto.

    La de la pausa siempre gana: la por defecto es lo que el convocado dijo de
    antemano, y la de ahora es más reciente.
    """
    efectivas = {v: decisions[v] for v in voter_ids if decisions.get(v) in DECISIONS}
    for v in voter_ids:
        por_defecto = (defaults or {}).get(v)
        if v not in efectivas and por_defecto in DECISIONS:
            efectivas[v] = por_defecto
    return efectivas


def resolve(voter_ids: list[str], decisions: dict,
            defaults: dict | None = None) -> tuple[str, list[str]]:
    """Qué pasa con la ley al terminar la pausa, y quiénes minan.

    - Algún "sí" → se abre la ventana, sólo para los que aceptaron.
    - Ningún "sí" y algún "no" → se descarta: el único voto emitido fue en
      contra, y quien no respondió no vota.
    - Nadie respondió → vuelve a la cola: no hubo decisión, así que no se puede
      leer como un rechazo.

    ``defaults`` son las respuestas por defecto: valen para quien no respondió
    en la pausa (``effective_decisions``). Sólo cuentan las respuestas de
    convocados: una decisión de alguien que no estaba convocado no suma ni resta.
    """
    decisions = effective_decisions(voter_ids, decisions, defaults)
    aceptaron = [v for v in voter_ids if decisions.get(v) == ACCEPT]
    if aceptaron:
        return OUTCOME_OPEN, aceptaron
    if any(decisions.get(v) == REJECT for v in voter_ids):
        return OUTCOME_DISCARD, []
    return OUTCOME_REQUEUE, []


def all_answered(voter_ids: list[str], decisions: dict) -> bool:
    """¿Respondieron todos? Entonces no hace falta esperar al final de la pausa.

    Cuentan sólo las respuestas dadas, no las por defecto: la por defecto es
    "si no respondo", y cerrar antes le quitaría al convocado la pausa para
    cambiarla. Con cero convocados es False a propósito: si no, la pausa se resolvería en
    el acto, la ley volvería a la cola y se volvería a anunciar en el mismo
    tick, en un bucle sin fin.
    """
    return bool(voter_ids) and all(decisions.get(v) in DECISIONS for v in voter_ids)


def participates(challenge: dict, voter_id: str) -> bool:
    """¿Este minero decide minar este desafío?

    Un desafío sin lista de participantes es de antes de la deliberación (o
    con ella apagada): lo mina cualquiera, como siempre.
    """
    participantes = challenge.get("participants")
    if participantes is None:
        return True
    return voter_id in participantes
