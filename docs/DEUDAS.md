# Deudas técnicas de Escapement — 2026-07-31

> **Estado: vigente** — única lista viva de deudas del repo. Última revisión: 2026-09-21 (Etapa
> E4.3 del [plan auto-guiado](PLAN_AUTOGUIADO.md) **completada**: cerrada **#11**. **Cero deudas
> abiertas**; la lista queda como registro histórico hasta que la próxima validación en real
> destape algo. Antes: 2026-09-20 (E4.2: cerradas **#17**, **#12** y **#13**). Antes: 2026-09-20 (E4.1: cerradas
> **#14, #15 y #16** en el commit `b5aed66`). Antes: 2026-08-28 (Etapa 2
> del [plan auto-guiado](PLAN_AUTOGUIADO.md) **completada**: cerrada #7 — ciclo
> evaluar→replanificar entregado en PRs #10/#11/#12 y validado en real; alta de #11–#17, hallazgos
> de esa validación. Antes: E1 cerró #10, #4, #6 y #8; E0 cerró #3, #5 y #9 con alta de #10.
> Deudas abiertas: #11–#17).

Lista **reconstruida desde cero**. Reemplaza a `docs/RETRO_DX_PLANES.md` (2026-07-18), que quedó
obsoleta: desde entonces cambió el sistema de voz (barge-in, TTS en streaming, sesión abierta, wake
word, latch de hotkey) y el flujo de fases (bus de eventos de la Fase 0, checkpoint de aprobación
del plan). Varias deudas de esa lista ya no existen o se resolvieron por otro camino; ver
[Cerrado](#cerrado-ya-no-son-deuda) al final.

**Método:** lectura directa del código en `master` @`216099d`, sin heredar conclusiones del reporte
anterior. Suite de tests en verde al momento de auditar.

**Orden:** por área (voz → fases/plan → DX/config), y dentro de cada área por prioridad.

---

## Voz

### 1. F2 está sobrecargada: el barge-in se come la confirmación hablada — ✅ cerrada (2026-08-01)

**Impacto: alto · Esfuerzo: medio** · *Se conserva el número para no romper las referencias cruzadas
de las deudas #2–#9.*

> **Cerrada.** F2 quedó con una semántica explícita por fase (🔵 abre · 🔴 cierra y descarta ·
> 🟡 reabre y fusiona · 🟣/🟢 barge-in que corta y fusiona), y el gate de permisos ya no compite con
> el barge-in: la pregunta hablada corre con el micrófono **reservado**. Detalle del arreglo abajo,
> después del diagnóstico original.

La misma tecla significa dos cosas incompatibles al mismo tiempo:

- El daemon corre `_consumir_interrumpible`, que compite entre consumir la respuesta y
  `_esperar_hotkey()` — un poll de `ears.hotkey_solicitada()` cada 80 ms
  ([daemon.py:299](../src/agent/voice/daemon.py#L299), [daemon.py:322](../src/agent/voice/daemon.py#L322),
  `_SONDEO_HOTKEY = 0.08` en [daemon.py:56](../src/agent/voice/daemon.py#L56)).
- Dentro de esa misma consumición, el SDK invoca el gate de permisos, que **pide al usuario que
  presione F2** para contestar sí/no: `tts.speak(f"... {verbo} efe dos y dime sí o no")` y luego
  bloquea en `ears.listen_once(wait_timeout=25)`
  ([interaction.py:99-103](../src/agent/voice/interaction.py#L99)).

`hotkey_solicitada()` es cierta tanto por `keyboard.is_pressed` (una pulsación humana dura ~100 ms,
más que el poll de 80 ms) como por `LATCH.pending()`. O sea: la pulsación con la que el usuario
**autoriza** dispara el guard de barge-in, que hace `cola.cancel()` + `client.interrupt()` y aborta
el turno que estaba pidiendo el permiso — mientras `_ask_voice` sigue bloqueado en un hilo del
executor con el micrófono abierto. El daemon vuelve al loop y puede abrir un segundo grabador sobre
el mismo dispositivo.

No hay ningún test que cubra la interacción: `tests/test_daemon.py` no menciona `VoiceGate`,
`_ask_voice` ni `listen_once`.

**Arreglo aplicado (2026-08-01):**

1. **Reserva de micrófono.** `ears.escucha_exclusiva()` (context manager reentrante, contador +
   `threading.Lock`) y `ears.escucha_reservada()`. `_ask_voice` envuelve **toda** la pregunta —el
   `tts.speak` y el `listen_once`— en la reserva, y al salir republica la fase 🟣 `pensando`, porque
   `listen_once` cierra su ciclo en `inactivo` y el turno sigue vivo.
2. **Guardia propia del barge-in.** `daemon._barge_in_pedido()` = `not ears.escucha_reservada() and
   ears.hotkey_solicitada()`; la usan `_stop_cb()` y `_esperar_hotkey()`. No alcanzaba con mirar la
   fase: `_abrir_escucha()` publica `escuchando` **después** de esperar la pulsación, así que la
   guardia dispararía en esa ventana.
3. **Semántica de F2 por fase** (la tabla que faltaba, no sólo la colisión con el gate):
   `record_toggle` devuelve el sentinel `CANCELADO` con el segundo toque (un `str` vacío que se
   distingue por identidad), `run_daemon` descarta lo arrastrado y **no** abre sesión, y las tres
   salidas de barge-in fusionan `pendiente = _fusionar(pendiente, text)` para no perder lo pedido.

**Tests:** `tests/test_interaction.py` (reserva de punta a punta, liberación ante excepción, fase
`pensando`, respuesta cancelada no autoriza), `tests/test_daemon.py` (`_barge_in_pedido` ve la
pulsación y queda suprimido con la reserva, `CANCELADO` no se transcribe ni abre sesión, el barge-in
al hablar arrastra y el habla completa no) y `tests/test_ears.py` (`CANCELADO`, `escucha_exclusiva`).
Documentado en [README.md](../README.md) (tabla de fases con columna *Sesión*, filas Barge-in y
Confirmación hablada).

### 2. Una corrida larga por voz no da feedback y F2 no la cancela de verdad — ✅ cerrada (2026-08-01)

**Impacto: medio · Esfuerzo: medio** · *Se conserva el número para no romper las referencias cruzadas
de las deudas #3–#9.*

> **Cerrada.** El barge-in levanta una señal cooperativa que `run_plan` consulta **entre pasos**
> (el paso ya despachado termina; el que seguía queda `bloqueado` y reanudable), y el tray cuenta el
> avance en vivo. Detalle del arreglo abajo, después del diagnóstico original.

Por voz se puede lanzar `mcp__orq__ejecutar`, que corre el plan entero en
`asyncio.to_thread(runner.run_plan, ...)` bajo `_RUN_LOCK`
([tools/orchestrator.py](../src/agent/tools/orchestrator.py)). Está bien que no bloquee el event
loop, pero durante minutos: (a) no hay ninguna señal audible ni visual de avance — los `on_step`
imprimen a stdout, que en el daemon nadie mira; (b) si el usuario aprieta F2, el barge-in aborta el
*turno* pero no el trabajo: el hilo sigue corriendo con el lock tomado, así que el siguiente
`ejecutar` rebota.

**Arreglo aplicado (2026-08-01):**

1. **Señal cooperativa** en un módulo propio y sin dependencias, [`agent/cancel.py`](../src/agent/cancel.py):
   `CancelSignal` (`pedir(motivo)` / `limpiar()` / `pedido()` / `motivo()`, sobre `threading.Event` +
   lock para el motivo) y el singleton `RUN` —uno solo, porque `_RUN_LOCK` ya garantiza una corrida a
   la vez—. Vive aparte justamente para que el daemon de voz no tenga que importar el runner.
2. **`run_plan(..., should_cancel=None)`**: se consulta antes de despachar cada paso; si dice que sí,
   el paso queda `bloqueado` con su nota, se persiste y la corrida termina — el mismo patrón que ya
   usaba `over_budget()`, así que es reanudable con otro `ejecutar`. Default `None` = no-op, el
   comportamiento previo queda intacto.
3. **El barge-in la levanta**: `_consumir_interrumpible` hace
   `cancel.RUN.pedir("cortaste el turno con F2")` junto al `interrupt()`. Cortar solo el turno dejaba
   al hilo del runner despachando pasos a espaldas del usuario. `ejecutar` **limpia** la señal al
   arrancar, para que un F2 de un turno sin corrida no mate la corrida siguiente.
4. **Progreso al tray**: topic nuevo `plan.step_start` `{repo, id, tipo, accion, hechos, total}`,
   publicado **antes** del dispatch y con `journal=False` (el hecho durable sigue siendo `plan.step`,
   que solo llega al TERMINAR el paso). `tray._seguir_plan` lo pinta en el tooltip
   (`plan 1/3 · paso 2 [editar]`) sin tocar el color —el color es del micrófono— y con `plan.done`
   restaura el rótulo de la fase vigente.
5. **El reporte lo dice**: `_reporte_corrida` agrega "Corrida CANCELADA a tu pedido (…)" con el
   motivo, y un `ejecutar` pedido mientras se está cerrando responde distinto que uno pedido durante
   una corrida normal.

*Fuera de alcance a propósito:* el feedback **audible**. El TTS del turno está ocupado con la
respuesta y dos voces encimadas empeoran el problema que se quería resolver; el canal visual (tray)
cubre el caso. Y el bus es in-process: un `escapement ejecutar` lanzado en otra terminal no mueve el
ícono del daemon. La cancelación tampoco mata el paso ya despachado — un executor externo no se
puede interrumpir a mitad.

**Tests:** `tests/test_cancel.py` (nuevo: bandera, motivo, limpieza, cruce de hilos),
`tests/test_runner.py` (corta entre pasos y deja el siguiente `bloqueado`, cancelar antes del primero
no despacha nada, sin `should_cancel` todo igual, `plan.step_start` precede al dispatch con
`hechos/total`), `tests/test_orchestrator_tools.py` (limpia la señal al arrancar, la pasa al runner,
reporte cancelado vs normal, rebote diferenciado), `tests/test_daemon.py` (el barge-in la pide con su
motivo; un turno que termina solo, o F2 sin barge-in, no) y `tests/test_tray.py` (tooltip de avance,
no toca el color, `plan.done` restaura la fase). Documentado en [README.md](../README.md) (tabla de
topics del bus, sección del orquestador conversacional y fila del tray).

### 3. `VOICE_BARGE_IN` es el único flag de voz sin override — ✅ cerrada (2026-08-26)

**Impacto: bajo · Esfuerzo: bajo (quick win)** · *Se conserva el número para no romper las
referencias cruzadas de las deudas #4–#10.*

> **Cerrada.** El flag pasa por `_voice_flag` como el resto de la voz; default `True` = el
> comportamiento previo exacto. Detalle abajo, después del diagnóstico original.

[config.py:535](../src/agent/config.py#L535) lo deja hardcodeado en `True`, mientras que todo el
resto de la voz pasa por `_voice_flag/_voice_str/_voice_float` (env var + `escapement.toml`). Tampoco
tiene línea en `escapement.toml.example`. Si el barge-in molesta (micrófono sensible, altavoces
abiertos) no se puede apagar sin editar el código.

**Arreglo aplicado (2026-08-26):** `VOICE_BARGE_IN = _voice_flag("AGENT_VOICE_BARGE_IN",
"barge_in", True)` + su bloque comentado en `escapement.toml.example` + tests de precedencia en
`tests/test_config.py` (default encendido, `barge_in = false` en el toml lo apaga, la env gana
sobre el toml; `AGENT_VOICE_BARGE_IN` sumada a `_sin_voice_env` para mantener la suite hermética).

---

## Fases y plan

### 4. El bus de eventos es de solo escritura — ✅ cerrada (2026-08-26)

**Impacto: medio · Esfuerzo: medio** · *Se conserva el número para no romper las referencias cruzadas
de las deudas #5–#10.*

> **Cerrada.** El journal ya tiene su consumidor de referencia: `escapement eventos [N] [--topic]`
> (alias `events`) sobre `bus.read`. Detalle del arreglo abajo, tras el diagnóstico original.

La Fase 0 dejó el bus montado y con journal (`data/events.jsonl`), y hay cinco productores
(`convlog`, `runner`, `daemon`, `ears`, `tray`). Pero **el único suscriptor en runtime de todo el
repo** es el tray, y `bus.read` no tiene ni un llamador fuera de los tests: ningún comando del CLI
muestra el journal.

> *Parcialmente atendida el 2026-08-01 al cerrar la deuda #2:* el tray sumó un segundo suscriptor,
> `_seguir_plan` sobre el prefijo `plan.`, así que `plan.step_start`/`plan.done` sí llegan a alguien.
> Lo que sigue abierto es el journal: `turn`, `voice.barge_in`, `voice.wake` y `plan.step` se
> escriben y nadie los lee.

**Arreglo aplicado (2026-08-26, E1):** `_run_eventos` en `cli.py`, calcado del patrón de
`_run_historial`: `N` posicional == `--limit`/`-n` (default 20, `0` = todos), `--topic`/`-t` filtra
por el mismo prefijo que `bus.subscribe`, salida `[ts] topic (source) {data}` en orden cronológico.
Solo lectura sobre `bus.read` — no publica ni trunca. El help documenta que los topics con
`journal=False` (`turn`, `voice.capture`, `plan.step_start`) no aparecen. Tests en `test_cli.py`
(vacío, alias, filtro por prefijo, equivalencia posicional/flag/corto, `--limit 0`, filtro sin
match) + sección en el README bajo *Bus de eventos*.

### 5. `escapement estado` no dice nada del plan — ✅ cerrada (2026-08-01)

**Impacto: alto · Esfuerzo: bajo** · *Se conserva el número para no romper las referencias cruzadas
de las deudas #6–#10.*

> **Cerrada** por `278550b` (2026-08-01): `_run_estado` suma el bloque del plan activo —objetivo,
> progreso `hechos/total`, paso en curso y checkpoint bloqueante con el comando exacto para
> desbloquearlo— ([cli.py:138](../src/agent/cli.py#L138)). Esta lista tardó 25 días en reflejarlo
> (se detectó el 2026-08-26 al auditar para el plan auto-guiado): evidencia directa de la deuda #9.

`_run_estado` ([cli.py:137](../src/agent/cli.py#L137)) es un dashboard del **orquestador**: cola
pendiente, throttle, ledger de optimizaciones, reviews de PRs. No menciona el objetivo activo, ni en
qué paso va, ni si hay un checkpoint esperando. Para eso hay que acordarse de `escapement plan ver`.
Justo cuando la Fase C (control loop) es el frente de trabajo, el comando que se llama "estado" no
habla del estado del trabajo.

**Arreglo aplicado (2026-08-01, `278550b`):** el bloque del plan activo en el dashboard, tal cual
lo pedía el diagnóstico.

### 6. El roadmap es inmutable desde el CLI — ✅ cerrada (2026-08-26)

**Impacto: medio · Esfuerzo: medio** · *Se conserva el número para no romper las referencias cruzadas
de las deudas #7–#10.*

> **Cerrada.** `escapement plan quitar <id> | mover <id> <pos> | editar <id> "<accion>"` editan el plan
> activo desde el CLI, con reconexión de dependencias y validación topológica. Detalle del arreglo
> abajo, tras el diagnóstico original.

`escapement plan` sólo ofrece `lista | ver [repo] | activar <repo>`
([cli.py:507-561](../src/agent/cli.py#L507)). No hay `editar`, `quitar` ni `mover`: si el planner
mete un paso de más o en mal orden, la única salida es editar el JSON a mano — cosa que el propio
mensaje de rechazo del checkpoint sugiere ("edita el plan a mano", [cli.py:372](../src/agent/cli.py#L372)).
`escapement paso <id> <estado>` sólo cambia estados, no la estructura.

**Arreglo aplicado (2026-08-26, E1):** la edición estructural vive en `planner.py` (el CLI solo
envuelve y persiste), operando sobre el plan **activo** — el mismo blanco que `escapement paso`:

- `remove_step` quita el paso y **empalma el grafo**: los dependientes heredan las dependencias del
  quitado, con dedup y sin auto-referencias; los ids restantes no se renumeran.
- `move_step` reubica con posición 1-based (fuera de rango se recorta) y **valida la topología de
  toda la lista** — si algún paso quedaría antes de una dependencia lanza `ValueError` y el plan
  queda intacto (valida sobre una copia). El orden importa: el runner despacha `listos[0]`, así que
  mover = repriorizar.
- `edit_step` solo reemplaza `accion`; tipo, done, dependencias, estado y nota quedan intactos.

El mensaje de rechazo del checkpoint ya no sugiere editar a mano: apunta a
`escapement plan quitar/mover/editar`. Tests en `test_planner.py` (empalme, dedup, topología,
plan-intacto) y `test_cli.py` (persistencia, uso, sin plan activo); sección en el README.

### 7. No hay evaluación del progreso global ni criterio de *done* del objetivo — ✅ cerrada (2026-08-28)

**Impacto: alto · Esfuerzo: alto**

> **Cerrada.** Las tres piezas del ciclo están en master (evaluación al cerrar — PR #10,
> replanificación automática — PR #11, telemetría `plan.eval`/`plan.replan` — PR #12) y la
> **validación en real** del "Done de la etapa" de E2 pasó en sus dos mitades — ver el bloque
> de validación al final de la sección. Diagnóstico original y avances por pieza abajo.

`VISION.md` marca la Fase C (control loop) como el frente actual y ahí está el hueco de fondo: el
runner decide paso a paso (frente topológico en `_ready()`/`pasos_listos()`), y el "done" existe por
paso (`s.done`) y como texto en `p.criterio_global`, pero **nadie evalúa el criterio global**. El
plan se declara completo por conteo — `all(s.estado == "hecho" for s in plan.pasos)`
([cli.py:478](../src/agent/cli.py#L478)) — no por haber logrado el objetivo. Tampoco hay
replanificación: si a mitad de camino queda claro que el roadmap no lleva al objetivo, no hay ciclo
que lo detecte.

**Arreglo:** paso de evaluación al cerrar el plan (contrastar el resultado contra `criterio_global`)
y, con eso, la puerta a replanificar en vez de dar por bueno el conteo. Es la deuda más grande de la
lista; conviene atacarla después de #4, que le da la telemetría (#5, la vista, ya quedó cerrada).

**Avance (2026-08-26, E2-1, PR #10):** primera de las tres piezas entregada — la **evaluación al cerrar**.
Con el roadmap agotado en `hecho` y `criterio_global` presente, `run_plan` despacha una evaluación
read-only (`_evaluar_plan` en [runner.py](../src/agent/runner.py), pseudo-tipo `evaluar` en
`route_step`, Clase B) que emite veredicto `done`/`incompleto` + gaps concretos (máx. 4). El
veredicto y los gaps **persisten en el plan** (`eval_veredicto`/`eval_gaps` en `Plan`, carga
backward-compatible) — son el insumo de la pieza 2 (replanificar). Un `incompleto` no captura la
trayectoria en el ReasoningBank; `escapement ejecutar` muestra el criterio y los gaps al cerrar. Gate:
`AGENT_PLAN_EVAL` / `[plan] evaluacion` (default ON; OFF = comportamiento previo exacto).
Best-effort: sin veredicto usable, cierre por conteo como siempre. Faltan las piezas 2
(replanificación con anti-bucle) y 3 (telemetría `plan.eval`/`plan.replan`) + validación en real —
la deuda sigue **abierta** hasta entonces.

**Avance (2026-08-26, E2-2, PR #11):** segunda pieza entregada — la **replanificación automática**.
Un veredicto `incompleto` convierte sus gaps en pasos nuevos (`_replanificar` en
[runner.py](../src/agent/runner.py): dispatch de razonamiento acotado a SOLO las brechas, kind
`reflexionar` Clase A, pasos vía `_insertar_pasos` con sus guardas anti-bucle) y la corrida sigue
con ellos hasta reevaluar. Anti-bucle: tope de `AGENT_PLAN_REPLAN_MAX` ciclos por plan (default 2,
`0` apaga solo la replanificación), con contador `replan_ciclos` **persistido en el JSON** —
reanudar no resetea el tope. Al tope (o si no sobrevive ningún paso), la corrida termina incompleta
con los gaps guardados y `escapement ejecutar` lo anuncia como checkpoint humano. El ciclo vive dentro
de `costs.track`: eval+replan ahora cuentan en el budget y el roll-up (cambio deliberado). Falta la
pieza 3 (telemetría `plan.eval`/`plan.replan`) + validación en real — la deuda sigue **abierta**.

**Avance (2026-08-27, E2-3, PR #12):** tercera pieza entregada — la **telemetría del ciclo**.
`run_plan` publica `plan.eval` `{repo, veredicto, gaps}` tras cada evaluación global (incluso con
veredicto `""` best-effort — esa ruta silenciosa ahora es observable) y `plan.replan`
`{repo, anadidos, ciclo, max}` tras cada replanificación efectiva, ambos con journal durable —
legibles con `escapement eventos --topic plan.` sin cambio en el renderer (es genérico). El bloque de
plan de `escapement estado` refleja el veredicto al cierre: `criterio global verificado` con `done`,
o `⏸` + primeros gaps + contador `replanificación N/máx` con `incompleto`. Contratos documentados
en el docstring de [bus.py](../src/agent/bus.py). Con esto las tres piezas de E2 están entregadas;
**falta la validación en real** (el "Done de la etapa" de
[PLAN_AUTOGUIADO.md](PLAN_AUTOGUIADO.md)) — la deuda sigue **abierta**.

**Validación en real (2026-08-28, cierre de E2):** dos corridas de laboratorio con
`escapement objetivo` + `escapement ejecutar` sobre repos desechables, cada una probando una mitad del
"Done de la etapa":

- **Escenario 1 — termina por criterio, no por conteo.** Objetivo: documentar `utils.py` con
  docstrings *de una línea*. El roadmap quedó completo pero con docstrings multilínea que violaban
  el criterio. Arco en el journal: `plan.eval` `""` (best-effort observable) → `incompleto` con gap
  concreto → `plan.replan {anadidos: 3, ciclo: 1, max: 2}` → los pasos nuevos corrigieron los
  docstrings → checkpoint humano intermedio resuelto con `escapement paso 7 hecho` + reanudación →
  eval final `done` y sello CLI "roadmap completo ✓ · criterio global verificado". Con el eval
  apagado, ese plan habría cerrado "completo" con el objetivo incumplido.
- **Escenario 2 — plan insuficiente, tope y freno humano.** Objetivo con un entregable imposible
  para el agente (aprobación que solo el humano puede firmar en persona, con instrucción explícita de
  no simularla) y `AGENT_PLAN_REPLAN_MAX=1`. Con el roadmap agotado en `hecho`, el eval dio
  `incompleto` con el gap correcto ("el humano debe rellenar en persona APROBACION.md…"), la
  replanificación añadió 3 pasos (`ciclo: 1, max: 1`), el segundo eval volvió a dar `incompleto` y
  la corrida frenó con "Replanificación automática agotada (1 ciclo(s), tope 1): checkpoint humano"
  — sin segundo replan, con `replan_ciclos: 1` persistido en el JSON. `APROBACION.md` terminó como
  plantilla vacía: ningún paso simuló la firma. Bonus: un paso insertado por el replan (condicional
  a una confirmación que nunca llegó) reportó un "verificado" falso y el eval global atrapó el gap
  de todos modos — el criterio global es exactamente la red que faltaba.

La validación destapó siete deudas nuevas (#11–#17, registradas en este archivo con su evidencia);
ninguna bloquea el ciclo evaluar→replanificar, que se comportó según diseño en ambos escenarios.

### 11. `_h_editar` con target edita el repo real, no el worktree del plan — ✅ cerrada (2026-09-21)

**Impacto: alto · Esfuerzo: medio** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** El carril `target` ahora despacha `optimize` **sobre el worktree del plan** y trae el
> commit de vuelta con un `merge --ff-only`. Detalle abajo, después del diagnóstico original.

`_h_editar` tiene dos carriles ([runner.py:736](../src/agent/runner.py#L736)): el autónomo corre en
`_workdir(plan, repo)` — el worktree del plan que S2 montó justamente para la continuidad — pero el
carril con `_target(step)` delega en `optimize(repo, ...)`
([runner.py:741](../src/agent/runner.py#L741)), y el orquestador trabaja sobre el **repo real** en
una rama propia `escapement/optimiza-<target>-<stamp>`. Resultado: la edición nunca aterriza en el
worktree del plan y los pasos siguientes no la ven — la continuidad que el worktree debía garantizar
se rompe exactamente en el tipo de paso que más la necesita.

**Evidencia (validación E2, escenario 1):** el paso 6 (`[editar]` los docstrings de utils.py) quedó
`hecho` con su edición en la rama `escapement/optimiza-utils-20260828-020040` del repo del lab,
mientras el worktree del plan conservaba la versión vieja; el paso 7 (`[verificar]`) falló
honestamente contra el worktree y frenó la corrida en checkpoint — hubo que traer el fix a mano
(`git checkout <commit> -- utils.py` dentro del worktree) para poder reanudar.

**Arreglo propuesto:** que el carril target opere también sobre `_workdir(plan, repo)` (o que, tras
el `optimize`, la rama resultante se fusione al worktree del plan antes de continuar). Mientras
tanto, el carril target además usa `repo` sin validar — ver #16.

**Arreglo aplicado (2026-09-21, E4.3):** el carril sale a su propia función,
[`_editar_target`](../src/agent/runner.py), y hace las **dos** cosas del arreglo propuesto, porque
ninguna sola alcanza. Pasarle el worktree a `optimize` no basta: `optimize` parte del **último
commit**, y los pasos `editar` dejan su trabajo *sin commitear* en el worktree — le habría dado a
revisar una versión vieja del target. La cadena es:

1. **`_fijar_avance(work, plan)`** — extraído de `traspasar_a_rama`, que ya hacía exactamente esto y
   ahora lo comparte sin duplicarlo: commitea como `escapement(wip): <objetivo>` lo que los pasos
   anteriores dejaron pendiente, para que `optimize` vea el árbol al día.
2. **`optimize(work, directiva, target, make_pr=False)`** — con el worktree como repo, así que su
   rama `escapement/optimiza-…` nace de la rama del plan y el refactor sale verificado y juzgado.
   Sin PR a propósito: la base sería la rama del plan, que no está en el remoto, y la entrega del
   plan es `traspasar_a_rama`, no un PR por paso.
3. **`merge --ff-only <rama>`** en el worktree — el ff siempre es posible (la rama salió del HEAD
   que acabamos de fijar y trae un commit encima), y los pasos siguientes ya ven la edición.

La rama de `optimize` **no se borra**: si el ff-merge fallara, ahí sigue vivo el trabajo, y la nota
`FALLIDO` la nombra en vez de reportar un éxito vacío. Sin worktree del plan (repo no-git, o el
montaje de S2 falló) se conserva el carril anterior —`optimize` sobre `repo`, con PR—: ahí no hay
continuidad que preservar. **Verificación:** 5 tests nuevos en `test_runner.py`, 3 de ellos fallan
sin el parche (el que replica el escenario 1 de E2 —el paso ve la edición sin commitear del paso
previo y deja la suya en el worktree—, el del ff-merge imposible, y el de `_fijar_avance`); los
otros 2 fijan el comportamiento que **no** cambia (carril sin worktree, con PR; y `optimize` no
verificado deja el worktree intacto). Suite completa: 920 pasando, `ruff` limpio.

### 12. `_h_verificar` despacha con `mode="edit"`: el verificador puede escribir lo que verifica — ✅ cerrada (2026-09-20)

**Impacto: alto · Esfuerzo: bajo** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** Dos capas: `no_write` le quita las tools de edición al dispatch y una huella del
> árbol antes/después descarta el veredicto si igual lo tocó. Detalle abajo, después del
> diagnóstico original.

`_h_verificar` ([runner.py:702](../src/agent/runner.py#L702)) despacha al agente con `mode="edit"`
([runner.py:710](../src/agent/runner.py#L710)), mientras que `_h_investigar` usa `mode="read"`
([runner.py:728](../src/agent/runner.py#L728)). Un verificador con permiso de escritura puede
"arreglar" él mismo lo que debía verificar y reportar `VERIFICADO` — un falso positivo de la clase
exacta que la deuda #7 quería eliminar.

**Evidencia (validación E2, escenario 1):** en el paso 4 el verificador encontró utils.py **sin**
docstrings (la edición del paso 3 se había ido al repo real, ver #11), los escribió él mismo
(multilínea, con bloques Args/Returns/Ejemplos) y reportó `verificado`. El plan siguió como si el
paso 3 hubiera funcionado.

**Arreglo propuesto:** verificación sin escritura de archivos. Si `mode="read"` bloquea también la
ejecución de comandos (un verificar legítimo corre `pytest`/`py_compile`), decidir mirando qué
permite cada modo del executor: lo indispensable es que no pueda **editar** el árbol que audita.

**Arreglo aplicado (2026-09-20, E4.2):** se mantiene `mode="edit"` —una verificación real corre
`pytest`, y `mode="read"` le quitaría el shell— y se cierra la **escritura**, en dos capas.

1. **Permisos del dispatch:** `no_write` en
   [`run_agent`](../src/agent/executors.py), calcado de `no_shell`: marca
   `AGENT_DENY_WRITE=1` en el env del subproceso (que el hook
   [`guard_cli`](../src/agent/security/guard_cli.py) hereda y usa para denegar `Write`, `Edit`,
   `MultiEdit` y `NotebookEdit`) y pasa `--disallowedTools` al CLI. Las dos marcas se acumulan sin
   pisarse; `agy`/`cursor` siguen ignorando el kwarg (misma limitación documentada de `no_shell`).
2. **Comprobación en disco:** eso no cubre un `echo > archivo` por shell, así que
   [`_h_verificar`](../src/agent/runner.py) compara `_huella_tracked(work)` antes y después del
   dispatch. Si difieren, el veredicto se descarta: el paso queda **FALLIDO** con los archivos
   sucios en la nota, aunque el agente haya dicho `VERIFICADO`.

`_huella_tracked` deja fuera los untracked a propósito —al revés que `_git_status`—: una
verificación legítima corre `pytest` y siembra `.pytest_cache/`/`__pycache__/`, y contar eso como
"el verificador editó" frenaría corridas sanas. Reescribir el código auditado sí es una
modificación de algo tracked y sí se ve. **Verificación:** 14 tests nuevos (4 del builder y el env
en `test_executors.py`, 6 del hook en `test_guard.py`, 4 de `_h_verificar`/`_huella_tracked` en
`test_runner.py`); 12 de ellos fallan con el `src/` del commit anterior (`4954776`) y pasan con el
parche — los 2 que no son los que fijan que el default sigue siendo no-op. Suite completa: 912
pasando.

### 13. `_insertar_pasos` no valida `tipo` ni calidad mínima de los pasos de la auto-evolución — ✅ cerrada (2026-09-20)

**Impacto: medio · Esfuerzo: bajo** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** El `tipo` desconocido se coerce a `investigar` y la acción que no da para un
> dispatch se descarta. Detalle abajo, después del diagnóstico original.

`_insertar_pasos` ([runner.py:943](../src/agent/runner.py#L943)) solo descarta `reflexionar`
(anti-bucle) y duplicados; acepta cualquier otro `tipo` y cualquier `accion`. Un tipo que no está
en los handlers cae en `_h_desconocido` → paso `bloqueado`, y una acción de una palabra no le da al
executor nada que hacer.

**Evidencia (validación E2, escenario 1):** la auto-evolución del paso 2 insertó 4 pasos basura —
`[investigar]` con acción vaga, `[editorificar]` (tipo inexistente), `[verificar]` con acción
literal "verificar" y `[refactor]` (tampoco es handler) —; la corrida frenó en el primero de ellos
bloqueado y hubo que quitar los cuatro a mano con `escapement plan quitar`.

**Arreglo propuesto:** whitelist de tipos (las claves de los handlers) con descarte o coerción a
`investigar`, y un umbral mínimo para `accion` (p. ej. largo mínimo y distinta del propio tipo).
Aplica igual a los pasos que llegan por replanificación (#7), que entran por la misma función.

**Arreglo aplicado (2026-09-20, E4.2):** dos guardas nuevas en
[`_insertar_pasos`](../src/agent/runner.py), después del anti-bucle de `reflexionar` y antes del
anti-duplicados, así que cubren por igual a la auto-evolución y a la replanificación (entran por
la misma función). Tratan distinto los dos defectos porque no son el mismo problema:

- **`tipo` desconocido → se coerce a `investigar`** (no se descarta): la tarea que propuso el
  modelo puede ser buena y solo estar mal etiquetada, e `investigar` es el tipo seguro —no edita
  nada—. Lo que se elimina es el paso `bloqueado` por `_h_desconocido` que frenaba en seco una
  corrida desatendida (`editorificar`, `refactor`).
- **`accion` inservible → se descarta** el paso entero: más corta que `_ACCION_MIN` (8) o igual al
  nombre pelado de un tipo, ignorando puntuación (el `[verificar] verificar` de la validación).
  Ahí no hay nada que corregir sin inventar la tarea.

**Limitación conocida:** la whitelist es la de `DEFAULT_HANDLERS`; un `run_plan(..., handlers=...)`
con tipos propios los verá coercidos, porque esta función no recibe el mapa de handlers efectivo
(queda documentado en su docstring). **Verificación:** 3 tests nuevos en `test_runner.py` —uno de
ellos replica la tanda de 4 pasos basura que hubo que quitar a mano en el escenario 1 y comprueba
que ahora sobreviven 3, todos con un tipo que sí tiene handler—; los 3 fallan sin el parche. Suite
completa: 915 pasando, `ruff` limpio.

### 17. El guard de efecto en disco es ciego al contenido de archivos untracked — ✅ cerrada (2026-09-20)

**Impacto: medio · Esfuerzo: bajo** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** Tercer componente de la huella con el sha256 de cada untracked, al final para no
> mover lo que ya consumía `_archivos_tocados`. Detalle abajo, después del diagnóstico original.

`_git_status` ([runner.py:139](../src/agent/runner.py#L139)) combina `git status --porcelain` +
`git diff HEAD` como huella "sensible al contenido" para que `_h_editar` detecte falsos `hecho`.
El diseño contempló reeditar un archivo **tracked** ya modificado (aparece en el diff), pero no
reeditar un archivo **nuevo que otro paso dejó untracked**: el porcelain muestra `?? archivo`
idéntico antes y después, y `git diff HEAD` no incluye untracked — la huella no cambia aunque el
contenido sí.

**Evidencia (validación E2, escenario 2):** el paso 9 reescribió `APROBACION.md` (que un paso
anterior había dejado untracked) de secciones heredadas a plantilla estricta — cambio real en
disco — y el guard lo marcó `fallido` con "sin efecto en disco: la edición no cambió ningún
archivo". Falso negativo: frenó la corrida en checkpoint por un cambio que sí ocurrió.

**Arreglo propuesto:** incluir el contenido de los untracked en la huella — p. ej. sumar
`git status --porcelain` + hash del contenido de cada `??` (o `git add -N` efímero para que el
diff los vea). Con eso el mismo guard cubre ambos carriles sin cambiar su semántica.

**Arreglo aplicado (2026-09-20, E4.2, commit `4954776`):** `_huella_untracked` lista con
`git ls-files --others --exclude-standard -z` —mismo criterio de ignorados que el porcelain, y
`-z` evita el quoting de `core.quotepath` en rutas con acentos— y devuelve una línea
`"<sha256> <ruta>"` por archivo, ordenadas para que la huella sea estable. `_git_status` la suma
como **tercer** componente, tras el segundo `\0`: `_archivos_tocados` lee solo hasta el primero,
así que la extracción de rutas no cambia (hay un test que lo fija). Techo de lectura de 8 MiB por
huella (`_HUELLA_UNTRACKED_BYTES`): pasado el presupuesto la marca cae a `size:<n>`, que aún capta
un cambio de tamaño, para que un `node_modules/` sin trackear no vuelva lenta cada comprobación;
`ilegible` si el archivo desaparece entre el listado y la lectura. **Verificación:** 5 tests nuevos
en `test_runner.py` —el central reescribe un untracked sin renombrarlo y comprueba que el porcelain
y el diff quedan idénticos (`h1.split("\0")[:2] == h2.split("\0")[:2]`, esa era la trampa) mientras
la huella completa sí se mueve—; los 4 que ejercen el código nuevo fallan con el `src/` anterior.

---

## DX y config

### 8. El checkpoint de aprobación es sólo previo, no inline — ✅ cerrada (2026-08-26)

**Impacto: bajo · Esfuerzo: medio** · *Se conserva el número para no romper las referencias
cruzadas.*

> **Cerrada.** `_checkpoint_inline` pregunta en el momento (`[h]echo`/`[r]eintentar`/`Enter`) y la
> corrida sigue en el mismo proceso; sin TTY todo queda como antes. Detalle abajo, después del
> diagnóstico original.

M1 (entregado) muestra el roadmap y pide `[s/N]` **antes** de la primera corrida
([cli.py:454](../src/agent/cli.py#L454)). Cuando el runner se frena a mitad de camino
(`bloqueado`/`fallido`) el proceso termina y hay que reanudar con `escapement paso ... && escapement
ejecutar`. En una terminal interactiva se podría preguntar en el momento y seguir sin salir.

**Arreglo aplicado (2026-08-26, E1):** puerta gemela de `_aprobar_plan` — `_checkpoint_inline`
respeta el no-TTY con la misma condición (`not config.PLAN_APPROVAL or not sys.stdin.isatty()` →
nunca pregunta) y el tail de `_run_ejecutar` pasó a un loop: si el checkpoint se resuelve, la
corrida continúa sin salir del proceso. Tres respuestas: `h`/`hecho` (o afirmativa) marca `hecho`,
`r`/`reintentar` devuelve a `pendiente` conservando la nota con la causa, y `Enter`/desconocido sale
con el mensaje de reanudación clásico (default seguro, como el `[s/N]`). `h <texto>` guarda la
decisión como nota del paso — en los checkpoints de DIVERGENCIA es la respuesta que fluye como
contexto a los dependientes, igual que `escapement paso <id> hecho "<decisión>"`. La respuesta se
persiste al momento (`planner.save_plan`).

### 9. `docs/` acumula reportes que envejecen sin marca — ✅ cerrada (2026-08-26)

**Impacto: bajo · Esfuerzo: bajo** · *Se conserva el número para no romper las referencias
cruzadas.*

> **Cerrada.** Los siete docs de `docs/` llevan marca de vigencia y este archivo quedó declarado
> como la única lista viva. Detalle abajo, después del diagnóstico original.

Este mismo archivo es la evidencia: `RETRO_DX_PLANES.md` siguió siendo la referencia dos semanas
después de que sus conclusiones dejaran de valer, sin nada que lo señalara. Igual pasa con
`ROADMAP_AIRI.md` (congelado hasta tener GPU) y `EVALUACION_RUFLO.md`.

**Arreglo aplicado (2026-08-26):** encabezado con fecha + estado en cada doc de `docs/` —
DEUDAS (`vigente`, única lista viva), PLAN_AUTOGUIADO (`vigente`), VISION (`vigente`, revisado),
ROADMAP_MEJORA_ESCAPEMENT (`completado/histórico`), ROADMAP_AIRI (`congelado`, con fecha de revisión),
EVALUACION_RUFLO (`decisión vigente: cosechar, no instalar`), DISENO_MINIPARSER_CLI (ya la tenía:
`entregado`). El caso #5 —cerrado en código el 2026-08-01 y reflejado aquí recién el 2026-08-26—
fue la última evidencia del problema.

**Nota (2026-09-20):** `EVALUACION_RUFLO.md` se retiró del repo. Su cosecha ya está en el
código — parrilla de personas (`personas.py`), ReasoningBank (`reasoning.py`), MMR + contador de
costo (`tools/semantic.py`, `costs.py`) y el paso `swarm` nativo (`runner._h_swarm`)— y la última
fase, adoptar ruflo como executor externo, quedó descartada: no compone con el modelo de permisos
del agente. El crédito conceptual sobrevive en `src/agent/reasoning.py`. Las menciones al archivo
en esta deuda y en `PLAN_AUTOGUIADO.md` (E0) son registro histórico de lo hecho el 2026-08-26, no
referencias a un archivo vivo.

### 10. No hay CI: los PRs llegan sin check automático — ✅ cerrada (2026-08-26)

**Impacto: alto · Esfuerzo: bajo** · *Se conserva el número para no romper las referencias
cruzadas.*

> **Cerrada.** `.github/workflows/tests.yml` corre `pytest` en cada PR y en `master`. Detalle
> abajo, después del diagnóstico original.

No existe `.github/workflows/`. Escapement abre PRs y **nunca mergea**: toda la verificación recae en
la revisión manual, sin siquiera un `pytest` automático que la respalde. Para el ciclo auto-guiado
([PLAN_AUTOGUIADO.md](PLAN_AUTOGUIADO.md)) es el enabler #1: un PR de Escapement debería llegar con
check verde/rojo antes de que un humano lo mire. *Registrada el 2026-08-26 al ejecutar la Etapa 0.*

**Arreglo aplicado (2026-08-26, E1):** workflow `tests.yml` (`windows-latest`, uv) que corre la
suite en cada `pull_request`, en push a `master` y a mano (`workflow_dispatch`). La verificación
previa en runner limpio se hizo tal cual pedía esta deuda: venv desde cero con
`uv sync --locked --no-group voice --no-group semantic` (fuera CUDA/ONNX/hardware; `local` sí entra
porque `test_local_models.py` importa `openai` de verdad) y sin `escapement.toml` ni `data/` → 753
passed, 1 skipped. Nada quedó excluido con marcas: la suite ya era 100% headless. Único ajuste de
deps: `numpy` se añadió también al grupo `dev` (los tests de voz construyen buffers falsos con él).

### 14. El guard no-TTY está roto en Windows: EOFError en los tres prompts interactivos — ✅ cerrada (2026-09-20)

**Impacto: alto · Esfuerzo: bajo** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** Los tres prompts pasan por `_respuesta()`, que devuelve `None` cuando no hay
> humano y deja que cada llamador aplique su default seguro. Detalle abajo, después del
> diagnóstico original.

Los tres prompts del CLI se protegen con `sys.stdin.isatty()` — el rescate de `_reporte_traza`
([cli.py:355](../src/agent/cli.py#L355)), `_aprobar_plan`
([cli.py:458](../src/agent/cli.py#L458)) y `_checkpoint_inline`
([cli.py:493](../src/agent/cli.py#L493)). En Windows ese guard no alcanza: un proceso lanzado en
background sin stdin, e incluso con `< /dev/null` (NUL es un *char device*, así que `isatty()`
devuelve `True`), pasa el guard y el `input()` muere con `EOFError`. Lo peor es dónde: el rescate
corre **al final de una corrida exitosa**, así que el proceso termina con traceback y exit code 1
después de haber hecho todo el trabajo bien.

**Evidencia (validación E2):** corrida background del escenario 1 — `[ejecutar] roadmap completo ✓`
seguido de `EOFError: EOF when reading a line` en el `input()` del rescate
([cli.py:362](../src/agent/cli.py#L362)), exit code 1. Workaround verificado: lanzar con un pipe en
stdin (`printf "" | escapement ...`) — un pipe no es char device y el guard sí funciona.

**Arreglo propuesto:** envolver cada `input()` en `try/except EOFError` que tome el mismo camino
que el no-TTY (el default seguro que ya existe). Es prerequisito práctico de E3: el ciclo
auto-guiado corre headless.

**Arreglo aplicado (2026-09-20, E4.1):** un único helper
[`_respuesta`](../src/agent/cli.py#L307) unifica los dos modos de *nadie va a responder* —el
guard `isatty()` y el `EOFError` que ese guard no atrapa— y devuelve `None` en ambos, más en
los descriptores rotos (`OSError`/`ValueError`: `pythonw`, servicio, subproceso sin stdin).
Cada llamador conserva el default que ya tenía: el rescate imprime la salida manual,
`_aprobar_plan` ejecuta (igual que sin TTY) y `_checkpoint_inline` sale sin tocar el plan. El
early-return por `isatty()` de `_aprobar_plan` se mantiene, así que headless sigue **sin**
renderizar el roadmap. 11 tests nuevos en `tests/test_cli.py` cubren TTY, sin TTY, `EOFError`,
stdin cerrado y `sys.stdin is None`.

### 15. Los tests de `run_plan` contaminan el journal real (`data/events.jsonl`) — ✅ cerrada (2026-09-20)

**Impacto: medio · Esfuerzo: bajo** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** Fixture `autouse` en `tests/conftest.py`: ningún test vuelve a escribir en el
> journal real. Detalle abajo, después del diagnóstico original.

Las llamadas a `run_plan(...)` en `tests/test_runner.py` (unas 20) no redirigen `config.EVENTS`:
cada corrida de la suite publica eventos de planes falsos al journal **real**. Solo los 6 tests de
la telemetría E2-3 lo parchean (`monkeypatch.setattr(runner.config, "EVENTS", tmp_path / ...)`).
Al 2026-08-28 el journal acumula **5,904 eventos falsos** (`"repo": "repo"`,
`"objetivo": "obj"`) desde el 2026-07-29 — ruido directo en `escapement eventos` y en cualquier
consumidor futuro del bus.

**Arreglo propuesto:** fixture `autouse` en `tests/conftest.py` que redirija `config.EVENTS` a
`tmp_path` para toda la suite (y, opcional, una limpieza única de los eventos falsos ya escritos en
el journal local).

**Arreglo aplicado (2026-09-20, E4.1):** `tests/conftest.py` nuevo con un fixture `autouse` que
apunta `config.EVENTS` a un archivo dentro del `tmp_path` de cada test. Cubre la suite entera
sin tocar los tests porque todo el código lee el atributo al publicar (`config.EVENTS.open(...)`,
nunca `from agent.config import EVENTS`); un test que quiera su propio journal puede seguir
parcheándolo, su `monkeypatch` corre después y gana. **Verificación:** `data/events.jsonl`
quedó en 664 líneas antes y después de una corrida completa de `pytest` (antes crecía ~592 por
corrida). Los eventos falsos ya escritos se dejaron como están: `data/` está gitignorado y
borrarlos es una decisión local, no del repo.

### 16. `escapement ejecutar` no valida que el repo exista — ✅ cerrada (2026-09-20)

**Impacto: bajo · Esfuerzo: bajo** · *Registrada el 2026-08-28: hallazgo de la validación en real
de E2 (deuda #7).*

> **Cerrada.** `_repo_utilizable()` valida antes de tocar el plan, en las dos vías (argumento y
> repo recordado). Detalle abajo, después del diagnóstico original.

El plan se resuelve por slug del path, así que un path con typo cuyo slug coincide carga el plan
igual y la corrida arranca. Los pasos sobreviven porque usan `plan.workdir`, pero el primer código
que usa `repo` directo muere feo: en la validación, el carril target de `_h_editar`
(`optimize(repo, ...)`) reventó con `NotADirectoryError` (WinError 267) dentro de un subprocess de
git, varios pasos después de arrancar.

**Arreglo propuesto:** validar `Path(repo).is_dir()` al entrar en `_run_ejecutar` y salir con un
mensaje claro antes de tocar el plan.

**Arreglo aplicado (2026-09-20, E4.1):** [`_repo_utilizable`](../src/agent/cli.py#L354) valida
`Path(repo).is_dir()` e imprime la ruta ofensora con su origen. Se llama en los **dos** puntos
donde `_run_ejecutar` fija el repo: justo después de resolver el argumento (antes de
`plan_slot`, así el typo ni carga el plan ni le pisa `plan.repo` —que la vía vieja persistía—) y
después de recuperar `plan.repo`, para el caso del repo movido o borrado tras planificar. La
colisión de slugs quedó fijada en un test: `vault_slug` machaca lo no alfanumérico, así que
`<tmp>/repo_aprobar` y `<tmp>/repo-aprobar` comparten `plan_<slug>.json`; con la ruta mala,
`run_plan` ya no se llama y el plan guardado conserva su `repo`. Los dos tests fallan sin el
parche y pasan con él.

---

## Cerrado (ya no son deuda)

| Antes | Por qué se cae |
|---|---|
| **#1** — F2 sobrecargada (barge-in vs confirmación hablada) | Arreglada el 2026-08-01: reserva de micrófono (`ears.escucha_exclusiva`) + guardia propia `daemon._barge_in_pedido` + semántica de F2 por fase (sentinel `CANCELADO`, arrastre en el barge-in). Ver la sección **Voz → 1** arriba, que se conserva en su lugar (con el diagnóstico y el detalle del arreglo) para no renumerar el resto. |
| **#2** — corrida larga sin feedback y F2 que no la cancela | Arreglada el 2026-08-01: señal cooperativa `cancel.RUN` + `run_plan(should_cancel=…)` que corta entre pasos dejando el siguiente `bloqueado`, y progreso en vivo al tray por `plan.step_start`. Ver la sección **Voz → 2** arriba, que se conserva en su lugar (con el diagnóstico y el detalle del arreglo) para no renumerar el resto. |
| **#3** — `VOICE_BARGE_IN` sin override | Arreglada el 2026-08-26: `_voice_flag("AGENT_VOICE_BARGE_IN", "barge_in", True)` + bloque comentado en `escapement.toml.example` + tests de precedencia en `tests/test_config.py`. Ver **Voz → 3** arriba. |
| **#4** — el bus de eventos era de solo escritura | Arreglada el 2026-08-26 (E1): `escapement eventos [N] [--topic]` (alias `events`) lee el journal vía `bus.read` — límite, filtro por prefijo y caveat de `journal=False` documentados. Ver **Fases y plan → 4** arriba. |
| **#5** — `escapement estado` no decía nada del plan | Arreglada el 2026-08-01 (`278550b`): `_run_estado` suma el bloque del plan activo (objetivo, `hechos/total`, paso en curso, checkpoint con el comando para desbloquear). Registrado aquí el 2026-08-26. Ver **Fases y plan → 5** arriba. |
| **#6** — el roadmap era inmutable desde el CLI | Arreglada el 2026-08-26 (E1): `escapement plan quitar/mover/editar` sobre el plan activo — `remove_step` empalma el grafo de dependencias, `move_step` valida la topología (rechazo deja el plan intacto), `edit_step` solo toca la acción. Ver **Fases y plan → 6** arriba. |
| **#7** — sin evaluación global ni criterio de *done* del objetivo | Cerrada el 2026-08-28 (E2, PRs #10/#11/#12): con el roadmap agotado, `run_plan` contrasta el resultado contra `criterio_global` (veredicto `done`/`incompleto` + gaps persistidos), replanifica los gaps con tope `AGENT_PLAN_REPLAN_MAX` (contador `replan_ciclos` persistido) y publica `plan.eval`/`plan.replan` al journal. Validada en real con dos escenarios de laboratorio: cierre por criterio (no por conteo) y freno en el tope con checkpoint humano. Ver **Fases y plan → 7** arriba. |
| **#8** — checkpoint sólo previo, no inline | Arreglada el 2026-08-26 (E1): `_checkpoint_inline` pregunta en TTY (`[h]echo`/`[r]eintentar`/`Enter`, con `h <texto>` para guardar la decisión como nota) y la corrida sigue en el mismo proceso; sin TTY o con `AGENT_PLAN_APPROVAL=0` nunca pregunta. Ver **DX y config → 8** arriba. |
| **#9** — docs que envejecen sin marca | Arreglada el 2026-08-26: marca de vigencia (fecha + estado) en los siete docs de `docs/` y este archivo declarado única lista viva. Ver **DX y config → 9** arriba. |
| **#10** — PRs sin check automático | Arreglada el 2026-08-26 (E1): workflow `tests.yml` (`windows-latest`, `uv sync --locked` sin voz/GPU ni semantic) corre `pytest` en cada PR, en `master` y a mano. Verificado antes en venv limpio: 753 passed, 1 skipped. Ver la sección **10** arriba. |
| **M2** — slash-commands del orquestador dentro del REPL | Superado: `session.build_options` cablea el server MCP `orq` en **ambos** modos (texto y voz) con sus 10 tools (estado, deuda, optimizar, vigilar, trabajar, revisar_prs, objetivo, plan, ejecutar, paso). La conversación ya maneja el orquestador; unos slash-commands serían una segunda superficie para lo mismo. |
| **QW1–QW5, S1, M1** | Entregados (launcher `v`, help por subcomando, `_int_arg` uniforme, flag `--repo`, `plan ver`, mini-parser `cliparse`, checkpoint de aprobación). |
| **C2/C3** del reporte viejo | Absorbidos por el mini-parser (`cliparse.split_args/opt_str/opt_int/arg_int/has_flag`), hoy usado por todos los handlers. |
