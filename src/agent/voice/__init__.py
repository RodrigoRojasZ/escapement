"""Capa de voz de Escapement (F2): STT, TTS, captura por hotkey (toggle/hold) y el daemon residente.

Las dependencias pesadas (faster-whisper, kokoro) se importan perezosamente dentro
de cada modulo, para que el modo solo-texto no las cargue.
"""
