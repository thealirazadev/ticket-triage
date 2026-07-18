"""Request and response schemas for ticket ingestion and reads."""

from datetime import UTC, datetime
from typing import Annotated

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
    model_validator,
)

from app.models import CHANNELS
from app.schemas.triage import Intent, Priority, Sentiment


def _iso_z(value: datetime) -> str:
    """Serialize a timestamp as ISO-8601 UTC with a trailing Z. Stored values are
    always UTC; SQLite returns them naive, so a missing tzinfo is treated as UTC."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    else:
        value = value.astimezone(UTC)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


UtcDatetime = Annotated[datetime, PlainSerializer(_iso_z, return_type=str)]


class TicketCreateRequest(BaseModel):
    """POST /tickets body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    external_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1, max_length=500)
    body: str = Field(min_length=1, max_length=50_000)
    sender: str = Field(min_length=1, max_length=320)
    channel: str

    @field_validator("channel")
    @classmethod
    def _check_channel(cls, value: str) -> str:
        if value not in CHANNELS:
            raise ValueError(f"channel must be one of {', '.join(CHANNELS)}")
        return value

    @field_validator("body")
    @classmethod
    def _body_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("body must not be blank")
        return value


class EmailWebhookRequest(BaseModel):
    """POST /webhooks/email body. Unknown extra fields are ignored."""

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    message_id: str = Field(min_length=1, max_length=128)
    sender: str = Field(min_length=1, max_length=320)
    text: str = Field(min_length=1, max_length=50_000)
    subject: str | None = Field(default=None, max_length=500)

    @model_validator(mode="before")
    @classmethod
    def _map_email_fields(cls, data: object) -> object:
        # Map the email-webhook "from" key onto sender before field validation.
        # Done here rather than with a field alias, which FastAPI re-applies in a
        # way that emits a spurious pydantic warning.
        if isinstance(data, dict) and "from" in data and "sender" not in data:
            data = {**data, "sender": data["from"]}
        return data

    @field_validator("text")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must not be blank")
        return value


class TriageOut(BaseModel):
    intent: Intent
    priority: Priority
    sentiment: Sentiment
    summary: str
    model: str
    prompt_version: str
    attempts: int
    created_at: UtcDatetime


class CorrectionOut(BaseModel):
    intent: Intent
    priority: Priority
    sentiment: Sentiment
    note: str | None
    created_at: UtcDatetime


class MetricsOut(BaseModel):
    llm_calls: int
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float
    latency_ms: int


class TicketListItem(BaseModel):
    """List form: omits body and correction."""

    id: str
    external_id: str
    channel: str
    sender: str
    subject: str
    status: str
    queue: str | None
    triage_error: str | None
    triage: TriageOut | None
    created_at: UtcDatetime
    updated_at: UtcDatetime


class TicketOut(TicketListItem):
    """Detail form: adds body, correction, and metrics."""

    body: str
    correction: CorrectionOut | None
    metrics: MetricsOut | None


class TicketListOut(BaseModel):
    tickets: list[TicketListItem]
    total: int
    limit: int
    offset: int
