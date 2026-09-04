"""Equipos de minado (pools nombrados) sobre Redis.

Un **equipo** es la capa de nombres que falta entre el usuario y los modos del
worker. Por debajo no hay nada nuevo: el coordinador del equipo es un worker en
modo ``pool-coordinator`` y los miembros son workers en modo ``pool-worker``
apuntando a su HTTP. Lo que agrega esta capa es que la dirección del coordinador
la resuelve el sistema —el worker la anuncia en su estado— en vez de que el
usuario escriba ``http://algo:9001`` a mano.

Esquema de claves:

- ``teams``                    set de ``team_id``
- ``team:<team_id>``           hash con los datos del equipo
- ``team:members:<team_id>``   set de ``worker_id`` de los miembros (sin el coordinador)
- ``worker:team:<worker_id>``  índice inverso: en qué equipo está cada worker
- ``team:owner:<owner>``       índice inverso: el (único) equipo que fundó cada identidad

El hash del equipo guarda su **agenda** (``categories``) como lista separada por
comas, porque un hash de Redis sólo almacena strings. Vacío significa "vota
todas las categorías", que es el pool clásico y por eso es el default.

Los dos índices inversos existen por la misma razón: las preguntas que más se
hacen son "¿este worker está en algún equipo?" y "¿esta identidad ya fundó
uno?", y sin ellos habría que recorrer todos los equipos en cada fila de la
tabla de mineros y en cada alta.

Se asume un cliente Redis con ``decode_responses=True``.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Optional

from common.blockchain import parse_categories

_NO_SLUG = re.compile(r"[^a-z0-9]+")

# Un equipo cuyo coordinador no reporta estado hace este tiempo se considera
# huérfano. Coincide con el TTL con el que el worker escribe worker:status:*
# (15 s), más un margen para no marcar como caído a alguien que justo estaba
# entre dos reportes.
COORDINATOR_STALE_SECONDS = 30


class TeamError(Exception):
    """Error de dominio de equipos; el router lo traduce a un HTTP 4xx."""

    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def slugify_team(name: str) -> str:
    """``team_id`` legible y único a partir del nombre elegido por el usuario.

    El nombre lo escribe una persona y puede traer acentos, espacios o
    mayúsculas; el ``team_id`` viaja en URLs y en claves de Redis, así que se
    normaliza. Se le agrega un sufijo aleatorio corto porque dos equipos pueden
    llamarse igual sin que eso sea un error: el nombre es de la persona, el id
    es del sistema.
    """
    slug = _NO_SLUG.sub("-", name.strip().lower()).strip("-")[:32].strip("-")
    if not slug:
        slug = "equipo"
    return f"{slug}-{uuid.uuid4().hex[:6]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class TeamsStore:
    """Fachada de persistencia de equipos. Recibe un cliente Redis."""

    def __init__(self, client):
        self.r = client

    # ---- lectura ---------------------------------------------------------
    def get_team(self, team_id: str) -> Optional[dict]:
        data = self.r.hgetall(f"team:{team_id}")
        if not data:
            return None
        data["members"] = self.member_ids(team_id)
        # En Redis la agenda es un string separado por comas; hacia afuera es
        # siempre una lista normalizada, para que nadie tenga que acordarse de
        # partirla (y de que "" no es lo mismo que [""]).
        data["categories"] = parse_categories(data.get("categories"))
        return data

    def member_ids(self, team_id: str) -> list[str]:
        return sorted(self.r.smembers(f"team:members:{team_id}"))

    def list_teams(self) -> list[dict]:
        teams = []
        for team_id in sorted(self.r.smembers("teams")):
            team = self.get_team(team_id)
            if team:
                teams.append(team)
            else:
                # El hash se fue pero el id quedó en el set: limpiamos en vez de
                # devolver un equipo fantasma que la UI no puede abrir.
                self.r.srem("teams", team_id)
        return teams

    def team_of_worker(self, worker_id: str) -> Optional[str]:
        return self.r.get(f"worker:team:{worker_id}")

    def team_of_owner(self, owner: str) -> Optional[str]:
        """El equipo que fundó esta identidad, si fundó alguno.

        Una identidad funda **un** equipo como máximo (ver ``create_team``). El
        índice se limpia solo si el equipo dejó de existir: puede quedar
        colgado si alguien borró el hash por fuera, y en ese caso preferimos
        dejar fundar de nuevo antes que bloquear a una persona para siempre.
        """
        team_id = self.r.get(f"team:owner:{owner}")
        if not team_id:
            return None
        if not self.r.exists(f"team:{team_id}"):
            self.r.delete(f"team:owner:{owner}")
            return None
        return team_id

    def role_of_worker(self, worker_id: str) -> Optional[str]:
        """``"coordinator"``, ``"member"`` o ``None`` si no está en un equipo."""
        team_id = self.team_of_worker(worker_id)
        if not team_id:
            return None
        team = self.r.hgetall(f"team:{team_id}")
        if not team:
            return None
        if team.get("coordinator_worker_id") == worker_id:
            return "coordinator"
        return "member"

    # ---- escritura -------------------------------------------------------
    def create_team(self, *, name: str, owner: str, coordinator_worker_id: str,
                    coordinator_url: str, categories=None) -> dict:
        """Crea el equipo y deja al worker indicado como su coordinador.

        **Una identidad funda un solo equipo.** No es una limitación técnica: es
        la regla del dominio. Un equipo es una facción política que concentra
        poder de cómputo (AGENT.md 3.9), y dejar que una misma clave funde
        varios le daría a un solo individuo tantos frentes como quisiera armar,
        agravando gratis la concentración de poder que el sistema ya documenta
        como su debilidad. Unirse al equipo de otro con más de un minero sí se
        puede: lo que se limita es fundar, no participar.

        ``categories`` es la agenda del equipo (AGENT.md 3.10): las áreas de ley
        a cuyas ventanas va a aportar cómputo. Sin agenda vota todas.
        """
        already = self.team_of_owner(owner)
        if already:
            existing_name = self.r.hget(f"team:{already}", "name") or already
            raise TeamError(
                f"Ya fundaste el equipo '{existing_name}'. Cada identidad puede "
                "fundar uno solo: disolvelo si querés armar otro.",
                status_code=409,
            )
        existing = self.team_of_worker(coordinator_worker_id)
        if existing:
            raise TeamError(
                f"El minero '{coordinator_worker_id}' ya pertenece a un equipo. "
                "Salí de ese equipo antes de crear uno nuevo.",
                status_code=409,
            )
        team_id = slugify_team(name)
        agenda = parse_categories(categories)
        team = {
            "team_id": team_id,
            "name": name.strip(),
            "owner": owner,
            "coordinator_worker_id": coordinator_worker_id,
            "coordinator_url": coordinator_url,
            "categories": ",".join(agenda),
            "created_at": _now_iso(),
        }
        pipe = self.r.pipeline()
        pipe.hset(f"team:{team_id}", mapping=team)
        pipe.sadd("teams", team_id)
        pipe.set(f"worker:team:{coordinator_worker_id}", team_id)
        pipe.set(f"team:owner:{owner}", team_id)
        pipe.execute()
        team["members"] = []
        team["categories"] = agenda
        return team

    def set_coordinator_url(self, team_id: str, coordinator_url: str) -> None:
        """Refresca la dirección del coordinador.

        La dirección puede cambiar sin que el equipo cambie: en Kubernetes basta
        con que el pod se reinicie para que le toque otra IP. Se re-escribe cada
        vez que se lee el estado del coordinador, así los que se unan después
        reciben la buena.
        """
        self.r.hset(f"team:{team_id}", "coordinator_url", coordinator_url)

    def set_categories(self, team_id: str, categories) -> list[str]:
        """Cambia la agenda del equipo. Devuelve la lista normalizada.

        Sólo toca el estado del equipo: **empujarla al coordinador es del
        llamador** (``push_voting_policy``). Están separadas porque el store no
        habla con RabbitMQ ni con Redis de workers, pero la separación es
        peligrosa: una agenda guardada que no llegó al coordinador es un equipo
        que dice votar economía y sigue minando todo. El router hace las dos
        cosas en la misma operación por eso.
        """
        if not self.r.exists(f"team:{team_id}"):
            raise TeamError("El equipo no existe", status_code=404)
        agenda = parse_categories(categories)
        self.r.hset(f"team:{team_id}", "categories", ",".join(agenda))
        return agenda

    def join_team(self, team_id: str, worker_id: str) -> dict:
        team = self.get_team(team_id)
        if not team:
            raise TeamError("El equipo no existe", status_code=404)
        current = self.team_of_worker(worker_id)
        if current == team_id:
            raise TeamError(f"El minero '{worker_id}' ya está en este equipo",
                            status_code=409)
        if current:
            raise TeamError(
                f"El minero '{worker_id}' ya está en otro equipo. "
                "Salí de ese equipo antes de unirte a este.",
                status_code=409,
            )
        pipe = self.r.pipeline()
        pipe.sadd(f"team:members:{team_id}", worker_id)
        pipe.set(f"worker:team:{worker_id}", team_id)
        pipe.execute()
        return self.get_team(team_id)

    def leave_team(self, worker_id: str) -> tuple[str, str]:
        """Saca a un miembro de su equipo. Devuelve ``(team_id, rol)``.

        No acepta al coordinador: un equipo sin coordinador no es un equipo, así
        que la salida del coordinador es una disolución y tiene su propio camino
        (``dissolve_team``) para que el llamador devuelva a todos los miembros a
        modo competitivo en lugar de dejarlos apuntando a un HTTP muerto.
        """
        team_id = self.team_of_worker(worker_id)
        if not team_id:
            raise TeamError(f"El minero '{worker_id}' no está en ningún equipo",
                            status_code=404)
        role = self.role_of_worker(worker_id)
        if role == "coordinator":
            raise TeamError(
                "El coordinador no puede abandonar su propio equipo: disolvelo.",
                status_code=409,
            )
        pipe = self.r.pipeline()
        pipe.srem(f"team:members:{team_id}", worker_id)
        pipe.delete(f"worker:team:{worker_id}")
        pipe.execute()
        return team_id, role or "member"

    def dissolve_team(self, team_id: str) -> list[str]:
        """Borra el equipo. Devuelve los ``worker_id`` que quedaron liberados.

        El llamador es responsable de devolver esos workers a modo competitivo:
        acá sólo se borra el estado, y dejar workers apuntando por HTTP a un
        coordinador que ya no reparte trabajo los dejaría girando en vacío.
        """
        team = self.get_team(team_id)
        if not team:
            raise TeamError("El equipo no existe", status_code=404)
        affected = [team["coordinator_worker_id"], *team["members"]]
        pipe = self.r.pipeline()
        for worker_id in affected:
            pipe.delete(f"worker:team:{worker_id}")
        pipe.delete(f"team:members:{team_id}")
        pipe.delete(f"team:{team_id}")
        # Libera a quien lo fundó para que pueda armar otro.
        pipe.delete(f"team:owner:{team.get('owner', '')}")
        pipe.srem("teams", team_id)
        pipe.execute()
        return affected

    def detach_worker(self, worker_id: str) -> Optional[tuple[str, str]]:
        """Desvincula un worker de su equipo sin validar el rol.

        Lo usa el camino "volver a competitivo" de la página de mineros: si el
        worker era coordinador esto **no** alcanza (hay que disolver), y por eso
        devuelve el rol para que el llamador decida. Devuelve ``None`` si el
        worker no estaba en ningún equipo.
        """
        team_id = self.team_of_worker(worker_id)
        if not team_id:
            return None
        role = self.role_of_worker(worker_id) or "member"
        if role == "member":
            pipe = self.r.pipeline()
            pipe.srem(f"team:members:{team_id}", worker_id)
            pipe.delete(f"worker:team:{worker_id}")
            pipe.execute()
        return team_id, role
