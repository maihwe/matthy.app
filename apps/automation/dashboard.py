"""A browser workspace protected by the existing admin MFA session."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import cast

from django.conf import settings
from django.contrib import admin
from django.http import Http404, HttpRequest, HttpResponse, HttpResponseRedirect
from django.shortcuts import get_object_or_404
from django.template.response import TemplateResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from apps.accounts.models import User
from apps.audit.services import AuditContext
from apps.automation import services
from apps.automation.errors import AutomationConflict, AutomationLimitReached, AutomationUnavailable
from apps.automation.forms import AutomationForm
from apps.automation.models import AutomationKind, AutomationRequest, AutomationStatus
from config import branding

STATUS_LABELS: dict[str, str] = {
    AutomationStatus.QUEUED: "Waiting",
    AutomationStatus.RUNNING: "Working",
    AutomationStatus.SUCCEEDED: "Completed",
    AutomationStatus.FAILED: "Could not complete",
}


@require_http_methods(["GET", "POST"])
def dashboard(request: HttpRequest) -> HttpResponse:
    user = cast(User, request.user)
    kind = request.GET.get("kind", AutomationKind.SUPPORT_ANSWER)
    if kind not in AutomationKind.values:
        kind = AutomationKind.SUPPORT_ANSWER
    form = AutomationForm(
        request.POST if request.method == "POST" else None,
        initial={"kind": kind},
    )
    status = 200
    retry_after = None
    if request.method == "POST":
        if not form.is_valid():
            status = 400
        else:
            try:
                job, _created = services.submit(
                    user=user,
                    context=AuditContext.from_request(request),
                    **form.cleaned_data,
                )
            except AutomationUnavailable:
                form.add_error(
                    None, "AI assistance is currently unavailable. Please try again later."
                )
                status = 503
            except AutomationLimitReached as exc:
                form.add_error(
                    None,
                    "You have used your requests for today. You can still read your saved results.",
                )
                status, retry_after = 429, exc.retry_after
            except AutomationConflict:
                form.add_error(
                    None,
                    "That request is already saved. Start a new request to change its details.",
                )
                status = 409
            else:
                return HttpResponseRedirect(
                    f"{reverse('admin:ai_dashboard')}?request={job.public_id}"
                )

    jobs = AutomationRequest.objects.filter(user=user)
    selected = None
    public_id = request.GET.get("request")
    if public_id:
        try:
            selected_id = uuid.UUID(public_id)
        except ValueError:
            raise Http404 from None
        selected = get_object_or_404(jobs, public_id=selected_id)
        if request.method == "GET" and "kind" not in request.GET:
            form.initial["kind"] = selected.kind
    used = jobs.filter(created_at__gte=timezone.now() - timedelta(days=1)).count()
    active_kind = form["kind"].value()
    context = {
        **admin.site.each_context(request),
        "title": "AI workspace",
        "brand_name": branding.BRAND_NAME,
        "brand_tagline": branding.BRAND_TAGLINE,
        "form": form,
        "active_kind": active_kind,
        "ai_available": services.available(),
        "requests_remaining": max(0, settings.AI_DAILY_REQUEST_LIMIT - used),
        "request_limit": settings.AI_DAILY_REQUEST_LIMIT,
        "selected_job": selected,
        "selected_status_label": STATUS_LABELS.get(selected.status) if selected else "",
        "recent_jobs": [
            {"job": job, "status_label": STATUS_LABELS[job.status]} for job in jobs[:12]
        ],
        "api_url": reverse("v1:automation-detail", args=[selected.public_id]) if selected else "",
    }
    response = TemplateResponse(request, "automation/dashboard.html", context, status=status)
    response["Cache-Control"] = "no-store, private"
    if retry_after is not None:
        response["Retry-After"] = str(retry_after)
    return response
