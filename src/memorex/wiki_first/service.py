from __future__ import annotations

import difflib
import json
import re
import shutil
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from threading import Event

from pydantic import ValidationError

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import AgentRunner, ProposalActions, RunnerResult
from memorex.wiki_first.prompts import (
    INGEST_PROMPT_VERSION,
    QUERY_PROMPT_VERSION,
    REVISE_PROMPT_VERSION,
    ingest_prompt,
    query_prompt,
    revise_prompt,
)
from memorex.wiki_first.runners import RunnerCancelled, RunnerError, configured_runner
from memorex.wiki_first.storage import WikiStorage, hash_tree
from memorex.wiki_first.validation import validate_wiki


class WikiFirstError(ValueError):
    pass


RunnerResolver = Callable[[str], AgentRunner]
ProgressCallback = Callable[[dict[str, object]], None]
SOURCE_REF = re.compile(r"\.\./sources/([^\s)#]+)")
WORD = re.compile(r"[^\W_]{4,}", re.UNICODE)
SAFE_PAGE = re.compile(r"(?:README|[a-z0-9]+(?:-[a-z0-9]+)*)\.md")


class WikiFirstService:
    def __init__(
        self,
        settings: WorkspaceSettings,
        *,
        runner_resolver: RunnerResolver | None = None,
        progress: ProgressCallback | None = None,
        cancel_event: Event | None = None,
        task_id: str | None = None,
    ):
        self.settings = settings
        self.storage = WikiStorage(settings)
        self._runner_resolver = runner_resolver or (
            lambda name: configured_runner(settings.wiki, name)
        )
        self._progress, self._cancel_event, self._task_id = progress, cancel_event, task_id
        self._job_id: str | None = None
        self._started = time.monotonic()

    def _emit(self, phase: str, **payload: object) -> None:
        event = {
            "phase": phase,
            "elapsed_ms": int((time.monotonic() - self._started) * 1000),
            **payload,
        }
        if self._progress:
            self._progress(event)
        if self._task_id:
            self.storage.add_task_event(self._task_id, phase, event, self._job_id)

    def initialize(self) -> dict[str, object]:
        return self.storage.initialize()

    def scan(self) -> list[dict[str, object]]:
        self.storage.initialize()
        return [
            self.storage.register_source(path)
            for path in sorted(self.settings.inbox_dir.rglob("*"))
            if path.is_file() and path.suffix.lower() in {".md", ".txt"}
        ]

    def ingest(self, *, runner_name: str | None = None) -> dict[str, object]:
        self.storage.initialize()
        self.storage.recover_interrupted_jobs()
        self.storage.verify_active()
        runner = runner_name or self.settings.wiki.ingest_runner
        _validate_runner_name(runner)
        self._emit("scan")
        scanned, pending = self.scan(), self.storage.pending_revisions()
        if not pending:
            return {"status": "unchanged", "scanned": scanned, "pending_sources": 0}
        return self._create_proposal("ingest", pending, runner, None)

    def tell(self, text: str, *, runner_name: str | None = None) -> dict[str, object]:
        self.storage.initialize()
        self.storage.recover_interrupted_jobs()
        self.storage.verify_active()
        runner = runner_name or self.settings.wiki.ingest_runner
        _validate_runner_name(runner)
        revision = self.storage.register_user_text(text, "note")
        return self._create_proposal(
            "tell", [self.storage.source_revision(int(revision["id"]))], runner, text
        )

    def review(self, job_id: str) -> dict[str, object]:
        proposal = self.storage.proposal(job_id)
        stage = self.storage.root / str(proposal["relative_path"])
        base = self.storage.snapshot_path(self._snapshot(str(proposal["base_snapshot_id"])))
        report = stage / "proposal-report.md"
        return {
            "job_id": job_id,
            "revision": proposal["revision_no"],
            "runner": proposal["runner"],
            "report": report.read_text(encoding="utf-8") if report.is_file() else "",
            "validation": json.loads(str(proposal["validation_json"])),
            "retrieval": json.loads(str(proposal.get("retrieval_json") or "{}")),
            "changed_pages": changed_pages(base / "wiki", stage / "wiki"),
            "diff": directory_diff(base / "wiki", stage / "wiki"),
        }

    def revise(
        self, job_id: str, feedback: str, *, runner_name: str | None = None
    ) -> dict[str, object]:
        if not feedback.strip():
            raise WikiFirstError("Revision feedback must not be empty")
        proposal = self.storage.proposal(job_id)
        if proposal["status"] != "proposed":
            raise WikiFirstError(f"Proposal {job_id} is not awaiting review")
        if runner_name is not None:
            _validate_runner_name(runner_name)
        runner_name = runner_name or str(proposal["runner"])
        old = self.storage.root / str(proposal["relative_path"])
        revision = int(proposal["revision_no"]) + 1
        stage = self.storage.jobs_dir / job_id / f"rev-{revision}"
        selected = self._select_local(feedback, old / "wiki", 8)
        base_snapshot = self.storage.snapshot_path(
            self._snapshot(str(proposal["base_snapshot_id"]))
        )
        for name in changed_pages(base_snapshot / "wiki", old / "wiki"):
            slug = Path(name).stem
            if slug not in selected and len(selected) < 12:
                selected.append(slug)
        self._prepare_subset(stage, old, selected, [])
        report = old / "proposal-report.md"
        if report.is_file():
            shutil.copy2(report, stage / "proposal-report.md")
        retrieval = self._retrieval(selected, old / "wiki")
        self._emit("retrieval", **retrieval)
        self.storage.set_job_status(job_id, "running")
        try:
            result = self._run_and_log(
                self._runner_resolver(runner_name),
                stage,
                revise_prompt(feedback),
                True,
                job_id,
                "revise",
                REVISE_PROMPT_VERSION,
            )
        except Exception:
            self.storage.set_job_status(job_id, "proposed")
            raise
        self._merge_stage(old, stage)
        validation = self._validate_stage(stage, proposal)
        if not validation["valid"]:
            raise WikiFirstError("Revised proposal is invalid: " + "; ".join(validation["errors"]))
        self.storage.add_proposal_revision(
            job_id,
            revision_no=revision,
            relative_path=str(stage.relative_to(self.storage.root)),
            tree_hash=hash_tree(stage),
            validation_json=json.dumps(validation, ensure_ascii=False),
            feedback=feedback,
            retrieval_json=json.dumps(retrieval, ensure_ascii=False),
        )
        return self._proposal_result(job_id, result)

    def apply(self, job_id: str) -> dict[str, object]:
        self.storage.verify_active()
        proposal = self.storage.proposal(job_id)
        if proposal["status"] != "proposed":
            raise WikiFirstError(f"Proposal {job_id} is not awaiting review")
        stage = self.storage.root / str(proposal["relative_path"])
        validation = self._validate_stage(stage, proposal)
        if not validation["valid"]:
            raise WikiFirstError("Proposal is invalid: " + "; ".join(validation["errors"]))
        snapshot_id = uuid.uuid4().hex[:12]
        target = self.storage.snapshots_dir / snapshot_id
        target.mkdir(parents=True)
        shutil.copytree(stage / "wiki", target / "wiki")
        shutil.copytree(stage / "sources", target / "sources")
        _make_read_only(target)
        self.storage.activate(
            job_id, snapshot_id, str(target.relative_to(self.storage.root)), hash_tree(target)
        )
        self.storage.rebuild_fts()
        vault = self.storage.sync_vault()
        return {
            "status": "applied",
            "job_id": job_id,
            "snapshot": snapshot_id,
            "wiki_path": str(target / "wiki"),
            "vault_path": str(vault),
            "validation": validation,
        }

    def reject(self, job_id: str, reason: str) -> dict[str, object]:
        self.storage.reject(job_id, reason.strip() or "rejected by administrator")
        return {"status": "rejected", "job_id": job_id, "reason": reason}

    def retry(self, job_id: str) -> dict[str, object]:
        job = self.storage.get_job(job_id)
        if job["status"] not in {"failed", "interrupted", "cancelled", "rejected"}:
            raise WikiFirstError("Job is not retryable")
        pending = [self.storage.source_revision(item) for item in job["source_revision_ids"]]
        return self._create_proposal(
            str(job["kind"]), pending, str(job["runner"]), job.get("user_input")
        )

    def ask(
        self, question: str, *, runner_name: str | None = None, session_id: str | None = None
    ) -> dict[str, object]:
        if not question.strip():
            raise WikiFirstError("Question must not be empty")
        runner_name = runner_name or self.settings.wiki.query_runner
        _validate_runner_name(runner_name)
        self.storage.initialize()
        self.storage.verify_active()
        snapshot = self.storage.active_snapshot()
        history = self.storage.chat_messages(session_id, 10) if session_id else []
        if session_id:
            self.storage.add_chat_message(
                session_id, "user", question, snapshot_id=str(snapshot["id"])
            )
        selected = self.storage.search_pages(question, limit=6, related_limit=4)
        if not selected:
            answer = "В активной Wiki недостаточно знаний для ответа на этот вопрос."
            record = self.storage.record_answer(
                snapshot_id=str(snapshot["id"]),
                question=question,
                answer=answer,
                runner="none",
                model="none",
            )
            if session_id:
                self.storage.add_chat_message(
                    session_id, "assistant", answer, snapshot_id=str(snapshot["id"])
                )
            return {
                "id": record,
                "snapshot": snapshot["id"],
                "answer": answer,
                "runner": "none",
                "model": "none",
                "selected_pages": [],
            }
        source = self.storage.snapshot_path(snapshot)
        workdir = self.storage.answers_dir / uuid.uuid4().hex[:12]
        self._prepare_subset(workdir, source, selected, [])
        _make_read_only(workdir / "wiki")
        _make_read_only(workdir / "sources")
        self._emit("retrieval", **self._retrieval(selected, source / "wiki"))
        result = self._run_and_log(
            self._runner_resolver(runner_name),
            workdir,
            query_prompt(question, context=_chat_context(history)),
            True,
            None,
            "query",
            QUERY_PROMPT_VERSION,
        )
        answer_path = workdir / "answer.md"
        if not answer_path.is_file() or not answer_path.read_text(encoding="utf-8").strip():
            raise WikiFirstError("Query runner did not create answer.md")
        answer = answer_path.read_text(encoding="utf-8").strip()
        record = self.storage.record_answer(
            snapshot_id=str(snapshot["id"]),
            question=question,
            answer=answer,
            runner=result.runner,
            model=result.model,
        )
        if session_id:
            self.storage.add_chat_message(
                session_id,
                "assistant",
                answer,
                snapshot_id=str(snapshot["id"]),
                runner=result.runner,
                model=result.model,
            )
        return {
            "id": record,
            "snapshot": snapshot["id"],
            "answer": answer,
            "runner": result.runner,
            "model": result.model,
            "selected_pages": selected,
        }

    def validate(self, job_id: str | None = None) -> dict[str, object]:
        self.storage.initialize()
        if job_id:
            proposal = self.storage.proposal(job_id)
            return self._validate_stage(
                self.storage.root / str(proposal["relative_path"]), proposal
            )
        self.storage.verify_active()
        root = self.storage.snapshot_path(self.storage.active_snapshot())
        return validate_wiki(root / "wiki", root / "sources").as_dict()

    def history(self) -> list[dict[str, object]]:
        self.storage.initialize()
        return self.storage.history()

    def rollback(self, snapshot_id: str) -> dict[str, object]:
        self.storage.initialize()
        self.storage.rollback(snapshot_id)
        self.storage.rebuild_fts()
        vault = self.storage.sync_vault()
        snapshot = self.storage.active_snapshot()
        return {
            "status": "rolled_back",
            "snapshot": snapshot["id"],
            "wiki_path": str(self.storage.snapshot_path(snapshot) / "wiki"),
            "vault_path": str(vault),
        }

    def status(self) -> dict[str, object]:
        self.storage.initialize()
        scanned = self.scan()
        status = self.storage.status()
        snapshot = self.storage.active_snapshot()
        status.update(
            {
                "wiki_path": str(self.storage.snapshot_path(snapshot) / "wiki"),
                "vault_path": str(self.settings.root / "vault"),
                "scanned_files": len(scanned),
            }
        )
        try:
            self.storage.verify_active()
            status["integrity"] = "ok"
        except ValueError:
            status["integrity"] = "modified"
        return status

    def _create_proposal(
        self, kind: str, pending: list[dict[str, object]], runner_name: str, user_input: str | None
    ) -> dict[str, object]:
        job_id = uuid.uuid4().hex[:12]
        self._job_id = job_id
        job = self.storage.create_job(
            job_id,
            kind=kind,
            runner=runner_name,
            source_revision_ids=[int(x["id"]) for x in pending],
            user_input=user_input,
        )
        base = self.storage.snapshot_path(self._snapshot(str(job["base_snapshot_id"])))
        selected = self.storage.search_pages(self._source_terms(pending), limit=8, related_limit=4)
        if "README" not in selected:
            selected.insert(0, "README")
        retrieval = self._retrieval(selected, base / "wiki")
        self._emit("retrieval", sources=len(pending), **retrieval)
        stage = self.storage.jobs_dir / job_id / "rev-1"
        names = self._prepare_subset(stage, base, selected, pending)
        prompt = ingest_prompt(
            language=self.settings.language,
            source_names=names,
            existing=str(job["base_snapshot_id"]) != "initial",
            selected_pages=selected,
        )
        result = None
        validation = None
        last_error: Exception | None = None
        for candidate in (runner_name, "codex" if runner_name == "claude" else "claude"):
            try:
                if candidate != runner_name:
                    self._emit("fallback", fallback=candidate)
                    _make_writable(stage)
                    shutil.rmtree(stage)
                    self._prepare_subset(stage, base, selected, pending)
                result = self._run_and_log(
                    self._runner_resolver(candidate),
                    stage,
                    prompt,
                    True,
                    job_id,
                    "ingest",
                    INGEST_PROMPT_VERSION,
                )
                self._merge_stage(base, stage)
                validation = self._validate_stage(stage, job)
                if validation["valid"]:
                    break
                last_error = WikiFirstError("; ".join(validation["errors"]))
            except RunnerCancelled as exc:
                self.storage.cancel_job(job_id)
                raise WikiFirstError(str(exc)) from exc
            except (RunnerError, WikiFirstError, OSError) as exc:
                last_error = exc
        if result is None or validation is None or not validation["valid"]:
            self.storage.fail_job(job_id, str(last_error or "proposal failed"))
            raise WikiFirstError(
                f"Both Wiki runners failed for job {job_id}: {last_error or 'invalid proposal'}"
            )
        self.storage.set_job_runner(job_id, result.runner)
        self.storage.add_proposal_revision(
            job_id,
            revision_no=1,
            relative_path=str(stage.relative_to(self.storage.root)),
            tree_hash=hash_tree(stage),
            validation_json=json.dumps(validation, ensure_ascii=False),
            feedback=None,
            retrieval_json=json.dumps(retrieval, ensure_ascii=False),
        )
        self._emit("review-ready", job_id=job_id, status="proposed")
        return self._proposal_result(job_id, result)

    def _source_terms(self, pending: list[dict[str, object]]) -> str:
        chunks = []
        for item in pending:
            chunks.append(Path(str(item["canonical_path"])).stem.replace("-", " "))
            text = (self.storage.root / str(item["normalized_path"])).read_text(encoding="utf-8")
            chunks += [line.lstrip("# ") for line in text.splitlines() if line.startswith("#")]
            chunks += [word for word, _ in Counter(WORD.findall(text.lower())).most_common(20)]
        return " ".join(chunks)

    def _select_local(self, query: str, wiki: Path, limit: int) -> list[str]:
        terms = set(WORD.findall(query.lower()))
        scored = []
        for page in wiki.glob("*.md"):
            scored.append(
                (
                    len(terms & set(WORD.findall(page.read_text(encoding="utf-8").lower()))),
                    page.stem,
                )
            )
        return [slug for score, slug in sorted(scored, reverse=True) if score][:limit] or ["README"]

    def _prepare_subset(
        self, stage: Path, base: Path, selected: list[str], pending: list[dict[str, object]]
    ) -> list[str]:
        (stage / "wiki").mkdir(parents=True)
        (stage / "sources").mkdir()
        cited = set()
        for slug in selected:
            page = base / "wiki" / f"{slug}.md"
            if page.is_file():
                shutil.copy2(page, stage / "wiki" / page.name)
                cited.update(SOURCE_REF.findall(page.read_text(encoding="utf-8")))
        for name in cited:
            if (base / "sources" / name).is_file():
                shutil.copy2(base / "sources" / name, stage / "sources" / name)
        names = []
        for item in pending:
            name = f"r{item['id']}-{_safe_source_name(Path(str(item['canonical_path'])).name)}"
            names.append(name)
            shutil.copy2(self.storage.root / str(item["normalized_path"]), stage / "sources" / name)
        _make_writable(stage / "wiki")
        _make_read_only(stage / "sources")
        return names

    def _merge_stage(self, base: Path, stage: Path) -> None:
        try:
            manifest = (
                ProposalActions.model_validate_json(
                    (stage / "proposal-actions.json").read_text(encoding="utf-8")
                )
                if (stage / "proposal-actions.json").is_file()
                else ProposalActions(
                    actions=[
                        {"action": "upsert", "path": p.name} for p in (stage / "wiki").glob("*.md")
                    ]
                )
            )
        except ValidationError as exc:
            raise WikiFirstError(f"Invalid proposal-actions.json: {exc}") from exc
        merged = stage.parent / f".{stage.name}-merged"
        shutil.copytree(base / "wiki", merged / "wiki")
        shutil.copytree(base / "sources", merged / "sources")
        _make_writable(merged)
        for source in (stage / "sources").glob("*"):
            if source.is_file():
                shutil.copy2(source, merged / "sources" / source.name)
        for action in manifest.actions:
            name = _safe_page(action.path)
            target = merged / "wiki" / name
            if action.action == "upsert":
                authored = stage / "wiki" / name
                if not authored.is_file():
                    raise WikiFirstError(f"Manifest upsert is missing file: {name}")
                shutil.copy2(authored, target)
            elif action.action == "delete":
                if target.exists():
                    target.unlink()
            else:
                destination = _safe_page(action.destination or "")
                if not target.is_file():
                    raise WikiFirstError(f"Manifest rename source is missing: {name}")
                target.rename(merged / "wiki" / destination)
        for extra in ("proposal-report.md", "proposal-actions.json"):
            if (stage / extra).is_file():
                shutil.copy2(stage / extra, merged / extra)
        _make_writable(stage)
        shutil.rmtree(stage)
        merged.rename(stage)

    def _retrieval(self, selected: list[str], wiki: Path) -> dict[str, object]:
        existing = [x for x in selected if (wiki / f"{x}.md").is_file()]
        return {
            "selected_pages": existing,
            "selected_count": len(existing),
            "total_pages": len(list(wiki.glob("*.md"))),
        }

    def _validate_stage(self, stage: Path, proposal: dict[str, object]) -> dict[str, object]:
        base = self.storage.snapshot_path(self._snapshot(str(proposal["base_snapshot_id"])))
        result = validate_wiki(stage / "wiki", stage / "sources", base / "wiki")
        if not (stage / "proposal-report.md").is_file():
            result.errors.append("proposal-report.md is required")
        return result.as_dict()

    def _run_and_log(
        self,
        runner: AgentRunner,
        workdir: Path,
        prompt: str,
        writable: bool,
        job_id: str | None,
        purpose: str,
        prompt_version: str,
    ) -> RunnerResult:
        self._emit("runner", runner=runner.name, model=runner.model)
        try:
            result = runner.run_with_progress(
                workdir,
                prompt,
                writable=writable,
                progress=lambda e: self._emit(
                    str(e.get("phase", "runner")), **{k: v for k, v in e.items() if k != "phase"}
                ),
                cancel_event=self._cancel_event,
            )
        except RunnerError as exc:
            if isinstance(exc, RunnerCancelled):
                self._emit("cancelled", status="cancelled")
            failed = exc.result
            self.storage.record_call(
                {
                    "job_id": job_id,
                    "purpose": purpose,
                    "runner": failed.runner if failed else runner.name,
                    "model": failed.model if failed else runner.model,
                    "command_version": failed.command_version if failed else None,
                    "prompt_version": prompt_version,
                    "duration_ms": failed.duration_ms if failed else 0,
                    "status": "cancelled" if isinstance(exc, RunnerCancelled) else "failed",
                    "stdout": failed.stdout if failed else "",
                    "stderr": failed.stderr if failed else str(exc),
                    "input_tokens": failed.input_tokens if failed else None,
                    "output_tokens": failed.output_tokens if failed else None,
                    "cached_input_tokens": failed.cached_input_tokens if failed else None,
                }
            )
            raise
        self.storage.record_call(
            {
                "job_id": job_id,
                "purpose": purpose,
                "runner": result.runner,
                "model": result.model,
                "command_version": result.command_version,
                "prompt_version": prompt_version,
                "duration_ms": result.duration_ms,
                "status": "succeeded",
                "stdout": result.stdout,
                "stderr": result.stderr,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cached_input_tokens": result.cached_input_tokens,
            }
        )
        return result

    def _proposal_result(self, job_id: str, result: RunnerResult) -> dict[str, object]:
        review = self.review(job_id)
        return {
            "status": "proposed",
            "job_id": job_id,
            "revision": review["revision"],
            "runner": result.runner,
            "model": result.model,
            "duration_ms": result.duration_ms,
            "validation": review["validation"],
            "report": review["report"],
            "retrieval": review["retrieval"],
        }

    def _snapshot(self, snapshot_id: str) -> dict[str, object]:
        with self.storage.connection() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise WikiFirstError(f"Unknown snapshot: {snapshot_id}")
        return dict(row)


def _chat_context(messages: list[dict[str, object]]) -> str:
    return "\n".join(f"{x['role']}: {x['content']}" for x in messages[-10:])[-16000:]


def _safe_page(value: str) -> str:
    name = Path(value).name
    if value != name or not SAFE_PAGE.fullmatch(name):
        raise WikiFirstError(f"Unsafe Wiki page path: {value}")
    return name


def directory_diff(before: Path, after: Path) -> str:
    paths = sorted(
        {p.relative_to(before) for p in before.rglob("*.md")}
        | {p.relative_to(after) for p in after.rglob("*.md")}
    )
    chunks = []
    for relative in paths:
        old = (
            (before / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            if (before / relative).is_file()
            else []
        )
        new = (
            (after / relative).read_text(encoding="utf-8").splitlines(keepends=True)
            if (after / relative).is_file()
            else []
        )
        chunks.extend(
            difflib.unified_diff(old, new, fromfile=f"a/{relative}", tofile=f"b/{relative}")
        )
    return "".join(chunks)


def changed_pages(before: Path, after: Path) -> list[str]:
    result = []
    for name in sorted(
        {p.name for p in before.glob("*.md")} | {p.name for p in after.glob("*.md")}
    ):
        old = (before / name).read_bytes() if (before / name).is_file() else None
        new = (after / name).read_bytes() if (after / name).is_file() else None
        if old != new:
            result.append(name)
    return result


def _safe_source_name(name: str) -> str:
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(name).stem).strip("-.") or "source"
    return f"{stem}{suffix if suffix in {'.md', '.txt'} else '.txt'}"


def _validate_runner_name(name: str) -> None:
    if name not in {"claude", "codex"}:
        raise WikiFirstError(f"Unsupported Wiki runner: {name}; choose claude or codex")


def _make_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _make_writable(root: Path) -> None:
    if not root.exists():
        return
    root.chmod(0o755)
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
