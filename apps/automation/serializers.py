from typing import Any

from rest_framework import serializers

from apps.automation.models import AutomationKind, AutomationRequest


class AutomationInput(serializers.Serializer[dict[str, Any]]):
    kind = serializers.ChoiceField(choices=AutomationKind.choices)
    input_text = serializers.CharField(max_length=4000)
    language = serializers.ChoiceField(choices=["en", "fr", "sw"], default="en")
    idempotency_key = serializers.UUIDField()


class AutomationSerializer(serializers.ModelSerializer[AutomationRequest]):
    class Meta:
        model = AutomationRequest
        fields = (
            "public_id",
            "kind",
            "language",
            "status",
            "output_text",
            "error_code",
            "created_at",
            "completed_at",
        )
        read_only_fields = fields


class AutomationEnvelope(serializers.Serializer[dict[str, Any]]):
    # The metaclass consumes this envelope field before Serializer.data is used.
    data = AutomationSerializer()  # type: ignore[assignment]
