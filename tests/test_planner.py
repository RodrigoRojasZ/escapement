"""Tests del planner (Fase A): parseo del roadmap + persistencia. Sin LLM real."""

from agent import planner
from agent.planner import Plan, Step, _parse

_JSON = (
    '{"criterio_global": "worker/ con 100% type hints y docstrings",'
    ' "pasos": ['
    '  {"id": 1, "accion": "medir cobertura", "tipo": "investigar", "done": "reporte de %", "depende_de": []},'
    '  {"id": 2, "accion": "añadir hints", "tipo": "editar", "done": "mypy pasa", "depende_de": [1]}'
    " ]}"
)


def test_parse_roadmap_valido():
    p = _parse("Aquí tienes: " + _JSON + " listo", "documenta worker/")
    assert p.objetivo == "documenta worker/"
    assert p.criterio_global == "worker/ con 100% type hints y docstrings"
    assert len(p.pasos) == 2
    assert p.pasos[0].tipo == "investigar"
    assert p.pasos[1].depende_de == [1]


def test_parse_asigna_persona_a_pasos_de_codigo():
    # El planner separa el trabajo y asigna un experto por paso; se normaliza a minúsculas.
    p = _parse(
        '{"criterio_global": "x", "pasos": ['
        '  {"id": 1, "accion": "mapear", "tipo": "investigar", "done": "d", "persona": ""},'
        '  {"id": 2, "accion": "cifrar tokens", "tipo": "editar", "done": "d", "persona": "Seguridad"}'
        " ]}",
        "obj",
    )
    assert p.pasos[0].persona == ""  # investigar -> sin asignar (auto-ruteo)
    assert p.pasos[1].persona == "seguridad"


def test_parse_persona_ausente_es_vacia():
    # Planes viejos / pasos sin 'persona' -> '' (backward compatible, auto-ruteo).
    p = _parse(_JSON, "obj")
    assert all(s.persona == "" for s in p.pasos)


def test_parse_basura_no_revienta():
    p = _parse("no hay json aquí", "obj")
    assert p.pasos == [] and p.objetivo == "obj"


def test_parse_ignora_pasos_malformados():
    p = _parse(
        '{"criterio_global": "x", "pasos": ["basura", {"id": 1, "accion": "a", "tipo": "crear", "done": "d"}]}',
        "obj",
    )
    assert len(p.pasos) == 1 and p.pasos[0].accion == "a"


def test_plan_usa_el_executor(monkeypatch):
    monkeypatch.setattr(planner.executors, "run_agent", lambda *a, **k: (True, _JSON))
    p = planner.plan("documenta worker/")
    assert len(p.pasos) == 2 and p.objetivo == "documenta worker/"


def _captura_model(monkeypatch):
    """Mockea run_agent capturando el kwarg ``model`` con que se despacha el planner."""
    capturado = {}

    def _fake(prompt, *a, **k):
        capturado["model"] = k.get("model")
        return (True, _JSON)

    monkeypatch.setattr(planner.executors, "run_agent", _fake)
    return capturado


def test_plan_usa_model_planner_por_defecto(monkeypatch):
    # el planner corre SIEMPRE en MODEL_PLANNER (online medio, sólido), no en el default del CLI.
    cap = _captura_model(monkeypatch)
    planner.plan("obj")
    assert cap["model"] == planner.config.MODEL_PLANNER


def test_plan_es_inmune_al_tiering(monkeypatch):
    # aunque el tiering esté ON, el planner NO se rutea por _TIER_POR_TIPO: queda en MODEL_PLANNER.
    monkeypatch.setattr(planner.config, "MODEL_TIERING", True)
    cap = _captura_model(monkeypatch)
    planner.plan("obj")
    assert cap["model"] == planner.config.MODEL_PLANNER


def test_plan_respeta_executor_model(monkeypatch):
    # EXECUTOR_MODEL (override manual de debugging end-to-end) gana sobre MODEL_PLANNER.
    monkeypatch.setattr(planner.config, "EXECUTOR_MODEL", "claude-opus-5")
    cap = _captura_model(monkeypatch)
    planner.plan("obj")
    assert cap["model"] == "claude-opus-5"


def test_plan_adjunta_context_al_prompt(monkeypatch):
    # R2: el contexto del ReasoningBank se ADJUNTA al prompt (default '' = comportamiento previo).
    capturado = {}

    def _fake(prompt, *a, **k):
        capturado["prompt"] = prompt
        return (True, _JSON)

    monkeypatch.setattr(planner.executors, "run_agent", _fake)
    planner.plan("documenta worker/", context="\n\nTRAYECTORIA PREVIA: usa mypy")
    assert capturado["prompt"].endswith("TRAYECTORIA PREVIA: usa mypy")
    # sin context el prompt no arrastra el bloque
    planner.plan("documenta worker/")
    assert "TRAYECTORIA PREVIA" not in capturado["prompt"]


def test_save_load_plan_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    original = Plan("obj", "done global", [Step(1, "a", "crear", "existe", [])], repo="G:/x")
    planner.save_plan(original)
    cargado = planner.load_plan()
    assert cargado.objetivo == "obj"
    assert cargado.criterio_global == "done global"
    assert cargado.repo == "G:/x"  # el plan recuerda el repo (Fase B)
    assert cargado.pasos[0].accion == "a" and cargado.pasos[0].tipo == "crear"


def test_load_plan_sin_archivo_es_none(tmp_path, monkeypatch):
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "noexiste.json")
    assert planner.load_plan() is None


def test_save_load_round_trip_conserva_veredicto_eval(tmp_path, monkeypatch):
    # E2 (deuda #7): el veredicto y los gaps sobreviven la reanudación (insumo de la replanificación)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    original = Plan("obj", "g", [Step(1, "a", "crear", "d", [])])
    original.eval_veredicto, original.eval_gaps = "incompleto", ["falta doc", "faltan tests"]
    original.replan_ciclos = 1
    planner.save_plan(original)
    cargado = planner.load_plan()
    assert cargado.eval_veredicto == "incompleto"
    assert cargado.eval_gaps == ["falta doc", "faltan tests"]
    assert cargado.replan_ciclos == 1  # E2 pieza 2: reanudar no resetea el tope de ciclos


def test_load_plan_json_previo_a_e2_usa_defaults_sin_evaluar(tmp_path, monkeypatch):
    # un plan guardado antes de E2 no trae eval_veredicto/eval_gaps: debe cargar como 'sin evaluar'
    import json

    archivo = tmp_path / "plan.json"
    monkeypatch.setattr(planner.config, "PLAN", archivo)
    viejo = {
        "objetivo": "obj",
        "criterio_global": "g",
        "pasos": [
            {
                "id": 1,
                "accion": "a",
                "tipo": "crear",
                "done": "d",
                "depende_de": [],
                "estado": "pendiente",
                "nota": "",
                "persona": "",
            }
        ],
        "repo": "G:/x",
    }
    archivo.write_text(json.dumps(viejo), encoding="utf-8")
    cargado = planner.load_plan()
    assert cargado.eval_veredicto == "" and cargado.eval_gaps == []
    assert cargado.replan_ciclos == 0  # pieza 2: sin el campo, arranca sin ciclos consumidos
    assert cargado.repo == "G:/x"  # el resto del plan carga como siempre


def test_repo_plan_path_por_slug(monkeypatch, tmp_path):
    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    p = planner.repo_plan_path(r"D:\mi_proyecto")
    assert p.parent == tmp_path and p.name == "plan_D--mi-proyecto.json"
    # el propio repo del agente tampoco es excepción: su plan va por el slug de la ruta
    propio = planner.config.vault_slug(str(planner.config.REPO_ROOT))
    assert planner.repo_plan_path(str(planner.config.REPO_ROOT)).name == f"plan_{propio}.json"


def test_save_load_plan_a_ruta_explicita_no_toca_el_slot_activo(tmp_path, monkeypatch):
    # planes por repo: guardar/cargar en un path dedicado sin pisar config.PLAN (plan.json)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    dedicado = tmp_path / "plan_escapement.json"
    planner.save_plan(Plan("obj-escapement", "g", [Step(1, "a", "investigar", "d", [])]), dedicado)
    assert dedicado.exists() and not planner.config.PLAN.exists()  # no tocó el slot activo
    cargado = planner.load_plan(dedicado)
    assert cargado.objetivo == "obj-escapement"


def test_active_plan_pointer_y_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    # sin puntero -> cae al slot scratch (config.PLAN)
    assert planner.active_plan_path() == tmp_path / "plan.json"
    # apuntar a un archivo por-repo existente
    dedicado = tmp_path / "plan_x.json"
    planner.save_plan(Plan("o", "g", [Step(1, "a", "investigar", "d", [])], repo="x"), dedicado)
    planner.set_active_plan(dedicado)
    assert planner.active_plan_path().resolve() == dedicado.resolve()
    # puntero a algo inexistente -> fallback al scratch (no revienta)
    planner.set_active_plan(tmp_path / "plan_borrado.json")
    assert planner.active_plan_path() == tmp_path / "plan.json"


def _plan(objetivo: str = "o") -> Plan:
    return Plan(
        objetivo, "g", [Step(1, "a", "investigar", "d", []), Step(2, "b", "editar", "d", [1])]
    )


def test_plan_files_scratch_primero_luego_por_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    assert planner.plan_files() == []  # nada guardado aún
    planner.save_plan(_plan(), tmp_path / "plan_z.json")
    planner.save_plan(_plan(), tmp_path / "plan_a.json")
    assert [f.name for f in planner.plan_files()] == ["plan_a.json", "plan_z.json"]  # ordenados
    planner.save_plan(_plan())  # el scratch (config.PLAN)
    assert [f.name for f in planner.plan_files()] == ["plan.json", "plan_a.json", "plan_z.json"]


def test_plan_slot_con_repo_apunta_y_activa(tmp_path, monkeypatch):
    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    slot = planner.plan_slot("mi-repo")
    assert slot == planner.repo_plan_path("mi-repo")
    planner.save_plan(_plan(), slot)  # el puntero solo resuelve a archivos que existen
    assert planner.active_plan_path().resolve() == slot.resolve()  # quedó ACTIVO


def test_plan_slot_sin_repo_sigue_el_activo(tmp_path, monkeypatch):
    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    assert planner.plan_slot() == tmp_path / "plan.json"  # sin puntero: scratch
    dedicado = tmp_path / "plan_x.json"
    planner.save_plan(_plan(), dedicado)
    planner.set_active_plan(dedicado)
    assert planner.plan_slot().resolve() == dedicado.resolve()


def test_set_step_state_muta_el_paso_y_devuelve_none_si_no_existe():
    p = _plan()
    step = planner.set_step_state(p, 2, "hecho", "usé mypy")
    assert step is p.pasos[1] and step.estado == "hecho" and step.nota == "usé mypy"
    # nota None = no tocar la nota previa (marcar estado sin perder el contexto del checkpoint)
    planner.set_step_state(p, 2, "bloqueado")
    assert p.pasos[1].estado == "bloqueado" and p.pasos[1].nota == "usé mypy"
    assert planner.set_step_state(p, 99, "hecho") is None


def test_step_states_son_los_que_el_runner_entiende():
    # 'pendiente' es el único que el runner considera ejecutable: un estado inventado lo dejaría
    # invisible, por eso las tools validan contra esta tupla.
    assert planner.STEP_STATES == ("pendiente", "hecho", "fallido", "bloqueado")


# --- deuda #6: edición estructural del roadmap (quitar / mover / editar) ---------------------
def _plan_encadenado():
    """Cadena 1 <- 2 <- 3, más un 4 que depende de 1 y 2."""
    return Plan(
        "obj",
        "g",
        [
            Step(1, "investigar auth", "investigar", "d1", []),
            Step(2, "escribir adaptador", "editar", "d2", [1]),
            Step(3, "correr tests", "verificar", "d3", [2]),
            Step(4, "documentar", "crear", "d4", [1, 2]),
        ],
    )


def test_remove_step_reconecta_dependencias():
    p = _plan_encadenado()
    quitado = planner.remove_step(p, 2)
    assert quitado is not None and quitado.id == 2
    assert [s.id for s in p.pasos] == [1, 3, 4]  # ids intactos, sin renumerar
    assert p.pasos[1].depende_de == [1]  # el 3 hereda la dependencia del 2
    assert p.pasos[2].depende_de == [1]  # el 4 dependía de 1 y 2 -> dedup a [1]


def test_remove_step_inexistente_devuelve_none_y_no_toca_nada():
    p = _plan_encadenado()
    assert planner.remove_step(p, 99) is None
    assert len(p.pasos) == 4


def test_remove_step_raiz_deja_al_dependiente_sin_dependencias():
    p = _plan_encadenado()
    planner.remove_step(p, 1)
    assert p.pasos[0].id == 2 and p.pasos[0].depende_de == []


def test_move_step_valido_reordena_sin_cambiar_ids():
    p = _plan_encadenado()
    paso = planner.move_step(p, 4, 3)  # el 4 solo depende de 1 y 2: puede ir antes del 3
    assert paso is not None
    assert [s.id for s in p.pasos] == [1, 2, 4, 3]


def test_move_step_que_rompe_topologia_lanza_y_no_muta():
    import pytest

    p = _plan_encadenado()
    with pytest.raises(ValueError, match="depende del"):
        planner.move_step(p, 3, 1)  # el 3 quedaría antes del 2, del que depende
    assert [s.id for s in p.pasos] == [1, 2, 3, 4]  # el plan queda intacto


def test_move_step_posicion_fuera_de_rango_se_recorta():
    p = _plan_encadenado()
    assert planner.move_step(p, 4, 99) is not None  # más allá del final = al final
    assert [s.id for s in p.pasos] == [1, 2, 3, 4]
    assert planner.move_step(p, 99, 1) is None  # id inexistente sigue siendo None


def test_edit_step_solo_toca_la_accion():
    p = _plan_encadenado()
    paso = planner.edit_step(p, 2, "escribir el adaptador OAuth")
    assert paso is not None and paso.accion == "escribir el adaptador OAuth"
    assert paso.tipo == "editar" and paso.done == "d2" and paso.depende_de == [1]
    assert paso.estado == "pendiente" and paso.nota == ""
    assert planner.edit_step(p, 99, "x") is None
