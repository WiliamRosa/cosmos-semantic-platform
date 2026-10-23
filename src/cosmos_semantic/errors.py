class CosmosError(Exception):
    """Erro base do Cosmos Semantic."""


class ModelError(CosmosError):
    """O modelo semântico é inválido."""


class QueryError(CosmosError):
    """O pedido de consulta não pode ser atendido com o modelo atual."""


class PolicyError(CosmosError):
    """Uma política de acesso ou de LGPD negou a consulta."""


class ConnectorError(CosmosError):
    """Falha ao executar SQL numa origem."""
