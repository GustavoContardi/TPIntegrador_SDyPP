"""Quórum de mineros: ¿hay red suficiente para que esta ley tenga chance?

**Qué resuelve.** El NCT abría la ventana apenas la ley llegaba a la cabeza de
la cola, sin mirar si había alguien del otro lado. Con la red vacía el resultado
era siempre el mismo: la ventana vencía, la ley se marcaba ``discarded``, su
``text_hash`` quedaba anotado como descartado —así que reproponerla costaba el
cooldown largo de reproposición idéntica (AGENT.md 3.5)— y el autor se comía la
penalización por una ausencia de infraestructura que no era suya. Peor: el modo
de falla es **mudo**, en los logs se ve igual que una ley que nadie quiso minar.

Acá la ley no se abre: **se queda encolada** hasta que haya mineros que puedan
resolverla, y el sistema deja dicho por qué (``nct:availability``) para que el
cliente vea "sistema no disponible, tu ley queda pospuesta" en vez de verla
morir.

**Por qué no alcanza con contar mineros vivos.** Una ley tiene área de gobierno
(AGENT.md 3.10) y un equipo sólo aporta cómputo a las áreas de su agenda
(``covers_category``). Diez mineros vivos que sólo votan ``economia`` son, para
una ley de ``salud``, exactamente cero: el coordinador no fragmenta el espacio y
ni él ni sus miembros suman un hash. Por eso el quórum se mide sobre los mineros
**elegibles para esta categoría**, no sobre la población total — si no, el
sistema se declararía disponible y la ventana vencería igual, que es justo el
modo de falla que esto viene a cerrar.

**La tensión con la abstención (AGENT.md 3.10), y cómo se resolvió.** Que un
área sin equipos que la voten hiciera expirar la ley era un mecanismo
*deliberado*: la abstención hecha regla, un veto silencioso de las facciones.
Medir el quórum por área lo convierte en una espera: la ley no muere, se queda en
la cola hasta que aparezca alguien que la mine. **Ésa es la decisión tomada**, y
vale igual para promulgar y para derogar. El argumento es que las dos situaciones
que antes se veían idénticas —"nadie quiso" y "nadie pudo"— son políticamente
distintas, y el sistema no tiene forma de distinguirlas *después* de que la
ventana venció: sólo puede hacerlo antes, no abriéndola.

``by_category=False`` queda como vía de escape para volver al veto por
abstención, pero no es el modo en que corre el sistema.

**Qué se mira, exactamente.** No alcanza con la agenda temática: un equipo
también puede vetar **una acción entera** —típicamente todas las derogaciones— o
una ley puntual (``pool:policy:<coordinador>``). Un equipo que vota ``salud``
pero rechaza derogaciones vale cero para la derogación de una ley de salud, y
contarlo reabriría exactamente el modo de falla que esto cierra. Por eso el
predicado que decide es **el mismo objeto de código** que usa el coordinador del
pool para decidir si fragmenta (``policy_accepts``): si el NCT y el pool
razonaran por separado, la predicción y la conducta se irían separando sin que
nadie lo note.

**Qué NO hace.** No promete que la ventana se selle: mide presencia, no
velocidad. Un minero elegible alcanza para abrir, y si su cómputo no llega a
resolver `n` ceros en el plazo, eso es asunto de la dificultad dinámica
(``difficulty.py``), que mueve `n` con la misma población viva. Las dos miran lo
mismo —``worker:status:*``, TTL 15 s— y por la misma razón: se cuenta lo vivo,
no lo registrado, para que dar de alta mineros apagados no compre nada.
"""

from __future__ import annotations

from dataclasses import dataclass

from .categories import covers_category, normalize_category

#: Mineros elegibles mínimos para abrir una ventana. 1 es el piso con sentido:
#: con cero, la ventana vence sí o sí. Se sube por config cuando se quiere
#: exigir una red de verdad antes de someter una ley al desafío.
MIN_WORKERS_FOR_WINDOW = 1

#: Si el quórum se mide sobre los mineros que efectivamente minarían **esta**
#: ventana (área + vetos de acción/ley) o sobre toda la población viva. Ver la
#: tensión con la abstención en el docstring del módulo: en False, un área que
#: nadie vota vuelve a expirar como manda AGENT.md 3.10, y el gate sólo protege
#: contra la red vacía.
QUORUM_BY_CATEGORY = True

#: Política de un minero sin nada declarado: mina todo lo que pase.
DEFAULT_POLICY: dict = {"decision": "accept", "categories": []}


def policy_accepts(policy: dict, challenge: dict) -> bool:
    """¿Este minero/equipo aportaría cómputo a esta ventana?

    **Es la única definición de la regla en todo el sistema.** La usan el
    coordinador del pool para decidir si fragmenta el espacio y el NCT para
    decidir si abre la ventana; con dos copias, la predicción del NCT y la
    conducta del pool se irían separando en silencio y el sistema volvería a
    abrir ventanas que nadie va a minar.

    Se evalúa en tres pasos, en este orden:

    1. **Agenda temática** (AGENT.md 3.10): es la decisión política del equipo y
       no depende de ``decision``, que es el veto puntual de siempre. Agenda
       vacía = vota todas las áreas.
    2. **Acciones rechazadas** (``rejected_actions``): la forma que tiene un
       minero individual de declarar, por ejemplo, que no mina derogaciones.
    3. **Veto puntual** (``decision``/``action``/``law_id``): rechazar todas las
       derogaciones, o una ley concreta. ``decision`` distinto de ``reject`` con
       ningún criterio declarado rechaza todo, que es el comportamiento
       histórico de esta clave.
    """
    if not policy:
        return True
    if not covers_category(policy.get("categories"), challenge.get("category")):
        return False
    rechazadas = policy.get("rejected_actions") or ()
    if challenge.get("action") in rechazadas:
        return False
    if policy.get("decision", "accept") == "accept":
        return True
    if policy.get("action") and challenge.get("action") == policy["action"]:
        return False
    if policy.get("law_id") and challenge.get("law_id") == policy["law_id"]:
        return False
    if not policy.get("action") and not policy.get("law_id"):
        return False
    return True


def prevetoed(policy: dict, law_id) -> bool:
    """¿Este equipo/minero dejó cargado un veto a **esta** ley puntual?

    Es el veto por ``law_id`` de siempre (``decision: reject`` sin acción). Con
    deliberación (AGENT.md 3.12) deja de ser una razón para no convocarlo y pasa
    a ser su respuesta anticipada: un "no" que ya dijo antes de que la ley
    saliera.
    """
    return (bool(policy) and bool(law_id)
            and policy.get("decision", "accept") != "accept"
            and not policy.get("action")
            and policy.get("law_id") == law_id)


def policy_convokes(policy: dict, challenge: dict) -> bool:
    """¿Se convoca a este equipo/minero a deliberar sobre esta ley?

    Igual que ``policy_accepts`` salvo por el veto a una ley puntual, que acá
    **no** excluye. La diferencia importa por la dificultad: se calcula sobre
    los convocados, y si un veto puntual sacara al equipo de la cuenta, el más
    grande podría vetar de antemano para que la ley saliera con la dificultad
    de los chicos — exactamente lo contrario de lo que su "no" tiene que lograr.
    La agenda y el veto a una acción entera sí excluyen: son la forma de decir
    "esto no me toca", no "a esta ley digo que no".
    """
    if prevetoed(policy, challenge.get("law_id")):
        policy = {**policy, "decision": "accept"}
    return policy_accepts(policy, challenge)


#: Modo de un minero que corre solo. Un worker viejo que no reporta modo se lee
#: así, que es lo que era antes de que existieran los equipos.
STANDALONE_MODE = "standalone"


def deliberates(worker: dict) -> bool:
    """¿Un minero que no está en ningún equipo tiene quién decida por él?

    Sólo el standalone: su dueño responde. El pool de infraestructura
    (``pool-auto``) es anónimo, y un pool-worker o coordinador sin equipo
    registrado no tiene a nadie que hable por el conjunto. Con "si no responde,
    no vota", ninguno de ellos mina nunca, así que tampoco se los cuenta.
    """
    return (worker.get("mode") or STANDALONE_MODE) == STANDALONE_MODE


@dataclass(frozen=True)
class Availability:
    """Foto del quórum para una categoría concreta."""

    ok: bool
    category: str
    #: Mineros vivos en total (toda la red, sin filtrar por política).
    live: int
    #: Los que aportarían cómputo a **esta** ventana.
    eligible: int
    #: Mínimo exigido para abrir.
    required: int
    #: Acción de la ventana evaluada, si se evaluó una concreta. Importa porque
    #: un equipo puede votar el área y aun así vetar todas las derogaciones.
    action: str = ""

    def reason(self) -> str:
        """Por qué no se puede abrir, en una línea apta para log y para UI.

        Distingue los casos porque piden acciones distintas: sin mineros hay que
        levantar mineros; con mineros que no toman la ventana hay que sumar un
        equipo que vote esa área (o que acepte esa acción). La acción se nombra
        sólo cuando se evaluó una, porque un equipo puede votar el área y aun así
        rechazar la derogación, y sin decirlo el mensaje sería desconcertante:
        "hay equipos de salud" y la derogación de una ley de salud sin abrirse.
        """
        if self.ok:
            return ""
        if self.live == 0:
            return ("no hay mineros vivos en la red "
                    f"(se necesitan {self.required})")
        que = f"el área '{self.category}'"
        if self.action:
            que = f"la {self.action} de una ley de '{self.category}'"
        return (f"hay {self.live} minero(s) vivo(s) pero "
                f"{self.eligible} dispuesto(s) a minar {que} "
                f"(se necesitan {self.required})")


def _team_policy(equipo: dict) -> dict:
    """Política efectiva de un equipo: la de su coordinador, o sólo su agenda.

    ``pool:policy:<coordinador>`` es la fuente autoritativa —lleva la agenda y
    además los vetos de acción/ley—, pero puede no existir todavía (un equipo
    recién fundado cuya política aún no bajó). En ese caso se usa la agenda del
    hash del equipo, que es lo único que se sabe.
    """
    policy = equipo.get("policy")
    if isinstance(policy, dict) and policy:
        return policy
    return {"categories": equipo.get("categories")}


def eligible_workers(workers: list[dict], teams: list[dict],
                     challenge: dict, *, convocation: bool = False) -> list[dict]:
    """Mineros que realmente aportarían cómputo a **esta** ventana.

    ``challenge`` es la ventana que se está por abrir: ``category``, ``action`` y
    ``law_id``. Se necesitan las tres porque un equipo puede vetar un área, una
    acción entera (todas las derogaciones) o una ley puntual, y cualquiera de las
    tres lo deja fuera.

    Espeja la decisión que cada minero toma por su cuenta al recibir el desafío,
    con el mismo predicado que ellos usan (``policy_accepts``), para que el NCT
    no abra una ventana que nadie va a tomar:

    - Un minero **en equipo** hereda la política del equipo: es su coordinador el
      que decide fragmentar o no (``PoolCoordinator._check_voting_policy``), así
      que si el equipo no toma la ventana, ni él ni sus miembros minan.
    - Un **standalone** declara la suya por entorno (``STANDALONE_CATEGORIES`` y
      ``STANDALONE_REJECTED_ACTIONS``) y la reporta en ``worker:status:*``. Sin
      nada declarado mina todo, que es el caso por defecto.

    Un equipo puede tener miembros que ya no estén vivos: se cuentan sólo los
    que aparecen en ``workers``.

    Con ``convocation=True`` (deliberación, AGENT.md 3.12) se cuentan los que se
    **convocarían** a decidir: el veto a la ley puntual no excluye
    (``policy_convokes``) y los mineros sueltos sin dueño que responda
    (``deliberates``) no cuentan, porque nunca van a minar.
    """
    acepta = policy_convokes if convocation else policy_accepts
    desafio = dict(challenge)
    desafio["category"] = normalize_category(desafio.get("category"))
    vivos = {w.get("worker_id"): w for w in workers if w.get("worker_id")}
    politica_por_worker: dict[str, dict] = {}

    for equipo in teams:
        miembros = [equipo.get("coordinator_worker_id"), *equipo.get("members", [])]
        politica = _team_policy(equipo)
        for wid in miembros:
            if wid in vivos:
                politica_por_worker[wid] = politica

    elegibles = []
    for wid, worker in vivos.items():
        # El `in` y no `.get()`: un equipo sin agenda declarada guarda una
        # política vacía, que significa "vota todo", y hay que distinguirlo de
        # "no está en ningún equipo" para no leerle la política de standalone a
        # un miembro de pool.
        if wid in politica_por_worker:
            politica = politica_por_worker[wid]
        else:
            if convocation and not deliberates(worker):
                continue
            politica = {"categories": worker.get("categories"),
                        "rejected_actions": worker.get("rejected_actions")}
        if acepta(politica, desafio):
            elegibles.append(worker)
    return elegibles


def assess(workers: list[dict], teams: list[dict], challenge, *,
           minimum: int = MIN_WORKERS_FOR_WINDOW,
           by_category: bool = QUORUM_BY_CATEGORY,
           convocation: bool = False) -> Availability:
    """Evalúa el quórum para abrir una ventana.

    ``challenge`` es la ventana a abrir (``category``, ``action``, ``law_id``).
    Se acepta también un string suelto, que se lee como la categoría y sin
    acción: es el camino del API, donde el ciudadano pregunta por un área antes
    de tener una ley.

    ``minimum <= 0`` desactiva el chequeo: siempre disponible. Es el default de
    los tests y la vía de escape para una demo sin mineros, donde bloquear la
    apertura sería más molesto que útil.

    ``by_category=False`` cuenta toda la población viva sin mirar políticas: el
    gate pasa a cubrir sólo la red vacía y un área desierta vuelve a expirar
    como veto político (AGENT.md 3.10).

    ``convocation=True`` mide a los que se convocarían a deliberar (ver
    ``eligible_workers``): con deliberación, un veto puntual ya no deja a la ley
    esperando, la lleva a votación para que se caiga.
    """
    desafio = ({"category": challenge} if isinstance(challenge, str)
               else dict(challenge or {}))
    area = normalize_category(desafio.get("category"))
    desafio["category"] = area
    accion = desafio.get("action") or ""
    if minimum <= 0:
        return Availability(ok=True, category=area, action=accion,
                            live=len(workers), eligible=len(workers),
                            required=0)
    elegibles = (eligible_workers(workers, teams, desafio, convocation=convocation)
                 if by_category
                 else [w for w in workers if w.get("worker_id")])
    return Availability(ok=len(elegibles) >= minimum, category=area,
                        action=accion, live=len(workers),
                        eligible=len(elegibles), required=minimum)
