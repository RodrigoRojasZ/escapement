"""Tests del habla en streaming (partido en frases + cola). Sin hardware de audio."""

import threading
import time

from agent.voice.speech import SentenceBuffer, SpeechQueue


# ---------------------------------------------------------------- SentenceBuffer


def test_frase_completa_sale_al_cerrarse():
    buf = SentenceBuffer()
    assert buf.feed("Ya revisé el repositorio completo. ") == ["Ya revisé el repositorio completo."]


def test_frase_incompleta_no_sale():
    buf = SentenceBuffer()
    assert buf.feed("Ya revisé el repo") == []
    assert buf.flush() == "Ya revisé el repo"


def test_deltas_parciales_se_acumulan():
    buf = SentenceBuffer()
    assert buf.feed("Ya revisé el ") == []
    assert buf.feed("repositorio comp") == []
    assert buf.feed("leto. Falta el resto") == ["Ya revisé el repositorio completo."]
    assert buf.flush() == "Falta el resto"


def test_varias_frases_en_un_solo_delta():
    buf = SentenceBuffer()
    frases = buf.feed("Primera frase bastante larga. Segunda frase igual de larga. ")
    assert frases == ["Primera frase bastante larga.", "Segunda frase igual de larga."]


def test_decimal_no_corta_la_frase():
    buf = SentenceBuffer()
    # "3.5" tiene el punto pegado a un dígito: no es cierre de frase.
    assert buf.feed("El proceso tardó 3.5 segundos en total") == []
    assert buf.flush() == "El proceso tardó 3.5 segundos en total"


def test_terminador_al_final_del_buffer_espera_mas_texto():
    buf = SentenceBuffer()
    assert buf.feed("El proceso tardó 3.") == []  # podría seguir un dígito
    assert buf.feed("5 segundos, nada más") == []


def test_abreviatura_no_corta_la_frase():
    buf = SentenceBuffer()
    assert buf.feed("Lo revisó el Dr. Rojas durante la mañana. ") == [
        "Lo revisó el Dr. Rojas durante la mañana."
    ]


def test_fragmento_corto_se_pega_al_siguiente():
    buf = SentenceBuffer()
    # "Sí." sola es demasiado corta: no se descarta, se une a la frase siguiente.
    assert buf.feed("Sí. ") == []
    assert buf.feed("Ya quedó todo listo del lado del daemon. ") == [
        "Sí. Ya quedó todo listo del lado del daemon."
    ]


def test_salto_de_linea_corta_sin_puntuacion():
    buf = SentenceBuffer()
    assert buf.feed("- primer punto de la lista larga\nsigue") == [
        "- primer punto de la lista larga"
    ]


def test_flush_vacia_el_buffer():
    buf = SentenceBuffer()
    buf.feed("cola pendiente")
    assert buf.flush() == "cola pendiente"
    assert buf.flush() == ""


def test_delta_vacio_es_noop():
    buf = SentenceBuffer()
    assert buf.feed("") == []


# ---------------------------------------------------------------- SpeechQueue


def test_cola_reproduce_en_orden():
    dichas = []
    cola = SpeechQueue(lambda f: bool(dichas.append(f)))
    cola.say("uno")
    cola.say("dos")
    cola.say("tres")
    assert cola.close(timeout=5) is False
    assert dichas == ["uno", "dos", "tres"]


def test_cola_ignora_frases_vacias():
    dichas = []
    cola = SpeechQueue(lambda f: bool(dichas.append(f)))
    cola.say("")
    cola.say("   ")
    cola.close(timeout=5)
    assert dichas == []


def test_barge_in_descarta_lo_pendiente():
    dichas = []
    arrancó = threading.Event()

    def hablar(frase):
        dichas.append(frase)
        arrancó.set()
        return frase == "uno"  # la primera se corta

    cola = SpeechQueue(hablar)
    cola.say("uno")
    arrancó.wait(timeout=5)
    cola.say("dos")
    cola.say("tres")
    assert cola.close(timeout=5) is True
    assert dichas == ["uno"]  # lo pendiente no se dijo


def test_close_devuelve_false_si_no_hubo_corte():
    cola = SpeechQueue(lambda _f: False)
    cola.say("hola")
    assert cola.close(timeout=5) is False
    assert cola.interrumpido is False


def test_excepcion_en_una_frase_no_tumba_la_cola():
    dichas = []

    def hablar(frase):
        if frase == "mala":
            raise RuntimeError("sin dispositivo de audio")
        dichas.append(frase)
        return False

    cola = SpeechQueue(hablar)
    cola.say("mala")
    cola.say("buena")
    assert cola.close(timeout=5) is False
    assert dichas == ["buena"]


def test_close_sin_frases_termina():
    cola = SpeechQueue(lambda _f: False)
    assert cola.close(timeout=5) is False


def test_close_es_idempotente():
    cola = SpeechQueue(lambda _f: False)
    cola.say("hola")
    assert cola.close(timeout=5) is False
    assert cola.close(timeout=5) is False  # sobre una cola ya cerrada no cuelga


def test_cancel_descarta_lo_pendiente():
    dichas = []
    arrancó = threading.Event()

    def hablar(frase):
        # La primera frase sigue "sonando" hasta que el test cancela: así el corte ocurre con
        # frases ya encoladas detrás, que es el caso que importa.
        dichas.append(frase)
        arrancó.set()
        while not cola.interrumpido:
            time.sleep(0.01)
        return False

    cola = SpeechQueue(hablar)
    cola.say("uno")
    arrancó.wait(timeout=5)
    cola.say("dos")
    cola.say("tres")
    cola.cancel(timeout=5)
    assert dichas == ["uno"]  # lo encolado detrás no se dijo
    assert not cola._hilo.is_alive()


def test_cancel_sin_frases_termina():
    cola = SpeechQueue(lambda _f: False)
    cola.cancel(timeout=5)
    assert not cola._hilo.is_alive()


def test_say_tras_cancel_es_noop():
    dichas = []
    cola = SpeechQueue(lambda f: bool(dichas.append(f)))
    cola.cancel(timeout=5)
    cola.say("tarde")
    assert dichas == []


def test_cancel_tras_close_no_cuelga():
    cola = SpeechQueue(lambda _f: False)
    cola.say("hola")
    cola.close(timeout=5)
    cola.cancel(timeout=5)  # cerrar y luego abortar no debe bloquear
    assert not cola._hilo.is_alive()
