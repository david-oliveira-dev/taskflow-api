"""Tasks, end to end against a real PostgreSQL.

Covers what the unit tests cannot: that the role required by each route is the one the
specification asks for, that a task id resolves to its project before authorisation runs,
and that the assignee rule holds through the whole stack rather than only in the rule
function.
"""

import uuid

import pytest
from httpx import AsyncClient

from taskflow_api.enums import AuditAction, ProjectRole, TaskStatus
from tests.integration.support import (
    audit_actions,
    audit_entries,
    auth,
    new_user,
    promote_to_admin,
)

pytestmark = pytest.mark.integration


async def _project(client: AsyncClient, token: str, name: str = "Apollo") -> str:
    response = await client.post("/projects", json={"name": name}, headers=auth(token))
    assert response.status_code == 201, response.text
    project_id: str = response.json()["id"]
    return project_id


async def _add_member(
    client: AsyncClient, token: str, project_id: str, user_id: uuid.UUID, role: ProjectRole
) -> None:
    response = await client.post(
        f"/projects/{project_id}/members",
        json={"user_id": str(user_id), "project_role": role.value},
        headers=auth(token),
    )
    assert response.status_code == 201, response.text


async def _task(
    client: AsyncClient, token: str, project_id: str, **overrides: object
) -> dict[str, object]:
    payload: dict[str, object] = {"title": "Ship it"}
    payload.update(overrides)
    response = await client.post(f"/projects/{project_id}/tasks", json=payload, headers=auth(token))
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


class TestCreate:
    async def test_an_editor_can_create_a_task(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        editor_id, editor_token = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, editor_id, ProjectRole.EDITOR)

        task = await _task(client, editor_token, project_id, description="do the thing")

        assert task["project_id"] == project_id
        assert task["status"] == TaskStatus.TODO
        assert task["priority"] == 3
        assert task["assignee_id"] is None

    async def test_a_viewer_cannot_create_a_task(self, client: AsyncClient) -> None:
        """A viewer only reads — the line the specification draws between the roles."""
        _, owner_token = await new_user(client)
        viewer_id, viewer_token = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, viewer_id, ProjectRole.VIEWER)

        response = await client.post(
            f"/projects/{project_id}/tasks", json={"title": "Nope"}, headers=auth(viewer_token)
        )

        assert response.status_code == 403

    async def test_a_stranger_gets_404(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        _, outsider_token = await new_user(client)
        project_id = await _project(client, owner_token)

        response = await client.post(
            f"/projects/{project_id}/tasks",
            json={"title": "Nope"},
            headers=auth(outsider_token),
        )

        assert response.status_code == 404

    async def test_a_task_can_be_assigned_to_a_member(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, member_id, ProjectRole.EDITOR)

        task = await _task(client, owner_token, project_id, assignee_id=str(member_id))

        assert task["assignee_id"] == str(member_id)

    async def test_assigning_to_a_non_member_is_refused(self, client: AsyncClient) -> None:
        """The rule from the specification: 422, not a silent unassigned task."""
        _, owner_token = await new_user(client)
        outsider_id, _ = await new_user(client)
        project_id = await _project(client, owner_token)

        response = await client.post(
            f"/projects/{project_id}/tasks",
            json={"title": "Ship it", "assignee_id": str(outsider_id)},
            headers=auth(owner_token),
        )

        assert response.status_code == 422

    @pytest.mark.parametrize("priority", [0, 6])
    async def test_priority_outside_the_range_is_rejected(
        self, client: AsyncClient, priority: int
    ) -> None:
        """422 naming the field, rather than a 500 from the database CHECK."""
        _, token = await new_user(client)
        project_id = await _project(client, token)

        response = await client.post(
            f"/projects/{project_id}/tasks",
            json={"title": "Ship it", "priority": priority},
            headers=auth(token),
        )

        assert response.status_code == 422

    async def test_an_unknown_field_is_rejected(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)

        response = await client.post(
            f"/projects/{project_id}/tasks",
            json={"titel": "Ship it"},
            headers=auth(token),
        )

        assert response.status_code == 422

    async def test_an_archived_project_accepts_no_new_tasks(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        await client.delete(f"/projects/{project_id}", headers=auth(token))

        response = await client.post(
            f"/projects/{project_id}/tasks", json={"title": "Ship it"}, headers=auth(token)
        )

        assert response.status_code == 409


class TestListing:
    async def test_tasks_are_listed_for_their_project_only(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        mine = await _project(client, token, name="Mine")
        other = await _project(client, token, name="Other")
        task = await _task(client, token, mine)
        await _task(client, token, other)

        response = await client.get(f"/projects/{mine}/tasks", headers=auth(token))

        assert [t["id"] for t in response.json()["items"]] == [task["id"]]

    async def test_the_status_filter_narrows_the_listing(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        done = await _task(client, token, project_id, title="Done", status="done")
        await _task(client, token, project_id, title="Todo")

        response = await client.get(
            f"/projects/{project_id}/tasks", params={"status": "done"}, headers=auth(token)
        )

        assert [t["id"] for t in response.json()["items"]] == [done["id"]]

    async def test_a_viewer_can_read_the_listing(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        viewer_id, viewer_token = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, viewer_id, ProjectRole.VIEWER)
        await _task(client, owner_token, project_id)

        response = await client.get(f"/projects/{project_id}/tasks", headers=auth(viewer_token))

        assert response.status_code == 200
        assert len(response.json()["items"]) == 1

    async def test_paging_visits_every_task_exactly_once(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        created = {(await _task(client, token, project_id, title=f"T{i}"))["id"] for i in range(5)}

        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, str | int] = {"limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (
                await client.get(
                    f"/projects/{project_id}/tasks", params=params, headers=auth(token)
                )
            ).json()
            seen.extend(t["id"] for t in page["items"])
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break

        assert len(seen) == len(set(seen)) == 5
        assert set(seen) == created

    async def test_an_archived_project_still_lists_its_tasks(self, client: AsyncClient) -> None:
        """Archiving stops writes, not reading. The work has to remain auditable."""
        _, token = await new_user(client)
        project_id = await _project(client, token)
        await _task(client, token, project_id)
        await client.delete(f"/projects/{project_id}", headers=auth(token))

        response = await client.get(f"/projects/{project_id}/tasks", headers=auth(token))

        assert response.status_code == 200
        assert len(response.json()["items"]) == 1


class TestReadUpdateDelete:
    async def test_a_member_can_read_a_task_by_id(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)

        response = await client.get(f"/tasks/{task['id']}", headers=auth(token))

        assert response.status_code == 200
        assert response.json()["title"] == "Ship it"

    async def test_a_stranger_gets_404_on_a_task(self, client: AsyncClient) -> None:
        """404, not 403: a 403 would confirm this task id exists."""
        _, owner_token = await new_user(client)
        _, outsider_token = await new_user(client)
        project_id = await _project(client, owner_token)
        task = await _task(client, owner_token, project_id)

        response = await client.get(f"/tasks/{task['id']}", headers=auth(outsider_token))

        assert response.status_code == 404

    async def test_an_unknown_task_is_404(self, client: AsyncClient) -> None:
        _, token = await new_user(client)

        response = await client.get(f"/tasks/{uuid.uuid4()}", headers=auth(token))

        assert response.status_code == 404

    async def test_an_editor_can_update_a_task(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        editor_id, editor_token = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, editor_id, ProjectRole.EDITOR)
        task = await _task(client, owner_token, project_id)

        response = await client.patch(
            f"/tasks/{task['id']}",
            json={"status": "doing", "priority": 1},
            headers=auth(editor_token),
        )

        assert response.status_code == 200
        assert response.json()["status"] == TaskStatus.DOING
        assert response.json()["priority"] == 1
        assert response.json()["title"] == "Ship it", "an omitted field must be left alone"

    async def test_a_viewer_cannot_update_a_task(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        viewer_id, viewer_token = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, viewer_id, ProjectRole.VIEWER)
        task = await _task(client, owner_token, project_id)

        response = await client.patch(
            f"/tasks/{task['id']}", json={"status": "done"}, headers=auth(viewer_token)
        )

        assert response.status_code == 403

    async def test_reassigning_to_a_non_member_is_refused(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        outsider_id, _ = await new_user(client)
        project_id = await _project(client, owner_token)
        task = await _task(client, owner_token, project_id)

        response = await client.patch(
            f"/tasks/{task['id']}",
            json={"assignee_id": str(outsider_id)},
            headers=auth(owner_token),
        )

        assert response.status_code == 422

    async def test_sending_assignee_as_null_unassigns(self, client: AsyncClient) -> None:
        """The difference `exclude_unset` preserves, at the task level."""
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, member_id, ProjectRole.EDITOR)
        task = await _task(client, owner_token, project_id, assignee_id=str(member_id))

        response = await client.patch(
            f"/tasks/{task['id']}", json={"assignee_id": None}, headers=auth(owner_token)
        )

        assert response.status_code == 200
        assert response.json()["assignee_id"] is None

    async def test_an_empty_patch_is_rejected(self, client: AsyncClient) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)

        response = await client.patch(f"/tasks/{task['id']}", json={}, headers=auth(token))

        assert response.status_code == 422

    async def test_a_task_in_an_archived_project_cannot_be_changed(
        self, client: AsyncClient
    ) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)
        await client.delete(f"/projects/{project_id}", headers=auth(token))

        patch = await client.patch(
            f"/tasks/{task['id']}", json={"status": "done"}, headers=auth(token)
        )
        delete = await client.delete(f"/tasks/{task['id']}", headers=auth(token))

        assert patch.status_code == 409
        assert delete.status_code == 409
        assert (await client.get(f"/tasks/{task['id']}", headers=auth(token))).status_code == 200

    async def test_deleting_a_task_removes_it_for_real(self, client: AsyncClient) -> None:
        """Tasks are the one entity deleted physically rather than flagged."""
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)

        deleted = await client.delete(f"/tasks/{task['id']}", headers=auth(token))

        assert deleted.status_code == 204
        assert (await client.get(f"/tasks/{task['id']}", headers=auth(token))).status_code == 404
        listing = await client.get(f"/projects/{project_id}/tasks", headers=auth(token))
        assert listing.json()["items"] == []

    async def test_a_viewer_cannot_delete_a_task(self, client: AsyncClient) -> None:
        _, owner_token = await new_user(client)
        viewer_id, viewer_token = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, viewer_id, ProjectRole.VIEWER)
        task = await _task(client, owner_token, project_id)

        response = await client.delete(f"/tasks/{task['id']}", headers=auth(viewer_token))

        assert response.status_code == 403


class TestAuditTrail:
    async def test_each_task_mutation_leaves_an_entry(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)
        task_id = uuid.UUID(str(task["id"]))

        await client.patch(f"/tasks/{task['id']}", json={"status": "done"}, headers=auth(token))
        await client.delete(f"/tasks/{task['id']}", headers=auth(token))

        assert await audit_actions(migrated_dsn, task_id) == [
            AuditAction.TASK_CREATED,
            AuditAction.TASK_UPDATED,
            AuditAction.TASK_DELETED,
        ]

    async def test_the_deletion_entry_keeps_the_title(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """The row is gone; an entry naming only an id records nothing anyone can act on."""
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id, title="Wire the relays")
        task_id = uuid.UUID(str(task["id"]))

        await client.delete(f"/tasks/{task['id']}", headers=auth(token))

        deletion = [
            e
            for e in await audit_entries(migrated_dsn, task_id)
            if e.action == AuditAction.TASK_DELETED
        ]
        assert deletion[0].details["title"] == "Wire the relays"

    async def test_an_assignee_change_is_recorded_as_json(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        """A UUID in the diff has to reach JSONB as a string, or the write raises."""
        _, token = await new_user(client)
        member_id, _ = await new_user(client)
        project_id = await _project(client, token)
        await _add_member(client, token, project_id, member_id, ProjectRole.EDITOR)
        task = await _task(client, token, project_id)
        task_id = uuid.UUID(str(task["id"]))

        response = await client.patch(
            f"/tasks/{task['id']}",
            json={"assignee_id": str(member_id)},
            headers=auth(token),
        )

        assert response.status_code == 200
        update = next(
            e
            for e in await audit_entries(migrated_dsn, task_id)
            if e.action == AuditAction.TASK_UPDATED
        )
        assert update.details["changed"] == {"assignee_id": {"from": None, "to": str(member_id)}}

    async def test_a_rejected_update_leaves_no_entry(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, token = await new_user(client)
        outsider_id, _ = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)
        task_id = uuid.UUID(str(task["id"]))

        rejected = await client.patch(
            f"/tasks/{task['id']}",
            json={"assignee_id": str(outsider_id)},
            headers=auth(token),
        )

        assert rejected.status_code == 422
        assert await audit_actions(migrated_dsn, task_id) == [AuditAction.TASK_CREATED]

    async def test_a_patch_that_changes_nothing_records_nothing(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, token = await new_user(client)
        project_id = await _project(client, token)
        task = await _task(client, token, project_id)
        task_id = uuid.UUID(str(task["id"]))

        response = await client.patch(
            f"/tasks/{task['id']}", json={"title": "Ship it"}, headers=auth(token)
        )

        assert response.status_code == 200
        assert await audit_actions(migrated_dsn, task_id) == [AuditAction.TASK_CREATED]


class TestMemberRemovalStillReleasesWork:
    async def test_removing_a_member_unassigns_their_tasks_through_the_api(
        self, client: AsyncClient
    ) -> None:
        """The Phase 3a rule, now provable end to end because tasks have endpoints."""
        _, owner_token = await new_user(client)
        member_id, _ = await new_user(client)
        project_id = await _project(client, owner_token)
        await _add_member(client, owner_token, project_id, member_id, ProjectRole.EDITOR)
        task = await _task(client, owner_token, project_id, assignee_id=str(member_id))

        removed = await client.delete(
            f"/projects/{project_id}/members/{member_id}", headers=auth(owner_token)
        )

        assert removed.status_code == 204
        readback = await client.get(f"/tasks/{task['id']}", headers=auth(owner_token))
        assert readback.status_code == 200, "the task must survive its assignee's removal"
        assert readback.json()["assignee_id"] is None


class TestGlobalAdmin:
    async def test_an_admin_reaches_a_task_in_a_project_they_are_not_in(
        self, client: AsyncClient, migrated_dsn: str
    ) -> None:
        _, owner_token = await new_user(client)
        admin_id, admin_token = await new_user(client)
        await promote_to_admin(migrated_dsn, admin_id)
        project_id = await _project(client, owner_token)
        task = await _task(client, owner_token, project_id)

        response = await client.get(f"/tasks/{task['id']}", headers=auth(admin_token))

        assert response.status_code == 200
