"""Runner del roadmap (Fase C): ejecuta el plan del planner de forma AUTÓNOMA.

Recorre los pasos en orden topológico (un paso corre solo cuando TODAS sus dependencias están
``hecho``), despacha cada uno según su ``tipo`` a un handler, y persiste el estado tras cada paso
-> es REANUDABLE. Se detiene solo si un paso queda ``fallido``.

AUTÓNOMO en lo mecánico, consulta SOLO en divergencias: cada paso rutinario se EJECUTA por su
cuenta —``investigar`` es un dispatch read-only; ``editar``/``crear`` van por ``optimize`` (worktree
+ PR sin mergear) o, sin target concreto, un dispatch de edición directa; ``ejecutar`` y
``verificar`` despachan a Claude para correr comandos y evaluar el ``done``; ``memoria`` compila
notas—. El ÚNICO checkpoint humano es ``preguntar``, que el planner reserva para una divergencia
real de caminos (decisión de rumbo/irreversible): ahí para y espera tu decisión.

Jaulas (S1+S2): los dispatch headless de claude llevan el guard como hook PreToolUse (SQL/ramas/
.env; ver ``executors._guard_settings_path``), y los pasos corren en el WORKTREE del plan
(``plan.workdir``, rama ``escapement/plan-<stamp>``), nunca con cwd=repo real — el repo solo se toca
al mergear tú la rama. Si el repo del plan no es git, se cae al comportamiento previo (cwd=repo).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from agent import bus, config, costs, executors, fsutil, local_models, personas, planner
from agent.planner import Plan, Step

PENDIENTE, HECHO, FALLIDO, BLOQUEADO = "pendiente", "hecho", "fallido", "bloqueado"

# Detector de FALSO ÉXITO: el executor salió con código 0 pero ADMITE en prosa que no aplicó el
# cambio (permiso denegado, "te lo dejo para pegar", no persistió). ``returncode == 0`` no basta para
# dar un paso por HECHO: esto atrapa el falso 'hecho' sin depender del modelo —cualquier executor
# honesto que reporte el fallo en texto cae aquí (Claude/Antigravity/Cursor)—. Se aplica SOLO a
# pasos que DEBEN cambiar algo (editar/ejecutar), no a investigar (que legítimamente solo da texto).
# Agnóstico al modelo Y al idioma (español + inglés). Cuidado con dos trampas de falso positivo:
# distinguir el IMPERATIVO dirigido al usuario ("cópialo y pégalo tú") de la descripción en pasado de un
# edit exitoso ("copié y pegué la función"); y "no persistió/persistí" (confesión) de "no persistir" (una
# decisión de diseño legítima). Por eso los patrones exigen formas concretas, no comodines amplios.
_NO_COMPLETO = re.compile(
    # -- permiso denegado (es/en) --
    r"no\s+(?:tengo|tuve|hay)\s+permiso"
    r"|permiso\s+deneg\w*"
    r"|permission\s+denied"
    r"|(?:write|escritura)\s+(?:fue\s+|se\s+|was\s+)?deneg\w*"
    r"|write\s+was\s+denied"
    # -- imperativo dirigido al usuario: pégalo/aplícalo tú (NO 'pegué' en pasado) --
    r"|para\s+que\s+(?:lo|los|la|las)\s+(?:pegu\w*|apliqu\w*|copi\w*)"
    r"|c[oó]pia\w*\s+y\s+p[eé]ga\w*"
    r"|copy\s+and\s+paste\s+it\s+yourself"
    r"|tendr[aá]s\s+que\s+\w+\s*(?:lo|la)?\s+manual"
    r"|solo\s+lectura|read-?only\s+file"
    # -- no pude/no se pudo escribir/crear/guardar/persistir/aplicar (es) --
    r"|no\s+(?:lo\s+|la\s+)?(?:pude|puedo|logr[eé])\s+(?:escribir|crear|guardar|persistir|aplicar)"
    r"|no\s+se\s+(?:pudo|pudieron)\s+(?:escribir|crear|guardar|guardaron|persistir|aplicar)"
    r"|no\s+se\s+guard\w+\s+(?:el\s+|los\s+|los\s+cambios|cambios)"
    r"|no\s+(?:se\s+)?persist(?:i[oó]|í)\b"
    r"|sin\s+persistir\s+(?:el\s+|los\s+)?archivo"
    # -- inglés: could not / unable to / failed to write|persist|save|create|apply|patch|edit --
    r"|could\s*n[o']?t\s+(?:write|persist|save|create|apply|edit)"
    r"|unable\s+to\s+(?:write|persist|save|create|apply|edit)"
    r"|failed\s+to\s+(?:write|persist|save|create|apply|edit|patch)",
    re.IGNORECASE,
)

# Palabras vacías para comparar la similitud entre pasos (dedup de auto-evolución): no aportan señal.
_STOP = frozenset(
    "de la el los las un una unos unas y o u a en con por para que se su sus al del lo le nos "
    "como mas más este esta esto ese esa sobre entre".split()
)


def _admite_no_completar(out: str) -> bool:
    """True si el texto del executor confiesa que no pudo aplicar el cambio (falso 'hecho')."""
    return bool(_NO_COMPLETO.search(out or ""))


def _confesion_no_completar(out: str) -> str | None:
    """El fragmento exacto donde el executor confiesa que no aplicó el cambio (o None).

    Igual que ``_admite_no_completar`` pero devuelve la evidencia para citarla en la nota del paso:
    una nota que dice *qué* frase delató el falso 'hecho' es diagnosticable; un genérico no lo es.
    """
    m = _NO_COMPLETO.search(out or "")
    return m.group(0).strip() if m else None


# Señal POSITIVA, simétrica a ``_NO_COMPLETO``: el executor AUDITÓ y concluyó que el repo YA cumple el
# criterio, así que no había nada que editar. Sin esto, un paso 'editar' de auditoría (p.ej. "audita que
# la comparación de la API-key sea constante") sobre código que ya es correcto no deja diff y se marcaría
# FALLIDO por "sin efecto en disco" —un falso fallo—. Conservador: exige una conclusión EXPLÍCITA de
# conformidad, no un genérico "todo listo". Se evalúa DESPUÉS de descartar una confesión de fallo
# (``_NO_COMPLETO`` tiene prioridad): "no pude, pero ya cumplía" es un fallo, no una auditoría limpia.
_YA_CUMPLE = re.compile(
    r"ya\s+(?:cumple|cumpl\w+|satisface|es\s+constante|usa\s+\w+|est[aá]\s+(?:correct\w*|bien))"
    r"|no\s+(?:fue|es|era|hac[eí]a?\s+falta)\s+necesari\w*\s+(?:modificar|cambiar|editar|tocar|aplicar)"
    r"|no\s+(?:se\s+)?requier\w*\s+(?:de\s+)?cambios?"
    r"|no\s+hay\s+(?:nada\s+que|cambios?\s+que)\s+(?:cambiar|modificar|aplicar|hacer)"
    r"|sin\s+cambios?\s+necesari\w*"
    r"|cumple\s+(?:con\s+)?(?:todos?\s+|cada\s+|ambos\s+)?(?:el|los)\s+criterios?"
    r"|todo\s+(?:correcto|conforme|en\s+orden|en\s+regla)"
    # -- inglés --
    r"|already\s+(?:compliant|correct|constant|uses|meets|satisfies)"
    r"|no\s+changes?\s+(?:needed|required|necessary)"
    r"|nothing\s+to\s+(?:change|modify|fix)"
    r"|no\s+(?:modification|edit)s?\s+(?:needed|required)",
    re.IGNORECASE,
)


def _sin_cambio_necesario(out: str) -> bool:
    """True si el executor concluye EXPLÍCITAMENTE que el repo ya cumple (auditoría sin cambios)."""
    return bool(_YA_CUMPLE.search(out or ""))


def _git_run(repo: str, *args: str) -> str | None:
    """Corre ``git <args>`` en ``repo`` y devuelve stdout, o None si git falla/ausente."""
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


_HUELLA_UNTRACKED_BYTES = 8 * 1024 * 1024  # techo de lectura por huella (deuda #17)


def _huella_untracked(repo: str) -> str:
    """Marca del CONTENIDO de cada archivo untracked, una línea por archivo. "" si no hay ninguno.

    Deuda #17. Tercer componente de la huella de :func:`_git_status`: ni el porcelain ni
    ``git diff HEAD`` ven el contenido de un archivo sin trackear —el porcelain imprime ``?? a.md``
    igual antes y después de reescribirlo, y el diff solo mira lo que el índice conoce—, así que
    reescribir un archivo que un paso anterior dejó untracked daba huella idéntica y el guard lo
    marcaba "sin efecto en disco". Pasó en la validación de E2 (escenario 2, paso 9).

    Lista con ``ls-files --others --exclude-standard -z``: el mismo criterio de ignorados que el
    porcelain, y ``-z`` evita el quoting de ``core.quotepath`` (las rutas con acentos llegan crudas).

    Args:
        repo: carpeta del repo git, la misma que recibe :func:`_git_status`.

    Returns:
        Una línea ``"<marca> <ruta>"`` por archivo, ordenadas por ruta para que la huella sea
        estable. La marca es el sha256 del contenido; ``size:<n>`` si el archivo ya no cabe en el
        presupuesto de lectura (``_HUELLA_UNTRACKED_BYTES``, para que un ``node_modules/`` sin
        trackear no vuelva lenta cada comprobación) e ``ilegible`` si desapareció entre el listado
        y la lectura. "" si git falla o no hay untracked: la huella queda como estaba.
    """
    listado = _git_run(repo, "ls-files", "--others", "--exclude-standard", "-z")
    if not listado:
        return ""
    lineas, presupuesto = [], _HUELLA_UNTRACKED_BYTES
    for rel in sorted(x for x in listado.split("\0") if x):
        ruta = Path(repo) / rel
        try:
            tam = ruta.stat().st_size
            if tam > presupuesto:
                marca = f"size:{tam}"  # sin presupuesto: al menos capta el cambio de tamaño
            else:
                presupuesto -= tam
                marca = hashlib.sha256(ruta.read_bytes()).hexdigest()
        except OSError:
            marca = "ilegible"  # borrado tras el listado, enlace roto, permiso denegado
        lineas.append(f"{marca} {rel}")
    return "\n".join(lineas)


def _huella_tracked(repo: str) -> str | None:
    """Huella del árbol SOLO de lo que git ya conoce: modificados y borrados, sin archivos nuevos.

    Deuda #12: sirve para detectar que un paso de ``verificar`` TOCÓ el árbol que auditaba. Deja
    fuera los untracked a propósito —al revés que :func:`_git_status`—: una verificación legítima
    corre ``pytest`` y eso siembra ``.pytest_cache/`` y ``__pycache__/`` en cualquier repo que no los
    ignore; contarlos como "el verificador escribió" frenaría corridas sanas. Lo que sí importa
    —reescribir el código que se estaba revisando— es una modificación de algo TRACKED y sí se ve.

    Args:
        repo: carpeta del worktree donde corre el paso.

    Returns:
        ``porcelain_sin_untracked + "\\0" + diff``, o None si no es repo git (ahí no se compara nada
        y ``verificar`` se comporta como antes).
    """
    porcelain = _git_run(repo, "status", "--porcelain", "--untracked-files=no")
    if porcelain is None:
        return None
    return porcelain + "\0" + (_git_run(repo, "diff", "HEAD") or "")


def _git_status(repo: str) -> str | None:
    """Huella del árbol de trabajo SENSIBLE AL CONTENIDO. None si no es repo git o git falla.

    Verifica EFECTO EN DISCO: si tras un paso de edición la huella no cambió, el executor no tocó
    nada (falso 'hecho') sin importar qué diga su prosa. Agnóstico al modelo. None (repo no-git o
    git ausente) desactiva la verificación —no bloquea— para no romper repos sin git.

    Combina ``git status --porcelain`` (capta archivos NUEVOS/untracked) con ``git diff HEAD`` (capta
    el CONTENIDO de lo modificado). Solo el porcelain sería ciego al contenido: reeditar un archivo que
    ya estaba modificado deja la lista idéntica y daría un falso 'sin efecto'. El diff cambia con cada
    edición real. ``git diff HEAD`` puede no existir (repo sin commits): en ese caso basta el porcelain.

    Un TERCER componente, tras el segundo ``\\0``, cubre el hueco que quedaba: el contenido de los
    archivos untracked, que no entran en el diff y cuyo porcelain es idéntico antes y después de
    reescribirlos (deuda #17, ver :func:`_huella_untracked`). Va al final a propósito:
    :func:`_archivos_tocados` lee solo hasta el PRIMER ``\\0``, así que no le afecta.
    """
    porcelain = _git_run(repo, "status", "--porcelain")
    if porcelain is None:
        return None
    diff = (
        _git_run(repo, "diff", "HEAD") or ""
    )  # "" si no hay HEAD (repo sin commits): usa solo porcelain
    return porcelain + "\0" + diff + "\0" + _huella_untracked(repo)


def _tokens(texto: str) -> set[str]:
    """Tokens significativos (minúsculas, sin palabras vacías) para medir similitud entre pasos."""
    return {t for t in re.findall(r"[a-záéíóúüñ0-9]+", (texto or "").lower()) if t not in _STOP}


_DEDUP_UMBRAL = 0.7  # Jaccard mínimo para tratar dos pasos como el mismo


def _es_duplicado(accion: str, done: str, plan: Plan) -> bool:
    """True si ``(accion, done)`` solapa fuerte (Jaccard >= _DEDUP_UMBRAL) con un paso VIVO del plan.

    Evita que la auto-evolución (``reflexionar``) re-agregue trabajo ya planificado —el modo de fallo
    de re-sintetizar un roadmap/memoria que ya era un paso—. Umbral alto: solo descarta casi-idénticos
    (un umbral bajo confundiría intenciones opuestas —"agregar X" vs "quitar X", que comparten casi
    todos los tokens— con duplicados). Ignora pasos ``fallido``/``bloqueado``: una reflexión que
    propone reintentar un paso que falló NO es un duplicado, es justo el paso correctivo que se busca.
    """
    nuevos = _tokens(accion) | _tokens(done)
    if len(nuevos) < 4:  # muy poca señal para juzgar solape con fiabilidad
        return False
    for s in plan.pasos:
        if s.estado in (FALLIDO, BLOQUEADO):
            continue  # reintentar algo que falló/quedó bloqueado no es duplicar
        viejos = _tokens(s.accion) | _tokens(s.done)
        if viejos and len(nuevos & viejos) / len(nuevos | viejos) >= _DEDUP_UMBRAL:
            return True
    return False


_NOTE_LIMIT = (
    1500  # cuánto del resultado de un paso se guarda en su nota (detalle sin inflar el plan)
)

# Un handler recibe (paso, plan, repo) y devuelve (nuevo_estado, nota).
Handler = Callable[[Step, Plan, str], "tuple[str, str]"]


def _ready(plan: Plan) -> list[Step]:
    """Pasos ``pendiente`` cuyas dependencias están TODAS en ``hecho`` (frente topológico)."""
    hechos = {s.id for s in plan.pasos if s.estado == HECHO}
    return [s for s in plan.pasos if s.estado == PENDIENTE and set(s.depende_de) <= hechos]


def pasos_listos(plan: Plan) -> list[Step]:
    """Pasos que el runner despacharía AHORA: pendientes con TODAS sus dependencias en ``hecho``.

    Superficie pública del frente topológico que ya calcula :func:`_ready`, para que la vista de
    lectura del CLI (``escapement plan ver``) muestre "el próximo paso listo" sin reimplementarlo.

    Args:
        plan: plan cargado; no se muta ni se persiste.

    Returns:
        Los pasos listos en el orden del roadmap (el runner despacha el primero). Lista vacía si
        el plan está completo o si todo lo pendiente espera a un paso trabado (checkpoint).
    """
    return _ready(plan)


def _target(step: Step) -> str | None:
    """Detecta un archivo concreto (``*.py``) en la acción, para despacharlo a ``optimize``."""
    m = re.search(r"[\w./\\-]+\.py\b", step.accion)
    return m.group(0) if m else None


def _context(step: Step, plan: Plan) -> str:
    """Notas de las dependencias del paso (respuestas de checkpoints previos, hallazgos).

    Es lo que hace que la aclaración que diste en un paso ``preguntar`` (guardada en su ``nota``)
    fluya como contexto a los pasos que dependen de él (investigar/editar).
    """
    por_id = {s.id: s for s in plan.pasos}
    notas = [por_id[d].nota for d in step.depende_de if por_id.get(d) and por_id[d].nota]
    if not notas:
        return ""
    return "\n\nContexto de pasos previos (respuestas/hallazgos):\n" + "\n".join(
        f"- {n}" for n in notas
    )


# --- Worktree del plan (S2): los pasos nunca corren con cwd=repo real ---


def _worktree_plan(plan: Plan, repo: str) -> str:
    """Crea (o reusa) el worktree persistente del plan. Devuelve su ruta, o '' si no aplica.

    S2/H2: contiene el blast radius del dispatch headless — los pasos editan en un worktree
    con rama propia (``escapement/plan-<stamp>``), y el repo real solo cambia cuando TÚ mergeas
    esa rama. Persiste en ``plan.workdir``/``plan.rama`` entre reanudaciones para que un
    ``verificar`` vea lo que dejó el ``editar`` previo; si el workdir se borró (temp limpiado)
    pero la rama existe, se re-monta sobre ella y el avance commiteado se conserva.
    Best-effort: si ``repo`` no es un repo git (o git falla), devuelve '' y el runner cae al
    comportamiento previo (cwd=repo).
    """
    if plan.workdir and os.path.isdir(plan.workdir):
        return plan.workdir
    if not os.path.isdir(repo):
        return ""
    try:
        probe = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--git-dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if probe.returncode != 0:
            return ""
        rama = plan.rama or f"escapement/plan-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        existe = (
            subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", rama],
                capture_output=True,
                text=True,
                timeout=10,
            ).returncode
            == 0
        )
        # Si el workdir anterior se borró (temp limpiado), git aún lo registra y cree la rama
        # "checked out": sin prune, el re-montaje fallaría y caeríamos al repo real en silencio.
        subprocess.run(
            ["git", "-C", repo, "worktree", "prune"], capture_output=True, text=True, timeout=30
        )
        dest = os.path.join(tempfile.mkdtemp(prefix="escapement-plan-"), "wt")
        cmd = ["git", "-C", repo, "worktree", "add"] + (
            [dest, rama] if existe else ["-b", rama, dest]
        )
        if subprocess.run(cmd, capture_output=True, text=True, timeout=60).returncode != 0:
            return ""
    except (OSError, subprocess.SubprocessError):
        return ""
    plan.workdir, plan.rama = dest, rama
    return dest


def _workdir(plan: Plan, repo: str) -> str:
    """Cwd de dispatch de los pasos: el worktree del plan si existe; si no, el repo real.

    El repo real como cwd es SOLO el fallback de repos no-git. Personas y memoria siguen
    resolviéndose con ``repo`` (rutas canónicas en ``config.REPOS``/vault), no con este cwd.
    """
    return plan.workdir if plan.workdir and os.path.isdir(plan.workdir) else repo


# --- Handlers por tipo ---


def _nota_fallo(out: str, generico: str) -> str:
    """Nota para un paso FALLIDO: siempre antepone el detalle real del executor (pre-flight
    de secretos, timeout, crash, denegación de tool) al mensaje genérico — sin esto el
    usuario ve "no completó la tarea" sin ninguna pista de la causa real."""
    detalle = (out or "").strip()
    if not detalle:
        return generico
    if detalle.startswith("pre-flight"):
        return detalle[:_NOTE_LIMIT]
    return f"{generico}: {detalle}"[:_NOTE_LIMIT]


def _archivos_tocados(fingerprint: str | None) -> list[str]:
    """Rutas del árbol de trabajo a partir de la huella de ``_git_status`` (su parte porcelain).

    Sirve para que la nota de un paso liste QUÉ archivos cambió en disco, sin un git extra: la huella
    ya trae ``git status --porcelain`` antes del ``\\0``. Toma el destino en renames ('old -> new').
    """
    if not fingerprint:
        return []
    rutas = []
    for ln in fingerprint.split("\0", 1)[0].splitlines():
        ruta = ln[3:].strip() if len(ln) > 3 else ln.strip()
        if "->" in ruta:  # rename 'old -> new': nos quedamos con el destino
            ruta = ruta.split("->", 1)[1].strip()
        if ruta:
            rutas.append(ruta)
    return rutas


def _nota_editar(veredicto: str, texto: str, despues: str | None = None) -> str:
    """Nota diagnóstica de un paso editar/crear: veredicto + archivos tocados + prosa del executor.

    Da los tres datos para juzgar el paso sin abrir el repo: qué decidió el runner (``veredicto``),
    qué archivos cambiaron en disco (de la huella ``despues``, si se pasa) y qué dijo el executor
    (``texto``). Recorta a ``_NOTE_LIMIT`` para no inflar el plan.
    """
    cabecera = veredicto
    tocados = _archivos_tocados(despues)
    if tocados:
        extra = f" (+{len(tocados) - 6} más)" if len(tocados) > 6 else ""
        cabecera += " — archivos: " + ", ".join(tocados[:6]) + extra
    cuerpo = (texto or "").strip()
    return (f"{cabecera}\n{cuerpo}" if cuerpo else cabecera)[:_NOTE_LIMIT]


# --- Trazabilidad del plan (traza de archivos) + rescate a una rama de entrega ---


def archivos_del_plan(plan: Plan) -> list[str]:
    """Rutas (relativas al repo) que el plan modificó en su worktree: commiteadas + sin commitear.

    Une lo que el worktree tiene SIN commitear (``git status --porcelain -uall``, capta archivos
    nuevos/untracked expandiendo carpetas) con lo ya COMMITEADO en la rama del plan desde que
    divergió de la rama por defecto (``git diff --name-only <base>...HEAD``). Así la traza final
    refleja TODO lo que cambió, se haya commiteado o no. Solo lee el worktree; nunca toca el repo
    real.

    Args:
        plan: el plan ejecutado. Se usan ``plan.workdir`` (worktree) y ``plan.repo`` (para hallar
            la rama base). Si el plan no tiene worktree en disco, devuelve ``[]``.

    Returns:
        Lista ordenada y sin duplicados de rutas relativas (separador ``/``); ``[]`` si no hay
        worktree, no es git, o el plan no modificó nada.
    """
    wt = plan.workdir
    if not wt or not os.path.isdir(wt):
        return []
    porcelain = _git_run(wt, "status", "--porcelain", "-uall")
    tocados = set(_archivos_tocados((porcelain or "") + "\0"))  # sin commitear (incluye untracked)
    base = default_branch(plan.repo) if plan.repo else ""
    if base:
        # tres puntos: los cambios del lado de HEAD desde el merge-base (los commits del plan)
        committed = _git_run(wt, "diff", "--name-only", f"{base}...HEAD")
        if committed:
            tocados.update(ln.strip() for ln in committed.splitlines() if ln.strip())
    return sorted(tocados)


def arbol_archivos(rutas: list[str]) -> str:
    """Dibuja una lista de rutas como un árbol ASCII (carpetas agrupadas, archivos como hojas).

    Función PURA (no toca disco): recibe rutas relativas con ``/`` como separador y devuelve un
    árbol con conectores ``├─``/``└─``. Las carpetas se listan antes que los archivos y todo va en
    orden alfabético dentro de cada nivel. Se usa para la traza de archivos modificados al terminar
    un plan (trazabilidad del worktree sin tener que rastrearlo a mano).

    Args:
        rutas: rutas relativas (``/`` o ``\\`` como separador; se normaliza a ``/``).

    Returns:
        El árbol como texto multilínea, o cadena vacía si ``rutas`` está vacía.
    """
    if not rutas:
        return ""
    raiz: dict = {}  # nombre -> subdict (carpeta) | None (archivo)
    for ruta in sorted(set(rutas)):
        partes = [p for p in ruta.replace("\\", "/").split("/") if p]
        nodo = raiz
        for i, parte in enumerate(partes):
            if i == len(partes) - 1:
                nodo.setdefault(parte, None)  # hoja: archivo
            else:
                sub = nodo.get(parte)
                if not isinstance(sub, dict):
                    sub = {}
                    nodo[parte] = sub
                nodo = sub
    lineas: list[str] = []

    def _render(nodo: dict, prefijo: str) -> None:
        # carpetas (valor dict) primero, archivos (None) después; alfabético dentro de cada grupo
        items = sorted(nodo.items(), key=lambda kv: (kv[1] is None, kv[0]))
        for idx, (nombre, sub) in enumerate(items):
            ultimo = idx == len(items) - 1
            conector = "└─ " if ultimo else "├─ "
            sufijo = "/" if isinstance(sub, dict) else ""
            lineas.append(f"{prefijo}{conector}{nombre}{sufijo}")
            if isinstance(sub, dict):
                _render(sub, prefijo + ("   " if ultimo else "│  "))

    _render(raiz, "")
    return "\n".join(lineas)


def default_branch(repo: str) -> str:
    """Rama por defecto del repo, para basar en ella la rama de entrega. '' si no se determina.

    Orden de resolución: (1) el HEAD del remoto (``origin/HEAD`` -> su símbolo, sin prefijo
    ``origin/``); (2) la primera de ``config.BASE_BRANCH_CANDIDATES`` que exista como rama
    local -- las protegidas (``main``/``master`` mas las que declare la config) y luego
    ``develop``; (3) la rama actualmente activa. Devuelve SIEMPRE un nombre de rama LOCAL
    usable (nunca ``origin/...``).

    Args:
        repo: raíz del repositorio git.

    Returns:
        El nombre de la rama por defecto, o ``''`` si el repo no es git / git no está disponible.
    """
    sym = _git_run(repo, "symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")
    if sym:
        nombre = sym.strip().rsplit("/", 1)[-1]
        if nombre and _git_run(repo, "rev-parse", "--verify", "--quiet", nombre) is not None:
            return nombre
    for cand in config.BASE_BRANCH_CANDIDATES:
        if _git_run(repo, "rev-parse", "--verify", "--quiet", cand) is not None:
            return cand
    cur = _git_run(repo, "rev-parse", "--abbrev-ref", "HEAD")
    return cur.strip() if cur else ""


@dataclass
class Traspaso:
    """Resultado de :func:`traspasar_a_rama`: si se pudo mover el trabajo y a qué rama.

    ``ok`` True -> el trabajo quedó en ``rama`` (basada en ``base``); False -> ``motivo`` explica
    por qué no (y el trabajo sigue seguro en la rama del worktree del plan).
    """

    ok: bool
    rama: str = ""  # rama de entrega creada (solo si ok)
    base: str = ""  # rama por defecto usada como base
    archivos: list[str] = field(default_factory=list)  # archivos traspasados
    motivo: str = ""  # por qué falló ('' si ok)


def _git_try(cwd: str, *args: str, stdin: str | None = None) -> tuple[bool, str]:
    """Corre ``git <args>`` en ``cwd`` y devuelve ``(ok, stdout+stderr)``. Nunca lanza.

    A diferencia de :func:`_git_run` (que descarta el stderr), preserva la salida de error para
    diagnosticar por qué falló un commit/apply/worktree. ``stdin`` alimenta el comando (p.ej. un
    patch para ``git apply -``).
    """
    try:
        r = subprocess.run(
            ["git", *args],
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def _slug_objetivo(objetivo: str, limite: int = 32) -> str:
    """Slug corto y seguro para nombrar la rama de entrega a partir del objetivo del plan."""
    s = re.sub(r"[^a-z0-9]+", "-", (objetivo or "").lower()).strip("-")
    return s[:limite].rstrip("-") or "plan"


def traspasar_a_rama(plan: Plan, repo: str, *, base: str = "", dest: str = "") -> Traspaso:
    """Traspasa el trabajo del worktree del plan a una rama NUEVA basada en la default del repo.

    Coloca cuidadosamente el delta del plan (todo lo que cambió en su worktree) como UN commit
    limpio sobre la rama por defecto actual, en una rama de entrega nueva, para que puedas
    revisarlo/mergearlo sin rastrear el worktree temporal. Nunca commitea sobre la rama base ni
    sobre ninguna rama protegida: la entrega SIEMPRE va a una rama nueva (``escapement/entrega-...``).

    Flujo: (1) commitea lo que quede sin commitear en la rama del plan (nada se pierde);
    (2) calcula el delta ``merge-base(base, rama_plan)..rama_plan``; (3) crea un worktree efímero
    sobre ``base``, aplica el delta en squash y lo commitea en ``dest``; (4) desmonta el worktree
    efímero. Si el delta no aplica limpio sobre ``base`` (conflicto), no crea la rama y lo reporta:
    el trabajo sigue intacto en la rama del worktree del plan.

    Args:
        plan: el plan ejecutado; se usan ``plan.workdir`` y ``plan.rama``. Sin worktree git activo
            devuelve ``Traspaso(ok=False)``.
        repo: raíz del repositorio git real.
        base: rama base para la entrega. ``''`` (default) -> :func:`default_branch`.
        dest: nombre de la rama de entrega. ``''`` (default) -> ``escapement/entrega-<slug>-<stamp>``.

    Returns:
        Un :class:`Traspaso` con el resultado (rama creada o motivo del fallo).
    """
    wt = plan.workdir
    if not wt or not os.path.isdir(wt) or not plan.rama:
        return Traspaso(False, motivo="el plan no tiene un worktree git activo")
    status = _git_run(wt, "status", "--porcelain")
    if status is None:
        return Traspaso(False, motivo="el worktree del plan no es un repo git")
    if status.strip():  # asegura que nada quede sin commitear en la rama del plan
        _git_try(wt, "add", "-A", "--", ".", ":(exclude)**/__pycache__/**", ":(exclude)*.py[co]")
        ok, out = _git_try(wt, "commit", "-m", f"escapement(wip): {plan.objetivo[:60]}")
        if not ok and "nothing to commit" not in out:
            return Traspaso(False, motivo=f"no pude commitear el avance del worktree: {out[:200]}")
    base = base or default_branch(repo)
    if not base:
        return Traspaso(False, motivo="no pude determinar la rama por defecto del repo")
    mb = _git_run(repo, "merge-base", base, plan.rama)
    mergebase = mb.strip() if mb else base
    patch = _git_run(repo, "diff", "--binary", f"{mergebase}..{plan.rama}")
    if not patch or not patch.strip():
        return Traspaso(False, base=base, motivo="no hay cambios que traspasar (delta vacío)")
    archivos = archivos_del_plan(plan)
    if not dest:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = f"escapement/entrega-{_slug_objetivo(plan.objetivo)}-{stamp}"
    if _git_run(repo, "rev-parse", "--verify", "--quiet", dest) is not None:
        return Traspaso(False, base=base, motivo=f"la rama de entrega '{dest}' ya existe")
    tmp = os.path.join(tempfile.mkdtemp(prefix="escapement-entrega-"), "wt")
    ok, out = _git_try(repo, "worktree", "add", "-b", dest, tmp, base)
    if not ok:
        return Traspaso(
            False, base=base, motivo=f"no pude crear el worktree de entrega: {out[:200]}"
        )
    exito = False
    try:
        applied, out = _git_try(tmp, "apply", "--index", "--whitespace=nowarn", "-", stdin=patch)
        if not applied:
            return Traspaso(
                False,
                base=base,
                archivos=archivos,
                motivo=f"el delta no aplica limpio sobre '{base}' (conflicto): {out[:200]}",
            )
        committed, out = _git_try(tmp, "commit", "-m", f"escapement: {plan.objetivo[:72]}")
        if not committed:
            return Traspaso(
                False,
                base=base,
                archivos=archivos,
                motivo=f"no pude commitear la entrega: {out[:200]}",
            )
        exito = True
        return Traspaso(True, rama=dest, base=base, archivos=archivos)
    finally:
        _git_try(repo, "worktree", "remove", "--force", tmp)
        if not exito:  # no dejes una rama de entrega a medias si algo falló
            _git_try(repo, "branch", "-D", dest)


def _dispatch_reason(
    kind: str,
    user: str,
    *,
    validate: Callable[[str], bool],
    cwd: object,
    system: str = "",
    persona: str = "",
    accion: str = "",
    done: str = "",
) -> tuple[bool, str]:
    """Despacha un paso de RAZONAMIENTO/lectura al backend que decida :func:`config.route_step`.

    Si el ruteo elige LOCAL y el LLM local está disponible, intenta resolverlo in-process
    (:func:`agent.local_models.complete`): si responde y ``validate`` acepta la salida, se usa (dispatch
    local, coste 0 frente al budget). Ante local no disponible, sin respuesta, o salida que NO valida,
    cae a :func:`executors.run_agent` (Claude/CLI) con el modelo del tiering —sin pérdida de corrección,
    solo se pierde el ahorro de ese paso—.

    Cuando ``route_step`` devuelve backend ``"cli"`` (persona pesada, dificultad alta, sesión por voz,
    o local no disponible), esto es EXACTAMENTE el ``run_agent`` de antes (mismo prompt, mismo modelo,
    ``mode="read"``): el ruteo local es una optimización transparente, nunca cambia el resultado.

    Args:
        kind: tipo/pseudo-tipo del paso para el ruteo y el tiering (p.ej. ``"reflexionar"``/``"triage"``).
        user: mensaje de usuario/tarea (sin el system).
        validate: predicado sobre la salida local; si es False, se descarta y se cae a Claude.
        cwd: directorio de trabajo del dispatch online (worktree del plan).
        system: system prompt de la tarea; ``""`` = sin system (la instrucción va en ``user``).
        persona: persona del paso (HEAVY fuerza CLI en ``route_step``). ``""`` = sin persona.
        accion, done: texto del paso para la heurística de dificultad de ``route_step``.

    Returns:
        ``(ok, texto)``: ``ok`` True si algún backend produjo salida usable.
    """
    route = config.route_step(kind, persona, accion, done)
    if route.backend == "local":
        if local_models.available():
            # observe=False: el conteo local lo decide ESTE caller y solo si la salida se USA (valida).
            # Así un intento local descartado no infla local_dispatches ni cuenta doble (local + cli).
            out = local_models.complete(user, system=system, observe=False)
            if out and validate(out):
                executors.notify_observer("local", user, out)  # ahorro real: dispatch local usado
                return True, out
            motivo = "sin_respuesta" if not out else "no_valido"
        else:
            motivo = "no_disponible"
        # el local no estaba, no respondió o no validó -> fallback online (sin perder corrección).
        # Se registra el motivo para observar la tasa de fallback local->Claude en el ledger.
        executors.notify_observer("local_fallback", motivo, "")
    prompt = f"{system}\n\n{user}" if system else user
    return executors.run_agent(prompt, cwd=cwd, mode="read", model=route.model)


def _senal_divergencia(out: str) -> str | None:
    """``'DIVERGENCIA'`` o ``'SEGUIR'`` si el marcador aparece en ``out``, o ``None`` si no hay señal.

    Tolera un preámbulo o markdown antes del marcador (``"Claro. DIVERGENCIA: ..."``, ``"**SEGUIR:**"``):
    exige el ``':'`` que el prompt ya pide, lo que evita confundir la palabra suelta en prosa
    (``"no hay divergencia real, SEGUIR: ..."`` → ``'SEGUIR'``, no ``'DIVERGENCIA'``) con el marcador de
    decisión. Si aparecen ambos, gana el primero (ante duda de divergencia, se bloquea y se pregunta).

    Con el ``startswith`` anterior, una salida con preámbulo no validaba (el local caía a Claude sin
    necesidad) y —peor— la de Claude (que no pasa por ``validate``) se leía como 'SEGUIR' aunque
    empezara con un preámbulo antes de ``DIVERGENCIA:``, saltándose un checkpoint humano real.
    """
    match = re.search(r"\b(DIVERGENCIA|SEGUIR)\s*:", (out or "").upper())
    return match.group(1) if match else None


def _h_preguntar(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    # Evalúa si HAY una divergencia real de caminos. Si la hay -> checkpoint (para y consulta;
    # respondes con 'escapement paso <id> hecho "<decisión>"' y reanudas, tu respuesta fluye al resto).
    # Si NO la hay -> resuelve y sigue (autónomo). Honra los 'preguntar' condicionales del planner.
    ok, out = _dispatch_reason(
        "triage",  # evaluar divergencia es clasificación barata (Clase A: razona sobre el texto)
        f"Evalúa si aquí hay una DIVERGENCIA REAL de caminos que exija decisión humana (algo "
        f"irreversible, un borrado masivo, o una ambigüedad de rumbo que cambie el resultado): "
        f"{step.accion}\nSi la hay: responde EMPEZANDO con 'DIVERGENCIA:' y descríbela en una línea. "
        f"Si NO: responde EMPEZANDO con 'SEGUIR:' y explica en una línea cómo procedes."
        + _context(step, plan),
        # Tolera preámbulo antes del marcador: un 'Claro, SEGUIR: ...' del local se acepta en vez de
        # gastar un dispatch a Claude; el marcador con ':' se busca en cualquier parte, no solo al inicio.
        validate=lambda o: _senal_divergencia(o) is not None,
        cwd=_workdir(plan, repo),
        accion=step.accion,
    )
    texto = (out or "").strip()
    # _senal_divergencia (no startswith): un preámbulo antes de 'DIVERGENCIA:' seguiría marcando el
    # checkpoint. El fallback de Claude no pasa por 'validate', así que aquí es donde se decide bloquear.
    if not ok or _senal_divergencia(texto) == "DIVERGENCIA":
        return BLOQUEADO, texto[
            :_NOTE_LIMIT
        ] or f"Divergencia — requiere tu decisión: {step.accion}"
    return HECHO, texto[:_NOTE_LIMIT]  # 'SEGUIR': no había divergencia, el agente procedió solo


def _h_ejecutar(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    # Autónomo: el especialista ejecuta la tarea (Bash/edición) hasta completarla.
    ok, out = executors.run_agent(
        personas.system_for(step.tipo, repo, step.persona)
        + f"Ejecuta esta tarea de forma autónoma hasta completarla: {step.accion}\n"
        f"Criterio de done: {step.done}" + _context(step, plan),
        cwd=_workdir(plan, repo),
        mode="edit",
        model=config.model_for("ejecutar", step.persona),
    )
    if not ok:
        return FALLIDO, _nota_fallo(out, "el executor no completó la tarea")
    texto = (out or "").strip()
    confesion = _confesion_no_completar(texto)
    if confesion:
        return FALLIDO, _nota_editar(
            f'el executor reportó que no completó la tarea (no aplicó el cambio) — confesión: "{confesion}"',
            texto,
        )
    return HECHO, texto[:_NOTE_LIMIT]


def _h_verificar(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    """Verifica el ``done`` del paso SIN poder editar lo que audita (deuda #12).

    Necesita shell —una verificación real corre ``pytest``, ``py_compile``, un script—, así que no
    puede ir por ``mode="read"``. Lo que se cierra es la ESCRITURA, en dos capas: ``no_write=True``
    le quita las tools de edición al dispatch, y como eso no cubre un ``echo > archivo`` por shell,
    se compara la huella de lo tracked antes/después. Si el verificador modificó el árbol, el
    veredicto no vale (en la validación de E2 uno escribió él mismo los docstrings que debía
    revisar y reportó ``verificado``): el paso queda FALLIDO con la evidencia.
    """
    work = _workdir(plan, repo)
    antes = _huella_tracked(work)
    # Autónomo: Claude corre la verificación del 'done' y su ÚLTIMA LÍNEA es el veredicto.
    ok, out = executors.run_agent(
        personas.system_for(step.tipo, repo, step.persona)
        + f"Verifica de forma autónoma que se cumple: {step.done}. Corre lo necesario y que tu ÚLTIMA "
        f"LÍNEA sea exactamente VERIFICADO (si pasa) o FALLO seguido del motivo (si no). "
        f"NO edites ningún archivo: solo observa y reporta."
        + _context(step, plan),
        cwd=work,
        mode="edit",
        no_write=True,
        model=config.model_for("verificar", step.persona),
    )
    if not ok:
        return FALLIDO, _nota_fallo(out, "no se pudo verificar")
    despues = _huella_tracked(work)
    if antes is not None and despues is not None and antes != despues:
        tocados = ", ".join(_archivos_tocados(despues)[:6]) or "(sin detalle)"
        return FALLIDO, (
            "veredicto descartado: el verificador MODIFICÓ el árbol que auditaba "
            f"(archivos sucios ahora: {tocados}). Revisa el cambio antes de dar el paso por hecho."
            f"\n{(out or '').strip()}"
        )[:_NOTE_LIMIT]
    # Miramos SOLO la última línea (el veredicto): así menciones de "fallo(s)" en el análisis no
    # tumban un verificar que en realidad pasó.
    lineas = [ln.strip() for ln in (out or "").strip().splitlines() if ln.strip()]
    ultima = (lineas[-1] if lineas else "").upper()
    if ultima.startswith("VERIFICADO"):
        return HECHO, "verificado"
    return FALLIDO, (out or "").strip()[-200:]


def _h_investigar(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    ok, out = executors.run_agent(
        personas.system_for(step.tipo, repo, step.persona) + step.accion + _context(step, plan),
        cwd=_workdir(plan, repo),  # ve las ediciones de pasos previos (continuidad del plan)
        mode="read",
        model=config.model_for("investigar", step.persona),
    )
    return (
        (HECHO, (out or "").strip()[:_NOTE_LIMIT]) if ok else (FALLIDO, "el executor no respondió")
    )


def _h_editar(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    target = _target(step)
    if target:
        from agent.orchestrator import optimize

        res = optimize(repo, step.accion + _context(step, plan), target)
        return (
            (HECHO, f"PR: {res.pr_url or res.verdict}") if res.verified else (FALLIDO, res.verdict)
        )
    # Sin archivo concreto: en modo autónomo el especialista desglosa y edita los que apliquen.
    # Este carril no pasa por el verify de optimize, así que comprobamos el EFECTO EN DISCO
    # nosotros: una edición que no cambia el árbol de trabajo es un falso 'hecho', diga lo que
    # diga el executor. Edita en el workdir del plan (S2), no en el repo real; y sin shell (S3):
    # editar archivos no requiere correr comandos, y la acción puede venir de input no confiable.
    work = _workdir(plan, repo)
    antes = _git_status(work)
    ok, out = executors.run_agent(
        personas.system_for(step.tipo, repo, step.persona)
        + f"Realiza autónomamente esta edición sobre todos los archivos que apliquen en el repo: "
        f"{step.accion}\nCriterio de done: {step.done}" + _context(step, plan),
        cwd=work,
        mode="edit",
        no_shell=True,
        model=config.model_for(step.tipo, step.persona),  # editar/crear
    )
    if not ok:
        return FALLIDO, _nota_fallo(out, "no se completó la edición")
    texto = (out or "").strip()
    confesion = _confesion_no_completar(texto)
    if confesion:
        return FALLIDO, _nota_editar(
            f'el executor no persistió el cambio (permiso/denegado) — confesión: "{confesion}"',
            texto,
        )
    despues = _git_status(work)
    if antes is not None and despues is not None and despues == antes:
        # Sin efecto en disco. NO siempre es un fallo: un paso 'editar' de AUDITORÍA sobre código que
        # ya cumple el criterio no deja diff, legítimamente. Distinguimos por la conclusión del executor:
        if _sin_cambio_necesario(texto):
            return HECHO, _nota_editar(
                "auditoría sin cambios: el repo ya cumple el criterio (sin diff, esperado)", texto
            )
        # Ni confesó un fallo ni justificó el no-cambio: ambiguo -> falso 'hecho'. FALLIDO, pero con la
        # prosa del executor en la nota para diagnosticar por qué no tocó nada (antes era un genérico).
        return FALLIDO, _nota_editar(
            "sin efecto en disco: la edición no cambió ningún archivo y el executor no lo justificó",
            texto,
        )
    return HECHO, _nota_editar("edición aplicada", texto, despues=despues)


# --- Memoria: Claude analiza el repo y GENERA las notas (JSON); Escapement las ESCRIBE con sus
#     funciones (formato/índice garantizados). Cierra el hueco de Fase B para tareas de memoria. ---
MEMORY_SYSTEM = (
    "Eres un archivista de conocimiento. Analizas un repositorio y produces NOTAS de memoria "
    "una-idea-por-nota (arquitectura, flujos, contratos, gotchas), precisas y sin relleno. "
    "Escribes CONOCIMIENTO DE REFERENCIA, no crónica: qué ES una pieza, cómo se USA y qué HACE. "
    "Nunca 'antes era X', 'se migró a Y' ni 'se implementó Z': eso es historia, y la historia "
    "ya vive en git. Y conectas: una nota suelta no se encuentra."
)

MEMORY_PROMPT = """Tarea de memoria: {accion}
Repositorio a analizar: {repo}
Carpeta del vault (FIJA para todas las notas): {project}

Explora el repo (solo lectura) y decide qué notas crear, complementar, archivar o borrar. Responde
SOLO con un JSON en una línea, sin texto alrededor:
{{"notas": [
  {{"action": "create|update|delete|archive", "project": "{project}", "name": "<slug-kebab a-z0-9->",
    "title": "<título corto>", "type": "project|reference|feedback|user",
    "description": "<una línea que resuma la nota>", "body": "<markdown; en update, el cuerpo COMPLETO ya fusionado>"}}
]}}
Reglas:
- Cada nota = UNA idea. Para 'update' devuelve el body COMPLETO (lo previo + lo nuevo), no solo lo añadido.
- ANTES de escribir, LEE lo que ya existe: usa list_memory sobre "{project}" y read_memory/recall_memory
  sobre lo que suene parecido. Si el tema ya tiene nota, es 'update', no 'create'. Un casi-duplicado
  ensucia el vault más de lo que aporta.
- REFERENCIA, NO CRÓNICA. La nota responde qué es, cómo se usa, qué hace, con qué habla y qué rompe.
  Prohibido narrar el proceso: nada de "se mejoró", "antes era", "se decidió migrar". Si una frase
  solo tiene sentido para quien vivió el cambio, sobra.
- ENLAZA. Dentro del body, referencia otras notas con wikilinks [[nombre-de-la-nota]] (el nombre es el
  slug del archivo sin .md, tal como lo lista list_memory). Toda nota nueva debería nacer con al menos
  un wikilink a la nota del subsistema que toca.
  Los wikilinks resuelven en TODO el vault, no solo dentro de "{project}": el recall busca por nombre
  de nota en todos los proyectos. Enlazar a una nota de OTRO repo es válido y es lo que MÁS falta —
  una costura entre dos repos no vive entera en ninguno de los dos. Los destinos de los otros repos
  están más abajo, en OTROS REPOS DEL VAULT.
  Pero: UNA ARISTA FALSA ES PEOR QUE NINGUNA. Enlaza solo si puedes nombrar el símbolo, archivo o
  endpoint concreto que ambas notas comparten. Si no lo hay, deja la nota sin enlaces.
  Y solo a notas que EXISTEN: un wikilink a un nombre inventado rompe el validador del vault.
  Al hacer 'update', NUNCA borres un wikilink que ya estaba solo porque su destino no aparezca en el
  inventario de "{project}": casi siempre apunta a otro proyecto del vault y resuelve perfectamente.
  Quitar una arista buena cuesta más que no haberla escrito.
- Las notas de type 'feedback' y 'project' llevan, después del texto, dos líneas obligatorias:
  "**Por qué:** <la razón>" y "**Cómo aplicarlo:** <la acción concreta>". Sin ellas la nota es inválida.
- Cita el código con archivo:línea (p.ej. `assign.py:27`). Una afirmación sin ancla no se puede verificar.
- 'archive' saca del contexto principal (reversible) las notas de casos particulares que ya no son contexto del repo; usa 'archive' en vez de 'delete' si la nota puede tener algún valor. NUNCA archives/borres notas 'feedback' (son reglas de trabajo del usuario).
- El destino del archivado lo GESTIONA el sistema (mueve la nota a un histórico y actualiza el índice, creando la carpeta si hace falta). NO crees carpetas, NO inventes rutas como 'vault-historico', NO uses el body para archivar: para archivar basta listar la nota con action 'archive' (name + project).
- 'project' SIEMPRE es exactamente "{project}" (la carpeta ya está fijada); no la cambies ni inventes otra. No inventes datos: básate en el repo.
"""


def _parse_memoria(out: str) -> list[dict]:
    """Extrae la lista de notas del JSON que devuelve el archivista (robusto a texto alrededor)."""
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    return [n for n in data.get("notas", []) if isinstance(n, dict) and n.get("name")]


def _aplicar_notas(notas: list[dict], *, projects_dir=None, project: str | None = None) -> int:
    """Escribe/actualiza/borra cada nota con las funciones de ``memory`` (formato e índice OK).

    Args:
        projects_dir: raíz del vault (inyectable en tests); por defecto ``config.PROJECTS_DIR``.
        project: si se indica, FUERZA la carpeta del vault de todas las notas (ignora el ``project``
            que adivinó el archivista). Evita que un paso de memoria disperse notas a carpetas
            distintas —el bug de scatter—: para una tarea acotada a un repo, todas van a su vault.

    Best-effort: una nota malformada se salta. Devuelve cuántas se aplicaron.
    """
    from agent.tools import memory

    aplicadas = 0
    for nota in notas:
        try:
            proj = project or nota["project"]
            name = nota["name"]
            action = nota.get("action")
            if action == "delete":
                memory.delete_note(proj, name, projects_dir=projects_dir)
            elif action == "archive":
                memory.archive_note(proj, name, projects_dir=projects_dir)
            else:
                path = memory.resolve_note_path(proj, name, projects_dir=projects_dir)
                fsutil.write_text_atomic(
                    path,
                    memory.build_note(
                        name,
                        nota.get("description", ""),
                        nota.get("type", "reference"),
                        nota.get("body", ""),
                    ),
                )
                memory.upsert_index(
                    path.parent, nota.get("title") or name, name, nota.get("description", "")
                )
            aplicadas += 1
        except Exception:  # noqa: BLE001 - una nota mala no aborta el resto
            continue
    return aplicadas


def _inventario_vault(proj: str, tope: int = 120) -> str:
    """Inventario del vault del proyecto, para INYECTARLO en el prompt del archivista.

    El archivista se despacha como ``claude -p`` en modo read: es un subproceso corto
    que NO tiene los tools MCP de memoria del server in-process. Sin esto escribe a
    ciegas y produce casi-duplicados de notas que ya existen. Calcularlo aquí es
    determinista: no depende de que el subagente acierte a llamar una herramienta.
    """
    from agent.tools import memory  # import local: igual que en _aplicar_notas

    try:
        notas = memory.list_notes(proj)
    except Exception:  # noqa: BLE001 - un vault ilegible no debe tumbar el paso
        return "(inventario no disponible)"
    if not notas:
        return "(vault vacío: todas las notas serán 'create')"
    filas = [
        f"- {n.get('name', '?')} [{n.get('type', '?')}] — {n.get('description', '')}"
        for n in notas[:tope]
    ]
    if len(notas) > tope:
        filas.append(f"- ... y {len(notas) - tope} más")
    return "\n".join(filas)


def _inventario_vecinos(proj: str, tope_por_repo: int = 80) -> str:
    """Nombres de las notas de los OTROS proyectos del vault, para poder enlazar CRUZANDO repo.

    Un wikilink resuelve contra TODO el vault (``PROJECTS_DIR/*/memory``), no solo contra la
    carpeta del proyecto: asi lo buscan tanto el recall como el validador. Sin esta lista el
    archivista solo ve su propio proyecto y, obedeciendo al pie de la letra la regla de "solo a
    notas que existen", BORRA en cada 'update' los wikilinks cruzados que ya estaban, creyendolos
    rotos. Son justo los que mas valen: un flujo que cruza repos no vive entero en ninguno.

    Solo nombres, sin descripcion: basta para escribir un wikilink valido y el detalle se lee con
    read_memory. Quedan fuera los vaults-historico ``<slug>-archivo``, apartados del contexto
    principal a proposito (ver :func:`agent.tools.memory.archive_note`).
    """
    from agent.tools import memory  # import local: igual que en _inventario_vault

    try:
        todas = memory.list_notes()
    except Exception:  # noqa: BLE001 - un vault ilegible no debe tumbar el paso
        return ""
    etiqueta = {config.vault_slug(ruta): nombre for nombre, ruta in config.REPOS.items()}
    por_proyecto: dict[str, list[str]] = {}
    for nota in todas:
        slug = nota.get("project", "")
        if not slug or slug == proj or slug.endswith("-archivo"):
            continue
        if nota.get("name"):
            por_proyecto.setdefault(slug, []).append(nota["name"])
    bloques = []
    for slug in sorted(por_proyecto):
        nombres = por_proyecto[slug]
        visibles = nombres[:tope_por_repo]
        resto = len(nombres) - len(visibles)
        cola = f" (+{resto} mas)" if resto > 0 else ""
        bloques.append(f"- {etiqueta.get(slug, slug)}: " + ", ".join(visibles) + cola)
    return "\n".join(bloques)


def _h_memoria(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    proj = config.vault_slug(
        repo
    )  # carpeta canónica del vault: todas las notas van AQUÍ (no scatter)
    prompt = f"{MEMORY_SYSTEM}\n\n" + MEMORY_PROMPT.format(
        accion=step.accion, repo=repo, project=proj
    )
    prompt += (
        f"\nNOTAS QUE YA EXISTEN en {proj} (nombre [tipo] — descripción). Es la lista contra la\n"
        "que decides 'create' vs 'update', y los destinos de wikilink de tu propio proyecto:\n"
        f"{_inventario_vault(proj)}\n"
        f"\nViven en {config.PROJECTS_DIR / proj / 'memory'} (una nota = <nombre>.md). Si vas a\n"
        "hacer 'update', LEE el archivo primero y devuelve el body completo ya fusionado: el\n"
        "body que mandes REEMPLAZA la nota entera, no se le anexa.\n"
    )
    vecinos = _inventario_vecinos(proj)
    if vecinos:
        prompt += (
            "\nOTROS REPOS DEL VAULT (solo nombres de nota). Un [[wikilink]] a cualquiera de estos\n"
            "resuelve igual que uno local: el recall y el validador buscan por nombre de nota en\n"
            f"TODO el vault. NO son candidatos a 'update' —este paso solo escribe en {proj}—: son\n"
            "DESTINOS de enlace, y son los que cosen los flujos que cruzan repos. Para leer una,\n"
            f"read_memory con su project.\n{vecinos}\n"
        )
    ok, out = executors.run_agent(
        prompt + _context(step, plan), cwd=repo, mode="read", model=config.model_for("memoria")
    )
    if not ok:
        return FALLIDO, "el executor no respondió"
    notas = _parse_memoria(out)
    if not notas:
        # 0 notas = no había nada que crear/complementar/borrar en este paso: es éxito, no checkpoint.
        return HECHO, "sin cambios (no había notas que aplicar)"
    n = _aplicar_notas(notas, project=proj)
    return (HECHO, f"{n} nota(s) aplicada(s)") if n else (FALLIDO, "ninguna nota se pudo escribir")


# --- Auto-evolución (Fase C): un paso 'reflexionar' revisa lo YA descubierto y AÑADE al plan, en
#     caliente, nuevas mejoras a aplicar o problemas a manejar que no estaban previstos. Es lo que
#     convierte el plan de estático a vivo: el agente aprovecha lo que encuentra sobre la marcha. ---
REFLEXION_SYSTEM = (
    "Eres un revisor que evoluciona el plan sobre la marcha. Dado el objetivo y lo YA descubierto y "
    "hecho, detectas MEJORAS aplicables o PROBLEMAS accionables —surgidos durante la ejecución— que "
    "aún NO están en el plan y valen la pena. Solo propones lo que aporta valor real y es alcanzable; "
    "si no hay nada, no inventas."
)

REFLEXION_PROMPT = """Objetivo: {objetivo}

Pasos ya completados y lo que revelaron:
{hechos}

¿Qué MEJORAS aplicar o PROBLEMAS manejar, descubiertos durante la ejecución, NO están ya en el plan
y vale la pena añadir? Responde SOLO con un JSON en una línea, sin texto alrededor:
{{"pasos": [{{"accion": "qué hacer", "tipo": "investigar|editar|ejecutar|verificar|memoria",
  "persona": "experto de la lista o vacío", "done": "criterio de done verificable"}}]}}
Si no hay nada que añadir, responde {{"pasos": []}}. No dupliques pasos existentes. Máximo 4 pasos.
Asigna 'persona' (especialista de la lista) cuando una especialidad domina el paso — investigar/editar/crear/ejecutar/verificar; gana sobre el coder de dominio. Vacía si basta el experto por defecto o en memoria.
Especialistas: {roster}
"""


def _parse_pasos(out: str) -> list[dict]:
    """Extrae los pasos nuevos del JSON de una reflexión (robusto a texto alrededor)."""
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    return [p for p in data.get("pasos", []) if isinstance(p, dict) and p.get("accion")]


def _json_tiene_lista(out: str, clave: str) -> bool:
    """``True`` si ``out`` contiene un objeto JSON con ``clave`` mapeada a una lista (aunque vacía).

    Valida ESTRUCTURA, no contenido: distingue una respuesta local bien formada pero vacía
    (``{"pasos": []}`` = "no hay nada que añadir", legítima según el prompt) de basura/no-JSON. Con
    el validador de contenido (``bool(_parse_pasos(o))``) el vacío válido no pasaba y disparaba un
    fallback a Claude para producir la MISMA salida; con este el vacío del local se acepta.
    """
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return False
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and isinstance(data.get(clave), list)


def _insertar_pasos(plan: Plan, tras: Step, nuevos: list[dict]) -> int:
    """Añade pasos nuevos al plan con ids únicos, dependientes de ``tras`` (corren después de él).

    Descarta pasos 'reflexionar' generados (evita el bucle de re-planning infinito).
    """
    max_id = max((s.id for s in plan.pasos), default=0)
    anadidos = 0
    for raw in nuevos:
        tipo = str(raw.get("tipo", "investigar")).strip().lower()
        if tipo == "reflexionar":
            continue
        accion = str(raw.get("accion", "")).strip()
        done = str(raw.get("done", "")).strip()
        # F4: la auto-evolución vuelve a proponer pasos que ya existen en el plan (una reflexión
        # posterior redescubre lo mismo). Descartamos los que se solapan fuerte con un paso previo
        # para no re-ejecutar trabajo ya hecho ni inflar el roadmap con duplicados.
        if _es_duplicado(accion, done, plan):
            continue
        max_id += 1
        plan.pasos.append(
            Step(
                id=max_id,
                accion=accion,
                tipo=tipo,
                done=done,
                depende_de=[tras.id],
                persona=str(raw.get("persona", "")).strip().lower(),
            )
        )
        anadidos += 1
    return anadidos


def _h_reflexionar(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    hechos = "\n".join(
        f"- [{s.tipo}] {s.accion}: {(s.nota or '').strip()[:200]}"
        for s in plan.pasos
        if s.estado == HECHO
    )
    ok, out = _dispatch_reason(
        "reflexionar",  # Clase A: razona sobre los hechos ya en el prompt, no explora el repo
        REFLEXION_PROMPT.format(
            objetivo=plan.objetivo, hechos=hechos, roster=personas.roster_brief()
        ),
        system=REFLEXION_SYSTEM,
        # Estructura, no contenido: un {"pasos": []} legítimo del local se acepta (misma salida que
        # el fallback habría producido) en vez de gastar un dispatch a Claude para el mismo vacío.
        validate=lambda o: _json_tiene_lista(o, "pasos"),
        cwd=_workdir(plan, repo),
        # Señal de dificultad para route_step: el objetivo global (estable). Los 'hechos' no sirven
        # —su tamaño siempre marcaría 'turno largo'—; el objetivo refleja si el replanning es duro.
        accion=plan.objetivo,
    )
    if not ok:
        return HECHO, "reflexión sin cambios"
    nuevos = _parse_pasos(out)
    if not nuevos:
        return HECHO, "sin nuevas mejoras ni problemas que añadir"
    anadidos = _insertar_pasos(plan, step, nuevos)
    resumen = "; ".join(str(p.get("accion", ""))[:50] for p in nuevos[:anadidos])
    return HECHO, f"auto-evolución: +{anadidos} paso(s) → {resumen}"


# --- Swarm (R4): fan-out nativo. Un paso 'swarm' descompone una investigación amplia en subtareas
#     INDEPENDIENTES y las corre EN PARALELO (cada una en su propio dispatch read-only), luego agrega
#     los hallazgos. Es el patrón multi-modal-sweep: varios ángulos a la vez, ninguno ve a los otros.
#     Read-only a propósito: escrituras paralelas sobre el MISMO worktree se pisarían —por eso el
#     fan-out que edita no se hace aquí—. La concurrencia la acota config.SWARM_MAX_WORKERS. ---
SWARM_SYSTEM = (
    "Eres un coordinador de swarm. Descompones una tarea de investigación/análisis en subtareas "
    "INDEPENDIENTES resolubles EN PARALELO: cada una cubre un ángulo, subsistema o dimensión "
    "distinta y no depende de las demás. Solo subtareas de SOLO LECTURA (explorar/analizar, no editar)."
)

SWARM_PROMPT = """Tarea a paralelizar: {accion}
Criterio de done: {done}

Descompón en subtareas INDEPENDIENTES (cada una la resolverá un agente aparte, en paralelo, sin ver
a las otras). Responde SOLO con un JSON en una línea, sin texto alrededor:
{{"subtareas": ["subtarea 1 autocontenida", "subtarea 2 autocontenida"]}}
Reglas: máximo {max} subtareas; cada una AUTOCONTENIDA (incluye el contexto que necesita) y de SOLO
LECTURA. Si la tarea no se puede paralelizar de forma útil, devuelve una sola subtarea.
"""


def _parse_subtareas(out: str) -> list[str]:
    """Extrae la lista de subtareas del JSON del coordinador (robusto a texto alrededor)."""
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return []
    return [s.strip() for s in data.get("subtareas", []) if isinstance(s, str) and s.strip()]


def _h_swarm(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    work = _workdir(plan, repo)
    # 1) Descompón (read, barato). Si no se puede, una sola rama = un 'investigar' normal.
    #    La descomposición es Clase A (razona sobre el texto de la tarea, no explora el repo) -> se
    #    rutea como 'triage' y puede caer en el local; las RAMAS (paso 2) siguen siempre online (Fase 2).
    ok, out = _dispatch_reason(
        "triage",  # descomponer en subtareas es clasificación barata
        SWARM_PROMPT.format(accion=step.accion, done=step.done, max=config.SWARM_MAX_TASKS)
        + _context(step, plan),
        system=SWARM_SYSTEM,
        validate=lambda o: bool(_parse_subtareas(o)),
        cwd=work,
        accion=step.accion,
        done=step.done,
    )
    subtareas = (_parse_subtareas(out) if ok else []) or [step.accion]
    subtareas = subtareas[: config.SWARM_MAX_TASKS]
    # cada rama investiga con el especialista asignado al swarm (o el default de investigación)
    sistema = personas.system_for("investigar", repo, step.persona)
    rama_model = config.model_for("investigar", step.persona)
    # 2) Fan-out en paralelo. run_agent es I/O-bound (subprocess), así que los hilos dan
    #    paralelismo real; el modo read-only evita el choque de escrituras sobre el worktree.
    workers = min(len(subtareas), max(1, config.SWARM_MAX_WORKERS))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futuros = [
            pool.submit(executors.run_agent, sistema + s, cwd=work, mode="read", model=rama_model)
            for s in subtareas
        ]
        resultados = [(subtareas[i], *futuros[i].result()) for i in range(len(subtareas))]
    # 3) Agrega los hallazgos en orden de submit (determinista). Best-effort: las ramas que
    #    fallaron se omiten; solo si NINGUNA respondió el paso queda fallido.
    oks = [(s, (txt or "").strip()) for s, ok_, txt in resultados if ok_]
    if not oks:
        return FALLIDO, "swarm: ninguna subtarea respondió"
    cuerpo = "\n\n".join(f"[{i}] {s}\n{txt}" for i, (s, txt) in enumerate(oks, 1))
    return HECHO, f"swarm {len(oks)}/{len(subtareas)} OK\n\n{cuerpo}"[:_NOTE_LIMIT]


# --- Evaluación global al cerrar (E2, deuda #7): el conteo de pasos no es el done real. Cuando el
#     roadmap se agota entero en 'hecho', un dispatch READ-ONLY contrasta el resultado contra
#     `criterio_global` y emite veredicto: 'done' (criterio cumplido) o 'incompleto' + gaps concretos.
#     Los gaps quedan persistidos en el plan: son el insumo de la replanificación (siguiente pieza
#     de E2). Best-effort deliberado: sin salida usable, el cierre queda como antes (por conteo). ---
EVAL_SYSTEM = (
    "Eres un evaluador de resultados. Contrastas lo LOGRADO por un plan ya ejecutado contra su "
    "criterio de done global y emites un veredicto honesto. SOLO LECTURA: puedes explorar el repo "
    "para comprobar el estado real, nunca editar."
)

EVAL_PROMPT = """OBJETIVO: {objetivo}
CRITERIO DE DONE GLOBAL: {criterio}

Pasos ejecutados y lo que reportaron:
{hechos}

¿Se cumple el CRITERIO DE DONE GLOBAL? Compruébalo contra el estado real del repo cuando puedas
(no te fíes solo de lo que los pasos dicen haber hecho). Responde SOLO con un JSON en una línea,
sin texto alrededor:
{{"veredicto": "done|incompleto", "gaps": ["qué falta, concreto y accionable"]}}
Si el criterio se cumple: veredicto "done" y gaps []. Si no: "incompleto", con cada gap redactado
como una tarea concreta que al completarse cerraría la brecha. Máximo {max_gaps} gaps.
"""

# Tope de gaps que se conservan del veredicto: acota el insumo de la replanificación (mismo espíritu
# que el "máximo 4 pasos" de la auto-evolución de `reflexionar`).
_EVAL_MAX_GAPS = 4


def _parse_eval(out: str) -> tuple[str, list[str]]:
    """Extrae ``(veredicto, gaps)`` del JSON del evaluador (robusto a texto alrededor).

    Un veredicto que no sea exactamente ``done``/``incompleto`` (o un JSON roto) devuelve
    ``("", [])``: sin veredicto usable, el caller no castiga el cierre.
    """
    match = re.search(r"\{.*\}", out or "", re.DOTALL)
    if not match:
        return "", []
    try:
        data = json.loads(match.group(0))
    except (ValueError, TypeError):
        return "", []
    if not isinstance(data, dict):
        return "", []
    veredicto = str(data.get("veredicto", "")).strip().lower()
    if veredicto not in ("done", "incompleto"):
        return "", []
    crudos = data.get("gaps")
    gaps = (
        [g.strip() for g in crudos if isinstance(g, str) and g.strip()]
        if isinstance(crudos, list)
        else []
    )
    return veredicto, gaps


def _evaluar_plan(plan: Plan, repo: str) -> tuple[str, list[str]]:
    """Contrasta el resultado del plan contra ``criterio_global`` y devuelve ``(veredicto, gaps)``.

    Dispatch de razonamiento read-only vía :func:`_dispatch_reason` (pseudo-tipo ``"evaluar"``,
    Clase B: puede explorar el repo, así que solo cae en el local con ``LOCAL_ORCH_TOOLS``).
    Best-effort: si ningún backend produce un veredicto parseable devuelve ``("", [])`` — el plan
    ya agotado nunca se bloquea por no poderse evaluar.
    """
    hechos = "\n".join(
        f"- [{s.tipo}] {s.accion}: {(s.nota or '').strip()[:200]}"
        for s in plan.pasos
        if s.estado == HECHO
    )
    ok, out = _dispatch_reason(
        "evaluar",
        EVAL_PROMPT.format(
            objetivo=plan.objetivo,
            criterio=plan.criterio_global,
            hechos=hechos,
            max_gaps=_EVAL_MAX_GAPS,
        ),
        system=EVAL_SYSTEM,
        validate=lambda o: _parse_eval(o)[0] != "",
        cwd=_workdir(plan, repo),
        # Señal de dificultad para route_step: el criterio global (estable), no los 'hechos'.
        accion=plan.objetivo,
        done=plan.criterio_global,
    )
    if not ok:
        return "", []
    veredicto, gaps = _parse_eval(out)
    return veredicto, gaps[:_EVAL_MAX_GAPS]


# --- Replanificación automática (E2, pieza 2): con veredicto 'incompleto', los gaps se vuelven
#     pasos nuevos por el mecanismo YA validado de la auto-evolución (`_insertar_pasos`) y el runner
#     sigue ejecutando en la misma corrida. Anti-bucle en dos capas: config.PLAN_REPLAN_MAX acota
#     los CICLOS (contador persistido en el plan) y `_insertar_pasos` descarta 'reflexionar' y
#     duplicados. ---
REPLAN_SYSTEM = (
    "Eres un planificador que cierra brechas. Un plan se ejecutó entero pero su evaluación global "
    "lo declaró incompleto: conviertes cada brecha reportada en los pasos ejecutables que faltan. "
    "SOLO las brechas listadas: no propones trabajo nuevo fuera de ellas."
)

REPLAN_PROMPT = """OBJETIVO: {objetivo}
CRITERIO DE DONE GLOBAL: {criterio}

El plan ya se ejecutó entero, pero la evaluación global lo declaró INCOMPLETO por estas brechas:
{gaps}

Convierte las brechas en los pasos que faltan para cumplir el criterio. Responde SOLO con un JSON
en una línea, sin texto alrededor:
{{"pasos": [{{"accion": "qué hacer", "tipo": "investigar|editar|crear|ejecutar|verificar",
  "persona": "experto de la lista o vacío", "done": "criterio de done verificable"}}]}}
Máximo {max} pasos y SOLO los necesarios para cerrar las brechas listadas; nada extra.
Asigna 'persona' (especialista de la lista) cuando una especialidad domina el paso; vacía si basta
el experto por defecto.
Especialistas: {roster}
"""


def _replanificar(plan: Plan, repo: str) -> int:
    """Convierte los ``eval_gaps`` del plan en pasos nuevos al final del roadmap.

    Args:
        plan: plan ya evaluado ``incompleto``; sus ``eval_gaps`` son el único insumo del prompt.
        repo: raíz del repo del plan (para resolver el cwd del dispatch, como el resto de handlers).

    Returns:
        Cuántos pasos se añadieron realmente (0 = sin salida usable, sin pasos propuestos, o todos
        descartados por las guardas de :func:`_insertar_pasos`); el caller no reintenta.

    Dispatch de razonamiento vía :func:`_dispatch_reason` con kind ``"reflexionar"`` (misma Clase
    A: razona sobre las brechas ya en el prompt, no explora el repo). Los pasos entran por
    :func:`_insertar_pasos` colgando del último paso del plan —ya ``hecho``, así que quedan listos
    de inmediato— y heredan sus guardas anti-bucle (sin ``reflexionar``, sin duplicados).
    """
    if not plan.pasos or not plan.eval_gaps:
        return 0
    gaps = "\n".join(f"- {g}" for g in plan.eval_gaps)
    ok, out = _dispatch_reason(
        "reflexionar",
        REPLAN_PROMPT.format(
            objetivo=plan.objetivo,
            criterio=plan.criterio_global,
            gaps=gaps,
            max=_EVAL_MAX_GAPS,
            roster=personas.roster_brief(),
        ),
        system=REPLAN_SYSTEM,
        # Estructura, no contenido (mismo criterio que reflexionar): un {"pasos": []} legítimo del
        # local se acepta sin gastar el fallback a Claude para producir el mismo vacío.
        validate=lambda o: _json_tiene_lista(o, "pasos"),
        cwd=_workdir(plan, repo),
        # Señal de dificultad para route_step: objetivo y criterio (estables), no los gaps.
        accion=plan.objetivo,
        done=plan.criterio_global,
    )
    if not ok:
        return 0
    nuevos = _parse_pasos(out)[:_EVAL_MAX_GAPS]
    if not nuevos:
        return 0
    tras = max(plan.pasos, key=lambda s: s.id)
    return _insertar_pasos(plan, tras, nuevos)


DEFAULT_HANDLERS: dict[str, Handler] = {
    "preguntar": _h_preguntar,
    "ejecutar": _h_ejecutar,
    "verificar": _h_verificar,
    "investigar": _h_investigar,
    "editar": _h_editar,
    "crear": _h_editar,
    "memoria": _h_memoria,
    "reflexionar": _h_reflexionar,
    "swarm": _h_swarm,
}


def _h_desconocido(step: Step, plan: Plan, repo: str) -> tuple[str, str]:
    return BLOQUEADO, f"Tipo '{step.tipo}' no automatizado; hazlo manual y marca hecho"


def run_plan(
    plan: Plan,
    repo: str,
    *,
    handlers: dict[str, Handler] | None = None,
    on_step: Callable[[Step], None] | None = None,
    on_complete: Callable[[Plan], None] | None = None,
    should_cancel: Callable[[], bool] | None = None,
    persist: bool = True,
    plan_path=None,
) -> Plan:
    """Ejecuta el roadmap hasta el primer checkpoint (bloqueado/fallido) o hasta agotarlo.

    E2 (deuda #7): al agotarse el roadmap con todo ``hecho``, la evaluación global emite veredicto;
    un ``incompleto`` con gaps se replanifica solo (gaps -> pasos nuevos) y la MISMA corrida sigue
    ejecutando, hasta ``config.PLAN_REPLAN_MAX`` ciclos (contador ``plan.replan_ciclos``,
    persistido). Al tope, la corrida termina incompleta con los gaps guardados: checkpoint humano.

    Args:
        repo: raíz del repo sobre el que actúan los pasos (editar/investigar).
        handlers: mapa ``tipo -> handler``; por defecto :data:`DEFAULT_HANDLERS`. Inyectable en tests.
        on_step: callback antes de ejecutar cada paso (feedback en vivo).
        on_complete: hook post-task que se llama con el plan UNA vez, solo si el roadmap se
            completó entero (hay pasos y todos quedaron ``hecho``). Default None = no se llama
            (comportamiento previo intacto). Lo usa el ReasoningBank para capturar la trayectoria
            exitosa. Best-effort: un fallo del hook no aborta la corrida. E2: si la evaluación
            global de esta corrida dicta ``incompleto``, NO se llama (la trayectoria no fue un
            éxito real, aunque el conteo diga completo).
        should_cancel: se consulta ANTES de despachar cada paso; si devuelve True, el paso queda
            ``bloqueado`` (reanudable) y la corrida termina. Es cancelación **cooperativa**: no
            interrumpe el paso ya despachado —un dispatch a un executor no se puede matar—, así
            que el corte llega al terminar el paso en vuelo. Default ``None`` = nunca se cancela
            (comportamiento previo intacto). Lo usa el daemon de voz para que F2 corte también el
            trabajo, no solo el turno. Ver :data:`agent.cancel.RUN`.
        persist: si True (default), guarda el plan tras cada paso -> reanudable. False en tests.
        plan_path: archivo donde persistir (el plan DEDICADO del repo); None = ``config.PLAN``.
            Es lo que hace que cada repo se ejecute y reanude en su propio archivo, sin pisar a otros.
    """
    handlers = handlers or DEFAULT_HANDLERS
    # S2: monta (o reusa) el worktree del plan ANTES de despachar pasos; si se creó recién,
    # persiste workdir/rama de inmediato para que una reanudación retome el mismo worktree.
    if _worktree_plan(plan, repo) and persist:
        planner.save_plan(plan, plan_path)
    # R3: contabiliza el gasto estimado de TODOS los dispatches de esta corrida (proxy por tokens)
    # y, si hay budget configurado, corta antes de despachar el paso que lo excedería. Sin budget
    # (default) el tope nunca se cumple: comportamiento previo intacto.
    with costs.track(plan.objetivo, budget=config.BUDGET_TOKENS_PER_PLAN) as costos:
        # E2: el ciclo ejecutar -> evaluar -> replanificar vive DENTRO del track para que el gasto
        # de evaluación y replanificación también cuente en el budget y en el roll-up del plan.
        veredicto = ""
        while True:
            listos = _ready(plan)
            if not listos:
                # Roadmap agotado. E2 (deuda #7): el conteo no es el done real — con todo 'hecho'
                # y un criterio global presente, la evaluación read-only emite el veredicto y
                # persiste los gaps en el plan.
                if (
                    plan.pasos
                    and all(s.estado == HECHO for s in plan.pasos)
                    and config.PLAN_EVAL
                    and plan.criterio_global.strip()
                ):
                    veredicto, gaps = _evaluar_plan(plan, repo)
                    plan.eval_veredicto, plan.eval_gaps = veredicto, gaps
                    if persist:
                        planner.save_plan(plan, plan_path)
                    # Telemetría (E2 pieza 3): un evento por cierre, SIEMPRE que se evaluó —
                    # incluso con veredicto "" (best-effort), para que esa ruta sea observable.
                    bus.publish(
                        "plan.eval",
                        {"repo": str(repo), "veredicto": veredicto, "gaps": gaps},
                        source="runner",
                    )
                    # E2 pieza 2: un 'incompleto' con gaps se replanifica SOLO, hasta el tope de
                    # ciclos (contador persistido: reanudar no lo resetea). Al alcanzar el tope, o
                    # si no salió ningún paso nuevo, la corrida termina incompleta con los gaps
                    # guardados = checkpoint humano (el CLI los muestra).
                    if (
                        veredicto == "incompleto"
                        and plan.eval_gaps
                        and plan.replan_ciclos < config.PLAN_REPLAN_MAX
                    ):
                        anadidos = _replanificar(plan, repo)
                        if anadidos:
                            plan.replan_ciclos += 1
                            if persist:
                                planner.save_plan(plan, plan_path)
                            bus.publish(
                                "plan.replan",
                                {
                                    "repo": str(repo),
                                    "anadidos": anadidos,
                                    "ciclo": plan.replan_ciclos,
                                    "max": config.PLAN_REPLAN_MAX,
                                },
                                source="runner",
                            )
                            continue  # los pasos nuevos entran al frente y el ciclo sigue
                break
            step = listos[0]
            if should_cancel is not None and should_cancel():
                # El usuario pidió parar (F2 en voz). Se corta ENTRE pasos: lo ya despachado
                # terminó, y el paso que seguía queda 'bloqueado' para poder reanudar sin repetir
                # trabajo (`escapement paso <id> pendiente` y de nuevo 'ejecutar').
                step.estado, step.nota = (
                    BLOQUEADO,
                    "corrida cancelada a tu pedido antes de despachar este paso; "
                    "reanuda con 'ejecutar' cuando quieras",
                )
                if persist:
                    planner.save_plan(plan, plan_path)
                break
            if costos.over_budget():
                # Presupuesto agotado: no despaches más. Marca el paso 'bloqueado' (reanudable:
                # revisas y sigues con 'escapement paso <id> hecho' o subes el budget) y para.
                step.estado, step.nota = (
                    BLOQUEADO,
                    (
                        f"presupuesto del plan agotado (~{costos.total} tokens estimados >= "
                        f"{costos.budget}); revisa y reanuda, o sube [budget] tokens_por_plan"
                    ),
                )
                if persist:
                    planner.save_plan(plan, plan_path)
                break
            if on_step:
                on_step(step)
            # Progreso EN VIVO: `plan.step` (abajo) llega recién cuando el paso termina, y un
            # dispatch tarda minutos. Este evento dice "empecé el paso N" para que la presencia
            # (el ícono de bandeja) no se quede muda toda la corrida. Sin journal: es señal de UI,
            # el hecho durable es `plan.step`.
            bus.publish(
                "plan.step_start",
                {
                    "repo": str(repo),
                    "id": step.id,
                    "tipo": step.tipo,
                    "accion": step.accion,
                    "hechos": sum(1 for s in plan.pasos if s.estado == HECHO),
                    "total": len(plan.pasos),
                },
                source="runner",
                journal=False,
            )
            step.estado, step.nota = handlers.get(step.tipo, _h_desconocido)(step, plan, repo)
            if persist:
                planner.save_plan(plan, plan_path)
            bus.publish(
                "plan.step",
                {
                    "repo": str(repo),
                    "id": step.id,
                    "tipo": step.tipo,
                    "estado": step.estado,
                    "nota": step.nota,
                },
                source="runner",
            )
            if step.estado in (BLOQUEADO, FALLIDO):
                break  # checkpoint: requiere intervención humana antes de seguir
    # Post-task (R2): el roadmap se agotó sin checkpoint y quedó entero en 'hecho' -> éxito. Se
    # recalcula AQUÍ (no se hereda del ciclo): un checkpoint tras una replanificación debe dejar
    # completado=False aunque un ciclo anterior hubiera visto todo 'hecho'.
    completado = bool(plan.pasos) and all(s.estado == HECHO for s in plan.pasos)
    # El veredicto de ESTA corrida (variable local, no el campo persistido) decide si la
    # trayectoria se captura como éxito: un 'incompleto' no debe envenenar el ReasoningBank; y con
    # la evaluación apagada un veredicto viejo pegado en el JSON no cambia el comportamiento previo.
    if on_complete and completado and veredicto != "incompleto":
        try:
            on_complete(plan)
        except Exception:  # noqa: BLE001 - un hook de cierre no debe tumbar la corrida ya exitosa
            pass
    bus.publish(
        "plan.done",
        {"repo": str(repo), "objetivo": plan.objetivo, "completado": completado},
        source="runner",
    )
    return plan
