from __future__ import annotations

from pathlib import Path

import pytest

from memorex.config import WorkspaceSettings
from memorex.wiki_first.models import AgentRunner, RunnerResult
from memorex.wiki_first.runners import _parse_usage
from memorex.wiki_first.service import WikiFirstService
from memorex.wiki_first.validation import validate_wiki


class FakeWikiRunner(AgentRunner):
    def __init__(self, name: str = "fake"):
        self.name = name
        self.model = "strong-fake"
        self.calls: list[str] = []

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        self.calls.append(prompt)
        if "Write the complete answer" in prompt:
            (workdir / "answer.md").write_text(
                "Решение описано на странице [[project-direction]].\n\n"
                "## Путь\n\n- [[project-direction]]\n",
                encoding="utf-8",
            )
        else:
            sources = sorted((workdir / "sources").glob("*"))
            source = sources[-1]
            page = workdir / "wiki" / "project-direction.md"
            previous = page.read_text(encoding="utf-8") if page.exists() else ""
            revised = "Revision feedback" in prompt
            extra = "\nТекст уточнён администратором. [S1]\n" if revised else ""
            page.write_text(
                "# Направление проекта\n\n"
                "Команда выбрала Wiki-first эксперимент. [S1]\n"
                f"{extra}\n"
                "Связанная тема: [[project-direction]].\n\n"
                "## Источники\n\n"
                f"- [S1] [{source.name}](../sources/{source.name}), строки 1-2.\n",
                encoding="utf-8",
            )
            (workdir / "wiki" / "README.md").write_text(
                "# Wiki\n\n- [[project-direction]] — выбранное направление.\n",
                encoding="utf-8",
            )
            (workdir / "proposal-report.md").write_text(
                f"# Report\n\nUpdated project direction. Previous bytes: {len(previous)}.\n",
                encoding="utf-8",
            )
        return RunnerResult(
            runner=self.name,
            model=self.model,
            command_version="fake 1",
            duration_ms=7,
            stdout="ok",
            stderr="",
        )


def make_service(tmp_path: Path) -> tuple[WikiFirstService, WorkspaceSettings, FakeWikiRunner]:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test Wiki")
    runner = FakeWikiRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    return service, settings, runner


def test_ingest_review_revise_apply_query_and_idempotence(tmp_path: Path) -> None:
    service, settings, runner = make_service(tmp_path)
    source = settings.inbox_dir / "discussion.txt"
    source.write_text("Решили проверить Wiki-first.\nЭто важный эксперимент.\n", encoding="utf-8")

    proposed = service.ingest()

    assert proposed["status"] == "proposed"
    job_id = str(proposed["job_id"])
    review = service.review(job_id)
    assert "project-direction.md" in review["diff"]
    assert review["validation"]["valid"] is True

    revised = service.revise(job_id, "Revision feedback: make the decision explicit")
    assert revised["revision"] == 2
    applied = service.apply(job_id)

    assert applied["status"] == "applied"
    wiki_path = Path(str(applied["wiki_path"]))
    assert "уточнён" in (wiki_path / "project-direction.md").read_text(encoding="utf-8")
    assert service.ingest()["status"] == "unchanged"
    assert service.status()["integrity"] == "ok"

    answer = service.ask("Какое направление выбрала команда?")
    assert "[[project-direction]]" in answer["answer"]
    assert len(runner.calls) == 3


def test_changed_source_creates_new_snapshot_and_rollback(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    source = settings.inbox_dir / "notes.md"
    source.write_text("Первая версия.\nВторая строка.\n", encoding="utf-8")
    first = service.ingest()
    first_snapshot = str(service.apply(str(first["job_id"]))["snapshot"])

    source.write_text("Изменённая версия.\nВторая строка.\n", encoding="utf-8")
    second = service.ingest()
    second_snapshot = str(service.apply(str(second["job_id"]))["snapshot"])

    assert first_snapshot != second_snapshot
    assert len(service.history()) == 3
    rolled_back = service.rollback(first_snapshot)
    assert rolled_back["snapshot"] == first_snapshot


def test_tell_is_a_reviewable_source_and_rejection_does_not_activate(tmp_path: Path) -> None:
    service, _settings, _runner = make_service(tmp_path)

    proposed = service.tell("Я считаю эту идею приоритетной.")
    before = service.status()["active_snapshot"]
    rejected = service.reject(str(proposed["job_id"]), "too broad")

    assert rejected["status"] == "rejected"
    assert service.status()["active_snapshot"] == before
    assert service.status()["pending_sources"] == 1


def test_validator_rejects_unresolved_links_and_missing_citations(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    sources = tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (wiki / "README.md").write_text("# Wiki\n", encoding="utf-8")
    (wiki / "broken.md").write_text(
        "# Broken\n\nUnsupported [S1] and [[missing]].\n\n## Источники\n",
        encoding="utf-8",
    )

    result = validate_wiki(wiki, sources)

    assert result.valid is False
    assert any("unresolved Wiki link" in error for error in result.errors)
    assert any("undefined citations" in error for error in result.errors)


def test_validator_checks_reference_style_source_links_and_line_fragments(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki"
    sources = tmp_path / "sources"
    wiki.mkdir()
    sources.mkdir()
    (sources / "note.txt").write_text("one\ntwo\n", encoding="utf-8")
    (wiki / "README.md").write_text("# Wiki\n", encoding="utf-8")
    (wiki / "topic.md").write_text(
        '# Topic\n\nKnown. [S1]\n\n## Источники\n\n[S1]: ../sources/note.txt#L1-L2 "Support"\n',
        encoding="utf-8",
    )

    assert validate_wiki(wiki, sources).valid is True


def test_active_snapshot_detects_external_modification(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    (settings.inbox_dir / "notes.txt").write_text("Материал.\nЕщё строка.\n", encoding="utf-8")
    proposal = service.ingest()
    applied = service.apply(str(proposal["job_id"]))
    page = Path(str(applied["wiki_path"])) / "README.md"
    page.chmod(0o644)
    page.write_text("tampered", encoding="utf-8")

    assert service.status()["integrity"] == "modified"
    with pytest.raises(ValueError, match="modified outside Memorex"):
        service.ask("Что известно?")


def test_next_ingest_recovers_job_interrupted_in_a_previous_process(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    source = settings.inbox_dir / "notes.txt"
    source.write_text("Незавершённый запуск.\nЕщё строка.\n", encoding="utf-8")
    service.storage.initialize()
    revision = service.storage.register_source(source)
    abandoned = service.storage.create_job(
        "abandoned",
        kind="ingest",
        runner="claude",
        source_revision_ids=[int(revision["id"])],
    )

    assert abandoned["status"] == "running"
    proposed = service.ingest()

    assert proposed["status"] == "proposed"
    assert service.storage.get_job("abandoned")["status"] == "failed"


def test_unknown_runtime_runner_is_rejected_before_creating_a_job(tmp_path: Path) -> None:
    service, settings, _runner = make_service(tmp_path)
    (settings.inbox_dir / "notes.txt").write_text("Текст.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="choose claude or codex"):
        service.ingest(runner_name="small-model")

    assert service.status()["proposal"] is None


def test_cli_telemetry_is_schema_validated_before_token_logging() -> None:
    claude = _parse_usage(
        "claude",
        '{"result":"done","usage":{"input_tokens":120,"output_tokens":40,'
        '"cache_read_input_tokens":80}}',
    )
    codex = _parse_usage(
        "codex",
        '{"type":"item.completed"}\n'
        '{"type":"turn.completed","usage":{"input_tokens":200,'
        '"cached_input_tokens":50,"output_tokens":60}}',
    )

    assert claude == {"input_tokens": 120, "output_tokens": 40, "cached_input_tokens": 80}
    assert codex == {"input_tokens": 200, "output_tokens": 60, "cached_input_tokens": 50}
