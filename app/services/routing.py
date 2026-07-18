"""First-match routing: the first rule whose non-null conditions all match wins.

Rules are evaluated in ascending position order; a rule's conditions are AND-ed;
a null condition is a wildcard. No match resolves to the default queue.
"""

from collections.abc import Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models import RoutingRule
from app.schemas.rules import RuleIn


def resolve_queue(
    rules: Sequence[RoutingRule],
    *,
    intent: str,
    priority: str,
    sentiment: str,
    default_queue: str,
) -> str:
    for rule in rules:
        if rule.intent is not None and rule.intent != intent:
            continue
        if rule.priority is not None and rule.priority != priority:
            continue
        if rule.sentiment is not None and rule.sentiment != sentiment:
            continue
        return rule.queue
    return default_queue


def load_rules(db: Session) -> list[RoutingRule]:
    return list(db.scalars(select(RoutingRule).order_by(RoutingRule.position)))


def replace_rules(db: Session, rules: Iterable[RuleIn]) -> None:
    """Atomically replace the whole ordered rule set. Positions come from array
    order (1-based). The caller commits; on error nothing is persisted."""
    db.execute(delete(RoutingRule))
    for position, rule in enumerate(rules, start=1):
        db.add(
            RoutingRule(
                position=position,
                intent=rule.intent,
                priority=rule.priority,
                sentiment=rule.sentiment,
                queue=rule.queue,
            )
        )
