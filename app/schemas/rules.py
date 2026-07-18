"""Routing-rule request and response schemas."""

from pydantic import BaseModel, Field, model_validator

from app.schemas.triage import Intent, Priority, Sentiment


class RuleIn(BaseModel):
    """One rule in a PUT /rules replacement. At least one condition required."""

    intent: Intent | None = None
    priority: Priority | None = None
    sentiment: Sentiment | None = None
    queue: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def _at_least_one_condition(self) -> "RuleIn":
        if self.intent is None and self.priority is None and self.sentiment is None:
            raise ValueError("each rule must set at least one of intent, priority, sentiment")
        return self


class RulesReplaceRequest(BaseModel):
    rules: list[RuleIn] = Field(max_length=100)


class RuleOut(BaseModel):
    id: int
    position: int
    intent: Intent | None
    priority: Priority | None
    sentiment: Sentiment | None
    queue: str


class RulesOut(BaseModel):
    rules: list[RuleOut]
    default_queue: str
