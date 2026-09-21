"""Tests del detector de secretos (P1). Sin cuota."""

from agent.security.secrets import find_secrets


def test_detecta_connection_string_con_credenciales():
    assert find_secrets('DB_URL = "mysql://admin:s3cr3tPass@db.host:3306/prod"')


def test_detecta_password_hardcodeada():
    assert find_secrets('password = "superSecret123"')


def test_detecta_private_key():
    assert find_secrets("-----BEGIN RSA PRIVATE KEY-----\nMIIabc...\n-----END RSA PRIVATE KEY-----")


def test_detecta_token_con_prefijo():
    assert find_secrets('KEY = "sk-abcdefghijklmnopqrstuvwxyz012345"')


def test_no_marca_referencia_a_env_var():
    assert find_secrets('password = os.getenv("DB_PASSWORD")') == []
    assert find_secrets('sql = {"password": os.getenv("AZURE_NEW_DB_PASSWORD")}') == []


def test_no_marca_placeholders():
    assert find_secrets('password = "changeme"') == []
    assert find_secrets('api_key = "your_api_key"') == []


def test_codigo_limpio_no_da_falsos_positivos():
    assert find_secrets("def parse(x: str) -> int:\n    return int(x) * 2\n") == []
