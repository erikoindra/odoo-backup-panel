from datetime import UTC, datetime

from django import template

from panel import health

register = template.Library()


@register.filter
def ago(value):
    """A past datetime as '6 hours ago'."""
    return health.format_age(datetime.now(UTC) - value) if value else ""


@register.filter
def duration(delta):
    return health.format_duration(delta) if delta is not None else ""


@register.filter
def interval(delta):
    return health.format_interval(delta) if delta is not None else ""


@register.filter
def expected(cron):
    """The longest gap a cron schedule leaves between backups, as '24 hours'."""
    return health.format_interval(health.expected_interval(cron)) if cron else "–"
