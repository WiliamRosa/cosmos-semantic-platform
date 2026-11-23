class CosmosError(Exception):
    """Base error for Cosmos Semantic."""


class ModelError(CosmosError):
    """The semantic model is invalid."""


class QueryError(CosmosError):
    """The query request cannot be answered with the current model."""


class PolicyError(CosmosError):
    """An access or LGPD (personal data) policy denied the query."""


class PolicyLossError(CosmosError):
    """An export would drop row filters or personal-data marking without being allowed to."""


class MetricLossError(CosmosError):
    """An export would drop metrics the target format cannot carry without being allowed to."""


class ConnectorError(CosmosError):
    """Running SQL on a data source failed."""


class NotFoundError(QueryError):
    """The thing asked for does not exist, such as an undeclared reconciliation case (HTTP 404)."""


class RateLimitError(CosmosError):
    """Asked again too soon, such as a reconciliation case run within its interval (HTTP 429)."""
