"""Tests del ReasoningBank (R2): captura de trayectorias exitosas + recall pre-plan.

Aislados: vault temporal, sin LLM, sin las deps del grupo 'semantic' (se monkeypatchea).
"""

from agent import reasoning
from agent.planner import Plan, Step
from agent.tools import memory


def _plan_ok(objetivo="mejora el worker"):
    return Plan(
        objetivo,
        "todos los tests pasan",
        [
            Step(
                1, "investiga el worker", "investigar", "d1", [], estado="hecho", nota="mapa listo"
            ),
            Step(
                2, "edita health_check.py", "editar", "d2", [1], estado="hecho", nota="PR abierto"
            ),
        ],
        repo="mi_scraper",
    )


# --- slug: valido y estable ---------------------------------------------------


def test_slug_es_valido_y_estable():
    s = reasoning._slug("Mejora el Worker  de scraping")
    memory.validate_slug(s)  # no lanza -> slug valido (a-z0-9-)
    assert s.startswith("traj-")
    assert reasoning._slug("Mejora el Worker  de scraping") == s  # deterministico


def test_slug_distingue_objetivos_con_prefijo_igual():
    a = reasoning._slug("migra la tabla de productos a postgres")
    b = reasoning._slug("migra la tabla de productos a parquet")
    assert a != b  # el hash del objetivo completo los separa


# --- _es_exito ----------------------------------------------------------------


def test_es_exito_solo_con_todos_hechos():
    assert reasoning._es_exito(_plan_ok()) is True
    p = _plan_ok()
    p.pasos[1].estado = "bloqueado"
    assert reasoning._es_exito(p) is False
    assert reasoning._es_exito(Plan("o", "g", [])) is False  # sin pasos no es exito


# --- capture_trajectory -------------------------------------------------------


def test_capture_escribe_nota_en_exito(tmp_path):
    ok = reasoning.capture_trajectory(_plan_ok(), projects_dir=tmp_path)
    assert ok is True
    mem = tmp_path / reasoning.REASONING_SLUG / "memory"
    notas = list(mem.glob("traj-*.md"))
    assert len(notas) == 1
    cuerpo = notas[0].read_text(encoding="utf-8")
    assert "mejora el worker" in cuerpo.lower()  # objetivo
    assert "[editar] edita health_check.py" in cuerpo  # secuencia de pasos
    assert (mem / "MEMORY.md").exists()  # indexada


def test_capture_no_escribe_si_no_hubo_exito(tmp_path):
    p = _plan_ok()
    p.pasos[1].estado = "fallido"
    assert reasoning.capture_trajectory(p, projects_dir=tmp_path) is False
    assert not (tmp_path / reasoning.REASONING_SLUG).exists()  # no escribio nada


def test_capture_es_idempotente_por_objetivo(tmp_path):
    reasoning.capture_trajectory(_plan_ok(), projects_dir=tmp_path)
    reasoning.capture_trajectory(_plan_ok(), projects_dir=tmp_path)  # mismo objetivo
    mem = tmp_path / reasoning.REASONING_SLUG / "memory"
    assert len(list(mem.glob("traj-*.md"))) == 1  # actualiza la misma nota, no duplica


# --- recall_trajectories ------------------------------------------------------


def test_recall_vacio_sin_deps_semantic(monkeypatch):
    # Sin el grupo 'semantic' instalado, search() lanza ImportError (numpy/fastembed ausentes)
    # -> recall lo traga y devuelve '' (el planner planea igual que antes).
    from agent.tools import semantic

    def _sin_deps(query, k=5):
        raise ImportError("No module named 'fastembed'")

    monkeypatch.setattr(semantic, "search", _sin_deps)
    assert reasoning.recall_trajectories("lo que sea") == ""


def test_recall_filtra_a_las_trayectorias(tmp_path, monkeypatch):
    # Escribe una trayectoria real y una nota de otro proyecto; el recall solo debe traer la primera.
    reasoning.capture_trajectory(_plan_ok("arregla el proxy"), projects_dir=tmp_path)
    traj = next((tmp_path / reasoning.REASONING_SLUG / "memory").glob("traj-*.md"))

    from agent.tools import semantic

    def _fake_search(query, k=5):
        return [
            (0.9, reasoning.REASONING_SLUG, traj.stem, str(traj)),
            (0.8, "otro_proyecto", "nota-ajena", str(tmp_path / "x.md")),
        ]

    monkeypatch.setattr(semantic, "search", _fake_search)
    out = reasoning.recall_trajectories("arregla el proxy", k=3)
    assert "TRAYECTORIAS EXITOSAS PREVIAS" in out
    assert "arregla el proxy" in out.lower()
    assert "nota-ajena" not in out  # filtrada: no es una trayectoria


def test_recall_vacio_si_no_hay_trayectorias(monkeypatch):
    from agent.tools import semantic

    monkeypatch.setattr(semantic, "search", lambda query, k=5: [(0.7, "repo_x", "nota", "/x.md")])
    assert reasoning.recall_trajectories("algo") == ""  # ningun hit es del slug de trayectorias
