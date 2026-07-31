"""The domain rules, offline.

These need no database and no HTTP client because the rules take no I/O — which is the
point of keeping them in `services/rules.py`. Every branch that can reject a request is
exercised here, so the integration tests can concentrate on the wiring instead of
re-proving the logic through a socket.
"""

import uuid

import pytest

from taskflow_api.db.models import Membership, Project, User
from taskflow_api.enums import GlobalRole, ProjectRole, TaskStatus
from taskflow_api.exceptions import BusinessRuleError, ConflictError
from taskflow_api.services import rules


def _project(*, archived: bool = False) -> Project:
    """A project instance, unattached to any session."""
    return Project(id=uuid.uuid4(), name="Apollo", owner_id=uuid.uuid4(), is_archived=archived)


def _user(*, active: bool = True, role: GlobalRole = GlobalRole.USER) -> User:
    return User(
        id=uuid.uuid4(),
        email="ada@example.com",
        hashed_password="x",
        full_name="Ada",
        global_role=role,
        is_active=active,
    )


class TestEnsureMutable:
    def test_an_open_project_may_be_written(self) -> None:
        rules.ensure_mutable(_project())

    def test_an_archived_project_may_not(self) -> None:
        with pytest.raises(ConflictError, match="archived"):
            rules.ensure_mutable(_project(archived=True))


class TestEnsureNotLastOwner:
    def test_the_only_owner_is_protected(self) -> None:
        """Otherwise the project reaches a state nobody can administer or repair."""
        with pytest.raises(ConflictError, match="only owner"):
            rules.ensure_not_last_owner(current_role=ProjectRole.OWNER, owner_count=1)

    def test_an_owner_among_several_is_not(self) -> None:
        rules.ensure_not_last_owner(current_role=ProjectRole.OWNER, owner_count=2)

    @pytest.mark.parametrize("role", [ProjectRole.EDITOR, ProjectRole.VIEWER])
    def test_a_non_owner_is_never_protected(self, role: ProjectRole) -> None:
        rules.ensure_not_last_owner(current_role=role, owner_count=1)

    def test_a_project_with_no_owners_still_protects_an_owner_row(self) -> None:
        """Defensive: a count of zero must not read as "plenty of owners left"."""
        with pytest.raises(ConflictError):
            rules.ensure_not_last_owner(current_role=ProjectRole.OWNER, owner_count=0)


class TestEnsureCanBeMember:
    def test_an_active_account_is_accepted(self) -> None:
        user = _user()

        assert rules.ensure_can_be_member(user) is user

    def test_a_missing_account_is_rejected(self) -> None:
        with pytest.raises(BusinessRuleError, match="No such user"):
            rules.ensure_can_be_member(None)

    def test_a_deactivated_account_is_rejected(self) -> None:
        with pytest.raises(BusinessRuleError, match="deactivated"):
            rules.ensure_can_be_member(_user(active=False))


class TestEnsureAssigneeIsMember:
    def test_a_member_may_be_assigned(self) -> None:
        rules.ensure_assignee_is_member(
            assignee_id=uuid.uuid4(), membership=Membership(project_role=ProjectRole.VIEWER)
        )

    def test_a_non_member_may_not(self) -> None:
        """Work handed to someone who cannot open the project is work nobody will do.

        It also leaks the project's existence to an outsider.
        """
        with pytest.raises(BusinessRuleError, match="must be a member"):
            rules.ensure_assignee_is_member(assignee_id=uuid.uuid4(), membership=None)

    def test_unassigning_is_always_allowed(self) -> None:
        """The `None` case is how a departing member's work is released, not an error."""
        rules.ensure_assignee_is_member(assignee_id=None, membership=None)


class TestJsonSafe:
    def test_uuids_become_strings(self) -> None:
        """Audit details go through `json.dumps`, which has never heard of a UUID."""
        assignee = uuid.uuid4()

        safe = rules.json_safe({"assignee_id": {"from": None, "to": assignee}})

        assert safe == {"assignee_id": {"from": None, "to": str(assignee)}}

    def test_enum_members_survive_as_their_values(self) -> None:
        safe = rules.json_safe({"status": {"from": TaskStatus.TODO, "to": TaskStatus.DONE}})

        assert safe["status"]["to"] == "done"

    def test_ordinary_values_are_untouched(self) -> None:
        safe = rules.json_safe({"priority": {"from": 3, "to": 1}})

        assert safe == {"priority": {"from": 3, "to": 1}}


class TestEffectiveRole:
    def test_a_member_acts_with_their_own_role(self) -> None:
        assert (
            rules.effective_role(user=_user(), membership_role=ProjectRole.EDITOR)
            is ProjectRole.EDITOR
        )

    def test_a_non_member_has_no_role(self) -> None:
        assert rules.effective_role(user=_user(), membership_role=None) is None

    def test_a_global_admin_acts_as_owner_without_a_membership(self) -> None:
        """The bypass that lets an administrator support a project they are not in."""
        admin = _user(role=GlobalRole.ADMIN)

        assert rules.effective_role(user=admin, membership_role=None) is ProjectRole.OWNER

    def test_a_global_admin_who_is_a_member_keeps_the_membership_role(self) -> None:
        """An explicit membership is a deliberate statement and wins over the bypass."""
        admin = _user(role=GlobalRole.ADMIN)

        role = rules.effective_role(user=admin, membership_role=ProjectRole.VIEWER)

        assert role is ProjectRole.VIEWER


class TestChangedFields:
    def test_only_genuine_changes_are_reported(self) -> None:
        diff = rules.changed_fields(
            current={"name": "Apollo", "description": "moon"},
            requested={"name": "Artemis", "description": "moon"},
        )

        assert diff == {"name": {"from": "Apollo", "to": "Artemis"}}

    def test_rewriting_a_value_to_itself_changes_nothing(self) -> None:
        """Keeps the audit trail free of entries that describe no change."""
        diff = rules.changed_fields(current={"name": "Apollo"}, requested={"name": "Apollo"})

        assert diff == {}

    def test_clearing_a_field_is_a_change(self) -> None:
        diff = rules.changed_fields(
            current={"description": "moon"}, requested={"description": None}
        )

        assert diff == {"description": {"from": "moon", "to": None}}

    def test_setting_a_field_that_was_empty_is_a_change(self) -> None:
        diff = rules.changed_fields(
            current={"description": None}, requested={"description": "moon"}
        )

        assert diff == {"description": {"from": None, "to": "moon"}}

    def test_fields_not_requested_are_left_out(self) -> None:
        diff = rules.changed_fields(
            current={"name": "Apollo", "description": "moon"}, requested={"name": "Artemis"}
        )

        assert set(diff) == {"name"}
