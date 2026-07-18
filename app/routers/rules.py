"""Routing-rule read and transactional replace."""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.config import Settings
from app.deps import get_db, require_api_key, settings_dep
from app.schemas.rules import RuleOut, RulesOut, RulesReplaceRequest
from app.services.routing import load_rules, replace_rules

log = logging.getLogger("app.rules")

router = APIRouter(dependencies=[Depends(require_api_key)])


def _rules_out(db: Session, settings: Settings) -> RulesOut:
    rules = [
        RuleOut(
            id=rule.id,
            position=rule.position,
            intent=rule.intent,
            priority=rule.priority,
            sentiment=rule.sentiment,
            queue=rule.queue,
        )
        for rule in load_rules(db)
    ]
    return RulesOut(rules=rules, default_queue=settings.default_queue)


@router.get("/rules")
def get_rules(
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> RulesOut:
    return _rules_out(db, settings)


@router.put("/rules")
def put_rules(
    payload: RulesReplaceRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(settings_dep),
) -> RulesOut:
    replace_rules(db, payload.rules)
    db.commit()
    log.info("routing rules replaced", extra={"rule_count": len(payload.rules)})
    return _rules_out(db, settings)
