"""Explicit public UUID contracts for group structure and invitations."""

from __future__ import annotations

from functools import lru_cache
from typing import Any, ClassVar, cast

from drf_spectacular.utils import OpenApiParameter, extend_schema_serializer, inline_serializer
from rest_framework import serializers

from apps.accounts.models import User
from apps.accounts.serializers import PaginationSerializer
from apps.groups.constants import MembershipMode
from apps.groups.models import (
    Group,
    GroupInvitation,
    GroupMember,
    GroupMemberHistory,
    GroupRole,
    GroupType,
    RoleTemplate,
)
from apps.locations.models import Country, Location, LocationType
from apps.locations.serializers import LocationDetailSerializer


class StrictInput(serializers.Serializer[dict[str, Any]]):
    def to_internal_value(self, data: Any) -> dict[str, Any]:
        if isinstance(data, dict):
            unknown = set(data) - set(self.fields)
            if unknown:
                raise serializers.ValidationError(dict.fromkeys(unknown, "Unknown field."))
        return cast(dict[str, Any], super().to_internal_value(data))


class UUIDRelatedField(serializers.SlugRelatedField[Any]):
    def to_internal_value(self, data: Any) -> Any:
        return super().to_internal_value(serializers.UUIDField().run_validation(data))


class OfficerInput(StrictInput):
    user = UUIDRelatedField(slug_field="public_id", queryset=User.objects.all())
    roles = serializers.ListField(
        child=serializers.RegexField(r"\A[A-Z][A-Z0-9_]{0,47}\Z"), max_length=8, allow_empty=False
    )


class GroupCreateInput(StrictInput):
    name = serializers.CharField(max_length=255)
    group_type = serializers.SlugRelatedField(slug_field="code", queryset=GroupType.objects.all())
    location = UUIDRelatedField(
        slug_field="public_id", queryset=Location.objects.filter(is_active=True)
    )
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    logo = serializers.CharField(max_length=500, required=False, allow_blank=True)
    rules = serializers.CharField(max_length=50000, required=False, allow_blank=True)
    membership_mode = serializers.ChoiceField(choices=MembershipMode.choices, required=False)
    requires_membership_approval = serializers.BooleanField(required=False)
    # DRF passes list bounds to ListSerializer; the serializer stub omits them.
    officers = OfficerInput(many=True, required=False, max_length=30)  # type: ignore[call-arg]


class GroupUpdateInput(StrictInput):
    name = serializers.CharField(max_length=255, required=False)
    group_type = serializers.SlugRelatedField(
        slug_field="code", queryset=GroupType.objects.all(), required=False
    )
    location = UUIDRelatedField(
        slug_field="public_id", queryset=Location.objects.filter(is_active=True), required=False
    )
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    logo = serializers.CharField(max_length=500, required=False, allow_blank=True)


class SettingsInput(StrictInput):
    membership_mode = serializers.ChoiceField(choices=MembershipMode.choices)
    requires_membership_approval = serializers.BooleanField()


class RulesInput(StrictInput):
    rules = serializers.CharField(max_length=50000, allow_blank=True)


class InviteInput(StrictInput):
    user = UUIDRelatedField(slug_field="public_id", queryset=User.objects.all())


class InvitationDecisionInput(StrictInput):
    token = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


class MemberActionInput(StrictInput):
    reason = serializers.CharField(max_length=2000, required=False, allow_blank=True)


class RoleInput(StrictInput):
    code = serializers.RegexField(r"\A[A-Z][A-Z0-9_]{0,47}\Z")
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)
    template = serializers.SlugRelatedField(
        slug_field="code", queryset=RoleTemplate.objects.filter(scope="GROUP")
    )


class RoleUpdateInput(StrictInput):
    name = serializers.CharField(max_length=100, required=False)
    description = serializers.CharField(max_length=5000, required=False, allow_blank=True)


class PermissionInput(StrictInput):
    permissions = serializers.DictField(child=serializers.BooleanField())

    def validate_permissions(self, value: dict[str, bool]) -> dict[str, bool]:
        if len(value) > 100:
            raise serializers.ValidationError("At most 100 permission codes may be supplied.")
        return value


class AssignRoleInput(StrictInput):
    role = serializers.UUIDField()


class TransferInput(StrictInput):
    member = serializers.UUIDField()


class PlatformAssignInput(StrictInput):
    user = UUIDRelatedField(slug_field="public_id", queryset=User.objects.all())
    role = serializers.UUIDField()


class GroupTypeInput(StrictInput):
    code = serializers.RegexField(r"\A[A-Z][A-Z0-9_]{0,47}\Z")
    name = serializers.CharField(max_length=100)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    roles = serializers.ListField(
        child=serializers.SlugRelatedField(
            slug_field="code", queryset=RoleTemplate.objects.filter(scope="GROUP")
        ),
        max_length=8,
        allow_empty=False,
    )


class GroupTypeUpdateInput(StrictInput):
    name = serializers.CharField(max_length=100, required=False)
    description = serializers.CharField(max_length=10000, required=False, allow_blank=True)
    roles = serializers.ListField(
        child=serializers.SlugRelatedField(
            slug_field="code", queryset=RoleTemplate.objects.filter(scope="GROUP")
        ),
        max_length=8,
        allow_empty=False,
        required=False,
    )


class LocationCreateInput(StrictInput):
    country = serializers.SlugRelatedField(
        slug_field="iso2", queryset=Country.objects.filter(is_active=True)
    )
    type = serializers.CharField(max_length=16)
    parent_id = UUIDRelatedField(
        slug_field="public_id",
        queryset=Location.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )
    name = serializers.CharField(max_length=160)
    slug = serializers.SlugField(max_length=160)
    code = serializers.CharField(max_length=32, required=False, allow_null=True, allow_blank=True)
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=-90, max_value=90, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-180,
        max_value=180,
        required=False,
        allow_null=True,
    )

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        country = attrs["country"]
        location_type = LocationType.objects.filter(country=country, code=attrs.pop("type")).first()
        if location_type is None:
            raise serializers.ValidationError({"type": "Choose a type belonging to this country."})
        attrs["location_type"] = location_type
        attrs["parent"] = attrs.pop("parent_id", None)
        return attrs


class LocationUpdateInput(StrictInput):
    name = serializers.CharField(max_length=160, required=False)
    slug = serializers.SlugField(max_length=160, required=False)
    code = serializers.CharField(max_length=32, required=False, allow_null=True, allow_blank=True)
    latitude = serializers.DecimalField(
        max_digits=9, decimal_places=6, min_value=-90, max_value=90, required=False, allow_null=True
    )
    longitude = serializers.DecimalField(
        max_digits=9,
        decimal_places=6,
        min_value=-180,
        max_value=180,
        required=False,
        allow_null=True,
    )
    is_active = serializers.BooleanField(required=False)


class RoleTemplateSerializer(serializers.ModelSerializer[RoleTemplate]):
    class Meta:
        model = RoleTemplate
        fields = ("code", "name", "scope", "policies")
        read_only_fields = fields


class GroupSummarySerializer(serializers.ModelSerializer[Group]):
    id = serializers.UUIDField(source="public_id", read_only=True)
    group_type = serializers.CharField(source="group_type.code", read_only=True)
    location = serializers.UUIDField(source="location.public_id", read_only=True)

    class Meta:
        model = Group
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "name",
            "description",
            "logo",
            "group_type",
            "location",
            "status",
            "membership_mode",
            "requires_membership_approval",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class GroupSerializer(GroupSummarySerializer):
    owner = serializers.UUIDField(source="owner.public_id", read_only=True)

    class Meta(GroupSummarySerializer.Meta):
        fields = (*GroupSummarySerializer.Meta.fields, "owner", "rules", "created_at", "updated_at")
        read_only_fields = fields


class MemberSerializer(serializers.ModelSerializer[GroupMember]):
    id = serializers.UUIDField(source="public_id", read_only=True)
    user = serializers.UUIDField(source="user.public_id", read_only=True)
    roles = serializers.SerializerMethodField()

    class Meta:
        model = GroupMember
        fields = (
            "id",
            "user",
            "status",
            "joined_at",
            "approved_at",
            "suspended_at",
            "suspension_reason",
            "left_at",
            "roles",
        )
        read_only_fields = fields

    def get_roles(self, obj: GroupMember) -> list[dict[str, str]]:
        return [
            {
                "id": str(a.group_role.public_id),
                "code": a.group_role.code,
                "name": a.group_role.name,
            }
            for a in obj.role_assignments.select_related("group_role")
        ]


class HistorySerializer(serializers.ModelSerializer[GroupMemberHistory]):
    changed_by = serializers.UUIDField(source="changed_by.public_id", read_only=True)

    class Meta:
        model = GroupMemberHistory
        fields = ("from_status", "to_status", "changed_by", "reason", "occurred_at")
        read_only_fields = fields


class RoleSerializer(serializers.ModelSerializer[GroupRole]):
    id = serializers.UUIDField(source="public_id", read_only=True)
    template = serializers.CharField(source="template.code", read_only=True)
    permissions = serializers.SerializerMethodField()
    permission_bounds = serializers.JSONField(source="template.policies", read_only=True)

    class Meta:
        model = GroupRole
        fields = (
            "id",
            "code",
            "name",
            "description",
            "template",
            "is_system",
            "is_configurable",
            "permissions",
            "permission_bounds",
        )
        read_only_fields = fields

    def get_permissions(self, obj: GroupRole) -> dict[str, bool]:
        return {
            g.permission.code: g.granted for g in obj.permission_grants.select_related("permission")
        }


class GroupTypeSerializer(serializers.ModelSerializer[GroupType]):
    roles: Any = serializers.SlugRelatedField(
        source="default_role_set.roles", slug_field="code", read_only=True, many=True
    )

    class Meta:
        model = GroupType
        fields = ("code", "name", "description", "is_system", "roles")
        read_only_fields = fields


class InvitationSerializer(serializers.ModelSerializer[GroupInvitation]):
    id = serializers.UUIDField(source="public_id", read_only=True)
    user = serializers.UUIDField(source="invited_user.public_id", read_only=True)

    class Meta:
        model = GroupInvitation
        fields: ClassVar[tuple[str, ...]] = (
            "id",
            "user",
            "expires_at",
            "accepted_at",
            "declined_at",
        )
        read_only_fields: ClassVar[tuple[str, ...]] = fields


class InvitationIssuedSerializer(InvitationSerializer):
    token = serializers.CharField(read_only=True)

    class Meta(InvitationSerializer.Meta):
        fields = (*InvitationSerializer.Meta.fields, "token")
        read_only_fields = fields


class OfficerInvitationSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(read_only=True)
    user = serializers.UUIDField(read_only=True)
    token = serializers.CharField(read_only=True)


class GroupCreatedSerializer(GroupSerializer):
    invitations = OfficerInvitationSerializer(many=True, read_only=True)

    class Meta(GroupSerializer.Meta):
        fields = (*GroupSerializer.Meta.fields, "invitations")
        read_only_fields = fields


class DashboardSerializer(serializers.Serializer[dict[str, Any]]):
    active_members = serializers.IntegerField(read_only=True)
    pending_approvals = serializers.IntegerField(read_only=True)
    roles = serializers.IntegerField(read_only=True)


class PermissionSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField(read_only=True)
    name = serializers.CharField(read_only=True)
    category = serializers.CharField(read_only=True)
    is_write = serializers.BooleanField(read_only=True)
    dual_control = serializers.BooleanField(read_only=True)


PAGE_PARAMETERS = [
    OpenApiParameter("cursor", str, description="Opaque cursor returned by the previous page."),
    OpenApiParameter("limit", int, description="Results per page (default 50, maximum 200)."),
]


@lru_cache
def response(serializer: Any, *, paginated: bool = False, many: bool = False) -> Any:
    """Describe the same data envelope used by the runtime response helper."""
    name = serializer.__name__.removesuffix("Serializer").removesuffix("Input")
    suffix = "Page" if paginated else "List" if many else ""
    fields = {"data": serializer(many=paginated or many)}
    if paginated:
        fields["pagination"] = PaginationSerializer()
    value = inline_serializer(name=f"{name}{suffix}Response", fields=fields)
    # The envelope is one object even when its data field contains a list.
    extend_schema_serializer(many=False)(type(value))
    return value


class GroupLocationDetailSerializer(LocationDetailSerializer):
    """A location with bounded children and an active group count."""

    group_count = serializers.SerializerMethodField()

    class Meta(LocationDetailSerializer.Meta):
        fields = (*LocationDetailSerializer.Meta.fields, "group_count")

    def get_group_count(self, obj: Location) -> int:
        return obj.hosted_groups.filter(status="ACTIVE").count()
