"""ReasoningBank (R2): aprende de las trayectorias que SI funcionaron.

Cierra el loop de auto-mejora de la memoria operativa. Cuando un plan se completa con EXITO
(todos los pasos 'hecho'), destila la trayectoria (objetivo, criterio y la secuencia de pasos
con sus hallazgos) en una nota del vault -> queda indexable por el recall semantico. Antes de
planear un objetivo nuevo, recupera las trayectorias exitosas mas parecidas y se las pasa al
planner como contexto ("asi resolviste algo similar"). Idea de ruflo/ReasoningBank, con
implementacion propia sobre la memoria que YA existe (tools/memory + tools/semantic).

Todo es ADITIVO y best-effort: sin las deps del grupo 'semantic', el recall devuelve '' (el
planner planea igual que antes); una captura que falla nunca rompe la ejecucion del plan. Las
trayectorias viven en su propia carpeta del vault (:data:`REASONING_SLUG`) para no mezclarse
con las notas de conocimiento de los repos.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from agent import fsutil
from agent.planner import Plan
from agent.tools import memory

REASONING_SLUG = "escapement-reasoning"  # carpeta del vault donde viven las trayectorias exitosas
_MAX_NOTE_STEPS = 40  # tope de pasos citados en una nota (un plan enorme no la infla)
_NOTA_TRIM = 200  # cuanto de la nota de cada paso se conserva en la trayectoria


def _slug(objetivo: str) -> str:
    """Slug estable y valido (a-z0-9-) para la nota de una trayectoria.

    Deterministico por objetivo: re-ejecutar el mismo objetivo ACTUALIZA su nota (no duplica).
    Combina las primeras palabras legibles con un hash corto del objetivo para garantizar
    unicidad aun cuando dos objetivos empiecen igual.
    """
    palabras = re.findall(r"[a-z0-9]+", (objetivo or "").lower())
    cuerpo = "-".join(palabras[:6]) or "objetivo"
    h = hashlib.sha1((objetivo or "").encode("utf-8")).hexdigest()[:6]
    return f"traj-{cuerpo}-{h}"[:80]


def _es_exito(plan: Plan) -> bool:
    """True si el plan se completo entero (hay pasos y TODOS quedaron 'hecho')."""
    return bool(plan.pasos) and all(s.estado == "hecho" for s in plan.pasos)


def _render(plan: Plan) -> tuple[str, str]:
    """(descripcion, body markdown) de una trayectoria exitosa a partir de un plan completo."""
    lineas = []
    for s in plan.pasos[:_MAX_NOTE_STEPS]:
        nota = " ".join((s.nota or "").split())[:_NOTA_TRIM]
        lineas.append(f"{s.id}. [{s.tipo}] {s.accion}" + (f" -> {nota}" if nota else ""))
    resto = len(plan.pasos) - _MAX_NOTE_STEPS
    extra = f"\n(+{resto} paso(s) mas)" if resto > 0 else ""
    descripcion = " ".join((plan.objetivo or "").split())[:200]
    body = (
        f"**Objetivo:** {plan.objetivo}\n\n"
        f"**Criterio global:** {plan.criterio_global}\n\n"
        f"**Repo:** {plan.repo or '(sin repo)'}\n\n"
        f"**Trayectoria que funciono ({len(plan.pasos)} pasos):**\n" + "\n".join(lineas) + extra
    )
    return descripcion, body


def capture_trajectory(plan: Plan, *, projects_dir: Path | None = None) -> bool:
    """Post-task: si el plan fue un EXITO, destila la trayectoria a una nota del vault.

    Deterministico (sin LLM): registra objetivo, criterio y la secuencia de pasos que
    funciono, para que el recall semantico la encuentre al planear algo parecido.

    Args:
        plan: el plan ya ejecutado; solo se captura si TODOS sus pasos quedaron 'hecho'.
        projects_dir: raiz del vault (inyectable en tests); None = ``config.PROJECTS_DIR``.

    Returns:
        True si escribio la nota; False si el plan no fue exito o si la escritura fallo
        (best-effort: un fallo aqui nunca debe tumbar la corrida del plan).
    """
    if not _es_exito(plan):
        return False
    try:
        name = _slug(plan.objetivo)
        descripcion, body = _render(plan)
        path = memory.resolve_note_path(REASONING_SLUG, name, projects_dir=projects_dir)
        fsutil.write_text_atomic(path, memory.build_note(name, descripcion, "reference", body))
        memory.upsert_index(path.parent, (plan.objetivo or name)[:60], name, descripcion)
        return True
    except Exception:  # noqa: BLE001 - una captura fallida nunca rompe la ejecucion del plan
        return False


def recall_trajectories(objetivo: str, k: int = 3) -> str:
    """Pre-plan: bloque de texto con las trayectorias exitosas mas parecidas a ``objetivo``.

    Read-only, sobre el recall semantico del vault filtrado a la carpeta de trayectorias. Listo
    para anteponer/adjuntar al prompt del planner.

    Args:
        objetivo: la meta que se va a planear (query semantica).
        k: cuantas trayectorias como maximo inyectar.

    Returns:
        El bloque de contexto, o '' si no hay coincidencias o si las deps del grupo 'semantic'
        no estan instaladas (en cuyo caso el planner planea igual que antes: comportamiento previo).
    """
    try:
        from agent.tools import semantic

        hits = semantic.search(objetivo, k=max(k * 4, 8))
    except ImportError:
        return ""  # sin deps de 'semantic': el recall es opcional, se planea sin el
    except Exception:  # noqa: BLE001 - el recall es una ayuda; si falla, no bloquea el planner
        return ""
    trajes = [h for h in hits if h[1] == REASONING_SLUG][:k]
    lineas = []
    for _score, _proj, _name, path in trajes:
        try:
            texto = " ".join(Path(path).read_text(encoding="utf-8", errors="replace").split())
        except OSError:
            continue
        lineas.append(f"- {texto[:400]}")
    if not lineas:
        return ""
    return (
        "\n\nTRAYECTORIAS EXITOSAS PREVIAS (referencia de como resolviste objetivos parecidos; "
        "adapta lo util, no lo copies a ciegas):\n" + "\n".join(lineas)
    )
