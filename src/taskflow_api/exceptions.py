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


class NotFoundError(TaskFlowError):
    """A referenced entity does not exist."""


class ConflictError(TaskFlowError):
    """The request conflicts with the current state, such as a duplicate email."""


class BusinessRuleError(TaskFlowError):
    """The request is well-formed but violates a domain invariant.

    Distinct from `ConflictError`: a conflict is about the *state* the resource is in and
    can be resolved by changing it, while this is about the request referring to something
    that could never be valid — assigning a task to someone outside the project, adding a
    member who does not exist. That difference is what maps one to 409 and the other to 422.
    """


class InvalidCursorError(TaskFlowError):
    """A pagination cursor could not be parsed.

    Cursors arrive from user input, so a malformed one is an ordinary 4xx, not a bug.
    """


class AuthenticationError(TaskFlowError):
    """The caller could not be identified: bad credentials, or a bad token."""


class AuthorizationError(TaskFlowError):
    """The caller is known but not allowed to do this."""
