"""GET /stats: operational totals for ticket flow and provider spend."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.deps import get_db, require_api_key
from app.errors import ValidationError
from app.schemas.stats import StatsOut
from app.services.stats import compute_stats

router = APIRouter(dependencies=[Depends(require_api_key)])


def _parse_since(since: str | None) -> datetime | None:
    """Parse an ISO-8601 timestamp to a naive-UTC datetime for comparison against
    stored timestamps (always UTC). Raise 422 on a malformed value."""
    if since is None:
        return None
    try:
        parsed = datetime.fromisoformat(since)
    except ValueError as exc:
        raise ValidationError("since must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


@router.get("/stats")
def get_stats(
    db: Session = Depends(get_db),
    since: str | None = Query(default=None),
) -> StatsOut:
    since_dt = _parse_since(since)
    return compute_stats(db, since_dt, since_raw=since)
