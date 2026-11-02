"""Human-readable times for the templates.

The database holds two timestamp shapes: ISO 8601 with an offset (written from
Python) and SQLite's ``YYYY-MM-DD HH:MM:SS`` (from ``datetime('now')``, UTC).
Both are accepted. Output is a relative phrase ("4 min ago", "in 2 h") with
the exact UTC time kept in the element's title, so nothing is lost.
"""
from __future__ import annotations

from datetime import UTC, date, datetime

from markupsafe import Markup, escape


def parse(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        moment = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            moment = datetime.fromisoformat(text)
        except ValueError:
            return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def relative(value: str | datetime | None, *, now: datetime | None = None) -> str:
    moment = parse(value)
    if moment is None:
        return "never"
    now = now or datetime.now(UTC)
    seconds = (now - moment).total_seconds()
    future = seconds < 0
    seconds = abs(seconds)

    if seconds < 45:
        phrase = "a few seconds"
    elif seconds < 90:
        phrase = "1 min"
    elif seconds < 3600:
        phrase = f"{round(seconds / 60)} min"
    elif seconds < 36 * 3600:
        hours = seconds / 3600
        phrase = f"{hours:.0f} h" if hours >= 10 else f"{hours:.1f} h".replace(".0 h", " h")
    else:
        phrase = f"{round(seconds / 86400)} days"

    if phrase == "a few seconds":
        return "in a few seconds" if future else "just now"
    return f"in {phrase}" if future else f"{phrase} ago"


def exact(value: str | datetime | None) -> str:
    moment = parse(value)
    if moment is None:
        return ""
    return moment.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def time_tag(value: str | datetime | None) -> Markup:
    """``<time>`` with a relative label and the exact time on hover."""
    moment = parse(value)
    if moment is None:
        return Markup('<span class="muted">never</span>')
    return Markup(
        f'<time datetime="{escape(moment.isoformat())}" title="{escape(exact(moment))}">'
        f"{escape(relative(moment))}</time>"
    )


def day(value: str | date | None) -> str:
    """A calendar date as "2 Nov 2026". Dates are already local; no zone shift."""
    if not value:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    if not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value)[:10])
        except ValueError:
            return str(value)
    return f"{value.day} {value.strftime('%b %Y')}"


def from_unix(value) -> datetime | None:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(seconds, UTC) if seconds > 0 else None


def register(env) -> None:
    env.filters["ago"] = time_tag
    env.filters["relative"] = relative
    env.filters["exact_time"] = exact
    env.filters["day"] = day
    env.filters["timestamp"] = from_unix
