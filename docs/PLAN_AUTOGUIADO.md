# Plan auto-guiado — retrospectiva, mejora e innovación

> **Estado: vigente** · Creado: 2026-08-26 · Base: `master` @`1d186b6`, v0.11.0
>
> Este doc es el **rumbo**; la lista viva de deudas sigue siendo [DEUDAS.md](DEUDAS.md) — aquí no
> se duplican diagnósticos, se ordenan. Cuando una etapa se complete, marcarla aquí y actualizar
> DEUDAS.md **en el mismo PR**. Si este doc deja de mandar, cambiar el encabezado a `superado por X`.

---

## 1 · Retrospectiva (qué pasó y qué aprendimos)

### Cerrado desde la auditoría de julio

| Frente | Estado | Evidencia |
|---|---|---|
| Seguridad (auditoría 2026-07-08, 10 hallazgos) | ✅ 100% — QW1–QW4, S1–S4, M1 | [ROADMAP_MEJORA_ESCAPEMENT.md](ROADMAP_MEJORA_ESCAPEMENT.md) §Estado de avance |
| Causa raíz (asimetría de motores) | ✅ resuelta | hook `guard_cli` en headless (S1), worktree por plan (S2), `no_shell` (S3) |
| Voz GPU-neutral | ✅ completo | bus v0.6 → barge-in v0.7 → STT turbo v0.8 → wake word v0.9 → TTS streaming v0.10 |
| Deudas de voz #1 y #2 | ✅ cerradas 2026-08-01 | reserva de micrófono + `cancel.RUN` cooperativo |
| Deuda #5 (`estado` sin plan) | ✅ cerrada — registrada en DEUDAS.md en E0 | `278550b`, [cli.py:138](../src/agent/cli.py#L138) |

### Fases de la visión ([VISION.md](VISION.md))

Fase 0 ✅ · Fase A validada · Fase B ✅ MVP · **Fase C ✅** (2026-08-28: auto-evolución + evaluador
global + replanificación, validados en real; deuda #7 cerrada) · Fase D pendiente.

### Lecciones que gobiernan este plan

1. **Los registros envejecen sin marca.** DEUDAS.md quedó desactualizado (#5) en 25 días; antes le
   pasó a RETRO_DX_PLANES. → deuda #9 sube de prioridad y este doc nace con encabezado de estado.
2. **Dos namespaces de roadmap colisionaron** (QW/S de la auditoría vs QW/S del retro DX). → un solo
   registro vivo (DEUDAS.md) y un solo doc de rumbo (este).
3. **"Validado en real" funcionó** (diff vacío, G1–G3, colisión F2/gate solo aparecieron en uso
   real). → cada etapa cierra con validación en vivo, no solo suite verde.
4. **Quick wins primero + dependencias explícitas** aceleraron el cierre del roadmap de seguridad.
   → mismo método aquí.

### Hueco estructural nuevo (no registrado en DEUDAS.md)

**No hay CI.** Escapement abre PRs y nunca mergea; sin un check automático, toda la carga de
verificación recae en la revisión manual del humano. Para un ciclo auto-guiado es el enabler #1.
→ registrada como deuda #10 en DEUDAS.md (2026-08-26, E0).

---

## 2 · Reglas del ciclo auto-guiado

Cómo trabaja una sesión de Claude (o el propio Escapement) sobre este repo:

1. **Arranque:** leer este doc + [DEUDAS.md](DEUDAS.md). Elegir el ítem abierto de la etapa más
   temprana sin cerrar. No saltarse etapas salvo pedido explícito.
2. **Done verificable antes de empezar** (principio de VISION): si el ítem no tiene criterio
   medible, definirlo primero.
3. **Un PR por deuda.** Rama feature, nunca commit directo a `master`. Nunca mergear.
4. **El done incluye el registro:** actualizar DEUDAS.md (y este doc si cierra una etapa) en el
   mismo PR. Deudas nuevas descubiertas → a DEUDAS.md en el momento, no a docs nuevos.
5. **Checkpoint humano** solo en divergencias de rumbo o cambios de comportamiento observable; lo
   mecánico corre solo.

---

## 3 · Plan por etapas

```
E0 higiene ──► E1 telemetría/control ──► E2 evaluador global (Fase C) ──► E3 ciclo auto-guiado
   (1 sesión)      (2–3 sesiones)             (la deuda grande, #7)          (innovación)
```

### E0 · Higiene del registro — ✅ completada (2026-08-26, 1 sesión)

| Ítem | Deuda | Done |
|---|---|---|
| ✅ Marcar #5 cerrada en DEUDAS.md (evidencia `278550b`) | — | DEUDAS.md refleja el estado real |
| ✅ Encabezado `fecha + estado` en cada doc de `docs/` (ROADMAP_AIRI → `congelado`, ROADMAP_MEJORA_ESCAPEMENT → `completado/histórico`, EVALUACION_RUFLO → `decisión vigente`, VISION → revisado, DEUDAS → `vigente/única lista viva`) — cierra la deuda #9 | #9 | ningún doc sin marca de vigencia |
| ✅ `VOICE_BARGE_IN` vía `_voice_flag` + bloque en `escapement.toml.example` + tests | #3 | barge-in apagable sin editar código |
| ✅ Registrar deuda #10: CI mínimo | — | entrada nueva en DEUDAS.md |

### E1 · Telemetría y control del plan — ✅ completada (2026-08-26, 1 sesión, 4 PRs apilados)

| Ítem | Deuda | Done |
|---|---|---|
| ✅ CI mínimo: workflow que corre `pytest` en cada PR (PR #5) | #10 | todo PR de Escapement llega con check verde/rojo |
| ✅ `escapement eventos [N] [--topic]` sobre `bus.read` (PR #6) | #4 | el journal (`turn`, `voice.*`, `plan.*`) es legible desde el CLI |
| ✅ `plan quitar <id>` / `mover <id> <pos>` / `editar <id> "<accion>"` validando dependencias (PR #7) | #6 | corregir un plan sin editar JSON a mano |
| ✅ Checkpoint inline en TTY (mismo respeto no-TTY que `_aprobar_plan`) (PR de esta deuda, el 4.º del stack) | #8 | reanudar sin salir del proceso |

Con E1 cerrada, la única deuda abierta es la #7 — exactamente el objeto de E2. #4 era su
prerrequisito y ya está entregado.

### E2 · Cierre de Fase C: evaluador global + replanificación — la deuda grande (#7) — ✅ completada (2026-08-28)

Partida en tres pasos incrementales, cada uno con PR propio:

1. ✅ **Evaluar al cerrar** (2026-08-26, PR #10). Cuando el conteo dice "completo", un paso de evaluación
   contrasta el resultado contra `criterio_global` (dispatch read-only vía `route_step`, pseudo-tipo
   `evaluar`, Clase B). Veredicto: `done` o `incompleto + gaps concretos` — persiste en el plan
   (`eval_veredicto`/`eval_gaps`), un `incompleto` no captura la trayectoria en el ReasoningBank.
   Gate `AGENT_PLAN_EVAL` (default ON), best-effort. Detalle en DEUDAS.md #7 → "Avance (E2-1)".
2. ✅ **Replanificar** (2026-08-26, PR #11). Si `incompleto`, los gaps se convierten en pasos nuevos
   vía el mecanismo ya validado de la auto-evolución (`_replanificar` → `_insertar_pasos` con sus
   guardas), y la corrida sigue hasta reevaluar. **Anti-bucle**: tope `AGENT_PLAN_REPLAN_MAX`
   ciclos (default 2, contador persistido en el plan — reanudar no lo resetea) y checkpoint humano
   al alcanzarlo. Detalle en DEUDAS.md #7 → "Avance (E2-2)".
3. ✅ **Telemetría del ciclo** (2026-08-27, PR #12). Topics `plan.eval` / `plan.replan` al bus
   (journal durable, contratos en el docstring de `bus.py`), visibles en `escapement eventos` y en el
   bloque de plan de `estado` (veredicto + gaps + contador de replanificación al cierre). Detalle
   en DEUDAS.md #7 → "Avance (E2-3)".

**Done de la etapa:** un objetivo real acotado (estilo *"documenta worker/ hasta 100% type hints"*)
termina por **criterio**, no por conteo; y un plan deliberadamente insuficiente se replanifica solo,
respeta el tope y frena en el checkpoint. Validación en real obligatoria antes de declarar Fase C ✅.

✅ **Validación en real cumplida (2026-08-28)** con dos escenarios de laboratorio sobre repos
desechables — el detalle con arcos de journal está en DEUDAS.md #7 → "Validación en real":

- **Escenario 1:** roadmap completo con el objetivo incumplido (docstrings multilínea vs criterio
  de una línea) → eval `incompleto` + gap → replan (`ciclo 1/2`) → corrección → eval `done` →
  sello "roadmap completo ✓ · criterio global verificado". Terminó por criterio, no por conteo.
- **Escenario 2:** entregable imposible para el agente (firma en persona del humano, prohibido
  simularla) con `AGENT_PLAN_REPLAN_MAX=1` → eval `incompleto` → replan (`ciclo 1/1`) → segundo
  eval `incompleto` → "Replanificación automática agotada (1 ciclo(s), tope 1): checkpoint humano",
  sin segundo replan y sin firma simulada.

La validación destapó siete deudas nuevas (#11–#17 en DEUDAS.md: continuidad del worktree en el
carril target, verificador con `mode="edit"`, calidad de pasos insertados, guard no-TTY roto en
Windows, contaminación del journal desde tests, repo sin validar en `ejecutar`, guard de efecto
ciego a untracked). Ninguna bloquea el ciclo evaluar→replanificar. **Fase C ✅.**

### E3 · Ciclo auto-guiado — innovación

Con E2 cerrado, el loop `objetivo → plan → ejecutar → evaluar → replanificar → PR` está completo.
La innovación es apuntarlo hacia adentro:

1. **Dogfooding:** `escapement objetivo escapement "<deuda de DEUDAS.md>"` — Escapement paga su propia deuda.
   Primer candidato: una deuda mecánica y acotada, no una estructural.
2. **Backlog = registro:** el planner toma DEUDAS.md como fuente (contexto del objetivo), de modo
   que el registro vivo y el backlog ejecutable sean el mismo archivo.
3. **Cadencia:** sesión de mejora recurrente (manual o programada) que aplica las reglas de §2 de
   punta a punta y deja un PR listo para revisar.
4. **Después, y solo después:** Fase D (dominios no-código) y local Fase 2
   (`AGENT_LOCAL_ORCH_TOOLS`), en ese orden, cada una arrancando por un objetivo con done medible.

---

## 4 · Métricas del ciclo

| Métrica | Fuente | Hoy | Meta |
|---|---|---|---|
| Deudas abiertas en DEUDAS.md | el propio doc | 5 tras E0: #4 #6 #7 #8 #10 | tendencia a la baja, edad < 1 mes |
| Docs sin marca de vigencia | `docs/` | 0 tras E0 | 0 |
| Planes cerrados por criterio (vs conteo) | topics `plan.eval` | 0% (no existe) | 100% post-E2 |
| PRs con check de CI | GitHub | 0% (no hay CI) | 100% post-E1 |
| Tasa de aceptación de PRs de Escapement | `ledger.stats` | (línea `[obs]` de `estado`) | sostenida o al alza |

## 5 · Congelado / fuera de alcance

- **AIRI / avatar / compañero:** congelado hasta GPU ≥16 GB VRAM ([ROADMAP_AIRI.md](ROADMAP_AIRI.md)).
- **FastAPI:** descartado mientras no exista un consumidor real (decisión jul 2026).
- **Feedback audible de corridas largas:** fuera de alcance deliberado (ver deuda #2 cerrada); el
  canal visual (tray) cubre el caso.
