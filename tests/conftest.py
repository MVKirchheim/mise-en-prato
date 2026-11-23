import pytest

from mise_en_prato.config import config


@pytest.fixture(autouse=True)
def _config_limpa(monkeypatch):
    """Cada teste começa e termina com a configuração relida do ambiente.

    `config()` é cacheada: teste que liga APP_MODE=demo e esquece o
    cache_clear deixaria o modo demo vazando para os seguintes — e os clientes
    de rede leem o modo para se recusar a sair da casa.

    A chave do Gemini sai do ambiente: o `.env` de quem desenvolve tem a chave
    de verdade, e sem isto todo TestClient subiria o laço de encartes falando
    com a API real. Teste que precisa da chave a põe de volta.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "")
    config.cache_clear()
    yield
    config.cache_clear()
