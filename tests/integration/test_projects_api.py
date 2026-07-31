"""Projects and membership, end to end against a real PostgreSQL.

These prove the parts that only exist once the layers are wired together: that a role check
happens before the service runs, that a rule violation becomes the right status, that the
audit entry and the mutation share a transaction, and that paging stays correct while rows
are being inserted underneath it.
"""

import uuid

import pytest
from httpx import AsyncClient

from taskflow_api.enums import AuditAction, ProjectRole
from tests.integration.support import (
    audit_actions,
    audit_entries,
    auth,
    deactivate,
    login,
    new_user,
    plant_task,
    promote_to_admin,
    register,
    task_assignee,
    unique_email,
)

pytestmark = pytest.mark.integration


async def _project(client: AsyncClient, token: str, name: str = "Apollo") -> dict[str, object]:
    """Create a project and return it."""
    response = await client.post(
        "/projects", json={"name": name, "description": "moon"}, headers=auth(token)
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


async def _member(
    client: AsyncClient,
    token: str,
    project_id: str,
    user_id: uuid.UUID,
    role: ProjectRole,
) -> None:
    """Add a member and assert it worked."""
    response = await client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(user_id), "project_role": role.value},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text


class TestCreateAndRead:
    async def test_creating_a_project_makes_the_caller_its_owner(self, client: AsyncClient) -> None:
        user_id, token = await new_user(client)

        project = await _project(client, token)

        assert project["owner_id"] == str(user_id)
        assert project["is_archived"] is False

        members = await client.get(f"/projects/{project['id']}/members", headers=auth(token))
        assert [m["user_id"] for m in members.json()["items"]] == [str(user_id)]
        assert members.json()["items"][0]["project_role"] == ProjectRole.OWNER

    async def test_creating_requires_authentication(self, client: AsyncClient) -> None:
        response = await client.post("/projects", json={"name": "Apollo"})

        assert response.status_code == 401

    async def test_a_member_can_read_the_project(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)

        response = await client.get(f"/projects/{project['id']}", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["name"] == "Apollo"

    async def test_a_stranger_gets_404_rather_than_403(self, client: AsyncClient) -> None:
        """403 would confirm the project exists, which is the disclosure being avoided."""
        _, owner_token = await new_user(client)
        _, outsider_token = await new_user(client)
        project = await _project(client, owner_token)

        response = await client.get(f"/projects/{project['id']}", headers=auth(outsider_token))

        assert response.status_code == 404

    async def test_an_unknown_project_is_404(self, client: AsyncClient) -> None:
        _, token = await new_user(client)

        response = await client.get(f"/projects/{uuid.uuid4()}", headers=auth(token))

        assert response.status_code == 404


class TestListing:
    async def test_only_your_own_projects_are_listed(self, client: AsyncClient) -> None:
        _, mine = await new_user(client)
        _, theirs = await new_user(client)
        my_project = await _project(client, mine, name="Mine")
        await _project(client, theirs, name="Theirs")

        response = await client.get("/projects", headers=auth(mine))

        assert [p["id"] for p in response.json()["items"]] == [my_project["id"]]

    async def test_archived_projects_are_hidden_unless_asked_for(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)
        await client.delete(f"/projects/{project['id']}", headers=auth(token))

        default = await client.get("/projects", headers=auth(token))
        included = await client.get(
            "/projects", params={"include_archived": True}, headers=auth(token)
        )

        assert [p["id"] for p in default.json()["items"]] == []
        assert [p["id"] for p in included.json()["items"]] == [project["id"]]

    async def test_paging_visits_every_project_exactly_once(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        created = {(await _project(client, token, name=f"P{i}"))["id"] for i in range(5)}

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await client.get("/projects", params=params, headers=auth(token))).json()
            seen.extend(p["id"] for p in page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break

        assert len(seen) == len(set(seen)) == 5
        assert set(seen) == created

    async def test_a_row_inserted_between_pages_skips_and_repeats_nothing(
        self, client: AsyncClient
    ) -> None:
        """The failure offset pagination has and keyset pagination does not.

        With `LIMIT/OFFSET`, a row inserted at the top of a descending listing shifts every
        later row down by one, so the second page re-serves an item the first page already
        returned. Anchoring on `(created_at, id)` makes the second page independent of what
        arrived after the first.
        """
        _, token = await new_user(client)
        original = {(await _project(client, token, name=f"P{i}"))["id"] for i in range(4)}

        first = (await client.get("/projects", params={"limit": 2}, headers=auth(token))).json()
        # Newest first, so this lands on page one's side of the cursor.
        await _project(client, token, name="Interloper")

        second = (
            await client.get(
                "/projects",
                params={"limit": 2, "cursor": first["next_cursor"]},
                headers=auth(token),
            )
        ).json()

        page_ids = [p["id"] for p in first["items"]] + [p["id"] for p in second["items"]]
        assert len(page_ids) == len(set(page_ids)), "a project was served twice"
        assert set(page_ids) == original, "the pages drifted off the original set"

    async def test_a_malformed_cursor_is_rejected(self, client: AsyncClient) -> None:
        """Cursors are user input, so a bad one is a 4xx and never a 500."""
        _, token = await new_user(client)

        response = await client.get(
            "/projects", params={"cursor": "not-a-cursor"}, headers=auth(token)
        )

        assert response.status_code == 422

    async def test_an_oversized_limit_is_clamped_not_rejected(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        await _project(client, token)

        response = await client.get("/projects", params={"limit": 100_000}, headers=auth(token))

        assert response.status_code == 200


class TestUpdate:
    async def test_an_owner_can_rename_the_project(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)

        response = await client.patch(
            f"/projects/{project['id']}", json={"name": "Artemis"}, headers=auth(token)
        )

        assert response.status_code == 200
        assert response.json()["name"] == "Artemis"
        assert response.json()["description"] == "moon", "an omitted field must be left alone"

    async def test_sending_description_as_null_clears_it(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)

        response = await client.patch(
            f"/projects/{project['id']}", json={"description": None}, headers=auth(token)
        )

        assert response.json()["description"] is None

    @pytest.mark.parametrize("role", [ProjectRole.EDITOR, ProjectRole.VIEWER])
    async def test_a_non_owner_cannot_update(self, client: AsyncClient, role: ProjectRole) -> None:
        _, owner_token = await new_user(client)
        member_id, member_token = await new_user(client)
        project = await _project(client, owner_token)
        await _member(client, owner_token, str(project["id"]), member_id, role)

        response = await client.patch(
            f"/projects/{project['id']}", json={"name": "Artemis"}, headers=auth(member_token)
        )

        assert response.status_code == 403

    async def test_an_empty_patch_is_rejected(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)

        response = await client.patch(f"/projects/{project['id']}", json={}, headers=auth(token))

        assert response.status_code == 422

    async def test_an_unknown_field_is_rejected(self, client: AsyncClient) -> None:
        """A misspelled field must not be silently dropped behind a 200."""
        _, token = await new_user(client)
        project = await _project(client, token)

        response = await client.patch(
            f"/projects/{project['id']}", json={"nmae": "Artemis"}, headers=auth(token)
        )

        assert response.status_code == 422

    async def test_a_patch_that_changes_nothing_records_nothing(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """An audit entry claiming an update that did not happen is noise in the trail."""
        _, token = await new_user(client)
        project = await _project(client, token)
        project_id = uuid.UUID(str(project["id"]))

        response = await client.patch(
            f"/projects/{project['id']}", json={"name": "Apollo"}, headers=auth(token)
        )

        assert response.status_code == 200
        assert await audit_actions(migrated_dsn, project_id) == [AuditAction.PROJECT_CREATED]


class TestArchive:
    async def test_archiving_hides_the_project_but_keeps_it_readable(
        self, client: AsyncClient
    ) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)

        response = await client.delete(f"/projects/{project['id']}", headers=auth(token))

        assert response.status_code == 204
        readback = await client.get(f"/projects/{project['id']}", headers=auth(token))
        assert readback.status_code == 200
        assert readback.json()["is_archived"] is True

    async def test_an_archived_project_refuses_further_mutation(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        other_id, _ = await new_user(client)
        project = await _project(client, token)
        await client.delete(f"/projects/{project['id']}", headers=auth(token))

        patch = await client.patch(
            f"/projects/{project['id']}", json={"name": "Artemis"}, headers=auth(token)
        )
        add = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(other_id), "project_role": "editor"},
            headers=auth(token),
        )
        archive_again = await client.delete(f"/projects/{project['id']}", headers=auth(token))

        assert patch.status_code == 409
        assert add.status_code == 409
        assert archive_again.status_code == 409

    async def test_an_editor_cannot_archive(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        editor_id, editor_token = await new_user(client)
        project = await _project(client, owner_token)
        await _member(client, owner_token, str(project["id"]), editor_id, ProjectRole.EDITOR)

        response = await client.delete(f"/projects/{project['id']}", headers=auth(editor_token))

        assert response.status_code == 403


class TestMembership:
    async def test_an_owner_can_add_a_member(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project = await _project(client, owner_token)

        response = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(member_id), "project_role": "editor"},
            headers=auth(owner_token),
        )

        assert response.status_code == 201
        body = response.json()
        assert body["user_id"] == str(member_id)
        assert body["project_role"] == ProjectRole.EDITOR
        assert body["email"], "the response should identify the account, not just its id"

    async def test_adding_the_same_member_twice_is_a_conflict(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        await _member(client, owner_token, str(project["id"]), member_id, ProjectRole.EDITOR)

        response = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(member_id), "project_role": "viewer"},
            headers=auth(owner_token),
        )

        assert response.status_code == 409

    async def test_adding_an_unknown_user_is_unprocessable(self, client: AsyncClient) -> None:
        """422, not 404: the member list addressed by the URL exists; the body is wrong."""
        _, owner_token = await new_user(client)
        project = await _project(client, owner_token)

        response = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(uuid.uuid4()), "project_role": "editor"},
            headers=auth(owner_token),
        )

        assert response.status_code == 422

    async def test_adding_a_deactivated_account_is_refused(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        await deactivate(migrated_dsn, member_id)
        project = await _project(client, owner_token)

        response = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(member_id), "project_role": "editor"},
            headers=auth(owner_token),
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("role", [ProjectRole.EDITOR, ProjectRole.VIEWER])
    async def test_only_an_owner_manages_membership(
        self, client: AsyncClient, role: ProjectRole
    ) -> None:
        """An editor edits work, not who has access to it."""
        _, owner_token = await new_user(client)
        member_id, member_token = await new_user(client)
        outsider_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        await _member(client, owner_token, str(project["id"]), member_id, role)

        response = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(outsider_id), "project_role": "viewer"},
            headers=auth(member_token),
        )

        assert response.status_code == 403

    async def test_members_are_listed_with_their_accounts(self, client: AsyncClient) -> None:
        owner_id, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        await _member(client, owner_token, str(project["id"]), member_id, ProjectRole.VIEWER)

        response = await client.get(f"/projects/{project['id']}/members", headers=auth(owner_token))

        assert response.status_code == 200
        by_id = {m["user_id"]: m for m in response.json()["items"]}
        assert set(by_id) == {str(owner_id), str(member_id)}
        assert by_id[str(owner_id)]["project_role"] == ProjectRole.OWNER
        assert by_id[str(member_id)]["project_role"] == ProjectRole.VIEWER
        # The account, not just its id — a member list of bare UUIDs is unusable.
        assert by_id[str(member_id)]["full_name"] == "Ada"
        assert "@" in by_id[str(member_id)]["email"]

    async def test_member_listing_pages(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        project = await _project(client, owner_token)
        for _ in range(3):
            member_id, _ = await new_user(client)
            await _member(client, owner_token, str(project["id"]), member_id, ProjectRole.VIEWER)

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (
                await client.get(
                    f"/projects/{project['id']}/members",
                    params=params,
                    headers=auth(owner_token),
                )
            ).json()
            seen.extend(m["user_id"] for m in page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break

        # Three added members plus the owner, each exactly once.
        assert len(seen) == len(set(seen)) == 4


class TestOwnerProtection:
    async def test_the_only_owner_cannot_be_demoted(self, client: AsyncClient) -> None:
        owner_id, owner_token = await new_user(client)
        project = await _project(client, owner_token)

        response = await client.patch(
            f"/projects/{project['id']}/members/{owner_id}",
            json={"project_role": "viewer"},
            headers=auth(owner_token),
        )

        assert response.status_code == 409

    async def test_the_only_owner_cannot_be_removed(self, client: AsyncClient) -> None:
        owner_id, owner_token = await new_user(client)
        project = await _project(client, owner_token)

        response = await client.delete(
            f"/projects/{project['id']}/members/{owner_id}", headers=auth(owner_token)
        )

        assert response.status_code == 409

    async def test_an_owner_can_step_down_once_another_exists(self, client: AsyncClient) -> None:
        """The escape hatch that keeps the rule from being a trap."""
        owner_id, owner_token = await new_user(client)
        heir_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        await _member(client, owner_token, str(project["id"]), heir_id, ProjectRole.OWNER)

        response = await client.patch(
            f"/projects/{project['id']}/members/{owner_id}",
            json={"project_role": "viewer"},
            headers=auth(owner_token),
        )

        assert response.status_code == 200
        assert response.json()["project_role"] == ProjectRole.VIEWER

    async def test_setting_the_role_a_member_already_has_records_nothing(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """A no-op role change must not appear in the trail as though something moved.

        It also must not trip the last-owner guard: re-asserting that the only owner is an
        owner is not a demotion, and answering 409 would be nonsense.
        """
        owner_id, owner_token = await new_user(client)
        project = await _project(client, owner_token)
        project_id = uuid.UUID(str(project["id"]))

        response = await client.patch(
            f"/projects/{project['id']}/members/{owner_id}",
            json={"project_role": "owner"},
            headers=auth(owner_token),
        )

        assert response.status_code == 200
        assert response.json()["project_role"] == ProjectRole.OWNER
        assert await audit_actions(migrated_dsn, project_id) == [AuditAction.PROJECT_CREATED]

    async def test_removing_a_non_member_is_404(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        stranger_id, _ = await new_user(client)
        project = await _project(client, owner_token)

        response = await client.delete(
            f"/projects/{project['id']}/members/{stranger_id}", headers=auth(owner_token)
        )

        assert response.status_code == 404


class TestMemberRemovalUnassignsTasks:
    async def test_removing_a_member_keeps_their_tasks_and_clears_the_assignee(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """Removal must not destroy work. The tasks stay; they lose their owner."""
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        project_id = uuid.UUID(str(project["id"]))
        await _member(client, owner_token, str(project["id"]), member_id, ProjectRole.EDITOR)
        task_id = await plant_task(migrated_dsn, project_id=project_id, assignee_id=member_id)

        response = await client.delete(
            f"/projects/{project['id']}/members/{member_id}", headers=auth(owner_token)
        )

        assert response.status_code == 204
        assert await task_assignee(migrated_dsn, task_id) is None

    async def test_the_audit_entry_says_how_many_tasks_were_unassigned(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """Otherwise a task losing its assignee is indistinguishable from a manual edit."""
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        project_id = uuid.UUID(str(project["id"]))
        await _member(client, owner_token, str(project["id"]), member_id, ProjectRole.EDITOR)
        for i in range(2):
            await plant_task(
                migrated_dsn, project_id=project_id, assignee_id=member_id, title=f"T{i}"
            )

        await client.delete(
            f"/projects/{project['id']}/members/{member_id}", headers=auth(owner_token)
        )

        removal = [
            e
            for e in await audit_entries(migrated_dsn, project_id)
            if e.action == AuditAction.MEMBER_REMOVED
        ]
        assert len(removal) == 1
        assert removal[0].details["unassigned_tasks"] == 2


class TestAuditTrail:
    async def test_every_mutation_leaves_an_entry(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        heir_id, _ = await new_user(client)
        project = await _project(client, owner_token)
        project_id = uuid.UUID(str(project["id"]))

        await client.patch(
            f"/projects/{project['id']}", json={"name": "Artemis"}, headers=auth(owner_token)
        )
        await _member(client, owner_token, str(project["id"]), member_id, ProjectRole.EDITOR)
        await _member(client, owner_token, str(project["id"]), heir_id, ProjectRole.OWNER)
        await client.patch(
            f"/projects/{project['id']}/members/{member_id}",
            json={"project_role": "viewer"},
            headers=auth(owner_token),
        )
        await client.delete(
            f"/projects/{project['id']}/members/{member_id}", headers=auth(owner_token)
        )
        await client.delete(f"/projects/{project['id']}", headers=auth(owner_token))

        assert await audit_actions(migrated_dsn, project_id) == [
            AuditAction.PROJECT_CREATED,
            AuditAction.PROJECT_UPDATED,
            AuditAction.MEMBER_ADDED,
            AuditAction.MEMBER_ADDED,
            AuditAction.MEMBER_ROLE_CHANGED,
            AuditAction.MEMBER_REMOVED,
            AuditAction.PROJECT_ARCHIVED,
        ]

    async def test_an_entry_records_who_acted_by_address(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """`actor_email` is denormalised so the trail outlives the account."""
        email = unique_email()
        body = await register(client, email)
        token = await login(client, email)
        project = await _project(client, token)

        entries = await audit_entries(migrated_dsn, uuid.UUID(str(project["id"])))

        assert entries[0].actor_id == uuid.UUID(str(body["id"]))
        assert entries[0].actor_email == email

    async def test_the_entry_records_what_changed(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, token = await new_user(client)
        project = await _project(client, token)

        await client.patch(
            f"/projects/{project['id']}", json={"name": "Artemis"}, headers=auth(token)
        )

        entries = await audit_entries(migrated_dsn, uuid.UUID(str(project["id"])))
        update = next(e for e in entries if e.action == AuditAction.PROJECT_UPDATED)
        assert update.details == {"changed": {"name": {"from": "Apollo", "to": "Artemis"}}}

    async def test_a_rejected_mutation_leaves_no_entry(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """The audit entry and the change share one transaction, so both roll back.

        This is the case a trail written outside the transaction gets wrong: it would show
        a member being added who never was.
        """
        _, owner_token = await new_user(client)
        project = await _project(client, owner_token)
        project_id = uuid.UUID(str(project["id"]))

        rejected = await client.post(
            f"/projects/{project['id']}/members",
            json={"user_id": str(uuid.uuid4()), "project_role": "editor"},
            headers=auth(owner_token),
        )

        assert rejected.status_code == 422
        assert await audit_actions(migrated_dsn, project_id) == [AuditAction.PROJECT_CREATED]


class TestAuditEndpoint:
    async def test_an_ordinary_user_is_refused(self, client: AsyncClient) -> None:
        _, token = await new_user(client)

        response = await client.get("/audit", headers=auth(token))

        assert response.status_code == 403

    async def test_it_needs_authentication(self, client: AsyncClient) -> None:
        response = await client.get("/audit")

        assert response.status_code == 401

    async def test_an_admin_reads_the_trail_of_a_project_they_are_not_in(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """The trail is global, which is exactly why it is restricted to administrators."""
        _, owner_token = await new_user(client)
        admin_id, admin_token = await new_user(client)
        await promote_to_admin(migrated_dsn, admin_id)
        project = await _project(client, owner_token)

        response = await client.get(
            "/audit",
            params={"entity_type": "project", "entity_id": str(project["id"])},
            headers=auth(admin_token),
        )

        assert response.status_code == 200
        assert [e["action"] for e in response.json()["items"]] == [AuditAction.PROJECT_CREATED]
        assert response.json()["items"][0]["actor_email"]

    async def test_filtering_narrows_the_trail(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        user_id, token = await new_user(client)
        await promote_to_admin(migrated_dsn, user_id)
        kept = await _project(client, token, name="Kept")
        await _project(client, token, name="Other")

        response = await client.get(
            "/audit", params={"entity_id": str(kept["id"])}, headers=auth(token)
        )

        assert {e["entity_id"] for e in response.json()["items"]} == {kept["id"]}


class TestGlobalAdminAccess:
    async def test_an_admin_reaches_a_project_without_being_a_member(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """Support without a database console — and every action lands in the trail."""
        _, owner_token = await new_user(client)
        admin_id, admin_token = await new_user(client)
        await promote_to_admin(migrated_dsn, admin_id)
        project = await _project(client, owner_token)

        response = await client.get(f"/projects/{project['id']}", headers=auth(admin_token))

        assert response.status_code == 200

    async def test_an_admins_own_listing_stays_membership_scoped(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """Administering one project is not the same capability as enumerating them all."""
        _, owner_token = await new_user(client)
        admin_id, admin_token = await new_user(client)
        await promote_to_admin(migrated_dsn, admin_id)
        await _project(client, owner_token, name="Someone else's")

        response = await client.get("/projects", headers=auth(admin_token))

        assert response.json()["items"] == []
