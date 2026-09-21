"""CLI del orquestador: subcomandos (optimiza/vigilar/trabajar/...) + entry point.

``main`` despacha subcomandos por tabla; sin subcomando abre el REPL de texto (``agent.repl``)
o el daemon de voz (``agent.voice.daemon``). Los handlers usan lazy imports del orquestador para
que el arranque del CLI sea rápido.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from agent import cliparse, config, ledger
from agent.repl import run_repl
from agent.session import build_options

if TYPE_CHECKING:  # solo para tipar: los handlers importan el planner en caliente (arranque rápido)
    from agent.planner import Plan, Step


def _run_optimiza(argv: list[str]) -> None:
    """`escapement optimiza <ruta> [directiva]` -> orquestador: rama + Claude Code + tests + PR."""
    from agent.orchestrator import git_root, optimize

    pos, opts = cliparse.split_args(argv)
    if not pos:
        print("uso: escapement optimiza <ruta_al_archivo> [directiva]   (flag: --directiva <texto>)")
        return
    path = Path(pos[0]).resolve()
    directiva = cliparse.opt_str(opts, "directiva") or (
        pos[1]
        if len(pos) > 1
        else "optimiza: legibilidad, type hints, docstrings y eficiencia, preservando el comportamiento"
    )
    repo = git_root(path)
    target = str(path.relative_to(repo)) if path.is_file() else "."
    print(f"[orquestador] repo={repo} target={target}")
    print(f"  directiva: {directiva}\n  despachando a Claude Code (rama aislada)...")
    res = optimize(repo, directiva, target, python_exe=sys.executable)
    print(f"\n  rama        : {res.branch}")
    print(f"  verificación: {res.verdict}")
    print(f"  veredicto   : {'SEGURO ✓' if res.verified else 'RECHAZADO ✗ (sin PR)'}")
    print(f"  diff        : {res.diff_stat or '(sin cambios)'}")
    print(f"  PR          : {res.pr_url or '(sin remoto: revisa la rama local)'}")


def _run_backlog(argv: list[str]) -> None:
    """`escapement backlog <ruta> [N]` -> muestra los candidatos de mejora del repo."""
    from agent.backlog import scan_repo

    pos, opts = cliparse.split_args(argv, short_map={"n": "limit"})
    repo_arg = cliparse.opt_str(opts, "repo") or (pos[0] if pos else "")
    root = Path(_resolve_repo(repo_arg)) if repo_arg else Path.cwd()
    limit = cliparse.opt_int(opts, "limit", "n", default=cliparse.arg_int(pos, 1, default=20))
    cands = scan_repo(root, limit=limit)
    print(f"[backlog] {root} — {len(cands)} candidatos (más deuda primero):")
    for i, c in enumerate(cands, 1):
        print(f"  {i:2}. [{c.score:2}] {c.path}  ({', '.join(c.reasons)})")


def _scan_to_tasks(root: Path, n: int) -> list[dict]:
    """Convierte los candidatos de deuda del repo en tareas encolables.

    Excluye los targets cuyo PR rechazaste (aprendizaje: no re-proponer lo descartado).
    """
    from agent.backlog import scan_repo
    from agent.orchestrator import _current_branch

    base = _current_branch(root)
    rechazados = ledger.rejected_targets(str(root))
    return [
        {
            "repo": str(root),
            "target": c.path,
            "base": base,
            "directiva": (
                f"optimiza {c.path}: añade type hints y docstrings, mejora legibilidad y "
                f"eficiencia, preservando el comportamiento ({', '.join(c.reasons)})"
            ),
        }
        for c in scan_repo(root, limit=n)
        if c.path not in rechazados
    ]


def _run_vigilar(argv: list[str]) -> None:
    """`escapement vigilar <ruta> [N]` -> escanea deuda y ENCOLA candidatos (no ejecuta)."""
    from agent import state

    pos, opts = cliparse.split_args(argv, short_map={"n": "limit"})
    repo_arg = cliparse.opt_str(opts, "repo") or (pos[0] if pos else "")
    root = Path(_resolve_repo(repo_arg)) if repo_arg else Path.cwd()
    n = cliparse.opt_int(opts, "limit", "n", default=cliparse.arg_int(pos, 1, default=10))
    added = state.enqueue(_scan_to_tasks(root, n))
    print(
        f"[vigilar] {root}: {added} tareas encoladas ({len(state.pending())} pendientes en total)."
    )


def _run_trabajar(argv: list[str]) -> None:
    """`escapement trabajar [N]` -> procesa la cola respetando la cuota. Reanudable (Ctrl-C seguro)."""
    from agent.orchestrator import run_queue

    pos, opts = cliparse.split_args(argv, short_map={"n": "limit"})
    # N ausente o 0 => procesa toda la cola
    limit = cliparse.opt_int(opts, "limit", "n", default=cliparse.arg_int(pos, 0, default=0))
    r = run_queue(
        limit,
        python_exe=sys.executable,
        on_start=lambda t: print(f"\n[trabajar] #{t['id']} {t['target']}"),
        on_done=lambda t, res: print(
            f"    {'SEGURO ✓' if res.verified else 'RECHAZADO ✗'} | PR {res.pr_url or '(local)'}"
        ),
    )
    if r["throttled"]:
        print(
            "[trabajar] ⏸ cuota de la suscripción agotada — cola pausada. Reanuda con 'escapement trabajar'."
        )
    elif r["procesadas"] == 0 and not r["fallidas"]:
        print("[trabajar] cola vacía.")
    resumen = f"[trabajar] procesadas {r['procesadas']}; pendientes {r['pendientes']}"
    if r["fallidas"]:
        resumen += f"; fallidas {r['fallidas']}"
    print(resumen + ".")


def _run_autoevoluciona(argv: list[str]) -> None:
    """`escapement autoevoluciona <ruta> [N]` = vigilar + trabajar (escanea, encola y procesa)."""
    _run_vigilar(argv)
    _run_trabajar([])


def _resumen_plan() -> None:
    """Bloque del plan ACTIVO para ``escapement estado``: objetivo, avance y qué toca ahora.

    Vista corta (cabe en tres líneas) del mismo estado que ``escapement plan ver`` imprime entero: el
    dashboard tiene que responder "¿en qué voy?" sin obligar a recordar otro comando. No muta ni
    persiste nada.
    """
    from agent import planner, runner

    path = planner.active_plan_path()
    plan = planner.load_plan(path)
    if plan is None or not plan.pasos:
        print("[plan] sin plan activo. Genera uno con 'escapement objetivo [repo] \"<meta>\"'.")
        return
    hechos = sum(1 for s in plan.pasos if s.estado == "hecho")
    print(f"[plan] {_una_linea(plan.objetivo, 160)}")
    print(f"  avance {hechos}/{len(plan.pasos)} pasos · {path.name} · {plan.repo or '(scratch)'}")
    bloq = next((s for s in plan.pasos if s.estado in ("bloqueado", "fallido")), None)
    if bloq:
        print(
            f"  {_ICONOS[bloq.estado]} checkpoint en el paso {bloq.id}. [{bloq.tipo}] {_una_linea(bloq.accion)}"
        )
        if bloq.nota:
            print(f"      {_una_linea(bloq.nota)}")
        print(f"      resuélvelo y marca 'escapement paso {bloq.id} hecho', luego 'escapement ejecutar'.")
    elif hechos == len(plan.pasos):
        # E2 (deuda #7): con todo 'hecho' el done real lo dicta el veredicto de la evaluación
        # global, no el conteo. "" = no hubo evaluación (gate apagado, sin criterio o best-effort).
        if plan.eval_veredicto == "incompleto":
            print(
                "  ⏸ roadmap completo pero el criterio global AÚN no se cumple"
                f" · replanificación {plan.replan_ciclos}/{config.PLAN_REPLAN_MAX}"
            )
            for gap in plan.eval_gaps[:2]:
                print(f"      gap: {_una_linea(gap)}")
            print("      ciérralos a mano o replantea con 'escapement objetivo [repo] \"<meta>\"'.")
        elif plan.eval_veredicto == "done":
            print(
                "  ✓ roadmap completo · criterio global verificado."
                " Siguiente meta con 'escapement objetivo [repo] \"<meta>\"'."
            )
        else:
            print("  ✓ roadmap completo. Siguiente meta con 'escapement objetivo [repo] \"<meta>\"'.")
    elif listos := runner.pasos_listos(plan):
        s = listos[0]
        print(
            f"  → próximo paso listo: {s.id}. [{s.tipo}] {_una_linea(s.accion)}  ('escapement ejecutar')"
        )


def _una_linea(texto: str, limite: int = 110) -> str:
    """Aplana un texto a UNA línea recortada (el dashboard es un resumen, no el roadmap entero).

    ``plan ver`` imprime objetivos, acciones y notas sin recortar; aquí no caben: el objetivo de un
    plan real ocupa un párrafo.

    Args:
        texto: el texto original (puede traer saltos de línea y espacios repetidos).
        limite: largo máximo antes del recorte; al superarlo se corta y se agrega "…".
    """
    plano = " ".join(texto.split())
    return plano if len(plano) <= limite else plano[:limite] + "…"


def _run_estado(argv: list[str]) -> None:
    """`escapement estado` -> dashboard: plan activo, cola pendiente, throttle y resumen del ledger."""
    from agent import state

    _resumen_plan()
    ps = state.pending()
    thr = state.throttled_until()
    linea = f"[estado] cola: {len(ps)} pendientes"
    if thr:
        linea += f" | ⏸ cuota agotada desde {thr}"
    print(linea)
    for task in ps[:10]:
        print(f"  · #{task['id']} {task['target']}")

    opt = [r for r in ledger.read() if r.get("action") == "optimize"]
    seguras = sum(1 for r in opt if r.get("verified"))
    mejoras = sum(1 for r in opt if r.get("improved"))
    print(f"[ledger] {len(opt)} optimizaciones | {seguras} seguras | {mejoras} con mejora medida")
    st = ledger.stats()
    if st["total"]:
        print(
            f"[obs] éxito {st['tasa_exito']:.0%} | prom {st['duracion_prom_s']}s | "
            f"bloqueadas {st['bloqueadas_secretos']} | motores {st['por_executor']}"
        )
    for r in opt[-5:]:
        marca = "✓" if r.get("verified") else "✗"
        print(f"  {marca} {r.get('target')} — {r.get('eval') or r.get('tests_verdict')}")

    reviews = ledger.latest_pr_reviews()
    if reviews:
        from collections import Counter

        conteo = Counter(reviews.values())
        print(
            f"[reviews] {conteo.get('accepted', 0)} aceptados | "
            f"{conteo.get('rejected', 0)} rechazados | {conteo.get('pending', 0)} pendientes"
        )


def _run_revisar(argv: list[str]) -> None:
    """`escapement revisar` -> consulta el estado de sus PRs (gh) y aprende del accept/reject."""
    from agent.orchestrator import review_prs

    nuevos = review_prs()
    if not nuevos:
        print("[revisar] sin cambios: no hay PRs nuevos ni resueltos desde la última revisión.")
        return
    iconos = {"accepted": "✓ aceptado", "rejected": "✗ rechazado", "pending": "· pendiente"}
    for r in nuevos:
        print(f"  {iconos[r['result']]}: {r['target']} ({r['pr_url']})")
    aceptados = sum(1 for r in nuevos if r["result"] == "accepted")
    rechazados = sum(1 for r in nuevos if r["result"] == "rejected")
    print(
        f"[revisar] {len(nuevos)} actualizaciones | "
        f"{aceptados} aceptados, {rechazados} rechazados aprendidos."
    )


def _run_historial(argv: list[str]) -> None:
    """`escapement historial [N]` -> últimos N turnos de conversación (local y Claude)."""
    from agent import convlog

    pos, opts = cliparse.split_args(argv, short_map={"n": "limit"})
    n = cliparse.opt_int(opts, "limit", "n", default=cliparse.arg_int(pos, 0, default=15))
    turnos = convlog.read(limit=n)
    if not turnos:
        print("[historial] sin conversaciones registradas todavía.")
        return
    print(f"[historial] últimos {len(turnos)} turnos (más reciente al final):")
    for t in turnos:
        print(f"\n[{t.get('ts', '')}] ({t.get('backend', '?')}) tú › {t.get('user', '')}")
        print(f"  {config.AGENT_NAME} › {t.get('reply', '')}")


def _run_eventos(argv: list[str]) -> None:
    """`escapement eventos [N] [--topic <prefijo>]` -> últimos N eventos del journal del bus."""
    import json

    from agent import bus

    pos, opts = cliparse.split_args(argv, short_map={"n": "limit", "t": "topic"})
    n = cliparse.opt_int(opts, "limit", "n", default=cliparse.arg_int(pos, 0, default=20))
    prefijo = cliparse.opt_str(opts, "topic", "t") or None
    eventos = bus.read(limit=n, topic=prefijo)
    if not eventos:
        filtro = f" con topic '{prefijo}'" if prefijo else ""
        print(f"[eventos] el journal no tiene eventos{filtro} todavía.")
        return
    encabezado = f"[eventos] últimos {len(eventos)}"
    if prefijo:
        encabezado += f" con topic '{prefijo}'"
    print(encabezado + " (más reciente al final):")
    for e in eventos:
        linea = f"[{e.get('ts', '')}] {e.get('topic', '?')}"
        if e.get("source"):
            linea += f" ({e['source']})"
        data = e.get("data") or {}
        if data:
            linea += " " + json.dumps(data, ensure_ascii=False, default=str)
        print(linea)


# Respuestas que cuentan como "sí" en los prompts interactivos (rescate a rama, aprobación del plan).
_AFIRMATIVAS = frozenset({"s", "si", "sí", "y", "yes"})


def _respuesta(prompt: str) -> str | None:
    """Pregunta por stdin y devuelve lo tecleado. ``None`` = no hay humano que conteste.

    Unifica los dos modos de "nadie va a responder" que antes se trataban distinto (deuda #14):
    el guard ``sys.stdin.isatty()`` (cron, pipe) y el ``EOFError`` que ese guard NO atrapa en
    Windows —un proceso lanzado sin consola recibe ``NUL`` como stdin, que SÍ es un char
    device, así que ``isatty()`` devuelve True y el ``input()`` muere al leer—. Ambos casos
    devuelven None y quien llama aplica su default seguro, en vez de reventar con traceback
    (exit 1) al final de una corrida que salió bien.

    Args:
        prompt: texto que se muestra antes de leer (igual que ``input``).

    Returns:
        La línea tecleada SIN normalizar (quien llama hace su ``strip``/``lower``), o ``None``
        si no hay canal interactivo usable: sin TTY, stdin en EOF, o descriptor cerrado/inválido.
    """
    try:
        if not sys.stdin or not sys.stdin.isatty():
            return None
        return input(prompt)
    except (EOFError, OSError, ValueError):
        # EOFError: stdin sin datos (background, NUL, heredoc agotado). OSError/ValueError:
        # descriptor cerrado o inválido (pythonw, servicio, subproceso sin stdin). Ninguno debe
        # tumbar el comando: son exactamente el caso "desatendido".
        return None


# Estado de un paso -> icono, para las vistas del plan (`ejecutar`, `plan ver`).
_ICONOS = {"hecho": "✓", "fallido": "✗", "bloqueado": "⏸", "pendiente": "·"}


def _es_repo(arg: str) -> bool:
    """True si ``arg`` identifica un repo (clave de REPOS o ruta existente)."""
    return arg in config.REPOS or Path(arg).exists()


def _resolve_repo(arg: str) -> str:
    """Resuelve un repo por nombre (clave de REPOS), ruta o slug -> ruta raíz del repo.

    Delega en ``orchestrator.resolve_repo`` (fuente única, compartida con las tools de chat/voz).
    """
    from agent.orchestrator import resolve_repo

    return resolve_repo(arg)


def _repo_utilizable(repo: str, origen: str) -> bool:
    """True si ``repo`` es una carpeta existente; si no, explica el porqué e indica que no sigas.

    Deuda #16. ``ejecutar`` resuelve el plan por el SLUG de la ruta
    (:func:`config.vault_slug` machaca todo lo no alfanumérico a ``-``), así que una ruta con typo
    cae en el MISMO archivo de plan que la buena —``G:\\repo_x`` y ``G:\\repo-x`` comparten slug—:
    sin esta validación el plan carga, la corrida arranca, y el primer código que usa ``repo`` de
    verdad muere feo y varios pasos adentro (``NotADirectoryError``, o un WinError 267 desde un
    subproceso de git). Peor: la ruta mala se persiste en ``plan.repo`` al pasar por aquí. Fallar
    de entrada, con la ruta a la vista, cuesta un ``is_dir()``.

    Args:
        repo: ruta ya resuelta por :func:`_resolve_repo`, o la que el plan recuerda.
        origen: de dónde salió, para el mensaje (``"'<arg>'"`` o ``"el plan"``).

    Returns:
        True si es un directorio existente. False —con el diagnóstico ya impreso— si no existe,
        si es un archivo, o si la ruta ni siquiera es consultable: ``is_dir()`` devuelve False
        ante cualquier OSError/ValueError (unidad caída, permisos, nombre ilegal), no levanta.
    """
    if Path(repo).is_dir():
        return True
    print(f"[ejecutar] {origen} apunta a una carpeta que no existe: {repo}")
    print("  revisa la ruta (o usa una clave de REPOS) y reintenta.")
    return False


def _reporte_traza(plan, repo: str, *, completo: bool, mostrar_vacio: bool = True) -> None:
    """Traza de archivos modificados (árbol) al terminar un plan + oferta de rescate a una rama.

    Cierra ``ejecutar``/``memoria`` con la trazabilidad que pediste: un árbol de los archivos que
    el plan tocó en su worktree (con su ruta), para no tener que rastrear el worktree a mano. Si el
    plan quedó COMPLETO y hay cambios, ofrece traspasar el trabajo a una rama nueva basada en la
    rama por defecto (rescate). El prompt de rescate solo se hace con stdin interactivo (TTY): en
    modo headless/voz no se pregunta —se imprime el comando manual— para no bloquear.

    Args:
        plan: el plan ejecutado (se leen ``workdir``/``rama``/``repo``).
        repo: raíz del repo real.
        completo: True si el roadmap quedó entero en 'hecho' (habilita la oferta de rescate).
        mostrar_vacio: si False, no imprime nada cuando el plan no modificó archivos (evita ruido
            en el bucle de ``memoria``, cuyos planes escriben al vault, no al worktree).
    """
    from agent import runner

    rutas = runner.archivos_del_plan(plan)
    if not rutas:
        if mostrar_vacio and plan.workdir:
            print("\n[traza] el plan no dejó archivos modificados en el worktree.")
        return
    print("\n[traza] archivos modificados por el plan:")
    print(f"  worktree: {plan.workdir}")
    print(f"  rama:     {plan.rama}")
    for ln in runner.arbol_archivos(rutas).splitlines():
        print(f"  {ln}")
    if not completo:
        return
    base = runner.default_branch(repo) or "(default)"
    resp = _respuesta(
        f"\n[rescate] ¿traspasar el trabajo a una rama nueva basada en '{base}'? [s/N] "
    )
    if resp is None:
        print(
            f"\n[rescate] para traspasar el trabajo a una rama basada en '{base}', re-lanza en una "
            "terminal interactiva y responde 's', o hazlo a mano desde la rama del worktree."
        )
        return
    if resp.strip().lower() not in _AFIRMATIVAS:
        print("  ok, lo dejo en el worktree del plan.")
        return
    res = runner.traspasar_a_rama(plan, repo)
    if res.ok:
        print(f"  ✓ trabajo traspasado a la rama '{res.rama}' (basada en '{res.base}').")
        print(f"    revísalo:  git -C {repo} checkout {res.rama}")
    else:
        print(f"  ✗ no se pudo traspasar: {res.motivo}")
        print(f"    el trabajo sigue seguro en la rama del worktree: {plan.rama}")


def _plan_por_arg(arg: str) -> Path:
    """Archivo de plan de un repo/ruta/slug: su plan dedicado, o ``plan_<slug>.json`` si dieron el slug.

    Resolución única compartida por ``plan ver`` y ``plan activar``; el archivo puede no existir
    (el llamador decide qué decir en ese caso).
    """
    from agent import planner

    cand = planner.repo_plan_path(_resolve_repo(arg))
    if not cand.exists():
        alt = config.DATA_DIR / f"plan_{arg}.json"  # por si pasaron el slug directo
        return alt if alt.exists() else cand
    return cand


def _render_plan(p: Plan, path: Path, *, activo: bool) -> None:
    """Imprime un plan ÍNTEGRO: objetivo, avance, cada paso con su done/nota, y qué sigue.

    Es la vista de ``escapement plan ver`` y la que se muestra en el checkpoint de aprobación previo a
    ejecutar. A diferencia de la tool conversacional ``plan`` (que recorta acciones y notas para
    caber en un turno), aquí no se recorta nada: la terminal no paga tokens y este es el lugar donde
    se lee el roadmap de verdad antes de aprobarlo.

    Args:
        p: plan cargado (no se muta).
        path: archivo del que salió; se muestra su nombre para ubicarse entre repos.
        activo: si es el plan activo (lo marca en el encabezado).
    """
    from agent import runner

    hechos = sum(1 for s in p.pasos if s.estado == "hecho")
    print(f"[plan] {p.objetivo}")
    print(f"  archivo: {path.name}{' (activo)' if activo else ''}")
    print(f"  repo:    {p.repo or '(scratch)'}")
    print(f"  avance:  {hechos}/{len(p.pasos)} pasos hechos")
    print(f"  done del objetivo: {p.criterio_global}")
    if p.workdir:
        print(f"  worktree: {p.workdir}  ·  rama: {p.rama}")
    for s in p.pasos:
        dep = f"  (tras {', '.join(map(str, s.depende_de))})" if s.depende_de else ""
        quien = f" @{s.persona}" if s.persona else ""
        print(f"   {_ICONOS.get(s.estado, '?')} {s.id}. [{s.tipo}]{quien} {s.accion}{dep}")
        print(f"       done: {s.done}")
        if s.nota:
            print(f"       nota: {s.nota}")
    bloq = next((s for s in p.pasos if s.estado in ("bloqueado", "fallido")), None)
    if bloq:
        print(
            f"\n  checkpoint en el paso {bloq.id} ({bloq.estado}). Resuélvelo y marca "
            f"'escapement paso {bloq.id} hecho', luego 'escapement ejecutar'."
        )
    elif p.pasos and hechos == len(p.pasos):
        print("\n  roadmap completo ✓")
    elif listos := runner.pasos_listos(p):
        s = listos[0]
        print(f"\n  próximo paso listo: {s.id}. [{s.tipo}] {s.accion}")


def _aprobar_plan(p: Plan, path: Path, *, saltar: bool) -> bool:
    """Checkpoint humano ANTES de la primera corrida de un plan: enseña el roadmap y pide el OK.

    El planner PROPONE y el humano APRUEBA. Sin esta puerta, ``objetivo`` -> ``ejecutar`` salta de
    una meta en lenguaje natural a una corrida autónoma que gasta presupuesto sin que nadie haya
    leído los pasos. Solo pregunta la PRIMERA vez: reanudar tras un checkpoint es continuar algo ya
    aprobado, no una decisión nueva.

    Args:
        p: plan a punto de ejecutarse.
        path: archivo del plan (encabezado de la vista).
        saltar: True si la invocación trae ``--si`` (aprobación explícita en la línea de comandos).

    Returns:
        True si se puede ejecutar. False solo cuando el humano dijo que no en una terminal
        interactiva: sin TTY (cron, pipeline autónomo) o con la aprobación apagada
        (``AGENT_PLAN_APPROVAL=0``) nunca bloquea, para no colgar una corrida desatendida.
    """
    if saltar or not config.PLAN_APPROVAL or not p.pasos:
        return True
    if any(s.estado != "pendiente" or s.nota for s in p.pasos):
        return True  # el plan ya corrió: reanudar no re-abre la aprobación
    if not sys.stdin.isatty():
        return True  # desatendido: ejecuta sin preguntar (comportamiento previo intacto)
    _render_plan(p, path, activo=True)
    resp = _respuesta(f"\n[aprobación] ¿ejecuto este roadmap ({len(p.pasos)} pasos)? [s/N] ")
    if resp is None:
        return True  # stdin se dijo TTY pero nadie contesta: mismo default que sin TTY
    if resp.strip().lower() in _AFIRMATIVAS:
        return True
    print(
        "  ok, no ejecuto nada. Replantea con 'escapement objetivo [repo] \"<meta>\"' o ajusta el "
        f"roadmap ({path.name}) con 'escapement plan quitar/mover/editar'."
    )
    return False


def _checkpoint_inline(paso: Step, p: Plan, path: Path) -> bool:
    """Checkpoint inline (deuda #8): resolver el paso trabado en el momento, sin salir del proceso.

    Cuando el runner se frena a mitad de camino (``bloqueado``/``fallido``), antes había que salir
    y reanudar con ``escapement paso <id> ... && escapement ejecutar``. En una terminal interactiva esta
    puerta pregunta ahí mismo y la corrida continúa en el mismo proceso.

    Mismo respeto por el no-TTY que :func:`_aprobar_plan`: sin TTY (cron, un pipe) o con la
    aprobación apagada (``AGENT_PLAN_APPROVAL=0``) NUNCA pregunta — devuelve False y el flujo
    termina con el mensaje de reanudación de siempre.

    Args:
        paso: el paso que frenó la corrida (su nota trae la causa o la pregunta de divergencia).
        p: plan cargado; si el humano responde, se muta y se persiste aquí mismo.
        path: archivo del plan (donde persistir la respuesta).

    Returns:
        True si el paso quedó resuelto (``hecho`` o ``pendiente``) y la corrida debe continuar;
        False para terminar como antes (desatendido, respuesta vacía o no reconocida).
    """
    from agent import planner

    if not config.PLAN_APPROVAL or not sys.stdin.isatty():
        return False  # desatendido: termina con el mensaje de reanudación (comportamiento previo)
    print(f"\n[checkpoint] paso {paso.id} ({paso.estado}): {_una_linea(paso.nota or paso.accion)}")
    resp = (
        _respuesta(
            "  ¿cómo sigo? [h]echo y continuar · [r]eintentar el paso · [Enter] salir\n"
            '  ("h <texto>" guarda tu decisión como nota del paso): '
        )
        or ""
    ).strip()
    if not resp:
        return False  # None (nadie contesta) o Enter: salir, el default seguro de siempre
    verbo, _, nota = resp.partition(" ")
    verbo = verbo.lower()
    estados = {"h": "hecho", "hecho": "hecho", "r": "pendiente", "reintentar": "pendiente"}
    estado = estados.get(verbo) or ("hecho" if verbo in _AFIRMATIVAS else None)
    if estado is None:
        return False  # respuesta no reconocida = salir (default seguro, como el [s/N])
    planner.set_step_state(p, paso.id, estado, nota.strip() or None)
    planner.save_plan(p, path)
    print(f"  paso {paso.id} -> {estado}. Sigo con el plan…")
    return True


def _run_objetivo(argv: list[str]) -> None:
    """`escapement objetivo [repo] "<meta>"` -> el planner propone un roadmap (Fase A).

    Si el primer arg es un repo (clave de REPOS o ruta), el plan se guarda en el archivo DEDICADO
    de ese repo (data/plan_<slug>.json) y queda activo; si no, va al slot scratch (plan.json).
    """
    from agent import planner, reasoning

    pos, opts = cliparse.split_args(argv, stop_at_positional=True)
    repo_flag = cliparse.opt_str(opts, "repo")
    if repo_flag:  # --repo explícito: sin ambigüedad con la meta
        repo = _resolve_repo(repo_flag)
        meta = pos
    elif pos and _es_repo(pos[0]):  # heurística de hoy: el 1er posicional como repo
        repo = _resolve_repo(pos[0])
        meta = pos[1:]
    else:
        repo = None
        meta = pos
    if not meta:
        print('uso: escapement objetivo [repo] "<lo que quieres lograr>"   (flag: --repo <repo>)')
        return
    objetivo = " ".join(meta)
    print(f"[objetivo] {objetivo}\n  planificando (usa Claude)...")
    contexto = reasoning.recall_trajectories(
        objetivo
    )  # ReasoningBank: trayectorias exitosas previas
    if contexto:
        print("  (recuperé trayectorias previas parecidas para guiar el plan)")
    p = planner.plan(objetivo, context=contexto)
    if repo:
        p.repo = repo
    destino = planner.repo_plan_path(repo) if repo else config.PLAN
    planner.save_plan(p, destino)
    planner.set_active_plan(destino)
    if not p.pasos:
        print("[planner] no pude generar un roadmap. Reformula el objetivo o reintenta.")
        return
    print(f"\n  Done del objetivo: {p.criterio_global}")
    print(f"  Roadmap ({len(p.pasos)} pasos):")
    for s in p.pasos:
        dep = f"  (tras {', '.join(map(str, s.depende_de))})" if s.depende_de else ""
        quien = f" @{s.persona}" if s.persona else ""
        print(f"   {s.id}. [{s.tipo}]{quien} {s.accion}{dep}")
        print(f"       done: {s.done}")
    print(f"\n  Plan guardado en {destino.name} (activo). Ejecútalo con 'escapement ejecutar'.")


def _run_ejecutar(argv: list[str]) -> None:
    """`escapement ejecutar [repo] [--si]` -> ejecuta el plan del repo (Fase B) hasta el checkpoint.

    Con repo: corre y persiste el plan DEDICADO de ese repo (data/plan_<slug>.json) y lo deja activo.
    Sin repo: sigue el plan activo (el último ejecutado). Cada repo se reanuda en su propio archivo,
    así que puedes alternar entre repos sin empezar de cero.

    Antes de la PRIMERA corrida muestra el roadmap y pide confirmación (ver :func:`_aprobar_plan`);
    ``--si`` la salta. En terminal interactiva, un checkpoint a mitad de camino pregunta INLINE y
    la corrida continúa en el mismo proceso (ver :func:`_checkpoint_inline`); sin TTY termina con
    el mensaje de reanudación de siempre.

    Al agotar el roadmap, el runner evalúa el resultado contra el criterio global del plan (E2,
    deuda #7): 'done' sella el cierre; 'incompleto' replanifica solo (los gaps se vuelven pasos
    nuevos y la corrida sigue) hasta ``AGENT_PLAN_REPLAN_MAX`` ciclos — al tope frena y aquí se
    imprimen los gaps como checkpoint humano. ``AGENT_PLAN_EVAL=0`` apaga la evaluación (cierre por
    conteo, como antes); ``AGENT_PLAN_REPLAN_MAX=0`` apaga solo la replanificación.
    """
    from agent import planner, reasoning, runner

    pos, opts = cliparse.split_args(
        argv, flags_bool=frozenset({"si", "yes"}), short_map={"s": "si"}
    )
    repo_arg = cliparse.opt_str(opts, "repo") or (pos[0] if pos else "")
    repo = _resolve_repo(repo_arg) if repo_arg else ""
    if repo and not _repo_utilizable(repo, f"'{repo_arg}'"):
        return  # antes de tocar el plan: un typo no debe cargarlo ni pisarle el repo (deuda #16)
    plan_path = planner.plan_slot(repo)  # con repo: su archivo dedicado + lo deja activo
    plan = planner.load_plan(plan_path)
    if plan is None:
        donde = f" para {repo_arg}" if repo_arg else " activo"
        print(f"[ejecutar] no hay plan{donde}. Genera con 'escapement objetivo [repo] \"<meta>\"'.")
        return
    if not repo:
        repo = plan.repo or str(Path.cwd())
        if not _repo_utilizable(repo, "el plan"):
            return  # el repo se movió o se borró desde que se planificó
    if plan.repo != repo:
        plan.repo = repo  # recuerda el repo para las próximas reanudaciones
        planner.save_plan(plan, plan_path)
    if not _aprobar_plan(plan, plan_path, saltar=cliparse.has_flag(opts, "si", "yes")):
        return
    print(f"[ejecutar] {plan.objetivo}\n  repo: {repo}  ·  plan: {plan_path.name}")
    while True:
        runner.run_plan(
            plan,
            repo,
            plan_path=plan_path,
            on_step=lambda s: print(
                f"  → paso {s.id} [{s.tipo}]{f' @{s.persona}' if s.persona else ''} {s.accion}"
            ),
            on_complete=reasoning.capture_trajectory,  # ReasoningBank: trayectoria si fue éxito
        )
        for s in plan.pasos:
            quien = f" @{s.persona}" if s.persona else ""
            print(f"   {_ICONOS.get(s.estado, '?')} {s.id}. [{s.tipo}]{quien} {s.accion}")
            if s.nota:
                print(f"       {s.nota}")
        bloq = next((s for s in plan.pasos if s.estado in ("bloqueado", "fallido")), None)
        if bloq is None:
            break
        if _checkpoint_inline(bloq, plan, plan_path):
            continue  # el checkpoint se resolvió en el momento: la corrida sigue en este proceso
        print(
            f"\n[ejecutar] checkpoint en el paso {bloq.id}. Resuélvelo y marca "
            f"'escapement paso {bloq.id} hecho', luego 'escapement ejecutar' para seguir."
        )
        _reporte_traza(plan, repo, completo=False)  # traza del avance parcial (aún sin rescate)
        return
    if all(s.estado == "hecho" for s in plan.pasos):
        # E2 (deuda #7): el veredicto de la evaluación global manda sobre el conteo. 'incompleto'
        # muestra los gaps (ya persistidos en el plan); el gate por config evita mostrar un
        # veredicto viejo pegado en el JSON cuando la evaluación está apagada.
        if config.PLAN_EVAL and plan.eval_veredicto == "incompleto":
            print("\n[ejecutar] pasos agotados, pero el criterio global AÚN no se cumple:")
            print(f"  criterio: {plan.criterio_global}")
            for gap in plan.eval_gaps:
                print(f"   - {gap}")
            if config.PLAN_REPLAN_MAX > 0 and plan.replan_ciclos >= config.PLAN_REPLAN_MAX:
                print(
                    f"  Replanificación automática agotada ({plan.replan_ciclos} ciclo(s), tope "
                    f"{config.PLAN_REPLAN_MAX}): checkpoint humano. Cierra los gaps a mano o "
                    "replantea con 'escapement objetivo ...'."
                )
            else:
                print(
                    "  Los gaps quedaron guardados en el plan. Ciérralos a mano o replantea con "
                    "'escapement objetivo ...'."
                )
        else:
            sello = " · criterio global verificado" if plan.eval_veredicto == "done" else ""
            print(f"\n[ejecutar] roadmap completo ✓{sello}")
        _reporte_traza(plan, repo, completo=True)  # traza + oferta de rescate a una rama


def _run_paso(argv: list[str]) -> None:
    """`escapement paso <id> <estado>` -> marca un paso del plan ACTIVO (para pasar checkpoints)."""
    from agent import planner

    pos, _ = cliparse.split_args(argv, stop_at_positional=True)
    pid = cliparse.arg_int(pos, 0, default=None)  # id inválido -> uso (antes: ValueError)
    if pid is None or len(pos) < 2:
        print('uso: escapement paso <id> <hecho|pendiente|fallido|bloqueado> ["respuesta o nota"]')
        return
    plan_path = planner.active_plan_path()
    plan = planner.load_plan(plan_path)
    if plan is None:
        print("[paso] no hay plan activo.")
        return
    estado = pos[1].lower()
    nota = " ".join(pos[2:]) if len(pos) > 2 else None
    # la nota es la respuesta al checkpoint -> contexto para los pasos siguientes
    if planner.set_step_state(plan, pid, estado, nota) is None:
        print(f"[paso] no existe el paso {pid}")
        return
    planner.save_plan(plan, plan_path)
    print(f"[paso] {pid} -> {estado}" + (f" · nota: {nota}" if nota else ""))


def _run_plan_cmd(argv: list[str]) -> None:
    """`escapement plan [lista|ver [repo]|activar <repo>|quitar|mover|editar]` -> planes por repo.

    Cada repo tiene su propio plan (data/plan_<slug>.json), que ``escapement ejecutar <repo>`` corre y
    reanuda en su archivo, sin pisar a los demás. ``lista`` muestra todos + cuál está activo; ``ver
    [repo]`` imprime el roadmap completo (sin repo: el activo) para leerlo antes de gastar
    presupuesto; ``activar <repo>`` apunta el activo a ese repo (para ``ejecutar``/``paso`` sin
    repetir la ruta). No copia nada: el plan vive siempre en el archivo de su repo.

    ``quitar <id>`` / ``mover <id> <pos>`` / ``editar <id> "<accion>"`` mutan la ESTRUCTURA del
    plan ACTIVO (mismo blanco que ``escapement paso``) validando que las dependencias sigan
    consistentes; ver :func:`planner.remove_step` / :func:`planner.move_step` /
    :func:`planner.edit_step`.
    """
    from agent import planner

    sub = argv[0].lower() if argv else "lista"
    if sub == "lista":
        activa = planner.active_plan_path()
        archivos = planner.plan_files()
        if not archivos:
            print("[plan] (vacío). Genera con 'escapement objetivo [repo] \"<meta>\"'.")
            return
        print("[plan] planes por repo (· = activo):")
        for f in archivos:
            p = planner.load_plan(f)
            if not p:
                continue
            hechos = sum(1 for s in p.pasos if s.estado == "hecho")
            marca = "·" if f.resolve() == activa.resolve() else " "
            print(
                f"  {marca} {f.name:34} [{len(p.pasos):2} pasos, {hechos:2} hechos]  {p.repo or '(scratch)'}"
            )
        return
    if sub == "ver":
        activa = planner.active_plan_path()
        cand = _plan_por_arg(argv[1]) if len(argv) > 1 else activa
        p = planner.load_plan(cand)
        if p is None:
            donde = f" para '{argv[1]}' ({cand.name})" if len(argv) > 1 else " activo"
            print(f"[plan] no hay plan{donde}. Genera con 'escapement objetivo [repo] \"<meta>\"'.")
            return
        _render_plan(p, cand, activo=cand.resolve() == activa.resolve())
        return
    if sub == "activar":
        if len(argv) < 2:
            print("uso: escapement plan activar <repo|ruta|slug>")
            return
        arg = argv[1]
        cand = _plan_por_arg(arg)
        if not cand.exists():
            print(f"[plan] no hay plan para '{arg}' ({cand.name}).")
            return
        planner.set_active_plan(cand)
        p = planner.load_plan(cand)
        print(
            f"[plan] activo -> {cand.name} ({len(p.pasos) if p else 0} pasos). Corre 'escapement ejecutar'."
        )
        return
    if sub in ("quitar", "mover", "editar"):
        plan_path = planner.active_plan_path()
        p = planner.load_plan(plan_path)
        if p is None:
            print("[plan] no hay plan activo que editar. Actívalo con 'escapement plan activar <repo>'.")
            return
        pid = cliparse.arg_int(argv, 1, default=None)
        if sub == "quitar":
            if pid is None:
                print("uso: escapement plan quitar <id>")
                return
            paso = planner.remove_step(p, pid)
            if paso is None:
                print(f"[plan] no existe el paso {pid}")
                return
            planner.save_plan(p, plan_path)
            print(
                f"[plan] quitado el paso {pid} ({_una_linea(paso.accion)}); dependencias "
                f"reconectadas. Quedan {len(p.pasos)} pasos — revisa con 'escapement plan ver'."
            )
            return
        if sub == "mover":
            destino = cliparse.arg_int(argv, 2, default=None)
            if pid is None or destino is None:
                print("uso: escapement plan mover <id> <posición>")
                return
            try:
                paso = planner.move_step(p, pid, destino)
            except ValueError as e:
                print(f"[plan] no se puede mover: {e}. El plan queda como estaba.")
                return
            if paso is None:
                print(f"[plan] no existe el paso {pid}")
                return
            planner.save_plan(p, plan_path)
            pos_final = next(i for i, s in enumerate(p.pasos, 1) if s.id == pid)
            print(f"[plan] paso {pid} movido a la posición {pos_final} de {len(p.pasos)}.")
            return
        accion = " ".join(argv[2:]).strip()
        if pid is None or not accion:
            print('uso: escapement plan editar <id> "<nueva acción>"')
            return
        paso = planner.edit_step(p, pid, accion)
        if paso is None:
            print(f"[plan] no existe el paso {pid}")
            return
        planner.save_plan(p, plan_path)
        print(f"[plan] paso {pid} -> {_una_linea(accion)}")
        return
    print(
        "uso: escapement plan [lista | ver [repo] | activar <repo> | quitar <id> | "
        'mover <id> <pos> | editar <id> "<accion>"]'
    )


def _run_agentes(argv: list[str]) -> None:
    """`escapement agentes [dest]` -> exporta las personas como subagentes de Claude Code."""
    from agent import personas

    pos, opts = cliparse.split_args(argv)
    dest = cliparse.opt_str(opts, "dest") or (pos[0] if pos else "")
    escritos = personas.export_agents(Path(dest) if dest else None)
    destino = escritos[0].parent if escritos else "?"
    print(f"[agentes] {len(escritos)} subagentes escritos en {destino}:")
    for p in escritos:
        print(f"  · {p.stem}")
    print("  Úsalos en Claude Code: delega solo por la descripción, o invócalo por su nombre.")


_OBJETIVO_COMPILAR = (
    "Compila y mejora la memoria de conocimiento del repo {nombre}: inventaria con list_memory, "
    "explora el repo, crea o complementa notas una-idea-por-archivo de arquitectura, flujos y "
    "gotchas, y elimina duplicados dejando MEMORY.md al día"
)
_OBJETIVO_REPASO = (
    "Repasa y actualiza la memoria de conocimiento del repo {nombre}: inventaria las notas "
    "existentes con list_memory y read_memory, verifica que siguen vigentes contra el código y la "
    "estructura ACTUALES del repo, actualiza las desactualizadas, elimina las obsoletas, y deja "
    "MEMORY.md consistente 1:1"
)


def _memoria_hecha(nombre: str, ruta: str) -> bool:
    """True si el repo ya tiene notas de memoria compiladas en su vault."""
    from agent.tools import memory

    return len(memory.list_notes(config.vault_slug(ruta))) > 0


def _repos_por_estado(argv: list[str], *, hechos: bool) -> list[str]:
    """Repos a procesar: los de ``argv``, o (si vacío) los que estén hechos/nuevos según ``hechos``."""
    if argv:
        return argv
    return [n for n, ruta in config.REPOS.items() if _memoria_hecha(n, ruta) == hechos]


def _procesar_memoria(nombres: list[str], objetivo_tpl: str, etiqueta: str) -> None:
    """Genera y ejecuta el plan de memoria de cada repo (autónomo). Para en el 1er checkpoint/fallo."""
    from agent import planner, runner

    if not nombres:
        print(f"[{etiqueta}] no hay repos que procesar.")
        return
    for nombre in nombres:
        ruta = config.REPOS.get(nombre, nombre)
        print(f"\n=== {etiqueta}: {nombre} ({ruta}) ===")
        plan = planner.plan(objetivo_tpl.format(nombre=nombre))
        plan.repo = ruta
        plan_path = planner.repo_plan_path(ruta)  # cada repo en su propio archivo, reanudable
        planner.save_plan(plan, plan_path)
        planner.set_active_plan(plan_path)
        runner.run_plan(
            plan,
            ruta,
            plan_path=plan_path,
            on_step=lambda s: print(
                f"  → {s.id} [{s.tipo}]{f' @{s.persona}' if s.persona else ''} {s.accion}"
            ),
        )
        bloq = next((s for s in plan.pasos if s.estado in ("bloqueado", "fallido")), None)
        if bloq:
            print(
                f"  ⏸ {nombre}: checkpoint en el paso {bloq.id}. Resuélvelo "
                f'(escapement paso {bloq.id} hecho "...") y re-lanza. Me detengo aquí.'
            )
            _reporte_traza(plan, ruta, completo=False, mostrar_vacio=False)
            return
        print(f"  ✓ {nombre}: {etiqueta} completo.")
        _reporte_traza(plan, ruta, completo=True, mostrar_vacio=False)
    print(f"\n[{etiqueta}] todos los repos procesados ✓")


def _run_memoria(argv: list[str]) -> None:
    """`escapement memoria [repos...]` -> compila la memoria de los repos NUEVOS (o los indicados)."""
    _procesar_memoria(_repos_por_estado(argv, hechos=False), _OBJETIVO_COMPILAR, "memoria")


def _run_repaso(argv: list[str]) -> None:
    """`escapement repaso [repos...]` -> repasa/actualiza la memoria de los repos HECHOS (o los indicados)."""
    _procesar_memoria(_repos_por_estado(argv, hechos=True), _OBJETIVO_REPASO, "repaso")


@dataclass(frozen=True)
class Command:
    """Fila de la tabla de subcomandos.

    Args:
        handler: la función ``_run_*`` que ejecuta el comando (recibe los tokens tras el nombre).
        resumen: línea única para ``escapement help`` (listado global).
        uso: bloque multilínea para ``escapement help <cmd>`` / ``escapement <cmd> --help``. Si es ``""``
            (default), el help por comando cae al ``resumen`` — no todos los comandos necesitan detalle.
    """

    handler: Callable[[list[str]], None]
    resumen: str
    uso: str = ""


# Tabla de subcomandos: nombre canónico -> Command(handler, resumen, uso). Añadir un comando es una
# fila más; el dispatch, el help (global y por comando) y la sugerencia de typos salen de aquí.
_COMMANDS: dict[str, Command] = {
    "optimiza": Command(
        _run_optimiza,
        "optimiza <ruta> [directiva] - un archivo/dir a un PR",
        "uso: escapement optimiza <ruta> [directiva]   ·   flags: --directiva <texto>\n"
        "  Un archivo o dir -> rama aislada + Claude Code + tests + PR. Sin directiva, pide type "
        "hints, docstrings, legibilidad y eficiencia preservando el comportamiento.",
    ),
    "backlog": Command(
        _run_backlog,
        "backlog <repo> [N] - lista candidatos de mejora (read-only)",
        "uso: escapement backlog <repo> [N]   ·   flags: --repo <repo|ruta>  --limit/-n <N>\n"
        "  Lista los N candidatos de mejora del repo (read-only, más deuda primero).",
    ),
    "vigilar": Command(
        _run_vigilar,
        "vigilar <repo> [N] - escanea deuda y encola tareas",
        "uso: escapement vigilar <repo> [N]   ·   flags: --repo <repo|ruta>  --limit/-n <N>\n"
        "  Escanea deuda y ENCOLA hasta N candidatos (no ejecuta). Procésalos con 'trabajar'.",
    ),
    "trabajar": Command(
        _run_trabajar,
        "trabajar [N] - procesa la cola (worktree+verify+PR), reanudable",
        "uso: escapement trabajar [N]   ·   flags: --limit/-n <N>  (N=0 o ausente: toda la cola)\n"
        "  Procesa la cola (worktree+verify+PR), respeta la cuota, reanudable (Ctrl-C seguro).",
    ),
    "revisar": Command(
        _run_revisar,
        "revisar - estado de los PRs, aprende del accept/reject",
    ),
    "estado": Command(
        _run_estado,
        "estado - dashboard: plan activo, cola, ledger y tasa de aceptación",
    ),
    "historial": Command(
        _run_historial,
        "historial [N] - últimos N turnos de conversación (local y Claude)",
        "uso: escapement historial [N]   ·   flags: --limit/-n <N>\n"
        "  Últimos N turnos de conversación (local y Claude).",
    ),
    "eventos": Command(
        _run_eventos,
        "eventos [N] - últimos N eventos del journal del bus (data/events.jsonl)",
        "uso: escapement eventos [N]   ·   flags: --limit/-n <N>  --topic/-t <prefijo>\n"
        "  Últimos N eventos del journal del bus (default 20). '--topic' filtra por prefijo, igual\n"
        "  que bus.subscribe: '--topic plan.' muestra plan.step y plan.done. Con '--limit 0', todos.\n"
        "  Ojo: 'turn', 'voice.capture' y 'plan.step_start' son señal in-process (journal=False) y\n"
        "  no aparecen aquí; los turnos viven en 'escapement historial'.",
    ),
    "objetivo": Command(
        _run_objetivo,
        'objetivo "<meta>" - el planner propone un roadmap con criterios de done',
        'uso: escapement objetivo [repo] "<meta>"   ·   flag: --repo <repo|ruta>\n'
        "  El planner propone un roadmap con criterios de done. Con repo, el plan queda dedicado y "
        "activo. Para una meta que empieza con '-', antepón '--':  objetivo -- --algo.",
    ),
    "ejecutar": Command(
        _run_ejecutar,
        "ejecutar [repo] - ejecuta el plan activo hasta el próximo checkpoint",
        "uso: escapement ejecutar [repo] [--si]   ·   flags: --repo <repo|ruta>, --si/-s\n"
        "  Ejecuta el plan del repo (o el activo) hasta el próximo checkpoint. Reanudable por repo.\n"
        "  Antes de la primera corrida muestra el roadmap y pide confirmación; '--si' la salta "
        "(apagar siempre: AGENT_PLAN_APPROVAL=0).\n"
        "  En TTY, un checkpoint a mitad de camino pregunta inline ([h]echo/[r]eintentar) y la "
        "corrida sigue sin salir; sin TTY termina y se reanuda con 'escapement paso'.\n"
        "  Al agotar el roadmap evalúa el resultado contra el criterio global: 'incompleto' "
        "replanifica solo (gaps -> pasos nuevos, tope AGENT_PLAN_REPLAN_MAX ciclos; 0 lo apaga) "
        "y al tope muestra los gaps como checkpoint humano (AGENT_PLAN_EVAL=0 apaga la evaluación).",
    ),
    "plan": Command(
        _run_plan_cmd,
        "plan [lista|ver|activar|quitar|mover|editar] - planes por repo, editables desde el CLI",
        "uso: escapement plan [lista | ver [repo] | activar <repo> | quitar <id> | mover <id> <pos> | "
        'editar <id> "<accion>"]\n'
        "  Planes por repo (data/plan_<slug>.json), independientes. 'lista' los muestra (· = activo); "
        "'ver [repo]' imprime el roadmap completo (sin repo: el activo); "
        "'activar <repo>' apunta el activo para 'ejecutar'/'paso' sin repetir la ruta.\n"
        "  Edición del plan ACTIVO: 'quitar <id>' saca el paso y reconecta sus dependencias; "
        "'mover <id> <pos>' lo recoloca (rechaza romper el orden topológico); "
        "'editar <id> \"<accion>\"' reemplaza el texto de la acción (tipo/done/estado intactos).",
    ),
    "paso": Command(
        _run_paso,
        'paso <id> <estado> ["respuesta"] - marca un paso / responde un checkpoint',
        'uso: escapement paso <id> <hecho|pendiente|fallido|bloqueado> ["nota"]\n'
        "  Marca un paso del plan activo / responde un checkpoint. La nota es texto libre (el resto "
        "de la línea) y queda como contexto para los pasos siguientes.",
    ),
    "agentes": Command(
        _run_agentes,
        "agentes [dest] - exporta las personas como subagentes de Claude Code",
        "uso: escapement agentes [dest]   ·   flag: --dest <ruta>\n"
        "  Exporta las personas como subagentes de Claude Code en 'dest' (o el destino por defecto).",
    ),
    "memoria": Command(
        _run_memoria,
        "memoria [repos...] - compila la memoria de los repos nuevos (o los indicados)",
        "uso: escapement memoria [repos...]\n"
        "  Compila la memoria de los repos NUEVOS (o los indicados). Autónomo; se detiene en el "
        "primer checkpoint o fallo.",
    ),
    "repaso": Command(
        _run_repaso,
        "repaso [repos...] - repasa/actualiza la memoria de los repos ya hechos",
        "uso: escapement repaso [repos...]\n"
        "  Repasa/actualiza la memoria de los repos ya HECHOS (o los indicados) contra el código actual.",
    ),
    "autoevoluciona": Command(
        _run_autoevoluciona,
        "autoevoluciona <repo> [N] - vigilar + trabajar",
        "uso: escapement autoevoluciona <repo> [N]   ·   flags: --repo <repo|ruta>  --limit/-n <N>\n"
        "  vigilar + trabajar: escanea deuda, encola hasta N candidatos y procesa la cola.",
    ),
}
# Alias -> comando canónico (formas cortas y en inglés; no rompen los nombres actuales).
_ALIASES: dict[str, str] = {
    "opt": "optimiza",
    "deuda": "backlog",
    "cola": "trabajar",
    "run": "trabajar",
    "watch": "vigilar",
    "review": "revisar",
    "status": "estado",
    "historia": "historial",
    "log": "historial",
    "events": "eventos",
    "meta": "objetivo",
    "ejecuta": "ejecutar",
    "auto": "autoevoluciona",
}


def _print_help() -> None:
    """Imprime los subcomandos disponibles (descubribilidad)."""
    print(f"{config.AGENT_NAME} — orquestador de ingeniería.\n\nComandos:")
    for c in _COMMANDS.values():
        print(f"  {c.resumen}")
    print("  --voz - modo voz (hotkey F2, toggle por default)")
    print("  help [<cmd>] - esta ayuda, o el detalle (flags) de un comando")
    print("\nSin comando: REPL de texto.")
    print("Alias: " + ", ".join(f"{a}={c}" for a, c in _ALIASES.items()))


def _print_cmd_help(name: str) -> None:
    """Imprime la ayuda detallada de UN subcomando (`escapement help <cmd>` / `escapement <cmd> --help`).

    Resuelve alias y muestra el bloque ``uso`` del :class:`Command` (flags incluidos); si el comando
    no define ``uso``, cae a su ``resumen``. Un nombre desconocido no revienta: lo dice y remite al
    help global.
    """
    cmd = _ALIASES.get(name, name)
    entry = _COMMANDS.get(cmd)
    if entry is None:
        print(f"{config.AGENT_NAME}: no conozco el comando '{name}'.  Usa 'escapement help'.")
        return
    print(entry.uso or entry.resumen)


def _dispatch(args: list[str]) -> bool:
    """Ejecuta el subcomando si lo hay. Devuelve True si manejó la invocación (no abrir REPL)."""
    if not args:
        return False
    raw = args[0].lower()
    if raw in ("help", "ayuda", "-h", "--help"):
        # `escapement help` -> ayuda global; `escapement help <cmd>` -> detalle de ese comando.
        if len(args) >= 2:
            _print_cmd_help(args[1].lower())
        else:
            _print_help()
        return True
    if raw.startswith("-"):
        return False  # es un flag (p.ej. --voz); lo maneja main
    cmd = _ALIASES.get(raw, raw)
    if cmd in _COMMANDS:
        # `escapement <cmd> --help|-h` (en primera posición): ayuda del comando, no lo ejecuta. Solo
        # esa posición para no robarle un `-h` a la frase libre de metas/notas (que va tras el 1.er
        # posicional y, con stop_at_positional, queda literal de todos modos).
        if len(args) >= 2 and args[1] in ("-h", "--help"):
            _print_cmd_help(cmd)
        else:
            _COMMANDS[cmd].handler(args[1:])
        return True
    import difflib

    cerca = difflib.get_close_matches(raw, list(_COMMANDS) + list(_ALIASES), n=1)
    hint = f" ¿Quisiste decir '{cerca[0]}'?" if cerca else ""
    print(f"{config.AGENT_NAME}: comando desconocido '{raw}'.{hint}  Usa 'escapement help'.")
    return True


def _voice_deps_ok() -> bool:
    """True si el grupo 'voice' está instalado; si falta, imprime cómo instalarlo (mensaje claro
    en vez del ImportError críptico al arrancar la captura de audio)."""
    import importlib.util

    faltan = [
        m
        for m in ("soundfile", "sounddevice", "faster_whisper", "keyboard")
        if importlib.util.find_spec(m) is None
    ]
    if faltan:
        print(f"Modo voz: faltan dependencias del grupo 'voice' ({', '.join(faltan)}).")
        print("Instálalas con:  uv sync --group voice   (o  uv sync --all-groups)")
    return not faltan


def main() -> None:
    """Entry point (scripts `agent`/`escapement`). Sin args: REPL. `--voz`: voz. `help`: comandos."""
    # La consola de Windows es cp1252 por defecto; las respuestas llevan acentos.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    args = sys.argv[1:]
    if _dispatch(args):
        return

    voice_mode = any(arg in ("--voz", "--voice") for arg in args)
    if voice_mode:
        if not _voice_deps_ok():
            return
        from agent.singleton import acquire_single_instance

        if not acquire_single_instance():
            print(f"{config.AGENT_NAME} ya está en ejecución; no abro una segunda instancia.")
            return
    try:
        if voice_mode:
            from agent.voice.daemon import run_daemon
            from agent.voice.tray import start_tray

            start_tray()  # ícono de bandeja (hilo aparte); "Salir" cierra el proceso
            asyncio.run(run_daemon(build_options))  # residente: idle⇄active, hilo persistente
        else:
            asyncio.run(run_repl(build_options()))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
