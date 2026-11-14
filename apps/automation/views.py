from __future__ import annotations

import uuid
from typing import cast

from django.shortcuts import get_object_or_404
from django.utils.decorators import method_decorator
from django.views.decorators.cache import never_cache
from drf_spectacular.utils import extend_schema
from rest_framework.exceptions import Throttled
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.models import User
from apps.audit.services import AuditContext
from apps.automation import services
from apps.automation.errors import AutomationConflict, AutomationLimitReached, AutomationUnavailable
from apps.automation.models import AutomationRequest
from apps.automation.serializers import AutomationEnvelope, AutomationInput, AutomationSerializer
from config.api.exceptions import APIError, IdempotencyConflict


class AIUnavailable(APIError):
    status_code = 503
    api_code = "ai_unavailable"
    default_detail = "AI automation is currently unavailable."


@method_decorator(never_cache, name="dispatch")
class AutomationCreateView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(
        tags=["automation"],
        request=AutomationInput,
        responses={202: AutomationEnvelope, 200: AutomationEnvelope},
    )
    def post(self, request: Request) -> Response:
        values = AutomationInput(data=request.data)
        values.is_valid(raise_exception=True)
        try:
            job, created = services.submit(
                user=cast(User, request.user),
                context=AuditContext.from_request(request),
                **values.validated_data,
            )
        except AutomationUnavailable:
            raise AIUnavailable from None
        except AutomationLimitReached as exc:
            raise Throttled(
                wait=exc.retry_after,
                detail="Your AI request limit for the past 24 hours has been reached.",
            ) from None
        except AutomationConflict:
            raise IdempotencyConflict from None
        response = Response(
            {"data": AutomationSerializer(job).data}, status=202 if created else 200
        )
        response["Location"] = f"/api/v1/ai/automations/{job.public_id}/"
        return response


@method_decorator(never_cache, name="dispatch")
class AutomationDetailView(APIView):
    permission_classes = (IsAuthenticated,)

    @extend_schema(tags=["automation"], responses=AutomationEnvelope)
    def get(self, request: Request, public_id: uuid.UUID) -> Response:
        job = get_object_or_404(AutomationRequest, public_id=public_id, user=request.user)
        return Response({"data": AutomationSerializer(job).data})
