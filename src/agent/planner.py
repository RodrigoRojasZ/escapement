"""Planner (Fase A): un objetivo en lenguaje natural -> un roadmap de pasos con criterios de done.

El salto de "tarea" a "objetivo": en vez de recibir una directiva concreta, Escapement recibe una
META ("logra esto") y propone CÓMO llegar —descompone en pasos heterogéneos (investigar / crear /
editar / ejecutar / verificar / preguntar), cada uno con un criterio de *done* verificable y sus
dependencias, más el criterio de done del objetivo completo—. El planner solo PROPONE; el humano
aprueba (checkpoint). La ejecución del roadmap es la Fase B; aquí se genera, se muestra y se
persiste. Principio rector: sin criterios de done verificables no hay autonomía fiable, así que
el planner los exige (medibles: un test que pasa, un archivo que existe, una métrica, un comando).
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from agent import config, executors, fsutil, personas

# Tipos de paso (roadmap heterogéneo). La Fase B mapea cada tipo a un ejecutor concreto.
STEP_TYPES = (
    "investigar",
    "crear",
    "editar",
    "ejecutar",
    "verificar",
    "preguntar",
    "memoria",
    "reflexionar",
    "swarm",
)

# Estados válidos de un paso (los que el runner entiende). El CLI histórico no valida lo que le
# pasas; las tools conversacionales sí, porque su input lo genera un modelo y un estado inventado
# dejaría el paso invisible para el runner (solo 'pendiente' entra en :func:`runner._ready`).
STEP_STATES = ("pendiente", "hecho", "fallido", "bloqueado")

PLANNER_SYSTEM = (
    "Eres un planificador de ingeniería para un agente AUTÓNOMO. El agente ejecuta los pasos "
    "MECÁNICOS por su cuenta (editar, optimizar, correr comandos, verificar, moverse) sin "
    "preguntar; reserva el tipo 'preguntar' SOLO para una DIVERGENCIA REAL de caminos: una decisión "
    "de rumbo, una ambigüedad que cambia el resultado, o algo irreversible. Descompones el OBJETIVO "
    "en un roadmap de pasos accionables y auto-verificables, con criterios de 'done' CONCRETOS, "
    "EXTENSOS y medibles (un test que pasa, un archivo que existe, una métrica, un comando exitoso). "
    "Para tareas a gran escala, descompón en tantos pasos como haga falta. Respondes en español."
)

PLANNER_PROMPT = """OBJETIVO: {objetivo}

Descompón el objetivo en pasos. Responde SOLO con un JSON en una línea, sin texto alrededor:
{{"criterio_global": "cómo sabremos que el OBJETIVO completo está logrado (verificable)",
  "pasos": [
    {{"id": 1, "accion": "qué hacer", "tipo": "investigar|crear|editar|ejecutar|verificar|preguntar|memoria|reflexionar|swarm",
      "persona": "nombre de la persona experta (de la lista) o vacío", "done": "criterio de done verificable de este paso", "depende_de": []}}
  ]}}
Reglas:
- Cada 'done' debe ser VERIFICABLE, EXTENSO y medible (comandos, archivos, métricas concretas; no "mejorar X").
- 'depende_de' lista los ids previos; ordena los pasos respetando sus dependencias.
- El agente ejecuta lo mecánico por su cuenta; usa 'preguntar' SOLO en una divergencia de caminos (decisión de rumbo/irreversible), NUNCA en pasos rutinarios (editar/optimizar/correr/verificar).
- Descompón según la ESCALA real: pocos pasos para algo chico, muchos (10, 20 o más) para algo grande — no te limites artificialmente.
- 'ejecutar'/'verificar' son para comandos que el agente correrá SOLO; 'memoria' para notas de conocimiento (el vault), no código.
- Un paso de AUDITORÍA/REVISIÓN que solo COMPRUEBA que el código ya cumple una propiedad o restricción (p.ej. "auditar que la comparación de la API-key sea constante", "revisar que la clave nunca se loguee") es 'verificar', NO 'editar': su esencia es un chequeo con veredicto, no un cambio en disco. Reserva 'editar'/'crear' para pasos que DEBEN modificar archivos. Si la auditoría podría exigir un arreglo, deja que un 'reflexionar' posterior añada el 'editar' correctivo solo cuando el chequeo falle.
- Para crear/actualizar/archivar/borrar/reorganizar notas del vault usa SIEMPRE pasos tipo 'memoria' (el sistema los aplica con formato, índice y archivado gestionados). NUNCA uses 'editar'/'verificar' para crear carpetas de archivo ni inventes rutas como 'vault-historico': archivar es un 'memoria' con action archive, y el destino lo maneja el sistema.
- Intercala pasos 'reflexionar' TRAS las fases de descubrimiento (investigar/verificar): revisan lo hallado y AÑADEN al plan mejoras a aplicar o problemas a manejar que no habías previsto (auto-evolución en caliente).
- Usa 'swarm' para una INVESTIGACIÓN AMPLIA paralelizable (varios subsistemas/ángulos/dimensiones a la vez, SOLO LECTURA): el agente la descompone en subtareas independientes y las corre en paralelo. No lo uses para editar ni cuando un solo 'investigar' basta.
- 'persona': separa el trabajo por ESPECIALIDAD y ASIGNA el experto transversal correcto (de la lista) cuando una especialidad domina el paso — seguridad, tests, rendimiento, dependencias, migraciones, datos, devops, documentación, refactor, debugging, arquitectura o frontend. Aplica a pasos investigar/editar/crear/ejecutar/verificar/swarm y GANA sobre el coder de dominio del repo (el especialista es repo-agnóstico: una auditoría de secretos la hace 'seguridad' en cualquier repo). Déjala VACÍA cuando basta el experto por defecto (investigador para explorar amplio, revisor para verificar, coder de dominio para editar de rutina) o cuando ninguna especialidad domina; y SIEMPRE vacía en memoria/reflexionar/preguntar. Especialistas asignables:
{roster}
"""


@dataclass
class Step:
    """Un paso del roadmap: qué hacer, de qué tipo, su criterio de done y de qué pasos depende.

    ``estado``/``nota`` los usa el runner de la Fase B para trackear la ejecución (reanudable);
    en un plan recién generado todos los pasos están ``pendiente`` con nota vacía.
    """

    id: int
    accion: str
    tipo: str  # uno de STEP_TYPES
    done: str  # criterio de done verificable de ESTE paso
    depende_de: list[int] = field(default_factory=list)
    estado: str = "pendiente"  # pendiente | hecho | fallido | bloqueado (Fase B)
    nota: str = ""  # resultado/hallazgo/motivo del checkpoint tras ejecutar el paso
    persona: str = (
        ""  # experto asignado por el planner (nombre de personas.PERSONAS); '' = auto-ruteo
    )


@dataclass
class Plan:
    """Roadmap propuesto para un objetivo: criterio de done global + pasos ordenados.

    ``repo`` (Fase B) recuerda sobre qué repositorio se ejecuta el plan, para no re-pasarlo en
    cada reanudación; lo fija ``escapement ejecutar <repo>`` la primera vez.
    ``workdir``/``rama`` (S2) son el worktree aislado del plan y su rama git: los pasos que
    editan corren ahí (nunca sobre el repo real) y persisten entre reanudaciones.
    ``eval_veredicto``/``eval_gaps`` (E2, deuda #7) son el resultado de la evaluación global al
    cerrar: ``""`` = aún sin evaluar, ``"done"`` = criterio global cumplido, ``"incompleto"`` =
    no cumplido, con los gaps concretos (insumo de la replanificación).
    ``replan_ciclos`` (E2, pieza 2) cuenta los ciclos de replanificación automática ya consumidos
    por este plan; persiste para que reanudar no resetee el tope (``config.PLAN_REPLAN_MAX``).
    """

    objetivo: str
    criterio_global: str
    pasos: list[Step] = field(default_factory=list)
    repo: str = ""
    workdir: str = ""
    rama: str = ""
    eval_veredicto: str = ""  # "" | "done" | "incompleto"
    eval_gaps: list[str] = field(default_factory=list)
    replan_ciclos: int = 0


def _parse(out: str, objetivo: str) -> Plan:
    """Extrae el roadmap del JSON que devuelve el planner (robusto a texto alrededor).

    Si no hay JSON válido, devuelve un Plan SIN pasos (el CLI lo reporta y no ejecuta nada).
    """
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return Plan(objetivo, "(no se pudo generar un criterio)", [])
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return Plan(objetivo, "(no se pudo generar un criterio)", [])
    pasos: list[Step] = []
    for i, raw in enumerate(data.get("pasos", []), 1):
        if not isinstance(raw, dict):
            continue
        pasos.append(
            Step(
                id=int(raw.get("id", i)),
                accion=str(raw.get("accion", "")).strip(),
                tipo=str(raw.get("tipo", "editar")).strip().lower(),
                done=str(raw.get("done", "")).strip(),
                depende_de=[
                    int(d) for d in raw.get("depende_de", []) if isinstance(d, int | float)
                ],
                persona=str(raw.get("persona", "")).strip().lower(),
            )
        )
    return Plan(objetivo, str(data.get("criterio_global", "")).strip(), pasos)


def plan(objetivo: str, *, context: str = "", timeout: float = 300) -> Plan:
    """Genera un roadmap para ``objetivo`` con el executor (razona, no edita nada).

    Args:
        objetivo: la meta en lenguaje natural.
        context: contexto adicional que se ADJUNTA al prompt del planner (p.ej. trayectorias
            exitosas previas del ReasoningBank). Default '' = sin contexto extra (comportamiento
            previo intacto). No cambia el formato de salida esperado; solo informa el razonamiento.
        timeout: segundos máximos de espera al planner.
    """
    prompt = (
        f"{PLANNER_SYSTEM}\n\n"
        + PLANNER_PROMPT.format(objetivo=objetivo, roster=personas.roster_brief())
        + (context or "")
    )
    with tempfile.TemporaryDirectory() as tmp:
        # El planner corre SIEMPRE en MODEL_PLANNER (online medio, sólido), ajeno al tiering y al
        # ruteo local. El único override que respeta es EXECUTOR_MODEL: un martillo manual y explícito
        # para probar un modelo nuevo end-to-end (planner incluido), no un flag de ruteo automático.
        _ok, out = executors.run_agent(
            prompt,
            cwd=tmp,
            timeout=timeout,
            mode="read",
            model=config.EXECUTOR_MODEL or config.MODEL_PLANNER,
        )
    return _parse(out, objetivo)


def repo_plan_path(repo: str) -> Path:
    """Ruta del plan DEDICADO de un repo (``data/plan_<slug>.json``): un plan por repo, independiente.

    Cada repo tiene su propio archivo de plan; ``escapement ejecutar <repo>`` lo corre y persiste ahí
    mismo (reanudable por repo, sin pisar a los demás). El slug es el del vault (:func:`config.vault_slug`).
    """
    return config.DATA_DIR / f"plan_{config.vault_slug(repo)}.json"


def _active_ptr() -> Path:
    """Archivo puntero (``data/active_plan``) que recuerda qué plan es el ACTIVO (por filename)."""
    return config.DATA_DIR / "active_plan"


def set_active_plan(path: Path) -> None:
    """Marca ``path`` como el plan activo (para ``ejecutar``/``paso`` sin repetir el repo).

    Guarda solo el *filename* (relativo a ``DATA_DIR``) para ser robusto a cambios de ruta base.
    """
    fsutil.write_text_atomic(_active_ptr(), Path(path).name)


def active_plan_path() -> Path:
    """Ruta del plan activo según el puntero; cae a ``config.PLAN`` (scratch) si no hay uno válido."""
    ptr = _active_ptr()
    if ptr.exists():
        name = ptr.read_text(encoding="utf-8").strip()
        cand = config.DATA_DIR / name
        if name and cand.exists():
            return cand
    return config.PLAN


def plan_files() -> list[Path]:
    """Archivos de plan que existen hoy: el scratch (``config.PLAN``) primero, luego los por-repo.

    Fuente única del inventario de planes, compartida por ``escapement plan lista`` y por la tool
    conversacional ``plan`` (mismo conjunto de archivos, formato de salida distinto en cada una).
    """
    return ([config.PLAN] if config.PLAN.exists() else []) + sorted(
        config.DATA_DIR.glob("plan_*.json")
    )


def plan_slot(repo: str = "") -> Path:
    """Archivo de plan a usar para ejecutar/reanudar, y lo deja ACTIVO si vino ``repo``.

    Args:
        repo: raíz YA resuelta del repo (ver ``orchestrator.resolve_repo``). Con repo devuelve su
            plan dedicado (``data/plan_<slug>.json``) y lo marca activo, para que las siguientes
            invocaciones sin repo lo retomen. Cadena vacía (default) = el plan activo, sin tocar
            el puntero.
    """
    if repo:
        path = repo_plan_path(repo)
        set_active_plan(path)
        return path
    return active_plan_path()


def set_step_state(p: Plan, step_id: int, estado: str, nota: str | None = None) -> Step | None:
    """Fija el ``estado`` del paso ``step_id`` (y su ``nota`` si no es None). NO persiste el plan.

    Es el mecanismo para pasar un checkpoint: el humano resuelve lo que bloqueó al runner y marca
    el paso, y la ``nota`` queda como contexto para los pasos siguientes.

    Args:
        p: plan cargado que se muta en sitio.
        step_id: id del paso a marcar.
        estado: nuevo estado (ver :data:`STEP_STATES`; no se valida aquí).
        nota: respuesta/observación a guardar; ``None`` (default) deja la nota previa intacta.

    Returns:
        El paso mutado, o ``None`` si el plan no tiene ningún paso con ese id.
    """
    for s in p.pasos:
        if s.id == step_id:
            s.estado = estado
            if nota is not None:
                s.nota = nota
            return s
    return None


def remove_step(p: Plan, step_id: int) -> Step | None:
    """Quita el paso ``step_id`` y RECONECTA las dependencias (empalme del grafo). NO persiste.

    Los pasos que dependían del quitado heredan sus dependencias: si 3 dependía de 2 y 2 de 1,
    quitar el 2 deja a 3 dependiendo de 1. Así el frente topológico del runner sigue consistente
    sin renumerar nada (los ids restantes no cambian; ``escapement paso <id>`` sigue apuntando a lo
    mismo).

    Args:
        p: plan cargado que se muta en sitio.
        step_id: id del paso a quitar.

    Returns:
        El paso quitado, o ``None`` si el plan no tiene ningún paso con ese id.
    """
    quitado = next((s for s in p.pasos if s.id == step_id), None)
    if quitado is None:
        return None
    herencia = [d for d in quitado.depende_de if d != step_id]
    p.pasos.remove(quitado)
    for s in p.pasos:
        if step_id not in s.depende_de:
            continue
        nuevas: list[int] = []
        for d in s.depende_de:
            for r in herencia if d == step_id else [d]:
                if r != s.id and r not in nuevas:
                    nuevas.append(r)
        s.depende_de = nuevas
    return quitado


def move_step(p: Plan, step_id: int, pos: int) -> Step | None:
    """Mueve el paso ``step_id`` a la posición ``pos`` (1-based) del roadmap. NO persiste.

    El orden de la lista es la prioridad de despacho (el runner corre el primer paso listo) y el
    orden de lectura de ``escapement plan ver``, así que se valida que la lista siga TOPOLÓGICAMENTE
    ordenada: ningún paso puede quedar antes de una de sus dependencias.

    Args:
        p: plan cargado que se muta en sitio.
        step_id: id del paso a mover (el id no cambia, solo su posición).
        pos: posición destino 1-based; fuera de rango se recorta a [1, len(pasos)].

    Returns:
        El paso movido, o ``None`` si el plan no tiene ningún paso con ese id.

    Raises:
        ValueError: si la nueva posición rompe el orden topológico (un paso quedaría antes de una
            dependencia). El plan queda intacto.
    """
    idx = next((i for i, s in enumerate(p.pasos) if s.id == step_id), None)
    if idx is None:
        return None
    step = p.pasos[idx]
    destino = max(1, min(pos, len(p.pasos))) - 1
    nuevos = p.pasos[:idx] + p.pasos[idx + 1 :]
    nuevos.insert(destino, step)
    posicion = {s.id: i for i, s in enumerate(nuevos)}
    for s in nuevos:
        for d in s.depende_de:
            if d in posicion and posicion[d] > posicion[s.id]:
                raise ValueError(f"el paso {s.id} depende del {d}, que quedaría después")
    p.pasos[:] = nuevos
    return step


def edit_step(p: Plan, step_id: int, accion: str) -> Step | None:
    """Reemplaza la ``accion`` del paso ``step_id``. NO persiste el plan.

    Solo toca el texto de la acción: tipo, done, dependencias, estado y nota quedan intactos
    (para eso están los otros comandos / ``set_step_state``).

    Args:
        p: plan cargado que se muta en sitio.
        step_id: id del paso a editar.
        accion: nuevo texto de la acción (el llamador valida que no venga vacío).

    Returns:
        El paso editado, o ``None`` si el plan no tiene ningún paso con ese id.
    """
    for s in p.pasos:
        if s.id == step_id:
            s.accion = accion
            return s
    return None


def save_plan(p: Plan, path: Path | None = None) -> None:
    """Persiste un plan en ``path`` (por defecto ``config.PLAN``, el slot activo)."""
    dest = path or config.PLAN
    fsutil.write_text_atomic(dest, json.dumps(asdict(p), ensure_ascii=False, indent=2))


def load_plan(path: Path | None = None) -> Plan | None:
    """Carga un plan de ``path`` (por defecto ``config.PLAN``, el slot activo), o None si no existe."""
    src = path or config.PLAN
    if not src.exists():
        return None
    try:
        data = json.loads(src.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return None  # archivo a medio escribir/corrupto (otra corrida escribiendo): no revientes
    pasos = [Step(**s) for s in data.get("pasos", [])]
    return Plan(
        data.get("objetivo", ""),
        data.get("criterio_global", ""),
        pasos,
        data.get("repo", ""),
        data.get("workdir", ""),
        data.get("rama", ""),
        # planes guardados antes de E2 no traen estos campos -> defaults (sin evaluar)
        data.get("eval_veredicto", ""),
        list(data.get("eval_gaps", []) or []),
        int(data.get("replan_ciclos", 0) or 0),
    )
