class GroupError(Exception):
    pass


class GroupConflict(GroupError):
    pass


class GroupRuleViolation(GroupError):
    pass


class GroupNotFound(GroupError):
    pass
