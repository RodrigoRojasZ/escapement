"""Tests del runner del roadmap (Fase B): orden topológico, checkpoints, reanudación. Sin efectos."""

import pytest

from agent import runner
from agent.planner import Plan, Step
from agent.runner import BLOQUEADO, HECHO, run_plan


@pytest.fixture(autouse=True)
def _local_off(monkeypatch):
    """Aísla el runner del backend local (requisito 'tests sin Ollama').

    Sin esto, con Ollama corriendo en la máquina de dev los pasos de razonamiento
    (reflexionar/swarm/preguntar) que ``route_step`` mande a ``local`` irían al modelo REAL, y los
    tests que solo mockean ``run_agent`` se volverían no deterministas. Forzar ``available()`` a
    ``False`` hace que ``_dispatch_reason`` caiga siempre al ``run_agent`` mockeado. Los tests que
    ejercen el path LOCAL reactivan ``available`` con su propio ``monkeypatch`` (gana el último).
    """
    monkeypatch.setattr(runner.local_models, "available", lambda: False)


@pytest.fixture(autouse=True)
def _eval_off(monkeypatch):
    """Apaga la evaluación global al cerrar (E2, deuda #7) para el resto de la suite.

    ``_plan()`` trae ``criterio_global`` no vacío, así que con el default (``PLAN_EVAL=True``)
    cada test que completa el roadmap dispararía ``_evaluar_plan`` -> ``_dispatch_reason`` ->
    ``executors.run_agent`` REAL (con ``_local_off`` el fallback online siempre se alcanza).
    Apaga también la replanificación (E2 pieza 2): un veredicto 'incompleto' mockeado dispararía
    ``_replanificar`` -> dispatch REAL con el default (``PLAN_REPLAN_MAX=2``).
    Los tests de la sección E2 reactivan los flags con su propio ``monkeypatch`` (gana el último).
    """
    monkeypatch.setattr(runner.config, "PLAN_EVAL", False)
    monkeypatch.setattr(runner.config, "PLAN_REPLAN_MAX", 0)


def _plan():
    return Plan(
        "obj",
        "done global",
        [
            Step(1, "investiga worker", "investigar", "d1", []),
            Step(2, "edita algo", "editar", "d2", [1]),
            Step(3, "verifica", "verificar", "d3", [2]),
        ],
    )


def test_ready_respeta_dependencias():
    p = _plan()
    assert [s.id for s in runner._ready(p)] == [1]  # solo el 1 (sin deps)
    p.pasos[0].estado = HECHO
    assert [s.id for s in runner._ready(p)] == [2]  # el 2 se habilita al completarse el 1


def test_pasos_listos_es_el_frente_publico(monkeypatch):
    # superficie pública de _ready para la vista del CLI: mismo frente, sin mutar el plan
    p = _plan()
    assert [s.id for s in runner.pasos_listos(p)] == [1]
    p.pasos[0].estado = BLOQUEADO
    assert runner.pasos_listos(p) == []  # todo lo pendiente espera a un paso trabado
    p.pasos[0].estado = HECHO
    p.pasos[1].estado = HECHO
    p.pasos[2].estado = HECHO
    assert runner.pasos_listos(p) == []  # plan completo


def test_run_para_en_checkpoint():
    p = _plan()
    handlers = {
        "investigar": lambda s, pl, r: (HECHO, "ok"),
        "editar": lambda s, pl, r: (HECHO, "PR"),
        "verificar": lambda s, pl, r: (BLOQUEADO, "corre mypy"),
    }
    run_plan(p, "repo", handlers=handlers, persist=False)
    assert [s.estado for s in p.pasos] == [HECHO, HECHO, BLOQUEADO]


def test_run_completa_si_no_hay_checkpoint():
    p = _plan()
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}
    run_plan(p, "repo", handlers=todo_ok, persist=False)
    assert all(s.estado == HECHO for s in p.pasos)


def test_on_complete_se_llama_solo_en_exito_total():
    # R2: el hook post-task corre UNA vez y solo si el roadmap quedó entero en 'hecho'.
    p = _plan()
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}
    llamado = []
    run_plan(p, "repo", handlers=todo_ok, on_complete=lambda pl: llamado.append(pl), persist=False)
    assert llamado == [p]  # exactamente una vez, con el plan


def test_on_complete_no_se_llama_en_checkpoint():
    p = _plan()
    handlers = {
        "investigar": lambda s, pl, r: (HECHO, "ok"),
        "editar": lambda s, pl, r: (BLOQUEADO, "para"),
    }
    llamado = []
    run_plan(p, "repo", handlers=handlers, on_complete=lambda pl: llamado.append(pl), persist=False)
    assert llamado == []  # hubo checkpoint -> no fue exito -> no captura


def test_on_complete_fallido_no_tumba_la_corrida():
    # best-effort: si el hook lanza, la corrida ya exitosa no revienta.
    p = _plan()
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}

    def _boom(pl):
        raise RuntimeError("fallo del hook")

    run_plan(p, "repo", handlers=todo_ok, on_complete=_boom, persist=False)
    assert all(s.estado == HECHO for s in p.pasos)  # completó pese al hook roto


def test_run_reanuda_desde_donde_quedo():
    p = _plan()
    p.pasos[0].estado = HECHO
    p.pasos[1].estado = HECHO
    visitados = []
    handlers = {"verificar": lambda s, pl, r: (visitados.append(s.id), (HECHO, "ok"))[1]}
    run_plan(p, "repo", handlers=handlers, persist=False)
    assert visitados == [3]  # no re-ejecuta 1 y 2 (ya 'hecho')


def test_run_plan_persiste_en_plan_path_dedicado(tmp_path, monkeypatch):
    # ejecución por-repo: run_plan persiste en el archivo del repo, no en el slot activo (config.PLAN)
    from agent import planner

    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    dedicado = tmp_path / "plan_repo.json"
    p = Plan("o", "g", [Step(1, "a", "investigar", "d", [])])
    run_plan(p, "repo", handlers={"investigar": lambda s, pl, r: (HECHO, "ok")}, plan_path=dedicado)
    assert dedicado.exists()  # persistió en el archivo dedicado
    assert not (tmp_path / "plan.json").exists()  # no tocó el slot activo


def test_target_detecta_archivo():
    assert (
        runner._target(Step(1, "edita worker/modules/health_check.py", "editar", "d"))
        == "worker/modules/health_check.py"
    )
    assert runner._target(Step(1, "agrega hints a todo", "editar", "d")) is None


def test_preguntar_para_si_hay_divergencia(monkeypatch):
    # 'preguntar' evalúa: si el agente detecta divergencia real -> checkpoint (bloquea).
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: (True, "DIVERGENCIA: borrar 20 notas")
    )
    p = Plan("o", "g", [Step(1, "¿reestructurar el vault?", "preguntar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == BLOQUEADO


def test_preguntar_sigue_si_no_hay_divergencia(monkeypatch):
    # si NO hay divergencia -> el agente procede solo (autónomo), no molesta al usuario.
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: (True, "SEGUIR: sin divergencia, procedo")
    )
    p = Plan("o", "g", [Step(1, "¿reestructurar el vault?", "preguntar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == HECHO


def test_senal_divergencia_tolera_preambulo_y_prosa():
    # el marcador se detecta tras un preámbulo/markdown (recupera ahorro: el local ya no cae a Claude)
    assert runner._senal_divergencia("DIVERGENCIA: borrar todo") == "DIVERGENCIA"
    assert runner._senal_divergencia("Claro. DIVERGENCIA: borrar todo") == "DIVERGENCIA"
    assert runner._senal_divergencia("**SEGUIR:** procedo") == "SEGUIR"
    # 'divergencia' suelta en prosa (sin ':') NO es el marcador: gana el SEGUIR: real
    assert runner._senal_divergencia("No hay divergencia real, SEGUIR: procedo") == "SEGUIR"
    assert runner._senal_divergencia("texto sin marcador") is None
    assert runner._senal_divergencia("") is None


def test_preguntar_bloquea_pese_a_preambulo_antes_de_divergencia(monkeypatch):
    # correctitud: la salida del fallback (Claude) NO pasa por 'validate'; un preámbulo antes de
    # 'DIVERGENCIA:' antes se leía como SEGUIR (checkpoint saltado). Ahora bloquea igual.
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda *a, **k: (True, "Por supuesto. DIVERGENCIA: hay que decidir el rumbo"),
    )
    p = Plan("o", "g", [Step(1, "¿reestructurar el vault?", "preguntar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == BLOQUEADO


def test_preguntar_acepta_seguir_del_local_con_preambulo(monkeypatch):
    # ahorro: el local responde con preámbulo antes de 'SEGUIR:'; se acepta sin caer a Claude
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("local", "")
    )
    monkeypatch.setattr(runner.local_models, "available", lambda: True)
    monkeypatch.setattr(
        runner.local_models,
        "complete",
        lambda user, system="", **k: "Entendido. SEGUIR: no hay divergencia, procedo",
    )
    llamadas = []
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: llamadas.append(1) or (True, "cli")
    )
    p = Plan("o", "g", [Step(1, "¿reestructurar?", "preguntar", "d", [])])
    estado, _ = runner._h_preguntar(p.pasos[0], p, "repo")
    assert estado == HECHO
    assert not llamadas  # el preámbulo ya no fuerza un dispatch a Claude


def test_mecanico_es_autonomo_no_bloquea(monkeypatch):
    # editar-genérico / ejecutar / verificar se EJECUTAN solos (dispatch), no hacen checkpoint.
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "VERIFICADO listo"))
    p = Plan(
        "o",
        "g",
        [
            Step(1, "agrega hints a todo el repo", "editar", "d", []),  # sin target concreto
            Step(2, "corre ruff format", "ejecutar", "ruff pasa", [1]),
            Step(3, "verifica los tests", "verificar", "pytest pasa", [2]),
        ],
    )
    run_plan(p, "repo", persist=False)
    assert [s.estado for s in p.pasos] == [HECHO, HECHO, HECHO]


def test_context_incluye_notas_de_dependencias():
    p = Plan(
        "o",
        "g",
        [
            Step(1, "q", "preguntar", "d", [], estado="hecho", nota="solo webhooks bajo modules/"),
            Step(2, "investiga", "investigar", "d", [1]),
        ],
    )
    assert "solo webhooks bajo modules/" in runner._context(p.pasos[1], p)
    assert runner._context(p.pasos[0], p) == ""  # sin dependencias -> sin contexto


def test_investigar_inyecta_contexto_de_deps(monkeypatch):
    p = Plan(
        "o",
        "g",
        [
            Step(1, "q", "preguntar", "d", [], estado="hecho", nota="alcance: solo modules/"),
            Step(2, "investiga X", "investigar", "d", [1]),
        ],
    )
    capturado = {}

    def fake_run_agent(prompt, **k):
        capturado["prompt"] = prompt
        return True, "ok"

    monkeypatch.setattr(runner.executors, "run_agent", fake_run_agent)
    run_plan(p, "repo", persist=False)
    assert "alcance: solo modules/" in capturado["prompt"]  # la nota del paso 1 llegó al prompt


# --- Handler de memoria (Fase B: compilar/organizar notas) ---

_MEM_JSON = (
    '{"notas": [{"action": "create", "project": "proj", "name": "arq-x", "title": "Arq",'
    ' "type": "project", "description": "resumen", "body": "el cuerpo"}, "basura-ignorada"]}'
)


def test_parse_memoria():
    notas = runner._parse_memoria("aquí tienes: " + _MEM_JSON)
    assert len(notas) == 1 and notas[0]["name"] == "arq-x"  # la entrada no-dict se descarta
    assert runner._parse_memoria("sin json") == []


def test_aplicar_notas_escribe_e_indexa(tmp_path):
    base = tmp_path / "projects"
    (base / "proj").mkdir(parents=True)
    nota = {
        "action": "create",
        "project": "proj",
        "name": "arq-x",
        "title": "Arq",
        "type": "project",
        "description": "resumen",
        "body": "el cuerpo",
    }
    assert runner._aplicar_notas([nota], projects_dir=base) == 1
    md = base / "proj" / "memory" / "arq-x.md"
    assert md.exists() and "el cuerpo" in md.read_text(encoding="utf-8")
    assert "arq-x.md" in (base / "proj" / "memory" / "MEMORY.md").read_text(encoding="utf-8")


def test_aplicar_notas_borra(tmp_path):
    from agent.tools import memory

    base = tmp_path / "projects"
    mem = base / "proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "vieja.md").write_text(
        memory.build_note("vieja", "d", "reference", "b"), encoding="utf-8"
    )
    assert (
        runner._aplicar_notas(
            [{"action": "delete", "project": "proj", "name": "vieja"}], projects_dir=base
        )
        == 1
    )
    assert not (mem / "vieja.md").exists()


def test_aplicar_notas_archiva(tmp_path):
    from agent.tools import memory

    base = tmp_path / "projects"
    mem = base / "proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "caso.md").write_text(memory.build_note("caso", "d", "reference", "b"), encoding="utf-8")
    n = runner._aplicar_notas(
        [{"action": "archive", "project": "proj", "name": "caso"}], projects_dir=base
    )
    assert n == 1
    assert not (mem / "caso.md").exists()  # movida, no borrada
    assert (base / "proj-archivo" / "memory" / "caso.md").exists()


def test_aplicar_notas_fuerza_project_no_dispersa(tmp_path):
    # El fix de scatter: aunque el archivista adivine carpetas distintas, todas van al vault forzado.
    base = tmp_path / "projects"
    (base / "correcto").mkdir(parents=True)
    notas = [
        {
            "action": "create",
            "project": "adivinado-mal",  # el archivista se equivoca de carpeta...
            "name": "arq-x",
            "title": "Arq",
            "type": "project",
            "description": "d",
            "body": "b",
        }
    ]
    assert runner._aplicar_notas(notas, projects_dir=base, project="correcto") == 1
    assert (base / "correcto" / "memory" / "arq-x.md").exists()  # ...pero se escribe en el forzado
    assert not (base / "adivinado-mal").exists()  # la carpeta adivinada nunca se crea


def test_h_memoria_flujo_completo(tmp_path, monkeypatch):
    from agent import config
    from agent.tools import memory

    base = tmp_path / "projects"
    repo = str(tmp_path / "mi_repo")
    slug = config.vault_slug(repo)  # _h_memoria FUERZA este vault (no el 'proj' del JSON)
    monkeypatch.setattr(memory.config, "PROJECTS_DIR", base)  # _aplicar_notas escribe aquí
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, _MEM_JSON))
    p = Plan("o", "g", [Step(1, "compila memoria del repo X", "memoria", "notas al día", [])])
    run_plan(p, repo, persist=False)
    assert p.pasos[0].estado == HECHO
    assert (base / slug / "memory" / "arq-x.md").exists()  # forzado al vault del repo
    assert not (base / "proj").exists()  # el 'proj' que adivinó el JSON se ignoró


def test_h_memoria_sin_notas_es_hecho(monkeypatch):
    # 0 notas (nada que crear/borrar) NO bloquea: es HECHO, para no romper la autonomía.
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, '{"notas": []}'))
    p = Plan("o", "g", [Step(1, "borra duplicados si los hay", "memoria", "sin duplicados", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == HECHO


def test_verificar_ignora_fallo_en_el_analisis(monkeypatch):
    # El análisis menciona 'fallos' pero termina con VERIFICADO -> HECHO (no falso fallido).
    out = "Analicé los posibles fallos y no arrojó candidatos.\n\nVERIFICADO"
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, out))
    p = Plan("o", "g", [Step(1, "verifica", "verificar", "consistencia", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == HECHO


# --- Deuda #12: el verificador no puede editar lo que audita (si lo hace, el veredicto no vale) ---


def _plan_verificar(repo):
    """Plan de un solo paso 'verificar' cuyo worktree ES el repo temporal."""
    p = Plan("o", "g", [Step(1, "verifica", "verificar", "los tests pasan", [])])
    p.workdir = str(repo)
    return p


def test_verificar_despacha_sin_poder_escribir(monkeypatch, tmp_path):
    capturado = {}

    def _fake(*a, **k):
        capturado.update(k)
        return True, "VERIFICADO"

    monkeypatch.setattr(runner.executors, "run_agent", _fake)
    p = _plan_verificar(_repo_con_commit(tmp_path))
    assert runner._h_verificar(p.pasos[0], p, str(tmp_path)) == (HECHO, "verificado")
    assert capturado["no_write"] is True  # primera capa: sin tools de edición
    assert capturado["mode"] == "edit"  # pero con shell: una verificación real corre pytest


def test_verificar_descarta_el_veredicto_si_toco_el_arbol(monkeypatch, tmp_path):
    # Pasó en la validación de E2: el verificador escribió él mismo lo que debía revisar.
    repo = _repo_con_commit(tmp_path)

    def _fake(*a, **k):
        (repo / "base.py").write_text("x = 99  # lo 'arreglé' yo\n", encoding="utf-8")
        return True, "Todo en orden.\nVERIFICADO"

    monkeypatch.setattr(runner.executors, "run_agent", _fake)
    p = _plan_verificar(repo)
    estado, nota = runner._h_verificar(p.pasos[0], p, str(repo))
    assert estado == runner.FALLIDO  # el VERIFICADO no se acepta: lo emitió quien tocó el árbol
    assert "MODIFICÓ el árbol que auditaba" in nota
    assert "base.py" in nota  # y dice cuál, para poder revisarlo


def test_verificar_no_se_queja_de_los_untracked_que_siembra_pytest(monkeypatch, tmp_path):
    # Correr la verificación deja .pytest_cache/ y __pycache__/: eso NO es "el verificador editó".
    repo = _repo_con_commit(tmp_path)

    def _fake(*a, **k):
        (repo / ".pytest_cache").mkdir()
        (repo / ".pytest_cache" / "CACHEDIR.TAG").write_text("basura\n", encoding="utf-8")
        return True, "VERIFICADO"

    monkeypatch.setattr(runner.executors, "run_agent", _fake)
    p = _plan_verificar(repo)
    assert runner._h_verificar(p.pasos[0], p, str(repo)) == (HECHO, "verificado")


def test_huella_tracked_ignora_untracked_y_es_none_fuera_de_repo(tmp_path):
    repo = _repo_con_commit(tmp_path)
    antes = runner._huella_tracked(str(repo))
    (repo / "nuevo.md").write_text("hola\n", encoding="utf-8")
    assert runner._huella_tracked(str(repo)) == antes  # un archivo nuevo no mueve la huella...
    (repo / "base.py").write_text("x = 2\n", encoding="utf-8")
    assert runner._huella_tracked(str(repo)) != antes  # ...pero modificar lo tracked sí
    fuera = tmp_path.parent / "sin_git"
    fuera.mkdir(exist_ok=True)
    assert runner._huella_tracked(str(fuera)) is None  # sin git: None -> no se compara, no bloquea


# --- Auto-evolución (Fase C): reflexionar añade pasos ---


def test_parse_pasos():
    j = '{"pasos": [{"accion": "arregla G3", "tipo": "editar", "done": "test pasa"}, "basura"]}'
    pasos = runner._parse_pasos("aquí: " + j)
    assert len(pasos) == 1 and pasos[0]["accion"] == "arregla G3"
    assert runner._parse_pasos("sin json") == []


def test_json_tiene_lista_distingue_vacio_valido_de_basura():
    # vacío BIEN FORMADO ('sin cambios', legítimo) -> True; no se confunde con basura/no-JSON
    assert runner._json_tiene_lista('{"pasos": []}', "pasos") is True
    assert (
        runner._json_tiene_lista('con texto {"pasos": [{"accion": "x"}]} alrededor', "pasos")
        is True
    )
    assert runner._json_tiene_lista("sin json", "pasos") is False
    assert runner._json_tiene_lista('{"otra": 1}', "pasos") is False  # falta la clave
    assert runner._json_tiene_lista('{"pasos": "x"}', "pasos") is False  # la clave no es lista
    assert runner._json_tiene_lista("", "pasos") is False


def test_insertar_pasos_ids_deps_y_anti_bucle():
    p = Plan("o", "g", [Step(1, "reflexiona", "reflexionar", "d", [])])
    n = runner._insertar_pasos(
        p,
        p.pasos[0],
        [
            {"accion": "arregla G3", "tipo": "editar", "done": "d"},
            {"accion": "más reflexión", "tipo": "reflexionar", "done": "d"},  # descartado
        ],
    )
    assert n == 1  # el 'reflexionar' generado se descarta (anti-bucle)
    nuevo = p.pasos[1]
    assert nuevo.id == 2 and nuevo.depende_de == [1] and nuevo.tipo == "editar"


# --- Deuda #13: lo que propone el modelo se sanea antes de entrar al plan ---


def test_insertar_pasos_coerce_el_tipo_desconocido_a_investigar():
    # un typo del modelo no puede frenar la corrida en _h_desconocido -> bloqueado
    p = Plan("o", "g", [Step(1, "reflexiona", "reflexionar", "d", [])])
    n = runner._insertar_pasos(
        p, p.pasos[0], [{"accion": "editar el worker de colas", "tipo": "editorificar", "done": "d"}]
    )
    assert n == 1 and p.pasos[1].tipo == "investigar"
    assert p.pasos[1].accion == "editar el worker de colas"  # la tarea se conserva tal cual


def test_insertar_pasos_descarta_la_accion_que_no_da_para_un_dispatch():
    p = Plan("o", "g", [Step(1, "reflexiona", "reflexionar", "d", [])])
    n = runner._insertar_pasos(
        p,
        p.pasos[0],
        [
            {"accion": "verificar", "tipo": "verificar", "done": "d"},  # el nombre pelado del tipo
            {"accion": "Investigar.", "tipo": "investigar", "done": "d"},  # idem con ruido
            {"accion": "hazlo", "tipo": "editar", "done": "d"},  # más corta que _ACCION_MIN
        ],
    )
    assert n == 0 and len(p.pasos) == 1  # nada que corregir sin inventar la tarea: se descartan


def test_insertar_pasos_sanea_la_tanda_basura_de_la_validacion_e2():
    # Los 4 pasos reales que la auto-evolución insertó en el escenario 1 y hubo que quitar a mano.
    p = Plan("o", "g", [Step(1, "reflexiona", "reflexionar", "d", [])])
    n = runner._insertar_pasos(
        p,
        p.pasos[0],
        [
            {"accion": "revisar el estado del repo", "tipo": "investigar", "done": "d"},
            {"accion": "documentar los helpers de utils", "tipo": "editorificar", "done": "d"},
            {"accion": "verificar", "tipo": "verificar", "done": "d"},
            {"accion": "limpiar duplicados de config", "tipo": "refactor", "done": "d"},
        ],
    )
    assert n == 3  # sobrevive todo menos el que no tenía acción
    assert all(s.tipo in runner.DEFAULT_HANDLERS for s in p.pasos[1:])  # ninguno cae en bloqueado


def test_reflexionar_anade_pasos_al_plan(monkeypatch):
    j = '{"pasos": [{"accion": "arreglar el bug G3", "tipo": "editar", "done": "el test pasa"}]}'
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, j))
    p = Plan(
        "o",
        "g",
        [
            Step(1, "investiga", "investigar", "d", [], estado="hecho", nota="hallé el bug G3"),
            Step(2, "reflexiona", "reflexionar", "d", [1]),
        ],
    )
    run_plan(p, "repo", persist=False)
    assert p.pasos[1].estado == HECHO
    assert any(s.id == 3 and "G3" in s.accion for s in p.pasos)  # el plan creció con lo hallado


def test_reflexionar_acepta_vacio_del_local_sin_fallback(monkeypatch):
    # El local responde {"pasos": []} (legítimo 'sin cambios'): se acepta y NO se gasta un dispatch
    # a Claude para el mismo vacío. Salida observable idéntica a la del fallback: 'sin nuevas mejoras'.
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("local", "")
    )
    monkeypatch.setattr(runner.local_models, "available", lambda: True)
    monkeypatch.setattr(
        runner.local_models, "complete", lambda user, system="", **k: '{"pasos": []}'
    )
    llamadas = []
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: llamadas.append(1) or (True, "cli")
    )
    p = Plan("o", "g", [Step(1, "reflexiona", "reflexionar", "d", [])])
    estado, nota = runner._h_reflexionar(p.pasos[0], p, "repo")
    assert estado == HECHO and nota == "sin nuevas mejoras ni problemas que añadir"
    assert not llamadas  # el vacío válido del local NO cayó a run_agent


# --- Endurecimiento de fallos del run (agnóstico al executor/modelo) ---

# F2: el executor sale con éxito (returncode 0) pero su prosa confiesa que NO aplicó el cambio.


def test_admite_no_completar_detecta_confesiones():
    for txt in (
        "No tengo permiso para escribir el archivo, cópialo y pégalo tú.",
        "permission denied al intentar el Write",
        "No pude crear el archivo, para que lo pegues manualmente.",
        "El cambio no persistió en disco.",
        "no se pudo guardar el archivo destino",
    ):
        assert runner._admite_no_completar(txt), txt


def test_admite_no_completar_detecta_confesiones_en_ingles():
    # 'agnóstico al modelo' también implica agnóstico al idioma: executors que responden en inglés.
    for txt in (
        "I could not write the file, please copy and paste it yourself.",
        "I was unable to persist the changes (read-only filesystem).",
        "failed to apply the patch",
        "the write was denied by permissions",
    ):
        assert runner._admite_no_completar(txt), txt


def test_admite_no_completar_no_falso_positivo():
    for txt in (
        "Escribí el archivo y corrí los tests: pasan.",
        "Listo, apliqué el cambio.",
        "",
        # pasado legítimo de un edit exitoso — NO es una confesión (falso positivo del regex viejo):
        "Apliqué el fix: copié y pegué la función refactorizada en utils.py y guardé.",
        # decisión de diseño, no confesión de fallo (falso positivo del patrón 'no persist'):
        "Decidí no persistir el cache temporal; el resto quedó aplicado.",
    ):
        assert not runner._admite_no_completar(txt), txt


def test_ejecutar_falla_si_confiesa_no_completar(monkeypatch):
    # exit 0 pero admite que no pudo -> FALLIDO (no HECHO): no over-claim.
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda *a, **k: (True, "No tengo permiso para escribir; cópialo y pégalo tú."),
    )
    p = Plan("o", "g", [Step(1, "corre la tarea", "ejecutar", "hecho", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == "fallido"


def test_editar_falla_si_confiesa_no_completar(monkeypatch):
    monkeypatch.setattr(runner, "_git_status", lambda repo: "")  # git no descarta antes
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: (True, "no pude escribir el archivo")
    )
    p = Plan("o", "g", [Step(1, "agrega hints a todo", "editar", "d", [])])  # sin target
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == "fallido"


# F1: la huella de _git_status debe ser SENSIBLE AL CONTENIDO (no solo a la lista de archivos):
# reeditar un archivo que ya estaba modificado debe cambiar la huella, o daría un falso 'sin efecto'.


def _git(repo, *args):
    import subprocess

    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", *args],
        cwd=repo,
        capture_output=True,
        check=True,
    )


def test_git_status_sensible_al_contenido_en_archivo_ya_sucio(tmp_path):
    repo = tmp_path
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "init")
    _git(repo, "add", "a.py")
    _git(repo, "commit", "-m", "init")
    (repo / "a.py").write_text("x = 2\n", encoding="utf-8")  # ya sucio
    h1 = runner._git_status(str(repo))
    (repo / "a.py").write_text("x = 2\ny = 3\n", encoding="utf-8")  # más edición sobre el mismo
    h2 = runner._git_status(str(repo))
    assert h1 is not None and h2 is not None
    assert h1 != h2  # el porcelain solo daría ' M a.py' == ' M a.py'; la huella capta el contenido


def test_git_status_none_fuera_de_repo(tmp_path):
    assert runner._git_status(str(tmp_path)) is None  # sin git init -> None (no bloquea)
# F1 / deuda #17: la huella también debe ver el CONTENIDO de los archivos UNTRACKED. Reescribir uno
# deja el porcelain idéntico ('?? a.md' == '?? a.md') y no entra en `git diff HEAD` -> falso
# 'sin efecto en disco' (pasó en la validación de E2: paso 9 reescribiendo APROBACION.md).


def _repo_con_commit(tmp_path):
    """Repo git con un archivo ya commiteado, para que `git diff HEAD` exista."""
    (tmp_path / "base.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "init")
    _git(tmp_path, "add", "base.py")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


def test_git_status_sensible_al_contenido_de_untracked(tmp_path):
    repo = _repo_con_commit(tmp_path)
    (repo / "APROBACION.md").write_text("secciones heredadas\n", encoding="utf-8")
    h1 = runner._git_status(str(repo))
    (repo / "APROBACION.md").write_text("plantilla estricta\n", encoding="utf-8")  # mismo nombre
    h2 = runner._git_status(str(repo))
    assert h1 is not None and h2 is not None
    assert h1.split("\0")[:2] == h2.split("\0")[:2]  # porcelain + diff idénticos: esa es la trampa
    assert h1 != h2  # el tercer componente (sha256 del untracked) sí se mueve


def test_archivos_tocados_no_ve_el_tercer_componente(tmp_path):
    # el hash va DESPUÉS del segundo \0 justamente para que la extracción de rutas no cambie.
    repo = _repo_con_commit(tmp_path)
    (repo / "nuevo.md").write_text("hola\n", encoding="utf-8")
    (repo / "base.py").write_text("x = 2\n", encoding="utf-8")
    assert sorted(runner._archivos_tocados(runner._git_status(str(repo)))) == ["base.py", "nuevo.md"]


def test_huella_untracked_vacia_sin_untracked_y_respeta_gitignore(tmp_path):
    repo = _repo_con_commit(tmp_path)
    assert runner._huella_untracked(str(repo)) == ""  # árbol limpio: nada que hashear
    (repo / ".gitignore").write_text("secreto.txt\n", encoding="utf-8")
    (repo / "secreto.txt").write_text("no me mires\n", encoding="utf-8")
    huella = runner._huella_untracked(str(repo))
    assert "secreto.txt" not in huella  # --exclude-standard: mismo criterio que el porcelain
    assert huella.endswith(" .gitignore")  # el .gitignore sí: untracked y no ignorado


def test_huella_untracked_cae_a_tamano_cuando_se_acaba_el_presupuesto(tmp_path, monkeypatch):
    repo = _repo_con_commit(tmp_path)
    (repo / "grande.bin").write_bytes(b"ab" * 100)
    monkeypatch.setattr(runner, "_HUELLA_UNTRACKED_BYTES", 10)  # un untracked enorme no se lee
    assert runner._huella_untracked(str(repo)) == "size:200 grande.bin"


def test_huella_untracked_con_ruta_acentuada(tmp_path):
    repo = _repo_con_commit(tmp_path)
    (repo / "informe_año.md").write_text("hola\n", encoding="utf-8")
    huella = runner._huella_untracked(str(repo))
    assert huella.endswith(" informe_año.md")  # -z: sin las comillas de core.quotepath
    assert "ilegible" not in huella  # la ruta llegó entera, el archivo se pudo leer


# F1: edición sin efecto en disco (git status idéntico antes/después) -> falso 'hecho'.


def test_editar_falla_si_no_hay_efecto_en_disco(monkeypatch):
    monkeypatch.setattr(runner, "_git_status", lambda repo: " M archivo.py")  # misma huella
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "listo, edité todo"))
    p = Plan("o", "g", [Step(1, "agrega hints a todo", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == "fallido"  # la huella no cambió -> no tocó nada


def test_editar_ok_si_cambia_el_disco(monkeypatch):
    huellas = iter(["", " M archivo.py"])  # antes vacío, después con cambio
    monkeypatch.setattr(runner, "_git_status", lambda repo: next(huellas))
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "edité archivo.py"))
    p = Plan("o", "g", [Step(1, "agrega hints a todo", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == HECHO


def test_editar_ok_si_git_no_disponible(monkeypatch):
    # repo no-git (o git ausente): _git_status None -> no bloquea, cae al camino normal.
    monkeypatch.setattr(runner, "_git_status", lambda repo: None)
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "edité todo"))
    p = Plan("o", "g", [Step(1, "agrega hints a todo", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == HECHO


# F1b: un 'editar' de AUDITORÍA sobre código que ya cumple no deja diff -> HECHO legítimo, no fallo.
# (Este es el caso del paso 14 del controller: auditar compare_digest sobre código ya correcto.)


def test_sin_cambio_necesario_detecta_conformidad():
    for txt in (
        "Audité los 3 criterios: la comparación ya usa compare_digest, no fue necesario modificar nada.",
        "El código ya cumple el criterio; sin cambios necesarios.",
        "Revisado: todo correcto, no hay nada que cambiar.",
        "The comparison already uses compare_digest; no changes needed.",
        "Nothing to change, the code already meets the requirement.",
    ):
        assert runner._sin_cambio_necesario(txt), txt


def test_sin_cambio_necesario_no_falso_positivo():
    for txt in (
        "Edité el archivo y apliqué el fix.",
        "No pude aplicar el cambio.",
        "",
        "Agregué la validación que faltaba.",
    ):
        assert not runner._sin_cambio_necesario(txt), txt


def test_editar_auditoria_sin_diff_es_hecho(monkeypatch):
    # Sin efecto en disco PERO el executor concluye que el repo ya cumple -> HECHO (no falso fallo).
    monkeypatch.setattr(runner, "_git_status", lambda repo: " M archivo.py")  # misma huella
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda *a, **k: (
            True,
            "Audité el criterio: el código ya cumple, no fue necesario modificar nada.",
        ),
    )
    p = Plan("o", "g", [Step(1, "audita que la comparación sea constante", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == HECHO
    assert "ya cumple" in p.pasos[0].nota


def test_editar_sin_diff_sin_justificacion_sigue_fallido(monkeypatch):
    # Sin efecto en disco y SIN conclusión de conformidad -> sigue FALLIDO (no relajamos el gate F1).
    monkeypatch.setattr(runner, "_git_status", lambda repo: " M archivo.py")
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "listo, edité todo"))
    p = Plan("o", "g", [Step(1, "agrega hints a todo", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == "fallido"


def test_confesion_no_completar_cita_el_fragmento(monkeypatch):
    # La nota del fallo cita la frase exacta que delató el falso 'hecho' (diagnóstico, no genérico).
    monkeypatch.setattr(runner, "_git_status", lambda repo: "")
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: (True, "no pude escribir el archivo destino")
    )
    p = Plan("o", "g", [Step(1, "agrega hints a todo", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == "fallido"
    assert "no pude escribir" in p.pasos[0].nota  # la confesión aparece citada en la nota


def test_archivos_tocados_parsea_porcelain():
    huella = " M server/auth/tokens.py\n?? nuevo.py\0diff-irrelevante"
    assert runner._archivos_tocados(huella) == ["server/auth/tokens.py", "nuevo.py"]
    assert runner._archivos_tocados(None) == []


# La causa de un bloqueo de seguridad (pre-flight de secretos) llega a la nota del paso:
# sin esto el usuario ve "no completó la tarea" cuando el dispatch se bloqueó a propósito.


def test_nota_fallo_prefiere_la_causa_del_preflight():
    causa = "pre-flight de seguridad: el prompt contiene un secreto"
    assert runner._nota_fallo(causa, "genérico") == causa
    # sin causa de pre-flight, se antepone el genérico al detalle real del executor
    assert runner._nota_fallo("salida cualquiera", "genérico") == "genérico: salida cualquiera"
    assert runner._nota_fallo("", "genérico") == "genérico"


def test_paso_bloqueado_por_preflight_muestra_la_causa(monkeypatch):
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda *a, **k: (False, "pre-flight de seguridad: el prompt contiene un secreto"),
    )
    p = Plan("o", "g", [Step(1, "corre el script de limpieza", "ejecutar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert p.pasos[0].estado == "fallido"
    assert "pre-flight de seguridad" in p.pasos[0].nota


# F4: la auto-evolución re-propone pasos que ya existen -> se descartan por similitud.


def test_es_duplicado_detecta_casi_identico():
    p = Plan(
        "o",
        "g",
        [Step(1, "auditar seguridad del executor headless", "investigar", "mapear riesgos", [])],
    )
    assert runner._es_duplicado("auditar seguridad del executor headless", "mapear riesgos", p)
    assert not runner._es_duplicado("optimizar el router local a online", "latencia baja", p)


def test_es_duplicado_ignora_pasos_muy_cortos():
    p = Plan("o", "g", [Step(1, "haz X", "editar", "ya", [])])
    assert not runner._es_duplicado("haz Y", "no", p)  # <4 tokens de señal -> no juzga


def test_es_duplicado_no_confunde_intenciones_opuestas():
    # 'agregar timeout' vs 'quitar timeout' comparten casi todo pero son opuestos: no son duplicados.
    p = Plan(
        "o", "g", [Step(1, "editar el runner para agregar timeout", "editar", "hay timeout", [])]
    )
    assert not runner._es_duplicado("editar el runner para quitar timeout", "sin timeout", p)


def test_es_duplicado_permite_reintentar_paso_fallido():
    # una reflexión que re-propone un paso que FALLÓ no es duplicar: es el correctivo que se busca.
    p = Plan(
        "o",
        "g",
        [Step(1, "auditar seguridad del executor headless", "investigar", "riesgos mapeados", [])],
    )
    p.pasos[0].estado = "fallido"
    assert not runner._es_duplicado(
        "auditar seguridad del executor headless", "riesgos mapeados", p
    )


def test_insertar_pasos_descarta_duplicado():
    p = Plan(
        "o",
        "g",
        [
            Step(
                1,
                "documentar el flujo del planner al runner",
                "memoria",
                "nota creada en el vault",
                [],
            )
        ],
    )
    n = runner._insertar_pasos(
        p,
        p.pasos[0],
        [
            {
                "accion": "documentar el flujo del planner al runner",
                "tipo": "memoria",
                "done": "nota creada en el vault",
            },
            {
                "accion": "añadir índice de dependencias al README raíz",
                "tipo": "editar",
                "done": "README con tabla",
            },
        ],
    )
    assert n == 1  # solo el paso genuinamente nuevo; el duplicado se descartó
    assert any("índice de dependencias" in s.accion for s in p.pasos)


# --- S2: los pasos corren en el worktree del plan, nunca con cwd=repo real (H1/H2/H4) ---


def _git_repo(tmp_path):
    """Repo git real minimo (un commit) para probar el worktree del plan."""
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    (repo / "a.txt").write_text("hola", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "-c", "user.email=v@t", "-c", "user.name=v", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return str(repo)


def _temp_en(tmp_path, monkeypatch):
    """Confina tempfile.mkdtemp (el padre del worktree) a tmp_path: sin basura en el temp real."""
    base = tmp_path / "tmp"
    base.mkdir()
    monkeypatch.setattr(runner.tempfile, "tempdir", str(base))


def test_s2_pasos_corren_en_worktree_no_en_repo(tmp_path, monkeypatch):
    import os

    repo = _git_repo(tmp_path)
    _temp_en(tmp_path, monkeypatch)
    cwds = []
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda prompt, cwd, **k: (cwds.append(str(cwd)), (True, "listo"))[1],
    )
    p = Plan("o", "g", [Step(1, "corre el linter", "ejecutar", "d", [])])
    run_plan(p, repo, persist=False)
    assert p.workdir and p.rama.startswith("escapement/plan-")
    assert cwds and all(os.path.samefile(c, p.workdir) for c in cwds)
    assert not os.path.samefile(p.workdir, repo)  # el repo real nunca es el cwd del dispatch


def test_s2_worktree_se_reusa_entre_llamadas(tmp_path, monkeypatch):
    repo = _git_repo(tmp_path)
    _temp_en(tmp_path, monkeypatch)
    p = Plan("o", "g", [])
    w1 = runner._worktree_plan(p, repo)
    w2 = runner._worktree_plan(p, repo)
    assert w1 and w1 == w2 == p.workdir  # no crea un segundo worktree


def test_s2_workdir_borrado_se_remonta_sobre_la_rama(tmp_path, monkeypatch):
    import shutil

    repo = _git_repo(tmp_path)
    _temp_en(tmp_path, monkeypatch)
    p = Plan("o", "g", [])
    w1 = runner._worktree_plan(p, repo)
    shutil.rmtree(w1)  # temp limpiado entre reanudaciones
    w2 = runner._worktree_plan(p, repo)
    assert w2 and w2 != w1  # re-montado (prune + add sobre la MISMA rama)
    assert p.workdir == w2 and p.rama.startswith("escapement/plan-")


def test_s2_repo_no_git_cae_a_cwd_repo(tmp_path, monkeypatch):
    plano = tmp_path / "plano"
    plano.mkdir()
    cwds = []
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda prompt, cwd, **k: (cwds.append(str(cwd)), (True, "listo"))[1],
    )
    p = Plan("o", "g", [Step(1, "corre el linter", "ejecutar", "d", [])])
    run_plan(p, str(plano), persist=False)
    assert p.workdir == "" and p.rama == ""  # sin worktree: comportamiento previo
    assert cwds == [str(plano)]


def test_s2_workdir_y_rama_persisten_para_reanudar(tmp_path, monkeypatch):
    from agent import planner

    repo = _git_repo(tmp_path)
    _temp_en(tmp_path, monkeypatch)
    dedicado = tmp_path / "plan_repo.json"
    p = Plan("o", "g", [Step(1, "a", "ejecutar", "d", [])])
    run_plan(p, repo, handlers={"ejecutar": lambda s, pl, r: (HECHO, "ok")}, plan_path=dedicado)
    cargado = planner.load_plan(dedicado)
    assert cargado.workdir == p.workdir and cargado.rama == p.rama
    assert cargado.rama.startswith("escapement/plan-")  # una reanudación retoma el mismo worktree


# --- S3: el carril editar-sin-target despacha SIN shell (input no confiable no corre comandos) ---


def test_editar_sin_target_despacha_no_shell(monkeypatch):
    kwargs = []
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda prompt, **k: (kwargs.append(k), (True, "editado"))[1],
    )
    p = Plan("o", "g", [Step(1, "actualiza los docstrings del paquete", "editar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert kwargs[0].get("no_shell") is True


def test_ejecutar_conserva_shell(monkeypatch):
    # 'ejecutar' SÍ corre comandos (es su propósito): su jaula es guard (S1) + worktree (S2)
    kwargs = []
    monkeypatch.setattr(
        runner.executors,
        "run_agent",
        lambda prompt, **k: (kwargs.append(k), (True, "listo"))[1],
    )
    p = Plan("o", "g", [Step(1, "corre la suite completa", "ejecutar", "d", [])])
    run_plan(p, "repo", persist=False)
    assert kwargs[0].get("no_shell", False) is False


# --- R3: corte por presupuesto (budget opcional; sin budget el runner nunca corta) ---


def test_run_corta_por_presupuesto(monkeypatch):
    # Con el budget agotado, el runner NO despacha el siguiente paso: lo marca 'bloqueado'
    # (reanudable) y para, sin invocar su handler.
    import contextlib

    @contextlib.contextmanager
    def _track_agotado(objetivo, budget=None):
        c = runner.costs.PlanCosts(objetivo, budget=10)
        c.est_in = 999  # ya por encima del budget -> over_budget() True desde el arranque
        yield c

    monkeypatch.setattr(runner.costs, "track", _track_agotado)
    llamado = []
    handlers = {"ejecutar": lambda s, pl, r: (llamado.append(s.id), (HECHO, "ok"))[1]}
    p = Plan("o", "g", [Step(1, "corre algo", "ejecutar", "d", [])])
    run_plan(p, "repo", handlers=handlers, persist=False)
    assert p.pasos[0].estado == BLOQUEADO
    assert "presupuesto" in p.pasos[0].nota
    assert llamado == []  # nunca se despachó el paso


def test_run_sin_budget_no_corta(monkeypatch):
    # Default (BUDGET_TOKENS_PER_PLAN None): over_budget() siempre False -> corre entero.
    monkeypatch.setattr(runner.config, "BUDGET_TOKENS_PER_PLAN", None)
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}
    p = _plan()
    run_plan(p, "repo", handlers=todo_ok, persist=False)
    assert all(s.estado == HECHO for s in p.pasos)


# --- R4: swarm (fan-out de subagentes en paralelo, read-only) ------------------


def test_parse_subtareas_filtra_no_str_y_vacios():
    out = 'ruido {"subtareas": ["a ", "  ", 5, "b"]} cola'
    assert runner._parse_subtareas(out) == ["a", "b"]  # trim + descarta no-str y en blanco
    assert runner._parse_subtareas("sin json") == []
    assert runner._parse_subtareas('{"otra": []}') == []  # sin la clave -> vacío


def _swarm_step():
    return Step(1, "investiga el subsistema X a fondo", "swarm", "mapa completo", [])


def test_swarm_descompone_y_agrega(monkeypatch):
    # El coordinador descompone en 3 subtareas; cada rama devuelve un hallazgo -> HECHO 3/3.
    calls = []

    def fake(prompt, **k):
        calls.append(prompt)
        if "coordinador de swarm" in prompt:
            return True, '{"subtareas": ["s1", "s2", "s3"]}'
        return True, "hallazgo"

    monkeypatch.setattr(runner.executors, "run_agent", fake)
    est, nota = runner._h_swarm(_swarm_step(), Plan("o", "g", []), "repo")
    assert est == HECHO
    assert "swarm 3/3 OK" in nota
    assert len(calls) == 4  # 1 descomposición + 3 ramas


def test_swarm_fallback_una_rama_si_no_descompone(monkeypatch):
    # Si el coordinador no da JSON parseable, cae a UNA rama = la acción original (un 'investigar').
    def fake(prompt, **k):
        if "coordinador de swarm" in prompt:
            return True, "no es json"
        return True, "hallazgo"

    monkeypatch.setattr(runner.executors, "run_agent", fake)
    est, nota = runner._h_swarm(_swarm_step(), Plan("o", "g", []), "repo")
    assert est == HECHO
    assert "swarm 1/1 OK" in nota


def test_swarm_todas_fallan_es_fallido(monkeypatch):
    # Ninguna rama responde -> el paso queda FALLIDO (best-effort agota todas las ramas antes).
    def fake(prompt, **k):
        if "coordinador de swarm" in prompt:
            return True, '{"subtareas": ["s1", "s2"]}'
        return False, "boom"

    monkeypatch.setattr(runner.executors, "run_agent", fake)
    est, nota = runner._h_swarm(_swarm_step(), Plan("o", "g", []), "repo")
    assert est == runner.FALLIDO
    assert "ninguna" in nota


def test_swarm_trunca_al_tope(monkeypatch):
    # Con SWARM_MAX_TASKS=2, aunque el coordinador proponga 5 subtareas solo se despachan 2 ramas.
    monkeypatch.setattr(runner.config, "SWARM_MAX_TASKS", 2)
    ramas = []

    def fake(prompt, **k):
        if "coordinador de swarm" in prompt:
            return True, '{"subtareas": ["s1", "s2", "s3", "s4", "s5"]}'
        ramas.append(prompt)
        return True, "ok"

    monkeypatch.setattr(runner.executors, "run_agent", fake)
    est, nota = runner._h_swarm(_swarm_step(), Plan("o", "g", []), "repo")
    assert est == HECHO
    assert "swarm 2/2 OK" in nota
    assert len(ramas) == 2  # el tope se respeta pese a las 5 propuestas


# --- _dispatch_reason: ruteo local/CLI de pasos de razonamiento (Clase A) ---


def test_dispatch_reason_cli_va_a_run_agent(monkeypatch):
    # route_step -> cli: reconstruye system+user y despacha a run_agent con el modelo decidido
    # (comportamiento previo exacto; p.ej. persona pesada, dificultad alta o local no disponible).
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("cli", "modelo-x")
    )
    llamadas = []

    def fake(prompt, cwd, mode, model):
        llamadas.append((prompt, mode, model))
        return True, "cli-out"

    monkeypatch.setattr(runner.executors, "run_agent", fake)
    ok, out = runner._dispatch_reason(
        "reflexionar", "u", system="s", validate=lambda o: True, cwd="/tmp"
    )
    assert ok and out == "cli-out"
    assert llamadas[0][0] == "s\n\nu" and llamadas[0][1] == "read"
    assert llamadas[0][2] == "modelo-x"  # usa el modelo que decidió route_step


def _capturar_observer(monkeypatch):
    """Captura las notificaciones al observer de costos (mode, prompt) durante el dispatch."""
    eventos = []
    monkeypatch.setattr(
        runner.executors,
        "notify_observer",
        lambda mode, prompt, output: eventos.append((mode, prompt)),
    )
    return eventos


def test_dispatch_reason_local_valido_usa_local(monkeypatch):
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("local", "")
    )
    monkeypatch.setattr(runner.local_models, "available", lambda: True)
    monkeypatch.setattr(runner.local_models, "complete", lambda user, system="", **k: "local-out")
    llamadas = []
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda *a, **k: llamadas.append(1) or (True, "cli")
    )
    eventos = _capturar_observer(monkeypatch)
    ok, out = runner._dispatch_reason("reflexionar", "u", validate=lambda o: True, cwd="/tmp")
    assert ok and out == "local-out"
    assert not llamadas  # no cayó a run_agent (ahorro de cuota online)
    # se cuenta UNA vez como dispatch local (sin doble-conteo), nunca como fallback
    assert eventos == [("local", "u")]


def test_dispatch_reason_local_invalido_cae_a_cli(monkeypatch):
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("local", "m")
    )
    monkeypatch.setattr(runner.local_models, "available", lambda: True)
    monkeypatch.setattr(runner.local_models, "complete", lambda user, system="", **k: "basura")
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "cli-out"))
    eventos = _capturar_observer(monkeypatch)
    ok, out = runner._dispatch_reason(
        "reflexionar", "u", validate=lambda o: o == "valido", cwd="/tmp"
    )
    assert ok and out == "cli-out"  # la validación falló -> fallback online sin perder corrección
    # el intento descartado NO cuenta como dispatch local; se registra el fallback con su motivo
    assert eventos == [("local_fallback", "no_valido")]


def test_dispatch_reason_local_none_cae_a_cli(monkeypatch):
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("local", "")
    )
    monkeypatch.setattr(runner.local_models, "available", lambda: True)
    monkeypatch.setattr(runner.local_models, "complete", lambda user, system="", **k: None)
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "cli-out"))
    eventos = _capturar_observer(monkeypatch)
    ok, out = runner._dispatch_reason("reflexionar", "u", validate=lambda o: True, cwd="/tmp")
    assert ok and out == "cli-out"
    assert eventos == [("local_fallback", "sin_respuesta")]  # local sin respuesta -> fallback


def test_dispatch_reason_local_no_disponible_registra_fallback(monkeypatch):
    # route quería local pero Ollama no responde: cae a Claude y se registra el motivo (sin llamar complete)
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("local", "")
    )
    monkeypatch.setattr(runner.local_models, "available", lambda: False)
    tocado = []
    monkeypatch.setattr(runner.local_models, "complete", lambda *a, **k: tocado.append(1) or "x")
    monkeypatch.setattr(runner.executors, "run_agent", lambda *a, **k: (True, "cli-out"))
    eventos = _capturar_observer(monkeypatch)
    ok, out = runner._dispatch_reason("reflexionar", "u", validate=lambda o: True, cwd="/tmp")
    assert ok and out == "cli-out"
    assert not tocado  # local no disponible: ni se intenta complete
    assert eventos == [("local_fallback", "no_disponible")]


def test_dispatch_reason_backend_cli_no_toca_local(monkeypatch):
    # si route_step decide cli, ni se consulta al local (available/complete no se llaman) ni se
    # registra fallback (no se INTENTÓ local: es un dispatch cli directo, no un local caído)
    monkeypatch.setattr(
        runner.config, "route_step", lambda *a, **k: runner.config.Route("cli", "mx")
    )
    tocado = []
    monkeypatch.setattr(runner.local_models, "available", lambda: tocado.append(1) or True)
    monkeypatch.setattr(
        runner.executors, "run_agent", lambda prompt, cwd, mode, model: (True, model)
    )
    eventos = _capturar_observer(monkeypatch)
    ok, out = runner._dispatch_reason("reflexionar", "u", validate=lambda o: True, cwd="/tmp")
    assert ok and out == "mx"  # usó el modelo del route
    assert not tocado
    assert not eventos  # cli directo: ni dispatch local ni fallback


# --- Cancelación cooperativa y progreso en vivo (deuda #2) ---------------------------------


def test_cancelacion_corta_entre_pasos_y_deja_el_siguiente_bloqueado():
    """F2 durante una corrida: el paso en vuelo termina, el que seguía queda reanudable."""
    p = _plan()
    despachados = []
    handlers = {
        t: (lambda s, pl, r: (despachados.append(s.id), (HECHO, "ok"))[1])
        for t in ("investigar", "editar", "verificar")
    }
    # cancelado a partir del segundo paso: el 1 se despacha, el 2 ya no
    run_plan(
        p,
        "repo",
        handlers=handlers,
        should_cancel=lambda: len(despachados) >= 1,
        persist=False,
    )

    assert despachados == [1]  # no se despachó nada después de pedir la cancelación
    assert [s.estado for s in p.pasos] == [HECHO, BLOQUEADO, "pendiente"]
    assert "cancelada" in p.pasos[1].nota


def test_cancelacion_antes_del_primer_paso_no_despacha_nada():
    p = _plan()
    despachados = []
    handlers = {
        t: (lambda s, pl, r: (despachados.append(s.id), (HECHO, "ok"))[1])
        for t in ("investigar", "editar", "verificar")
    }
    run_plan(p, "repo", handlers=handlers, should_cancel=lambda: True, persist=False)

    assert despachados == []
    assert p.pasos[0].estado == BLOQUEADO


def test_sin_should_cancel_la_corrida_es_la_de_siempre():
    """Default None = no-op: el parámetro nuevo no cambia el comportamiento previo."""
    p = _plan()
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}
    run_plan(p, "repo", handlers=todo_ok, persist=False)

    assert all(s.estado == HECHO for s in p.pasos)


def test_publica_step_start_antes_de_despachar_el_paso():
    """El tray necesita saber que el paso ARRANCÓ: `plan.step` solo llega al terminarlo."""
    p = _plan()
    vistos = []
    cancelar = runner.bus.subscribe("plan.step_start", lambda e: vistos.append(e.data))

    orden = []

    def _handler(s, pl, r):
        orden.append(("dispatch", s.id, len(vistos)))
        return (HECHO, "ok")

    try:
        run_plan(
            p,
            "repo",
            handlers=dict.fromkeys(("investigar", "editar", "verificar"), _handler),
            persist=False,
        )
    finally:
        cancelar()

    assert [d["id"] for d in vistos] == [1, 2, 3]
    assert [d["tipo"] for d in vistos] == ["investigar", "editar", "verificar"]
    # el evento precede al dispatch de SU paso (al despachar el 1 ya había 1 evento)
    assert orden == [("dispatch", 1, 1), ("dispatch", 2, 2), ("dispatch", 3, 3)]
    # progreso utilizable: cuántos había hechos al arrancar cada paso, sobre el total
    assert [(d["hechos"], d["total"]) for d in vistos] == [(0, 3), (1, 3), (2, 3)]


def test_step_start_no_se_publica_para_el_paso_cancelado():
    p = _plan()
    vistos = []
    cancelar = runner.bus.subscribe("plan.step_start", lambda e: vistos.append(e.data))
    try:
        run_plan(
            p,
            "repo",
            handlers=dict.fromkeys(
                ("investigar", "editar", "verificar"), lambda s, pl, r: (HECHO, "ok")
            ),
            should_cancel=lambda: True,
            persist=False,
        )
    finally:
        cancelar()

    assert vistos == []  # nada arrancó, así que el tray no anuncia un paso que no corrió


# --- Evaluación global al cerrar (E2, deuda #7): _parse_eval, _evaluar_plan y el post-loop ---


def test_parse_eval_done_limpio():
    assert runner._parse_eval('{"veredicto": "done", "gaps": []}') == ("done", [])


def test_parse_eval_incompleto_filtra_gaps_invalidos():
    out = '{"veredicto": "Incompleto", "gaps": [" falta doc ", "", 42, "cubre tests"]}'
    assert runner._parse_eval(out) == ("incompleto", ["falta doc", "cubre tests"])


def test_parse_eval_tolera_texto_alrededor():
    out = 'Claro, aquí va:\n{"veredicto": "done", "gaps": []}\nListo.'
    assert runner._parse_eval(out) == ("done", [])


@pytest.mark.parametrize(
    "out",
    [
        "",  # vacío
        "no hay json aquí",  # sin JSON
        '{"veredicto": "done", "gaps": [',  # JSON roto
        '{"veredicto": "quizas", "gaps": []}',  # veredicto fuera del contrato
        '{"gaps": ["x"]}',  # sin veredicto
    ],
)
def test_parse_eval_salida_no_usable_devuelve_vacio(out):
    assert runner._parse_eval(out) == ("", [])


def test_evaluar_plan_despacha_kind_evaluar_con_criterio_y_hechos(monkeypatch):
    p = _plan()
    p.pasos[0].estado = HECHO
    p.pasos[0].nota = "hallazgo A"
    p.pasos[1].estado = "fallido"  # no debe aparecer en los hechos
    capturado = {}

    def _fake(kind, user, **kw):
        capturado["kind"], capturado["user"] = kind, user
        capturado.update(kw)
        return True, '{"veredicto": "incompleto", "gaps": ["g1", "g2", "g3", "g4", "g5"]}'

    monkeypatch.setattr(runner, "_dispatch_reason", _fake)
    veredicto, gaps = runner._evaluar_plan(p, "repo")
    assert veredicto == "incompleto"
    assert gaps == ["g1", "g2", "g3", "g4"]  # tope _EVAL_MAX_GAPS
    assert capturado["kind"] == "evaluar"
    assert "done global" in capturado["user"]  # el criterio global viaja en el prompt
    assert "- [investigar] investiga worker: hallazgo A" in capturado["user"]
    assert "edita algo" not in capturado["user"]  # el paso fallido no cuenta como hecho
    assert capturado["done"] == "done global"  # señal de dificultad para route_step


def test_evaluar_plan_best_effort_sin_salida_usable(monkeypatch):
    p = _plan()
    monkeypatch.setattr(runner, "_dispatch_reason", lambda *a, **k: (False, ""))
    assert runner._evaluar_plan(p, "repo") == ("", [])


def _run_con_eval(monkeypatch, veredicto, gaps, *, criterio="done global"):
    """Corre un plan que completa por conteo, con `_evaluar_plan` mockeado. Devuelve (plan, capturas)."""
    monkeypatch.setattr(runner.config, "PLAN_EVAL", True)
    p = _plan()
    p.criterio_global = criterio
    evaluados, capturados = [], []
    monkeypatch.setattr(
        runner, "_evaluar_plan", lambda pl, r: (evaluados.append(pl), (veredicto, gaps))[1]
    )
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}
    run_plan(p, "repo", handlers=todo_ok, on_complete=lambda pl: capturados.append(pl), persist=False)
    return p, evaluados, capturados


def test_run_plan_eval_done_sella_el_plan_y_captura(monkeypatch):
    p, evaluados, capturados = _run_con_eval(monkeypatch, "done", [])
    assert evaluados == [p]  # evaluó exactamente una vez
    assert p.eval_veredicto == "done" and p.eval_gaps == []
    assert capturados == [p]  # éxito real -> el ReasoningBank sí captura


def test_run_plan_eval_incompleto_persiste_gaps_y_no_captura(monkeypatch):
    p, _, capturados = _run_con_eval(monkeypatch, "incompleto", ["falta la doc de worker/"])
    assert p.eval_veredicto == "incompleto"
    assert p.eval_gaps == ["falta la doc de worker/"]
    assert capturados == []  # el conteo dijo completo, pero NO fue éxito: no envenena el banco


def test_run_plan_eval_sin_veredicto_usable_cierra_como_antes(monkeypatch):
    # best-effort: la evaluación no produjo veredicto -> cierre por conteo, captura intacta
    p, _, capturados = _run_con_eval(monkeypatch, "", [])
    assert p.eval_veredicto == "" and capturados == [p]


def test_run_plan_sin_criterio_global_no_evalua(monkeypatch):
    _, evaluados, capturados = _run_con_eval(monkeypatch, "done", [], criterio="  ")
    assert evaluados == []  # sin criterio no hay contra qué evaluar
    assert len(capturados) == 1


def test_run_plan_eval_off_ignora_veredicto_viejo(monkeypatch):
    # AGENT_PLAN_EVAL=0 = comportamiento previo exacto: ni evalúa ni un 'incompleto' pegado en el
    # JSON de una corrida anterior frena la captura (el veredicto que manda es el de ESTA corrida)
    p = _plan()
    p.eval_veredicto, p.eval_gaps = "incompleto", ["gap viejo"]
    evaluados, capturados = [], []
    monkeypatch.setattr(runner, "_evaluar_plan", lambda pl, r: (evaluados.append(pl), ("", []))[1])
    todo_ok = {t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")}
    run_plan(p, "repo", handlers=todo_ok, on_complete=lambda pl: capturados.append(pl), persist=False)
    assert evaluados == []  # la fixture _eval_off dejó PLAN_EVAL=False -> no se despachó nada
    assert capturados == [p]


def test_run_plan_eval_no_corre_en_checkpoint(monkeypatch):
    monkeypatch.setattr(runner.config, "PLAN_EVAL", True)
    p = _plan()
    evaluados = []
    monkeypatch.setattr(runner, "_evaluar_plan", lambda pl, r: (evaluados.append(pl), ("done", []))[1])
    handlers = {
        "investigar": lambda s, pl, r: (HECHO, "ok"),
        "editar": lambda s, pl, r: (BLOQUEADO, "para"),
    }
    run_plan(p, "repo", handlers=handlers, persist=False)
    assert evaluados == []  # roadmap incompleto: la evaluación es solo del cierre


def test_run_plan_eval_persiste_veredicto_en_el_json(tmp_path, monkeypatch):
    import json

    from agent import planner

    monkeypatch.setattr(runner.config, "PLAN_EVAL", True)
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")  # journal aislado
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    dedicado = tmp_path / "plan_repo.json"
    monkeypatch.setattr(
        runner, "_evaluar_plan", lambda pl, r: ("incompleto", ["cerrar la brecha X"])
    )
    p = Plan("o", "done global", [Step(1, "a", "investigar", "d", [])])
    run_plan(p, "repo", handlers={"investigar": lambda s, pl, r: (HECHO, "ok")}, plan_path=dedicado)
    data = json.loads(dedicado.read_text(encoding="utf-8"))
    assert data["eval_veredicto"] == "incompleto"
    assert data["eval_gaps"] == ["cerrar la brecha X"]


# --- Replanificación automática (E2, pieza 2): gaps -> pasos nuevos, con tope de ciclos ---


def _plan_evaluado_incompleto(gaps):
    """Plan con el roadmap entero en 'hecho' y un veredicto 'incompleto' ya persistido."""
    p = _plan()
    for s in p.pasos:
        s.estado = HECHO
    p.eval_veredicto, p.eval_gaps = "incompleto", gaps
    return p


def test_replanificar_convierte_gaps_en_pasos(monkeypatch):
    p = _plan_evaluado_incompleto(["documentar worker/x", "cubrir tests de y"])
    llamadas = []
    salida = (
        '{"pasos": [{"accion": "documentar worker/x completo", "tipo": "editar",'
        ' "persona": "Backend", "done": "worker/x con docstrings"}]}'
    )

    def fake(kind, prompt, **kw):
        llamadas.append((kind, prompt, kw))
        return True, salida

    monkeypatch.setattr(runner, "_dispatch_reason", fake)
    assert runner._replanificar(p, "repo") == 1
    nuevo = p.pasos[-1]
    assert nuevo.id == 4 and nuevo.depende_de == [3]  # cuelga del último paso (ya hecho)
    assert nuevo.estado == runner.PENDIENTE and nuevo.persona == "backend"
    kind, prompt, kw = llamadas[0]
    assert kind == "reflexionar"  # misma Clase A que la auto-evolución
    assert "documentar worker/x" in prompt and "cubrir tests de y" in prompt  # los gaps van al prompt
    assert "done global" in prompt  # y el criterio global también
    assert kw["done"] == "done global"  # señal de dificultad para route_step


def test_replanificar_best_effort_devuelve_cero(monkeypatch):
    p = _plan_evaluado_incompleto(["falta x"])
    monkeypatch.setattr(runner, "_dispatch_reason", lambda *a, **k: (False, ""))
    assert runner._replanificar(p, "repo") == 0
    assert len(p.pasos) == 3  # el plan queda intacto


def test_replanificar_sin_gaps_no_despacha(monkeypatch):
    p = _plan_evaluado_incompleto([])
    llamadas = []
    monkeypatch.setattr(
        runner, "_dispatch_reason", lambda *a, **k: (llamadas.append(1), (True, '{"pasos": []}'))[1]
    )
    assert runner._replanificar(p, "repo") == 0 and llamadas == []


def test_replanificar_hereda_guardas_de_insertar_pasos(monkeypatch):
    # los pasos propuestos pasan por _insertar_pasos: ni 'reflexionar' ni duplicados de pasos vivos
    p = _plan_evaluado_incompleto(["gap"])
    p.pasos[0].accion = "documenta el modulo worker completo"
    p.pasos[0].done = "worker documentado con docstrings"
    salida = (
        '{"pasos": ['
        '{"accion": "documenta el modulo worker completo", "tipo": "editar",'
        ' "persona": "", "done": "worker documentado con docstrings"},'
        '{"accion": "reflexiona de nuevo", "tipo": "reflexionar", "persona": "", "done": "x"}'
        "]}"
    )
    monkeypatch.setattr(runner, "_dispatch_reason", lambda *a, **k: (True, salida))
    assert runner._replanificar(p, "repo") == 0
    assert len(p.pasos) == 3


def test_replanificar_acota_los_pasos_al_tope_de_gaps(monkeypatch):
    p = _plan_evaluado_incompleto(["gap"])
    pasos = ",".join(
        f'{{"accion": "tarea {letras}", "tipo": "editar", "persona": "", "done": "done {letras}"}}'
        for letras in ("alfa beta gamma", "delta epsilon zeta", "eta theta iota",
                       "kappa lambda mu", "nu xi omicron", "pi rho sigma")
    )
    monkeypatch.setattr(runner, "_dispatch_reason", lambda *a, **k: (True, f'{{"pasos": [{pasos}]}}'))
    assert runner._replanificar(p, "repo") == runner._EVAL_MAX_GAPS  # 6 propuestos -> tope 4


def _run_con_replan(monkeypatch, veredictos, *, max_ciclos=2, replan=None, ciclos_previos=0):
    """Corre un plan completo con eval+replan mockeados. Devuelve (plan, contadores)."""
    monkeypatch.setattr(runner.config, "PLAN_EVAL", True)
    monkeypatch.setattr(runner.config, "PLAN_REPLAN_MAX", max_ciclos)
    p = _plan()
    p.replan_ciclos = ciclos_previos
    it = iter(veredictos)
    contadores = {"eval": 0, "replan": 0, "ejecutados": [], "capturados": []}

    def fake_eval(pl, r):
        contadores["eval"] += 1
        return next(it)

    def fake_replan(pl, r):
        contadores["replan"] += 1
        if replan is None:
            pl.pasos.append(Step(90 + contadores["replan"], "cierra gap", "editar", "dx", []))
            return 1
        return replan

    monkeypatch.setattr(runner, "_evaluar_plan", fake_eval)
    monkeypatch.setattr(runner, "_replanificar", fake_replan)
    handlers = {
        t: (lambda s, pl, r: (contadores["ejecutados"].append(s.id), (HECHO, "ok"))[1])
        for t in ("investigar", "editar", "verificar")
    }
    run_plan(
        p, "repo", handlers=handlers,
        on_complete=lambda pl: contadores["capturados"].append(pl), persist=False,
    )
    return p, contadores


def test_run_plan_replanifica_y_ejecuta_los_pasos_nuevos(monkeypatch):
    p, c = _run_con_replan(monkeypatch, [("incompleto", ["falta x"]), ("done", [])])
    assert c["eval"] == 2  # evaluó, replanificó, ejecutó lo nuevo y volvió a evaluar
    assert c["replan"] == 1 and 91 in c["ejecutados"]  # el paso nuevo sí se despachó
    assert p.replan_ciclos == 1
    assert p.eval_veredicto == "done" and c["capturados"] == [p]  # cierre real -> sí captura


def test_run_plan_replan_respeta_el_tope_de_ciclos(monkeypatch):
    # eval SIEMPRE incompleto: replanifica hasta el tope y frena (checkpoint humano, sin captura)
    p, c = _run_con_replan(
        monkeypatch, [("incompleto", ["gap"])] * 3, max_ciclos=2
    )
    assert c["replan"] == 2 and c["eval"] == 3  # la última eval ya no replanifica
    assert p.replan_ciclos == 2 and c["capturados"] == []


def test_run_plan_replan_apagado_no_replanifica(monkeypatch):
    p, c = _run_con_replan(monkeypatch, [("incompleto", ["gap"])], max_ciclos=0)
    assert c["replan"] == 0 and c["eval"] == 1
    assert p.replan_ciclos == 0 and c["capturados"] == []  # comportamiento de la pieza 1 exacto


def test_run_plan_replan_sin_pasos_nuevos_corta_sin_consumir_ciclo(monkeypatch):
    # _replanificar best-effort devolvió 0: la corrida termina incompleta y el contador no sube
    p, c = _run_con_replan(monkeypatch, [("incompleto", ["gap"])], replan=0)
    assert c["replan"] == 1 and c["eval"] == 1
    assert p.replan_ciclos == 0 and c["capturados"] == []


def test_run_plan_replan_contador_persistido_ya_en_tope(monkeypatch):
    # reanudar no resetea el tope: un plan que ya consumió sus ciclos no vuelve a replanificar
    p, c = _run_con_replan(
        monkeypatch, [("incompleto", ["gap"])], max_ciclos=2, ciclos_previos=2
    )
    assert c["replan"] == 0 and p.replan_ciclos == 2 and c["capturados"] == []


def test_run_plan_replan_paso_nuevo_bloqueado_frena(monkeypatch):
    monkeypatch.setattr(runner.config, "PLAN_EVAL", True)
    monkeypatch.setattr(runner.config, "PLAN_REPLAN_MAX", 2)
    p = _plan()
    evaluados, capturados = [], []
    monkeypatch.setattr(
        runner, "_evaluar_plan",
        lambda pl, r: (evaluados.append(1), ("incompleto", ["gap"]))[1],
    )

    def fake_replan(pl, r):
        pl.pasos.append(Step(9, "requiere humano", "ejecutar", "dx", []))
        return 1

    monkeypatch.setattr(runner, "_replanificar", fake_replan)
    handlers = {
        **{t: (lambda s, pl, r: (HECHO, "ok")) for t in ("investigar", "editar", "verificar")},
        "ejecutar": lambda s, pl, r: (BLOQUEADO, "para"),
    }
    run_plan(p, "repo", handlers=handlers, on_complete=capturados.append, persist=False)
    assert p.pasos[-1].estado == BLOQUEADO  # checkpoint normal en el paso replanificado
    assert len(evaluados) == 1 and p.replan_ciclos == 1
    assert capturados == []  # el plan NO quedó completo: nada que capturar


def test_run_plan_replan_persiste_ciclos_en_el_json(tmp_path, monkeypatch):
    import json

    from agent import planner

    monkeypatch.setattr(runner.config, "PLAN_EVAL", True)
    monkeypatch.setattr(runner.config, "PLAN_REPLAN_MAX", 1)
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")  # journal aislado
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    dedicado = tmp_path / "plan_repo.json"
    veredictos = iter([("incompleto", ["falta b"]), ("done", [])])
    monkeypatch.setattr(runner, "_evaluar_plan", lambda pl, r: next(veredictos))

    def fake_replan(pl, r):
        pl.pasos.append(Step(2, "b", "investigar", "d2", []))
        return 1

    monkeypatch.setattr(runner, "_replanificar", fake_replan)
    p = Plan("o", "done global", [Step(1, "a", "investigar", "d", [])])
    run_plan(p, "repo", handlers={"investigar": lambda s, pl, r: (HECHO, "ok")}, plan_path=dedicado)
    data = json.loads(dedicado.read_text(encoding="utf-8"))
    assert data["replan_ciclos"] == 1  # el contador sobrevive la reanudación
    assert data["eval_veredicto"] == "done"
    assert [s["id"] for s in data["pasos"]] == [1, 2]


# --- Telemetría del ciclo (E2, pieza 3): topics plan.eval / plan.replan al bus ---


def _capturar(topic):
    """Suscribe una lista acumuladora al topic. Devuelve (vistos, cancelar)."""
    vistos = []
    cancelar = runner.bus.subscribe(topic, lambda e: vistos.append(e.data))
    return vistos, cancelar


def test_publica_plan_eval_con_veredicto_y_gaps(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")  # journal aislado
    vistos, cancelar = _capturar("plan.eval")
    try:
        _run_con_eval(monkeypatch, "incompleto", ["falta la doc de worker/"])
    finally:
        cancelar()
    assert vistos == [
        {"repo": "repo", "veredicto": "incompleto", "gaps": ["falta la doc de worker/"]}
    ]


def test_publica_plan_eval_incluso_best_effort(tmp_path, monkeypatch):
    # veredicto "" (la evaluación no produjo salida usable) también se publica: esa ruta
    # silenciosa es justo la que la telemetría debe hacer observable
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")
    vistos, cancelar = _capturar("plan.eval")
    try:
        _run_con_eval(monkeypatch, "", [])
    finally:
        cancelar()
    assert vistos == [{"repo": "repo", "veredicto": "", "gaps": []}]


def test_no_publica_plan_eval_si_no_se_evaluo(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")
    vistos, cancelar = _capturar("plan.eval")
    try:
        _run_con_eval(monkeypatch, "done", [], criterio="  ")  # sin criterio no hay evaluación
    finally:
        cancelar()
    assert vistos == []


def test_publica_plan_replan_con_ciclo_y_tope(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")
    evals, cancelar_e = _capturar("plan.eval")
    replans, cancelar_r = _capturar("plan.replan")
    try:
        _run_con_replan(monkeypatch, [("incompleto", ["falta x"]), ("done", [])])
    finally:
        cancelar_e()
        cancelar_r()
    assert replans == [{"repo": "repo", "anadidos": 1, "ciclo": 1, "max": 2}]
    # el ciclo entero queda contado: una eval por cierre (incompleto y luego done)
    assert [e["veredicto"] for e in evals] == ["incompleto", "done"]


def test_no_publica_plan_replan_sin_pasos_nuevos(tmp_path, monkeypatch):
    # _replanificar best-effort (0 pasos) no publica: no hubo replanificación efectiva
    monkeypatch.setattr(runner.config, "EVENTS", tmp_path / "events.jsonl")
    vistos, cancelar = _capturar("plan.replan")
    try:
        _run_con_replan(monkeypatch, [("incompleto", ["gap"])], replan=0)
    finally:
        cancelar()
    assert vistos == []


def test_plan_eval_y_replan_quedan_en_el_journal(tmp_path, monkeypatch):
    # journal=True: los eventos son legibles después con `escapement eventos --topic plan.eval`
    import json

    journal = tmp_path / "events.jsonl"
    monkeypatch.setattr(runner.config, "EVENTS", journal)
    _run_con_replan(monkeypatch, [("incompleto", ["falta x"]), ("done", [])])
    topics = [json.loads(li)["topic"] for li in journal.read_text(encoding="utf-8").splitlines()]
    assert topics.count("plan.eval") == 2
    assert topics.count("plan.replan") == 1
