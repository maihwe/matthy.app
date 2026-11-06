"""Scoped group operations, invitation consent, policy bounds and revocation."""

from __future__ import annotations

import io
import json
import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from django.apps import apps
from django.conf import settings
from django.core.management import call_command
from django.db import DatabaseError
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.services import mfa
from apps.groups.catalogue import seed
from apps.groups.models import (
    Group,
    GroupInvitation,
    GroupMemberRole,
    GroupPermission,
    GroupRole,
    GroupType,
    Permission,
    RoleSet,
    RoleTemplate,
)
from apps.groups.permissions import has_perm
from apps.groups.services import create_group, respond_invitation
from tests.api.test_authentication_api import AUTH, _bearer, _keys, _login
from tests.api.test_mfa_api import _code
from tests.factories import UserFactory
from tests.factories.accounts import FACTORY_PASSWORD
from tests.factories.locations import LocationFactory

pytestmark = [pytest.mark.api, pytest.mark.security, pytest.mark.django_db]
GROUPS = "/api/v1/groups/"


@pytest.fixture(scope="module")
def keypair():
    return _keys()


@pytest.fixture(autouse=True)
def security_settings(monkeypatch, keypair, db):
    monkeypatch.setattr(settings, "JWT_SIGNING_KEY", keypair[0])
    monkeypatch.setattr(settings, "JWT_VERIFYING_KEY", keypair[1])
    monkeypatch.setattr(settings, "MFA_ENCRYPTION_KEYS", Fernet.generate_key().decode())
    seed(apps)


def _user(**values):
    user = UserFactory(**values)
    setup = mfa.begin(user=user, method="TOTP", current_password=FACTORY_PASSWORD)
    codes = mfa.confirm(user=user, code=_code(setup["secret"], 0))
    user.refresh_from_db()
    return user, codes


def _client(user):
    client = APIClient(REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}")
    client.force_authenticate(user=user)
    return client


def _real_client(user, codes):
    client = APIClient(REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}")
    challenge = _login(client, user)
    response = client.post(
        f"{AUTH}mfa/challenge/",
        {"mfa_token": challenge["mfa_token"], "code": codes[0]},
        format="json",
    )
    assert response.status_code == 200, response.json()
    pair = response.json()["data"]
    _bearer(client, pair["access_token"])
    return client, pair


def _family(*, approval=False, mode="OPEN", officers=True):
    owner, owner_codes = _user()
    templates = RoleTemplate.objects.filter(scope="GROUP")
    role_set, _ = RoleSet.objects.get_or_create(code="ALL_TEST_ROLES")
    role_set.roles.set(templates)
    group_type, _ = GroupType.objects.get_or_create(
        code="ALL_TEST_ROLES", defaults={"name": "Test group", "default_role_set": role_set}
    )
    holders = {"GROUP_OWNER": (owner, owner_codes)}
    nominations = []
    if officers:
        for template in templates.exclude(code="GROUP_OWNER"):
            holder, codes = _user()
            holders[template.code] = (holder, codes)
            nominations.append({"user": holder, "roles": [template.code]})
    group, invitations = create_group(
        actor=owner,
        name="Opebi community",
        group_type=group_type,
        location=LocationFactory(),
        membership_mode=mode,
        requires_membership_approval=approval,
        officers=nominations,
    )
    owner.refresh_from_db()
    if not approval:
        for invitation in invitations:
            holder = next(
                user for user, _ in holders.values() if str(user.public_id) == invitation["user"]
            )
            respond_invitation(actor=holder, token=invitation["token"], accept=True)
            holder.refresh_from_db()
    return group, holders, invitations


def _url(group, path=""):
    return f"{GROUPS}{group.public_id}/{path}"


def test_group_creation_revokes_unverified_owner_login_and_requires_mfa_enrollment():
    user = UserFactory()
    client = APIClient(REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}")
    pair = _login(client, user)
    _bearer(client, pair["access_token"])
    response = client.post(
        GROUPS,
        {
            "name": "Daily traders",
            "group_type": "DAILY_CONTRIBUTION",
            "location": str(LocationFactory().public_id),
        },
        format="json",
    )
    assert response.status_code == 201, response.json()
    group = Group.objects.get(public_id=response.json()["data"]["id"])
    assert client.get(_url(group)).status_code == 401
    client.credentials()
    challenge = _login(client, user)
    assert challenge["enrollment_required"] is True
    setup = client.post(
        f"{AUTH}mfa/enable/",
        {
            "mfa_token": challenge["mfa_token"],
            "method": "TOTP",
            "current_password": FACTORY_PASSWORD,
        },
        format="json",
    ).json()["data"]
    response = client.post(
        f"{AUTH}mfa/confirm/",
        {"mfa_token": challenge["mfa_token"], "code": _code(setup["secret"], -1)},
        format="json",
    )
    assert response.status_code == 200
    client, _ = _real_client(user, response.json()["data"]["recovery_codes"])
    assert client.get(_url(group)).status_code == 200


def test_permission_matrix_matches_all_group_roles_and_auditor_never_writes():
    group, holders, _ = _family()
    for code, (holder, _) in holders.items():
        template = RoleTemplate.objects.get(code=code)
        for permission in Permission.objects.all():
            expected = template.policies.get(permission.code) == "required"
            if code == "AUDITOR" and permission.is_write:
                expected = False
            assert has_perm(holder, group, permission.code) is expected, (code, permission.code)
        role = group.roles.get(code=code)
        grants = {
            g.permission.code: g.granted
            for g in role.permission_grants.select_related("permission")
        }
        assert grants == {
            name: bound in ("required", "dual") for name, bound in template.policies.items()
        }


def test_every_forbidden_group_permission_is_rejected_through_api(monkeypatch):
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    quotas = {k: dict(v) for k, v in settings.RATE_LIMITS.items()}
    quotas["financial_write"]["limit"] = 10000
    monkeypatch.setattr(settings, "RATE_LIMITS", quotas)
    client = _client(owner)
    for role in group.roles.select_related("template"):
        for permission in Permission.objects.exclude(code__in=role.template.policies):
            response = client.put(
                _url(group, f"roles/{role.public_id}/permissions/"),
                {"permissions": {permission.code: True}},
                format="json",
            )
            assert response.status_code == 422, (role.code, permission.code, response.json())


def test_treasurer_permissions_do_not_cross_groups_and_object_ids_are_scoped():
    group, holders, _ = _family()
    other, _, _ = _family(officers=False)
    treasurer = holders["TREASURER"][0]
    client = _client(treasurer)
    assert has_perm(treasurer, group, "contributions.verify")
    assert not has_perm(treasurer, other, "contributions.verify")
    assert client.get(_url(other)).status_code == 404
    owner = holders["GROUP_OWNER"][0]
    client = _client(owner)
    foreign_member = other.members.get(user=other.owner)
    assert (
        client.post(
            _url(group, f"members/{foreign_member.public_id}/suspend/"), {}, format="json"
        ).status_code
        == 404
    )
    foreign_role = other.roles.get(code="MEMBER")
    member = group.members.get(user=treasurer)
    assert (
        client.post(
            _url(group, f"members/{member.public_id}/roles/"),
            {"role": str(foreign_role.public_id)},
            format="json",
        ).status_code
        == 404
    )


def test_head_requests_have_the_same_scope_as_get():
    group, holders, _ = _family(officers=False)
    client = _client(UserFactory())
    assert client.head(_url(group)).status_code == 404
    assert client.head(_url(group, "settings/")).status_code == 404
    assert _client(holders["GROUP_OWNER"][0]).head(_url(group)).status_code == 200


def test_role_assignment_and_permission_edits_cannot_escalate_self():
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    client = _client(owner)
    member = group.members.get(user=owner)
    role = group.roles.get(code="TREASURER")
    response = client.post(
        _url(group, f"members/{member.public_id}/roles/"),
        {"role": str(role.public_id)},
        format="json",
    )
    assert (
        response.status_code == 403 and response.json()["error"]["code"] == "separation_of_duties"
    )
    target = group.members.get(user=holders["MEMBER"][0])
    assert (
        client.post(
            _url(group, f"members/{target.public_id}/roles/"),
            {"role": str(role.public_id)},
            format="json",
        ).status_code
        == 403
    )
    chairman = holders["CHAIRMAN"][0]
    response = _client(chairman).put(
        _url(group, f"roles/{group.roles.get(code='CHAIRMAN').public_id}/permissions/"),
        {"permissions": {"groups.manage_settings": True}},
        format="json",
    )
    assert response.status_code == 403


def test_custom_role_create_rename_delegate_and_delete_with_bounds():
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    client = _client(owner)
    response = client.post(
        _url(group, "roles/"),
        {"code": "ASSISTANT_OWNER", "name": "Assistant owner", "template": "GROUP_OWNER"},
        format="json",
    )
    assert response.status_code == 201, response.json()
    role = GroupRole.objects.get(public_id=response.json()["data"]["id"])
    assert (
        client.patch(
            _url(group, f"roles/{role.public_id}/"), {"name": "Deputy owner"}, format="json"
        ).status_code
        == 200
    )
    member = group.members.get(user=holders["MEMBER"][0])
    assert (
        client.post(
            _url(group, f"members/{member.public_id}/roles/"),
            {"role": str(role.public_id)},
            format="json",
        ).status_code
        == 200
    )
    assert client.delete(_url(group, f"roles/{role.public_id}/")).status_code == 422
    assert (
        client.delete(
            _url(group, f"members/{member.public_id}/roles/{role.public_id}/")
        ).status_code
        == 204
    )
    assert client.delete(_url(group, f"roles/{role.public_id}/")).status_code == 204
    assert not GroupRole.objects.filter(pk=role.pk).exists()
    assert not GroupPermission.objects.filter(group_role_id=role.pk).exists()


def test_system_roles_and_required_grants_are_protected():
    group, holders, _ = _family(officers=False)
    client = _client(holders["GROUP_OWNER"][0])
    owner_role = group.roles.get(code="GROUP_OWNER")
    assert (
        client.patch(
            _url(group, f"roles/{owner_role.public_id}/"), {"name": "Replacement"}, format="json"
        ).status_code
        == 422
    )
    assert client.delete(_url(group, f"roles/{owner_role.public_id}/")).status_code == 422
    assert (
        client.put(
            _url(group, f"roles/{owner_role.public_id}/permissions/"),
            {"permissions": {"groups.view": False}},
            format="json",
        ).status_code
        == 422
    )


def test_owner_cannot_leave_be_suspended_or_removed_before_transfer():
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    client = _client(owner)
    member = group.members.get(user=owner)
    for action in ("suspend", "remove"):
        assert (
            client.post(
                _url(group, f"members/{member.public_id}/{action}/"), {}, format="json"
            ).status_code
            == 422
        )
    assert client.post(_url(group, "leave/"), {}, format="json").status_code == 422
    next_owner = group.members.get(user=holders["MEMBER"][0])
    assert (
        client.post(
            _url(group, "transfer-ownership/"), {"member": str(next_owner.public_id)}, format="json"
        ).status_code
        == 200
    )
    group.refresh_from_db()
    assert group.owner_id == next_owner.user_id
    assert not GroupMemberRole.objects.filter(
        group_member=member, group_role__code="GROUP_OWNER"
    ).exists()
    assert GroupMemberRole.objects.filter(
        group_member=next_owner, group_role__code="GROUP_OWNER"
    ).exists()


def test_optional_grant_revocation_is_immediate_with_cache_and_real_tokens():
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    collector, codes = holders["COLLECTOR"]
    role = group.roles.get(code="COLLECTOR")
    member_client, pair = _real_client(collector, codes)
    assert not has_perm(collector, group, "members.invite")
    owner_client = _client(owner)
    assert (
        owner_client.put(
            _url(group, f"roles/{role.public_id}/permissions/"),
            {"permissions": {"members.invite": True}},
            format="json",
        ).status_code
        == 200
    )
    assert member_client.get(_url(group)).status_code == 401
    collector.refresh_from_db()
    assert has_perm(collector, group, "members.invite")
    member_client, _ = _real_client(collector, codes[1:])
    assert (
        owner_client.put(
            _url(group, f"roles/{role.public_id}/permissions/"),
            {"permissions": {"members.invite": False}},
            format="json",
        ).status_code
        == 200
    )
    assert member_client.get(_url(group)).status_code == 401
    collector.refresh_from_db()
    assert not has_perm(collector, group, "members.invite")
    assert (
        member_client.post(
            f"{AUTH}token/refresh/", {"refresh_token": pair["refresh_token"]}, format="json"
        ).status_code
        == 401
    )


def test_permission_cache_outage_uses_current_database_state(monkeypatch):
    from apps.groups import permissions

    group, holders, _ = _family(officers=False)
    owner = holders["GROUP_OWNER"][0]

    def unavailable(*args, **kwargs):
        raise OSError("synthetic cache outage")

    monkeypatch.setattr(permissions.cache, "get", unavailable)
    monkeypatch.setattr(permissions.cache, "set", unavailable)
    assert has_perm(owner, group, "groups.update")
    assert not has_perm(UserFactory(), group, "groups.update")


def test_invitation_accept_requires_target_consent_and_is_single_use():
    group, holders, _ = _family(officers=False)
    owner = holders["GROUP_OWNER"][0]
    target, _ = _user()
    response = _client(owner).post(
        _url(group, "members/invite/"), {"user": str(target.public_id)}, format="json"
    )
    assert response.status_code == 201 and response["Cache-Control"] == "no-store"
    token = response.json()["data"]["token"]
    row = GroupInvitation.objects.get(public_id=response.json()["data"]["id"])
    assert token != row.token_hash
    assert (
        _client(UserFactory())
        .post(f"{GROUPS}invitations/accept/", {"token": token}, format="json")
        .status_code
        == 404
    )
    response = _client(target).post(f"{GROUPS}invitations/accept/", {"token": token}, format="json")
    assert response.status_code == 200 and response.json()["data"]["status"] == "ACTIVE"
    target.refresh_from_db()
    assert (
        _client(target)
        .post(f"{GROUPS}invitations/accept/", {"token": token}, format="json")
        .status_code
        == 404
    )
    assert group.members.get(user=target).history.count() == 2


@pytest.mark.parametrize("decision", ["decline", "expiry", "resend"])
def test_invitation_decline_expiry_and_resend_retire_old_token(decision):
    group, holders, _ = _family(officers=False)
    client = _client(holders["GROUP_OWNER"][0])
    target, _ = _user()
    token = client.post(
        _url(group, "members/invite/"), {"user": str(target.public_id)}, format="json"
    ).json()["data"]["token"]
    if decision == "decline":
        assert (
            _client(target)
            .post(f"{GROUPS}invitations/decline/", {"token": token}, format="json")
            .status_code
            == 204
        )
    elif decision == "expiry":
        GroupInvitation.objects.filter(group=group).update(
            created_at=timezone.now() - timedelta(days=8),
            expires_at=timezone.now() - timedelta(seconds=1),
        )
    else:
        fresh = client.post(
            _url(group, "members/invite/"), {"user": str(target.public_id)}, format="json"
        ).json()["data"]["token"]
        assert fresh != token
    assert (
        _client(target)
        .post(f"{GROUPS}invitations/accept/", {"token": token}, format="json")
        .status_code
        == 404
    )


def test_membership_approval_suspend_reinstate_leave_and_history():
    group, holders, _ = _family(officers=False, approval=True)
    owner = holders["GROUP_OWNER"][0]
    target, _ = _user()
    response = _client(target).post(_url(group, "join/"), {}, format="json")
    assert response.status_code == 201 and response.json()["data"]["status"] == "PENDING_APPROVAL"
    member = group.members.get(user=target)
    client = _client(owner)
    assert (
        client.post(
            _url(group, f"members/{member.public_id}/approve/"), {}, format="json"
        ).status_code
        == 200
    )
    target.refresh_from_db()
    assert has_perm(target, group, "groups.view")
    assert (
        client.post(
            _url(group, f"members/{member.public_id}/suspend/"),
            {"reason": "Membership review"},
            format="json",
        ).status_code
        == 200
    )
    target.refresh_from_db()
    assert not has_perm(target, group, "groups.view")
    assert (
        client.post(
            _url(group, f"members/{member.public_id}/reinstate/"), {}, format="json"
        ).status_code
        == 200
    )
    target.refresh_from_db()
    assert _client(target).post(_url(group, "leave/"), {}, format="json").status_code == 200
    assert list(member.history.order_by("occurred_at").values_list("to_status", flat=True)) == [
        "PENDING_APPROVAL",
        "ACTIVE",
        "SUSPENDED",
        "ACTIVE",
        "LEFT",
    ]
    history = client.get(_url(group, f"members/{member.public_id}/history/")).json()
    assert len(history["data"]) == 5 and "pagination" in history
    assert not member.role_assignments.exists()


def test_removed_member_rejoins_without_restoring_old_officer_roles():
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    target = holders["TREASURER"][0]
    member = group.members.get(user=target)
    assert (
        _client(owner)
        .post(_url(group, f"members/{member.public_id}/remove/"), {}, format="json")
        .status_code
        == 200
    )
    target.refresh_from_db()
    assert _client(target).post(_url(group, "join/"), {}, format="json").status_code == 201
    assert list(member.role_assignments.values_list("group_role__code", flat=True)) == ["MEMBER"]


def test_invite_only_group_rejects_uninvited_join():
    group, _, _ = _family(officers=False, mode="INVITE_ONLY")
    assert _client(UserFactory()).post(_url(group, "join/"), {}, format="json").status_code == 404


def test_group_settings_rules_type_change_and_archive_preserve_history():
    group, holders, _ = _family(officers=False)
    owner = holders["GROUP_OWNER"][0]
    client = _client(owner)
    original_roles = set(group.roles.values_list("code", flat=True))
    assert (
        client.patch(
            _url(group), {"name": "Renamed", "group_type": "DAILY_CONTRIBUTION"}, format="json"
        ).status_code
        == 200
    )
    assert set(group.roles.values_list("code", flat=True)) == original_roles
    assert (
        client.put(
            _url(group, "settings/"),
            {"membership_mode": "APPLICATION", "requires_membership_approval": True},
            format="json",
        ).status_code
        == 200
    )
    assert client.get(_url(group, "settings/")).json()["data"]["membership_mode"] == "APPLICATION"
    assert (
        client.put(
            _url(group, "rules/"), {"rules": "Contribute together."}, format="json"
        ).status_code
        == 200
    )
    assert client.get(_url(group, "rules/")).json()["data"]["rules"] == "Contribute together."
    member = UserFactory()
    group.members.create(user=member, status="ACTIVE", status_changed_by=owner)
    member.refresh_from_db()
    assert client.post(_url(group, "archive/"), {}, format="json").status_code == 200
    assert client.get(_url(group)).status_code == 200
    assert client.patch(_url(group), {"name": "Forbidden"}, format="json").status_code == 403
    assert _client(member).post(_url(group, "leave/"), {}, format="json").status_code == 409
    assert group.members.get(user=owner).history.exists()


def test_location_discovery_and_counts_return_only_public_active_groups():
    group, holders, _ = _family(officers=False)
    response = APIClient().get(f"/api/v1/locations/{group.location.public_id}/")
    assert response.status_code == 200 and response.json()["data"]["group_count"] == 1
    response = APIClient().get(f"/api/v1/locations/{group.location.public_id}/groups/")
    assert response.status_code == 200 and response.json()["data"][0]["id"] == str(group.public_id)
    assert "owner" not in response.json()["data"][0]
    assert (
        _client(holders["GROUP_OWNER"][0])
        .post(_url(group, "archive/"), {}, format="json")
        .status_code
        == 200
    )
    assert (
        APIClient().get(f"/api/v1/locations/{group.location.public_id}/groups/").json()["data"]
        == []
    )


def test_member_lists_are_paginated_and_list_scope_ignores_other_users_departures():
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    client = _client(owner)
    first = client.get(_url(group, "members/?limit=2")).json()
    assert len(first["data"]) == 2 and first["pagination"]["has_more"]
    next_page = client.get(
        _url(group, f"members/?limit=2&cursor={first['pagination']['next_cursor']}")
    ).json()
    assert {m["id"] for m in first["data"]}.isdisjoint({m["id"] for m in next_page["data"]})
    target = holders["MEMBER"][0]
    assert _client(target).post(_url(group, "leave/"), {}, format="json").status_code == 200
    assert client.get(GROUPS).json()["data"][0]["id"] == str(group.public_id)


@pytest.mark.parametrize(
    "payload", [{"owner": "unexpected"}, {"location": "malformed-uuid"}, {"group_type": "UNKNOWN"}]
)
def test_group_create_rejects_unknown_fields_and_invalid_identifiers(payload):
    user = UserFactory()
    body = {
        "name": "Validation",
        "group_type": "DAILY_CONTRIBUTION",
        "location": str(LocationFactory().public_id),
        **payload,
    }
    assert _client(user).post(GROUPS, body, format="json").status_code == 400
    assert not Group.objects.filter(created_by=user).exists()


def test_audit_failure_rolls_back_group_roles_mfa_state_and_membership(monkeypatch):
    from apps.groups import services

    user = UserFactory()

    def unavailable(*args, **kwargs):
        raise DatabaseError("synthetic audit failure")

    monkeypatch.setattr(services, "record", unavailable)
    response = _client(user).post(
        GROUPS,
        {
            "name": "Rollback",
            "group_type": "DAILY_CONTRIBUTION",
            "location": str(LocationFactory().public_id),
        },
        format="json",
    )
    assert response.status_code == 500
    user.refresh_from_db()
    assert user.authorization_version == 0 and not user.role_mfa_required
    assert not Group.objects.filter(created_by=user).exists()


def test_platform_roles_are_explicit_protected_and_never_authorize_money():
    from apps.groups.services import bootstrap_platform

    super_admin, _ = _user(is_staff=True, is_superuser=True)
    bootstrap_platform(user=super_admin)
    super_admin.refresh_from_db()
    client = _client(super_admin)
    target, _ = _user()
    role = GroupRole.objects.get(group=None, code="PLATFORM_ADMIN")
    assert (
        client.post(
            "/api/v1/platform/roles/",
            {"user": str(target.public_id), "role": str(role.public_id)},
            format="json",
        ).status_code
        == 204
    )
    target.refresh_from_db()
    assert has_perm(target, None, "platform.manage_users")
    for principal in (super_admin, target):
        for permission in Permission.objects.filter(
            category__in=["contributions", "disbursements", "settlements"]
        ):
            assert not has_perm(principal, None, permission.code)
        assert not has_perm(principal, None, "loans.approve")
        assert not has_perm(principal, None, "ledger.post_adjustment")
    assert (
        client.post(
            "/api/v1/platform/roles/",
            {"user": str(super_admin.public_id), "role": str(role.public_id)},
            format="json",
        ).status_code
        == 403
    )
    assert (
        _client(target)
        .post(
            "/api/v1/platform/roles/",
            {"user": str(UserFactory().public_id), "role": str(role.public_id)},
            format="json",
        )
        .status_code
        == 403
    )
    assert (
        client.delete(
            f"/api/v1/platform/users/{target.public_id}/roles/{role.public_id}/"
        ).status_code
        == 204
    )
    target.refresh_from_db()
    assert not has_perm(target, None, "platform.manage_users")


def test_platform_permission_bounds_and_group_type_creation():
    from apps.groups.services import bootstrap_platform

    admin, _ = _user(is_staff=True, is_superuser=True)
    bootstrap_platform(user=admin)
    admin.refresh_from_db()
    client = _client(admin)
    role = GroupRole.objects.get(group=None, code="SUPPORT_ADMIN")
    assert (
        client.put(
            f"/api/v1/platform/roles/{role.public_id}/permissions/",
            {"permissions": {"platform.manage_users": True}},
            format="json",
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/v1/platform/roles/{role.public_id}/permissions/",
            {"permissions": {"disbursements.approve": True}},
            format="json",
        ).status_code
        == 422
    )
    response = client.post(
        "/api/v1/group-types/",
        {"code": "NEIGHBOURHOOD", "name": "Neighbourhood", "roles": ["GROUP_OWNER", "MEMBER"]},
        format="json",
    )
    assert response.status_code == 201, response.json()
    assert {r["code"] for r in client.get("/api/v1/platform/roles/").json()["data"]} == {
        "SUPER_ADMIN",
        "PLATFORM_ADMIN",
        "SUPPORT_ADMIN",
    }
    assert "NEIGHBOURHOOD" in {
        t["code"] for t in APIClient().get("/api/v1/group-types/").json()["data"]
    }


def test_seed_command_preserves_optional_platform_and_group_customisations():
    from apps.groups.services import bootstrap_platform

    admin, _ = _user(is_staff=True, is_superuser=True)
    bootstrap_platform(user=admin)
    admin.refresh_from_db()
    role = GroupRole.objects.get(group=None, code="SUPPORT_ADMIN")
    assert (
        _client(admin)
        .put(
            f"/api/v1/platform/roles/{role.public_id}/permissions/",
            {"permissions": {"platform.manage_users": True}},
            format="json",
        )
        .status_code
        == 200
    )
    call_command("seed_group_catalogue", stdout=io.StringIO())
    assert role.permission_grants.get(permission__code="platform.manage_users").granted
    frozen = json.loads((Path("apps/groups/migrations/catalogue_v1.json")).read_text())
    assert len(frozen["permissions"]) == Permission.objects.count()


def test_every_group_mutation_requires_cookie_csrf():
    user = UserFactory()
    client = APIClient(enforce_csrf_checks=True)
    client.force_login(user)
    response = client.post(
        GROUPS,
        {
            "name": "CSRF",
            "group_type": "DAILY_CONTRIBUTION",
            "location": str(LocationFactory().public_id),
        },
        format="json",
    )
    assert response.status_code == 403


def test_group_role_revocation_invalidates_browser_admin_cookie():
    from tests.api.test_browser_mfa import _finish, _password

    owner, _ = _user(is_staff=True)
    group, _ = create_group(
        actor=owner,
        name="Browser roles",
        group_type=GroupType.objects.get(code="DAILY_CONTRIBUTION"),
        location=LocationFactory(),
    )
    owner.refresh_from_db()
    # Fresh recovery codes after the role bootstrap invalidated earlier proofs.
    recovery = mfa._recovery_codes(owner)
    client = APIClient(
        enforce_csrf_checks=True, REMOTE_ADDR=f"2001:db8::{uuid.uuid4().int % 65535:x}"
    )
    assert _password(client, owner).status_code == 302
    assert _finish(client, recovery[0]).status_code == 302

    # A transfer to an active nominee is the allowed way to revoke ownership.
    target, _ = _user()
    group.members.create(user=target, status="ACTIVE", status_changed_by=owner)
    from apps.groups.services import transfer_ownership

    transfer_ownership(actor=owner, group=group, member=group.members.get(user=target))
    assert client.get("/admin/").status_code == 302
    assert client.get(f"{AUTH}sessions/").status_code == 401


def test_platform_group_type_edits_preserve_existing_groups_and_survive_seed():
    from apps.groups.services import bootstrap_platform

    admin, _ = _user(is_superuser=True)
    bootstrap_platform(user=admin)
    admin.refresh_from_db()
    client = _client(admin)
    kind = GroupType.objects.get(code="DAILY_CONTRIBUTION")
    owner, _ = _user()
    existing, _ = create_group(
        actor=owner, name="Original roles", group_type=kind, location=LocationFactory()
    )
    path = f"/api/v1/group-types/{kind.code}/"
    response = client.patch(
        path,
        {"name": "Custom daily", "description": "Platform override", "roles": ["TREASURER"]},
        format="json",
    )
    assert response.status_code == 200, response.json()
    seed(apps)
    kind.refresh_from_db()
    assert kind.name == "Custom daily" and list(
        kind.default_role_set.roles.values_list("code", flat=True)
    ) == ["TREASURER"]
    assert set(existing.roles.values_list("code", flat=True)) == {
        "GROUP_OWNER",
        "MEMBER",
        "COLLECTOR",
    }
    newcomer = UserFactory()
    future, _ = create_group(
        actor=newcomer, name="New defaults", group_type=kind, location=existing.location
    )
    assert set(future.roles.values_list("code", flat=True)) == {
        "GROUP_OWNER",
        "MEMBER",
        "TREASURER",
    }
    assert _client(owner).patch(path, {"name": "No authority"}, format="json").status_code == 403
    assert client.patch(path, {"roles": []}, format="json").status_code == 400
    assert client.patch(path, {"code": "REPLACED"}, format="json").status_code == 400
    assert APIClient().get(path).status_code == 200


def test_location_administration_requires_platform_permission_and_valid_tree():
    from apps.groups.services import bootstrap_platform

    admin, _ = _user(is_superuser=True)
    bootstrap_platform(user=admin)
    admin.refresh_from_db()
    client = _client(admin)
    root = LocationFactory()
    path = "/api/v1/locations/"
    payload = {
        "country": root.country.iso2,
        "type": root.location_type.code,
        "name": "Platform-created place",
        "slug": "platform-created-place",
        "latitude": "6.500000",
        "longitude": "3.400000",
    }
    assert APIClient().post(path, payload, format="json").status_code == 401
    user = UserFactory()
    assert _client(user).post(path, payload, format="json").status_code == 403
    response = client.post(path, payload, format="json")
    assert response.status_code == 201, response.json()
    detail = f"{path}{response.json()['data']['id']}/"
    assert APIClient().get(detail).json()["data"]["group_count"] == 0
    assert client.post(path, payload, format="json").status_code == 422
    assert (
        client.post(
            path, {**payload, "slug": "wrong-tree", "parent_id": str(root.public_id)}, format="json"
        ).status_code
        == 422
    )
    assert client.post(path, {**payload, "type": "UNKNOWN"}, format="json").status_code == 400
    assert (
        client.patch(detail, {"parent_id": str(root.public_id)}, format="json").status_code == 400
    )
    assert client.patch(detail, {"latitude": "91"}, format="json").status_code == 400
    assert (
        client.patch(
            detail, {"name": "Renamed place", "is_active": False}, format="json"
        ).status_code
        == 200
    )
    assert APIClient().get(detail).status_code == 404
    assert client.patch(detail, {"is_active": True}, format="json").status_code == 200
    assert APIClient().get(detail).status_code == 200
    assert APIClient().get(f"{path}?country={root.country.iso2}").status_code == 200
    assert client.delete(detail).status_code == 204
    assert APIClient().get(detail).status_code == 404
    assert client.patch(detail, {"is_active": True}, format="json").status_code == 200


def test_role_template_catalogue_is_authenticated_and_frozen():
    path = "/api/v1/role-templates/"
    assert APIClient().get(path).status_code == 401
    user = UserFactory()
    client = _client(user)
    response = client.get(f"{path}?limit=2")
    assert response.status_code == 200
    assert len(response.json()["data"]) == 2 and response.json()["pagination"]["has_more"]
    assert response.json()["data"][0]["scope"] == "GROUP"
    assert client.post(path, {}, format="json").status_code == 405


def test_group_admin_is_scoped_read_only_and_hides_invitation_secrets():
    from django.contrib import admin
    from django.test import RequestFactory

    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    outsider, _ = _user(is_staff=True)
    other, _ = create_group(
        actor=outsider,
        name="Outside admin scope",
        group_type=group.group_type,
        location=group.location,
    )
    outsider.refresh_from_db()
    request = RequestFactory().get("/admin/")
    request.user = owner
    inspection = admin.site._registry[Group]
    assert set(inspection.get_queryset(request).values_list("pk", flat=True)) == {group.pk}
    assert inspection.has_view_permission(request, group)
    assert not inspection.has_view_permission(request, other)
    assert not inspection.has_add_permission(request)
    assert not inspection.has_change_permission(request, group)
    assert not inspection.has_delete_permission(request, group)
    for model in (GroupRole, GroupPermission, GroupMemberRole, GroupInvitation):
        view = admin.site._registry[model]
        assert view.get_queryset(request).exists()
        assert not view.has_change_permission(request) and not view.has_delete_permission(request)
    invitation_admin = admin.site._registry[GroupInvitation]
    assert "token_hash" not in invitation_admin.get_fields(request)
    assert "invited_phone" not in invitation_admin.get_fields(request)
    request.user = UserFactory(is_staff=True, is_superuser=True)
    assert not inspection.get_queryset(request).exists()


def test_group_write_rate_limit_returns_retry_after(monkeypatch):
    group, holders, _ = _family()
    owner = holders["GROUP_OWNER"][0]
    limits = dict(settings.RATE_LIMITS)
    limits["financial_write"] = {"limit": 1, "window_seconds": 60}
    monkeypatch.setattr(settings, "RATE_LIMITS", limits)
    client = _client(owner)
    path = f"{GROUPS}{group.public_id}/settings/"
    payload = {"membership_mode": "OPEN", "requires_membership_approval": True}
    assert client.put(path, payload, format="json").status_code == 200
    response = client.put(path, payload, format="json")
    assert response.status_code == 429 and int(response["Retry-After"]) > 0


def test_group_openapi_describes_envelopes_pages_tokens_and_decline():
    from drf_spectacular.generators import SchemaGenerator

    schema = SchemaGenerator().get_schema(request=None, public=True)
    components = schema["components"]["schemas"]

    def resolve(value):
        return components[value["$ref"].rsplit("/", 1)[1]] if "$ref" in value else value

    def body(path, method, status):
        return resolve(
            schema["paths"][path][method]["responses"][str(status)]["content"]["application/json"][
                "schema"
            ]
        )

    created = body("/api/v1/groups/", "post", 201)
    resource = resolve(created["properties"]["data"])
    invitation = resolve(resource["properties"]["invitations"]["items"])
    assert "token" in invitation["properties"]
    page = body("/api/v1/groups/{public_id}/members/", "get", 200)
    assert set(page["properties"]) == {"data", "pagination"}
    assert page["properties"]["data"]["type"] == "array"
    issued = body("/api/v1/groups/{public_id}/members/invite/", "post", 201)
    assert "token" in resolve(issued["properties"]["data"])["properties"]
    decline = schema["paths"]["/api/v1/groups/invitations/decline/"]["post"]["responses"]
    assert "204" in decline and "content" not in decline["204"]
    detail = body("/api/v1/locations/{public_id}/", "get", 200)
    assert "group_count" in resolve(detail["properties"]["data"])["properties"]


def test_platform_bootstrap_command_handles_invalid_public_uuid():
    from django.core.management.base import CommandError

    with pytest.raises(CommandError):
        call_command("bootstrap_platform_admin", "invalid-uuid")


def test_location_admin_uses_platform_permissions_and_audited_api_for_writes():
    from django.contrib import admin
    from django.test import RequestFactory

    from apps.groups.services import bootstrap_platform
    from apps.locations.models import Location

    location = LocationFactory()
    operator, _ = _user(is_staff=True, is_superuser=True)
    request = RequestFactory().get("/admin/")
    request.user = operator
    inspection = admin.site._registry[Location]
    assert not inspection.get_queryset(request).exists()
    bootstrap_platform(user=operator)
    operator.refresh_from_db()
    assert inspection.has_view_permission(request, location)
    assert inspection.get_queryset(request).filter(pk=location.pk).exists()
    assert not inspection.has_add_permission(request)
    assert not inspection.has_change_permission(request, location)
    assert not inspection.has_delete_permission(request, location)
