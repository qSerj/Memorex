from __future__ import annotations

from dataclasses import dataclass

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
