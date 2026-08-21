from __future__ import annotations

import difflib
import json
import re
import shutil
import uuid
from collections.abc import Callable
from pathlib import Path

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import AgentRunner, RunnerResult
from memorex.wiki_first.prompts import (
    INGEST_PROMPT_VERSION,
    QUERY_PROMPT_VERSION,
    REVISE_PROMPT_VERSION,
    ingest_prompt,
    query_prompt,
    revise_prompt,
)
from memorex.wiki_first.runners import RunnerError, configured_runner
from memorex.wiki_first.storage import WikiStorage, hash_tree
from memorex.wiki_first.validation import validate_wiki


class WikiFirstError(ValueError):
    """Raised when a Wiki-first workflow cannot continue safely."""


RunnerResolver = Callable[[str], AgentRunner]


class WikiFirstService:
    def __init__(
        self,
        settings: WorkspaceSettings,
        *,
        runner_resolver: RunnerResolver | None = None,
    ):
        self.settings = settings
        self.storage = WikiStorage(settings)
        self._runner_resolver = runner_resolver or (
            lambda name: configured_runner(self.settings.wiki, name)
        )

    def initialize(self) -> dict[str, object]:
        return self.storage.initialize()

    def scan(self) -> list[dict[str, object]]:
        self.storage.initialize()
        results: list[dict[str, object]] = []
        for path in sorted(self.settings.inbox_dir.rglob("*")):
            if path.is_file() and path.suffix.lower() in {".md", ".txt"}:
                results.append(self.storage.register_source(path))
        return results

    def ingest(self, *, runner_name: str | None = None) -> dict[str, object]:
        self.storage.initialize()
        self.storage.recover_interrupted_jobs()
        self.storage.verify_active()
        selected_runner = runner_name or self.settings.wiki.ingest_runner
        _validate_runner_name(selected_runner)
        scanned = self.scan()
        pending = self.storage.pending_revisions()
        if not pending:
            return {"status": "unchanged", "scanned": scanned, "pending_sources": 0}
        return self._create_proposal(
            kind="ingest",
            pending=pending,
            runner_name=selected_runner,
            user_input=None,
        )

    def tell(self, text: str, *, runner_name: str | None = None) -> dict[str, object]:
        self.storage.initialize()
        self.storage.recover_interrupted_jobs()
        self.storage.verify_active()
        selected_runner = runner_name or self.settings.wiki.ingest_runner
        _validate_runner_name(selected_runner)
        revision = self.storage.register_user_text(text, "note")
        pending = [self.storage.source_revision(int(revision["id"]))]
        return self._create_proposal(
            kind="tell",
            pending=pending,
            runner_name=selected_runner,
            user_input=text,
        )

    def review(self, job_id: str) -> dict[str, object]:
        proposal = self.storage.proposal(job_id)
        stage = self.storage.root / str(proposal["relative_path"])
        base = self.storage.snapshot_path(self._snapshot(str(proposal["base_snapshot_id"])))
        report = stage / "proposal-report.md"
        validation = json.loads(str(proposal["validation_json"]))
        return {
            "job_id": job_id,
            "revision": proposal["revision_no"],
            "runner": proposal["runner"],
            "report": report.read_text(encoding="utf-8") if report.is_file() else "",
            "validation": validation,
            "diff": directory_diff(base / "wiki", stage / "wiki"),
        }

    def revise(
        self, job_id: str, feedback: str, *, runner_name: str | None = None
    ) -> dict[str, object]:
        if not feedback.strip():
            raise WikiFirstError("Revision feedback must not be empty")
        if runner_name is not None:
            _validate_runner_name(runner_name)
        proposal = self.storage.proposal(job_id)
        if proposal["status"] != "proposed":
            raise WikiFirstError(f"Proposal {job_id} is not awaiting review")
        old_stage = self.storage.root / str(proposal["relative_path"])
        revision_no = int(proposal["revision_no"]) + 1
        stage = self.storage.jobs_dir / job_id / f"rev-{revision_no}"
        shutil.copytree(old_stage, stage)
        runner = self._runner_resolver(runner_name or str(proposal["runner"]))
        result = self._run_and_log(
            runner,
            stage,
            revise_prompt(feedback),
            writable=True,
            job_id=job_id,
            purpose="revise",
            prompt_version=REVISE_PROMPT_VERSION,
        )
        validation = self._validate_stage(stage, proposal)
        if not validation["valid"]:
            raise WikiFirstError("Revised proposal is invalid: " + "; ".join(validation["errors"]))
        self.storage.add_proposal_revision(
            job_id,
            revision_no=revision_no,
            relative_path=str(stage.relative_to(self.storage.root)),
            tree_hash=hash_tree(stage),
            validation_json=json.dumps(validation, ensure_ascii=False),
            feedback=feedback,
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
        tree_hash = hash_tree(target)
        self.storage.activate(
            job_id,
            snapshot_id,
            str(target.relative_to(self.storage.root)),
            tree_hash,
        )
        return {
            "status": "applied",
            "job_id": job_id,
            "snapshot": snapshot_id,
            "wiki_path": str(target / "wiki"),
            "validation": validation,
        }

    def reject(self, job_id: str, reason: str) -> dict[str, object]:
        self.storage.reject(job_id, reason.strip() or "rejected by administrator")
        return {"status": "rejected", "job_id": job_id, "reason": reason}

    def ask(self, question: str, *, runner_name: str | None = None) -> dict[str, object]:
        if not question.strip():
            raise WikiFirstError("Question must not be empty")
        selected_runner = runner_name or self.settings.wiki.query_runner
        _validate_runner_name(selected_runner)
        self.storage.initialize()
        self.storage.verify_active()
        snapshot = self.storage.active_snapshot()
        answer_id = uuid.uuid4().hex[:12]
        workdir = self.storage.answers_dir / answer_id
        workdir.mkdir(parents=True)
        source = self.storage.snapshot_path(snapshot)
        shutil.copytree(source / "wiki", workdir / "wiki")
        shutil.copytree(source / "sources", workdir / "sources")
        _make_read_only(workdir / "wiki")
        _make_read_only(workdir / "sources")
        runner = self._runner_resolver(selected_runner)
        result = self._run_and_log(
            runner,
            workdir,
            query_prompt(question),
            writable=True,
            job_id=None,
            purpose="query",
            prompt_version=QUERY_PROMPT_VERSION,
        )
        answer_path = workdir / "answer.md"
        if not answer_path.is_file() or not answer_path.read_text(encoding="utf-8").strip():
            raise WikiFirstError("Query runner did not create answer.md")
        answer = answer_path.read_text(encoding="utf-8").strip()
        record_id = self.storage.record_answer(
            snapshot_id=str(snapshot["id"]),
            question=question,
            answer=answer,
            runner=result.runner,
            model=result.model,
        )
        return {
            "id": record_id,
            "snapshot": snapshot["id"],
            "answer": answer,
            "runner": result.runner,
            "model": result.model,
        }

    def validate(self, job_id: str | None = None) -> dict[str, object]:
        self.storage.initialize()
        if job_id:
            proposal = self.storage.proposal(job_id)
            stage = self.storage.root / str(proposal["relative_path"])
            return self._validate_stage(stage, proposal)
        self.storage.verify_active()
        snapshot = self.storage.active_snapshot()
        root = self.storage.snapshot_path(snapshot)
        return validate_wiki(root / "wiki", root / "sources").as_dict()

    def history(self) -> list[dict[str, object]]:
        self.storage.initialize()
        return self.storage.history()

    def rollback(self, snapshot_id: str) -> dict[str, object]:
        self.storage.initialize()
        self.storage.rollback(snapshot_id)
        snapshot = self.storage.active_snapshot()
        return {
            "status": "rolled_back",
            "snapshot": snapshot["id"],
            "wiki_path": str(self.storage.snapshot_path(snapshot) / "wiki"),
        }

    def status(self) -> dict[str, object]:
        self.storage.initialize()
        scanned = self.scan()
        status = self.storage.status()
        snapshot = self.storage.active_snapshot()
        status["wiki_path"] = str(self.storage.snapshot_path(snapshot) / "wiki")
        try:
            self.storage.verify_active()
            status["integrity"] = "ok"
        except ValueError:
            status["integrity"] = "modified"
        status["scanned_files"] = len(scanned)
        return status

    def _create_proposal(
        self,
        *,
        kind: str,
        pending: list[dict[str, object]],
        runner_name: str,
        user_input: str | None,
    ) -> dict[str, object]:
        job_id = uuid.uuid4().hex[:12]
        job = self.storage.create_job(
            job_id,
            kind=kind,
            runner=runner_name,
            source_revision_ids=[int(item["id"]) for item in pending],
            user_input=user_input,
        )
        stage = self.storage.jobs_dir / job_id / "rev-1"
        source_names = self._prepare_stage(stage, job, pending)
        prompt = ingest_prompt(
            language=self.settings.language,
            source_names=source_names,
            existing=str(job["base_snapshot_id"]) != "initial",
        )
        attempted: list[str] = []
        last_error: Exception | None = None
        result: RunnerResult | None = None
        validation: dict[str, object] | None = None
        fallback = "codex" if runner_name == "claude" else "claude"
        for candidate in (runner_name, fallback):
            if candidate in attempted:
                continue
            attempted.append(candidate)
            if candidate != runner_name:
                shutil.rmtree(stage)
                source_names = self._prepare_stage(stage, job, pending)
                prompt = ingest_prompt(
                    language=self.settings.language,
                    source_names=source_names,
                    existing=str(job["base_snapshot_id"]) != "initial",
                )
            try:
                runner = self._runner_resolver(candidate)
                result = self._run_and_log(
                    runner,
                    stage,
                    prompt,
                    writable=True,
                    job_id=job_id,
                    purpose="ingest",
                    prompt_version=INGEST_PROMPT_VERSION,
                )
                validation = self._validate_stage(stage, job)
                if validation["valid"]:
                    break
                last_error = WikiFirstError("; ".join(validation["errors"]))
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
        )
        return self._proposal_result(job_id, result)

    def _prepare_stage(
        self,
        stage: Path,
        job: dict[str, object],
        pending: list[dict[str, object]],
    ) -> list[str]:
        base = self._snapshot(str(job["base_snapshot_id"]))
        base_path = self.storage.snapshot_path(base)
        stage.mkdir(parents=True)
        shutil.copytree(base_path / "wiki", stage / "wiki")
        shutil.copytree(base_path / "sources", stage / "sources")
        _make_writable(stage / "wiki")
        _make_writable(stage / "sources")
        source_names: list[str] = []
        for revision in pending:
            original = Path(str(revision["canonical_path"]))
            safe = _safe_source_name(original.name)
            name = f"r{revision['id']}-{safe}"
            normalized = self.storage.root / str(revision["normalized_path"])
            shutil.copy2(normalized, stage / "sources" / name)
            (stage / "sources" / name).chmod(0o444)
            source_names.append(name)
        for source in (stage / "sources").rglob("*"):
            source.chmod(0o755 if source.is_dir() else 0o444)
        return source_names

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
        *,
        writable: bool,
        job_id: str | None,
        purpose: str,
        prompt_version: str,
    ) -> RunnerResult:
        try:
            result = runner.run(workdir, prompt, writable=writable)
        except RunnerError as exc:
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
                    "status": "failed",
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
        }

    def _snapshot(self, snapshot_id: str) -> dict[str, object]:
        with self.storage.connection() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise WikiFirstError(f"Unknown snapshot: {snapshot_id}")
        return dict(row)


def directory_diff(before: Path, after: Path) -> str:
    paths = sorted(
        {path.relative_to(before) for path in before.rglob("*.md")}
        | {path.relative_to(after) for path in after.rglob("*.md")}
    )
    chunks: list[str] = []
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


def _safe_source_name(name: str) -> str:
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", Path(name).stem).strip("-.") or "source"
    return f"{stem}{suffix if suffix in {'.md', '.txt'} else '.txt'}"


def _validate_runner_name(name: str) -> None:
    if name not in {"claude", "codex"}:
        raise WikiFirstError(f"Unsupported Wiki runner: {name}; choose claude or codex")


def _make_read_only(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    for path in sorted((item for item in root.rglob("*") if item.is_dir()), reverse=True):
        path.chmod(0o555)
    root.chmod(0o555)


def _make_writable(root: Path) -> None:
    root.chmod(0o755)
    for path in root.rglob("*"):
        path.chmod(0o755 if path.is_dir() else 0o644)
