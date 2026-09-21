"""Herramientas del orquestador expuestas al loop conversacional (chat/voz).

Permiten dirigir el orquestador por lenguaje natural ("optimiza tal archivo del scraper",
"planea migrar el reporter y arranca") en vez del CLI. Wrappers delgados sobre las funciones core
(optimize, run_queue, review_prs, scan_repo, planner, runner, state, ledger). Dos familias:

- **Tarea suelta** (`optimizar`/`vigilar`/`trabajar`/`revisar_prs`): un archivo -> un PR.
- **Plan** (`objetivo`/`plan`/`ejecutar`/`paso`): un objetivo grande -> roadmap con criterios de
  done -> ejecución reanudable en un worktree aislado, con checkpoints que se resuelven hablando.

El gate en capas decide el permiso: `estado`/`deuda`/`plan` son read-only (Anillo 0,
auto-aprobadas); el resto son acciones y caen a confirmación (hablada en modo voz) bajo la
categoría `orq`, así un solo "sí" cubre la tarea. Las acciones largas corren en un hilo para no
bloquear el event loop del agente; `ejecutar` además obedece la señal cooperativa de
:data:`agent.cancel.RUN`, así el F2 que corta el turno por voz corta también la corrida (entre
pasos: un dispatch ya lanzado no se puede matar).
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from claude_agent_sdk import create_sdk_mcp_server, tool

from agent import cancel, config, ledger

if TYPE_CHECKING:  # solo para tipar: el módulo importa planner/runner en caliente (import ligero)
    from agent.planner import Plan, Step

_ICONOS = {"hecho": "✓", "fallido": "✗", "bloqueado": "⏸", "pendiente": "·"}
# Los planes reales del planner son verbosos: acciones y criterios de done de 300-800 chars, y
# hasta 40 pasos. Sin recortar, un solo 'plan' pesaría decenas de miles de chars en el turno (y
# sería impronunciable por voz). Estos topes son de PRESENTACIÓN: el archivo del plan queda intacto,
# y el texto completo de un paso siempre está en `escapement plan` / el JSON.
_NOTA_MAX = 200  # notas del runner (llegan a 1500 chars)
_ACCION_MAX = 180  # acción de un paso
_DONE_MAX = 120  # criterio de done de un paso
_CRITERIO_MAX = 300  # criterio de done GLOBAL del objetivo
_OBJETIVO_MAX = 220  # el objetivo del plan (el título)
# Una corrida del runner a la vez EN ESTE proceso: el modelo podría llamar 'ejecutar' dos veces
# seguidas y dos runners sobre el mismo plan se pisarían al persistir. No cubre otro proceso
# (p.ej. un `escapement ejecutar` en paralelo desde una terminal): eso sigue siendo cosa del usuario.
_RUN_LOCK = asyncio.Lock()


def _text(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}]}


def _corto(texto: str, limite: int = _NOTA_MAX) -> str:
    """Una línea recortada de ``texto`` (las notas multilínea del runner no caben en una respuesta)."""
    plano = " ".join((texto or "").split())
    return plano if len(plano) <= limite else plano[:limite] + "…"


def _linea_paso(step: Step, *, con_done: bool = False) -> str:
    """Un paso en UNA línea: icono de estado, id, tipo, persona, acción (recortada) y dependencias."""
    dep = f" (tras {', '.join(map(str, step.depende_de))})" if step.depende_de else ""
    quien = f" @{step.persona}" if step.persona else ""
    done = f"  | done: {_corto(step.done, _DONE_MAX)}" if con_done else ""
    icono = _ICONOS.get(step.estado, "?")
    return f" {icono} {step.id}. [{step.tipo}]{quien} {_corto(step.accion, _ACCION_MAX)}{dep}{done}"


def _progreso(p: Plan) -> str:
    """``hechos/total`` del roadmap (el criterio de done GLOBAL sigue siendo humano)."""
    return f"{sum(1 for s in p.pasos if s.estado == 'hecho')}/{len(p.pasos)}"


def _plan_path(arg: str) -> Path:
    """Archivo de plan de ``arg`` (nombre/ruta/slug), con el fallback a ``plan_<slug>.json`` del CLI."""
    from agent import planner
    from agent.orchestrator import resolve_repo

    cand = planner.repo_plan_path(resolve_repo(arg))
    if not cand.exists():
        alt = config.DATA_DIR / f"plan_{arg}.json"  # por si pasaron el slug directo
        return alt if alt.exists() else cand
    return cand


def _listado_planes(activa: Path) -> list[str]:
    """Una línea por plan existente (· = activo), para ubicarse entre repos."""
    from agent import planner

    lineas: list[str] = []
    for f in planner.plan_files():
        p = planner.load_plan(f)
        if not p:
            continue
        marca = "·" if f.resolve() == activa.resolve() else " "
        lineas.append(f" {marca} {f.name} [{_progreso(p)} pasos hechos] {p.repo or '(scratch)'}")
    return lineas


def _reporte_corrida(p: Plan, plan_path: Path, repo: str) -> str:
    """Resultado de una corrida del runner: avance, checkpoint pendiente o cierre con traza.

    Es el gemelo conversacional del reporte que imprime ``escapement ejecutar``, con una diferencia
    deliberada: NO ofrece el rescate a una rama, porque esa oferta se resuelve con un ``input()``
    interactivo y aquí colgaría el turno (chat) o el daemon (voz). Se reporta el worktree y la rama
    para hacerlo desde la terminal.
    """
    from agent import runner

    lineas = [
        f"Plan «{_corto(p.objetivo, _OBJETIVO_MAX)}» ({plan_path.name}, repo {repo}): "
        f"{_progreso(p)} pasos hechos.",
        *[_linea_paso(s) for s in p.pasos],
    ]
    if cancel.RUN.pedido():
        motivo = cancel.RUN.motivo()
        lineas.append(
            "Corrida CANCELADA a tu pedido"
            + (f" ({motivo})" if motivo else "")
            + "; el paso que estaba despachado terminó y no arranqué el siguiente. "
            "Retómala con 'ejecutar' cuando quieras."
        )
    bloq = next((s for s in p.pasos if s.estado in ("bloqueado", "fallido")), None)
    if bloq:
        lineas += [
            f"Checkpoint en el paso {bloq.id} [{bloq.tipo}]: {bloq.accion}",
            f"  motivo: {_corto(bloq.nota)}" if bloq.nota else "  (sin nota)",
            f"  resuélvelo con la tool 'paso' (id {bloq.id}, estado hecho, nota = tu respuesta) "
            "y vuelve a 'ejecutar'.",
        ]
        return "\n".join(lineas)
    if p.pasos and all(s.estado == "hecho" for s in p.pasos):
        lineas.append("Roadmap completo ✓")
        if rutas := runner.archivos_del_plan(p):
            lineas += [
                f"  worktree: {p.workdir}",
                f"  rama:     {p.rama}",
                "  archivos tocados:",
                *[f"    {ln}" for ln in runner.arbol_archivos(rutas).splitlines()],
                f"  para traspasarlo a una rama nueva basada en '{runner.default_branch(repo) or '(default)'}', "
                "corre 'escapement ejecutar' en una terminal (el rescate pide confirmación interactiva).",
            ]
        else:
            lineas.append("  (el plan no dejó archivos modificados en el worktree)")
    return "\n".join(lineas)


def _linea_plan_activo() -> str:
    """El plan ACTIVO en UNA línea, para el dashboard de :func:`estado`.

    La tool ``plan`` da el roadmap entero; aquí sólo entra lo que responde "¿en qué voy?" sin
    inflar la respuesta: objetivo, avance y si hay un checkpoint esperando.
    """
    from agent import planner

    p = planner.load_plan(planner.active_plan_path())
    if p is None or not p.pasos:
        return "Plan: ninguno activo (genera uno con la tool 'objetivo')."
    linea = f"Plan «{_corto(p.objetivo, _OBJETIVO_MAX)}»: {_progreso(p)} pasos hechos"
    if bloq := next((s for s in p.pasos if s.estado in ("bloqueado", "fallido")), None):
        return f"{linea}; checkpoint pendiente en el paso {bloq.id} ({bloq.estado})."
    if all(s.estado == "hecho" for s in p.pasos):
        return f"{linea}; roadmap completo ✓"
    return f"{linea}."


@tool(
    "estado",
    "Estado del orquestador Escapement: plan activo (objetivo, avance y checkpoint), tareas en cola, "
    "resumen del ledger y tasa de aceptación de PRs. Solo lectura.",
    {},
)
async def estado(args: dict[str, Any]) -> dict[str, Any]:
    from agent import state

    ps = state.pending()
    opt = [r for r in ledger.read() if r.get("action") == "optimize"]
    reviews = ledger.latest_pr_reviews()
    acc = sum(1 for v in reviews.values() if v == "accepted")
    rej = sum(1 for v in reviews.values() if v == "rejected")
    pen = sum(1 for v in reviews.values() if v == "pending")
    lineas = [
        _linea_plan_activo(),
        f"Cola: {len(ps)} pendientes"
        + (f" ({', '.join(t['target'] for t in ps[:5])})" if ps else ""),
        f"Ledger: {len(opt)} optimizaciones, {sum(1 for r in opt if r.get('verified'))} seguras.",
        f"PRs: {acc} aceptados, {rej} rechazados, {pen} pendientes.",
    ]
    if thr := state.throttled_until():
        lineas.append(f"Cuota agotada desde {thr} (cola pausada).")
    return _text("\n".join(lineas))


@tool(
    "deuda",
    "Lista los archivos con más deuda técnica de un repo (candidatos a optimizar). Solo lectura. "
    "repo: ruta del repo. n: cuántos listar (default 10).",
    {"repo": str, "n": int},
)
async def deuda(args: dict[str, Any]) -> dict[str, Any]:
    from agent.backlog import scan_repo
    from agent.orchestrator import git_root

    root = git_root(Path(args["repo"]).resolve())
    cands = scan_repo(root, limit=int(args.get("n") or 10))
    if not cands:
        return _text(f"Sin candidatos de deuda en {root}.")
    lineas = [f"{i}. {c.path} ({', '.join(c.reasons)})" for i, c in enumerate(cands, 1)]
    return _text(f"Deuda en {root}:\n" + "\n".join(lineas))


@tool(
    "optimizar",
    "Optimiza un archivo (o directorio) de un repo: despacha el refactor a Claude Code en una "
    "rama aislada, verifica que no rompe nada, mide la mejora y abre un PR para tu revisión. "
    "NUNCA mergea. repo: ruta del repo. target: archivo relativo (o '.' para todo). "
    "directiva: qué hacer (opcional).",
    {"repo": str, "target": str, "directiva": str},
)
async def optimizar(args: dict[str, Any]) -> dict[str, Any]:
    from agent.orchestrator import git_root, optimize

    root = git_root(Path(args["repo"]).resolve())
    directiva = (args.get("directiva") or "").strip() or (
        "optimiza: legibilidad, type hints, docstrings y eficiencia, preservando el comportamiento"
    )
    res = await asyncio.to_thread(
        optimize, root, directiva, args["target"], python_exe=sys.executable
    )
    veredicto = "SEGURO" if res.verified else "RECHAZADO (sin PR)"
    return _text(f"{args['target']}: {veredicto}. {res.verdict}. PR: {res.pr_url or '(local)'}")


@tool(
    "vigilar",
    "Escanea la deuda de un repo y ENCOLA los peores candidatos para optimizarlos luego con "
    "'trabajar'. No los procesa aún. repo: ruta del repo. n: cuántos encolar (default 5).",
    {"repo": str, "n": int},
)
async def vigilar(args: dict[str, Any]) -> dict[str, Any]:
    from agent import state
    from agent.backlog import scan_repo
    from agent.orchestrator import _current_branch, git_root

    root = git_root(Path(args["repo"]).resolve())
    base = _current_branch(root)
    rechazados = ledger.rejected_targets(str(root))
    tasks = [
        {
            "repo": str(root),
            "target": c.path,
            "base": base,
            "directiva": (
                f"optimiza {c.path}: type hints, docstrings, legibilidad y eficiencia, "
                f"preservando el comportamiento ({', '.join(c.reasons)})"
            ),
        }
        for c in scan_repo(root, limit=int(args.get("n") or 5))
        if c.path not in rechazados
    ]
    added = state.enqueue(tasks)
    return _text(f"{added} tareas encoladas de {root}. Total pendientes: {len(state.pending())}.")


@tool(
    "trabajar",
    "Procesa la cola de tareas encoladas: por cada una despacha el refactor, verifica y abre PR. "
    "Respeta el límite de tu suscripción (pausa si se agota). n: cuántas procesar "
    "(default 1; 0 = todas).",
    {"n": int},
)
async def trabajar(args: dict[str, Any]) -> dict[str, Any]:
    from agent.orchestrator import run_queue

    n = int(args["n"]) if args.get("n") is not None else 1
    r = await asyncio.to_thread(run_queue, n, sys.executable)
    partes = [f"Procesadas {r['procesadas']}, pendientes {r['pendientes']}."]
    if r["throttled"]:
        partes.append("Cuota agotada: cola pausada, reanuda luego.")
    partes += [
        f"- {res['target']}: {'SEGURO' if res['verified'] else 'RECHAZADO'} {res['pr_url'] or ''}"
        for res in r["resultados"]
    ]
    return _text("\n".join(partes))


@tool(
    "revisar_prs",
    "Consulta el estado de los PRs que Escapement abrió (merged/closed/open) y aprende del "
    "resultado: los rechazados no se vuelven a proponer. Solo cambia el ledger. Sin params.",
    {},
)
async def revisar_prs(args: dict[str, Any]) -> dict[str, Any]:
    from agent.orchestrator import review_prs

    nuevos = await asyncio.to_thread(review_prs)
    if not nuevos:
        return _text("Sin cambios: no hay PRs nuevos ni resueltos.")
    acc = sum(1 for r in nuevos if r["result"] == "accepted")
    rej = sum(1 for r in nuevos if r["result"] == "rejected")
    return _text(f"{len(nuevos)} PRs actualizados: {acc} aceptados, {rej} rechazados aprendidos.")


@tool(
    "objetivo",
    "Planea un OBJETIVO grande (no un archivo suelto): genera un roadmap de pasos con criterio de "
    "done y dependencias, lo guarda como el plan del repo y lo deja activo. Solo planea, todavía no "
    "toca código —eso es 'ejecutar'—. meta: qué quieres lograr. repo: ruta o nombre del repo "
    "(opcional; sin repo va al plan scratch).",
    {"meta": str, "repo": str},
)
async def objetivo(args: dict[str, Any]) -> dict[str, Any]:
    from agent import planner, reasoning
    from agent.orchestrator import resolve_repo

    meta = (args.get("meta") or "").strip()
    if not meta:
        return _text("Falta 'meta': dime qué objetivo quieres planear.")
    arg = (args.get("repo") or "").strip()
    repo = resolve_repo(arg) if arg else ""
    # Mismo camino que `escapement objetivo`: ReasoningBank -> planner (MODEL_PLANNER) -> plan activo.
    contexto = await asyncio.to_thread(reasoning.recall_trajectories, meta)
    p = await asyncio.to_thread(planner.plan, meta, context=contexto)
    if repo:
        p.repo = repo
    destino = planner.repo_plan_path(repo) if repo else config.PLAN
    planner.save_plan(p, destino)
    planner.set_active_plan(destino)
    if not p.pasos:
        return _text("No pude generar un roadmap para eso; reformula el objetivo o reintenta.")
    lineas = [
        f"Roadmap de «{_corto(meta, _OBJETIVO_MAX)}»: {len(p.pasos)} pasos -> {destino.name} (activo).",
        f"Done del objetivo: {_corto(p.criterio_global, _CRITERIO_MAX)}",
        *[_linea_paso(s, con_done=True) for s in p.pasos],
        "Arráncalo con la tool 'ejecutar'" + (f" (repo {repo})" if repo else "") + ".",
    ]
    return _text("\n".join(lineas))


@tool(
    "plan",
    "Muestra el plan de trabajo: objetivo, criterio de done global, cada paso con su estado y el "
    "checkpoint pendiente si hay. Solo lectura. repo: de qué repo (opcional; sin repo, el plan "
    "activo más un listado de los demás).",
    {"repo": str},
)
async def plan(args: dict[str, Any]) -> dict[str, Any]:
    from agent import planner

    arg = (args.get("repo") or "").strip()
    activa = planner.active_plan_path()
    path = _plan_path(arg) if arg else activa
    p = planner.load_plan(path)
    if p is None:
        otros = _listado_planes(activa)
        falta = f"No hay plan en {path.name}."
        return _text(
            f"{falta}\nPlanes que sí existen:\n" + "\n".join(otros)
            if otros
            else f"{falta} Genera uno con la tool 'objetivo'."
        )
    lineas = [
        f"Plan «{_corto(p.objetivo, _OBJETIVO_MAX)}» ({path.name}"
        + (", activo" if path.resolve() == activa.resolve() else "")
        + ")",
        f"  repo: {p.repo or '(sin repo)'}  ·  avance: {_progreso(p)} pasos hechos",
        f"  done del objetivo: {_corto(p.criterio_global, _CRITERIO_MAX)}",
        *[_linea_paso(s, con_done=True) for s in p.pasos],
    ]
    for s in p.pasos:
        if s.nota:
            lineas.append(f"   nota {s.id}: {_corto(s.nota)}")
    if bloq := next((s for s in p.pasos if s.estado in ("bloqueado", "fallido")), None):
        lineas.append(f"  Checkpoint pendiente: paso {bloq.id} ({bloq.estado}).")
    elif p.pasos and all(s.estado == "hecho" for s in p.pasos):
        lineas.append("  Roadmap completo ✓")
    if not arg and (otros := [ln for ln in _listado_planes(activa) if not ln.startswith(" ·")]):
        lineas += ["  Otros planes:", *[f"  {ln}" for ln in otros]]
    return _text("\n".join(lineas))


@tool(
    "ejecutar",
    "Ejecuta el plan (el activo, o el del repo indicado) hasta el próximo checkpoint: despacha cada "
    "paso al experto que le toca, en un worktree aislado del repo, y persiste el avance tras cada "
    "uno (reanudable). Puede tardar varios minutos; el usuario puede cancelarla con la hotkey de "
    "voz y se detiene al terminar el paso en vuelo. repo: ruta o nombre del repo (opcional).",
    {"repo": str},
)
async def ejecutar(args: dict[str, Any]) -> dict[str, Any]:
    from agent import planner, reasoning, runner
    from agent.orchestrator import resolve_repo

    if _RUN_LOCK.locked():
        if cancel.RUN.pedido():
            return _text(
                "La corrida en curso ya tiene pedida la cancelación: está terminando el paso que "
                "quedó despachado (no se puede matar a mitad) y para ahí. Vuelve a pedírmelo en un "
                "momento."
            )
        return _text("Ya hay una corrida del plan en curso; espera a que llegue a su checkpoint.")
    async with _RUN_LOCK:
        arg = (args.get("repo") or "").strip()
        repo = resolve_repo(arg) if arg else ""
        plan_path = planner.plan_slot(repo)  # con repo lo deja activo; sin repo, sigue el activo
        p = planner.load_plan(plan_path)
        if p is None:
            donde = f" para {arg}" if arg else " activo"
            return _text(f"No hay plan{donde}. Genera uno con la tool 'objetivo'.")
        repo = repo or p.repo
        if not repo:
            # A diferencia del CLI, aquí NO se cae al cwd: el daemon vive en HOME y montar el
            # worktree de un plan ahí sería un accidente. Mejor pedir el repo.
            return _text(
                f"El plan «{p.objetivo}» no tiene repo asociado; dime sobre cuál correrlo."
            )
        if p.repo != repo:
            p.repo = repo  # recuerda el repo para las próximas reanudaciones
            planner.save_plan(p, plan_path)
        # Arranca limpio: una cancelación vieja (un F2 de otro turno, cuando no había corrida)
        # no puede matar esta antes de empezar.
        cancel.RUN.limpiar()
        await asyncio.to_thread(
            runner.run_plan,
            p,
            repo,
            plan_path=plan_path,
            on_step=lambda s: print(f"  → paso {s.id} [{s.tipo}] {s.accion}"),
            on_complete=reasoning.capture_trajectory,  # ReasoningBank: trayectoria si fue éxito
            should_cancel=cancel.RUN.pedido,  # F2 en voz corta también el trabajo, no solo el turno
        )
        return _text(_reporte_corrida(p, plan_path, repo))


@tool(
    "paso",
    "Marca un paso del plan activo para pasar un checkpoint (típicamente responder un paso "
    "'preguntar' o desbloquear uno fallido). id: número del paso. estado: hecho | pendiente | "
    "fallido | bloqueado. nota: tu respuesta u observación, queda como contexto de los pasos "
    "siguientes (opcional). Después sigue con 'ejecutar'.",
    {"id": int, "estado": str, "nota": str},
)
async def paso(args: dict[str, Any]) -> dict[str, Any]:
    from agent import planner

    try:
        pid = int(args["id"])
    except (KeyError, TypeError, ValueError):
        return _text("Falta 'id': el número del paso a marcar (lo ves con la tool 'plan').")
    estado = str(args.get("estado") or "").strip().lower()
    if estado not in planner.STEP_STATES:
        return _text(f"Estado inválido: usa uno de {', '.join(planner.STEP_STATES)}.")
    plan_path = planner.active_plan_path()
    p = planner.load_plan(plan_path)
    if p is None:
        return _text("No hay plan activo. Genera uno con la tool 'objetivo'.")
    nota = args.get("nota")
    step = planner.set_step_state(p, pid, estado, None if nota is None else str(nota))
    if step is None:
        return _text(f"El plan «{p.objetivo}» no tiene un paso {pid}.")
    planner.save_plan(p, plan_path)
    listos = sum(1 for s in p.pasos if s.estado == "pendiente")
    return _text(
        f"Paso {pid} -> {estado}" + (f" · nota: {_corto(step.nota)}" if step.nota else "") + ". "
        f"Quedan {listos} pasos pendientes; sigue con 'ejecutar'."
    )


orchestrator_server = create_sdk_mcp_server(
    "orq",
    version="0.2.0",  # + planner/runner: objetivo, plan, ejecutar, paso
    tools=[
        estado,
        deuda,
        optimizar,
        vigilar,
        trabajar,
        revisar_prs,
        objetivo,
        plan,
        ejecutar,
        paso,
    ],
)
