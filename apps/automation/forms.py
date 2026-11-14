from __future__ import annotations

import uuid

from django import forms

from apps.automation.models import AutomationKind, AutomationRequest


class AutomationForm(forms.Form):
    kind = forms.ChoiceField(choices=AutomationKind.choices)
    input_text = forms.CharField(
        label="Your question or announcement details",
        max_length=4000,
        widget=forms.Textarea(
            attrs={
                "rows": 7,
                "placeholder": "What would you like help with?",
                "aria-describedby": "prompt-help character-count",
            }
        ),
    )
    language = forms.ChoiceField(
        choices=AutomationRequest._meta.get_field("language").choices or (),
        initial="en",
    )
    idempotency_key = forms.UUIDField(initial=uuid.uuid4, widget=forms.HiddenInput)
