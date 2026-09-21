"""Tests del registro de personas (agentes especializados): selección por tipo + repo."""

import pytest

from agent import personas
from agent.personas import select, system_for


@pytest.fixture(autouse=True)
def _repos(monkeypatch):
    # Hermético: se parte del registro BASE para que la suite no dependa del escapement.toml
    # de quien la corre (un [personas] local rebindearía los slots y rompería el ruteo aquí).
    monkeypatch.setattr(personas, "PERSONAS", list(personas._PERSONAS_BASE))
    monkeypatch.setattr(
        personas.config,
        "REPOS",
        {
            "scraper": r"D:\mi_scraper",  # el nombre = el slot que declara la persona
            "backend": r"D:\mi_backend",
        },
    )


def test_investigar_usa_el_investigador():
    p = select("investigar", r"G:\cualquier_repo")  # genérica: cualquier repo
    assert p is not None and p.name == "investigador"


def test_editar_en_repo_de_scraping():
    p = select("editar", r"D:\mi_scraper")
    assert p is not None and p.name == "scraping"


def test_editar_en_repo_backend():
    p = select("editar", r"D:\mi_backend")
    assert p is not None and p.name == "backend-app"


def test_verificar_usa_el_revisor():
    p = select("verificar", r"G:\lo_que_sea")
    assert p is not None and p.name == "revisor"


def test_tipos_sin_persona_en_registro():
    assert select("preguntar", r"G:\x") is None
    assert select("memoria", r"G:\x") is None  # memoria/reflexionar tienen su propio system


def test_editar_en_repo_no_registrado_usa_ingeniero():
    # 'editar' sin persona de dominio del repo -> cae al ingeniero (coder generalista por defecto).
    p = select("editar", r"G:\repo_desconocido")
    assert p is not None and p.name == "ingeniero"


def test_system_for_antepone_o_vacio():
    s = system_for("investigar", r"G:\x")
    assert s.startswith("Eres un investigador") and s.endswith("\n\n")
    assert system_for("preguntar", r"G:\x") == ""


def test_especialistas_de_dominio_no_auto_enrutan():
    # Los arquetipos de dominio (R1) son subagentes de CC invocables a mano: 'tipos' vacío para no
    # ganarle a las personas de dominio ni a la ruta genérica. (El ingeniero SÍ auto-enruta ahora.)
    solo_cc = {
        "tester",
        "refactor",
        "optimizador",
        "depurador",
        "documentador",
        "dependencias",
        "migraciones",
        "frontend",
    }
    por_nombre = {p.name: p for p in personas.PERSONAS}
    assert solo_cc <= set(por_nombre)  # todos presentes en el registro
    for name in solo_cc:
        assert por_nombre[name].tipos == ()  # no auto-enruta -> ruteo del runner intacto


def test_ingeniero_auto_enruta_codigo():
    # El ingeniero se habilitó como coder/coordinador por defecto para los tipos de código.
    ing = next(p for p in personas.PERSONAS if p.name == "ingeniero")
    assert ing.tipos == ("editar", "crear", "ejecutar") and ing.repos == ()


def test_dominio_gana_sobre_ingeniero():
    # En un repo de dominio, la persona del repo manda sobre el ingeniero genérico.
    assert select("editar", r"D:\mi_scraper").name == "scraping"


# --- Asignación explícita de persona (planner: separar-y-asignar) ---------------


def test_by_name_resuelve_o_none():
    assert personas.by_name("Seguridad").name == "seguridad"  # case-insensitive
    assert personas.by_name("  optimizador ").name == "optimizador"
    assert personas.by_name("no_existe") is None
    assert personas.by_name("") is None


def test_system_for_especialista_explicito_gana_sobre_generico():
    # Repo sin dominio: el especialista asignado por el planner pisa al ingeniero genérico.
    s = system_for("editar", r"G:\repo_desconocido", "seguridad")
    assert s.startswith("Eres un ingeniero de seguridad")


def test_system_for_especialista_gana_sobre_dominio():
    # El especialista transversal (repo-agnóstico) PISA al coder de dominio del repo: una
    # optimización la hace 'optimizador' aunque el paso corra sobre un repo de scraping.
    s = system_for("editar", r"D:\mi_scraper", "optimizador")
    assert s.startswith("Eres un ingeniero de performance")


def test_system_for_auto_router_como_persona_no_pisa_dominio():
    # Un nombre de AUTO-ROUTER (no especialista) pasado como persona explícita NO override:
    # cae al ruteo por tipo+repo. 'backend-app' sobre un repo de scraping -> gana 'scraping'.
    s = system_for("editar", r"D:\mi_scraper", "backend-app")
    assert s.startswith("Eres un desarrollador experto en scraping")


def test_system_for_persona_desconocida_cae_al_auto_ruteo():
    s = system_for("editar", r"G:\repo_desconocido", "persona_inexistente")
    assert s.startswith("Eres un ingeniero de software senior full-stack")  # ingeniero fallback


def test_system_for_persona_vacia_es_comportamiento_previo():
    # persona='' -> auto-ruteo por tipo+repo (backward compatible).
    assert system_for("verificar", r"G:\x", "") == system_for("verificar", r"G:\x")


def test_roster_brief_solo_especialistas_asignables():
    r = personas.roster_brief()
    # Lista SOLO los especialistas transversales (tipos=()), asignables por el planner.
    assert "- seguridad:" in r and "- optimizador:" in r and "- tester:" in r
    # Excluye los auto-routers (default por tipo+repo): no se asignan a mano.
    for auto in ("- ingeniero:", "- scraping:", "- backend-app:", "- investigador:", "- revisor:"):
        assert auto not in r
    especialistas = [p for p in personas.PERSONAS if personas.es_especialista(p)]
    assert len(r.splitlines()) == len(especialistas)  # una línea por especialista


def test_export_agents_formato_cc(tmp_path):
    escritos = personas.export_agents(tmp_path)
    assert len(escritos) == len(personas.PERSONAS)  # una por persona (auto-enrutadas + de dominio)
    inv = (tmp_path / "investigador.md").read_text(encoding="utf-8")
    assert inv.startswith("---\nname: investigador\n")  # frontmatter de subagente CC
    assert 'description: "' in inv and "Eres un investigador" in inv  # description + system en body
    assert (tmp_path / "seguridad.md").exists() and (tmp_path / "arquitecto.md").exists()


# --- Overrides locales desde [personas] de escapement.toml -------------------------


def test_override_repos_rebindea_el_slot_al_repo_real():
    # El registro público trae el slot "scraper"; el usuario apunta sus repos reales sin tocar código.
    reg = personas._aplicar_overrides(
        personas.PERSONAS, {"scraping": {"repos": ["mi_repo_a", "mi_repo_b"]}}
    )
    p = next(x for x in reg if x.name == "scraping")
    assert p.repos == ("mi_repo_a", "mi_repo_b")
    assert p.system == personas.by_name("scraping").system  # lo no declarado no se toca


def test_override_system_y_description():
    reg = personas._aplicar_overrides(
        personas.PERSONAS, {"scraping": {"system": "Eres X.", "description": "Para Y."}}
    )
    p = next(x for x in reg if x.name == "scraping")
    assert p.system == "Eres X." and p.description == "Para Y."
    assert p.repos == personas.by_name("scraping").repos  # el binding sobrevive


def test_override_tipos_vacios_vuelve_especialista():
    # tipos=[] es significativo: saca a la persona del auto-ruteo y la deja asignable por el planner.
    reg = personas._aplicar_overrides(personas._PERSONAS_BASE, {"scraping": {"tipos": []}})
    p = next(x for x in reg if x.name == "scraping")
    assert p.tipos == () and personas.es_especialista(p)


def test_override_sin_tabla_devuelve_el_registro_intacto():
    for vacia in ({}, None):
        reg = personas._aplicar_overrides(
            personas._PERSONAS_BASE, vacia if vacia is not None else {}
        )
        assert [(p.name, p.system, p.repos, p.tipos) for p in reg] == [
            (p.name, p.system, p.repos, p.tipos) for p in personas.PERSONAS
        ]


def test_override_no_muta_el_registro_base():
    antes = [(p.name, p.system, p.repos) for p in personas.PERSONAS]
    personas._aplicar_overrides(
        personas._PERSONAS_BASE, {"scraping": {"system": "Otro.", "repos": ["z"]}}
    )
    assert [(p.name, p.system, p.repos) for p in personas.PERSONAS] == antes


def test_override_basura_degrada_al_default_sin_romper():
    reg = personas._aplicar_overrides(
        personas.PERSONAS,
        {
            "no_existe": {"system": "fantasma"},  # persona inexistente: se ignora
            "scraping": {
                "system": "   ",  # str vacío: se ignora
                "repos": "mi_repo",  # str en vez de lista: se ignora
                "campo_raro": 1,  # clave desconocida: se ignora
                "description": "Sí vale.",  # el único válido, sí se aplica
            },
        },
    )
    assert [p.name for p in reg] == [
        p.name for p in personas.PERSONAS
    ]  # no aparecen personas nuevas
    p = next(x for x in reg if x.name == "scraping")
    base = personas.by_name("scraping")
    assert p.system == base.system and p.repos == base.repos
    assert p.description == "Sí vale."


def test_override_preserva_el_orden_del_registro():
    # select() desempata por orden de lista: si el override lo alterara, cambiaría el ruteo.
    reg = personas._aplicar_overrides(personas._PERSONAS_BASE, {"scraping": {"repos": ["z"]}})
    assert [p.name for p in reg] == [p.name for p in personas.PERSONAS]
