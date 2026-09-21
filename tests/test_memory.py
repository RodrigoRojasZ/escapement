"""Tests de la tool de memoria. Aislados: usan vaults temporales y un script dummy."""

import pytest

from agent.tools import memory


def test_validate_slug_ok():
    assert memory.validate_slug("mi-nota_1") == "mi-nota_1"


@pytest.mark.parametrize("bad", ["Mi Nota", "", "con espacio", "MAYUS", "acento-é"])
def test_validate_slug_rechaza(bad):
    with pytest.raises(ValueError):
        memory.validate_slug(bad)


def test_build_note_ok():
    note = memory.build_note("mi-nota", "desc   con   espacios", "reference", "  cuerpo\n")
    assert "name: mi-nota" in note
    assert "type: reference" in note
    assert "description: desc con espacios" in note  # descripcion normalizada
    assert note.strip().endswith("cuerpo")


def test_build_note_type_invalido():
    with pytest.raises(ValueError):
        memory.build_note("x", "d", "bogus", "b")


def test_resolve_note_path_dentro_del_vault(tmp_path):
    base = tmp_path / "projects"
    (base / "proj").mkdir(parents=True)
    p = memory.resolve_note_path("proj", "nota", projects_dir=base)
    assert p == (base / "proj" / "memory" / "nota.md").resolve()


def test_resolve_note_path_rechaza_proyecto_fuera_del_vault(tmp_path):
    base = tmp_path / "projects"
    (base / "proj").mkdir(parents=True)
    with pytest.raises(ValueError):
        memory.resolve_note_path("..", "nota", projects_dir=base)


def test_upsert_index_no_duplica_y_actualiza(tmp_path):
    mem = tmp_path / "memory"
    mem.mkdir()
    memory.upsert_index(mem, "Mi Titulo", "mi-nota", "un gancho")
    memory.upsert_index(mem, "Mi Titulo", "mi-nota", "gancho NUEVO")  # actualiza en sitio
    txt = (mem / "MEMORY.md").read_text(encoding="utf-8")
    assert txt.count("mi-nota.md") == 1  # no duplica
    assert "gancho NUEVO" in txt and "un gancho" not in txt  # actualizó el hook


def _seed_vault(base, project, notes):
    """Crea un vault temporal con notas ``(name, description, type, body)``. Devuelve memory/."""
    mem = base / project / "memory"
    mem.mkdir(parents=True)
    for name, desc, type_, body in notes:
        (mem / f"{name}.md").write_text(
            memory.build_note(name, desc, type_, body), encoding="utf-8"
        )
    return mem


def test_parse_frontmatter():
    fm = memory._parse_frontmatter(
        memory.build_note("mi-nota", "una descripcion", "reference", "b")
    )
    assert fm["name"] == "mi-nota"
    assert fm["description"] == "una descripcion"
    assert fm["type"] == "reference"


def test_list_notes_inventario(tmp_path):
    base = tmp_path / "projects"
    _seed_vault(
        base,
        "proj-a",
        [("nota-1", "desc uno", "project", "b"), ("nota-2", "desc dos", "reference", "b")],
    )
    notas = memory.list_notes(projects_dir=base)
    assert {n["name"] for n in notas} == {"nota-1", "nota-2"}
    n1 = next(n for n in notas if n["name"] == "nota-1")
    assert n1["type"] == "project" and n1["description"] == "desc uno" and n1["project"] == "proj-a"


def test_list_notes_filtra_por_proyecto(tmp_path):
    base = tmp_path / "projects"
    _seed_vault(base, "proj-a", [("a", "d", "project", "b")])
    _seed_vault(base, "proj-b", [("b", "d", "project", "b")])
    assert {n["name"] for n in memory.list_notes("proj-a", projects_dir=base)} == {"a"}


def test_read_note(tmp_path):
    base = tmp_path / "projects"
    _seed_vault(base, "proj", [("nota", "d", "reference", "el cuerpo")])
    assert "el cuerpo" in memory.read_note("proj", "nota", projects_dir=base)
    assert "no existe" in memory.read_note("proj", "fantasma", projects_dir=base)


def test_delete_note_quita_archivo_e_indice(tmp_path):
    base = tmp_path / "projects"
    mem = _seed_vault(base, "proj", [("nota", "gancho", "reference", "b")])
    memory.upsert_index(mem, "Nota", "nota", "gancho")
    memory.delete_note("proj", "nota", projects_dir=base)
    assert not (mem / "nota.md").exists()
    assert "nota.md" not in (mem / "MEMORY.md").read_text(encoding="utf-8")


def test_delete_note_inexistente_lanza(tmp_path):
    base = tmp_path / "projects"
    _seed_vault(base, "proj", [])
    with pytest.raises(ValueError):
        memory.delete_note("proj", "fantasma", projects_dir=base)


def test_archive_note_mueve_a_historico_reversible(tmp_path):
    base = tmp_path / "projects"
    mem = _seed_vault(base, "proj", [("caso", "gancho", "reference", "cuerpo")])
    memory.upsert_index(mem, "Caso", "caso", "gancho")
    dst = memory.archive_note("proj", "caso", projects_dir=base)
    assert not (mem / "caso.md").exists()  # fuera del vault principal
    assert (
        dst.exists() and dst.parent == base / "proj-archivo" / "memory"
    )  # conservada en histórico
    assert "cuerpo" in dst.read_text(encoding="utf-8")  # no se pierde el contenido
    assert "caso.md" not in (mem / "MEMORY.md").read_text(encoding="utf-8")  # fuera del índice


def test_run_recall_con_script_dummy(tmp_path):
    script = tmp_path / "dummy.py"
    script.write_text(
        "import sys; print('### demo'); print('  hit:', sys.argv[1])", encoding="utf-8"
    )
    out = memory.run_recall("scrapers", script=script)
    assert "demo" in out and "scrapers" in out


def test_run_recall_script_inexistente(tmp_path):
    out = memory.run_recall("x", script=tmp_path / "nope.py")
    assert "no encontrado" in out
