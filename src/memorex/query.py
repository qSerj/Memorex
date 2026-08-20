from __future__ import annotations

import re
import time
from typing import Any

from pydantic import ValidationError

from memorex.config import LLMConfig
from memorex.domain import AnswerResult
from memorex.llm import LLMProvider
from memorex.storage import Storage, utc_now

ANSWER_PROMPT_VERSION = "answer-v1"
MAX_ATTEMPTS = 3

ANSWER_SYSTEM_PROMPT = """Answer only from the supplied claims and evidence.
Every factual statement must cite one or more candidate labels such as [C12].
Return the database claim IDs you cited. If the evidence is insufficient, say so.
Never cite an ID that is not present in the supplied context."""


class QueryError(RuntimeError):
    """Raised when a supported answer cannot be produced safely."""


def build_fts_query(question: str) -> str:
    tokens = re.findall(r"[^\W_]+", question.casefold(), flags=re.UNICODE)
    unique_tokens = list(dict.fromkeys(tokens))
    return " OR ".join(f'"{token.replace(chr(34), chr(34) * 2)}"' for token in unique_tokens)


def answer_question(
    storage: Storage,
    question: str,
    provider: LLMProvider,
    config: LLMConfig,
    *,
    limit: int = 8,
) -> dict[str, Any]:
    fts_query = build_fts_query(question)
    if not fts_query:
        raise QueryError("Question must contain at least one word or number")
    candidates = storage.search_claims(fts_query, limit)
    if not candidates:
        return {
            "status": "no_matches",
            "question": question,
            "answer": "No supported claims were found. Add and extract relevant sources first.",
            "citations": [],
        }

    context = "\n\n".join(
        (
            f"[C{candidate['id']}] {candidate['statement']}\n"
            f"Evidence: {candidate['quote']}\n"
            f"Source: {candidate['canonical_path']} (revision {candidate['revision']}, "
            f"chars {candidate['char_start']}:{candidate['char_end']})"
        )
        for candidate in candidates
    )
    allowed_ids = {candidate["id"] for candidate in candidates}
    last_error = "unknown answer error"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        started_at = utc_now()
        started = time.monotonic()
        raw_output: str | None = None
        input_tokens: int | None = None
        output_tokens: int | None = None
        try:
            result = provider.complete(
                messages=[
                    {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": f"Question: {question}\n\n<context>\n{context}\n</context>",
                    },
                ],
                response_model=AnswerResult,
                schema_name="cited_answer",
            )
            raw_output = result.raw_output
            input_tokens = result.input_tokens
            output_tokens = result.output_tokens
            answer = AnswerResult.model_validate_json(raw_output)
            _validate_citations(answer, allowed_ids)
        except Exception as exc:
            last_error = _friendly_validation_error(exc)
            storage.record_llm_call(
                job_id=None,
                purpose="answer",
                segment_id=None,
                attempt=attempt,
                model=config.model,
                base_url=config.base_url,
                prompt_version=ANSWER_PROMPT_VERSION,
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
            job_id=None,
            purpose="answer",
            segment_id=None,
            attempt=attempt,
            model=config.model,
            base_url=config.base_url,
            prompt_version=ANSWER_PROMPT_VERSION,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started) * 1_000),
            status="succeeded",
            validation_valid=True,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            error=None,
            raw_output=raw_output,
        )
        cited = [candidate for candidate in candidates if candidate["id"] in answer.citations]
        return {
            "status": "answered",
            "question": question,
            "answer": answer.answer,
            "citations": cited,
        }
    raise QueryError(f"Answer failed after {MAX_ATTEMPTS} attempts: {last_error}")


def _validate_citations(answer: AnswerResult, allowed_ids: set[int]) -> None:
    listed = set(answer.citations)
    markers = {int(value) for value in re.findall(r"\[C(\d+)]", answer.answer)}
    if not listed:
        raise ValueError("answer must cite at least one retrieved claim")
    if not listed <= allowed_ids:
        raise ValueError("answer cites a claim outside the retrieved context")
    if markers != listed:
        raise ValueError("citation markers in answer must exactly match citations")


def _friendly_validation_error(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        return f"Structured output validation failed: {exc}"
    return str(exc)
