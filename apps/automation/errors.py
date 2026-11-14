class AutomationError(Exception):
    pass


class AutomationUnavailable(AutomationError):
    pass


class AutomationLimitReached(AutomationError):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__("AI request limit reached")


class AutomationConflict(AutomationError):
    pass
