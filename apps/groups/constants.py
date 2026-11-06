from django.db import models


class GroupStatus(models.TextChoices):
    DRAFT = "DRAFT", "Draft"
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    CLOSED = "CLOSED", "Closed"
    ARCHIVED = "ARCHIVED", "Archived"


class MembershipMode(models.TextChoices):
    INVITE_ONLY = "INVITE_ONLY", "Invitation only"
    APPLICATION = "APPLICATION", "Application"
    OPEN = "OPEN", "Open"


class MemberStatus(models.TextChoices):
    INVITED = "INVITED", "Invited"
    PENDING_APPROVAL = "PENDING_APPROVAL", "Pending approval"
    ACTIVE = "ACTIVE", "Active"
    SUSPENDED = "SUSPENDED", "Suspended"
    LEFT = "LEFT", "Left"
    REMOVED = "REMOVED", "Removed"


MFA_ROLES = frozenset({"SUPER_ADMIN", "PLATFORM_ADMIN", "GROUP_OWNER", "CHAIRMAN", "TREASURER"})
PLATFORM_ROLES = frozenset({"SUPER_ADMIN", "PLATFORM_ADMIN", "SUPPORT_ADMIN"})
