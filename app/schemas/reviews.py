"""Review-workflow request and export schemas."""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.triage import Intent, Priority, Sentiment


class CorrectRequest(BaseModel):
    """POST /tickets/{id}/correct body: a full label snapshot."""

    model_config = ConfigDict(str_strip_whitespace=True)

    intent: Intent
    priority: Priority
    sentiment: Sentiment
    note: str | None = Field(default=None, max_length=1000)


class ExportLine(BaseModel):
    """One line of GET /corrections/export, matching evals/dataset.jsonl fields."""

    external_id: str
    channel: str
    sender: str
    subject: str
    body: str
    intent: Intent
    priority: Priority
    sentiment: Sentiment
    source: str
