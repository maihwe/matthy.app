"""Scoped group APIs. Every mutation repeats its permission check in the service."""

from __future__ import annotations

from typing import Any, ClassVar, cast

from django.shortcuts import get_object_or_404
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import NotFound
from rest_framework.permissions import AllowAny, BasePermission, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from apps.accounts.models import User
from apps.accounts.services import mfa
from apps.accounts.views import AccountAPIView
from apps.audit.services import AuditContext
from apps.groups import serializers as contract
from apps.groups import services
from apps.groups.errors import GroupConflict, GroupNotFound, GroupRuleViolation
from apps.groups.models import Group, GroupMember, GroupRole, GroupType, Permission, RoleTemplate
from apps.groups.permissions import has_perm
from apps.locations.models import Location
from apps.locations.serializers import LocationSerializer
from apps.locations.views import LocationDetailView, LocationListView
from config.api.exceptions import Conflict, Unprocessable
from config.api.pagination import CursorPagination, data_response
from config.api.throttling import AnonymousReadThrottle, client_ip


class GroupScopePermission(BasePermission):
    def has_permission(self, request: Request, view: Any) -> bool:
        code = view.permission_codes.get("GET" if request.method == "HEAD" else request.method)
        if code is None:
            return True
        group = view.get_group() if view.group_scoped and "public_id" in view.kwargs else None
        user = cast(User, request.user)
        if group is not None and not has_perm(user, group, "groups.view"):
            raise NotFound()
        return has_perm(user, group, code)


class GroupAPIView(AccountAPIView):
    permission_classes: tuple[type[BasePermission], ...] = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {}
    ordering: ClassVar[tuple[str, ...]] = ("public_id",)
    group_scoped: ClassVar[bool] = True

    def get_group(self) -> Group:
        return get_object_or_404(
            Group.objects.select_related("owner", "group_type", "location"),
            public_id=self.kwargs["public_id"],
        )

    def member(self, group: Group, public_id: Any) -> GroupMember:
        return get_object_or_404(
            GroupMember.objects.select_related("user", "group"), group=group, public_id=public_id
        )

    def role(self, group: Group, public_id: Any) -> GroupRole:
        return get_object_or_404(
            GroupRole.objects.select_related("template"), group=group, public_id=public_id
        )

    def context(self, request: Request) -> AuditContext:
        return AuditContext(
            ip_address=client_ip(request), user_agent=request.META.get("HTTP_USER_AGENT", "")[:512]
        )

    def input(self, request: Request, serializer: Any) -> dict[str, Any]:
        value = serializer(data=request.data)
        value.is_valid(raise_exception=True)
        return cast(dict[str, Any], value.validated_data)

    def initial(self, request: Request, *args: Any, **kwargs: Any) -> None:
        super().initial(request, *args, **kwargs)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            mfa.limit("financial_write", str(cast(User, request.user).public_id))

    def paginate(
        self,
        request: Request,
        queryset: Any,
        serializer: Any,
        *,
        ordering: tuple[str, ...] | None = None,
    ) -> Response:
        paginator = CursorPagination()
        if ordering is not None:
            paginator.ordering = ordering
            page = paginator.paginate_queryset(queryset, request)
        else:
            page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(serializer(page, many=True).data)

    def handle_exception(self, exc: Exception) -> Response:
        if isinstance(exc, GroupNotFound):
            exc = NotFound(str(exc))
        elif isinstance(exc, GroupConflict):
            exc = Conflict(str(exc))
        elif isinstance(exc, GroupRuleViolation):
            exc = Unprocessable(str(exc))
        return super().handle_exception(exc)


class GroupListView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "groups.create"}
    ordering = ("-created_at", "public_id")

    @extend_schema(
        tags=["groups"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.GroupSummarySerializer, paginated=True),
        operation_id="groups_list",
    )
    def get(self, request: Request) -> Response:
        rows = (
            Group.objects.filter(
                members__user=cast(User, request.user),
                members__status__in=["INVITED", "PENDING_APPROVAL", "ACTIVE", "SUSPENDED"],
            )
            .select_related("group_type", "location")
            .distinct()
        )
        return self.paginate(request, rows, contract.GroupSummarySerializer)

    @extend_schema(
        tags=["groups"],
        request=contract.GroupCreateInput,
        responses={201: contract.response(contract.GroupCreatedSerializer)},
    )
    def post(self, request: Request) -> Response:
        group, invitations = services.create_group(
            actor=cast(User, request.user),
            context=self.context(request),
            **self.input(request, contract.GroupCreateInput),
        )
        result = dict(contract.GroupSerializer(group).data)
        result["invitations"] = invitations
        return data_response(result, status=201, headers={"Cache-Control": "no-store"})


class GroupDetailView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"GET": "groups.view", "PATCH": "groups.update"}

    @extend_schema(tags=["groups"], responses=contract.response(contract.GroupSerializer))
    def get(self, request: Request, public_id: Any) -> Response:
        return data_response(contract.GroupSerializer(self.get_group()).data)

    @extend_schema(
        tags=["groups"],
        request=contract.GroupUpdateInput,
        responses=contract.response(contract.GroupSerializer),
    )
    def patch(self, request: Request, public_id: Any) -> Response:
        group = services.update_group(
            actor=cast(User, request.user),
            group=self.get_group(),
            values=self.input(request, contract.GroupUpdateInput),
            context=self.context(request),
        )
        return data_response(contract.GroupSerializer(group).data)


class GroupArchiveView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "groups.archive"}

    @extend_schema(
        tags=["groups"], request=None, responses=contract.response(contract.GroupSerializer)
    )
    def post(self, request: Request, public_id: Any) -> Response:
        self.input(request, contract.StrictInput)
        group = services.archive(
            actor=cast(User, request.user), group=self.get_group(), context=self.context(request)
        )
        return data_response(contract.GroupSerializer(group).data)


class GroupTransferView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "groups.transfer_ownership"}

    @extend_schema(
        tags=["groups"],
        request=contract.TransferInput,
        responses=contract.response(contract.GroupSerializer),
    )
    def post(self, request: Request, public_id: Any) -> Response:
        group = self.get_group()
        values = self.input(request, contract.TransferInput)
        group = services.transfer_ownership(
            actor=cast(User, request.user),
            group=group,
            member=self.member(group, values["member"]),
            context=self.context(request),
        )
        return data_response(contract.GroupSerializer(group).data)


class GroupMembersView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"GET": "members.view"}

    @extend_schema(
        tags=["members"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.MemberSerializer, paginated=True),
    )
    def get(self, request: Request, public_id: Any) -> Response:
        return self.paginate(
            request, self.get_group().members.select_related("user"), contract.MemberSerializer
        )


class GroupInviteView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "members.invite"}

    @extend_schema(
        tags=["members"],
        request=contract.InviteInput,
        responses={201: contract.response(contract.InvitationIssuedSerializer)},
    )
    def post(self, request: Request, public_id: Any) -> Response:
        values = self.input(request, contract.InviteInput)
        row, token = services.invite(
            actor=cast(User, request.user),
            group=self.get_group(),
            target=values["user"],
            context=self.context(request),
        )
        result = dict(contract.InvitationSerializer(row).data)
        result["token"] = token
        return data_response(result, status=201, headers={"Cache-Control": "no-store"})


class InvitationAcceptView(GroupAPIView):
    permission_classes = (IsAuthenticated,)
    accept: ClassVar[bool] = True

    @extend_schema(
        tags=["members"],
        request=contract.InvitationDecisionInput,
        responses=contract.response(contract.MemberSerializer),
    )
    def post(self, request: Request) -> Response:
        row = services.respond_invitation(
            actor=cast(User, request.user),
            token=self.input(request, contract.InvitationDecisionInput)["token"],
            accept=self.accept,
            context=self.context(request),
        )
        return data_response(contract.MemberSerializer(row).data) if row else Response(status=204)


class InvitationDeclineView(InvitationAcceptView):
    permission_classes = (IsAuthenticated,)
    accept = False

    @extend_schema(
        tags=["members"], request=contract.InvitationDecisionInput, responses={204: None}
    )
    def post(self, request: Request) -> Response:
        return super().post(request)


class GroupJoinView(GroupAPIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        tags=["members"],
        request=None,
        responses={201: contract.response(contract.MemberSerializer)},
    )
    def post(self, request: Request, public_id: Any) -> Response:
        self.input(request, contract.StrictInput)
        member = services.join(
            actor=cast(User, request.user), group=self.get_group(), context=self.context(request)
        )
        return data_response(contract.MemberSerializer(member).data, status=201)


class MemberActionView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "members.approve"}
    action: ClassVar[str] = "approve"

    @extend_schema(
        tags=["members"],
        request=contract.MemberActionInput,
        responses=contract.response(contract.MemberSerializer),
    )
    def post(self, request: Request, public_id: Any, member_id: Any) -> Response:
        group = self.get_group()
        member = services.transition_member(
            actor=cast(User, request.user),
            group=group,
            member=self.member(group, member_id),
            action=self.action,
            context=self.context(request),
            **self.input(request, contract.MemberActionInput),
        )
        return data_response(contract.MemberSerializer(member).data)


class MemberSuspendView(MemberActionView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "members.suspend"}
    action = "suspend"


class MemberReinstateView(MemberSuspendView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    action = "reinstate"


class MemberRemoveView(MemberActionView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "members.remove"}
    action = "remove"


class GroupLeaveView(GroupAPIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        tags=["members"],
        request=contract.MemberActionInput,
        responses=contract.response(contract.MemberSerializer),
    )
    def post(self, request: Request, public_id: Any) -> Response:
        group = self.get_group()
        member = get_object_or_404(GroupMember, group=group, user=request.user)
        member = services.transition_member(
            actor=cast(User, request.user),
            group=group,
            member=member,
            action="leave",
            context=self.context(request),
            **self.input(request, contract.MemberActionInput),
        )
        return data_response(contract.MemberSerializer(member).data)


class MemberHistoryView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"GET": "members.view_history"}

    @extend_schema(
        tags=["members"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.HistorySerializer, paginated=True),
    )
    def get(self, request: Request, public_id: Any, member_id: Any) -> Response:
        member = self.member(self.get_group(), member_id)
        return self.paginate(
            request,
            member.history.select_related("changed_by"),
            contract.HistorySerializer,
            ordering=("-occurred_at", "pk"),
        )


class GroupRolesView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {
        "GET": "groups.manage_settings",
        "POST": "groups.manage_settings",
    }

    @extend_schema(
        tags=["roles"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.RoleSerializer, paginated=True),
    )
    def get(self, request: Request, public_id: Any) -> Response:
        return self.paginate(
            request, self.get_group().roles.select_related("template"), contract.RoleSerializer
        )

    @extend_schema(
        tags=["roles"],
        request=contract.RoleInput,
        responses={201: contract.response(contract.RoleSerializer)},
    )
    def post(self, request: Request, public_id: Any) -> Response:
        role = services.create_role(
            actor=cast(User, request.user),
            group=self.get_group(),
            context=self.context(request),
            **self.input(request, contract.RoleInput),
        )
        return data_response(contract.RoleSerializer(role).data, status=201)


class GroupRolePermissionsView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"PUT": "groups.manage_settings"}

    @extend_schema(
        tags=["roles"],
        request=contract.PermissionInput,
        responses=contract.response(contract.RoleSerializer),
    )
    def put(self, request: Request, public_id: Any, role_id: Any) -> Response:
        group = self.get_group()
        role = services.configure_role(
            actor=cast(User, request.user),
            group=group,
            role=self.role(group, role_id),
            grants=self.input(request, contract.PermissionInput)["permissions"],
            context=self.context(request),
        )
        return data_response(contract.RoleSerializer(role).data)


class MemberRolesView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {
        "POST": "members.assign_roles",
        "DELETE": "members.assign_roles",
    }
    http_method_names: list[str] = ["post", "options"]  # noqa: RUF012 — Django View API

    @extend_schema(
        tags=["roles"],
        request=contract.AssignRoleInput,
        responses=contract.response(contract.MemberSerializer),
    )
    def post(self, request: Request, public_id: Any, member_id: Any) -> Response:
        group = self.get_group()
        member = self.member(group, member_id)
        services.assign_role(
            actor=cast(User, request.user),
            group=group,
            member=member,
            role=self.role(group, self.input(request, contract.AssignRoleInput)["role"]),
            context=self.context(request),
        )
        return data_response(contract.MemberSerializer(member).data)

    @extend_schema(tags=["roles"], responses={204: None})
    def delete(self, request: Request, public_id: Any, member_id: Any, role_id: Any) -> Response:
        group = self.get_group()
        services.revoke_role(
            actor=cast(User, request.user),
            group=group,
            member=self.member(group, member_id),
            role=self.role(group, role_id),
            context=self.context(request),
        )
        return Response(status=204)


class MemberRoleRevokeView(MemberRolesView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    http_method_names: list[str] = ["delete", "options"]  # noqa: RUF012 — Django View API


class GroupRoleDetailView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {
        "PATCH": "groups.manage_settings",
        "DELETE": "groups.manage_settings",
    }

    @extend_schema(
        tags=["roles"],
        request=contract.RoleUpdateInput,
        responses=contract.response(contract.RoleSerializer),
    )
    def patch(self, request: Request, public_id: Any, role_id: Any) -> Response:
        group = self.get_group()
        role = services.rename_role(
            actor=cast(User, request.user),
            group=group,
            role=self.role(group, role_id),
            values=self.input(request, contract.RoleUpdateInput),
            context=self.context(request),
        )
        return data_response(contract.RoleSerializer(role).data)

    @extend_schema(tags=["roles"], responses={204: None})
    def delete(self, request: Request, public_id: Any, role_id: Any) -> Response:
        group = self.get_group()
        services.delete_role(
            actor=cast(User, request.user),
            group=group,
            role=self.role(group, role_id),
            context=self.context(request),
        )
        return Response(status=204)


class GroupSettingsView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {
        "GET": "groups.manage_settings",
        "PUT": "groups.manage_settings",
    }

    @extend_schema(tags=["groups"], responses=contract.response(contract.SettingsInput))
    def get(self, request: Request, public_id: Any) -> Response:
        group = self.get_group()
        return data_response(
            {
                "membership_mode": group.membership_mode,
                "requires_membership_approval": group.requires_membership_approval,
            }
        )

    @extend_schema(
        tags=["groups"],
        request=contract.SettingsInput,
        responses=contract.response(contract.SettingsInput),
    )
    def put(self, request: Request, public_id: Any) -> Response:
        group = services.configure(
            actor=cast(User, request.user),
            group=self.get_group(),
            values=self.input(request, contract.SettingsInput),
            context=self.context(request),
        )
        return data_response(
            {
                "membership_mode": group.membership_mode,
                "requires_membership_approval": group.requires_membership_approval,
            }
        )


class GroupRulesView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {
        "GET": "groups.manage_rules",
        "PUT": "groups.manage_rules",
    }

    @extend_schema(tags=["groups"], responses=contract.response(contract.RulesInput))
    def get(self, request: Request, public_id: Any) -> Response:
        return data_response({"rules": self.get_group().rules})

    @extend_schema(
        tags=["groups"],
        request=contract.RulesInput,
        responses=contract.response(contract.RulesInput),
    )
    def put(self, request: Request, public_id: Any) -> Response:
        group = services.configure(
            actor=cast(User, request.user),
            group=self.get_group(),
            values=self.input(request, contract.RulesInput),
            rules=True,
            context=self.context(request),
        )
        return data_response({"rules": group.rules})


class GroupDashboardView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"GET": "reports.view_group"}

    @extend_schema(tags=["groups"], responses=contract.response(contract.DashboardSerializer))
    def get(self, request: Request, public_id: Any) -> Response:
        group = self.get_group()
        return data_response(
            {
                "active_members": group.members.filter(status="ACTIVE").count(),
                "pending_approvals": group.members.filter(status="PENDING_APPROVAL").count(),
                "roles": group.roles.count(),
            }
        )


class GroupTypesView(GroupAPIView):
    permission_classes = (AllowAny,)
    throttle_classes = (AnonymousReadThrottle,)
    ordering = ("code",)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "platform.manage_group_types"}

    def get_permissions(self) -> list[BasePermission]:
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [AllowAny()]
        return [IsAuthenticated(), GroupScopePermission()]

    @extend_schema(
        tags=["groups"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.GroupTypeSerializer, paginated=True),
        operation_id="group_types_list",
    )
    def get(self, request: Request) -> Response:
        return self.paginate(
            request,
            GroupType.objects.select_related("default_role_set").prefetch_related(
                "default_role_set__roles"
            ),
            contract.GroupTypeSerializer,
        )

    @extend_schema(
        tags=["groups"],
        request=contract.GroupTypeInput,
        responses={201: contract.response(contract.GroupTypeSerializer)},
    )
    def post(self, request: Request) -> Response:
        result = services.create_group_type(
            actor=cast(User, request.user),
            context=self.context(request),
            **self.input(request, contract.GroupTypeInput),
        )
        return data_response(contract.GroupTypeSerializer(result).data, status=201)


class PlatformRolesView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {
        "GET": "platform.manage_users",
        "POST": "platform.manage_users",
    }
    ordering = ("code",)

    @extend_schema(
        tags=["roles"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.RoleSerializer, paginated=True),
    )
    def get(self, request: Request) -> Response:
        return self.paginate(
            request,
            GroupRole.objects.filter(group=None).select_related("template"),
            contract.RoleSerializer,
        )

    @extend_schema(tags=["roles"], request=contract.PlatformAssignInput, responses={204: None})
    def post(self, request: Request) -> Response:
        values = self.input(request, contract.PlatformAssignInput)
        role = get_object_or_404(
            GroupRole.objects.select_related("template"), public_id=values["role"], group=None
        )
        services.assign_platform_role(
            actor=cast(User, request.user),
            target=values["user"],
            role=role,
            context=self.context(request),
        )
        return Response(status=204)


class PlatformRolePermissionsView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"PUT": "settings.manage_platform"}

    @extend_schema(
        tags=["roles"],
        request=contract.PermissionInput,
        responses=contract.response(contract.RoleSerializer),
    )
    def put(self, request: Request, role_id: Any) -> Response:
        role = get_object_or_404(
            GroupRole.objects.select_related("template"), public_id=role_id, group=None
        )
        role = services.configure_platform_role(
            actor=cast(User, request.user),
            role=role,
            grants=self.input(request, contract.PermissionInput)["permissions"],
            context=self.context(request),
        )
        return data_response(contract.RoleSerializer(role).data)


class PlatformRoleRevokeView(GroupAPIView):
    permission_classes = (IsAuthenticated, GroupScopePermission)
    permission_codes: ClassVar[dict[str, str]] = {"DELETE": "platform.manage_users"}

    @extend_schema(tags=["roles"], responses={204: None})
    def delete(self, request: Request, user_id: Any, role_id: Any) -> Response:
        target = get_object_or_404(User, public_id=user_id)
        role = get_object_or_404(GroupRole, public_id=role_id, group=None)
        services.revoke_platform_role(
            actor=cast(User, request.user), target=target, role=role, context=self.context(request)
        )
        return Response(status=204)


class PermissionCatalogueView(GroupAPIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        tags=["roles"], responses=contract.response(contract.PermissionSerializer, many=True)
    )
    def get(self, request: Request) -> Response:
        return data_response(
            list(
                Permission.objects.order_by("code").values(
                    "code", "name", "category", "is_write", "dual_control"
                )
            )
        )


class LocationGroupsView(GroupAPIView):
    permission_classes = (AllowAny,)
    throttle_classes = (AnonymousReadThrottle,)
    ordering = ("name", "public_id")

    @extend_schema(
        tags=["groups"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.GroupSummarySerializer, paginated=True),
    )
    def get(self, request: Request, public_id: Any) -> Response:
        location = get_object_or_404(Location, public_id=public_id, is_active=True)
        return self.paginate(
            request,
            Group.objects.filter(location=location, status="ACTIVE").select_related(
                "location", "group_type"
            ),
            contract.GroupSummarySerializer,
        )


class PublicPlatformView(GroupAPIView):
    permission_classes = (AllowAny,)
    group_scoped = False

    def get_permissions(self) -> list[BasePermission]:
        if self.request.method in ("GET", "HEAD", "OPTIONS"):
            return [AllowAny()]
        return [IsAuthenticated(), GroupScopePermission()]

    def get_throttles(self) -> list[Any]:
        return [AnonymousReadThrottle()] if self.request.method in ("GET", "HEAD") else []


class GroupTypeDetailView(PublicPlatformView):
    permission_classes = (AllowAny,)
    permission_codes: ClassVar[dict[str, str]] = {"PATCH": "platform.manage_group_types"}

    @extend_schema(tags=["groups"], responses=contract.response(contract.GroupTypeSerializer))
    def get(self, request: Request, code: str) -> Response:
        return data_response(
            contract.GroupTypeSerializer(get_object_or_404(GroupType, code=code)).data
        )

    @extend_schema(
        tags=["groups"],
        request=contract.GroupTypeUpdateInput,
        responses=contract.response(contract.GroupTypeSerializer),
    )
    def patch(self, request: Request, code: str) -> Response:
        result = services.update_group_type(
            actor=cast(User, request.user),
            group_type=get_object_or_404(GroupType, code=code),
            values=self.input(request, contract.GroupTypeUpdateInput),
            context=self.context(request),
        )
        return data_response(contract.GroupTypeSerializer(result).data)


class RoleTemplatesView(GroupAPIView):
    permission_classes = (IsAuthenticated,)
    ordering = ("code",)

    @extend_schema(
        tags=["roles"],
        parameters=contract.PAGE_PARAMETERS,
        responses=contract.response(contract.RoleTemplateSerializer, paginated=True),
    )
    def get(self, request: Request) -> Response:
        return self.paginate(
            request, RoleTemplate.objects.filter(scope="GROUP"), contract.RoleTemplateSerializer
        )


class GroupLocationListView(PublicPlatformView, LocationListView):
    permission_classes = (AllowAny,)
    permission_codes: ClassVar[dict[str, str]] = {"POST": "platform.manage_locations"}
    serializer_class = LocationSerializer
    ordering = ("name", "public_id")

    @extend_schema(
        tags=["locations"],
        request=contract.LocationCreateInput,
        responses={201: contract.response(LocationSerializer)},
    )
    def post(self, request: Request) -> Response:
        result = services.manage_location(
            actor=cast(User, request.user),
            values=self.input(request, contract.LocationCreateInput),
            context=self.context(request),
        )
        return data_response(LocationSerializer(result).data, status=201)


class GroupLocationDetailView(PublicPlatformView, LocationDetailView):
    permission_classes = (AllowAny,)
    permission_codes: ClassVar[dict[str, str]] = {
        "PATCH": "platform.manage_locations",
        "DELETE": "platform.manage_locations",
    }
    serializer_class = contract.GroupLocationDetailSerializer

    @extend_schema(responses=contract.response(contract.GroupLocationDetailSerializer))
    def get(self, request: Request, *args: object, **kwargs: object) -> Response:
        return self.retrieve(request, *args, **kwargs)

    @extend_schema(
        tags=["locations"],
        request=contract.LocationUpdateInput,
        responses=contract.response(LocationSerializer),
    )
    def patch(self, request: Request, public_id: Any) -> Response:
        result = services.manage_location(
            actor=cast(User, request.user),
            location=get_object_or_404(Location, public_id=public_id),
            values=self.input(request, contract.LocationUpdateInput),
            context=self.context(request),
        )
        return data_response(LocationSerializer(result).data)

    @extend_schema(tags=["locations"], responses={204: None})
    def delete(self, request: Request, public_id: Any) -> Response:
        self.input(request, contract.StrictInput)
        services.manage_location(
            actor=cast(User, request.user),
            location=get_object_or_404(Location, public_id=public_id),
            values={"is_active": False},
            context=self.context(request),
        )
        return Response(status=204)
