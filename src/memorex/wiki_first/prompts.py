from __future__ import annotations

INGEST_PROMPT_VERSION = "wiki-first-ingest-v1"
REVISE_PROMPT_VERSION = "wiki-first-revise-v1"
QUERY_PROMPT_VERSION = "wiki-first-query-v1"


def ingest_prompt(*, language: str, source_names: list[str], existing: bool) -> str:
    mode = (
        "An active Wiki has been copied into wiki/. Preserve useful existing knowledge and "
        "integrate the new material into it."
        if existing
        else "The Wiki is empty. Build its first coherent version."
    )
    listed = "\n".join(f"- sources/{name}" for name in source_names)
    return f"""You are the semantic administrator of a durable {language}-language Wiki.

{mode}

Read every NEW source listed below as a semantically coherent document. Do not reduce it to
independent 2,000-character fragments. Understand themes, arguments, decisions, changes of view,
open questions, contradictions, and relations to the existing Wiki.

NEW SOURCES:
{listed}

You may read any existing file under wiki/ and sources/. Edit only wiki/ and proposal-report.md.
Never edit sources/. Do not use the Internet or outside knowledge.

The result is not one summary per source and not a claim database. Prefer a small set of durable,
substantial topic pages. Update an existing page when the subject already exists. Create a page
only for a long-lived topic worth revisiting. Preserve disagreements and evolution. It is allowed
to synthesize meaning across passages:

- direct: state it normally and cite the supporting source;
- synthesis: mark a non-obvious combined conclusion with **Синтез:** and cite every input;
- hypothesis: mark uncertainty with **Гипотеза:**;
- user source: describe it as the user's statement, correction, preference, or instruction.

Markdown contract:

- wiki/README.md is a concise landing page, not a global mega-summary;
- all other filenames are top-level ASCII kebab-case.md with a natural-language H1;
- use readable connected prose and [[page-slug]] links;
- cite important statements inline as [S1], [S2], etc.;
- every thematic page ends with ## Источники and defines all labels;
- definitions link into ../sources/ with a useful locator, preferably line ranges;
- never fabricate quotes, dates, attachment contents, or source locators.

Before finishing, reread the whole Wiki, merge duplicates, resolve every Wiki link, verify every
citation and source path, and remove unsupported additions. Write proposal-report.md describing:
sources read; pages created, changed, or removed; major synthesis decisions; conflicts and
uncertainties; ignored low-value material; and self-check results. Finish the files—do not merely
propose a structure.
"""


def revise_prompt(feedback: str) -> str:
    return f"""Revise the pending Wiki proposal in this directory according to the administrator's
feedback below. Read the current wiki/, proposal-report.md, and relevant sources/. Preserve
correct work that the feedback does not challenge. Edit only wiki/ and proposal-report.md.

ADMINISTRATOR FEEDBACK:
{feedback.strip()}

Keep the original Wiki contract: coherent topic pages, working [[links]], inline citations, and a
final ## Источники section on each thematic page. Update proposal-report.md with what changed in
this revision and run a complete self-check. Do not just explain what you would change.
"""


def query_prompt(question: str) -> str:
    return f"""Answer the user's question by navigating the active Wiki in wiki/. Start with text
search and the smallest relevant set of pages; follow [[links]] when helpful. Open files under
sources/ only when the Wiki is insufficient or a claim needs verification. The Wiki and sources
are read-only. Do not use outside knowledge.

QUESTION:
{question.strip()}

Write the complete answer to answer.md. Use natural prose, distinguish direct knowledge from
synthesis or uncertainty, cite the Wiki pages used as [[page-slug]], and mention raw source paths
only if you opened them. End with a short "Путь" section listing Wiki pages visited and raw sources
opened. Do not edit any other file.
"""
