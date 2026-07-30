"""Project exception hierarchy.

Every exception raised on purpose by this project inherits from `TaskFlowError`, so the
API edge can translate domain failures into the standard error envelope without ever
catching bare `Exception` (general standard, section 3).
"""


class TaskFlowError(Exception):
    """Base class for every error this project raises deliberately."""


class ConfigurationError(TaskFlowError):
    """The service is misconfigured and cannot start.

    Raised at startup rather than on first use: a missing secret should stop the process,
    not surface as a confusing 500 on the first request that happens to need it.
    """
