# Roadmap de mejora priorizado — auditoría de Escapement

> **Estado: completado (histórico)** · Verificado: 2026-08-26 — QW1–QW4, S1–S4 y M1 entregados al
> 100% (ver §Estado de avance). Se conserva como referencia de la auditoría 2026-07-08; la lista
> viva de deudas es [DEUDAS.md](DEUDAS.md) y el rumbo vigente, [PLAN_AUTOGUIADO.md](PLAN_AUTOGUIADO.md).

Consolidación del paso 10 de la auditoría. Ordena las remediaciones por **prioridad** (P0 → P2),
distinguiendo **quick wins** (bajo esfuerzo, riesgo inmediato) de **cambios estructurales** (atacan la
causa raíz). Cada mejora enlaza los hallazgos que resuelve, con su ubicación `archivo:línea`.

## Catálogo de hallazgos (referencia)

| ID | Hallazgo | Severidad | Evidencia (archivo:línea) |
|---|---|---|---|
| **H1** | El guard duro solo se cablea en el SDK conversacional; el dispatch headless (`claude -p --dangerously-skip-permissions`) no hereda hook ni gate → Bash arbitrario, SQL destructivo, push a rama protegida, lectura de `.env` | 🔴 Crítica | guard en `session.py:67`; dispatch sin hook en `executors.py:34-36`; handlers `runner.py:87-100,103-121,145-154` |
| **H2** | El runner despacha edición sobre el **repo real** (`cwd=repo`), no un worktree aislado — sin jaula ni pre-flight | 🔴 Crítica | `runner.py:145-151` (`_h_editar` sin target), `runner.py:87-95`, `runner.py:103-112` |
| **H3** | Input no confiable (transcripción de voz, salida del planner/reflexión, contenido externo leído en `investigar`) llega a `run_agent(mode="edit")` sin allowlist de tools ni confirmación → prompt-injection → ejecución | 🟠 Alta | `runner.py:325-337` (reflexión ejecuta lo que el LLM propone), `runner.py:70-95`; sink en `executors.run_agent` |
| **H4** | El pre-flight de secretos hardcodeados solo existe en `optimize()`; las rutas headless del runner despachan código a la API sin él | 🟠 Alta | presente en `orchestrator.py:246-277`; ausente en `runner.py` |
| **H5** | El guard solo evalúa `tool_name == "Bash"`; PowerShell (categoría `shell` en el gate) **no lo evalúa** → bypass total de SQL/`.env`/ramas vía PowerShell | 🟠 Alta | `guard.py:99`; `permissions.py:54` mapea `PowerShell` a `shell` |
| **H6** | Detección de lectura de `.env` limitada a comandos concretos (`cat/type/head/…`); no cubre `python -c "open('.env')"`, `strings`, `od`, `curl file://` | 🟡 Media | `guard.py:44-46,120-123` |
| **H7** | El SQL destructivo solo se bloquea si `_SQL` y `_EXEC` aparecen en la **misma** línea de comando; SQL dentro de un `.py` ejecutado con `python script.py` no se inspecciona | 🟡 Media | `guard.py:103` (`_SQL.search AND _EXEC.search`) |
| **H8** | `state.json` se maneja read-modify-write (`_load` → mutar → `_save`) **sin lock ni atomicidad**; `run_queue` corre en un hilo (`asyncio.to_thread`) mientras el loop principal puede encolar → lost update / cola corrupta | 🟡 Media | `state.py:28-83`; hilo en `tools/orchestrator.py:135` |
| **H9** | Escrituras no atómicas (`write_text` directo) en `state.json`, `plan.json`, notas del vault y ledger → archivo truncado/corrupto si se interrumpe (plan irreanudable) | 🟡 Media | `state.py:39`, `planner.py:176`, `ledger.py:22-23` |
| **H10** | `config.py` es god-module (importado por ~20 módulos) con rutas y `REPOS` hardcodeados; acopla el paquete a la máquina de un usuario | 🟢 Baja | `config.py` (`REPOS`, `REPO_ROOT`) |

**Causa raíz dominante:** la *asimetría de dos motores* — el loop conversacional está protegido (guard +
gate en capas, `session.py:55-72`) y el motor headless (planner/runner/orquestador) no. H1–H4 son todos
síntomas de esa misma frontera de confianza mal cerrada.

## Roadmap priorizado

| # | Mejora | Hallazgos que resuelve | Severidad mitigada | Esfuerzo | Prioridad | Dependencias |
|---|---|---|---|---|---|---|
| **QW1** | Extender `guard.evaluate` a PowerShell (tratar `PowerShell` igual que `Bash`) | H5 | Alta | S (bajo) | **P0** | — |
| **QW2** | Endurecer patrones del guard: lectura de `.env` por cualquier sink (Bash/py/`strings`/`curl`) y detectar SQL destructivo también cuando corre un script `.py` | H6, H7 | Media | S (bajo) | **P0** | — |
| **QW3** | Pre-flight de secretos en **toda** ruta que despache código (mover `find_secrets` dentro de `run_agent(mode="edit")` o a un wrapper común) | H4 | Alta | S-M (bajo-medio) | **P0** | — |
| **QW4** | Helper `write_atomic` (tmp + `os.replace`) y usarlo en `state.py`, `planner.save_plan`, notas y ledger | H9 | Media | S (bajo) | **P0** | — |
| **S1** | **Unificar el gate**: pasar los comandos del dispatch headless por `guard.evaluate` (envolver `run_agent`, o inyectar el hook `PreToolUse` al `claude -p`), reusando los patrones endurecidos | H1, H3 (parcial), H6, H7 | Crítica | M-L (medio-alto) | **P1** | QW1, QW2 |
| **S2** | **Aislar siempre en worktree**: enrutar `_h_ejecutar` / `_h_editar`-sin-target / `_h_verificar` por el carril `optimize` (worktree efímero + PR sin mergear), nunca `cwd=repo` real | H1, H2, H4 | Crítica | M (medio) | **P1** | QW3 (pre-flight ya disponible) |
| **S3** | Allowlist de tools / confinar Bash en rutas autónomas: exigir gate (o denegar shell) para el dispatch headless disparado por input no confiable | H3 | Alta | M (medio) | **P1** | S1 |
| **S4** | Serializar el acceso a `state.json` (single-writer o lock por proceso) para runs concurrentes voz + REPL + orquestador | H8 | Media | M (medio) | **P1** | QW4 |
| **M1** | Externalizar rutas y `REPOS` de `config.py` a un archivo de config del usuario; reducir la superficie del god-module | H10 | Baja | M (medio) | **P2** | — |

**S** = horas, **M** = 1–2 días, **L** = varios días.

## Orden de implementación y dependencias

```
P0 (quick wins, independientes entre sí — hacerlos primero, bajan el riesgo ya):
  QW1 ─┐
  QW2 ─┤ (endurecen guard.evaluate → habilitan S1)
  QW3 ─┼──────────────► S2   (pre-flight disponible antes de aislar)
  QW4 ─┴──────────────► S4   (escritura atómica antes de serializar el estado)

P1 (estructural, causa raíz):
  QW1+QW2 ──► S1 ──► S3      (unificar el gate → luego allowlist de shell)
  QW3 ──────► S2            (worktree siempre — mayor reducción de blast radius)
  QW4 ──────► S4            (lock sobre state.json)

P2:
  M1 (independiente, maintainability)
```

**Ruta crítica recomendada:** `QW1 → QW2 → S1` cierra el bypass del guard, y `QW3 → S2` neutraliza el blast
radius de la ejecución sin jaula. Esas dos cadenas, en paralelo, resuelven los dos hallazgos críticos (H1,
H2) y dos altos (H3, H4). QW4 → S4 corre en paralelo y cierra la corrupción/carrera de estado. M1 es
oportunista.

## Estado de avance

| Ítem | Estado | Cómo se implementó |
|---|---|---|
| QW1–QW4 | ✅ Hecho | PowerShell = Bash en el guard; sinks de `.env` + SQL dentro de scripts; `find_secrets` en `run_agent(mode="edit")`; `fsutil.write_text_atomic` en state/plan/notas/ledger |
| S4 | ✅ Hecho | `fsutil.locked` (lock del OS: `msvcrt`/`flock`, se libera solo si el proceso muere) envolviendo los 3 mutadores de `state.py`; tests de concurrencia con threads |
| S1 | ✅ Hecho | `security/guard_cli.py` (hook standalone, fail-open) + `--settings` con el hook `PreToolUse` inyectado por `executors._claude_edit`; e2e por subproceso en tests. Limitación: `agy`/`cursor` no tienen hooks — su gate sigue siendo worktree + pre-flight |
| S2 | ✅ Hecho | Worktree persistente POR PLAN (rama `escapement/plan-<stamp>`, `plan.workdir`/`plan.rama` persistidos → reanudable; prune+re-montaje si el temp se limpió): todos los handlers despachan con `cwd=_workdir(plan, repo)`, nunca el repo real — este solo cambia al mergear tú la rama. Repos no-git caen al comportamiento previo. Personas/memoria siguen resolviendo con el repo canónico |
| S3 | ✅ Hecho | `run_agent(no_shell=True)` confina el dispatch de EDICIÓN pura (carril `_h_editar` sin target, el que recibe acciones del planner/reflexión y no necesita comandos): doble capa en claude — env `AGENT_DENY_SHELL=1` que el hook del guard honra (garantizado bajo `skip-permissions`) + `--disallowedTools Bash PowerShell` declarativo. `ejecutar`/`verificar` conservan shell (es su propósito): su jaula es guard (S1) + worktree (S2). agy/cursor ignoran `no_shell` (sin mecanismo equivalente, limitación documentada) |
| M1 | ✅ Hecho | `escapement.toml` opcional (gitignorado; plantilla `escapement.toml.example`; ubicación `AGENT_CONFIG_FILE` o `<repo>/escapement.toml`) con `recall_script`/`projects_dir`/`data_dir` y tabla `[repos]` que agrega/sobreescribe sobre `_DEFAULT_REPOS`. Precedencia por valor: env var > archivo > default — sin archivo, comportamiento EXACTO al previo. Fail-open: ilegible/corrupto = como si no existiera |
