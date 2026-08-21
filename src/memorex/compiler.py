from __future__ import annotations

import re
import time
from collections.abc import Sequence
from typing import Any

from memorex.config import LLMConfig
from memorex.domain import (
    CompilationBatch,
    ResolutionBatch,
    SourceSummaryResult,
)
from memorex.extraction import ExtractionError, _find_quote_matches, _friendly_validation_error
from memorex.llm import LLMProvider
from memorex.storage import Storage, utc_now

COMPILATION_PROMPT_VERSION = "decision-compile-v1"
SUMMARY_PROMPT_VERSION = "source-summary-v1"
RESOLUTION_PROMPT_VERSION = "decision-resolution-v1"
MAX_ATTEMPTS = 3

COMPILATION_SYSTEM_PROMPT = """Compile one source segment into evidence-backed knowledge.
Preserve the source language. Extract only explicit information; do not infer missing facts,
authors, dates, decisions, or motivations. Classify each claim as observation, problem, goal,
idea, decision, or action_item. lifecycle describes the source's explicit stance: proposed,
active, rejected, completed, or unknown. A discussed option is not automatically a decision.
Every evidence_quote must be a minimal verbatim substring. Preserve punctuation, Markdown,
spaces, and line breaks. Entities and typed relations must be supported by the same evidence.
Return empty lists when the segment has no durable knowledge."""

SUMMARY_SYSTEM_PROMPT = """Summarize a source from its already extracted claims only.
Do not add facts. Preserve uncertainty and clearly distinguish active decisions, rejected
options, proposals, problems, goals, and action items. Write a concise Russian summary."""

RESOLUTION_SYSTEM_PROMPT = """Compare new claims with existing evidence-backed claims.
Propose only clear contradictions or cases where a newer claim explicitly supersedes an older
claim. Differences in wording, scope, uncertainty, or context are not contradictions. Return
an empty list when no review is needed. Never invent IDs or indices."""


def compile_source(
    storage: Storage,
    source_id: int,
    fast_provider: LLMProvider,
    fast_config: LLMConfig,
    strong_provider: LLMProvider,
    strong_config: LLMConfig,
    *,
    force: bool = False,
) -> dict[str, Any]:
    version = storage.get_current_version(source_id)
    job_prompt_version = f"{COMPILATION_PROMPT_VERSION}+{strong_config.model}"
    if not force:
        existing = storage.find_successful_job(
            version["id"], fast_config.model, fast_config.base_url, job_prompt_version
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
        version["id"], fast_config.model, fast_config.base_url, job_prompt_version
    )
    pending_claims: list[dict[str, Any]] = []
    try:
        metadata = storage.get_source_metadata(version["id"])
        for segment in storage.get_segments(version["id"]):
            pending_claims.extend(
                _compile_segment(
                    storage,
                    job_id,
                    segment,
                    metadata,
                    fast_provider,
                    fast_config,
                )
            )
        summary = _summarize_source(
            storage,
            job_id,
            pending_claims,
            metadata,
            strong_provider,
            strong_config,
        )
        proposals = _propose_resolutions(
            storage,
            job_id,
            pending_claims,
            strong_provider,
            strong_config,
        )
        count = storage.finish_job_success(
            job_id,
            version["id"],
            pending_claims,
            summary=summary,
            proposals=proposals,
        )
    except Exception as exc:
        storage.finish_job_failure(job_id, str(exc))
        if isinstance(exc, ExtractionError):
            raise
        raise ExtractionError(str(exc)) from exc
    return {
        "status": "compiled",
        "source_id": source_id,
        "version_id": version["id"],
        "job_id": job_id,
        "claims": count,
        "review_proposals": len(proposals),
    }


def _compile_segment(
    storage: Storage,
    job_id: int,
    segment: dict[str, Any],
    metadata: dict[str, Any] | None,
    provider: LLMProvider,
    config: LLMConfig,
) -> list[dict[str, Any]]:
    metadata_context = _metadata_context(metadata)
    last_error = "unknown compilation error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started_at = utc_now()
        started = time.monotonic()
        raw_output: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            segment_content = f"{metadata_context}\n\n<segment>\n{segment['text']}\n</segment>"
            result = provider.complete(
                messages=[
                    {"role": "system", "content": COMPILATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": segment_content,
                    },
                ],
                response_model=CompilationBatch,
                schema_name="knowledge_compilation_batch",
            )
            raw_output = result.raw_output
            input_tokens = result.input_tokens
            output_tokens = result.output_tokens
            batch = CompilationBatch.model_validate_json(raw_output)
            claims = _anchor_compilation(batch, segment)
        except Exception as exc:
            last_error = _friendly_validation_error(exc)
            storage.record_llm_call(
                job_id=job_id,
                purpose="extract",
                segment_id=segment["id"],
                attempt=attempt,
                model=config.model,
                base_url=config.base_url,
                prompt_version=COMPILATION_PROMPT_VERSION,
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
            prompt_version=COMPILATION_PROMPT_VERSION,
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


def _anchor_compilation(batch: CompilationBatch, segment: dict[str, Any]) -> list[dict[str, Any]]:
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
                "kind": claim.kind,
                "lifecycle": claim.lifecycle,
                "polarity": claim.polarity,
                "actor": claim.actor,
                "valid_from": claim.valid_from,
                "valid_to": claim.valid_to,
                "confidence": claim.confidence,
                "quote": segment["text"][local_start:local_end],
                "char_start": segment["char_start"] + local_start,
                "char_end": segment["char_start"] + local_end,
                "local_start": local_start,
                "local_end": local_end,
                "entities": [entity.model_dump() for entity in claim.entities],
                "relations": [],
            }
        )

    for relation in batch.relations:
        matches = _find_quote_matches(relation.evidence_quote, segment["text"])
        if len(matches) != 1:
            raise ValueError(
                "relation evidence_quote must occur exactly once in its segment; "
                f"found {len(matches)} occurrences"
            )
        relation_start, relation_end = matches[0].span()
        relation_data = relation.model_dump(exclude={"evidence_quote"})
        owner = next(
            (
                claim
                for claim in anchored
                if claim["local_start"] <= relation_start and claim["local_end"] >= relation_end
            ),
            None,
        )
        if owner is None:
            quote = segment["text"][relation_start:relation_end]
            relation_statement = (
                f"{relation.source_name} {relation.predicate} {relation.target_name}"
            )
            owner = {
                "segment_id": segment["id"],
                "statement": (
                    f"{relation.source_name} {relation.predicate.replace('_', ' ')} "
                    f"{relation.target_name}"
                ),
                "normalized_statement": " ".join(relation_statement.casefold().split()),
                "kind": "observation",
                "lifecycle": "unknown",
                "polarity": "positive",
                "actor": None,
                "valid_from": None,
                "valid_to": None,
                "confidence": relation.confidence,
                "quote": quote,
                "char_start": segment["char_start"] + relation_start,
                "char_end": segment["char_start"] + relation_end,
                "local_start": relation_start,
                "local_end": relation_end,
                "entities": [
                    {
                        "name": relation.source_name,
                        "entity_type": relation.source_type,
                        "role": "subject",
                    },
                    {
                        "name": relation.target_name,
                        "entity_type": relation.target_type,
                        "role": "object",
                    },
                ],
                "relations": [],
            }
            anchored.append(owner)
        owner["relations"].append(relation_data)

    for claim in anchored:
        claim.pop("local_start", None)
        claim.pop("local_end", None)
    return anchored


def _summarize_source(
    storage: Storage,
    job_id: int,
    claims: Sequence[dict[str, Any]],
    metadata: dict[str, Any] | None,
    provider: LLMProvider,
    config: LLMConfig,
) -> dict[str, Any]:
    if not claims:
        return {
            "title": (metadata or {}).get("title") or "Источник без извлечённых утверждений",
            "body": "В источнике не найдено проверяемых утверждений для досье.",
            "prompt_version": SUMMARY_PROMPT_VERSION,
            "model": config.model,
        }
    context = "\n".join(
        f"- [{claim['kind']}/{claim['lifecycle']}] {claim['statement']}" for claim in claims
    )
    last_error = "unknown summary error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started_at = utc_now()
        started = time.monotonic()
        raw_output: str | None = None
        try:
            summary_content = f"{_metadata_context(metadata)}\n\n<claims>\n{context}\n</claims>"
            result = provider.complete(
                messages=[
                    {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": summary_content,
                    },
                ],
                response_model=SourceSummaryResult,
                schema_name="source_summary",
            )
            raw_output = result.raw_output
            summary = SourceSummaryResult.model_validate_json(raw_output)
        except Exception as exc:
            last_error = _friendly_validation_error(exc)
            storage.record_llm_call(
                job_id=job_id,
                purpose="extract",
                segment_id=None,
                attempt=attempt,
                model=config.model,
                base_url=config.base_url,
                prompt_version=SUMMARY_PROMPT_VERSION,
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1_000),
                status="failed",
                validation_valid=False,
                input_tokens=None,
                output_tokens=None,
                error=last_error,
                raw_output=raw_output,
            )
            continue
        storage.record_llm_call(
            job_id=job_id,
            purpose="extract",
            segment_id=None,
            attempt=attempt,
            model=config.model,
            base_url=config.base_url,
            prompt_version=SUMMARY_PROMPT_VERSION,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1_000),
            status="succeeded",
            validation_valid=True,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            error=None,
            raw_output=raw_output,
        )
        return {
            "title": summary.title,
            "body": summary.summary,
            "prompt_version": SUMMARY_PROMPT_VERSION,
            "model": config.model,
        }
    raise ExtractionError(f"Source summary failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _propose_resolutions(
    storage: Storage,
    job_id: int,
    claims: Sequence[dict[str, Any]],
    provider: LLMProvider,
    config: LLMConfig,
) -> list[dict[str, Any]]:
    significant = [
        (index, claim)
        for index, claim in enumerate(claims)
        if claim["kind"] in {"idea", "decision", "action_item"}
    ]
    candidates: dict[int, dict[str, Any]] = {}
    for _, claim in significant:
        tokens = re.findall(r"[^\W_]+", claim["statement"].casefold(), flags=re.UNICODE)
        query = " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens) if len(token) > 2)
        if not query:
            continue
        for candidate in storage.search_claims(query, 5):
            candidates[candidate["id"]] = candidate
    if not significant or not candidates:
        return []

    new_context = "\n".join(f"[N{index}] {claim['statement']}" for index, claim in significant)
    old_context = "\n".join(
        f"[C{candidate_id}] {candidate['statement']}"
        for candidate_id, candidate in candidates.items()
    )
    started_at = utc_now()
    started = time.monotonic()
    raw_output: str | None = None
    try:
        resolution_content = (
            f"<new>\n{new_context}\n</new>\n\n<existing>\n{old_context}\n</existing>"
        )
        result = provider.complete(
            messages=[
                {"role": "system", "content": RESOLUTION_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": resolution_content,
                },
            ],
            response_model=ResolutionBatch,
            schema_name="resolution_proposals",
        )
        raw_output = result.raw_output
        batch = ResolutionBatch.model_validate_json(raw_output)
        allowed_indices = {index for index, _ in significant}
        allowed_ids = set(candidates)
        proposals = [
            proposal.model_dump()
            for proposal in batch.proposals
            if proposal.new_claim_index in allowed_indices
            and proposal.existing_claim_id in allowed_ids
        ]
    except Exception as exc:
        storage.record_llm_call(
            job_id=job_id,
            purpose="extract",
            segment_id=None,
            attempt=1,
            model=config.model,
            base_url=config.base_url,
            prompt_version=RESOLUTION_PROMPT_VERSION,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1_000),
            status="failed",
            validation_valid=False,
            input_tokens=None,
            output_tokens=None,
            error=_friendly_validation_error(exc),
            raw_output=raw_output,
        )
        return []
    storage.record_llm_call(
        job_id=job_id,
        purpose="extract",
        segment_id=None,
        attempt=1,
        model=config.model,
        base_url=config.base_url,
        prompt_version=RESOLUTION_PROMPT_VERSION,
        started_at=started_at,
        duration_ms=int((time.monotonic() - started) * 1_000),
        status="succeeded",
        validation_valid=True,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        error=None,
        raw_output=raw_output,
    )
    return proposals


def _metadata_context(metadata: dict[str, Any] | None) -> str:
    if not metadata:
        return "Source metadata is unknown."
    return (
        "Source metadata (trusted user input):\n"
        f"title={metadata.get('title')!r}\n"
        f"kind={metadata.get('source_kind')!r}\n"
        f"author={metadata.get('author')!r}\n"
        f"authority={metadata.get('authority')!r}\n"
        f"occurred_from={metadata.get('occurred_from')!r}\n"
        f"occurred_to={metadata.get('occurred_to')!r}"
    )
