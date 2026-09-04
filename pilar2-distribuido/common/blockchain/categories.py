"""Categorías temáticas de las leyes (AGENT.md 3.10).

Una ley no es sólo un texto: **pertenece a un área de gobierno**. La categoría
es el eje sobre el que los equipos deciden en qué se gastan su cómputo: un
equipo declara las categorías que le importan y sólo aporta poder de cálculo a
las ventanas de esas categorías (``pool:policy:<coordinador>``, ver
``PoolCoordinator._check_voting_policy``). Es la forma más directa que tiene el
sistema de representar una facción política con agenda propia, en vez de un
pool que mina todo lo que pasa por delante.

Decisiones de diseño cerradas:

- **La categoría NO entra en ``partial_hash_base``.** El desafío se sigue
  serializando como ``law_id + text_hash + voting_window_id + action``
  (AGENT.md 5): el ``law_id`` ya identifica unívocamente la ley, y meter la
  categoría en el string base obligaría a tocar el puente con el minero de
  Pilar 1 sin ganar integridad — la categoría vive en ``law:<id>`` y viaja en
  el desafío publicado sólo para que los equipos puedan filtrar.
- **La categoría SÍ entra en el mensaje firmado** de la propuesta
  (``proposal_message``): quien propone declara el área bajo su firma, y nadie
  puede re-etiquetar una ley ajena en tránsito para desviar qué equipos la van
  a minar.
- **``general`` es el default**, no una categoría de descarte: es donde caen la
  ley que no encaja en ningún área y todas las leyes anteriores a esta función.

Los slugs son ASCII en minúsculas porque viajan en claves de Redis, en query
params y en el payload del desafío; la etiqueta con acentos es sólo para la UI.
"""

from __future__ import annotations

CATEGORY_GENERAL = "general"
CATEGORY_ECONOMIA = "economia"
CATEGORY_SALUD = "salud"
CATEGORY_EDUCACION = "educacion"
CATEGORY_SEGURIDAD = "seguridad"
CATEGORY_AMBIENTE = "ambiente"
CATEGORY_INFRAESTRUCTURA = "infraestructura"
CATEGORY_DERECHOS = "derechos"

#: Slug → etiqueta legible. El orden es el que usa la UI para mostrarlas.
CATEGORY_LABELS: dict[str, str] = {
    CATEGORY_ECONOMIA: "Economía y finanzas",
    CATEGORY_SALUD: "Salud",
    CATEGORY_EDUCACION: "Educación",
    CATEGORY_SEGURIDAD: "Seguridad y justicia",
    CATEGORY_AMBIENTE: "Ambiente",
    CATEGORY_INFRAESTRUCTURA: "Infraestructura y transporte",
    CATEGORY_DERECHOS: "Derechos y ciudadanía",
    CATEGORY_GENERAL: "General",
}

VALID_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_LABELS)

DEFAULT_CATEGORY = CATEGORY_GENERAL


def category_label(category: str) -> str:
    """Etiqueta legible de una categoría; el slug crudo si no se la conoce."""
    return CATEGORY_LABELS.get(normalize_category(category), category)


def normalize_category(raw) -> str:
    """Lectura tolerante: vacío o desconocido ⇒ ``general``.

    Es el camino de **lectura** (leyes viejas sin categoría, estado que quedó en
    Redis de antes de esta función, mensajes de un nodo desactualizado). Nunca
    lanza: una categoría que no reconocemos no puede tumbar la apertura de una
    ventana ni el listado de leyes.
    """
    if not raw:
        return DEFAULT_CATEGORY
    slug = str(raw).strip().lower()
    return slug if slug in CATEGORY_LABELS else DEFAULT_CATEGORY


def validate_category(raw) -> str:
    """Camino de **escritura**: valida y devuelve el slug, o lanza ``ValueError``.

    Se usa al ingresar una propuesta (API y NCT). Acá sí conviene rechazar: si
    aceptáramos silenciosamente una categoría inventada normalizándola a
    ``general``, el autor firmaría una cosa y el sistema encolaría otra, y los
    equipos que declararon esa área nunca minarían la ley que creyeron pedir.
    """
    if raw is None or str(raw).strip() == "":
        return DEFAULT_CATEGORY
    slug = str(raw).strip().lower()
    if slug not in CATEGORY_LABELS:
        raise ValueError(
            f"categoría inválida: {raw!r}; esperado una de {VALID_CATEGORIES}")
    return slug


def parse_categories(raw) -> list[str]:
    """Normaliza la selección de categorías de un equipo.

    Acepta lista o string separado por comas (que es como viaja en el hash de
    Redis del equipo). Descarta desconocidas y duplicadas, y conserva el orden
    canónico de ``CATEGORY_LABELS`` para que dos equipos con la misma agenda
    tengan exactamente la misma representación.

    La lista **vacía significa "todas"** (ver ``covers_category``): un equipo que
    no declaró agenda es el pool clásico que mina lo que venga, y ése tiene que
    seguir siendo el comportamiento por defecto.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        items = raw.split(",")
    else:
        items = list(raw)
    selected = {str(item).strip().lower() for item in items if str(item).strip()}
    return [c for c in VALID_CATEGORIES if c in selected]


def covers_category(selected, category) -> bool:
    """¿Un equipo con la agenda ``selected`` aporta cómputo a ``category``?

    Sin agenda declarada (lista vacía) aporta a todo. Con agenda, sólo a lo que
    declaró: si la ventana en curso es de un área que no eligió, su coordinador
    no fragmenta el espacio de nonces y **ni él ni sus mineros suman un solo
    hash** a esa ley.
    """
    agenda = parse_categories(selected)
    if not agenda:
        return True
    return normalize_category(category) in agenda
