"""Celery wrappers; durable state and retry decisions live in services."""

from celery import shared_task

from apps.automation import services


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]
def run_automation(public_id: str) -> None:
    services.run(public_id)


@shared_task(ignore_result=True)  # type: ignore[untyped-decorator]
def dispatch_automations() -> int:
    return services.dispatch_pending()
