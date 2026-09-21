# Auditoría de Escapement — informe final

> Auditoría senior del paquete `src/agent` (agente autónomo con motor conversacional + motor headless).
> Todas las ubicaciones `archivo:línea` fueron verificadas contra el árbol actual (`master`, commit `0af6263`).
> Severidad: 🔴 Crítica · 🟠 Alta · 🟡 Media · 🟢 Baja.

## Resumen ejecutivo

Escapement tiene **dos motores de ejecución** con posturas de seguridad asimétricas, y esa asimetría es la
causa raíz de casi todos los hallazgos serios:

- **Motor conversacional (SDK)** — cableado con defensa en capas: guard duro como hook `PreToolUse`, gate de
  confirmación (texto/voz), anillo 0 read-only y `setting_sources=[]` para no heredar permisos del entorno
  ([session.py:55-72](src/agent/session.py#L55)). **Postura correcta.**
- **Motor headless (planner → runner → orquestador)** — despacha `claude -p --dangerously-skip-permissions`
  ([executors.py:34-36](src/agent/executors.py#L34)) **sin heredar el hook ni el gate**,
  y sobre el **repo real** (`cwd=repo`), no un worktree aislado. Aquí viven H1–H4.

**Veredicto:** el motor conversacional es apto para producción; el motor headless **no lo es** en su forma
actual. Un plan o una transcripción de voz que dispare `run_agent(mode="edit")` puede ejecutar Bash
arbitrario, SQL destructivo o `git push` a rama protegida sin pasar por ninguna de las defensas que sí
protegen el REPL. La ruta crítica de remediación es cerrar esa frontera: `QW1 → QW2 → S1` (unificar el gate)
y `QW3 → S2` (aislar siempre en worktree). Ambas cadenas, en paralelo, neutralizan los dos hallazgos críticos
(H1, H2) y dos altos (H3, H4).

**Conteo:** 2 críticas · 3 altas · 4 medias · 1 baja.

---

## 1. Mapa base → módulos y fronteras de subsistemas

Sin ciclos de import: los cruces potenciales se rompen con imports lazy dentro de funciones. `cli.py` es el
**composition root** (entry points `agent` y `escapement` → `agent.cli:main`,
[pyproject.toml:11-13](pyproject.toml#L11)).

### Subsistemas

| # | Subsistema | Módulos | Rol |
|---|---|---|---|
| **Base** | Configuración | [config.py](src/agent/config.py) | Rutas, `REPOS`, modelos, anillos de tools. God-module (fan-in ~20). |
| **1** | Seguridad / gate | [security/guard.py](src/agent/security/guard.py), [permissions.py](src/agent/security/permissions.py), [secrets.py](src/agent/security/secrets.py) | Hook duro, gate de confirmación por categoría, detección de secretos. |
| **2** | Sesión conversacional | [session.py](src/agent/session.py), [repl.py](src/agent/repl.py), [router.py](src/agent/router.py), [convlog.py](src/agent/convlog.py) | Loop SDK protegido, ruteo local/agente. |
| **3** | Motor headless | [planner.py](src/agent/planner.py), [runner.py](src/agent/runner.py), [orchestrator.py](src/agent/orchestrator.py), [executors.py](src/agent/executors.py), [personas.py](src/agent/personas.py), [judge.py](src/agent/judge.py), [evals.py](src/agent/evals.py), [verify.py](src/agent/verify.py), [backlog.py](src/agent/backlog.py) | Planificación, ejecución paso a paso, optimize/PR, juez. |
| **4** | Estado / persistencia | [state.py](src/agent/state.py), [ledger.py](src/agent/ledger.py) | Cola de tareas, journal append-only. |
| **5** | Voz | [voice/](src/agent/voice) (daemon, ears, stt, tts, loop, interaction, controls, tray) | Entrada/salida de voz, gate de voz. |
| **6** | Herramientas MCP | [tools/memory.py](src/agent/tools/memory.py), [tools/orchestrator.py](src/agent/tools/orchestrator.py), [tools/semantic.py](src/agent/tools/semantic.py) | Servidores MCP expuestos al SDK. |

### Fan-in de la base

`config` es consumido por ~22 módulos (prácticamente todos salvo los standalone). Import directo en, entre
otros, [router.py:17](src/agent/router.py#L17),
[session.py:16](src/agent/session.py#L16),
[repl.py:21](src/agent/repl.py#L21),
[executors.py:22](src/agent/executors.py#L22),
[state.py:16](src/agent/state.py#L16),
[ledger.py:15](src/agent/ledger.py#L15),
[planner.py:20](src/agent/planner.py#L20). El único que importa un símbolo suelto es
[permissions.py:22](src/agent/security/permissions.py#L22) (`AGENT_NAME`). Los cruces
runner↔orchestrator y cli↔orchestrator se resuelven con imports lazy
([runner.py:138](src/agent/runner.py#L138),
[cli.py:22](src/agent/cli.py#L22)) para evitar ciclos.

### Frontera de confianza (clave)

```
                    [ input no confiable: voz / plan LLM / contenido externo ]
                                          │
              ┌───────────────────────────┴───────────────────────────┐
              ▼                                                         ▼
   Motor conversacional (SDK)                             Motor headless (subprocess)
   session.build_options                                  executors.run_agent
   · hook PreToolUse (guard) ✅                            · claude -p --dangerously-skip-permissions ❌
   · gate can_use_tool ✅                                  · sin hook, sin gate ❌
   · RING0 read-only ✅                                    · cwd = repo REAL ❌ (salvo carril optimize)
   · setting_sources=[] ✅                                 · pre-flight de secretos solo en optimize ❌
```

---

## 2. Vulnerabilidades agent-specific

| ID | Hallazgo | Severidad | Evidencia (archivo:línea) |
|---|---|---|---|
| **H1** | El guard duro solo se cablea en el SDK conversacional; el dispatch headless (`claude -p --dangerously-skip-permissions`) no hereda hook ni gate → Bash arbitrario, SQL destructivo, push a rama protegida, lectura de `.env`. | 🔴 Crítica | guard en [session.py:67](src/agent/session.py#L67); dispatch sin hook en [executors.py:34-36](src/agent/executors.py#L34); handlers [runner.py:87-100](src/agent/runner.py#L87), [runner.py:103-121](src/agent/runner.py#L103), [runner.py:145-154](src/agent/runner.py#L145) |
| **H2** | El runner despacha edición sobre el **repo real** (`cwd=repo`), no un worktree aislado — sin jaula ni pre-flight. | 🔴 Crítica | [runner.py:145-151](src/agent/runner.py#L145) (`_h_editar` sin target), [runner.py:87-95](src/agent/runner.py#L87), [runner.py:103-112](src/agent/runner.py#L103) |
| **H3** | Input no confiable (transcripción de voz, salida del planner/reflexión, contenido externo leído en `investigar`) llega a `run_agent(mode="edit")` sin allowlist de tools ni confirmación → prompt-injection → ejecución. | 🟠 Alta | reflexión ejecuta lo que el LLM propone [runner.py:325-337](src/agent/runner.py#L325); sink en [runner.py:70-95](src/agent/runner.py#L70) y `executors.run_agent` |
| **H4** | El pre-flight de secretos hardcodeados solo existe en `optimize()`; las rutas headless del runner despachan código a la API sin él. | 🟠 Alta | presente en [orchestrator.py:246-250](src/agent/orchestrator.py#L246); ausente en [runner.py](src/agent/runner.py) |
| **H5** | El guard solo evalúa `tool_name == "Bash"`; PowerShell (categoría `shell` en el gate) **no lo evalúa** → bypass total de SQL/`.env`/ramas vía PowerShell. | 🟠 Alta | [guard.py:99](src/agent/security/guard.py#L99); [permissions.py:54](src/agent/security/permissions.py#L54) mapea `PowerShell` a `shell` |
| **H6** | Detección de lectura de `.env` limitada a comandos concretos (`cat/type/head/…`); no cubre `python -c "open('.env')"`, `strings`, `od`, `curl file://`. | 🟡 Media | [guard.py:44-46](src/agent/security/guard.py#L44), [guard.py:120-123](src/agent/security/guard.py#L120) |
| **H7** | El SQL destructivo solo se bloquea si `_SQL` y `_EXEC` aparecen en la **misma** línea; SQL dentro de un `.py` ejecutado con `python script.py` no se inspecciona. | 🟡 Media | [guard.py:103](src/agent/security/guard.py#L103) (`_SQL.search AND _EXEC.search`) |
| **H10** | `config.py` es god-module (importado por ~20 módulos) con rutas y `REPOS` hardcodeados; acopla el paquete a la máquina de un usuario. | 🟢 Baja | [config.py:66](src/agent/config.py#L66) (`REPO_ROOT`), [config.py:90](src/agent/config.py#L90) (`REPOS`) |

**Causa raíz dominante:** la *asimetría de dos motores*. El loop conversacional está protegido
([session.py:55-72](src/agent/session.py#L55)) y el motor headless no.
H1–H4 son síntomas de esa misma frontera de confianza mal cerrada.

---

## 3. Concurrencia

| ID | Hallazgo | Severidad | Evidencia (archivo:línea) |
|---|---|---|---|
| **H8** | `state.json` se maneja read-modify-write (`_load` → mutar → `_save`) **sin lock ni atomicidad**; `run_queue` corre en un hilo (`asyncio.to_thread`) mientras el loop principal puede encolar → lost update / cola corrupta. | 🟡 Media | [state.py:28-83](src/agent/state.py#L28); hilo en [tools/orchestrator.py:135](src/agent/tools/orchestrator.py#L135) |
| **H9** | Escrituras no atómicas (`write_text` directo) en `state.json`, `plan.json` y notas del vault → archivo truncado/corrupto si se interrumpe (plan irreanudable). El ledger sí usa append (`open("a")`) pero sin `fsync`. | 🟡 Media | [state.py:39](src/agent/state.py#L39), [planner.py:176](src/agent/planner.py#L176), [ledger.py:22-23](src/agent/ledger.py#L22) |

### Detalle

- **Carrera concreta (H8):** `enqueue()` en el loop principal hace `_load` → mutar `queue` → `_save`
  ([state.py:41-43](src/agent/state.py#L41)) mientras `run_queue` corre en
  `asyncio.to_thread` ([tools/orchestrator.py:135](src/agent/tools/orchestrator.py#L135))
  y también lee-muta-escribe el mismo archivo. Sin lock, la última escritura gana → **lost update**.
  Escenarios de disparo simultáneo: voz + REPL + orquestador sobre el mismo `state.json`.
- **Atomicidad (H9):** `_save` hace `STATE.write_text(...)` directo
  ([state.py:37-39](src/agent/state.py#L37)); `save_plan` igual
  ([planner.py:176](src/agent/planner.py#L176)). Una interrupción a mitad de
  escritura deja el JSON truncado → el plan no reanuda. Fix: `write_atomic` (tmp + `os.replace`).

---

## 4. Roadmap priorizado (paso 10)

| # | Mejora | Hallazgos que resuelve | Severidad mitigada | Esfuerzo | Prioridad | Dependencias |
|---|---|---|---|---|---|---|
| **QW1** | Extender `guard.evaluate` a PowerShell (tratar `PowerShell` igual que `Bash`). | H5 | Alta | S (bajo) | **P0** | — |
| **QW2** | Endurecer patrones del guard: lectura de `.env` por cualquier sink (Bash/py/`strings`/`curl`) y detectar SQL destructivo también cuando corre un script `.py`. | H6, H7 | Media | S (bajo) | **P0** | — |
| **QW3** | Pre-flight de secretos en **toda** ruta que despache código (mover `find_secrets` dentro de `run_agent(mode="edit")` o a un wrapper común). | H4 | Alta | S-M (bajo-medio) | **P0** | — |
| **QW4** | Helper `write_atomic` (tmp + `os.replace`) y usarlo en `state.py`, `planner.save_plan`, notas y ledger. | H9 | Media | S (bajo) | **P0** | — |
| **S1** | **Unificar el gate:** pasar los comandos del dispatch headless por `guard.evaluate` (envolver `run_agent`, o inyectar el hook `PreToolUse` al `claude -p`), reusando los patrones endurecidos. | H1, H3 (parcial), H6, H7 | Crítica | M-L (medio-alto) | **P1** | QW1, QW2 |
| **S2** | **Aislar siempre en worktree:** enrutar `_h_ejecutar` / `_h_editar`-sin-target / `_h_verificar` por el carril `optimize` (worktree efímero + PR sin mergear), nunca `cwd=repo` real. | H1, H2, H4 | Crítica | M (medio) | **P1** | QW3 |
| **S3** | Allowlist de tools / confinar Bash en rutas autónomas: exigir gate (o denegar shell) para el dispatch headless disparado por input no confiable. | H3 | Alta | M (medio) | **P1** | S1 |
| **S4** | Serializar el acceso a `state.json` (single-writer o lock por proceso) para runs concurrentes voz + REPL + orquestador. | H8 | Media | M (medio) | **P1** | QW4 |
| **M1** | Externalizar rutas y `REPOS` de `config.py` a un archivo de config del usuario; reducir la superficie del god-module. | H10 | Baja | M (medio) | **P2** | — |

**S** = horas · **M** = 1–2 días · **L** = varios días.

### Orden de implementación y dependencias

```
P0 (quick wins, independientes — hacerlos primero, bajan el riesgo ya):
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
H2) y dos altos (H3, H4). `QW4 → S4` corre en paralelo y cierra la corrupción/carrera de estado. M1 es
oportunista.
