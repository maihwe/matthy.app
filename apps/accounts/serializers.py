"""Explicit input and output contracts; credentials never appear in resource serializers."""

from __future__ import annotations

from typing import Any

from rest_framework import serializers

from apps.accounts.constants import DevicePlatform
from apps.accounts.models import Device, User, UserSession


class LoginDeviceSerializer(serializers.Serializer[dict[str, Any]]):
    id = serializers.UUIDField(required=False)
    fingerprint = serializers.CharField(required=False, max_length=255)
    platform = serializers.ChoiceField(choices=DevicePlatform.choices, required=False)
    model = serializers.CharField(required=False, max_length=100, allow_blank=True)
    os_version = serializers.CharField(required=False, max_length=50, allow_blank=True)
    app_version = serializers.CharField(required=False, max_length=50, allow_blank=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "id" in attrs and "fingerprint" in attrs:
            raise serializers.ValidationError("Supply a device id or fingerprint, not both.")
        return attrs


class LoginSerializer(serializers.Serializer[dict[str, Any]]):
    identifier = serializers.CharField(max_length=254)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=4096)
    device = LoginDeviceSerializer(required=False)


class MFAEnableSerializer(serializers.Serializer[dict[str, Any]]):
    method = serializers.ChoiceField(choices=["TOTP", "SMS"])
    current_password = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=4096
    )
    mfa_token = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, required=False
    )


class MFAConfirmSerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.RegexField(r"\A[0-9]{6}\Z", trim_whitespace=False, write_only=True)
    mfa_token = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=128, required=False
    )


class MFAChallengeSerializer(serializers.Serializer[dict[str, Any]]):
    mfa_token = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    code = serializers.RegexField(
        r"\A(?:[0-9]{6}|[0-9a-f]{20})\Z", trim_whitespace=False, write_only=True
    )


class MFADisableSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(
        write_only=True, trim_whitespace=False, max_length=4096
    )
    code = serializers.RegexField(
        r"\A(?:[0-9]{6}|[0-9a-f]{20})\Z", trim_whitespace=False, write_only=True
    )


class MFAEnrollmentSerializer(serializers.Serializer[dict[str, Any]]):
    method = serializers.ChoiceField(choices=["TOTP", "SMS"], read_only=True)
    expires_in = serializers.IntegerField(read_only=True)
    secret = serializers.CharField(read_only=True, required=False)
    provisioning_uri = serializers.CharField(read_only=True, required=False)


class MFAEnrollmentResponseSerializer(serializers.Serializer[dict[str, Any]]):
    data = MFAEnrollmentSerializer()  # type: ignore[assignment]


class MFARecoverySerializer(serializers.Serializer[dict[str, Any]]):
    recovery_codes = serializers.ListField(child=serializers.CharField(), read_only=True)


class MFARecoveryResponseSerializer(serializers.Serializer[dict[str, Any]]):
    data = MFARecoverySerializer()  # type: ignore[assignment]


class MFALoginSerializer(serializers.Serializer[dict[str, Any]]):
    mfa_required = serializers.BooleanField(read_only=True)
    mfa_token = serializers.CharField(read_only=True)
    enrollment_required = serializers.BooleanField(read_only=True)
    method = serializers.ChoiceField(choices=["TOTP", "SMS"], read_only=True, allow_null=True)
    expires_in = serializers.IntegerField(read_only=True)
    sms_sent = serializers.BooleanField(read_only=True, allow_null=True)


class MFALoginResponseSerializer(serializers.Serializer[dict[str, Any]]):
    data = MFALoginSerializer()  # type: ignore[assignment]


class RegistrationSerializer(serializers.Serializer[dict[str, Any]]):
    full_name = serializers.CharField(max_length=150)
    phone = serializers.CharField(max_length=20)
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=4096)
    email = serializers.EmailField(
        max_length=254, required=False, allow_blank=True, allow_null=True
    )


class EmailVerificationRequestSerializer(serializers.Serializer[dict[str, Any]]):
    """Delivery always uses the authenticated account's saved address."""


class EmailVerificationSerializer(serializers.Serializer[dict[str, Any]]):
    token = serializers.CharField(max_length=128, trim_whitespace=False, write_only=True)


class EmailVerifiedSerializer(serializers.ModelSerializer[User]):
    id = serializers.UUIDField(source="public_id", read_only=True)

    class Meta:
        model = User
        fields = ("id", "email", "is_email_verified", "verification_status")
        read_only_fields = fields


class EmailVerifiedResponseSerializer(serializers.Serializer[dict[str, Any]]):
    data = EmailVerifiedSerializer()  # type: ignore[assignment]


class PhoneCodeRequestSerializer(serializers.Serializer[dict[str, Any]]):
    phone = serializers.CharField(max_length=20)
    purpose = serializers.ChoiceField(choices=["PHONE_VERIFY"], default="PHONE_VERIFY")


class PhoneVerificationSerializer(serializers.Serializer[dict[str, Any]]):
    phone = serializers.CharField(max_length=20)
    code = serializers.RegexField(r"\A[0-9]{6}\Z", trim_whitespace=False, write_only=True)


class MessageSerializer(serializers.Serializer[dict[str, Any]]):
    message = serializers.CharField(read_only=True)


class MessageResponseSerializer(serializers.Serializer[dict[str, Any]]):
    data = MessageSerializer()  # type: ignore[assignment]


class PhoneVerifiedSerializer(serializers.ModelSerializer[User]):
    id = serializers.UUIDField(source="public_id", read_only=True)

    class Meta:
        model = User
        fields = ("id", "phone", "is_phone_verified", "verification_status")
        read_only_fields = fields


class PhoneVerifiedResponseSerializer(serializers.Serializer[dict[str, Any]]):
    data = PhoneVerifiedSerializer()  # type: ignore[assignment]


class RefreshSerializer(serializers.Serializer[dict[str, Any]]):
    refresh_token = serializers.CharField(write_only=True, trim_whitespace=False, max_length=512)


class PasswordResetRequestSerializer(serializers.Serializer[dict[str, Any]]):
    identifier = serializers.CharField(max_length=254)


class PasswordResetConfirmSerializer(serializers.Serializer[dict[str, Any]]):
    phone = serializers.CharField(max_length=20, required=False)
    code = serializers.RegexField(
        r"\A[0-9]{6}\Z", trim_whitespace=False, write_only=True, required=False
    )
    reset_token = serializers.CharField(
        max_length=128, trim_whitespace=False, write_only=True, required=False
    )
    new_password = serializers.CharField(max_length=4096, trim_whitespace=False, write_only=True)

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        has_token = "reset_token" in attrs
        has_phone = "phone" in attrs
        has_code = "code" in attrs
        if (has_token and (has_phone or has_code)) or (
            not has_token and not (has_phone and has_code)
        ):
            raise serializers.ValidationError("Supply a reset token or a phone and code.")
        return attrs


class PasswordChangeSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(
        max_length=4096, trim_whitespace=False, write_only=True
    )
    new_password = serializers.CharField(max_length=4096, trim_whitespace=False, write_only=True)


class PinField(serializers.RegexField):
    def __init__(self) -> None:
        super().__init__(r"\A[0-9]{4}\Z", trim_whitespace=False, write_only=True)

    def to_internal_value(self, data: Any) -> str:
        if not isinstance(data, str):
            raise serializers.ValidationError("Enter the PIN as a four-digit string.")
        return super().to_internal_value(data)


class PinSetSerializer(serializers.Serializer[dict[str, Any]]):
    current_password = serializers.CharField(
        max_length=4096, trim_whitespace=False, write_only=True
    )
    pin = PinField()


class PinChangeSerializer(serializers.Serializer[dict[str, Any]]):
    current_pin = PinField()
    new_pin = PinField()


class UserIdentitySerializer(serializers.ModelSerializer[User]):
    id = serializers.UUIDField(source="public_id", read_only=True)
    mfa_required = serializers.BooleanField(default=False, read_only=True)

    class Meta:
        model = User
        fields = ("id", "full_name", "mfa_required")
        read_only_fields = fields


class TokenPairSerializer(serializers.Serializer[dict[str, Any]]):
    access_token = serializers.CharField(read_only=True)
    refresh_token = serializers.CharField(read_only=True)
    expires_in = serializers.IntegerField(read_only=True)
    device_id = serializers.UUIDField(read_only=True)
    user = UserIdentitySerializer(read_only=True)


class TokenResponseSerializer(serializers.Serializer[dict[str, Any]]):
    # DRF's metaclass removes this field before the inherited data property is used.
    data = TokenPairSerializer()  # type: ignore[assignment]


class DeviceSerializer(serializers.ModelSerializer[Device]):
    id = serializers.UUIDField(source="public_id", read_only=True)

    class Meta:
        model = Device
        fields = (
            "id",
            "platform",
            "model",
            "os_version",
            "app_version",
            "last_seen_at",
            "created_at",
        )
        read_only_fields = fields


class SessionSerializer(serializers.ModelSerializer[UserSession]):
    id = serializers.UUIDField(source="public_id", read_only=True)
    device = DeviceSerializer(read_only=True, allow_null=True)

    class Meta:
        model = UserSession
        fields = ("id", "kind", "device", "issued_at", "expires_at", "last_used_at", "ip_address")
        read_only_fields = fields


class PaginationSerializer(serializers.Serializer[dict[str, Any]]):
    next_cursor = serializers.CharField(allow_null=True)
    has_more = serializers.BooleanField()
    limit = serializers.IntegerField()


class SessionListSerializer(serializers.Serializer[dict[str, Any]]):
    data = SessionSerializer(many=True)  # type: ignore[assignment]
    pagination = PaginationSerializer()


class DeviceListSerializer(serializers.Serializer[dict[str, Any]]):
    data = DeviceSerializer(many=True)  # type: ignore[assignment]
    pagination = PaginationSerializer()


class ErrorDetailSerializer(serializers.Serializer[dict[str, Any]]):
    field = serializers.CharField()
    issue = serializers.CharField()


class ErrorBodySerializer(serializers.Serializer[dict[str, Any]]):
    code = serializers.CharField()
    message = serializers.CharField()
    details = ErrorDetailSerializer(many=True)
    request_id = serializers.CharField()


class ErrorResponseSerializer(serializers.Serializer[dict[str, Any]]):
    error = ErrorBodySerializer()
