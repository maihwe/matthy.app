"""Browser sign-in inputs; secret values are never echoed into bound forms."""

from django import forms

from apps.accounts.constants import TwoFactorMethod


class BrowserLoginForm(forms.Form):
    username = forms.CharField(
        label="Phone number or email",
        max_length=254,
        widget=forms.TextInput(attrs={"autocomplete": "username", "autofocus": True}),
    )
    password = forms.CharField(
        strip=False,
        max_length=4096,
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class BrowserCodeForm(forms.Form):
    code = forms.RegexField(
        regex=r"\A(?:[0-9]{6}|[a-f0-9]{20})\Z",
        max_length=20,
        label="MFA or recovery code",
        error_messages={"invalid": "Enter a six-digit MFA code or a 20-character recovery code."},
        widget=forms.PasswordInput(attrs={"autocomplete": "one-time-code", "autofocus": True}),
    )


class BrowserSetupForm(forms.Form):
    method = forms.ChoiceField(
        choices=[
            (TwoFactorMethod.TOTP, "Authenticator app (recommended)"),
            (TwoFactorMethod.SMS, "SMS"),
        ],
        initial=TwoFactorMethod.TOTP,
    )
    current_password = forms.CharField(
        strip=False,
        max_length=4096,
        label="Confirm your password",
        widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}),
    )


class BrowserConfirmForm(forms.Form):
    code = forms.RegexField(
        regex=r"\A[0-9]{6}\Z",
        max_length=6,
        label="Six-digit code",
        widget=forms.PasswordInput(
            attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}
        ),
    )
