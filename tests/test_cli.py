"""Tests del dispatcher de comandos (tabla + alias + help + sugerencia de typo). Sin efectos."""

from agent import cli


def test_todos_los_alias_apuntan_a_comandos_reales():
    for canonico in cli._ALIASES.values():
        assert canonico in cli._COMMANDS


def test_comandos_esperados_registrados():
    esperados = {
        "optimiza",
        "backlog",
        "vigilar",
        "trabajar",
        "revisar",
        "estado",
        "historial",
        "eventos",
        "objetivo",
        "ejecutar",
        "plan",
        "paso",
        "agentes",
        "memoria",
        "repaso",
        "autoevoluciona",
    }
    assert esperados <= set(cli._COMMANDS)


def test_plan_activar_apunta_al_archivo_del_repo(tmp_path, monkeypatch):
    # planes por repo: activar mueve el PUNTERO al archivo del repo (sin copiar)
    from agent import planner
    from agent.planner import Plan, Step

    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.config, "PLAN", tmp_path / "plan.json")

    repo = r"G:\algun_repo"
    destino = planner.repo_plan_path(repo)
    planner.save_plan(Plan("obj-x", "g", [Step(1, "a", "investigar", "d", [])], repo=repo), destino)
    cli._run_plan_cmd(["activar", repo])
    assert planner.active_plan_path().resolve() == destino.resolve()  # puntero, no copia
    assert not (tmp_path / "plan.json").exists()  # el archivo del repo es la fuente, no plan.json


def test_dispatch_sin_args_no_maneja():
    assert cli._dispatch([]) is False


def test_dispatch_help_lista_comandos(capsys):
    assert cli._dispatch(["help"]) is True
    assert "Comandos:" in capsys.readouterr().out


def test_dispatch_flag_lo_maneja_main():
    # --voz es un flag, no un subcomando: el dispatcher lo deja pasar a main.
    assert cli._dispatch(["--voz"]) is False


def test_dispatch_typo_sugiere_y_no_abre_repl(capsys):
    assert cli._dispatch(["trabjar"]) is True
    out = capsys.readouterr().out
    assert "desconocido" in out and "trabajar" in out


def test_dispatch_historial_vacio(capsys, tmp_path, monkeypatch):
    from agent import convlog

    monkeypatch.setattr(convlog.config, "CONVERSATIONS", tmp_path / "c.jsonl")
    assert cli._dispatch(["historial"]) is True
    assert "historial" in capsys.readouterr().out.lower()


def test_dispatch_historial_muestra_turnos(capsys, tmp_path, monkeypatch):
    from agent import convlog

    monkeypatch.setattr(convlog.config, "CONVERSATIONS", tmp_path / "c.jsonl")
    convlog.record("cuál es tu ruta", "vivo en C:/repos/escapement", backend="local")
    assert cli._dispatch(["log"]) is True  # alias de historial
    out = capsys.readouterr().out
    assert "cuál es tu ruta" in out and "escapement" in out


# --- deuda #4: `escapement eventos` da un consumidor al journal del bus -------------------------
def _aisla_eventos(tmp_path, monkeypatch):
    from agent import bus

    monkeypatch.setattr(bus.config, "EVENTS", tmp_path / "e.jsonl")
    return bus


def test_dispatch_eventos_vacio(capsys, tmp_path, monkeypatch):
    _aisla_eventos(tmp_path, monkeypatch)
    assert cli._dispatch(["eventos"]) is True
    assert "no tiene eventos" in capsys.readouterr().out.lower()


def test_dispatch_eventos_muestra_ts_topic_source_y_data(capsys, tmp_path, monkeypatch):
    bus = _aisla_eventos(tmp_path, monkeypatch)
    bus.publish("plan.step", {"paso": 2, "estado": "hecho"}, source="runner")
    bus.publish("voice.state", {"estado": "listening"})
    assert cli._dispatch(["events"]) is True  # alias de eventos
    out = capsys.readouterr().out
    assert "plan.step" in out and "(runner)" in out and '"paso": 2' in out
    assert "voice.state" in out and '"estado": "listening"' in out


def test_eventos_filtra_por_prefijo_de_topic(capsys, tmp_path, monkeypatch):
    bus = _aisla_eventos(tmp_path, monkeypatch)
    bus.publish("plan.step", {"paso": 1})
    bus.publish("plan.done", {})
    bus.publish("voice.state", {"estado": "idle"})
    for argv in (["--topic", "plan."], ["-t", "plan."]):
        cli._run_eventos(argv)
        out = capsys.readouterr().out
        assert "plan.step" in out and "plan.done" in out, argv
        assert "voice.state" not in out, argv  # mismo match por prefijo que bus.subscribe


def test_eventos_limit_posicional_flag_y_corto_equivalen(capsys, tmp_path, monkeypatch):
    bus = _aisla_eventos(tmp_path, monkeypatch)
    bus.publish("plan.step", {"paso": 1})
    bus.publish("plan.step", {"paso": 2})
    bus.publish("plan.done", {})
    for argv in (["1"], ["--limit", "1"], ["-n", "1"]):
        cli._run_eventos(argv)
        out = capsys.readouterr().out
        assert "plan.done" in out and "plan.step" not in out, argv  # solo el último
    cli._run_eventos(["--limit", "0"])  # 0 = todos (semántica de bus.read)
    out = capsys.readouterr().out
    assert '"paso": 1' in out and '"paso": 2' in out and "plan.done" in out


def test_eventos_filtro_sin_match_menciona_el_filtro(capsys, tmp_path, monkeypatch):
    bus = _aisla_eventos(tmp_path, monkeypatch)
    bus.publish("plan.step", {"paso": 1})
    cli._run_eventos(["--topic", "voice."])
    out = capsys.readouterr().out
    assert "no tiene eventos" in out.lower() and "voice." in out


def test_repos_por_estado_con_argv_devuelve_los_indicados():
    assert cli._repos_por_estado(["a", "b"], hechos=True) == ["a", "b"]
    assert cli._repos_por_estado(["x"], hechos=False) == ["x"]


def test_memoria_hecha_detecta_notas(tmp_path, monkeypatch):
    import os
    import re

    from agent.tools import memory

    monkeypatch.setattr(memory.config, "PROJECTS_DIR", tmp_path)
    ruta = r"G:\repo_x"
    slug = re.sub(r"[^a-zA-Z0-9]", "-", os.path.abspath(ruta))
    mem = tmp_path / slug / "memory"
    mem.mkdir(parents=True)
    (mem / "n.md").write_text(memory.build_note("n", "d", "reference", "b"), encoding="utf-8")
    assert cli._memoria_hecha("repo_x", ruta) is True
    assert cli._memoria_hecha("otro", r"G:\repo_vacio") is False


# --- mini-parser en los handlers: flags y posicionales conviven (agent.cliparse) --------------
def _fake_queue_result():
    return {"throttled": False, "procesadas": 0, "pendientes": 0, "fallidas": 0}


def test_trabajar_posicional_flag_y_corto_equivalen(monkeypatch):
    # trabajar 5 == trabajar --limit 5 == trabajar -n 5 == trabajar --limit=5; sin N => 0 (toda la cola)
    from agent import orchestrator

    cap = {}

    def fake_run_queue(limit, **kw):
        cap["limit"] = limit
        return _fake_queue_result()

    monkeypatch.setattr(orchestrator, "run_queue", fake_run_queue)
    for argv in (["5"], ["--limit", "5"], ["-n", "5"], ["--limit=5"]):
        cli._run_trabajar(argv)
        assert cap["limit"] == 5, argv
    cli._run_trabajar([])
    assert cap["limit"] == 0  # ausente => toda la cola


def test_paso_id_no_numerico_no_crashea(capsys):
    # antes int(argv[0]) reventaba con ValueError; ahora arg_int degrada a None -> uso
    cli._run_paso(["x", "hecho"])
    assert "uso:" in capsys.readouterr().out.lower()


def test_backlog_n_no_numerico_usa_default_sin_crashear(monkeypatch):
    from agent import backlog

    cap = {}

    def fake_scan(root, *, limit):
        cap["limit"] = limit
        return []

    monkeypatch.setattr(backlog, "scan_repo", fake_scan)
    cli._run_backlog(["abc", "xyz"])  # xyz no es número -> default 20 (antes: ValueError)
    assert cap["limit"] == 20
    cli._run_backlog(["--repo", "abc", "--limit", "7"])  # flags explícitos
    assert cap["limit"] == 7


def test_objetivo_flag_repo_separa_meta_de_repo(monkeypatch, tmp_path):
    from agent import planner, reasoning
    from agent.planner import Plan

    cap = {}

    def fake_plan(objetivo, context=None):
        cap["meta"] = objetivo
        return Plan(objetivo, "g", [])  # sin pasos: el handler retorna tras guardar

    monkeypatch.setattr(reasoning, "recall_trajectories", lambda o: "")
    monkeypatch.setattr(planner, "plan", fake_plan)
    monkeypatch.setattr(planner, "repo_plan_path", lambda repo: tmp_path / "p.json")
    monkeypatch.setattr(planner, "save_plan", lambda p, d: cap.__setitem__("repo", p.repo))
    monkeypatch.setattr(planner, "set_active_plan", lambda d: None)
    cli._run_objetivo(["--repo", "mirepo", "arregla", "el", "bug"])
    assert cap["meta"] == "arregla el bug"  # la frase libre no se contamina con el repo
    assert cap["repo"] == "mirepo"


def test_objetivo_escape_preserva_meta_con_guiones(monkeypatch, tmp_path):
    from agent import planner, reasoning
    from agent.planner import Plan

    cap = {}
    monkeypatch.setattr(reasoning, "recall_trajectories", lambda o: "")
    monkeypatch.setattr(
        planner, "plan", lambda o, context=None: (cap.__setitem__("meta", o), Plan(o, "g", []))[1]
    )
    monkeypatch.setattr(planner, "save_plan", lambda p, d: None)
    monkeypatch.setattr(planner, "set_active_plan", lambda d: None)
    cli._run_objetivo(["--", "--dashes", "en", "la", "meta"])
    assert cap["meta"] == "--dashes en la meta"  # tras '--' todo es literal


def test_help_de_un_comando_muestra_flags(capsys):
    assert cli._dispatch(["help", "trabajar"]) is True
    out = capsys.readouterr().out
    assert "flags:" in out and "--limit" in out


def test_help_de_alias_resuelve_al_canonico(capsys):
    cli._dispatch(["help", "log"])  # log -> historial
    assert "historial" in capsys.readouterr().out.lower()


def test_help_de_comando_desconocido_no_crashea(capsys):
    cli._dispatch(["help", "noexiste"])
    assert "no conozco" in capsys.readouterr().out.lower()


def test_plan_ver_imprime_roadmap_completo(capsys, tmp_path, monkeypatch):
    # QW5: leer el plan sin gastar tokens -> pasos, estados, notas y el próximo paso listo
    from agent import planner
    from agent.planner import Plan, Step

    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)

    repo = r"G:\repo_ver"
    destino = planner.repo_plan_path(repo)
    plan = Plan(
        "migrar el login",
        "el login usa OAuth",
        [
            Step(1, "leer el auth actual", "investigar", "sé cómo funciona", [], estado="hecho"),
            Step(2, "escribir el adaptador", "editar", "hay adaptador", [1]),
            Step(3, "correr los tests", "verificar", "verde", [2]),
        ],
        repo=repo,
    )
    planner.save_plan(plan, destino)
    cli._run_plan_cmd(["ver", repo])
    out = capsys.readouterr().out
    assert "migrar el login" in out and "1/3 pasos hechos" in out
    assert (
        "escribir el adaptador" in out and "sé cómo funciona" in out
    )  # acción y done sin recortar
    assert "próximo paso listo: 2" in out  # el 3 espera al 2: no está listo


def test_plan_ver_sin_repo_usa_el_activo(capsys, tmp_path, monkeypatch):
    from agent import planner
    from agent.planner import Plan, Step

    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)

    repo = r"G:\repo_activo"
    destino = planner.repo_plan_path(repo)
    planner.save_plan(
        Plan("obj-activo", "g", [Step(1, "a", "investigar", "d", [])], repo=repo), destino
    )
    planner.set_active_plan(destino)
    cli._run_plan_cmd(["ver"])
    out = capsys.readouterr().out
    assert "obj-activo" in out and "(activo)" in out


def _aisla_planes(tmp_path, monkeypatch):
    from agent import planner

    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)


def _plan_activo(monkeypatch, tmp_path, *estados):
    """Deja activo un plan de 3 pasos encadenados (1 -> 2 -> 3) con los estados dados."""
    from agent import planner
    from agent.planner import Plan, Step

    _aisla_planes(tmp_path, monkeypatch)
    repo = r"G:\repo_estado"
    pasos = [
        Step(1, "leer el auth actual", "investigar", "sé cómo funciona", []),
        Step(2, "escribir el adaptador", "editar", "hay adaptador", [1]),
        Step(3, "correr los tests", "verificar", "verde", [2]),
    ]
    for s, e in zip(pasos, estados, strict=False):
        s.estado = e
    destino = planner.repo_plan_path(repo)
    planner.save_plan(Plan("migrar el login", "el login usa OAuth", pasos, repo=repo), destino)
    planner.set_active_plan(destino)
    return destino


def test_estado_sin_plan_activo_lo_dice(capsys, tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    cli._resumen_plan()
    assert "sin plan activo" in capsys.readouterr().out.lower()


def test_estado_muestra_avance_y_proximo_paso_listo(capsys, tmp_path, monkeypatch):
    _plan_activo(monkeypatch, tmp_path, "hecho", "pendiente", "pendiente")
    cli._resumen_plan()
    out = capsys.readouterr().out
    assert "migrar el login" in out and "avance 1/3 pasos" in out
    assert "próximo paso listo: 2" in out  # el 3 espera al 2: no está listo


def test_estado_muestra_el_checkpoint_bloqueante(capsys, tmp_path, monkeypatch):
    from agent import planner

    destino = _plan_activo(monkeypatch, tmp_path, "hecho", "bloqueado", "pendiente")
    plan = planner.load_plan(destino)
    plan.pasos[1].nota = "falta   la\ncredencial"
    planner.save_plan(plan, destino)
    cli._resumen_plan()
    out = capsys.readouterr().out
    assert "checkpoint en el paso 2" in out and "escapement paso 2 hecho" in out
    assert "falta la credencial" in out  # nota aplanada a una línea


def test_estado_marca_el_roadmap_completo(capsys, tmp_path, monkeypatch):
    _plan_activo(monkeypatch, tmp_path, "hecho", "hecho", "hecho")
    cli._resumen_plan()
    out = capsys.readouterr().out
    assert "avance 3/3 pasos" in out and "roadmap completo" in out


def test_estado_roadmap_completo_pero_criterio_incompleto_muestra_gaps(
    capsys, tmp_path, monkeypatch
):
    # E2 pieza 3: con todo 'hecho' el dashboard refleja el veredicto, no solo el conteo
    from agent import planner

    monkeypatch.setattr(cli.config, "PLAN_REPLAN_MAX", 2)
    destino = _plan_activo(monkeypatch, tmp_path, "hecho", "hecho", "hecho")
    plan = planner.load_plan(destino)
    plan.eval_veredicto = "incompleto"
    plan.eval_gaps = ["falta   la\ndoc de worker/", "cubrir tests de y", "un tercer gap"]
    plan.replan_ciclos = 2
    planner.save_plan(plan, destino)
    cli._resumen_plan()
    out = capsys.readouterr().out
    assert "AÚN no se cumple" in out and "replanificación 2/2" in out
    assert "gap: falta la doc de worker/" in out  # aplanado a una línea
    assert "cubrir tests de y" in out
    assert "un tercer gap" not in out  # el dashboard muestra máximo 2 gaps
    assert "ciérralos a mano" in out
    assert "✓ roadmap completo" not in out  # el conteo ya no sella el cierre


def test_estado_roadmap_completo_con_criterio_verificado(capsys, tmp_path, monkeypatch):
    from agent import planner

    destino = _plan_activo(monkeypatch, tmp_path, "hecho", "hecho", "hecho")
    plan = planner.load_plan(destino)
    plan.eval_veredicto = "done"
    planner.save_plan(plan, destino)
    cli._resumen_plan()
    out = capsys.readouterr().out
    assert "roadmap completo · criterio global verificado" in out
    assert "Siguiente meta" in out


def test_estado_recorta_el_objetivo_largo(capsys, tmp_path, monkeypatch):
    # Los objetivos reales ocupan un párrafo; el dashboard es un resumen, no el roadmap.
    from agent import planner
    from agent.planner import Plan, Step

    _aisla_planes(tmp_path, monkeypatch)
    destino = planner.repo_plan_path(r"G:\repo_largo")
    planner.save_plan(
        Plan("x" * 500, "g", [Step(1, "a" * 500, "editar", "d", [])], repo=r"G:\repo_largo"),
        destino,
    )
    planner.set_active_plan(destino)
    cli._resumen_plan()
    assert all(len(ln) < 200 for ln in capsys.readouterr().out.splitlines())


def test_una_linea_aplana_y_recorta():
    assert cli._una_linea("hola\n  mundo") == "hola mundo"
    assert cli._una_linea("x" * 200).endswith("…") and len(cli._una_linea("x" * 200)) == 111


def test_plan_ver_sin_plan_no_crashea(capsys, tmp_path, monkeypatch):
    from agent import planner

    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    cli._run_plan_cmd(["ver", r"G:\no_existe"])
    assert "no hay plan" in capsys.readouterr().out.lower()


# --- deuda #6: `escapement plan quitar / mover / editar` sobre el plan activo -------------------
def test_plan_quitar_reconecta_y_persiste(capsys, tmp_path, monkeypatch):
    from agent import planner

    destino = _plan_activo(monkeypatch, tmp_path)
    cli._run_plan_cmd(["quitar", "2"])
    out = capsys.readouterr().out
    assert "reconectadas" in out and "Quedan 2 pasos" in out
    p = planner.load_plan(destino)
    assert [s.id for s in p.pasos] == [1, 3]
    assert p.pasos[1].depende_de == [1]  # el 3 hereda la dependencia del 2 quitado


def test_plan_quitar_sin_plan_activo_lo_dice(capsys, tmp_path, monkeypatch):
    _aisla_planes(tmp_path, monkeypatch)
    cli._run_plan_cmd(["quitar", "1"])
    assert "no hay plan activo" in capsys.readouterr().out


def test_plan_quitar_id_inexistente_avisa_sin_tocar_el_plan(capsys, tmp_path, monkeypatch):
    from agent import planner

    destino = _plan_activo(monkeypatch, tmp_path)
    cli._run_plan_cmd(["quitar", "99"])
    assert "no existe el paso 99" in capsys.readouterr().out
    assert len(planner.load_plan(destino).pasos) == 3


def test_plan_quitar_sin_id_muestra_uso(capsys, tmp_path, monkeypatch):
    _plan_activo(monkeypatch, tmp_path)
    cli._run_plan_cmd(["quitar"])
    assert "uso: escapement plan quitar" in capsys.readouterr().out


def test_plan_mover_valido_persiste_e_imprime_posicion(capsys, tmp_path, monkeypatch):
    from agent import planner
    from agent.planner import Step

    destino = _plan_activo(monkeypatch, tmp_path)
    p = planner.load_plan(destino)
    p.pasos.append(Step(4, "documentar", "crear", "hay README", [1]))
    planner.save_plan(p, destino)
    cli._run_plan_cmd(["mover", "4", "2"])  # el 4 solo depende del 1: puede ir justo después
    assert "paso 4 movido a la posición 2 de 4" in capsys.readouterr().out
    assert [s.id for s in planner.load_plan(destino).pasos] == [1, 4, 2, 3]


def test_plan_mover_invalido_no_persiste_y_avisa(capsys, tmp_path, monkeypatch):
    from agent import planner

    destino = _plan_activo(monkeypatch, tmp_path)
    cli._run_plan_cmd(["mover", "3", "1"])  # el 3 depende del 2: no puede ir antes
    out = capsys.readouterr().out
    assert "no se puede mover" in out and "queda como estaba" in out
    assert [s.id for s in planner.load_plan(destino).pasos] == [1, 2, 3]


def test_plan_editar_cambia_la_accion_y_persiste(capsys, tmp_path, monkeypatch):
    from agent import planner

    destino = _plan_activo(monkeypatch, tmp_path)
    cli._run_plan_cmd(["editar", "2", "escribir", "el", "adaptador", "OAuth"])
    assert "paso 2 ->" in capsys.readouterr().out
    paso = planner.load_plan(destino).pasos[1]
    assert paso.accion == "escribir el adaptador OAuth"
    assert paso.tipo == "editar" and paso.depende_de == [1]  # solo cambia la acción


def test_plan_editar_sin_texto_muestra_uso(capsys, tmp_path, monkeypatch):
    _plan_activo(monkeypatch, tmp_path)
    cli._run_plan_cmd(["editar", "2"])
    assert "uso: escapement plan editar" in capsys.readouterr().out


# --- M1: checkpoint de aprobación antes de la primera corrida --------------------------------
class _Stdin:
    """stdin falso: decide si `_aprobar_plan` cree estar en una terminal interactiva."""

    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _plan_para_ejecutar(tmp_path, monkeypatch, *, pasos=None, tty=True, respuesta="s"):
    """Deja `escapement ejecutar` listo para correr contra un plan de tmp_path. Devuelve las llamadas."""
    from agent import planner, runner
    from agent.planner import Plan, Step

    monkeypatch.setattr(planner.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(planner.config, "PLAN", tmp_path / "plan.json")
    monkeypatch.setattr(cli.config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty))
    monkeypatch.setattr("builtins.input", lambda _p: respuesta)

    repo = r"G:\repo_aprobar"
    destino = planner.repo_plan_path(repo)
    planner.save_plan(
        Plan("obj", "g", pasos or [Step(1, "hacer algo", "editar", "d", [])], repo=repo), destino
    )
    planner.set_active_plan(destino)
    corridas = []
    monkeypatch.setattr(runner, "run_plan", lambda p, r, **kw: corridas.append(r))
    return corridas


def test_ejecutar_pide_aprobacion_y_no_corre_si_dicen_que_no(capsys, tmp_path, monkeypatch):
    corridas = _plan_para_ejecutar(tmp_path, monkeypatch, respuesta="n")
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert "hacer algo" in out  # mostró el roadmap antes de preguntar
    assert corridas == []  # y no gastó nada


def test_ejecutar_corre_si_aprueban(tmp_path, monkeypatch):
    corridas = _plan_para_ejecutar(tmp_path, monkeypatch, respuesta="s")
    cli._run_ejecutar([])
    assert len(corridas) == 1


def test_ejecutar_sin_tty_no_pregunta(tmp_path, monkeypatch):
    # cron / `memoria` / `repaso`: desatendido corre igual que antes (no cuelga en un input)
    corridas = _plan_para_ejecutar(tmp_path, monkeypatch, tty=False, respuesta="n")
    cli._run_ejecutar([])
    assert len(corridas) == 1


def test_ejecutar_flag_si_salta_la_aprobacion(tmp_path, monkeypatch):
    corridas = _plan_para_ejecutar(tmp_path, monkeypatch, respuesta="n")
    for argv in (["--si"], ["-s"], ["--yes"]):
        cli._run_ejecutar(argv)
    assert len(corridas) == 3


def test_ejecutar_no_repregunta_en_un_plan_ya_empezado(tmp_path, monkeypatch):
    # reanudar tras un checkpoint es continuar algo aprobado, no una decisión nueva
    from agent.planner import Step

    pasos = [
        Step(1, "a", "editar", "d", [], estado="hecho"),
        Step(2, "b", "editar", "d", [1]),
    ]
    corridas = _plan_para_ejecutar(tmp_path, monkeypatch, pasos=pasos, respuesta="n")
    cli._run_ejecutar([])
    assert len(corridas) == 1


def test_ejecutar_con_aprobacion_apagada_no_pregunta(tmp_path, monkeypatch):
    corridas = _plan_para_ejecutar(tmp_path, monkeypatch, respuesta="n")
    monkeypatch.setattr(cli.config, "PLAN_APPROVAL", False)
    cli._run_ejecutar([])
    assert len(corridas) == 1


# --- deuda #8: checkpoint inline en TTY (resolver sin salir del proceso) ---------------------
def _paso_trabado(tmp_path, monkeypatch, *, tty=True, respuesta=""):
    """Plan activo con un paso `fallido` (nota con la causa), listo para `_checkpoint_inline`."""
    from agent import planner
    from agent.planner import Plan, Step

    _aisla_planes(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty))
    monkeypatch.setattr("builtins.input", lambda _p: respuesta)
    repo = r"G:\repo_chk"
    destino = planner.repo_plan_path(repo)
    p = Plan(
        "obj", "g", [Step(1, "a", "editar", "d", [], estado="fallido", nota="explotó")], repo=repo
    )
    planner.save_plan(p, destino)
    planner.set_active_plan(destino)
    return p, destino


def test_checkpoint_inline_hecho_persiste_la_nota(capsys, tmp_path, monkeypatch):
    from agent import planner

    p, destino = _paso_trabado(tmp_path, monkeypatch, respuesta="h ya lo resolví a mano")
    assert cli._checkpoint_inline(p.pasos[0], p, destino) is True
    paso = planner.load_plan(destino).pasos[0]
    assert paso.estado == "hecho" and paso.nota == "ya lo resolví a mano"


def test_checkpoint_inline_reintentar_conserva_la_nota(tmp_path, monkeypatch):
    from agent import planner

    p, destino = _paso_trabado(tmp_path, monkeypatch, respuesta="r")
    assert cli._checkpoint_inline(p.pasos[0], p, destino) is True
    paso = planner.load_plan(destino).pasos[0]
    assert paso.estado == "pendiente" and paso.nota == "explotó"  # la causa queda como contexto


def test_checkpoint_inline_afirmativa_tambien_marca_hecho(tmp_path, monkeypatch):
    # el reflejo del [s/N] de la aprobación: 's' funciona igual que 'h'
    from agent import planner

    p, destino = _paso_trabado(tmp_path, monkeypatch, respuesta="s")
    assert cli._checkpoint_inline(p.pasos[0], p, destino) is True
    assert planner.load_plan(destino).pasos[0].estado == "hecho"


def test_checkpoint_inline_enter_o_desconocido_salen_sin_tocar(tmp_path, monkeypatch):
    from agent import planner

    for respuesta in ("", "x"):
        p, destino = _paso_trabado(tmp_path, monkeypatch, respuesta=respuesta)
        assert cli._checkpoint_inline(p.pasos[0], p, destino) is False
        assert planner.load_plan(destino).pasos[0].estado == "fallido"


def test_checkpoint_inline_sin_tty_nunca_pregunta(tmp_path, monkeypatch):
    p, destino = _paso_trabado(tmp_path, monkeypatch, tty=False)
    monkeypatch.setattr("builtins.input", lambda _p: pytest.fail("no debe preguntar sin TTY"))
    assert cli._checkpoint_inline(p.pasos[0], p, destino) is False


def test_checkpoint_inline_con_aprobacion_apagada_no_pregunta(tmp_path, monkeypatch):
    p, destino = _paso_trabado(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.config, "PLAN_APPROVAL", False)
    monkeypatch.setattr("builtins.input", lambda _p: pytest.fail("no debe preguntar apagado"))
    assert cli._checkpoint_inline(p.pasos[0], p, destino) is False


def _ejecutar_con_checkpoint(tmp_path, monkeypatch, *, tty, respuesta):
    """`escapement ejecutar` contra un run_plan falso que falla la 1.ª corrida y completa la 2.ª."""
    from agent import planner, runner
    from agent.planner import Plan, Step

    _aisla_planes(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(tty))
    monkeypatch.setattr("builtins.input", lambda _p: respuesta)
    repo = r"G:\repo_inline"
    destino = planner.repo_plan_path(repo)
    # la nota marca el plan como "ya corrió": la aprobación previa no se re-abre y el único
    # input del test es el del checkpoint inline
    pasos = [Step(1, "a", "editar", "d", [], nota="ya corrió")]
    planner.save_plan(Plan("obj", "g", pasos, repo=repo), destino)
    planner.set_active_plan(destino)

    corridas = []

    def fake_run(p, r, **kw):
        corridas.append(r)
        p.pasos[0].estado = "fallido" if len(corridas) == 1 else "hecho"

    monkeypatch.setattr(runner, "run_plan", fake_run)
    return corridas


def test_ejecutar_checkpoint_inline_continua_en_el_mismo_proceso(capsys, tmp_path, monkeypatch):
    corridas = _ejecutar_con_checkpoint(tmp_path, monkeypatch, tty=True, respuesta="r")
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert len(corridas) == 2  # el checkpoint se resolvió inline y la corrida siguió
    assert "[checkpoint] paso 1 (fallido)" in out and "roadmap completo" in out


def test_ejecutar_checkpoint_sin_tty_termina_como_antes(capsys, tmp_path, monkeypatch):
    corridas = _ejecutar_con_checkpoint(tmp_path, monkeypatch, tty=False, respuesta="r")
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert len(corridas) == 1  # desatendido: ni pregunta ni sigue
    assert "checkpoint en el paso 1" in out and "escapement paso 1 hecho" in out


def test_ejecutar_checkpoint_inline_enter_sale_con_mensaje_de_reanudacion(
    capsys, tmp_path, monkeypatch
):
    corridas = _ejecutar_con_checkpoint(tmp_path, monkeypatch, tty=True, respuesta="")
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert len(corridas) == 1
    assert "escapement paso 1 hecho" in out  # el camino manual de siempre sigue impreso


# --- Evaluación global al cerrar (E2, deuda #7): visibilidad del veredicto en `ejecutar` ---


def _ejecutar_con_veredicto(
    tmp_path, monkeypatch, *, veredicto, gaps, plan_eval=True, replan_ciclos=0, replan_max=2
):
    """`escapement ejecutar` contra un run_plan falso que completa el plan y deja el veredicto dado."""
    from agent import planner, runner
    from agent.planner import Plan, Step

    _aisla_planes(tmp_path, monkeypatch)
    monkeypatch.setattr(cli.config, "PLAN_EVAL", plan_eval)
    monkeypatch.setattr(cli.config, "PLAN_REPLAN_MAX", replan_max)
    monkeypatch.setattr(cli.sys, "stdin", _Stdin(False))  # sin TTY: ni aprobación ni rescate
    repo = r"G:\repo_eval"
    destino = planner.repo_plan_path(repo)
    pasos = [Step(1, "documenta worker/", "editar", "d", [], nota="ya corrió")]
    planner.save_plan(Plan("obj", "100% type hints en worker/", pasos, repo=repo), destino)
    planner.set_active_plan(destino)

    def fake_run(p, r, **kw):
        p.pasos[0].estado = "hecho"
        p.eval_veredicto, p.eval_gaps = veredicto, gaps
        p.replan_ciclos = replan_ciclos

    monkeypatch.setattr(runner, "run_plan", fake_run)


def test_ejecutar_veredicto_incompleto_muestra_criterio_y_gaps(capsys, tmp_path, monkeypatch):
    _ejecutar_con_veredicto(
        tmp_path, monkeypatch, veredicto="incompleto", gaps=["anotar worker/utils.py"]
    )
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert "AÚN no se cumple" in out
    assert "100% type hints en worker/" in out  # el criterio que quedó pendiente
    assert "anotar worker/utils.py" in out  # el gap concreto
    assert "roadmap completo ✓" not in out  # el conteo ya no sella el cierre
    assert "Replanificación automática agotada" not in out  # con ciclos libres, mensaje genérico


def test_ejecutar_incompleto_con_tope_agotado_anuncia_checkpoint(capsys, tmp_path, monkeypatch):
    # E2 pieza 2: el plan ya consumió sus ciclos de replanificación -> checkpoint humano explícito
    _ejecutar_con_veredicto(
        tmp_path, monkeypatch, veredicto="incompleto", gaps=["anotar worker/utils.py"],
        replan_ciclos=2, replan_max=2,
    )
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert "Replanificación automática agotada" in out
    assert "2 ciclo(s)" in out and "tope 2" in out


def test_ejecutar_veredicto_done_imprime_el_sello(capsys, tmp_path, monkeypatch):
    _ejecutar_con_veredicto(tmp_path, monkeypatch, veredicto="done", gaps=[])
    cli._run_ejecutar([])
    assert "roadmap completo ✓ · criterio global verificado" in capsys.readouterr().out


def test_ejecutar_sin_veredicto_cierra_como_antes(capsys, tmp_path, monkeypatch):
    # best-effort: la evaluación no produjo veredicto -> el cierre por conteo de siempre, sin sello
    _ejecutar_con_veredicto(tmp_path, monkeypatch, veredicto="", gaps=[])
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert "roadmap completo ✓" in out and "criterio global verificado" not in out


def test_ejecutar_eval_off_ignora_veredicto_viejo(capsys, tmp_path, monkeypatch):
    # AGENT_PLAN_EVAL=0: un 'incompleto' pegado en el JSON de otra corrida no cambia el cierre
    _ejecutar_con_veredicto(
        tmp_path, monkeypatch, veredicto="incompleto", gaps=["gap viejo"], plan_eval=False
    )
    cli._run_ejecutar([])
    out = capsys.readouterr().out
    assert "roadmap completo ✓" in out and "gap viejo" not in out


def test_cmd_con_help_flag_no_ejecuta_handler(capsys, monkeypatch):
    from agent import orchestrator

    llamado = {"v": False}

    def boom(*a, **k):
        llamado["v"] = True
        return _fake_queue_result()

    monkeypatch.setattr(orchestrator, "run_queue", boom)
    assert cli._dispatch(["trabajar", "--help"]) is True
    assert llamado["v"] is False  # --help en 1.ª posición muestra ayuda, no corre la cola
    assert "uso:" in capsys.readouterr().out.lower()
