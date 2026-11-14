from django.urls import path

from apps.automation.views import AutomationCreateView, AutomationDetailView

urlpatterns = [
    path("automations/", AutomationCreateView.as_view(), name="automation-create"),
    path("automations/<uuid:public_id>/", AutomationDetailView.as_view(), name="automation-detail"),
]
