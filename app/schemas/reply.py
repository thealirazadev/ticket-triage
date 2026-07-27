"""Schemas for suggested reply drafts.

``ReplyDraftResult`` is the strict gate for provider draft output: only a
validated reply string ever becomes a stored draft, mirroring how
``TriageResult`` gates classification. The rest are the API request and response
shapes. Drafts are never sent; these types only describe the human-in-the-loop
review of a draft.
"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.tickets import MetricsOut, UtcDatetime

# A suggested first reply is short by design; longer output is a signal the model
# ignored the instruction, so it is rejected rather than truncated.
MAX_REPLY_CHARS = 2000


class ReplyDraftResult(BaseModel):
    """Validated provider draft output. Unknown extra keys are ignored."""

    model_config = ConfigDict(extra="ignore")

    reply: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)

    @field_validator("reply", mode="before")
    @classmethod
    def _strip(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ReplyEditRequest(BaseModel):
    """PUT /tickets/{id}/reply body: a human-edited draft body."""

    model_config = ConfigDict(str_strip_whitespace=True)

    body: str = Field(min_length=1, max_length=MAX_REPLY_CHARS)


class ReplyDraftOut(BaseModel):
    """A stored reply draft. ``metrics`` sums only the draft-generation calls for
    the ticket, so draft cost is separable from triage cost."""

    ticket_id: str
    body: str
    state: str
    model: str
    prompt_version: str
    attempts: int
    metrics: MetricsOut | None
    created_at: UtcDatetime
    updated_at: UtcDatetime
