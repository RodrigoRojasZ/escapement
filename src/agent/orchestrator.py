"""Orquestador de ingeniería (objetivo estrella): despacha tareas a un agente de código.

MVP supervisado: por cada directiva crea una rama feature, despacha el trabajo al executor
configurado (Claude Code por defecto; también Antigravity/Cursor — ver ``executors.py``), corre
los tests para verificar que se preservó el comportamiento, y deja el resultado para revisión
humana (diff local; PR con ``gh`` en repos con remoto). **Nunca mergea.**
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# En Windows `gh` es .CMD/.exe; shutil.which resuelve la extensión.
GH = shutil.which("gh") or "gh"

PROMPT_TEMPLATE = """Abre y edita AHORA, directamente, el/los archivo(s) `{target}` para: {directiva}
(NO explores el resto del repositorio ni leas otros archivos: ve directo al target y edítalo.)

Aplica estas acciones directamente con Edit/Write (no muestres código, no preguntes, hazlo):
1. Añade type hints a parámetros y retornos.
2. Añade o mejora docstrings (con Args/Returns/Raises cuando aplique).
3. Mejora los nombres de variables LOCALES (no de funciones/clases públicas).
4. Usa idioms pythónicos (comprehensions, sum(), enumerate, context managers, etc.) donde encaje.
5. Mejora estructura, eficiencia y legibilidad.

Restricciones DURAS:
- NO cambies la API pública (nombres de funciones/clases/módulos) ni los resultados observables.
- Los tests existentes deben seguir pasando; NO los modifiques.
- NO añadas dependencias.

Cuando los archivos ya estén editados en disco, resume en 3-5 bullets qué cambiaste.
"""


@dataclass
class Result:
    branch: str
    dispatched: bool
    tests_ok: bool | None
    verified: bool
    verdict: str
    diff_stat: str
    summary: str
    pr_url: str | None = None
    log: str = field(default="", repr=False)


def _run(cmd: list[str], cwd: Path, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


# Instrucción de sistema: obliga a EJECUTAR los edits (en modo -p el modelo tiende a
# proponer un refactor abierto en vez de aplicarlo).
SYSTEM_APPEND = (
    "Eres un agente de refactor autónomo en modo no interactivo. SIEMPRE aplicas los cambios "
    "editando los archivos con Edit/Write. NUNCA propones, muestras código ni pides confirmación. "
    "Trabaja SOLO sobre el/los archivo(s) indicados: NO explores ni leas el resto del repositorio; "
    "en un repo grande, ve DIRECTO a abrir y editar el target, sin navegar la estructura. Tu turno "
    "solo termina cuando los archivos ya están editados en disco."
)


def dispatch(
    repo: Path, prompt: str, timeout: float | None = None, allow_secrets: bool = False
) -> tuple[bool, str]:
    """Aplica el refactor en ``repo`` con el executor configurado (Claude por defecto).

    El ``SYSTEM_APPEND`` (aplicar los edits, no proponer) se antepone al prompt en vez de usar un
    flag propietario, para que funcione con cualquier motor. El executor corre con permisos de
    edición en ``repo`` — por eso siempre es un worktree aislado. Configurable: ``AGENT_EXECUTOR``.

    Args:
        timeout: techo de tiempo del dispatch, en segundos. ``None`` (default) = el de
            ``config.EXECUTOR_TIMEOUT`` (env ``AGENT_EXECUTOR_TIMEOUT``); antes acá había un
            literal propio que pisaba en silencio al del executor.
        allow_secrets: se propaga a ``run_agent`` para saltar su pre-flight de secretos
            (False = escanear el prompt antes de despachar; el default seguro).
    """
    from agent import config, executors

    return executors.run_agent(
        f"{SYSTEM_APPEND}\n\n{prompt}",
        cwd=repo,
        timeout=config.EXECUTOR_TIMEOUT if timeout is None else timeout,
        mode="edit",
        allow_secrets=allow_secrets,
        model=config.model_for("editar"),  # refactor autónomo (no-op si el tiering está OFF)
    )


def _dispatch_isolated(
    work: Path, target: str, directiva: str, allow_secrets: bool = False
) -> tuple[bool, str]:
    """Despacha el refactor del target en un dir efímero con SOLO ese archivo.

    En un repo grande el executor gasta el turno EXPLORANDO en vez de editar (validado: el mismo
    módulo en un dir de 1 archivo SÍ se edita; en el worktree de miles de archivos, no). Aísla el
    target, lo despacha y copia el resultado de vuelta al worktree. Trade-off: el executor no ve el
    resto del repo (basta para type hints/docstrings; limitado si el refactor necesita el contexto
    de otros módulos). Si el target no es un archivo único (dir/multi), despacha en el worktree.
    """
    tgt = Path(work) / target
    if not tgt.is_file():
        return dispatch(
            Path(work),
            PROMPT_TEMPLATE.format(directiva=directiva, target=target),
            allow_secrets=allow_secrets,
        )
    iso = Path(tempfile.mkdtemp(prefix="escapement-iso-"))
    try:
        iso_file = iso / Path(target).name
        shutil.copy(tgt, iso_file)
        ok, out = dispatch(
            iso,
            PROMPT_TEMPLATE.format(directiva=directiva, target=Path(target).name),
            allow_secrets=allow_secrets,
        )
        if iso_file.is_file():
            shutil.copy(iso_file, tgt)  # traer el resultado al worktree
        return ok, out
    finally:
        shutil.rmtree(iso, ignore_errors=True)


def run_tests(repo: Path, python_exe: str = "python", timeout: float = 300) -> tuple[bool, str]:
    """Corre pytest en ``repo``. Devuelve (paso, salida)."""
    r = _run([python_exe, "-m", "pytest", "-q"], repo, timeout=timeout)
    return r.returncode == 0, (r.stdout or "") + (r.stderr or "")


def git_root(path: Path | str) -> Path:
    """Raíz del repo git que contiene ``path`` (archivo o directorio)."""
    p = Path(path)
    d = p if p.is_dir() else p.parent
    out = _run(["git", "-C", str(d), "rev-parse", "--show-toplevel"], d)
    return Path(out.stdout.strip()) if out.returncode == 0 else d


def resolve_repo(arg: str) -> str:
    """Resuelve un repo por nombre (clave de ``config.REPOS``), ruta o slug -> ruta raíz del repo.

    Fuente única de la resolución para todas las superficies (CLI y tools conversacionales), así
    "scraper", "C:/…/mi_scraper/run.py" y el slug del vault llegan al mismo sitio.

    Args:
        arg: nombre registrado, ruta (archivo o directorio, se sube a la raíz git) o slug.

    Returns:
        La ruta raíz del repo; si ``arg`` no es una clave conocida ni una ruta existente, se
        devuelve tal cual (un slug se usa literal para derivar el archivo de plan).
    """
    from agent import config  # lazy como el resto del módulo: import ligero

    if arg in config.REPOS:
        return config.REPOS[arg]
    p = Path(arg)
    if p.exists():
        return str(git_root(p.resolve()))
    return arg


def has_remote(repo: Path) -> bool:
    """True si el repo tiene un remoto configurado (para poder abrir PR)."""
    return bool(_run(["git", "remote"], repo).stdout.strip())


def open_pr(repo: Path, branch: str, title: str, body: str, base: str) -> tuple[bool, str]:
    """Pushea la rama y abre un PR con gh. Devuelve (ok, url_o_error). Nunca mergea."""
    push = _run(["git", "push", "-u", "origin", branch], repo, timeout=120)
    if push.returncode != 0:
        return False, "push falló: " + ((push.stderr or push.stdout) or "")[-300:]
    pr = _run(
        [GH, "pr", "create", "--base", base, "--head", branch, "--title", title, "--body", body],
        repo,
        timeout=120,
    )
    return pr.returncode == 0, ((pr.stdout or pr.stderr) or "").strip()


def _current_branch(repo: Path) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], repo).stdout.strip() or "master"


def _is_pr_url(url: object) -> bool:
    """True si ``url`` es una URL de PR real (no un marcador de fallo/sin-remoto)."""
    return isinstance(url, str) and url.startswith("http")


def pr_state(repo: Path, pr_url: str) -> str:
    """Estado de un PR vía gh: ``'MERGED' | 'CLOSED' | 'OPEN' | 'UNKNOWN'``."""
    r = _run([GH, "pr", "view", pr_url, "--json", "state", "-q", ".state"], repo)
    return r.stdout.strip().upper() if r.returncode == 0 else "UNKNOWN"


def review_prs() -> list[dict]:
    """Lee el estado (merged/closed/open) de los PRs que Escapement abrió y lo anota en el ledger.

    Cierra el bucle de aprendizaje: MERGED=aceptaste, CLOSED=rechazaste, OPEN=pendiente. Solo
    registra cambios (no duplica ``pending``) y no re-consulta PRs ya terminales. Devuelve los
    reviews nuevos o actualizados.
    """
    from agent import ledger

    previos = ledger.latest_pr_reviews()
    nuevos: list[dict] = []
    for ev in ledger.read():
        if ev.get("action") != "optimize":
            continue
        url = ev.get("pr_url")
        if not _is_pr_url(url):
            continue
        prev = previos.get(url)
        if prev in ("accepted", "rejected"):  # terminal: ya aprendido, no re-consultar
            continue
        estado = pr_state(Path(ev["repo"]), url)
        result = {"MERGED": "accepted", "CLOSED": "rejected"}.get(estado, "pending")
        if result == prev:  # sigue igual (pending): no duplicar
            continue
        rec = {
            "action": "review",
            "repo": ev["repo"],
            "target": ev["target"],
            "pr_url": url,
            "pr_state": estado,
            "result": result,
        }
        ledger.record(rec)
        previos[url] = result
        nuevos.append(rec)
    return nuevos


def optimize(
    repo: Path | str,
    directiva: str,
    target: str,
    *,
    python_exe: str = "python",
    stamp: str | None = None,
    make_pr: bool = True,
    base_branch: str | None = None,
    judge: bool = True,
    test_path: str | None = None,
    allow_secrets: bool = False,
) -> Result:
    """Orquestador: worktree AISLADO -> baseline -> dispatch -> VERIFY -> JUEZ -> PR. NUNCA mergea.

    El refactor corre en un **git worktree efímero** creado desde ``base``: aísla el árbol de
    trabajo del repo principal y —al crearse desde un commit— NO incluye archivos gitignored
    (``.env``, secretos no versionados), acotando el blast radius del ``--dangerously-skip-permissions``.
    Riesgo residual: secretos hardcodeados en archivos versionados.

    Args:
        judge: si True (default), un verifier adversarial (LLM-juez) refuta el diff tras pasar
            tests+API; el PR se abre solo si el juez no encuentra riesgo. False lo salta (más
            rápido y sin cuota extra, pero sin ese filtro de calidad).
        base_branch: encadena varias tareas desde la misma base sin acumular ramas.
    """
    from agent import config
    from agent import evals as E
    from agent import judge as J
    from agent import ledger
    from agent import verify as V

    started = time.monotonic()
    repo = Path(repo)
    base = base_branch or _current_branch(repo)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S")
    branch = f"escapement/optimiza-{Path(target).stem}-{stamp}"
    log: list[str] = []

    # Pre-flight de seguridad (P1): no despachar código con secretos hardcodeados a la API del
    # executor. El worktree ya excluye gitignored (.env); esto cubre secretos VERSIONADOS.
    tgt_src = repo / target
    if not allow_secrets and tgt_src.is_file():
        from agent.security.secrets import find_secrets

        secretos = find_secrets(tgt_src.read_text(encoding="utf-8", errors="replace"))
        if secretos:
            ledger.record(
                {
                    "action": "optimize",
                    "repo": str(repo),
                    "target": target,
                    "directiva": directiva,
                    "branch": branch,
                    "verified": False,
                    "blocked_secrets": len(secretos),
                    "executor": config.EXECUTOR,
                    "duration_s": round(time.monotonic() - started, 1),
                    "pr_url": None,
                }
            )
            return Result(
                branch=branch,
                dispatched=False,
                tests_ok=None,
                verified=False,
                verdict=f"BLOQUEADO: {len(secretos)} secreto(s) hardcodeado(s) en {target}",
                diff_stat="",
                summary="; ".join(secretos[:5]),
                log=(
                    "⛔ pre-flight de seguridad: no se despacha código con secretos a la API "
                    f"(allow_secrets=True para forzar). Hallazgos: {'; '.join(secretos)}"
                ),
            )

    work = Path(tempfile.mkdtemp(prefix="escapement-wt-"))
    wt = _run(["git", "worktree", "add", "-b", branch, str(work), base], repo)
    if wt.returncode != 0:
        _run(["git", "worktree", "remove", "--force", str(work)], repo)
        return Result(
            branch=branch,
            dispatched=False,
            tests_ok=None,
            verified=False,
            verdict="no se pudo crear el worktree",
            diff_stat="",
            summary="",
            log=((wt.stderr or wt.stdout) or "")[-300:],
        )
    log.append(f"worktree efímero: {work} (rama {branch} desde {base}; sin archivos gitignored)")

    ok, out, diff_stat, pr_url = False, "", "", None
    ver = V.Verification("dispatch no ejecutado", None, True, [], False)
    ev: E.Eval | None = None
    jd = J.Judgment("SEGURO")
    safe = False
    try:
        # BASELINE en el worktree, ANTES del refactor (no atribuir fallos pre-existentes).
        baseline_tests, _ = V.run_pytest(work, python_exe, test_path=test_path)
        tgt = work / target
        baseline_api = V.public_api(tgt) if tgt.is_file() else {}
        before_metrics = E.metrics(tgt) if tgt.is_file() else {}
        before_ruff = E.ruff_issues(work, target) if tgt.is_file() else -1

        ok, out = _dispatch_isolated(work, target, directiva, allow_secrets=allow_secrets)
        log.append(f"dispatch claude -p: {'ok' if ok else 'ERROR'}")

        # add selectivo: nunca versionar artefactos Python (repos sin .gitignore colaban __pycache__).
        _run(
            ["git", "add", "-A", "--", ".", ":(exclude)**/__pycache__/**", ":(exclude)**/*.py[co]"],
            work,
        )
        _run(["git", "commit", "-q", "-m", f"escapement: {directiva[:60]}"], work)

        ver = V.verify(work, target, baseline_tests, baseline_api, python_exe, test_path=test_path)
        log.append(f"verificación: {ver.tests_verdict}")
        if not ver.api_preserved:
            log.append("API pública CAMBIÓ: " + "; ".join(ver.api_changes))
        log.append(f"veredicto: {'SEGURO' if ver.ok else 'RECHAZADO'}")

        if before_metrics and tgt.is_file():
            ev = E.evaluate(
                before_metrics, E.metrics(tgt), before_ruff, E.ruff_issues(work, target)
            )
            log.append(f"eval: {ev.summary} ({'mejora' if ev.improved else 'sin mejora clara'})")

        diff_stat = _run(["git", "diff", "--stat", f"{base}...{branch}"], work).stdout.strip()

        # Verifier adversarial: si tests+API pasan, un juez independiente refuta el diff
        # (atrapa cambios de comportamiento sutiles que igual pasan los tests).
        if ver.ok and judge:
            full_diff = _run(["git", "diff", f"{base}...{branch}"], work, timeout=60).stdout
            jd = J.judge_diff(full_diff, directiva)
            log.append("juez: " + jd.verdict + (f" — {'; '.join(jd.issues)}" if jd.issues else ""))
        safe = ver.ok and jd.ok

        if make_pr and has_remote(repo):
            if safe:
                api_txt = (
                    "preservada"
                    if ver.api_preserved
                    else "CAMBIÓ (" + "; ".join(ver.api_changes) + ")"
                )
                title = f"escapement: {directiva[:60]}"
                calidad = (
                    f"**Calidad:** {ev.summary} ({'mejora' if ev.improved else 'sin mejora clara'})\n"
                    if ev
                    else ""
                )
                juez_txt = f"**Juez adversarial:** {jd.verdict}\n"
                body = (
                    f"**Directiva:** {directiva}\n**Archivo(s):** {target}\n"
                    f"**Verificación:** {ver.tests_verdict}; API pública {api_txt}\n{calidad}{juez_txt}\n"
                    f"_Refactor autónomo por Escapement en worktree aislado. Revisa antes de mergear._\n\n---\n{out[-1200:]}"
                )
                pr_ok, pr_out = open_pr(work, branch, title, body, base=base)
                pr_url = pr_out if pr_ok else f"(PR falló: {pr_out})"
            else:
                motivo = (
                    ver.tests_verdict if not ver.ok else f"juez RIESGOSO: {'; '.join(jd.issues)}"
                )
                pr_url = f"(sin PR — {motivo})"
            log.append(f"PR: {pr_url}")
    finally:
        _run(["git", "worktree", "remove", "--force", str(work)], repo)
        log.append("worktree efímero removido")

    ledger.record(
        {
            "action": "optimize",
            "repo": str(repo),
            "target": target,
            "directiva": directiva,
            "branch": branch,
            "verified": safe,
            "tests_verdict": ver.tests_verdict,
            "api_changes": ver.api_changes,
            "eval": ev.summary if ev else None,
            "improved": ev.improved if ev else None,
            "judge": jd.verdict,
            "judge_issues": jd.issues,
            "executor": config.EXECUTOR,
            "duration_s": round(time.monotonic() - started, 1),
            "pr_url": pr_url,
        }
    )

    verdict = ver.tests_verdict + ("" if ver.api_preserved else " | API cambió")
    if not jd.ok:
        verdict += f" | juez: {jd.verdict}"
    return Result(
        branch=branch,
        dispatched=ok,
        tests_ok=ver.tests_ok,
        verified=safe,
        verdict=verdict,
        diff_stat=diff_stat,
        summary=out[-1500:],
        pr_url=pr_url,
        log="\n".join(log),
    )


_MAX_ATTEMPTS = (
    2  # reintentos por tarea antes de marcarla "fallido" (fallos transitorios vs. poison-pill)
)


def run_queue(
    limit: int = 0,
    python_exe: str = "python",
    on_start: Callable[[dict], None] | None = None,
    on_done: Callable[[dict, Result], None] | None = None,
) -> dict:
    """Procesa la cola persistente respetando la cuota. Devuelve un resumen.

    ``limit=0`` procesa toda la cola. Se detiene si detecta rate-limit (marca el throttle y
    deja la tarea pendiente para reanudar). ``on_start``/``on_done`` dan feedback en vivo sin
    acoplar la presentación a la lógica: el CLI imprime, las tools conversacionales no.
    """
    from agent import config, ledger, state

    procesadas, throttled, fallidas, resultados = 0, False, 0, []
    intentados: set[int] = set()  # ids tocados en esta corrida: no re-tomar en el mismo barrido
    while not (limit and procesadas >= limit):
        task = state.next_pending(skip=intentados)
        if task is None:
            break
        intentados.add(task["id"])
        if on_start:
            on_start(task)
        started = time.monotonic()
        try:
            res = optimize(
                task["repo"],
                task["directiva"],
                task["target"],
                python_exe=python_exe,
                base_branch=task.get("base"),
            )
        except Exception as exc:
            # Un fallo por-tarea NO tumba la cola: se registra y se reintenta de forma acotada.
            # Agotados los intentos -> "fallido" (terminal), para no re-despachar un poison-pill.
            attempts = int(task.get("attempts", 0)) + 1
            agotado = attempts >= _MAX_ATTEMPTS
            detalle = f"{type(exc).__name__}: {exc}"[:300]
            ledger.record(
                {
                    "action": "optimize",
                    "repo": str(task["repo"]),
                    "target": task["target"],
                    "directiva": task["directiva"],
                    "verified": False,
                    "error": detalle,
                    "traceback": traceback.format_exc()[-600:],
                    "attempts": attempts,
                    "executor": config.EXECUTOR,
                    "duration_s": round(time.monotonic() - started, 1),
                    "pr_url": None,
                }
            )
            state.mark(
                task["id"], "fallido" if agotado else "pending", attempts=attempts, error=detalle
            )
            fallidas += int(agotado)
            continue  # la cola sigue con la próxima tarea
        if state.is_rate_limited(f"{res.summary} {res.log}"):
            state.set_throttled(datetime.now().isoformat(timespec="seconds"))
            throttled = True
            break
        state.mark(
            task["id"], "seguro" if res.verified else "rechazado", branch=res.branch, pr=res.pr_url
        )
        resultados.append(
            {"target": task["target"], "verified": res.verified, "pr_url": res.pr_url}
        )
        procesadas += 1
        if on_done:
            on_done(task, res)
    return {
        "procesadas": procesadas,
        "throttled": throttled,
        "fallidas": fallidas,
        "pendientes": len(state.pending()),
        "resultados": resultados,
    }
