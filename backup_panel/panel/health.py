"""How healthy a backup stack looks: is its newest backup recent enough for its schedule?

Pure functions, no Docker. The schedule comes from the backup container's CRON_SCHEDULE.
"""

import itertools
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

STALE_FACTOR = 1.5  # a backup is stale once it is older than this many times the expected interval
DEFAULT_INTERVAL = timedelta(hours=24)

FIELD_RANGES = (
    (0, 59),
    (0, 23),
    (1, 31),
    (1, 12),
    (0, 7),
)  # minute, hour, day of month, month, day of week (0 and 7 = Sunday)


def parse_field(text, low, high):
    """The set of values a cron field matches, or None when it cannot be understood.
    Supports *, */n, a, a-b, a-b/n and comma lists."""
    values = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, step_text = part.partition("/")
            if not step_text.isdigit() or int(step_text) < 1:
                return None
            step = int(step_text)
        if part == "*":
            start, end = low, high
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            start, end = int(a), int(b)
        elif part.isdigit():
            start = int(part)
            end = high if step > 1 else start
        else:
            return None
        if start < low or end > high or start > end:
            return None
        values.update(range(start, end + 1, step))
    return values


def parse_cron(expression):
    """(minutes, hours, days, months, weekdays, day_restricted, weekday_restricted) or None."""
    fields = expression.split()
    if len(fields) != 5:
        return None
    sets = [parse_field(f, *r) for f, r in zip(fields, FIELD_RANGES, strict=True)]
    if any(s is None for s in sets):
        return None
    sets[4] = {d % 7 for d in sets[4]}  # 7 is Sunday too
    return (*sets, fields[2] != "*", fields[4] != "*")


def expected_interval(expression, start=None, days=60):
    """The longest gap between two runs of a cron schedule over the next `days` days. Longest, not average:
    a weekday-only schedule is quiet over the weekend, and that is not a missed backup."""
    parsed = parse_cron(expression or "")
    if parsed is None:
        return DEFAULT_INTERVAL
    minutes, hours, dom, months, dow, dom_restricted, dow_restricted = parsed
    start = start or datetime(2026, 1, 1, tzinfo=UTC)
    runs = []
    for offset in range(days):
        day = start + timedelta(days=offset)
        if day.month not in months:
            continue
        in_dom, in_dow = day.day in dom, (day.weekday() + 1) % 7 in dow
        if dom_restricted and dow_restricted:
            matches = in_dom or in_dow  # cron: when both are restricted either one is enough
        else:
            matches = in_dom and in_dow
        if matches:
            runs.extend(
                day.replace(hour=h, minute=m, second=0, microsecond=0) for h in sorted(hours) for m in sorted(minutes)
            )
    gaps = [b - a for a, b in itertools.pairwise(runs)]
    return max(gaps) if gaps else DEFAULT_INTERVAL


def format_age(delta):
    """'just now', '12 minutes ago', '6 hours ago', '3 days ago'."""
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} minutes ago"
    if seconds < 48 * 3600:
        return f"{seconds // 3600} hours ago"
    return f"{seconds // 86400} days ago"


def format_interval(delta):
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return f"{round(delta.total_seconds() / 60)} minutes"
    if hours >= 48 and hours % 24 == 0:
        return f"{int(hours // 24)} days"
    return f"{hours:g} hours"


def format_duration(delta):
    """'41 s', '3 min 12 s', '1 h 2 min'."""
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 60:
        return f"{seconds} s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} min {seconds} s" if seconds else f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min"


@dataclass(frozen=True, slots=True)
class Health:
    """level is one of: fresh, stale, empty, stopped, unreadable, loading. It drives the colour AND an
    icon and a label, so the state never depends on colour alone."""

    level: str
    label: str
    reason: str = ""

    @property
    def css(self):
        return {"fresh": "good", "stale": "warn", "empty": "warn", "stopped": "bad", "unreadable": "bad"}.get(
            self.level, "idle"
        )


def assess(instance, listing, error="", now=None):
    """The Health of an instance from its backup listing, or from why there is none: a stopped
    container, or a listing that could not be read (`error`)."""
    now = now or datetime.now(UTC)
    if not instance.is_running:
        return Health(
            "stopped",
            "Container stopped",
            "Backups cannot be read while the container is stopped. Start the stack in Portainer.",
        )
    if error:
        return Health("unreadable", "Unreadable", error)
    if listing is None:
        return Health("loading", "Loading")
    latest = listing.latest
    if latest is None:
        return Health("empty", "No backups yet", "The first backup runs on schedule, or start one now.")
    interval = expected_interval(instance.config.get("CRON_SCHEDULE", ""))
    age = now - latest.mtime
    limit = interval * STALE_FACTOR
    if age > limit:
        hours = age.total_seconds() / 3600
        return Health(
            "stale",
            "Stale",
            f"{hours:.0f} hours since the last backup, past {STALE_FACTOR:g}× the expected interval "
            f"({limit.total_seconds() / 3600:.0f} hours). Check the backup container's cron.",
        )
    return Health("fresh", "Fresh")
