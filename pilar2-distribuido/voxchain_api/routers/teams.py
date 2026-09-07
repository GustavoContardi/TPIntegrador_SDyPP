"""Equipos de minado: crear un pool, unirse al de otro, salir, disolver.

Es la cara de usuario del modo cooperativo. Un equipo no es un servicio nuevo:
es un ``pool-coordinator`` con nombre, más la lista de quiénes se le unieron. Lo
que aporta este router es que la dirección del coordinador la resuelve el
sistema leyendo el estado que el propio worker publica, en vez de que el usuario
tenga que averiguar y escribir ``http://algo:9001``.

Regla de oro del diseño: **el estado del equipo y el modo real del worker se
mueven siempre juntos**. Toda alta o baja de un equipo despacha el `switch_mode`
correspondiente en la misma operación, y el camino inverso (volver a competitivo
desde la página de mineros) desarma la membresía. Si se pudiera cambiar el modo
por un lado y la membresía por otro, la lista de miembros mentiría.

Lo mismo vale para la **agenda temática** (AGENT.md 3.10): las categorías de ley
que el equipo vota se guardan en el equipo, pero quien decide si se mina una
ventana es el coordinador, que corre en otra máquina. Toda escritura de agenda
baja en el acto a ``pool:policy:<coordinador>`` en la misma operación — una
agenda guardada que no llegó al coordinador es un equipo que dice votar economía
y sigue minando todo.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from common.blockchain import parse_categories, validate_category
from voxchain_api.models import (
    CreateTeamRequest,
    Team,
    TeamCategoriesRequest,
    TeamMember,
    TeamMembershipRequest,
)
from voxchain_api.routers.workers import (
    get_owner_id,
    get_rabbitmq_publisher,
    get_redis_reader,
    persist_worker_registration,
    verify_worker_ownership,
)
from voxchain_api.services.redis_reader import RedisReader
from voxchain_api.services.teams_store import TeamError, TeamsStore
from voxchain_api.services.worker_control import (
    clear_voting_policy,
    coordinator_address,
    dispatch_switch_mode,
    push_voting_policy,
    read_worker_status,
    refresh_desired_pool_url,
)

router = APIRouter(prefix="/api/teams", tags=["teams"])

log = logging.getLogger("voxchain.api.teams")


def get_teams_store(redis: RedisReader = Depends(get_redis_reader)) -> TeamsStore:
    return TeamsStore(redis.store.r)


def _as_http(exc: TeamError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=str(exc))


def _valid_agenda(categories) -> list[str]:
    """Valida la agenda pedida y la devuelve normalizada.

    ``parse_categories`` es tolerante (descarta lo que no conoce) porque lee
    estado viejo; acá, en cambio, el usuario está eligiendo, y tragarse en
    silencio una categoría inexistente le dejaría un equipo que no vota lo que
    creyó elegir. Por eso se valida una por una antes de normalizar.
    """
    try:
        for category in categories or []:
            validate_category(category)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return parse_categories(categories)


def _roster(store: TeamsStore, redis_client, team: dict) -> list[TeamMember]:
    """Plantel completo del equipo: coordinador primero, después los miembros.

    El estado (modo, si corre) sale de lo que cada worker reporta, no de lo que
    el equipo cree: si alguien se cayó, se ve acá antes que en ningún lado.
    """
    roster: list[TeamMember] = []
    ids = [(team["coordinator_worker_id"], "coordinator")]
    ids += [(worker_id, "member") for worker_id in team.get("members", [])]
    for worker_id, role in ids:
        status = read_worker_status(redis_client, worker_id) or {}
        roster.append(TeamMember(
            worker_id=worker_id,
            role=role,
            mode=status.get("mode", "unknown"),
            running=bool(status.get("running", False)),
            pubkey=status.get("pubkey"),
        ))
    return roster


def _hydrate(store: TeamsStore, redis_client, team: dict) -> Team:
    """Completa un equipo guardado con lo que se sabe de él ahora mismo."""
    coordinator_id = team["coordinator_worker_id"]
    status = read_worker_status(redis_client, coordinator_id)
    coordinator_online = bool(status and status.get("running"))

    coordinator_url = team.get("coordinator_url", "")
    if status:
        # La dirección del coordinador puede cambiar sola: en Kubernetes alcanza
        # con que el pod se reinicie para que le toque otra IP, y un coordinador
        # que se registró antes de arrancar recién la publica ahora. Se
        # re-escribe en cada lectura para que quien se una después reciba la
        # vigente...
        fresh = coordinator_address(status, coordinator_id)
        if fresh and fresh != coordinator_url:
            store.set_coordinator_url(team["team_id"], fresh)
            coordinator_url = fresh
            # ...y se corrige la de los que ya estaban adentro, que si no
            # seguirían pidiéndole fragmentos a una dirección muerta. No hace
            # falta re-despachar por RabbitMQ: cada minero reconcilia su modo
            # contra este registro.
            for member_id in team.get("members", []):
                refresh_desired_pool_url(redis_client, member_id, fresh)

    miners_connected = None
    try:
        import json

        raw = redis_client.get(f"pool:health:{coordinator_id}")
        if raw:
            miners_connected = json.loads(
                raw.decode("utf-8") if isinstance(raw, bytes) else raw
            ).get("miners")
    except Exception:  # noqa: BLE001
        log.debug("sin pool:health para %s", coordinator_id, exc_info=True)

    roster = _roster(store, redis_client, team)
    return Team(
        team_id=team["team_id"],
        name=team.get("name", team["team_id"]),
        owner=team.get("owner", ""),
        categories=parse_categories(team.get("categories")),
        coordinator_worker_id=coordinator_id,
        coordinator_url=coordinator_url,
        created_at=team.get("created_at", ""),
        members=roster,
        member_count=len(roster),
        miners_connected=miners_connected,
        coordinator_online=coordinator_online,
    )


@router.get("", response_model=list[Team])
async def list_teams(store: TeamsStore = Depends(get_teams_store),
                     redis: RedisReader = Depends(get_redis_reader)):
    """Todos los equipos de la red, con su plantel y el estado del coordinador."""
    return [_hydrate(store, redis.store.r, team) for team in store.list_teams()]


@router.get("/{team_id}", response_model=Team)
async def get_team(team_id: str, store: TeamsStore = Depends(get_teams_store),
                   redis: RedisReader = Depends(get_redis_reader)):
    team = store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="El equipo no existe")
    return _hydrate(store, redis.store.r, team)


@router.post("", response_model=Team)
async def create_team(
    request: CreateTeamRequest,
    owner_id: str = Depends(get_owner_id),
    store: TeamsStore = Depends(get_teams_store),
    redis: RedisReader = Depends(get_redis_reader),
    publisher=Depends(get_rabbitmq_publisher),
):
    """Crea un equipo y promueve un minero propio a coordinador.

    Si viene ``new_worker``, el minero se da de alta en el mismo paso: es el caso
    del usuario que se registró recién y todavía no tiene ninguno. Sin eso, el
    formulario sería un callejón sin salida — "creá un equipo" contra "no tenés
    con qué".
    """
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="El equipo necesita un nombre")

    worker_id = request.worker_id.strip()
    if not worker_id:
        raise HTTPException(status_code=400, detail="Falta el ID del minero")

    agenda = _valid_agenda(request.categories)
    redis_client = redis.store.r
    try:
        if request.new_worker:
            if request.new_worker.worker_id.strip() != worker_id:
                raise HTTPException(
                    status_code=400,
                    detail="El minero a registrar no coincide con el del equipo",
                )
            # Reusa el alta de la página de mineros: verifica firma, frescura del
            # timestamp y despliega el pod si vino la clave privada.
            persist_worker_registration(request.new_worker, redis_client)
        else:
            verify_worker_ownership(worker_id, owner_id, redis_client)

        status = read_worker_status(redis_client, worker_id)
        url = coordinator_address(status or {}, worker_id)

        try:
            team = store.create_team(name=name, owner=owner_id,
                                     coordinator_worker_id=worker_id,
                                     coordinator_url=url,
                                     categories=agenda)
        except TeamError as exc:
            raise _as_http(exc) from exc

        await dispatch_switch_mode(worker_id, "pool-coordinator", "",
                                   redis_client, publisher)
        # La agenda va después del switch a propósito: el coordinador la relee
        # de Redis en cada tick, así que llega igual aunque el pod todavía no
        # haya arrancado — y si la escribiéramos antes, un pod que arranca en
        # modo standalone la ignoraría por completo.
        await push_voting_policy(worker_id, agenda, redis_client)
    finally:
        publisher.close()

    log.info("equipo %s creado por %s (coordinador %s, agenda %s)",
             team["team_id"], owner_id, worker_id, agenda or "todas")
    return _hydrate(store, redis_client, store.get_team(team["team_id"]))


@router.post("/{team_id}/join", response_model=Team)
async def join_team(
    team_id: str,
    request: TeamMembershipRequest,
    owner_id: str = Depends(get_owner_id),
    store: TeamsStore = Depends(get_teams_store),
    redis: RedisReader = Depends(get_redis_reader),
    publisher=Depends(get_rabbitmq_publisher),
):
    """Suma un minero propio al equipo de otro, en modo ``pool-worker``."""
    worker_id = request.worker_id.strip()
    redis_client = redis.store.r
    verify_worker_ownership(worker_id, owner_id, redis_client)

    team = store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="El equipo no existe")

    # La dirección se resuelve al unirse con lo mejor que se sepa en el momento:
    # el `address` que el coordinador publica si está encendido, y si no la
    # última conocida.
    #
    # Antes acá había un 409 si el coordinador no reportaba, para no mandar a un
    # minero a pedirle trabajo a un HTTP muerto. Resultó peor el remedio: un
    # equipo cuyo coordinador todavía no arrancó quedaba imposible de integrar
    # **para siempre**, y como registrar un minero no lo enciende, ese era el
    # caso normal y no la excepción. Ahora se puede entrar antes: la URL se
    # corrige sola cuando el coordinador aparece (ver `_hydrate`) y el
    # `PoolWorker` reintenta el registro indefinidamente, así que el equipo se
    # arma solo en cuanto las dos puntas están encendidas.
    coordinator_id = team["coordinator_worker_id"]
    coordinator_status = read_worker_status(redis_client, coordinator_id)
    url = (coordinator_address(coordinator_status, coordinator_id)
           if coordinator_status
           else (team.get("coordinator_url")
                 or coordinator_address({}, coordinator_id)))
    store.set_coordinator_url(team_id, url)

    try:
        try:
            store.join_team(team_id, worker_id)
        except TeamError as exc:
            raise _as_http(exc) from exc
        await dispatch_switch_mode(worker_id, "pool-worker", url,
                                   redis_client, publisher)
    finally:
        publisher.close()

    log.info("minero %s se unió al equipo %s", worker_id, team_id)
    return _hydrate(store, redis_client, store.get_team(team_id))


@router.post("/{team_id}/leave", response_model=dict)
async def leave_team(
    team_id: str,
    request: TeamMembershipRequest,
    owner_id: str = Depends(get_owner_id),
    store: TeamsStore = Depends(get_teams_store),
    redis: RedisReader = Depends(get_redis_reader),
    publisher=Depends(get_rabbitmq_publisher),
):
    """Saca un minero propio del equipo y lo devuelve a modo competitivo."""
    worker_id = request.worker_id.strip()
    redis_client = redis.store.r
    verify_worker_ownership(worker_id, owner_id, redis_client)

    if store.team_of_worker(worker_id) != team_id:
        raise HTTPException(status_code=404,
                            detail=f"El minero '{worker_id}' no está en este equipo")

    try:
        try:
            store.leave_team(worker_id)
        except TeamError as exc:
            raise _as_http(exc) from exc
        await dispatch_switch_mode(worker_id, "standalone", "",
                                   redis_client, publisher)
    finally:
        publisher.close()

    log.info("minero %s salió del equipo %s", worker_id, team_id)
    return {"ok": True, "worker_id": worker_id, "mode": "standalone"}


@router.put("/{team_id}/categories", response_model=Team)
async def set_team_categories(
    team_id: str,
    request: TeamCategoriesRequest,
    owner_id: str = Depends(get_owner_id),
    store: TeamsStore = Depends(get_teams_store),
    redis: RedisReader = Depends(get_redis_reader),
):
    """Cambia las áreas de ley sobre las que vota el equipo (AGENT.md 3.10).

    Es la decisión política del equipo: con agenda declarada, su coordinador
    ignora las ventanas de otras áreas y ni él ni sus mineros aportan un solo
    hash a esas leyes. Lista vacía = vuelve a votar todo.

    Sólo quien fundó el equipo la cambia. La agenda es lo que el equipo *es*
    frente al resto de la red, y dejar que cualquier miembro la reescriba
    permitiría entrar a un equipo sólo para desviarle el cómputo.
    """
    team = store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="El equipo no existe")
    if team.get("owner") != owner_id:
        raise HTTPException(
            status_code=403,
            detail="Sólo quien fundó el equipo puede cambiar sus categorías")

    agenda = _valid_agenda(request.categories)
    redis_client = redis.store.r
    try:
        store.set_categories(team_id, agenda)
    except TeamError as exc:
        raise _as_http(exc) from exc
    await push_voting_policy(team["coordinator_worker_id"], agenda, redis_client)

    log.info("equipo %s cambió su agenda a %s", team_id, agenda or "todas")
    return _hydrate(store, redis_client, store.get_team(team_id))


@router.delete("/{team_id}", response_model=dict)
async def dissolve_team(
    team_id: str,
    owner_id: str = Depends(get_owner_id),
    store: TeamsStore = Depends(get_teams_store),
    redis: RedisReader = Depends(get_redis_reader),
    publisher=Depends(get_rabbitmq_publisher),
):
    """Disuelve el equipo y devuelve a todos sus mineros a modo competitivo.

    Dejar a los miembros en ``pool-worker`` apuntando a un coordinador que ya no
    reparte trabajo los dejaría pidiendo fragmentos a un HTTP muerto para
    siempre, así que la disolución los libera explícitamente.
    """
    team = store.get_team(team_id)
    if not team:
        raise HTTPException(status_code=404, detail="El equipo no existe")
    if team.get("owner") != owner_id:
        raise HTTPException(status_code=403,
                            detail="Sólo quien creó el equipo puede disolverlo")

    redis_client = redis.store.r
    try:
        try:
            affected = store.dissolve_team(team_id)
        except TeamError as exc:
            raise _as_http(exc) from exc
        for worker_id in affected:
            await dispatch_switch_mode(worker_id, "standalone", "",
                                       redis_client, publisher)
        # El coordinador deja de serlo: su política de voto ya no representa a
        # nadie, y dejarla puesta le impondría la agenda de este equipo si más
        # adelante funda otro.
        clear_voting_policy(team["coordinator_worker_id"], redis_client)
    finally:
        publisher.close()

    log.info("equipo %s disuelto por %s (%d mineros liberados)",
             team_id, owner_id, len(affected))
    return {"ok": True, "team_id": team_id, "released": affected}
