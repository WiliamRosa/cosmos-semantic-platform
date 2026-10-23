class CosmosError(Exception):
    """Base error for Cosmos Semantic."""


class ModelError(CosmosError):
    """The semantic model is invalid."""


class QueryError(CosmosError):
    """The query request cannot be answered with the current model."""


class PolicyError(CosmosError):
    """An access or LGPD (personal data) policy denied the query."""


class ConnectorError(CosmosError):
    """Running SQL on a data source failed."""
