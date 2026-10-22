from typing import Any, Literal
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_TRANSCRIPT_CHARS = 100000


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Turn(Model):
    id: str
    speaker: Literal["customer", "sales", "other", "excluded", "unknown", "pending"]
    text: str


class Evidence(Model):
    turn_id: str
    quote: str = Field(min_length=1, max_length=2500)


class Fact(Model):
    value: str | int | None = None
    evidence: list[Evidence] = Field(default_factory=list)
    qualifier: Literal["exact", "approximate", "range", "unknown"] = "exact"


class Fields(Model):
    raw_company: Fact = Field(default_factory=Fact)
    brand_name: Fact = Field(default_factory=Fact)
    industry: Fact = Field(default_factory=Fact)
    area: Fact = Field(default_factory=Fact)
    contact_name: Fact = Field(default_factory=Fact)
    contact_title: Fact = Field(default_factory=Fact)
    phone: Fact = Field(default_factory=Fact)
    mendian: Fact = Field(default_factory=Fact)
    shejishi: Fact = Field(default_factory=Fact)
    team_count: Fact = Field(default_factory=Fact)
    demand_summary: Fact = Field(default_factory=Fact)


SignalKind = Literal[
    "need_none", "need_general", "need_problem", "need_impact",
    "time_none", "time_later", "time_1_3m", "time_soon",
    "budget_none", "budget_discuss", "budget_range", "budget_approved",
    "action_polite", "action_contact", "action_request", "action_booked",
    "unknown"
]


class Signal(Model):
    kind: SignalKind = "unknown"
    evidence: list[Evidence] = Field(default_factory=list)
    note: str = Field(default="", max_length=400)


class Signals(Model):
    N: Signal = Field(default_factory=Signal)
    T: Signal = Field(default_factory=Signal)
    B: Signal = Field(default_factory=Signal)
    E: Signal = Field(default_factory=Signal)


class Extraction(Model):
    call_type: Literal["sales", "service", "invalid", "unknown"] = "unknown"
    classification_evidence: list[Evidence] = Field(default_factory=list)
    do_not_contact: bool = False
    refusal: bool = False
    policy_evidence: list[Evidence] = Field(default_factory=list)
    fields: Fields = Field(default_factory=Fields)
    signals: Signals = Field(default_factory=Signals)
    conflicts: list[str] = Field(default_factory=list, max_length=20)


class AnalyzeRequest(Model):
    transcript: str = Field(min_length=2, max_length=MAX_TRANSCRIPT_CHARS)
    title: str = Field(default="新通话", max_length=100)
    mode: Literal["llm", "rules"] = "rules"
    data_mode: Literal["real", "synthetic", "snapshot_demo"] = "real"
    call_time: str | None = None
    idempotency_key: str = Field(min_length=8, max_length=100)
    customer_id: str | None = None
    transcription_id: str | None = None

    @field_validator("transcript")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("通话文本不能为空")
        return value.strip()

    @field_validator("call_time")
    @classmethod
    def timestamp(cls, value):
        if value is not None:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None:
                raise ValueError("通话时间必须带时区，例如 +08:00")
        return value


class ReviewRequest(Model):
    company_name: str | None = Field(default=None, max_length=2000)
    intent_level: Literal["high", "medium", "low"] | None = None
    recording_reviewed: bool = False
    expected_revision: int = Field(ge=0)
    action: Literal["save", "approve", "defer", "reject", "second_review"]
    reason: str = Field(default="", max_length=2000)
    fields: dict[str, Any] = Field(default_factory=dict)
    signal_kinds: dict[str, SignalKind] = Field(default_factory=dict)
    evidence_turn_ids: list[str] = Field(default_factory=list, max_length=20)
    entity_id: str | None = None
    relation_note: str = Field(default="", max_length=1000)
    source_reference: str = Field(default="", max_length=1000)
    call_type: Literal["sales", "service", "invalid", "unknown"] | None = None


class BatchItem(Model):
    id: str
    revision: int


class BatchPreviewRequest(Model):
    items: list[BatchItem] = Field(min_length=1)
    action: Literal["defer", "reject", "approve", "set_intent"]
    reason: str = Field(default="", max_length=2000)
    intent_level: Literal["high", "medium", "low"] | None = None


class BatchCommitRequest(Model):
    preview_id: str


class Lead(Model):
    model_config = ConfigDict(extra="forbid", strict=True)
    company_name: str | None
    industry: str | None
    area: str | None
    contact_name: str | None
    contact_title: str | None
    phone: str | None
    intent_level: Literal["high", "medium", "low"]
    intent_reason: str
    demand_summary: str | None
    mendian: int | None
    shejishi: int | None
    call_time: str | None
    next_action: str
    confidence_score: float = Field(ge=0, le=1)
    need_human_review: bool


class LoginRequest(Model):
    username: str = Field(min_length=3, max_length=40, pattern=r'^[a-z0-9][a-z0-9_.-]*$')
    password: str = Field(min_length=1, max_length=128)


class UserCreate(LoginRequest):
    password: str = Field(min_length=12, max_length=128)
    name: str = Field(min_length=1)
    role: Literal['employee', 'admin'] = 'employee'


class UserUpdate(Model):
    active: bool | None = None
    password: str | None = Field(default=None, min_length=12, max_length=128)
    reason: str = Field(min_length=3, max_length=500)


class PasswordChange(Model):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class CustomerCreate(Model):
    name: str = Field(min_length=1, max_length=100)
    owner_id: str | None = None
    contact: str = Field(default='', max_length=100)
    phone: str = Field(default='', max_length=50)
    notes: str = Field(default='', max_length=2000)


class CustomerUpdate(Model):
    expected_revision: int = Field(ge=0)
    name: str = Field(min_length=1, max_length=100)
    contact: str = Field(default='', max_length=100)
    phone: str = Field(default='', max_length=50)
    notes: str = Field(default='', max_length=2000)


class AssignmentRequest(Model):
    items: list[BatchItem] = Field(min_length=1, max_length=100)
    owner_id: str | None = None
    reason: str = Field(min_length=3, max_length=500)


class TranscriptConfirm(Model):
    expected_revision: int = Field(ge=1)
    transcript: str = Field(min_length=2, max_length=MAX_TRANSCRIPT_CHARS)


class AudioIdentityRequest(Model):
    expected_revision: int = Field(ge=1)


class AudioCustomerConfirm(AudioIdentityRequest):
    customer_id: str | None = Field(default=None, min_length=1)
    new_customer: CustomerCreate | None = None
