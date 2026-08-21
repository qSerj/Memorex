from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


@dataclass(frozen=True)
class SegmentDraft:
    ordinal: int
    text: str
    section: str | None
    char_start: int
    char_end: int


class ExtractedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=1)

    @field_validator("statement", "evidence_quote")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class ClaimBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[ExtractedClaim]


ClaimKind = Literal["observation", "problem", "goal", "idea", "decision", "action_item"]
ClaimLifecycle = Literal["proposed", "active", "rejected", "completed", "unknown"]
EntityType = Literal[
    "person", "organization", "project", "product", "technology", "concept", "other"
]
EntityRole = Literal["subject", "actor", "object", "about"]
RelationPredicate = Literal[
    "addresses",
    "proposes",
    "accepts",
    "rejects",
    "because_of",
    "depends_on",
    "assigned_to",
    "supports",
    "contradicts",
    "supersedes",
    "about",
]


class CompiledEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    entity_type: EntityType
    role: EntityRole


class CompiledClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    kind: ClaimKind
    lifecycle: ClaimLifecycle
    polarity: Literal["positive", "negative", "unknown"]
    actor: str | None
    valid_from: str | None
    valid_to: str | None
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_quote: str = Field(min_length=1)
    entities: list[CompiledEntity]

    @field_validator("statement", "evidence_quote")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class CompiledRelation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_name: str = Field(min_length=1)
    source_type: EntityType
    predicate: RelationPredicate
    target_name: str = Field(min_length=1)
    target_type: EntityType
    evidence_quote: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class CompilationBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claims: list[CompiledClaim]
    relations: list[CompiledRelation]


class SourceSummaryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    summary: str = Field(min_length=1)


class ResolutionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_claim_index: int = Field(ge=0)
    existing_claim_id: int = Field(ge=1)
    proposal_type: Literal["contradiction", "supersedes"]
    rationale: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class ResolutionBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposals: list[ResolutionProposal]


class AnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer: str = Field(min_length=1)
    citations: list[int]

    @field_validator("answer")
    @classmethod
    def strip_answer(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


@dataclass(frozen=True)
class ModelCallResult:
    raw_output: str
    input_tokens: int | None
    output_tokens: int | None
