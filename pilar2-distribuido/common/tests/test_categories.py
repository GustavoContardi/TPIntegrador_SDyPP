"""Categorías de ley: el eje sobre el que los equipos deciden qué minan.

Lo que se protege acá es la asimetría entre **leer** y **escribir** una
categoría. Leer tiene que ser tolerante (hay leyes anteriores a esta función, y
un nodo desactualizado puede mandar cualquier cosa) y escribir tiene que ser
estricto (el autor firma su categoría; aceptarle una inventada normalizándola en
silencio le dejaría la ley esperando a equipos que nunca la van a minar).
"""

import pytest

from common.blockchain import (
    DEFAULT_CATEGORY,
    VALID_CATEGORIES,
    category_label,
    covers_category,
    normalize_category,
    parse_categories,
    validate_category,
)


class TestNormalizar:
    """Camino de lectura: tolerante, nunca lanza."""

    def test_vacio_es_general(self):
        assert normalize_category(None) == DEFAULT_CATEGORY
        assert normalize_category("") == DEFAULT_CATEGORY

    def test_desconocida_cae_en_general(self):
        # Una ley guardada antes de que existieran las categorías, o un nodo con
        # una versión más nueva: se lee, no se rompe.
        assert normalize_category("astrologia") == DEFAULT_CATEGORY

    def test_normaliza_mayusculas_y_espacios(self):
        assert normalize_category("  ECONOMIA ") == "economia"


class TestValidar:
    """Camino de escritura: estricto."""

    def test_acepta_las_conocidas(self):
        for category in VALID_CATEGORIES:
            assert validate_category(category) == category

    def test_rechaza_la_inventada(self):
        with pytest.raises(ValueError) as exc:
            validate_category("astrologia")
        assert "astrologia" in str(exc.value)

    def test_vacio_es_general_no_error(self):
        # Proponer sin declarar área es válido: cae en general.
        assert validate_category(None) == DEFAULT_CATEGORY
        assert validate_category("") == DEFAULT_CATEGORY


class TestAgenda:
    def test_acepta_lista_o_string_separado_por_comas(self):
        # El string es como viaja en el hash de Redis del equipo.
        assert parse_categories("salud,economia") == ["economia", "salud"]
        assert parse_categories(["salud", "economia"]) == ["economia", "salud"]

    def test_orden_canonico_para_que_dos_agendas_iguales_sean_iguales(self):
        # Sin esto, el coordinador vería "cambió la política" en cada tick sólo
        # porque el usuario tildó las casillas en otro orden.
        assert parse_categories(["salud", "economia"]) == \
               parse_categories(["economia", "salud"])

    def test_descarta_desconocidas_y_duplicadas(self):
        assert parse_categories(["salud", "salud", "astrologia"]) == ["salud"]

    def test_vacia_no_es_none(self):
        assert parse_categories(None) == []
        assert parse_categories("") == []


class TestCubertura:
    def test_sin_agenda_vota_todo(self):
        # El pool clásico: sin agenda declarada mina lo que venga.
        assert covers_category([], "economia") is True
        assert covers_category(None, "salud") is True

    def test_con_agenda_solo_lo_declarado(self):
        assert covers_category(["economia"], "economia") is True
        assert covers_category(["economia"], "salud") is False

    def test_una_categoria_desconocida_se_compara_como_general(self):
        # Coherente con normalize_category: la ventana rara la mina quien vota
        # general, no nadie y no todos.
        assert covers_category(["general"], "astrologia") is True
        assert covers_category(["economia"], "astrologia") is False


def test_toda_categoria_tiene_etiqueta():
    # La UI muestra la etiqueta; un slug sin etiqueta se vería crudo en pantalla.
    for category in VALID_CATEGORIES:
        assert category_label(category) != category
