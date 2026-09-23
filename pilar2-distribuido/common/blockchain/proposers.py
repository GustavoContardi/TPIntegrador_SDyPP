"""Quién puede proponer una ley (AGENT.md 3.2).

Proponer —promulgar o derogar— queda reservado a quien **responde por cómputo
en la red**: el fundador de un equipo y el dueño de un minero standalone. Son
exactamente los que responden en la deliberación (3.12), y no es casualidad: la
ley la trae a la mesa quien después va a decidir si la mina. Un ciudadano sin
minero no tiene nada que aportar a la ventana que abriría, y un miembro de
equipo ya delegó su voz en el fundador — si pudiera proponer por su cuenta, el
equipo dejaría de "decidir como uno".

La regla **no cierra Sybil** (AGENT.md 9): registrar un minero sigue costando
sólo una identidad nueva. Lo que cambia es que proponer exige además tener un
minero dado de alta, y que un equipo con muchos miembros tiene una sola voz
para proponer, no una por cabeza.

Acá vive sólo la decisión, sobre datos ya leídos. Juntar esos datos de Redis es
de ``VoxChainStore.proposer_standing``, que usan tanto el API (para responder
un 403 con el motivo) como el NCT (verificación autoritativa, igual que con la
firma: un mensaje puede llegar a ``propuestas`` sin pasar por el API).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

#: Fundó un equipo: propone por él.
STANDING_TEAM_OWNER = "team_owner"
#: Tiene un minero que corre solo: propone por sí mismo.
STANDING_STANDALONE = "standalone"

_MODE_STANDALONE = "standalone"
_MODE_POOL_COORDINATOR = "pool-coordinator"


@dataclass(frozen=True)
class ProposerStanding:
    """Veredicto sobre una identidad: si puede proponer y en calidad de qué."""

    #: ``STANDING_TEAM_OWNER``, ``STANDING_STANDALONE`` o ``None``.
    role: Optional[str]
    #: Por qué no puede, en una línea apta para log y para UI. Vacío si puede.
    reason: str = ""

    @property
    def allowed(self) -> bool:
        return self.role is not None


def assess_proposer(*, founded_team: bool, worker_id: Optional[str],
                    worker_team: Optional[str],
                    worker_mode: Optional[str]) -> ProposerStanding:
    """Decide si una identidad puede proponer.

    - ``founded_team``: la identidad fundó un equipo que sigue existiendo.
    - ``worker_id``: el minero que registró (cada identidad tiene uno como
      máximo), o ``None``.
    - ``worker_team``: el equipo en el que está ese minero, o ``None``.
    - ``worker_mode``: el modo con el que el minero reporta estado. Vacío si no
      está vivo: un minero fuera de todo equipo es standalone aunque esté
      apagado, porque que no esté minando ahora no le quita a su dueño el lugar
      en la red — eso ya lo cubre el quórum (3.11), que no abre la ventana.

    El fundador va primero: su propio minero es el coordinador del equipo y
    figura *en* un equipo, así que mirando sólo el minero se lo leería como un
    miembro más.

    Un ``pool-coordinator`` sin equipo registrado cuenta como dueño de equipo:
    coordina un pool y responde por él. Pasa con un pool que se arma por
    despliegue y no por ``/api/teams``; con equipos reales el coordinador
    siempre tiene su registro y entra por ``founded_team``.
    """
    if founded_team:
        return ProposerStanding(STANDING_TEAM_OWNER)
    if not worker_id:
        return ProposerStanding(
            None,
            "Para proponer leyes necesitás un minero propio que mine por su "
            "cuenta, o fundar un equipo. Registrá un minero para poder "
            "proponer.")
    if worker_team:
        return ProposerStanding(
            None,
            f"Tu minero '{worker_id}' es parte del equipo '{worker_team}', y en "
            "un equipo las leyes las propone quien lo fundó. Si querés proponer "
            "vos, sacá tu minero del equipo.")
    mode = worker_mode or _MODE_STANDALONE
    if mode == _MODE_STANDALONE:
        return ProposerStanding(STANDING_STANDALONE)
    if mode == _MODE_POOL_COORDINATOR:
        return ProposerStanding(STANDING_TEAM_OWNER)
    return ProposerStanding(
        None,
        f"Tu minero '{worker_id}' mina dentro de un equipo: las leyes las "
        "propone quien lo lidera.")
