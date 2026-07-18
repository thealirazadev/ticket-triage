"""The strict schema that gates LLM output into the system.

Only ``TriageResult`` may turn provider text into data. Anything that fails this
model is a logged failure, never a stored value. Label values are normalized
cheaply (case/whitespace) before enum checking to avoid pointless repair calls.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Canonical label sets. Changing these is a contract-level change (see rules.md).
INTENTS: tuple[str, ...] = (
    "billing",
    "bug",
    "how_to",
    "feature_request",
    "account_access",
    "refund",
    "other",
)
PRIORITIES: tuple[str, ...] = ("P1", "P2", "P3", "P4")
SENTIMENTS: tuple[str, ...] = ("negative", "neutral", "positive")

Intent = Literal["billing", "bug", "how_to", "feature_request", "account_access", "refund", "other"]
Priority = Literal["P1", "P2", "P3", "P4"]
Sentiment = Literal["negative", "neutral", "positive"]


class TriageResult(BaseModel):
    """Validated classification of one ticket. Unknown extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    priority: Priority
    sentiment: Sentiment
    summary: str = Field(min_length=1, max_length=600)

    @field_validator("intent", "sentiment", mode="before")
    @classmethod
    def _normalize_lower(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("priority", mode="before")
    @classmethod
    def _normalize_upper(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @field_validator("summary", mode="before")
    @classmethod
    def _strip_summary(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value
