# Roadmap AIRI — frontend de presencia (congelado hasta nueva GPU)

Estado: **congelado a propósito** (revisado 2026-08-26; se retoma con GPU ≥16 GB de VRAM). No es
trabajo pendiente por falta de tiempo, es una espera deliberada por hardware. Este documento existe
para poder retomarlo sin reconstruir el contexto.

## Por qué está congelado

La GPU actual es una **RTX 3050 (8 GB)** y en sesión por voz ya está comprometida:

| Consumidor | VRAM | Referencia |
| --- | --- | --- |
| STT `faster-whisper large-v3-turbo` | ~1.6 GB (CUDA/float16) | [README](../README.md) — tabla del stack de voz |
| TTS Kokoro-82M | residente mientras habla | [`voice/tts.py`](../src/agent/voice/tts.py) |
| Modelo local del orquestador | **vetado en voz** | `AGENT_LOCAL_ORCH_DISABLE_ON_VOICE=1` |

Ese veto es la prueba de que el presupuesto ya está ajustado: en sesión por voz el orquestador se
va 100% online precisamente para no pelear por la VRAM ([VISION.md](VISION.md), "veto de GPU").
Un avatar 3D con render continuo entra en el mismo presupuesto y no cabe.

**Disparador para descongelar:** GPU con **≥16 GB** de VRAM. Con ese margen el avatar convive con
Whisper + Kokoro sin desalojar el modelo local ni forzar el veto.

## Qué ya está hecho (Fase 0 y preparación GPU-neutral)

Todo esto se construyó para que el frontend sea un añadido, no una cirugía:

| Pieza | Estado | Dónde |
| --- | --- | --- |
| Bus de eventos in-process + journal durable | ✅ | [`bus.py`](../src/agent/bus.py) |
| Barge-in (F2 corta al TTS) | ✅ | `voice.barge_in` en el bus |
| STT ligero (`large-v3-turbo`, ~1.6 GB) | ✅ | [`voice/stt.py`](../src/agent/voice/stt.py) |
| Wake word manos libres (openWakeWord) | ✅ | `voice.wake` en el bus |
| Habla en streaming (por frases) | ✅ | [`voice/speech.py`](../src/agent/voice/speech.py) |

## Qué falta

| # | Pieza | Nota |
| --- | --- | --- |
| 1 | **Servidor FastAPI** | expone el bus hacia fuera del proceso. Hoy no existe **a propósito**: sin consumidor real sería infraestructura muerta |
| 2 | **Cliente WS del plugin-protocol** | habla el protocolo de plugins de AIRI sobre WebSocket |
| 3 | **AIRI Stage** | el runtime de escena que hospeda al personaje |
| 4 | **Avatar 3D** | modelo, rig y mapeo de estados a animación/expresión |

Orden natural: 1 → 2 → 3 → 4. Cada uno depende del anterior.

## Contratos ya congelados (el adaptador se monta SOBRE esto)

Decisión firme: **el adaptador de AIRI no modifica el bus**, se suscribe a él. Los topics ya
publicados son el contrato estable y no deben cambiar de forma para acomodar al frontend.

| Topic | Payload | Uso probable en el avatar |
| --- | --- | --- |
| `voice.state` | `{state: "activo" \| "congelado" \| "apagado"}` | postura/estado idle del personaje |
| `voice.wake` | `{model}` | reacción al ser llamado |
| `voice.barge_in` | `{}` | cortar animación de habla en el acto |
| `turn` | `{backend, session_id, user, reply}` | subtítulos / lipsync (**`journal=False`**, solo señal in-process) |
| `plan.step` | `{repo, id, tipo, estado, nota}` | indicador de trabajo en curso |
| `plan.done` | `{repo, objetivo, completado}` | fin de tarea |

Dos vías de consumo, ambas ya disponibles:

- **In-process:** `bus.subscribe(prefix, callback)` — match por `startswith`, sin comodines.
- **Fuera de proceso:** leer `data/events.jsonl` (`config.EVENTS`), JSONL append-only. Es lo que
  permite que un frontend externo siga el flujo **sin servidor HTTP** todavía.

Ojo con `turn`: se publica con `journal=False` porque el texto ya es durable en
`conversations.jsonl`. Un consumidor externo que lea solo el journal **no verá los turnos** — si el
avatar los necesita fuera del proceso, eso lo resuelve el servidor FastAPI (pieza 1), no un cambio
en el bus.

## Principios que aplican al retomar

- **El bus no se toca.** Si el frontend necesita algo nuevo, se añade un topic; no se cambia la
  forma de uno existente.
- **Nada de infraestructura especulativa.** FastAPI entra cuando haya un consumidor real, no antes.
- **Best-effort hacia abajo.** `publish` nunca lanza: un frontend caído no puede tumbar al daemon
  de voz. El adaptador debe mantener esa propiedad.
- **Degradar sin avatar.** Escapement tiene que seguir siendo plenamente usable por voz y texto con el
  frontend apagado.

## Decisiones aún abiertas

- Si el avatar corre en el mismo host o en otro (cambia si FastAPI escucha en loopback o en red).
- Si el lipsync se deriva del texto de `turn` o del audio real del TTS.
- Qué pasa con el veto de GPU (`AGENT_LOCAL_ORCH_DISABLE_ON_VOICE`) cuando sobre VRAM: ¿se relaja
  el default, o se deja el veto y se gana margen para el avatar?
