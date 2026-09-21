<div align="center">

# Escapement

**Asistente personal sobre el [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python) (Python)**
*Local-first · multi-capacidad · permisos en anillos concéntricos tras **una sola puerta***

![Python](https://img.shields.io/badge/python-≥3.10-3776AB?logo=python&logoColor=white)
![uv](https://img.shields.io/badge/deps-uv-DE5FE9?logo=uv&logoColor=white)
![pytest](https://img.shields.io/badge/tests-pytest-0A9EDC?logo=pytest&logoColor=white)
![estado](https://img.shields.io/badge/núcleo%20%2B%20voz%20%2B%20orquestador-operativos-brightgreen)
![licencia](https://img.shields.io/badge/licencia-MIT-blue)

</div>

> El nombre del agente es configurable (`AGENT_NAME`): copia [`.env.example`](.env.example) a `.env`
> y [`escapement.toml.example`](escapement.toml.example) a `escapement.toml` (ambos gitignorados).

---

## Qué es Escapement

Asistente personal residente (voz con hotkey <kbd>F2</kbd> + REPL de texto) sobre el Claude Agent
SDK, con un **orquestador de ingeniería** encima: recibe una directiva ("optimiza este archivo"),
escanea deuda, despacha el refactor a un motor pluggable (Claude Code / Antigravity / Cursor) en un
worktree aislado, verifica (tests + diff de API pública + juez adversarial), mide la mejora y sube los cambios de lo modificado.

Hoy ejecuta tareas bien definidas de punta a punta. El rumbo —de "ejecutar tareas" a "gestionar
objetivos hasta *done*"— y el estado de cada pieza viven en **[`docs/VISION.md`](docs/VISION.md)**,
documento vivo.

> [!NOTE]
> **Asistente personal, publicado como referencia.** Nada en el código apunta a una máquina, un
> repo o una persona: eso vive en `.env` y `escapement.toml`, gitignorados (sus `.example`
> documentan cada clave). Sin config arranca con defaults genéricos y solo se conoce a sí mismo
> como repo. Sin soporte ni compromiso de compatibilidad entre versiones.

---

## Tabla de contenido

- [Qué es Escapement](#qué-es-escapement)
- [Filosofía: anillos tras una puerta](#filosofía-anillos-tras-una-puerta)
- [Roadmap](#roadmap)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Comandos](#comandos)
  - [CLI (`escapement <comando>`)](#cli-escapement-comando)
  - [Lenguaje natural (tools MCP)](#lenguaje-natural-tools-mcp)
- [Trazabilidad y rescate (worktrees)](#trazabilidad-y-rescate-worktrees)
- [El gate de seguridad](#el-gate-de-seguridad)
- [Memoria](#memoria)
- [Voz (F2)](#voz-f2--daemon-residente)
- [Bus de eventos](#bus-de-eventos)
- [Tests](#tests)
- [Configuración](#configuración)
- [Estructura del repo](#estructura-del-repo)
- [Licencia](#licencia)

---

## Filosofía: anillos tras una puerta

```
        +------ GATE ------  PreToolUse hook (guard)  ------------------+
        |  corre ANTES de todo, deniega hasta en bypassPermissions      |
        |                                                               |
  VOZ ->|   ANILLO 0 - Núcleo read-only ...... Read/Grep/recall_memory  |  auto-aprobado
 (F2)   |                                       (allowed_tools)         |  (allowed_tools)
        |   ANILLO 1 - escribir memoria ....... write_memory            |  -+
  CLI ->|   ANILLO 2 - operar scrapers ........ run/diagnose            |   +- can_use_tool
 (F0)   |   ANILLO 3 - sistema/Chrome/desktop . shell/browser/os (MCP)  |  -+  confirma c/u
        +---------------------------------------------------------------+
```

| Anillo | Tools | Cómo se aprueba |
| :------: | ------- | ----------------- |
| **0** | `Read`, `Grep`, `Glob`, `WebSearch`, `WebFetch`, `mcp__memory__recall_memory`, `mcp__semantic__recall_semantic`, `mcp__orq__estado` / `deuda` / `plan` | va en `allowed_tools` → sin fricción |
| **1-2** | `mcp__memory__write_memory`, `mcp__orq__optimizar` / `vigilar` / `trabajar` / `revisar_prs` / `objetivo` / `ejecutar` / `paso` | cae a `can_use_tool` → confirmación explícita (`Allow`/`Deny`) |
| **3** (F4) | sistema / Chrome / desktop (MCP): `shell`, `browser`, `os` | `can_use_tool`, nunca auto-aprobado |

El hook `PreToolUse` corre antes que todo lo demás — ver
[El gate de seguridad](#el-gate-de-seguridad).

---

## Roadmap

| Fase | Entrega | Estado |
| :----: | --------- | :------: |
| **F0** | Núcleo texto: scaffold + `recall_memory` + guard hook + Anillo 0 | 🟢 listo |
| **F1** | Memoria (`write_memory` + `recall_semantic`) + router híbrido local/online + **orquestador** (backlog, worktree, verify, juez, PR, cola) | 🟢 listo |
| **F2** | Voz: hotkey <kbd>F2</kbd> → STT → **daemon residente** (idle⇄active, hilo persistente) → TTS + ícono de bandeja | 🟢 listo |
| **F3** | Operador: lanzar/diagnosticar scrapers (Anillo 2) | ⚪ pendiente |
| **F4** | Control total: MCP Chrome + computer-use (Anillo 3) | ⚪ pendiente |

El MVP (F0-F2) corre en real sobre repos propios. El roadmap post-MVP está versionado aparte en
[`docs/VISION.md`](docs/VISION.md), hoy en la fase **"0 · Robustez"** (observabilidad del ledger,
N-jueces, recuperación de fallos).

---

## Requisitos

- Python **≥ 3.10** (probado en 3.12) · [`uv`](https://docs.astral.sh/uv/)
- Auth de Claude: `ANTHROPIC_API_KEY` en `.env`, o la sesión de Claude Code ya autenticada.
- **Vault de memoria** (requisito del usuario, **NO** incluido en el repo): notas markdown en
  `~/.claude/projects/*/memory/` + el motor `~/.claude/tools/recall_memory.py`. Rutas configurables
  con `AGENT_PROJECTS_DIR` / `AGENT_RECALL_SCRIPT` (ver [Configuración](#configuración)).
- **Ollama** en `localhost:11434` (opcional): habilita el router híbrido —charla simple en local
  (`qwen2.5:3b`, sin egress), lo complejo a Claude— y el
  [ruteo del orquestador](#ruteo-local--online-en-el-orquestador), que con VRAM holgada sube al
  modelo medio (`ollama pull qwen2.5-coder:7b`). Sin Ollama, todo degrada a Claude.
- **GPU NVIDIA** (opcional): el modo voz corre STT (`faster-whisper large-v3-turbo`, ~1.6 GB de
  VRAM) en CUDA; si no, cae a CPU/int8.

---

## Instalación

```bash
uv sync                    # crea el venv e instala deps (incluye pytest)
uv run pytest              # tests aislados (no requieren API ni Chrome)
uv run escapement          # loop REPL (requiere auth de Claude)
uv run escapement --voz    # loop de voz (hotkey F2; requiere `uv sync --group voice`)
```

| Grupo | Instala con | Habilita |
| ------- | ------------- | ---------- |
| `dev` | `uv sync --group dev` | `pytest` (por defecto en `uv sync`) |
| `voice` | `uv sync --group voice` | STT (faster-whisper) + TTS (kokoro-onnx/SAPI) + push-to-talk |
| `semantic` | `uv sync --group semantic` | `recall_semantic` (embeddings ONNX vía `fastembed`) |
| `local` | `uv sync --group local` | Router híbrido → LLM local (Ollama) |

---

## Comandos

Cada capacidad es invocable de **dos formas equivalentes**: por CLI (`escapement <comando>`) o
hablándole al REPL/voz (el LLM llama la tool MCP). Ambas caen al mismo código y al mismo gate.

### CLI (`escapement <comando>`)

| Comando | Alias | Uso | Qué hace | Anillo |
| --------- | ------- | ----- | ---------- | :------: |
| `optimiza` | `opt` | `escapement optimiza <ruta> [directiva]` · `--directiva` | Despacha un archivo/dir en una rama aislada, verifica (tests + diff de API + juez adversarial), mide la mejora y abre un PR. | 🟡 2 |
| `backlog` | `deuda` | `escapement backlog <repo> [N]` · `--repo` `--limit`/`-n` | Escanea el repo (AST + regex, sin LLM/cuota) y lista los `N` candidatos con más deuda. Solo lectura. | 🟢 0 |
| `vigilar` | `watch` | `escapement vigilar <repo> [N]` · `--repo` `--limit`/`-n` | Escanea deuda y **encola** los `N` peores candidatos. No los procesa aún. | 🟡 2 |
| `trabajar` | `cola`, `run` | `escapement trabajar [N]` · `--limit`/`-n` | Procesa la cola (worktree + verify + PR) respetando el límite de la suscripción; **reanudable** (Ctrl-C seguro). `N=0` = toda la cola. | 🟡 2 |
| `revisar` | `review` | `escapement revisar` | Consulta el estado de los PRs abiertos (`gh`) y **aprende** del accept/reject (lo rechazado no se re-propone). | 🟡 2 |
| `estado` | `status` | `escapement estado` | Dashboard: plan activo (objetivo, avance, checkpoint o próximo paso), cola, throttle de cuota, `ledger.stats()` y tasa de aceptación de PRs. Solo lectura. | 🟢 0 |
| `autoevoluciona` | `auto` | `escapement autoevoluciona <repo> [N]` · `--repo` `--limit`/`-n` | Atajo: `vigilar` + `trabajar` en una invocación. | 🟡 2 |
| `objetivo` | `meta` | `escapement objetivo [repo] "<meta>"` · `--repo` | El planner propone un roadmap con criterios de *done*. Con repo, el plan queda **dedicado** a ese repo y activo. | 🟢 0 |
| `ejecutar` | `ejecuta` | `escapement ejecutar [repo]` · `--repo` `--si`/`-s` | Corre el plan del repo (o el activo) hasta el próximo checkpoint. **Reanudable**. Antes de la primera corrida muestra el roadmap y pide OK (`--si` lo salta); en TTY, un checkpoint a mitad pregunta inline. | 🟡 2 |
| `plan` | — | `escapement plan [lista\|ver [repo]\|activar <repo>\|quitar <id>\|mover <id> <pos>\|editar <id> "<accion>"]` | Planes por repo (uno por repo, independientes): `lista` (· = activo), `ver` imprime el roadmap completo, `activar` apunta el activo; `quitar`/`mover`/`editar` editan el plan activo. | 🟢 0 |
| `paso` | — | `escapement paso <id> <estado> ["nota"]` | Marca un paso / responde un checkpoint (`hecho`\|`pendiente`\|`fallido`\|`bloqueado`); la nota queda como contexto. | 🟢 0 |
| `memoria` | — | `escapement memoria [repos...]` | Compila la memoria de los repos **nuevos** (o los indicados). Autónomo; para en el 1er checkpoint o fallo. | 🟡 2 |
| `repaso` | — | `escapement repaso [repos...]` | Repasa la memoria de los repos ya **hechos** contra el código actual. | 🟡 2 |
| `agentes` | — | `escapement agentes [dest]` · `--dest` | Exporta las personas como subagentes de Claude Code. | 🟢 0 |
| `historial` | `historia`, `log` | `escapement historial [N]` · `--limit`/`-n` | Últimos `N` turnos de conversación (`convlog.py`). Solo lectura. | 🟢 0 |
| `eventos` | `events` | `escapement eventos [N]` · `--limit`/`-n`, `--topic`/`-t` | Últimos `N` eventos del journal del bus (`data/events.jsonl`), con filtro por prefijo. Solo lectura. | 🟢 0 |
| `help` | `ayuda`, `-h`, `--help` | `escapement help [<cmd>]` | Lista los subcomandos, o los flags de uno; sugiere typos vía `difflib`. | 🟢 0 |
| `--voz` | `--voice` | `escapement --voz` | Arranca en **modo voz** (hotkey <kbd>F2</kbd>) en vez del REPL. | — |

> 🟢 0 = auto-aprobado · 🟡 2 = confirma (`[s/N]` en consola, hablada en modo voz). Los comandos del
> planner (`objetivo`/`ejecutar`/`plan`/`paso`) no pasan por el gate del orquestador: `ejecutar` sí
> lo hace **por cada tool** que edita código dentro del plan.

#### Aprobar el roadmap antes de gastar

`plan ver` imprime el roadmap íntegro (estados, criterios de *done*, notas y próximo paso listo) sin
gastar un token; `ejecutar` lo vuelve a mostrar y pide OK antes de la primera corrida.

```bash
escapement objetivo controller "migra el login a OAuth"   # el planner propone
escapement plan ver controller                            # léelo (solo lectura, 0 tokens)
escapement ejecutar controller                            # muestra el roadmap y pide [s/N]
escapement ejecutar controller --si                       # arranca directo
escapement estado                                         # avance + checkpoint, en 3 líneas
```

`estado` abre con el plan activo —objetivo, `hechos/total` y el checkpoint que lo traba (con el
`escapement paso <id> hecho` exacto), el próximo paso listo, o `roadmap completo ✓`— antes de la cola
y el ledger. Con todo `hecho` refleja el **veredicto de la evaluación global**, no el conteo:
`criterio global verificado` (`done`) o `⏸ ... el criterio global AÚN no se cumple` con los primeros
gaps y el contador de replanificación (`N/máx`). `mcp__orq__estado` abre con la misma línea.

Reglas: pregunta **una vez por plan**; solo en TTY —sin terminal interactiva ejecuta sin
preguntar—; se apaga con `AGENT_PLAN_APPROVAL=0` o `[plan] aprobacion = false`.
`memoria`, `repaso` y la tool MCP `ejecutar` llaman al runner directo y no pasan por esta puerta.

#### Checkpoint inline

Un paso `bloqueado` o `fallido` a mitad de corrida se resuelve en el mismo proceso, sin salir:

```
[checkpoint] paso 3 (fallido): pytest salió con código 1
  ¿cómo sigo? [h]echo y continuar · [r]eintentar el paso · [Enter] salir
  ("h <texto>" guarda tu decisión como nota del paso):
```

- `h` / `hecho` / `s` → marca `hecho` y sigue. `r` / `reintentar` → vuelve a `pendiente`
  conservando la nota con la causa del fallo.
- `h <texto>` guarda la decisión como nota: en un checkpoint de **DIVERGENCIA** esa es la respuesta
  que fluye a los pasos dependientes, igual que `escapement paso <id> hecho "<decisión>"`.
- `Enter` (o algo no reconocido) sale con el mensaje de reanudación. La respuesta se persiste al
  momento.
- Sin TTY o con `AGENT_PLAN_APPROVAL=0` no pregunta: se reanuda con
  `escapement paso ... && escapement ejecutar`.

#### Evaluación global al cerrar

Que todos los pasos queden `hecho` no garantiza el objetivo. Al agotar el roadmap, un dispatch
**read-only** contrasta el estado real del repo contra el `criterio_global` y emite veredicto:

```
[ejecutar] roadmap completo ✓ · criterio global verificado        # veredicto: done

[ejecutar] pasos agotados, pero el criterio global AÚN no se cumple:   # veredicto: incompleto
  criterio: 100% type hints en worker/
   - anotar worker/utils.py (sin hints en 3 funciones)
  Los gaps quedaron guardados en el plan. Ciérralos a mano o replantea con 'escapement objetivo ...'
```

- Corre **solo** con el roadmap agotado entero en `hecho` y `criterio_global` presente; un
  checkpoint a mitad no dispara nada.
- `incompleto` persiste hasta 4 gaps concretos en el plan (insumo de la replanificación) y **no**
  captura la trayectoria como éxito en el ReasoningBank.
- **Best-effort**: sin veredicto usable, el cierre queda por conteo, sin sello y sin bloquear.
- `AGENT_PLAN_EVAL=0` (o `[plan] evaluacion = false`) la apaga por completo, incluso si el JSON
  trae un veredicto viejo.
- El dispatch usa el pseudo-tipo `evaluar` en `route_step` (Clase B: solo va a local con
  `AGENT_LOCAL_ORCH_TOOLS=1`).

#### Replanificación automática

Un veredicto `incompleto` convierte los gaps en pasos nuevos (dispatch acotado a **solo** las
brechas listadas) y la corrida sigue hasta volver a evaluar:

```
[ejecutar] paso 3/3 verificar · ...                # roadmap original agotado
  (evaluación: incompleto — 1 gap)                 # gaps -> pasos nuevos, la corrida sigue
[ejecutar] paso 4/4 editar · anotar worker/utils.py
[ejecutar] roadmap completo ✓ · criterio global verificado   # segunda evaluación: done
```

- **Tope anti-bucle**: `AGENT_PLAN_REPLAN_MAX` ciclos evaluar→replanificar por plan (default `2`).
  Al tope, la corrida termina incompleta y el CLI lo anuncia como checkpoint humano.
- El contador (`replan_ciclos`) **persiste en el JSON**: reanudar no lo resetea.
- Los pasos nuevos pasan las guardas de siempre: sin `reflexionar`, sin duplicados, máximo 4 por
  ciclo. Si no sobrevive ninguno, termina incompleta sin consumir ciclo.
- `AGENT_PLAN_REPLAN_MAX=0` apaga solo la replanificación; la evaluación sigue.
- El gasto cuenta en el budget del plan (`[budget] tokens_por_plan`) y en el roll-up de costos.
- Telemetría: cada evaluación publica `plan.eval` y cada replanificación efectiva `plan.replan`
  (journal durable, `escapement eventos --topic plan.`).

#### Editar el roadmap sin abrir el JSON

`plan quitar / mover / editar` operan sobre el plan **activo** (el mismo blanco que `escapement paso`)
y persisten al momento.

```bash
escapement plan ver                          # ids y dependencias
escapement plan quitar 3                     # elimina el paso 3
escapement plan mover 5 2                    # lleva el paso 5 a la posición 2
escapement plan editar 2 "escribir el adaptador OAuth"   # reescribe la acción del paso 2
```

- **`quitar <id>` empalma el grafo**: los dependientes heredan las dependencias del quitado (en
  `1 ← 2 ← 3`, quitar el 2 deja al 3 dependiendo del 1), sin duplicados ni auto-referencias. Los
  ids **no se renumeran**.
- **`mover <id> <pos>` valida la topología**: posición 1-based (fuera de rango se recorta) y se
  **rechaza completo** si dejaría un paso antes de una dependencia. El orden es prioridad: el runner
  despacha el primer paso listo.
- **`editar <id> "<accion>"` solo toca la acción**: tipo, criterio, dependencias, estado y nota
  quedan intactos.
- Sin plan activo o con id inexistente avisan y no tocan nada; los tres son anillo 0.

#### Flags y ayuda por comando

Los posicionales de siempre siguen funcionando; los flags son un alias opcional. El mini-parser
común ([`cliparse.py`](src/agent/cliparse.py)) da la misma gramática a todos los subcomandos:

| Forma | Ejemplo | Notas |
| ------- | --------- | ------- |
| Posicional | `escapement trabajar 5` | sin cambios; backward compatible |
| Flag con valor | `escapement trabajar --limit 5` · `--limit=5` | `--clave valor` o `--clave=valor` |
| Alias corto | `escapement backlog -n 5` | `-n` = `--limit` |
| Flag antes del texto libre | `escapement objetivo --repo mi_scraper "agrega tests"` | el `--repo` va al frente; la meta queda intacta |
| Escape `--` | `escapement objetivo -- --recupera-el-flag-x` | tras `--`, todo es literal |

- **Flag > posicional > default**. Un valor no numérico donde se espera número
  (`escapement paso abc hecho`) cae al `uso:` o al default, no revienta.
- `backlog`/`vigilar`/`autoevoluciona`/`objetivo`/`ejecutar` aceptan el repo como 1.er posicional
  **o** como `--repo <repo|ruta>`.
- `escapement help <cmd>` o `escapement <cmd> --help` (`-h` en 1.ª posición) imprime uso y flags sin
  ejecutar.

### Lenguaje natural (tools MCP)

Las mismas capacidades, pedidas hablando o escribiendo en el REPL (ej. *"optimiza el scraper de
mercado libre"*):

| Tool MCP | Servidor | Anillo | Efectos | Equivale a |
| ---------- | :--------: | :------: | --------- | ------------ |
| `mcp__memory__recall_memory` | `memory` | 🟢 0 | ninguno | Busca en la memoria markdown de **todos** los proyectos (vault); `exclude_project` para omitir uno. |
| `mcp__semantic__recall_semantic` | `semantic` | 🟢 0 | ninguno | Busca por **significado** (embeddings) cuando el keyword no encuentra nada. |
| `mcp__memory__write_memory` | `memory` | 🟡 1 | crea 1 nota | Crea una nota nueva en el vault (una-idea-por-archivo); `type`: `user`\|`feedback`\|`project`\|`reference`. |
| `mcp__orq__estado` | `orq` | 🟢 0 | ninguno | = `escapement estado` (abre con el plan activo en una línea). |
| `mcp__orq__deuda` | `orq` | 🟢 0 | ninguno | = `escapement backlog`. |
| `mcp__orq__optimizar` | `orq` | 🟡 2 | rama + PR | = `escapement optimiza`. |
| `mcp__orq__vigilar` | `orq` | 🟡 2 | encola tareas | = `escapement vigilar`. |
| `mcp__orq__trabajar` | `orq` | 🟡 2 | procesa cola, abre PRs | = `escapement trabajar`. |
| `mcp__orq__revisar_prs` | `orq` | 🟡 2 | actualiza el ledger | = `escapement revisar`. |
| `mcp__orq__objetivo` | `orq` | 🟡 2 | escribe el plan del repo | = `escapement objetivo`. Solo planea. |
| `mcp__orq__plan` | `orq` | 🟢 0 | ninguno | = `escapement plan`: objetivo, avance y checkpoint pendiente. |
| `mcp__orq__ejecutar` | `orq` | 🟡 2 | edita en un worktree, commits | = `escapement ejecutar` (tarda minutos). |
| `mcp__orq__paso` | `orq` | 🟡 2 | escribe el plan | = `escapement paso <id> <estado>`: la `nota` es tu respuesta. |

El ciclo del plan, hablando — la misma máquina (planner → runner reanudable → checkpoints):

```
tú › planea migrar el reporter a la API nueva
      → objetivo(meta, repo)  ·  roadmap de N pasos, guardado y activo
tú › arranca
      → ejecutar(repo)  ·  corre en el worktree hasta el primer checkpoint
Escapement › paso 3 pregunta: ¿mantengo compatibilidad con la v2?
tú › no, corta la v2
      → paso(id=3, estado="hecho", nota="no mantener v2")  ·  la nota es contexto de los pasos siguientes
tú › sigue      → ejecutar()   ·  reanuda donde quedó
tú › ¿cómo va?  → plan()       ·  solo lectura, sin confirmación
```

Diferencias con la CLI, por vivir en un turno de chat/voz (nada interactivo):

- `ejecutar` **sin repo conocido no corre**: no cae al `cwd` (el daemon de voz vive en `HOME`).
- Al cerrar un roadmap reporta worktree, rama y árbol de archivos, pero **no ofrece el rescate a una
  rama** (esa oferta usa `input()`).
- Una corrida a la vez por proceso (`_RUN_LOCK`), para que dos `ejecutar` seguidos no se pisen al
  persistir el plan.
- **F2 corta también el trabajo, no solo el turno.** Una corrida vive en un hilo
  (`asyncio.to_thread`) que sobrevive al `interrupt()`; el barge-in levanta la señal cooperativa
  [`cancel.RUN`](src/agent/cancel.py) y `run_plan` la consulta **entre pasos** (`should_cancel`): el
  paso despachado termina, el siguiente queda `bloqueado` con su nota y la corrida para ahí.
  Reanudable con otro `ejecutar`; el reporte lo dice y distingue "en curso" de "ya está parando".
- **Progreso visible**: cada paso publica `plan.step_start` y el ícono de bandeja pasa a
  `plan 1/3 · paso 2 [editar]`; al terminar (`plan.done`) vuelve a la fase de voz vigente. Sin
  feedback hablado (el TTS está ocupado con la respuesta). El bus es in-process: un
  `escapement ejecutar` desde otra terminal no mueve el ícono.
- Los textos largos se **recortan solo al mostrarlos** (acción, `done`, objetivo, notas): un roadmap
  real trae acciones de 300-800 chars. El archivo del plan queda intacto.

---

## Trazabilidad y rescate (worktrees)

Los pasos corren en un **worktree aislado** (`plan.workdir`, rama `escapement/plan-<stamp>`): el repo
real no se toca hasta que **tú** mergeas. Al terminar cada plan (`ejecutar`, `memoria`, `repaso`)
Escapement cierra con dos cosas.

**1. Traza en árbol** — unión de lo commiteado en la rama del plan y lo que sigue sin commitear en el
worktree:

```
[traza] archivos modificados por el plan:
  worktree: C:\...\escapement-plan-8jqgqk17\wt
  rama:     escapement/plan-20260713-111346
  ├─ integrations/
  │  └─ relay_storage.py
  ├─ storage/
  │  ├─ azure_blob.py
  │  └─ s3.py
  └─ tests/
     └─ test_s3.py
```

En un checkpoint la traza muestra el avance parcial; el rescate solo aparece con el roadmap completo.

**2. Rescate a una rama de entrega** — traspasa el delta como **un commit limpio** a una rama nueva
basada en la rama por defecto del repo:

```
[rescate] ¿traspasar el trabajo a una rama nueva basada en 'main'? [s/N] s
  ✓ trabajo traspasado a la rama 'escapement/entrega-integrar-s3-20260716-…' (basada en 'main').
    revísalo:  git -C <repo> checkout escapement/entrega-integrar-s3-…
```

- Siempre a una rama nueva (`escapement/entrega-<slug>-<stamp>`); no commitea sobre una rama
  protegida ni sobre la base.
- Antes de traspasar commitea lo pendiente en la rama del plan → nada se pierde si el traspaso falla.
- Si el delta **no aplica limpio** sobre la base, no crea la rama y avisa: el trabajo sigue intacto
  en el worktree.
- El prompt solo se hace con TTY; headless/voz imprime el comando manual en vez de bloquear.

API en [`runner.py`](src/agent/runner.py): `archivos_del_plan(plan)`, `arbol_archivos(rutas)` (pura),
`default_branch(repo)` y `traspasar_a_rama(plan, repo, *, base, dest)`.

---

## El gate de seguridad

Un único hook `PreToolUse` ([`security/guard.py`](src/agent/security/guard.py)) corre **antes que
cualquier otra regla** — deniega incluso tools ya presentes en `allowed_tools`:

| # | Regla | Dispara con |
| :-: | ------- | -------------- |
| 1 | ⛔ SQL destructivo **en ejecución** (`UPDATE`/`INSERT`/`DELETE`/`ALTER`/`GRANT`/`DROP`/`TRUNCATE`) | `Bash` + indicador de ejecución (`cursor`, `.execute`, `psql`, `sqlalchemy`, ...) — no un `grep` sobre el texto de la query |
| 2 | ⛔ `git commit` / `git push` sobre rama protegida | `main` y `master`, más las de `[git] ramas_protegidas` |
| 3 | ⛔ Leer o escribir archivos `.env` / `.env.*` | `Read`, `Grep`, `Glob`, `Write`, `Edit`, `MultiEdit`, y comandos de shell que muestran contenido (`cat`, `type`, `Get-Content`, ...) |

`main` y `master` están protegidas siempre. Una rama de integración propia se declara en
`escapement.toml`:

```toml
[git]
ramas_protegidas = ["integracion"]
```

La clave **solo añade**: no hay forma de desproteger `main`/`master` desde el archivo ni desde
`AGENT_PROTECTED_BRANCHES`. La misma lista alimenta los candidatos a rama base de una entrega
(`runner.default_branch`), que prueba las protegidas y luego `develop`.

Lo que no choca con estas reglas y no está en Anillo 0 cae a
[`security/permissions.py`](src/agent/security/permissions.py) (`confirm_action`): pide
`¿Autorizar? [s/N]` con el detalle de la acción, y es **fail-safe** — sin TTY, deniega.

### Detección de secretos antes de despachar (P1)

Distinta del gate (que vigila *tools*), corre **dentro del orquestador**, justo antes de que
`optimizar`/`trabajar` manden un archivo al motor externo:
[`security/secrets.py`](src/agent/security/secrets.py) escanea el target por secretos **literales**
(connection strings con credenciales, claves PEM, AWS access keys, tokens con prefijo reconocible,
`password = "..."`). Si encuentra alguno **aborta el despacho sin PR**. No marca referencias a
env vars (`os.getenv("X_PASSWORD")`) ni placeholders obvios. El evento queda en el ledger (`blocked_secrets`).

---

## Memoria

Cuatro tipos, cada uno con su sustrato y su anillo:

| Tipo | Qué guarda | Dónde vive | Acceso |
| ------ | ------------ | ------------ | -------- |
| **Conocimiento** (vault) | Notas markdown una-idea-por-archivo: decisiones, flujos, gotchas; cross-proyecto | `~/.claude/projects/*/memory/` (fuente externa, no incluida en el repo) | lectura: `recall_memory` / `recall_semantic` (🟢 0) · escritura: `write_memory` (🟡 1) |
| **Operacional** (ledger) | JSONL append-only de cada tarea del orquestador: directiva, repo, rama, PR, tests, accept/reject | `config.LEDGER` → `~/.claude/projects/<agent-slug>/ledger.jsonl` | interna, vía [`ledger.py`](src/agent/ledger.py) |
| **De estado** (cola) | Cola persistente de tareas + estado de cuota (reanudable) | `~/.claude/projects/<agent-slug>/state.json` | interna, vía [`state.py`](src/agent/state.py) |
| **Conversacional** (historial) | JSONL append-only de cada turno usuario↔agente, texto y voz, local o Claude | `config.CONVERSATIONS` → `~/.claude/projects/<agent-slug>/conversations.jsonl` | interna, vía [`convlog.py`](src/agent/convlog.py); `escapement historial [N]` |

- `recall_memory` reusa `~/.claude/tools/recall_memory.py` vía subprocess (única fuente de verdad).
- `recall_semantic`: embeddings ONNX (`fastembed`, sin torch) + similitud coseno en memoria;
  complementa el recall por keyword cuando este no encuentra nada.
- `write_memory` crea notas con el mismo patrón, acotado al vault, nunca toca código (Anillo 1).
- `ledger.stats()` agrega el JSONL en métricas —total, tasa de éxito, duración promedio, despachos
  bloqueados por secretos, distribución por executor— y `escapement estado` la imprime (línea `[obs]`).

---

## Voz (F2) — daemon residente

`escapement --voz` arranca un daemon residente que convive con el REPL de texto sin tocar el cableado
de permisos (mismo gate, misma confirmación, solo que hablada). Arranca ligero (solo el hook F2; con
wake word activa también mantiene el micrófono abierto) y por turno:

1. **Idle → active**: F2 carga STT/TTS y reanuda el hilo por su `session_id`, persistido en
   `AGENT_HOME/voice_session.json` (sobrevive reinicios del proceso).
2. **Active → idle**: tras 10 min sin F2 (`FREEZE_AFTER`) descarga los modelos y cierra el cliente
   para liberar VRAM; el hilo se conserva en disco.
3. **Hilo nuevo**: "nuevo tema" / "olvida todo", o 12 h de inactividad (`NEW_THREAD_AFTER`).

| Pieza | Implementación | Notas |
| ------- | ----------------- | ------- |
| Captura | [`voice/ears.py`](src/agent/voice/ears.py) | `VOICE_PTT_MODE` (`AGENT_VOICE_PTT_MODE` / `[voice] ptt_mode`): **`toggle`** (default) — un toque abre, cierra a ~2 s de silencio (endpointing RMS adaptativo) o al tope (90 s; 15 s si nunca hubo voz), y un segundo toque cancela el turno; **`hold`** — push-to-talk clásico |
| Sesión abierta | `ears.record_sesion` + `daemon._escuchar_en_sesion` | **Default ON**: F2 abre una conversación, no un turno. El micrófono reabre solo tras cada respuesta (chime), con corte a ~1.6 s de silencio, tope 90 s y cierre automático a los 45 s sin voz. F2 con el micrófono abierto cierra y descarta lo arrastrado (pausar desde el tray también); F2 en otras fases es barge-in y **no** cierra. `AGENT_VOICE_OPEN_SESSION=0` / `[voice] open_session = false` = una pulsación por turno |
| Wake word (opcional) | openWakeWord ([`voice/wakeword.py`](src/agent/voice/wakeword.py)) | Manos libres: la frase abre un turno sin F2 (chime, grabación hasta ~1.2 s de silencio, VAD silero en el STT). **Apagada por default** (`VOICE_WAKE_WORD = ""`); `AGENT_VOICE_WAKE_WORD=hey_jarvis` o `[voice] wake_word` (preentrenados `hey_jarvis`/`alexa`/`hey_mycroft`, o ruta a un `.onnx`). Detección local en CPU, modelo <1 MB descargado en el primer uso. Sin `openwakeword` degrada a solo F2. Sensibilidad: `AGENT_VOICE_WAKE_THRESHOLD` (0.5) |
| STT | `faster-whisper` `large-v3-turbo` ([`voice/stt.py`](src/agent/voice/stt.py)) | CUDA/`int8_float16` (~1.2 GB de VRAM) con fallback automático a CPU/int8; idioma fijo en español (`VOICE_LANG`), sin detección. Configurable con `AGENT_VOICE_STT_MODEL`/`_DEVICE`/`_COMPUTE` o `[voice]` (env > archivo > default). Los `distil-*` no aplican (solo inglés) |
| Precalentamiento | `stt.prewarm()` desde `ears._abrir_escucha` | Abrir el micrófono lanza la carga en un hilo aparte, solapándola con lo que tardas en hablar (sin esto, el primer turno tras congelar pagaba ~5 s). Aplica a los tres modos y solo si el micrófono llegó a abrirse. Idempotente y best-effort; `_carga_lock` serializa la carga —`lru_cache` no lo hace, y sin él quedarían dos copias del modelo en VRAM |
| TTS | Kokoro-82M ([`voice/tts.py`](src/agent/voice/tts.py)) | voz `ef_dora` (es); fallback SAPI nativo en Windows |
| Prioridad de F2 | [`voice/hotkey.py`](src/agent/voice/hotkey.py) — `LATCH` | Un **único** hook de teclado para todo el proceso: la pulsación queda en un flag que la captura consume (abre el turno siguiente sin esperar) y el daemon consulta sin consumir (para cortar lo que esté haciendo). Es flag, no contador: tres toques abren un turno. La captura lo limpia en sus dos bordes |
| Arrastre de contexto | `daemon._fusionar` | F2 mientras transcribe/piensa/habla no tira lo ya dicho: se acumula y viaja con el turno siguiente (separado por punto si no traía cierre). Se descarta al congelar y al cerrar el micrófono con F2 |
| Barge-in | `tts.speak(..., stop=...)` + `daemon._barge_in_pedido` / `_consumir_interrumpible` | F2 mientras responde corta **el turno entero**: aborta la cola de habla (`cancel`), llama `client.interrupt()` y vuelve a escuchar (Kokoro vía `sd.stop`, SAPI vía async+purga), publicando `voice.barge_in`. La condición mira la tecla **o** el latch ([`ears.hotkey_solicitada`](src/agent/voice/ears.py)), salvo con el micrófono reservado (`ears.escucha_exclusiva`). `stop=None` = no interrumpible; `VOICE_BARGE_IN = False` lo desactiva. Best-effort: si el hook falla, degrada a espera bloqueante |
| Habla en streaming | [`voice/speech.py`](src/agent/voice/speech.py) — `SentenceBuffer` + `SpeechQueue` | Habla desde la **primera frase**: el daemon pide los deltas (`include_partial_messages`, solo en voz), los parte en frases y una cola las reproduce en orden en otro hilo. No se hablan `thinking_delta` ni `input_json_delta`. Si el turno se cancela, la cola se aborta (`cancel()`) en vez de cerrarse. `AGENT_VOICE_STREAM_TTS=0` / `[voice] stream_tts = false` = una sola reproducción al final |
| Confirmación hablada | [`voice/interaction.py`](src/agent/voice/interaction.py) — `VoiceGate` | Sí/no por voz, **una vez por categoría** (archivos / terminal / memoria / orquestador), con `reset()` al cambiar de tema. Describe la acción en lenguaje natural. Corre con el micrófono reservado para que la F2 con la que contestas no dispare el barge-in del turno que pide el permiso; al terminar republica la fase `pensando`. Fail-safe: si no entiende, no escucha o expira, deniega |
| Instancia única | [`singleton.py`](src/agent/singleton.py) | Bind a un puerto loopback: un segundo `--voz` detecta la instancia viva y sale (sin tray/hotkey/modelos duplicados) |
| Sin ventanas fantasma | [`voice/_nowindow.py`](src/agent/voice/_nowindow.py) | Parchea `subprocess.Popen` con `CREATE_NO_WINDOW`: bajo `pythonw` evita el parpadeo de CMD en cada conexión del CLI de Claude |
| Daemon residente | [`voice/daemon.py`](src/agent/voice/daemon.py) | idle/active, hilo persistente, señales de "nuevo tema" y timeouts |
| Bandeja | [`voice/tray.py`](src/agent/voice/tray.py) | Ícono + menú ("Salir", "Pausar escucha", "Nuevo tema", ...) en hilo aparte. El color sigue `voice.capture`: 🔵 reposo · 🔴 micrófono abierto · 🟠 transcribiendo · 🟣 pensando · 🟢 hablando — en `toggle` la tecla no queda presionada, así que es la única señal de que sigue grabando. Durante una corrida (`plan.*`) el tooltip cuenta los pasos. Si el ícono falla al repintar, el turno sigue |
| Diagnóstico de micrófono | [`voice/miccheck.py`](src/agent/voice/miccheck.py) | `python -m agent.voice.miccheck` lista las entradas y marca cuál usa la captura (la de defecto de Windows: `ears` abre el stream sin `device=`). `--grabar 6` da medidor en vivo con el **mismo umbral** que `record_toggle` (importa los parámetros de `ears`): `MUDO` · `BAJO` (se descarta sin transcribir) · `JUSTO` (<6 dB) · `OK`. `--stt` transcribe lo grabado |
| Autoarranque | [`scripts/escapement-voz.vbs`](scripts/escapement-voz.vbs) + [`scripts/install-startup.vbs`](scripts/install-startup.vbs) | Doble clic deja un acceso directo en `shell:startup`; `uninstall-startup.vbs` lo quita. El launcher corre `.venv\Scripts\pythonw.exe -m agent.cli --voz` con la ventana oculta; no pasa por `uv run` a propósito (evita el sync y el lock del `.exe`). Sin venv avisa con un diálogo |

#### Ciclo de un turno y qué hace F2 en cada fase

El ciclo rota `inactivo → escuchando → transcribiendo → pensando → hablando → inactivo`.

| Fase (color) | F2 hace | Sesión | Qué pasa con lo ya dicho |
| -------------- | --------- | -------- | -------------------------- |
| 🔵 `inactivo` | abre el micrófono | se abre (con `open_session`) | — |
| 🔴 `escuchando` | **cierra**: el micrófono ya estaba abierto | se cierra | se descarta, junto con lo arrastrado |
| 🟠 `transcribiendo` | queda en el latch; al terminar el STT el micrófono reabre sin esperar | sigue abierta | se acumula y viaja con el turno siguiente |
| 🟣 `pensando` | aborta el turno del modelo (`client.interrupt()`) y reabre el micrófono | sigue abierta | se guarda en el log; lo pedido se arrastra |
| 🟢 `hablando` | calla el TTS (`cancel`), aborta el turno y reabre el micrófono | sigue abierta | igual que arriba |

La fila 🔴 es la misma dentro y fuera de una sesión abierta: con el micrófono abierto un toque
descarta el audio (`ears.CANCELADO`) y no abre nada. Interrumpir (🟣/🟢) **sí** arrastra; para
desactivarlo, quitar los `pendiente = _fusionar(...)` de las salidas por barge-in en
[`daemon.py`](src/agent/voice/daemon.py).

Cortes que no dependen de F2: ~2 s de silencio, 90 s de tope duro, 15 s si nunca hubo voz; en sesión
abierta, ~1.6 s por turno, 90 s de tope y 45 s sin voz para cerrar la sesión. El arrastre se descarta
al congelar (`FREEZE_AFTER`) y al cerrar la sesión.

> Para detenerlo: "Salir" en la bandeja, o finalizar `pythonw.exe`. El lock de instancia única evita
> que el autoarranque y un `--voz` manual levanten dos daemons.

---

## Bus de eventos

[`bus.py`](src/agent/bus.py) es la Fase 0 del desacoplamiento presencia↔cerebro (rumbo a un frontend
tipo AIRI): pub/sub in-process (stdlib puro, sin servidor ni dependencias) donde los productores
publican por topic y los consumidores se suscriben por prefijo. Los eventos con `journal=True` quedan
en `data/events.jsonl` (append-only, gitignorado), así un proceso externo sigue el flujo leyendo el
archivo — sin FastAPI hasta que exista un consumidor real.

```python
from agent import bus

# Consumidor: prefijo "plan." recibe plan.step y plan.done; "" recibe todo.
cancelar = bus.subscribe("plan.", lambda ev: print(ev.topic, ev.data))

# Productor: persiste en data/events.jsonl y notifica a los suscriptores.
bus.publish("plan.step", {"id": 1, "estado": "hecho"}, source="runner")

# Releer el journal (con filtro por prefijo y límite).
ultimos = bus.read(limit=10, topic="plan.")
cancelar()  # idempotente
```

- **Best-effort siempre**: `publish` nunca lanza — un suscriptor roto o un journal ilegible no tumban
  al productor.
- `journal=False` = solo señal in-process. Lo usa `turn`, cuyo texto ya es durable en
  `conversations.jsonl`.
- Los callbacks corren en el hilo del productor: rápidos y sin bloquear.
- Importar `agent.bus` no toca red ni escribe disco (misma regla que `config`).

| Topic | Payload | Emisor | Journal |
| ------- | --------- | -------- | --------- |
| `turn` | `{backend, session_id, user, reply}` | `convlog.record` (REPL y voz) | no |
| `voice.state` | `{state: "activo" \| "congelado" \| "apagado"}` | daemon de voz | sí |
| `voice.barge_in` | `{}` | daemon de voz, cuando F2 corta al TTS | sí |
| `voice.wake` | `{model}` | daemon de voz, cuando la wake word abre un turno | sí |
| `voice.capture` | `{state: "escuchando" \| "transcribiendo" \| "pensando" \| "hablando" \| "inactivo"}` | [`voice/ears.py`](src/agent/voice/ears.py) publica las dos primeras en sus cuatro rutas; el daemon, las otras dos. Solo al **cambiar** de fase | no |
| `plan.step_start` | `{repo, id, tipo, accion, hechos, total}` | `runner.run_plan`, **antes** de despachar cada paso | no |
| `plan.step` | `{repo, id, tipo, estado, nota}` | `runner.run_plan`, tras cada paso | sí |
| `plan.eval` | `{repo, veredicto, gaps}` | `runner.run_plan`, tras la evaluación global. `veredicto`: `"done"`, `"incompleto"` o `""` (best-effort sin salida usable, también observable); `gaps` solo con `incompleto` | sí |
| `plan.replan` | `{repo, anadidos, ciclo, max}` | `runner.run_plan`, cuando los gaps se volvieron pasos nuevos | sí |
| `plan.done` | `{repo, objetivo, completado}` | `runner.run_plan`, al terminar la corrida | sí |

### `escapement eventos`

Consumidor de referencia del journal: imprime los últimos `N` eventos en orden cronológico, uno por
línea, como `[ts] topic (source) {data}`.

```bash
escapement eventos                    # últimos 20
escapement eventos 50                 # posicional == --limit/-n
escapement eventos --limit 0          # todos (0 = sin tope, semántica de bus.read)
escapement eventos --topic plan.      # plan.step, plan.eval, plan.replan y plan.done (prefijo)
escapement eventos -n 5 -t voice.     # combinables
```

- **Solo lectura**: no publica, no rota ni trunca el journal.
- `--topic` es por **prefijo**, el mismo match que `bus.subscribe` (`plan.` ≠ `plan`: el segundo
  también matchearía `planeta.x`).
- Filtra por topic y **después** aplica el límite: `--topic plan. -n 5` son los últimos 5 de plan.
- Los topics con `journal=False` (`turn`, `voice.capture`, `plan.step_start`) no aparecen; los turnos
  se leen con `escapement historial`.

---

## Tests

```bash
uv run pytest              # toda la suite (aislada: sin API ni Chrome)
uv run pytest -k guard     # solo el gate de seguridad
```

| Archivo | Cubre |
| --------- | ------- |
| `test_cli.py` | dispatch de subcomandos, alias, help global y por comando, typos, el mini-parser en los handlers (flag ≡ posicional, `--help` no ejecuta), `plan ver` y el checkpoint de aprobación (TTY, sin TTY, `--si`, plan ya empezado, flag apagada) |
| `test_cliparse.py` | `split_args` + `arg_int`/`opt_int`/`opt_str`/`has_flag`: posicionales vs flags, escape `--`, `stop_at_positional`, coerción sin crashear |
| `test_config.py` | rutas, anillo 0, resolución de env vars, precedencia `VOICE_STT_*`, `VOICE_WAKE_*` y `PLAN_APPROVAL` (env > toml > default) |
| `test_guard.py` | las 3 reglas duras del gate (SQL, ramas protegidas, `.env`) |
| `test_secrets.py` | detector de secretos hardcodeados (connection strings, PEM, AWS keys, tokens, passwords) |
| `test_memory.py` | `recall_memory` / `write_memory` (lógica pura) |
| `test_orchestrator_tools.py` | tools MCP del orquestador y la cancelación de `ejecutar` (limpia la señal al arrancar, la pasa como `should_cancel`, el reporte avisa solo si quedó cancelada, y la segunda corrida distingue "en curso" de "ya está parando") |
| `test_router.py` | router híbrido local/online; `/no_think` condicional por modelo (qwen3 vs qwen2.5) |
| `test_local_models.py` | backend local in-process + auto-selección por hardware (VRAM, veto de voz, `complete` que nunca lanza) |
| `test_backlog.py` | scanner de deuda técnica (AST+regex), exclusión de directorios |
| `test_executors.py` | registry de motores pluggables y sus builders de comando |
| `test_verify.py` | baseline + diff de API pública |
| `test_judge.py` | juez adversarial, incluido el voto de mayoría (`AGENT_JUDGE_VOTERS`) |
| `test_evals.py` | métricas de mejora (type hints, docstrings, ruff) |
| `test_review.py` | `revisar` / `revisar_prs` y aprendizaje accept/reject |
| `test_ledger.py` | `ledger.stats()`: tasa de éxito, duración, secretos bloqueados, por executor |
| `test_bus.py` | pub/sub por prefijo, journal `events.jsonl`, best-effort (suscriptor roto / journal ilegible), cableado `convlog.record` → evento `turn` |
| `test_cancel.py` | señal cooperativa (`cancel.RUN`): arranca limpia, `pedir` guarda el motivo, `limpiar` borra bandera y motivo, el último motivo gana, y la bandera cruza de hilo |
| `test_daemon.py` | persistencia de sesión (guardar/cargar, expiración a las 12 h), barge-in en `_say`, semántica de F2 por fase (latch, micrófono reservado, `CANCELADO` sin transcribir, qué arrastra y qué no), habla en streaming, `_esperar_turno` (captura + STT en dos etapas, sentinel de timeout, `voice.wake`), `_fusionar` y prioridad de F2 en `_consumir_interrumpible` |
| `test_speech.py` | `SentenceBuffer` (deltas parciales, varias frases por delta, decimales y abreviaturas, piso de longitud, `flush`) y `SpeechQueue` (orden, barge-in, excepción aislada, `close`/`cancel` idempotentes) |
| `test_ears.py` | `hotkey_pressed` (hook roto → False sin lanzar), `record_until_silence` y `listen_hands_free`, `record_toggle` (corte por silencio, cancelación con el segundo toque, topes, latch), reserva del micrófono (`escucha_exclusiva`: anidamiento, libera ante excepción), fases de `voice.capture` y disparo del precalentamiento |
| `test_stt_prewarm.py` | `prewarm()` con un `lru_cache` falso: carga en segundo plano sin bloquear, no-op si ya está o hay otra carga, un turno concurrente no duplica, un fallo no se propaga, `cargado()` no dispara y `unload()` espera al prewarm en vuelo |
| `test_miccheck.py` | el análisis del diagnóstico (puro, sin audio): la calibración no cuenta como voz, umbral acotado, veredictos `MUDO`/`BAJO`/`JUSTO`/`OK`, margen en dB y el medidor que no desborda |
| `test_tray.py` | el ícono sigue `voice.capture` (color/tooltip por fase, sin repintar de más, ícono roto no tumba al productor) y `plan.*` (tooltip `n/total · paso`, sin tocar el color, `plan.done` restaura la fase) |
| `test_hotkey.py` | latch: flag y no contador, `take`/`clear`/`pending`, `key` sigue a config, `start`/`stop` (engancha una vez, ignora un unhook roto), `wait` con pulsación previa; latch único compartido por `ears` y `daemon` |
| `test_interaction.py` | `VoiceGate`: aprobar-una-vez-por-categoría, `reset()`, fail-safe; `_ask_voice` reserva el micrófono de punta a punta, deja la fase en `pensando` y no autoriza si la respuesta llegó cancelada |
| `test_singleton.py` | lock de instancia única (bind a puerto loopback) |
| `test_nowindow.py` | parche `CREATE_NO_WINDOW` sobre `subprocess.Popen` |
| `test_tts.py` | síntesis Kokoro/SAPI, cola de silencio final, barge-in (corta/termina/lanza, degradación, purga SAPI), chime de wake word |
| `test_wakeword.py` | detección con reset, timeout por score bajo, F2 gana durante la detección, degradación a solo hotkey, `_detector()` cachea None |
| `test_integration.py` | pipeline end-to-end de `optimize`/`run_queue` sobre un repo git de fixture (solo mockea dispatch y juez) |

---

## Configuración

Variables de entorno (ver `.env.example`; el `.env` real nunca lo lee el agente, lo bloquea el gate):

| Variable | Default | Qué controla |
| ---------- | --------- | -------------- |
| `ANTHROPIC_API_KEY` | — | auth de Claude (o la sesión de Claude Code ya logueada) |
| `AGENT_NAME` | `Escapement` | nombre de marca; deriva `AGENT_SLUG` |
| `AGENT_USER_NAME` | *(vacío = "tu usuario")* | cómo te nombra en sus system prompts (`[usuario] nombre`) |
| `AGENT_USER_PROFILE` | *(vacío)* | aposición con tu oficio (`[usuario] perfil`): "el asistente personal de Ada Lovelace **(analytical engines)**" |
| `AGENT_PROJECTS_DIR` (legacy `ESCAPEMENT_PROJECTS_DIR`) | `~/.claude/projects` | raíz del vault de memoria |
| `AGENT_RECALL_SCRIPT` (legacy `ESCAPEMENT_RECALL_SCRIPT`) | `~/.claude/tools/recall_memory.py` | motor de `recall_memory` (subprocess) |
| `AGENT_EXECUTOR` | `claude` | motor que aplica los refactors y juzga: `claude`, `antigravity` o `cursor` (ver [Motor de ejecución](#motor-de-ejecución-executor)) |
| `AGENT_EXECUTOR_TIMEOUT` | `5400` | techo de **un** dispatch, en segundos (piso 60). Al cortar, el runner marca el paso fallido y lo no commiteado queda suelto en el worktree |
| `AGENT_JUDGE_VOTERS` | `1` | jueces adversariales que votan cada diff antes del PR. Impar >1 (p.ej. `3`) activa voto por mayoría: reduce el falso-SEGURO a costa de N× cuota; empate o mayoría RIESGOSO bloquea |
| `AGENT_EXECUTOR_MODEL` | — | fija UN modelo para **todo** dispatch (`--model <id>`). Gana sobre el tiering |
| `AGENT_MODEL_TIERING` | `0` | tiering de modelo por tipo de tarea (ver [Tiering](#tiering-de-modelo-por-tarea)). `1`/`true` = ON |
| `AGENT_LOCAL_LLM_ENABLED` | `1` | activa **todo** el backend local (router de voz + ruteo del orquestador). `0` = todo va a Claude |
| `AGENT_LOCAL_ORCH_DISABLE_ON_VOICE` | `1` | **veto de GPU**: en sesión por voz el orquestador va 100% online (Whisper+Kokoro ocupan la VRAM). `0` = sin veto |
| `AGENT_LOCAL_AUTO_SELECT` | `1` | elige modelo local ligero/medio según VRAM libre (`nvidia-smi`). `0` = siempre el ligero |
| `AGENT_LOCAL_LLM_TIMEOUT` | `60` | tope duro (s) de una request local; si Ollama se cuelga, el paso cae a Claude |
| `AGENT_PLAN_APPROVAL` | `1` | checkpoint humano antes de la primera corrida (`[plan] aprobacion`) y checkpoint inline a mitad. Solo en TTY; `memoria`/`repaso` y la tool MCP no pasan por esta puerta. `--si` lo salta por corrida |
| `AGENT_PLAN_EVAL` | `1` | evaluación global al cerrar el plan (`[plan] evaluacion`). Best-effort: sin veredicto usable, cierre por conteo. `0` = comportamiento previo exacto |
| `AGENT_PLAN_REPLAN_MAX` | `2` | ciclos de replanificación automática por plan (`[plan] replan_max`). El contador (`replan_ciclos`) persiste en el JSON. `0` apaga solo la replanificación; basura o negativo = default |
| `AGENT_VOICE_STT_MODEL` | `large-v3-turbo` | modelo faster-whisper (`[voice] stt_model`). `large-v3` usa ~3 GB de VRAM vs ~1.6 GB del turbo; `distil-*` no sirven (solo inglés) |
| `AGENT_VOICE_STT_DEVICE` | `cuda` | device del STT (`cuda`/`cpu`); con `cuda`, fallback automático a CPU/int8 |
| `AGENT_VOICE_STT_COMPUTE` | `int8_float16` | compute_type de ctranslate2 (~1.2 GB con el turbo). `float16` usa ~2.2 GB: en una GPU de 8 GB compartida con el escritorio, CUDA desborda a RAM y una transcripción de 6 s salta de ~1 s a 17-60 s |
| `AGENT_VOICE_WAKE_WORD` | *(vacío = apagada)* | wake word (`[voice] wake_word`): modelo preentrenado de openWakeWord (`hey_jarvis`, `alexa`, `hey_mycroft`, ...) o ruta a un `.onnx`. Vacío = solo F2 |
| `AGENT_VOICE_WAKE_THRESHOLD` | `0.5` | score mínimo (0-1) para dar la wake word por detectada (`[voice] wake_threshold`) |
| `AGENT_VOICE_STREAM_TTS` | `1` | habla por frases mientras el modelo escribe (`[voice] stream_tts`). `0` = una sola reproducción al final |
| `AGENT_VOICE_OPEN_SESSION` | `1` | sesión abierta (`[voice] open_session`): F2 abre una conversación, no un turno. `0` = una pulsación por turno |

### Tiering de modelo por tarea

Por defecto cada dispatch usa el modelo por defecto del CLI. Con `AGENT_MODEL_TIERING=1` (o
`[modelos] tiering = true`) Escapement asigna el modelo **según el trabajo**: los tres se definen en
[`config.py`](src/agent/config.py) (`MODEL_MAIN`/`MODEL_HEAVY`/`MODEL_CHEAP`) y el mapa por tipo en
`_TIER_POR_TIPO`.

| Tier | Constante (default) | Tareas |
| ------ | --------------------- | -------- |
| **HEAVY** | `MODEL_HEAVY` = `claude-opus-5` | cualquier paso con persona de razonamiento duro (`seguridad`/`arquitecto`/`optimizador`/`depurador`/`migraciones`/`datos`) |
| **MAIN** | `MODEL_MAIN` / `MODEL_PLANNER` = `claude-sonnet-5` | `planner` (fijo); `editar`/`crear`/`investigar`/`ejecutar`/`verificar`/`reflexionar` + ramas de `swarm` + el juez adversarial |
| **CHEAP** | `MODEL_CHEAP` = `claude-haiku-4-5-20251001` | clasificación barata: `memoria`, descomposición del `swarm`, divergencia de `preguntar` |

Precedencia:

- El **planner** corre siempre en `MODEL_PLANNER`: su descomposición gobierna el plan entero, así que
  no depende del tiering ni del ruteo local. El ruteo se aplica **después**, paso por paso.
- Una **persona dura** sube su paso a HEAVY aunque el tipo sea barato.
- `AGENT_EXECUTOR_MODEL` **gana** sobre el tiering, y el planner —que ignora el tiering— sí lo
  respeta: es un martillo manual para probar un modelo end-to-end. Solo aplica a `claude`;
  `agy`/`cursor` no exponen selección de modelo por flag.
- **Fable está desactivado**: un `claude-fable-*` en `AGENT_EXECUTOR_MODEL` lo normaliza
  `config._sin_fable()` a `MODEL_HEAVY`, en silencio (importar `config` no debe imprimir ni fallar).
  Para reactivarlo basta borrar `_sin_fable` y su llamada.

```bash
AGENT_MODEL_TIERING=1 uv run escapement ejecutar mi_backend   # persona dura→Opus 5, notas→Haiku 4.5
```

### Ruteo local / online en el orquestador

El tiering reparte entre modelos de Claude. Un paso más: **por default y sin flag de activación**, el
orquestador reparte los pasos de **razonamiento read-only** entre el LLM local (Ollama/Qwen, sin
egress) y Claude según la dificultad. Para apagarlo todo (voz incluida): `AGENT_LOCAL_LLM_ENABLED=0`.

**Qué puede ir a local** (Fase 1, razonamiento sobre texto ya en el prompt, sin tocar disco):
`reflexionar` (replan), la descomposición del `swarm` y el triage de `preguntar`. La
**edición/creación/ejecución de código NUNCA es local**. `investigar`/`verificar` (que exploran el
repo) quedan tras `AGENT_LOCAL_ORCH_TOOLS` (Fase 2, tool-loop local, default OFF).

La decisión la toma [`config.route_step()`](src/agent/config.py), de mayor a menor fuerza:

| Regla | Resultado |
| ------- | ----------- |
| **1.** Sesión por **voz** y veto activo | `cli` — no competir por la VRAM con Whisper+Kokoro |
| **2.** Persona pesada (`seguridad`/`arquitecto`/…) | `cli` (Opus 5) |
| **3.** Tipo no elegible (`editar`/`crear`/`ejecutar`/`planner`) | `cli` |
| **4.** Señal de dificultad en el texto (regex del router, o turno largo) | `cli` |
| **5.** Clase B (`investigar`/`verificar`) sin `LOCAL_ORCH_TOOLS`, o con shell/build | `cli` |
| **6.** Local disponible | `local` (modelo por hardware) · si no, `cli` (fallback duro) |

- **Selección por hardware**: [`local_models.select_local_model()`](src/agent/local_models.py) lee la
  VRAM libre con `nvidia-smi` y elige entre el ligero (`qwen2.5:3b`) y el medio
  (`qwen2.5-coder:7b`). Sube al medio solo con VRAM holgada (`vram_medium_mb`, default 5600), voz
  inactiva **y** confirmación de que el medio está descargado (`/api/tags`, cacheada 5 min) — esa
  verificación evita que cada paso 404ee para caer a Claude. `AGENT_LOCAL_AUTO_SELECT=0` fija el ligero.
- **Veto de voz (determinista)**: los entrypoints de voz marcan la sesión
  (`config.set_voice_session(True)`) y `route_step` fuerza todo el orquestador a Claude. No adivina
  por VRAM: es una bandera de proceso. La charla de voz trivial **sigue** en el local ligero
  (`router.ask_local`); lo vetado es *añadir* razonamiento del orquestador encima.
- **Fallback de calidad**: si el local responde pero la salida no valida (parseo real de
  `_parse_pasos`/`_parse_subtareas`, o la señal `DIVERGENCIA:`/`SEGUIR:` tras un posible preámbulo),
  el paso se reintenta en Claude — sin pérdida de corrección, solo del ahorro. Los dispatches locales
  no consumen budget (`costs.add_local`): van aparte en el ledger (`local_dispatches`, `est_local_*`).
- **Telemetría**: el roll-up `plan_cost` distingue los pasos resueltos en local (contabilizados solo
  cuando la salida se *usa*) de los que cayeron a Claude (`local_fallbacks` + `local_fallback_motivos`:
  `no_disponible` · `sin_respuesta` · `no_valido`). La tasa de fallback es la señal para calibrar los
  umbrales antes de ampliar el uso local.
- **Robustez**: cada request local tiene tope duro (`AGENT_LOCAL_LLM_TIMEOUT`, 60 s) y la
  disponibilidad de Ollama se **re-prueba cada 45 s** durante el plan (no se congela como el ping
  one-shot del router de voz): si se cae a mitad, los pasos siguientes van directo a Claude sin pagar
  el timeout; si arranca tarde, se empieza a aprovechar en cuanto responde.

```bash
uv run escapement ejecutar mi_objetivo   # replan/triage triviales → local; lo difícil → Claude
```

### Motor de ejecución (executor)

El orquestador (`optimiza`/`trabajar` y el juez) **no está atado a Claude**: `AGENT_EXECUTOR` elige
el CLI agéntico que aplica los refactors. El resto del pipeline (worktree, verify, eval, PR, cola) es
agnóstico del motor — solo cambia [`executors.py`](src/agent/executors.py).

| Motor | `AGENT_EXECUTOR` | CLI requerido | Invocación headless |
| ------- | ------------------ | --------------- | --------------------- |
| Claude Code (default) | `claude` | `claude` | `claude -p …` (prompt por stdin) |
| Google Antigravity | `antigravity` | `agy` | `agy -p "…" --headless --approve all` |
| Cursor | `cursor` | `cursor-agent` | `cursor-agent -p "…" --force` |

```bash
AGENT_EXECUTOR=antigravity uv run escapement trabajar 1   # despacha el refactor con agy
```

> Cada motor usa su propia suscripción y su propio CLI (instalado y autenticado). Los flags viven en
> `executors.py` como *builders* por motor. El aislamiento en worktree aplica a los tres.

### Personas

Cada dispatch del runner (editar/crear/ejecutar/investigar/verificar/swarm) recibe un **system prompt
de experto**. El registro vive en [`personas.py`](src/agent/personas.py); `system_for(tipo, repo,
persona)` decide cuál aplica, en este orden:

1. **Especialista asignado por el planner (gana).** El planner separa el trabajo por especialidad y
   asigna el experto transversal —`seguridad`, `tester`, `optimizador`, `datos`, `devops`,
   `migraciones`, `documentador`, `refactor`, `depurador`, `arquitecto`, `dependencias`, `frontend`—
   vía el campo `persona`. Son **repo-agnósticos** (`tipos=()`), así que pisan al coder de dominio en
   cualquier repo. Se muestran en el roadmap como `@persona`.
2. **Default por tipo + repo (auto-ruteo).** Sin especialista, el runner elige el coder de dominio
   del repo (`scraper` → `scraping`, `backend` → `backend-app`) o el genérico por tipo
   (`investigador` para explorar, `revisor` para verificar).
3. **`ingeniero` (fallback).** Coder generalista cuando ningún dominio aplica. Auto-enruta
   `editar`/`crear`/`ejecutar`.

El planner solo asigna especialistas; los auto-routers nunca se nombran a mano, así que
`roster_brief()` solo muestra los primeros. `memoria`/`reflexionar` usan su propio system en el runner
y **no pasan por personas**. Una `persona` vacía, desconocida o con nombre de auto-router cae al
auto-ruteo (backward compatible). `escapement agentes` las exporta como subagentes de Claude Code.

#### Ajustar las personas a tus repos (`[personas]`)

El registro del código es el default **público**: prompts de dominio genéricos y **slots** de repo
(`scraper`, `backend`) en vez de nombres concretos. El binding real y el dominio se declaran en
`escapement.toml` (gitignorado), sin tocar el código:

```toml
[repos]
mi_scraper = "D:/mi_scraper"
mi_reporter = "D:/mi_reporter"

[personas.scraping]
repos = ["mi_scraper", "mi_reporter"]          # rebindea el slot a TUS repos
system = "Eres un experto en scraping y en el dominio de mi_scraper. … Recuerdas los gotchas reales: …"

[personas."backend-app"]                        # los nombres con guion van entre comillas
repos = ["mi_api"]
```

| Campo | Tipo | Qué hace si lo declaras |
|---|---|---|
| `repos` | lista de str | Nombres de `[repos]` que esta persona atiende. Vacía = genérica (pierde el desempate ante las específicas). |
| `system` | str | El system prompt del dispatch. Aquí va el dominio concreto de TU código. |
| `description` | str | Cuándo usar el agente: frontmatter del subagente de Claude Code y auto-enrutado. |
| `tipos` | lista de str | Tipos de paso que auto-enruta. Vacía = sale del auto-ruteo y queda como especialista asignable. |

Lo que no declares queda en su default y el orden del registro se preserva (`select` desempata por
él). Un nombre inexistente, una clave desconocida, un `system` vacío o un tipo equivocado se
**ignoran en silencio**: un `escapement.toml` mal escrito degrada al default.

---

## Estructura del repo

```text
docs/
└── VISION.md            # propósito + roadmap post-MVP (documento vivo)
scripts/
├── escapement-voz.vbs    # launcher sin consola: pythonw -m agent.cli --voz (residente)
├── install-startup.vbs   # acceso directo en la carpeta de Inicio de Windows (autoarranque)
└── uninstall-startup.vbs # quita el autoarranque
src/agent/
├── cli.py               # subcomandos + dispatch + entry point (main)
├── cliparse.py          # mini-parser común de argumentos: posicionales vs flags
├── repl.py              # REPL de texto (loop conversacional)
├── session.py           # build_options: wiring del gate en capas
├── config.py            # rutas, modelos, executor, Anillo 0 (RING0_TOOLS), config de voz
├── orchestrator.py      # rama aislada + executor headless + verify + PR (nunca mergea)
├── executors.py         # motor pluggable: claude | antigravity | cursor (dispatch + juez)
├── backlog.py           # escáner de deuda técnica (AST+regex, sin LLM) -> candidatos priorizados
├── verify.py            # baseline + diff de API pública -> veredicto
├── judge.py             # juez adversarial: refuta el refactor antes del PR
├── evals.py             # mide la MEJORA (type hints, docstrings, ruff) antes/después
├── ledger.py            # memoria operacional: JSONL append-only + stats() de observabilidad
├── state.py             # memoria de estado: cola persistente + cuota (agente continuo)
├── convlog.py           # historial de conversación: JSONL append-only de cada turno
├── bus.py               # bus de eventos in-process (pub/sub por prefijo) + journal events.jsonl
├── singleton.py         # lock de instancia única (bind a puerto loopback) para el daemon de voz
├── router.py            # router híbrido F1: LLM local (Ollama) vs Claude
├── planner.py           # Fase A: objetivo -> roadmap de pasos con criterios de done
├── runner.py            # Fase B/C: ejecuta el roadmap (reanudable, worktree, auto-evolución)
├── personas.py          # registro de expertos: system_for(tipo, repo, persona) por dispatch
├── prompts/
│   └── system.md        # system prompt base (formatea AGENT_NAME)
├── tools/
│   ├── memory.py        # recall_memory / write_memory (servidor MCP "memory")
│   ├── semantic.py      # recall_semantic (servidor MCP "semantic")
│   └── orchestrator.py  # servidor MCP "orq": tarea suelta (estado/deuda/optimizar/vigilar/
│                        #   trabajar/revisar_prs) + plan (objetivo/plan/ejecutar/paso)
├── security/
│   ├── guard.py         # el gate: hook PreToolUse (SQL, ramas protegidas, .env)
│   ├── permissions.py   # confirm_action: confirmación explícita por consola
│   └── secrets.py       # P1: detecta secretos hardcodeados antes de despachar al executor
└── voice/               # F2: STT (faster-whisper), TTS (kokoro/SAPI)
    ├── daemon.py        # daemon residente: idle⇄active + hilo conversacional persistente
    ├── tray.py          # ícono en la bandeja del sistema (arranca junto al daemon)
    ├── interaction.py   # VoiceGate: confirmación hablada, aprobar-una-vez-por-categoría
    ├── _nowindow.py     # parche CREATE_NO_WINDOW (sin ventanas CMD fantasma bajo pythonw)
    ├── wakeword.py      # gatillo manos libres (openWakeWord); degrada a solo F2 si falta
    └── ears.py, stt.py, tts.py
tests/                   # suite aislada (ver Tests)
```

---

## Licencia

[MIT](LICENSE) © Rodrigo Rojas. Úsalo, modifícalo y redistribúyelo; se entrega **sin garantía**.

El repositorio no incluye credenciales: `.env` y `escapement.toml` están gitignorados y solo se
publican sus `.example`. Los modelos de voz de terceros (openWakeWord, Kokoro) no se redistribuyen
aquí: se descargan en tiempo de ejecución bajo sus propias licencias.
