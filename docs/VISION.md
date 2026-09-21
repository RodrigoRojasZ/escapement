# Escapement — Visión y roadmap

> **Estado: vigente** — documento vivo. Última actualización de contenido: 2026-08-28 (Fase C
> ✅ cerrada — evaluador global + replanificación, deuda #7; el frente pasa a E3 según
> [PLAN_AUTOGUIADO.md](PLAN_AUTOGUIADO.md)).

## Qué es hoy

Un asistente personal **residente** (voz F2 + texto) sobre el Claude Agent SDK, con un
**orquestador de ingeniería** que auto-mejora código: escanea deuda → despacha el refactor a un
motor (Claude / Antigravity / Cursor) en un worktree aislado → verifica (tests + diff de API +
juez adversarial) → mide la mejora → abre un PR para tu revisión. **Nunca mergea.** Memoria en
tres sustratos (vault de conocimiento, ledger operacional, cola de estado) y un gate de
seguridad en capas.

## Hacia dónde va — de "tarea" a "objetivo"

Hoy Escapement es un **martillo especializado**: recibe una tarea bien definida y la ejecuta. La
visión es un **contratista**: recibe un **objetivo** ("logra esto") y lo lleva hasta *done*
—planifica, ejecuta lo que haga falta, verifica su propio trabajo, itera—, deteniéndose en los
checkpoints que importan a pedir tu OK.

El salto es de **ejecutar tareas** → **gestionar proyectos hasta completarlos**.

### El agentic loop

```
OBJETIVO → Planificar → Actuar → Observar → Evaluar → ¿done?
                ^                                  | no
                +-------- Re-planificar <----------+
```

## Cimientos ya construidos (se reusan)

| Rol del agente-objetivo | Componente actual |
|---|---|
| Memoria de trabajo (estado del proyecto) | cola persistente (`state`) + ledger |
| Ejecutor reanudable de pasos | `run_queue` |
| La "mano" que actúa | `dispatch` (aislado, multi-motor) |
| Verificación por paso | `verify` + `eval` + juez adversarial |
| Seguridad de la autonomía | gate (`guard`) + pre-flight de secretos |
| Aprender del resultado | `revisar` (accept/reject → ledger) |

## Componentes faltantes

1. **Planner** — objetivo en lenguaje natural → **roadmap** de pasos editable (con dependencias y criterios de done).
2. **Pasos heterogéneos** — la cola generaliza de "optimize" a `crear / editar / ejecutar / verificar / preguntar`.
3. **Evaluador de progreso y de "done"** — "¿esto acercó al objetivo?" y "¿está completo?".
4. **Loop de control con re-planning** — el bucle que orquesta y ajusta el plan, con **checkpoints humanos** en las decisiones irreversibles.

## El problema más duro: "¿cuándo está DONE?"

Para código, los tests son el criterio de done (por eso el pipeline funciona). Para objetivos
abiertos ("mejora mi Obsidian") el done es difuso, y un agente sin criterio verificable **no
sabe cuándo parar**: se detiene a medias o itera infinito quemando cuota.

> **Principio rector:** cada objetivo arranca definiendo criterios de done **explícitos y
> verificables** (el planner los propone, el humano los aprueba). Empezar por objetivos con done
> medible y escalar hacia los difusos — no al revés.

## Roadmap por fases

| Fase | Entrega | Se apoya en |
|---|---|---|
| **0 · Robustez** ✅ | Observabilidad/telemetría + N-jueces (voto) + recuperación de fallos | ledger, juez, cola |
| **A · Planner** 🟡 validado | `objetivo "…"` → roadmap editable con criterios de done | dispatch (LLM planifica) |
| **B · Executor** ✅ | la cola procesa pasos heterogéneos, verificando cada uno | run_queue |
| **C · Control loop** ← *actual* | evaluar progreso → re-planificar → hasta done | runner autónomo + reflexionar |
| **D · Dominios** | de "código" a Obsidian / apps nuevas / operativa | dispatch generalizado |

## Principios rectores

- **Done verificable primero.** Sin criterio de done medible, no hay autonomía fiable.
- **Autónomo en lo mecánico, consulta solo en las divergencias.** Escapement ejecuta por su cuenta lo
  rutinario (editar, optimizar, correr comandos, verificar, moverse) sin pedir permiso paso a paso;
  se detiene a consultar SOLO ante una divergencia real de caminos —una decisión de rumbo o algo
  irreversible— vía el paso `preguntar` del plan. Frenos estructurales que se conservan: el modelo
  de PRs de `optimize` (código a rama, sin merge) y el guard duro. Trade-off asumido: los dispatch
  headless de los pasos mecánicos corren con permisos plenos (el guard no los cubre).
- **Empezar acotado.** Validar el loop con un objetivo pequeño y de done medible (p.ej.
  *"documenta `worker/` del scraper hasta 100% type hints + docstrings"*) antes de dominios abiertos.
- **Robustez antes que alcance.** Un agente autónomo amplifica cualquier fragilidad del pipeline
  (el "diff vacío" solo apareció al validar en real); observabilidad y verificación son
  prerequisito, no adorno.
- **Jugar estratégicamente entre local y online.** El propósito fundacional de Escapement es repartir
  el trabajo entre modelos **locales** (Ollama/Qwen, sin cuota ni egress) y **online** (Claude)
  según la dificultad, para ahorrar tokens sin sacrificar corrección. Ya operaba en la charla (router
  F1); ahora el **orquestador** también lo hace (`route_step`, **siempre activo, sin flag**): el
  razonamiento read-only barato (replan, descomposición del swarm, triage) puede ir a local; la
  edición/ejecución y todo lo difícil se quedan en Claude — lo mismo que la **asignación del plan**
  (el planner corre siempre online medio/sólido, `MODEL_PLANNER`). Con un **veto de GPU**
  determinista: en sesión por voz (Whisper+Kokoro ya ocupan la VRAM de la 3050) el orquestador va
  100% online. Regla de oro: en la duda, online — un fallback a Claude cuesta cuota, nunca corrección.

## Estado actual

- ✅ Orquestador de refactors validado en real (un repo de scraping en producción); dispatch aislado; P1 secretos.
- ✅ Modo voz residente (daemon idle⇄active, hilo persistente, tray icon, arranque con Windows).
- ✅ **Ruteo estratégico local/online en el orquestador** (`v0.2.0`, **siempre activo, sin flag de
  activación**) — `config.route_step` reparte los pasos de razonamiento read-only (reflexionar,
  descomposición del swarm, triage) entre Qwen local y Claude según dificultad, con auto-selección
  del modelo local por VRAM (`local_models.py`), veto de GPU en sesión por voz, y fallback a Claude
  si la salida no valida. El **planner** queda fuera del ruteo: corre siempre online medio/sólido
  (`MODEL_PLANNER`), porque su descomposición gobierna todo el plan. Para apagar TODO lo local (voz
  incluida) -> `AGENT_LOCAL_LLM_ENABLED=0`. Fase 2 (investigar/verificar con tool-loop local) tras
  `AGENT_LOCAL_ORCH_TOOLS`, aún sin implementar.
- ✅ **Fase 0 (robustez)** — observabilidad (`ledger.stats` + línea `[obs]` en `estado`),
  N-jueces por voto (`AGENT_JUDGE_VOTERS`, self-consistency), y recuperación de fallos en la
  cola (reintentos acotados + estado terminal `fallido`, un fallo ya no tumba el barrido).
- 🚧 **Fase A (planner)** — MVP **validado en vivo** (2026-07-04): `escapement objetivo "documenta
  worker/…"` produjo 8 pasos heterogéneos con criterios de *done* TODOS verificables (mypy
  `--disallow-untyped-defs`, `ruff --select D`/interrogate, `ruff format --check`, suite de tests),
  dependencias correctas y un paso inicial `preguntar` para resolver ambigüedad. Persistido en
  `data/plan.json` (`planner.py`). Solo PROPONE. Falta en A: darle contexto del repo (plan
  específico, no genérico), editar el roadmap y el checkpoint de aprobación antes de la ejecución.
- 🚧 **Fase B (executor)** — MVP: `escapement ejecutar` recorre el roadmap por dependencias
  (`runner.py`, orden topológico, estado persistido en el plan → **reanudable**); automatiza
  `investigar` (dispatch read-only), `editar`/`crear` (vía `optimize`, PR sin mergear) y `memoria`
  (Claude genera las notas → Escapement las escribe con sus funciones), y hace
  **checkpoint humano** en `preguntar`/`ejecutar`/`verificar`. `escapement paso <id> <estado>` pasa
  los checkpoints. Falta: `verificar` que corra el comando solo, y `editar` con target concreto
  (depende del planner-con-contexto de la Fase A).
- ✅ **Fase C (control loop / auto-evolución)** — el runner ejecuta **100% autónomo** (checkpoint
  SOLO en divergencias reales vía `_h_preguntar` evaluador) y **evoluciona el plan en caliente**:
  un paso `reflexionar` revisa lo descubierto y AÑADE al plan mejoras/problemas nuevos
  (`_insertar_pasos`, anti-bucle). Validado en real: en el repo del controller el runner
  descubrió 3 bugs (G1/G2/G3) — la auto-evolución los convierte en pasos accionables en vez de
  solo documentarlos. Cerrada el 2026-08-28 (E2, deuda #7): con el roadmap agotado, el runner
  **evalúa el progreso GLOBAL** contra `criterio_global` (veredicto `done`/`incompleto` + gaps),
  **replanifica** los gaps con tope `AGENT_PLAN_REPLAN_MAX` y checkpoint humano al agotarlo, y
  publica `plan.eval`/`plan.replan` al bus. Validación en real en DEUDAS.md #7.
- 🚧 **Agentes especializados (personas)** — cada paso se despacha a un especialista según su
  tipo+repo (investigador, backend-app para controller/front, scraping para el repo del scraper, revisor;
  `personas.py`) en vez de a un Claude genérico. Sube la calidad del trabajo; extensible.
