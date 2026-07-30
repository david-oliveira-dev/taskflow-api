"""Domain enumerations.

These live outside the ORM layer on purpose: services and schemas need them, and neither
should have to import a SQLAlchemy model to name a role or a task status.
"""

from enum import StrEnum


class GlobalRole(StrEnum):
    """Account-wide role, independent of any project."""

    ADMIN = "admin"
    USER = "user"


class ProjectRole(StrEnum):
    """Role a user holds inside one project.

    Ordered from most to least privileged; `outranks` relies on that order.
    """

    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"

    @property
    def rank(self) -> int:
        """Privilege rank, higher means more privileged."""
        order = {ProjectRole.VIEWER: 0, ProjectRole.EDITOR: 1, ProjectRole.OWNER: 2}
        return order[self]

    def can_act_as(self, required: "ProjectRole") -> bool:
        """Whether this role satisfies a requirement for `required`.

        Args:
            required: The minimum role an operation demands.

        Returns:
            True when this role is at least as privileged as `required`.
        """
        return self.rank >= required.rank


class TaskStatus(StrEnum):
    """Lifecycle position of a task."""

    TODO = "todo"
    DOING = "doing"
    DONE = "done"


class IdempotencyStatus(StrEnum):
    """State of a recorded idempotent request.

    `IN_PROGRESS` is the whole point of the column: it is what lets a concurrent replay be
    told apart from a completed one, so the second caller gets a 409 instead of either
    reprocessing the request or reading a response that does not exist yet.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
