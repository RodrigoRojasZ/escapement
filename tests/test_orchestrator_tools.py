"""Tests de las tools conversacionales del orquestador. Sin SDK live, sin cuota."""

import asyncio

import pytest

from agent import config, ledger
from agent import state
from agent.cancel import RUN
from agent.tools import orchestrator as O


@pytest.fixture(autouse=True)
def _cancel_limpia():
    """`cancel.RUN` es un singleton de proceso: una cancelación colgada teñiría otros tests."""
    RUN.limpiar()
    yield
    RUN.limpiar()


def _call(tool, args):
    """Invoca el handler async de una SdkMcpTool y devuelve su dict de respuesta."""
    return asyncio.run(tool.handler(args))


def test_las_diez_tools_registradas():
    tools = [
        O.estado,
        O.deuda,
        O.optimizar,
        O.vigilar,
        O.trabajar,
        O.revisar_prs,
        O.objetivo,
        O.plan,
        O.ejecutar,
        O.paso,
    ]
    assert {t.name for t in tools} == {
        "estado",
        "deuda",
        "optimizar",
        "vigilar",
        "trabajar",
        "revisar_prs",
        "objetivo",
        "plan",
        "ejecutar",
        "paso",
    }


def test_gate_readonly_en_ring0_acciones_fuera():
    for lectura in ("estado", "deuda", "plan"):
        assert f"mcp__orq__{lectura}" in config.RING0_TOOLS
    for accion in (
        "optimizar",
        "vigilar",
        "trabajar",
        "revisar_prs",
        "objetivo",
        "ejecutar",
        "paso",
    ):
        assert f"mcp__orq__{accion}" not in config.RING0_TOOLS  # piden confirmación
    # Las tools de EFECTO no pueden auto-aprobarse: pasan por el gate (aprobar-una-vez).
    for efecto in ("Bash", "Write", "Edit", "PowerShell"):
        assert efecto not in config.RING0_TOOLS


def test_estado_reporta_formato(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.config, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(state, "STATE", tmp_path / "state.json")
    _aisla_planes(tmp_path, monkeypatch)
    txt = _call(O.estado, {})["content"][0]["text"]
    assert "Cola:" in txt and "Ledger:" in txt and "PRs:" in txt
    assert "Plan: ninguno activo" in txt  # sin plan, lo dice en vez de callarlo


def test_estado_abre_con_el_plan_activo(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.config, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(state, "STATE", tmp_path / "state.json")
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan(estados=("hecho", "bloqueado"))
    txt = _call(O.estado, {})["content"][0]["text"]
    primera = txt.splitlines()[0]
    assert "migrar el reporter" in primera and "1/2 pasos hechos" in primera
    assert "checkpoint pendiente en el paso 2" in primera


def test_estado_marca_el_roadmap_completo(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger.config, "LEDGER", tmp_path / "ledger.jsonl")
    monkeypatch.setattr(state, "STATE", tmp_path / "state.json")
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan(estados=("hecho", "hecho"))
    txt = _call(O.estado, {})["content"][0]["text"]
    assert "2/2 pasos hechos" in txt and "roadmap completo" in txt


def test_optimizar_es_wrapper_de_optimize(tmp_path, monkeypatch):
    from agent import orchestrator

    fake = orchestrator.Result(
        branch="b",
        dispatched=True,
        tests_ok=True,
        verified=True,
        verdict="ok",
        diff_stat="",
        summary="",
        pr_url="http://pr/1",
    )
    monkeypatch.setattr(orchestrator, "git_root", lambda p: tmp_path)
    monkeypatch.setattr(orchestrator, "optimize", lambda *a, **k: fake)
    txt = _call(O.optimizar, {"repo": str(tmp_path), "target": "a.py"})["content"][0]["text"]
    assert "SEGURO" in txt and "http://pr/1" in txt


def test_revisar_prs_sin_cambios(tmp_path, monkeypatch):
    from agent import orchestrator

    monkeypatch.setattr(orchestrator, "review_prs", lambda: [])
    txt = _call(O.revisar_prs, {})["content"][0]["text"]
    assert "Sin cambios" in txt


# --- Familia plan: objetivo / plan / ejecutar / paso (planner+runner por conversación) -----------


def _aisla_planes(tmp_path, monkeypatch):
    """Manda DATA_DIR y el slot scratch a tmp_path: los planes de los tests no tocan los reales."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "PLAN", tmp_path / "plan.json")


def _guarda_plan(objetivo="migrar el reporter", repo="G:/rep", estados=("pendiente", "pendiente")):
    """Guarda un plan de 2 pasos con los estados dados en el archivo del repo, y lo deja activo."""
    from agent import planner

    pasos = [
        planner.Step(1, "mapear el módulo", "investigar", "hay mapa", []),
        planner.Step(2, "aplicar el cambio", "editar", "tests pasan", [1], persona="refactor"),
    ]
    for s, e in zip(pasos, estados, strict=False):
        s.estado = e
    p = planner.Plan(objetivo, "el reporter corre sobre la nueva API", pasos, repo=repo)
    path = planner.repo_plan_path(repo)
    planner.save_plan(p, path)
    planner.set_active_plan(path)
    return p, path


def test_objetivo_guarda_el_plan_del_repo_y_lo_deja_activo(tmp_path, monkeypatch):
    from agent import planner, reasoning

    _aisla_planes(tmp_path, monkeypatch)
    monkeypatch.setattr(reasoning, "recall_trajectories", lambda meta: "")
    monkeypatch.setattr(
        planner,
        "plan",
        lambda meta, context="": planner.Plan(
            meta, "criterio global", [planner.Step(1, "medir", "investigar", "hay reporte", [])]
        ),
    )
    monkeypatch.setattr(config, "REPOS", {"rep": "G:/rep"})
    txt = _call(O.objetivo, {"meta": "documenta el reporter", "repo": "rep"})["content"][0]["text"]
    destino = planner.repo_plan_path("G:/rep")
    assert destino.exists() and planner.active_plan_path().resolve() == destino.resolve()
    assert planner.load_plan(destino).repo == "G:/rep"  # el plan recuerda su repo
    assert "documenta el reporter" in txt and "criterio global" in txt and "1. [investigar]" in txt


def test_objetivo_sin_meta_pide_la_meta(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    assert "Falta 'meta'" in _call(O.objetivo, {"meta": "  "})["content"][0]["text"]


def test_objetivo_sin_roadmap_no_promete_ejecucion(tmp_path, monkeypatch):
    from agent import planner, reasoning

    _aisla_planes(tmp_path, monkeypatch)
    monkeypatch.setattr(reasoning, "recall_trajectories", lambda meta: "")
    monkeypatch.setattr(planner, "plan", lambda meta, context="": planner.Plan(meta, "?", []))
    txt = _call(O.objetivo, {"meta": "algo imposible"})["content"][0]["text"]
    assert "No pude generar un roadmap" in txt


def test_plan_es_solo_lectura_y_muestra_avance_y_checkpoint(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan(estados=("hecho", "bloqueado"))
    txt = _call(O.plan, {})["content"][0]["text"]
    assert "migrar el reporter" in txt and "1/2 pasos hechos" in txt
    assert "✓ 1." in txt and "⏸ 2." in txt and "@refactor" in txt
    assert "Checkpoint pendiente: paso 2" in txt


def test_plan_recorta_un_roadmap_verboso_a_una_linea_por_paso(tmp_path, monkeypatch):
    # Los planes reales traen acciones/criterios de cientos de chars y hasta 40 pasos: sin recorte
    # un solo 'plan' inundaría el turno (e sería impronunciable por voz).
    from agent import planner

    _aisla_planes(tmp_path, monkeypatch)
    largo = "x" * 900
    pasos = [planner.Step(i, largo, "editar", largo, []) for i in range(1, 41)]
    p = planner.Plan(largo, largo, pasos, repo="G:/rep")
    path = planner.repo_plan_path("G:/rep")
    planner.save_plan(p, path)
    planner.set_active_plan(path)

    txt = _call(O.plan, {})["content"][0]["text"]
    cuerpo = [ln for ln in txt.splitlines() if ln.startswith(" ·")]
    assert len(cuerpo) == 40  # un paso = una línea, sin multilínea
    assert all(len(ln) < 400 for ln in cuerpo) and len(txt) < 20_000
    assert "…" in txt  # se ve que está recortado, no truncado en silencio
    assert planner.load_plan(path).pasos[0].accion == largo  # el archivo NO se toca


def test_plan_no_modifica_el_archivo(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    _p, path = _guarda_plan()
    antes = path.read_text(encoding="utf-8")
    _call(O.plan, {})
    assert path.read_text(encoding="utf-8") == antes  # read-only de verdad (está en el Anillo 0)


def test_plan_sin_plan_lista_los_que_existen(tmp_path, monkeypatch):
    from agent import planner

    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan(repo="G:/otro")
    monkeypatch.setattr(planner, "active_plan_path", lambda: tmp_path / "plan_vacio.json")
    txt = _call(O.plan, {})["content"][0]["text"]
    assert "No hay plan en plan_vacio.json" in txt and "plan_G--otro.json" in txt


def test_ejecutar_sin_plan_manda_a_objetivo(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    txt = _call(O.ejecutar, {})["content"][0]["text"]
    assert "No hay plan" in txt and "'objetivo'" in txt


def test_ejecutar_sin_repo_no_cae_al_cwd(tmp_path, monkeypatch):
    # el daemon de voz vive en HOME: montar el worktree del plan ahí sería un accidente.
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan(repo="")
    txt = _call(O.ejecutar, {})["content"][0]["text"]
    assert "no tiene repo asociado" in txt


def test_ejecutar_corre_el_runner_y_reporta_el_checkpoint(tmp_path, monkeypatch):
    from agent import runner

    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()
    llamadas = {}

    def _fake_run(p, repo, **kw):
        llamadas["repo"] = repo
        llamadas["plan_path"] = kw["plan_path"]
        p.pasos[0].estado = "hecho"
        p.pasos[1].estado = "bloqueado"
        p.pasos[1].nota = "necesito saber a qué versión de la API migrar"

    monkeypatch.setattr(runner, "run_plan", _fake_run)
    txt = _call(O.ejecutar, {})["content"][0]["text"]
    assert llamadas["repo"] == "G:/rep" and llamadas["plan_path"].name == "plan_G--rep.json"
    assert "1/2 pasos hechos" in txt
    assert "Checkpoint en el paso 2" in txt and "versión de la API" in txt
    assert "'paso'" in txt  # dice cómo desbloquearlo


def test_ejecutar_roadmap_completo_reporta_traza(tmp_path, monkeypatch):
    from agent import runner

    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()
    monkeypatch.setattr(
        runner,
        "run_plan",
        lambda p, repo, **kw: [setattr(s, "estado", "hecho") for s in p.pasos],
    )
    monkeypatch.setattr(runner, "archivos_del_plan", lambda p: ["src/a.py", "src/b.py"])
    monkeypatch.setattr(runner, "arbol_archivos", lambda rutas: "src/\n  a.py\n  b.py")
    monkeypatch.setattr(runner, "default_branch", lambda repo: "main")
    txt = _call(O.ejecutar, {})["content"][0]["text"]
    assert "Roadmap completo ✓" in txt and "a.py" in txt
    assert "escapement ejecutar" in txt  # el rescate a rama se hace en terminal (pide input())


def test_ejecutar_rechaza_una_segunda_corrida_en_paralelo(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()

    async def _con_lock_tomado():
        async with O._RUN_LOCK:  # simula la corrida en curso
            return await O.ejecutar.handler({})

    txt = asyncio.run(_con_lock_tomado())["content"][0]["text"]
    assert "en curso" in txt


def test_segunda_corrida_con_cancelacion_pedida_lo_dice(tmp_path, monkeypatch):
    """Rebote distinto: si ya pediste parar, el mensaje explica que está terminando, no que sigue."""
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()
    RUN.pedir("cortaste el turno con F2")

    async def _con_lock_tomado():
        async with O._RUN_LOCK:
            return await O.ejecutar.handler({})

    txt = asyncio.run(_con_lock_tomado())["content"][0]["text"]
    assert "cancelación" in txt and "terminando el paso" in txt


def test_ejecutar_pasa_la_señal_cooperativa_al_runner(tmp_path, monkeypatch):
    """F2 debe cortar el trabajo, no solo el turno: el runner consulta `cancel.RUN`."""
    from agent import runner

    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()
    visto = {}

    def _fake_run(p, repo, **kw):
        visto["antes"] = kw["should_cancel"]()  # arranca sin cancelación pendiente
        RUN.pedir("cortaste el turno con F2")
        visto["despues"] = kw["should_cancel"]()

    monkeypatch.setattr(runner, "run_plan", _fake_run)
    RUN.pedir("F2 de un turno viejo, sin corrida")  # no puede matar la corrida que recién empieza
    _call(O.ejecutar, {})

    assert visto == {"antes": False, "despues": True}


def test_reporte_avisa_cuando_la_corrida_quedo_cancelada(tmp_path, monkeypatch):
    from agent import runner

    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()

    def _fake_run(p, repo, **kw):
        p.pasos[0].estado = "hecho"
        RUN.pedir("cortaste el turno con F2")
        p.pasos[1].estado = "bloqueado"
        p.pasos[1].nota = "corrida cancelada a tu pedido antes de despachar este paso"

    monkeypatch.setattr(runner, "run_plan", _fake_run)
    txt = _call(O.ejecutar, {})["content"][0]["text"]
    assert "CANCELADA a tu pedido" in txt and "cortaste el turno con F2" in txt
    assert "Retómala con 'ejecutar'" in txt


def test_reporte_normal_no_habla_de_cancelacion(tmp_path, monkeypatch):
    from agent import runner

    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()
    monkeypatch.setattr(
        runner, "run_plan", lambda p, repo, **kw: setattr(p.pasos[1], "estado", "bloqueado")
    )
    txt = _call(O.ejecutar, {})["content"][0]["text"]
    assert "CANCELADA" not in txt


def test_paso_marca_el_checkpoint_y_persiste(tmp_path, monkeypatch):
    from agent import planner

    _aisla_planes(tmp_path, monkeypatch)
    _p, path = _guarda_plan(estados=("hecho", "bloqueado"))
    txt = _call(O.paso, {"id": 2, "estado": "hecho", "nota": "migrar a la v3"})["content"][0][
        "text"
    ]
    guardado = planner.load_plan(path)
    assert guardado.pasos[1].estado == "hecho" and guardado.pasos[1].nota == "migrar a la v3"
    assert "Paso 2 -> hecho" in txt and "migrar a la v3" in txt


def test_paso_rechaza_estado_inventado(tmp_path, monkeypatch):
    from agent import planner

    _aisla_planes(tmp_path, monkeypatch)
    _p, path = _guarda_plan(estados=("hecho", "bloqueado"))
    txt = _call(O.paso, {"id": 2, "estado": "listo"})["content"][0]["text"]
    assert "Estado inválido" in txt
    # y no tocó el plan: un estado fuera de STEP_STATES dejaría el paso invisible para el runner
    assert planner.load_plan(path).pasos[1].estado == "bloqueado"


def test_paso_id_ausente_o_inexistente(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    _guarda_plan()
    assert "Falta 'id'" in _call(O.paso, {"estado": "hecho"})["content"][0]["text"]
    txt = _call(O.paso, {"id": 99, "estado": "hecho"})["content"][0]["text"]
    assert "no tiene un paso 99" in txt


def test_paso_sin_plan_activo(tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    txt = _call(O.paso, {"id": 1, "estado": "hecho"})["content"][0]["text"]
    assert "No hay plan activo" in txt
