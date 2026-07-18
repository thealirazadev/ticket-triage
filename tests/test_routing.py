"""Rule engine: order, wildcards, AND semantics, empty set, default queue."""

from app.models import RoutingRule
from app.services.routing import resolve_queue


def _rule(position, intent=None, priority=None, sentiment=None, queue="q"):
    return RoutingRule(
        position=position, intent=intent, priority=priority, sentiment=sentiment, queue=queue
    )


def test_first_match_wins():
    rules = [
        _rule(1, priority="P1", queue="urgent"),
        _rule(2, intent="billing", queue="billing"),
    ]
    queue = resolve_queue(
        rules, intent="billing", priority="P1", sentiment="neutral", default_queue="general"
    )
    assert queue == "urgent"


def test_wildcard_null_condition_matches_any():
    rules = [_rule(1, priority="P1", queue="urgent")]
    queue = resolve_queue(
        rules, intent="bug", priority="P1", sentiment="positive", default_queue="general"
    )
    assert queue == "urgent"


def test_conditions_are_anded():
    rules = [_rule(1, intent="billing", priority="P1", queue="vip")]
    # intent matches but priority does not -> no match.
    assert (
        resolve_queue(
            rules, intent="billing", priority="P3", sentiment="neutral", default_queue="general"
        )
        == "general"
    )
    assert (
        resolve_queue(
            rules, intent="billing", priority="P1", sentiment="neutral", default_queue="general"
        )
        == "vip"
    )


def test_no_match_returns_default_queue():
    rules = [_rule(1, intent="refund", queue="billing")]
    assert (
        resolve_queue(
            rules, intent="bug", priority="P4", sentiment="neutral", default_queue="general"
        )
        == "general"
    )


def test_empty_rule_set_returns_default():
    assert (
        resolve_queue([], intent="bug", priority="P1", sentiment="neutral", default_queue="general")
        == "general"
    )


def test_seeded_rules_route_as_documented(db):
    from app.services.routing import load_rules

    rules = load_rules(db)
    assert (
        resolve_queue(
            rules, intent="bug", priority="P3", sentiment="negative", default_queue="general"
        )
        == "technical"
    )
    assert (
        resolve_queue(
            rules, intent="how_to", priority="P1", sentiment="neutral", default_queue="general"
        )
        == "urgent"
    )
    assert (
        resolve_queue(
            rules, intent="refund", priority="P4", sentiment="neutral", default_queue="general"
        )
        == "billing"
    )
    assert (
        resolve_queue(
            rules, intent="other", priority="P4", sentiment="neutral", default_queue="general"
        )
        == "general"
    )
