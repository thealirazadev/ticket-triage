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


def test_sentiment_only_condition_discriminates():
    # The sentiment branch is never exercised by the seeded rules (they set only
    # intent/priority), so cover a rule whose sole condition is sentiment.
    rules = [_rule(1, sentiment="negative", queue="escalations")]
    assert (
        resolve_queue(
            rules, intent="how_to", priority="P3", sentiment="negative", default_queue="general"
        )
        == "escalations"
    )
    assert (
        resolve_queue(
            rules, intent="how_to", priority="P3", sentiment="positive", default_queue="general"
        )
        == "general"
    )


def test_falls_through_to_later_matching_rule():
    # The first rule does not match; a later rule does. The result must be the
    # later rule's queue, not the default and not the skipped rule's queue.
    rules = [
        _rule(1, intent="refund", queue="billing"),
        _rule(2, priority="P1", queue="urgent"),
    ]
    assert (
        resolve_queue(
            rules, intent="bug", priority="P1", sentiment="neutral", default_queue="general"
        )
        == "urgent"
    )


def test_all_three_conditions_must_all_match():
    rules = [_rule(1, intent="billing", priority="P1", sentiment="negative", queue="vip")]
    # All three align -> match.
    assert (
        resolve_queue(
            rules, intent="billing", priority="P1", sentiment="negative", default_queue="general"
        )
        == "vip"
    )
    # Only the sentiment differs -> the AND fails and it falls to default.
    assert (
        resolve_queue(
            rules, intent="billing", priority="P1", sentiment="neutral", default_queue="general"
        )
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
