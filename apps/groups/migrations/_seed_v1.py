"""Immutable v1 seed algorithm, using only the supplied historical registry."""

import json
from pathlib import Path
from typing import Any

from django.apps.registry import Apps


def seed(apps: Apps, database: str = "default") -> None:
    data = json.loads((Path(__file__).parent / "catalogue_v1.json").read_text())
    permission_model = apps.get_model("groups", "Permission")
    template_model = apps.get_model("groups", "RoleTemplate")
    role_model = apps.get_model("groups", "GroupRole")
    grant_model = apps.get_model("groups", "GroupPermission")
    type_model = apps.get_model("groups", "GroupType")
    set_model = apps.get_model("groups", "RoleSet")
    state_model = apps.get_model("groups", "PermissionState")
    permissions = {}
    for values in data["permissions"]:
        permission, _ = permission_model.objects.using(database).update_or_create(
            code=values["code"], defaults={k: v for k, v in values.items() if k != "code"}
        )
        permissions[permission.code] = permission
    templates: dict[str, Any] = {}
    for values in data["roles"]:
        template, _ = template_model.objects.using(database).update_or_create(
            code=values["code"], defaults={k: v for k, v in values.items() if k != "code"}
        )
        templates[template.code] = template
        if template.scope == "PLATFORM":
            role, _ = role_model.objects.using(database).get_or_create(
                group=None,
                code=template.code,
                defaults={
                    "name": template.name,
                    "template": template,
                    "is_system": True,
                    "is_configurable": True,
                },
            )
            for code, policy in template.policies.items():
                # Existing optional customisations survive an idempotent repair.
                if policy == "optional":
                    grant_model.objects.using(database).get_or_create(
                        group_role=role, permission=permissions[code], defaults={"granted": False}
                    )
                else:
                    # Required grants cannot be weakened by the DB guards.
                    # Avoid rewriting their historical actor on a no-op repair.
                    grant_model.objects.using(database).get_or_create(
                        group_role=role, permission=permissions[code], defaults={"granted": True}
                    )
    for code, names in data["group_types"].items():
        role_set, _ = set_model.objects.using(database).get_or_create(code=code)
        role_set.roles.set([templates[name] for name in names])
        type_model.objects.using(database).get_or_create(
            code=code,
            defaults={
                "name": code.replace("_", " ").title(),
                "is_system": True,
                "default_role_set": role_set,
            },
        )
    state_model.objects.using(database).get_or_create(pk=1)
