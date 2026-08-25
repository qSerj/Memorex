from __future__ import annotations

INGEST_PROMPT_VERSION = "wiki-first-ingest-v5"
REVISE_PROMPT_VERSION = "wiki-first-revise-v2"
QUERY_PROMPT_VERSION = "wiki-first-query-v1"


def ingest_prompt(
    *,
    language: str,
    source_items: list[dict[str, str]],
    existing: bool,
    selected_pages: list[str] | None = None,
    packet: bool = False,
) -> str:
    mode = (
        "An active Wiki has been copied into wiki/. Preserve useful existing knowledge and "
        "integrate the new material into it."
        if existing
        else "The Wiki is empty. Build its first coherent version."
    )
    listed_lines = []
    for item in source_items:
        detail = "image" if item["kind"] == "image" else "text"
        instruction = item.get("instruction", "").strip()
        suffix = f"; user instruction: {instruction}" if instruction else ""
        listed_lines.append(f"- sources/{item['name']} ({detail}{suffix})")
    listed = "\n".join(listed_lines)
    packet_context = (
        "The NEW sources are the currently processable parts of one user Packet. Understand "
        "them together; the user's note describes why the Packet was saved. Unsupported Packet "
        "items are intentionally not supplied yet.\n\n"
        if packet
        else ""
    )
    return f"""You are the semantic administrator of a durable {language}-language Wiki.

{mode}

{packet_context}Read or inspect every NEW source listed below as one semantically coherent input.
Do not reduce it to independent 2,000-character fragments. Understand themes, arguments,
decisions, changes of view, open questions, contradictions, and relations to the existing Wiki.

NEW SOURCES:
{listed}

The retrieval layer supplied only relevant existing pages ({", ".join(selected_pages or [])}).
Other active pages intentionally are not present and will be preserved by deterministic merge.
You may read any supplied file under wiki/ and sources/. Edit only wiki/, proposal-report.md, and
proposal-actions.json.
Never edit sources/. Do not use the Internet or outside knowledge.

For image sources, inspect the actual pixels. Follow any per-image user instruction. OCR or visual
descriptions are derived analysis, not verbatim text evidence: preserve uncertainty when text or
details are illegible. Image citations link directly to the image and never claim line ranges.

The result is not one summary per source and not a claim database. Prefer a small set of durable
pages. Update an existing page when the subject already exists. Create a page only for a
long-lived topic worth revisiting. Choose the page shape from the user's intent and the material:

- personal collections, watchlists, wishlists, and recommendations are compact accumulating
  lists or tables; add one item, not an essay about how it was captured;
- readings, measurements, payments, and other repeated observations are stable chronological
  tables; preserve the visible date, object, value, unit, and source without surrounding prose;
- small reference facts use short fields or bullets;
- explanatory sources may become connected prose when their actual content warrants it.

The user's requested shape (for example "make a list" or "keep one table") overrides a generic
article shape. Do not narrate that the user saved a Packet, omitted a deadline, did not name a
platform, or supplied a screenshot. Do not add boilerplate open questions. Do not turn filenames,
interface chrome, or capture dates into hypotheses unless the user asks. If a visually recognized
name or value is uncertain, mark that specific cell or bullet briefly as uncertain; do not write
paragraphs defending the uncertainty. A simple input should normally cause a simple Wiki change.

Preserve disagreements and evolution when they are genuinely present. It is allowed to synthesize
meaning across passages:

- direct: state it normally and cite the supporting source;
- synthesis: mark a non-obvious combined conclusion with **Синтез:** and cite every input;
- hypothesis: mark uncertainty with **Гипотеза:**;
- user source: describe it as the user's statement, correction, preference, or instruction.

Markdown contract:

- wiki/README.md is a concise landing page, not a global mega-summary;
- all other filenames are top-level ASCII kebab-case.md with a natural-language H1;
- use the smallest readable structure: list/table/fields or connected prose as appropriate, plus
  [[page-slug]] links where they are useful;
- cite important statements inline as [S1], [S2], etc.;
- every thematic page ends with ## Источники and defines all labels;
- definitions link into ../sources/ with a useful locator, preferably line ranges;
- image sources may also be embedded as ![description](../sources/name); their source definition
  uses a direct link without a line locator;
- never fabricate quotes, dates, attachment contents, or source locators.

Before finishing, reread the whole Wiki, merge duplicates, resolve every Wiki link, verify every
citation and source path, and remove unsupported additions. Keep proposal-report.md under 300 words
and describe only:
sources read; pages created, changed, or removed; major synthesis decisions; conflicts and
uncertainties; ignored low-value material; and self-check results. Finish the files—do not merely
propose a structure. Write proposal-actions.json as strict JSON with an "actions" array. Each item
has action "upsert", "delete", or "rename", path, and destination only for rename. List every
created/changed page as upsert; deletions and renames happen only through this explicit manifest.
Manifest path and destination values are filenames relative to wiki/, for example "README.md" or
"topic-name.md"—never include the "wiki/" prefix or another directory.
"""


def revise_prompt(feedback: str) -> str:
    return f"""Revise the pending Wiki proposal in this directory according to the administrator's
feedback below. Read the current wiki/, proposal-report.md, and relevant sources/. Preserve
correct work that the feedback does not challenge. Edit only wiki/ and proposal-report.md.

ADMINISTRATOR FEEDBACK:
{feedback.strip()}

Keep the original Wiki contract: coherent topic pages, working [[links]], inline citations, and a
final ## Источники section on each thematic page. Follow a requested literal structure exactly.
Lists, logs, and tables must stay compact; remove meta-commentary, capture narration, boilerplate
questions, and unnecessary hypotheses. Update proposal-report.md with what changed in this
revision and run a complete self-check. Keep the report under 300 words. Do not just explain what
you would change.
"""


def query_prompt(question: str, *, context: str = "") -> str:
    history = (
        f"\nRECENT CHAT CONTEXT (context for the question, never evidence):\n{context}\n"
        if context
        else ""
    )
    return f"""Answer the user's question using only the retrieval-selected Wiki pages in wiki/.
Open supplied files under sources/ when a claim needs verification. Absence of other pages is
intentional; never search outside this directory or use outside knowledge.{history}

QUESTION:
{question.strip()}

Write the complete answer to answer.md. Use natural prose, distinguish direct knowledge from
synthesis or uncertainty, cite the Wiki pages used as [[page-slug]], and mention raw source paths
only if you opened them. End with a short "Путь" section listing Wiki pages visited and raw sources
opened. Do not edit any other file.
"""
