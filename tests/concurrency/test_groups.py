"""Group locks serialize invitation redemption and authority mutations."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.apps import apps
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections

from apps.groups.catalogue import seed
from apps.groups.errors import GroupError
from apps.groups.models import GroupMemberRole, GroupType, PlatformRoleAssignment
from apps.groups.permissions import has_perm
from apps.groups.services import (
    assign_role,
    bootstrap_platform,
    create_group,
    invite,
    respond_invitation,
    revoke_role,
    transfer_ownership,
    update_group,
)
from tests.factories import UserFactory
from tests.factories.locations import LocationFactory

pytestmark = [
    pytest.mark.concurrency,
    pytest.mark.security,
    pytest.mark.django_db(transaction=True),
]


def _foundation():
    seed(apps)
    owner = UserFactory(two_factor_enabled=True)
    group, _ = create_group(
        actor=owner,
        name="Concurrent group",
        group_type=GroupType.objects.get(code="DAILY_CONTRIBUTION"),
        location=LocationFactory(),
        requires_membership_approval=False,
    )
    owner.refresh_from_db()
    return owner, group


def _together(actions):
    barrier = Barrier(len(actions))

    def run(action):
        close_old_connections()
        try:
            barrier.wait(timeout=10)
            try:
                action()
                return True
            except (GroupError, PermissionDenied):
                return False
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=len(actions)) as pool:
        jobs = [pool.submit(run, action) for action in actions]
        return [job.result(timeout=30) for job in jobs]


def test_invitation_has_one_concurrent_winner():
    owner, group = _foundation()
    target = UserFactory()
    invitation, token = invite(actor=owner, group=group, target=target)

    def accept():
        return respond_invitation(actor=target, token=token, accept=True)

    assert _together([accept, accept]).count(True) == 1
    invitation.refresh_from_db()
    assert invitation.accepted_at and group.members.get(user=target).status == "ACTIVE"


def test_competing_ownership_transfers_leave_one_canonical_owner():
    owner, group = _foundation()
    first, second = [UserFactory(two_factor_enabled=True) for _ in range(2)]
    members = [
        group.members.create(user=u, status="ACTIVE", status_changed_by=owner)
        for u in (first, second)
    ]
    actions = [lambda m=m: transfer_ownership(actor=owner, group=group, member=m) for m in members]
    assert _together(actions).count(True) == 1
    group.refresh_from_db()
    assert group.owner_id in (first.pk, second.pk)
    assert (
        GroupMemberRole.objects.filter(
            group_member__group=group, group_role__code="GROUP_OWNER"
        ).count()
        == 1
    )


def test_revocation_and_permission_use_share_the_group_lock():
    owner, group = _foundation()
    target = UserFactory(two_factor_enabled=True)
    member = group.members.create(user=target, status="ACTIVE", status_changed_by=owner)
    role = group.roles.get(code="GROUP_OWNER")
    assign_role(actor=owner, group=group, member=member, role=role)
    target.refresh_from_db()
    outcomes = _together(
        [
            lambda: revoke_role(actor=owner, group=group, member=member, role=role),
            lambda: update_group(
                actor=target, group=group, values={"name": "Changed before revocation"}
            ),
        ]
    )
    assert outcomes[0]
    group.refresh_from_db()
    assert (group.name == "Changed before revocation") == outcomes[1]
    target.refresh_from_db()
    assert not has_perm(target, group, "groups.update")


def test_concurrent_platform_bootstrap_has_one_winner():
    seed(apps)
    first, second = [UserFactory(is_superuser=True, two_factor_enabled=True) for _ in range(2)]
    outcomes = _together(
        [lambda: bootstrap_platform(user=first), lambda: bootstrap_platform(user=second)]
    )
    assert outcomes.count(True) == 1
    assert PlatformRoleAssignment.objects.filter(role__code="SUPER_ADMIN").count() == 1
