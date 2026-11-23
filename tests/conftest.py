import pytest

from mise_en_prato.config import config


@pytest.fixture(autouse=True)
def _config_limpa():
    """Cada teste começa e termina com a configuração relida do ambiente.

    `config()` é cacheada: teste que liga APP_MODE=demo e esquece o
    cache_clear deixaria o modo demo vazando para os seguintes — e os clientes
    de rede leem o modo para se recusar a sair da casa.
    """
    config.cache_clear()
    yield
    config.cache_clear()
