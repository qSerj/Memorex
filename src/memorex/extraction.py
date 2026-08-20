from __future__ import annotations

import re
import time
from typing import Any

from pydantic import ValidationError

from memorex.config import LLMConfig
from memorex.domain import ClaimBatch
from memorex.llm import LLMProvider
from memorex.storage import Storage, utc_now

EXTRACTION_PROMPT_VERSION = "extract-v2"
MAX_ATTEMPTS = 3

EXTRACTION_SYSTEM_PROMPT = """You extract atomic claims from one source segment.
Return only claims explicitly supported by the segment. Do not infer missing facts.
Preserve the source language. Each evidence_quote must be a verbatim, minimal substring
of the segment that uniquely supports the statement. Preserve all punctuation, Markdown
characters, spaces, and line breaks in evidence_quote exactly as they appear in the
segment. Return an empty list when there are no factual claims."""


class ExtractionError(RuntimeError):
    """Raised when a source revision cannot be extracted safely."""


def extract_source(
    storage: Storage,
    source_id: int,
    provider: LLMProvider,
    config: LLMConfig,
    *,
    force: bool = False,
) -> dict[str, Any]:
    version = storage.get_current_version(source_id)
    if not force:
        existing = storage.find_successful_job(
            version["id"], config.model, config.base_url, EXTRACTION_PROMPT_VERSION
        )
        if existing:
            return {
                "status": "unchanged",
                "source_id": source_id,
                "version_id": version["id"],
                "job_id": existing["id"],
                "claims": len(storage.list_claims(source_id)),
            }

    job_id = storage.start_job(
        version["id"], config.model, config.base_url, EXTRACTION_PROMPT_VERSION
    )
    pending_claims: list[dict[str, Any]] = []
    try:
        for segment in storage.get_segments(version["id"]):
            validated = _extract_segment(storage, job_id, segment, provider, config)
            pending_claims.extend(validated)
        count = storage.finish_job_success(job_id, version["id"], pending_claims)
    except Exception as exc:
        storage.finish_job_failure(job_id, str(exc))
        if isinstance(exc, ExtractionError):
            raise
        raise ExtractionError(str(exc)) from exc
    return {
        "status": "extracted",
        "source_id": source_id,
        "version_id": version["id"],
        "job_id": job_id,
        "claims": count,
    }


def _extract_segment(
    storage: Storage,
    job_id: int,
    segment: dict[str, Any],
    provider: LLMProvider,
    config: LLMConfig,
) -> list[dict[str, Any]]:
    last_error = "unknown extraction error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started_at = utc_now()
        started = time.monotonic()
        raw_output: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            result = provider.complete(
                messages=[
                    {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"<segment>\n{segment['text']}\n</segment>",
                    },
                ],
                response_model=ClaimBatch,
                schema_name="claim_batch",
            )
            raw_output = result.raw_output
            input_tokens = result.input_tokens
            output_tokens = result.output_tokens
            batch = ClaimBatch.model_validate_json(raw_output)
            claims = _anchor_claims(batch, segment)
        except Exception as exc:
            last_error = _friendly_validation_error(exc)
            storage.record_llm_call(
                job_id=job_id,
                purpose="extract",
                segment_id=segment["id"],
                attempt=attempt,
                model=config.model,
                base_url=config.base_url,
                prompt_version=EXTRACTION_PROMPT_VERSION,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1_000),
                status="failed",
                validation_valid=False,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                error=last_error,
                raw_output=raw_output,
            )
            continue
        storage.record_llm_call(
            job_id=job_id,
            purpose="extract",
            segment_id=segment["id"],
            attempt=attempt,
            model=config.model,
            base_url=config.base_url,
            prompt_version=EXTRACTION_PROMPT_VERSION,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1_000),
            status="succeeded",
            validation_valid=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=None,
            raw_output=raw_output,
        )
        return claims
    raise ExtractionError(
        f"Segment {segment['id']} failed after {MAX_ATTEMPTS} attempts: {last_error}"
    )


def _anchor_claims(batch: ClaimBatch, segment: dict[str, Any]) -> list[dict[str, Any]]:
    anchored: list[dict[str, Any]] = []
    for claim in batch.claims:
        matches = _find_quote_matches(claim.evidence_quote, segment["text"])
        if len(matches) != 1:
            raise ValueError(
                "evidence_quote must occur exactly once in its segment; "
                f"found {len(matches)} occurrences"
            )
        local_start, local_end = matches[0].span()
        anchored.append(
            {
                "segment_id": segment["id"],
                "statement": claim.statement,
                "normalized_statement": " ".join(claim.statement.casefold().split()),
                "confidence": claim.confidence,
                "quote": segment["text"][local_start:local_end],
                "char_start": segment["char_start"] + local_start,
                "char_end": segment["char_start"] + local_end,
            }
        )
    return anchored


def _find_quote_matches(quote: str, segment_text: str) -> list[re.Match[str]]:
    """Find an exact quote, tolerating only model-collapsed whitespace as a fallback."""
    exact_matches = list(re.finditer(re.escape(quote), segment_text))
    if exact_matches:
        return exact_matches

    whitespace_tolerant_pattern = r"\s+".join(
        re.escape(part) for part in re.split(r"\s+", quote) if part
    )
    return list(re.finditer(whitespace_tolerant_pattern, segment_text))


def _friendly_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return f"Structured output validation failed: {exc}"
    return str(exc)
