"""Use staged browser MFA for the administration site."""

from django.contrib.admin.apps import AdminConfig


class MFAAdminConfig(AdminConfig):
    default_site = "config.admin_site.MFAAdminSite"
