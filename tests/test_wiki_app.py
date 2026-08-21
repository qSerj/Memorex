from __future__ import annotations

import asyncio
from pathlib import Path

import httpx2

from memorex.config import WorkspaceSettings
from memorex.wiki_app import create_app, render_markdown
from memorex.wiki_first.models import AgentRunner, RunnerResult
from memorex.wiki_first.service import WikiFirstService


class RetrievalRunner(AgentRunner):
    name = "fake"
    model = "fake-model"

    def __init__(self) -> None:
        self.visible_pages: list[set[str]] = []

    def run(self, workdir: Path, prompt: str, *, writable: bool) -> RunnerResult:
        pages = {path.name for path in (workdir / "wiki").glob("*.md")}
        self.visible_pages.append(pages)
        sources = sorted((workdir / "sources").glob("*"))
        newest = sources[-1]
        if len(self.visible_pages) == 1:
            (workdir / "wiki" / "alpha-topic.md").write_text(
                "# Alpha topic\n\nAlpha datum. [S1]\n\n## Источники\n\n"
                f"- [S1] [{newest.name}](../sources/{newest.name}), строка 1.\n",
                encoding="utf-8",
            )
            (workdir / "wiki" / "unrelated-topic.md").write_text(
                "# Unrelated topic\n\nUnrelated datum. [S1]\n\n## Источники\n\n"
                f"- [S1] [{newest.name}](../sources/{newest.name}), строка 1.\n",
                encoding="utf-8",
            )
            (workdir / "wiki" / "README.md").write_text(
                "# Wiki\n\n[[alpha-topic]].\n", encoding="utf-8"
            )
        else:
            page = workdir / "wiki" / "alpha-topic.md"
            page.write_text(
                "# Alpha topic\n\nAlpha updated. [S1]\n\n## Источники\n\n"
                f"- [S1] [{newest.name}](../sources/{newest.name}), строка 1.\n",
                encoding="utf-8",
            )
        (workdir / "proposal-report.md").write_text("# Report\n\nDone.\n", encoding="utf-8")
        return RunnerResult(self.name, self.model, "1", 1, "", "")


def test_web_setup_upload_last_workspace_and_safe_markdown(tmp_path: Path) -> None:
    preferences = tmp_path / "preferences.json"

    async def exercise() -> None:
        transport = httpx2.ASGITransport(app=create_app(None, user_settings_path=preferences))
        async with httpx2.AsyncClient(transport=transport, base_url="http://test") as client:
            assert "Создайте новый workspace" in (await client.get("/")).text
            created = await client.post(
                "/workspace",
                data={"path": str(tmp_path / "knowledge"), "action": "create", "name": "Test"},
            )
            assert created.status_code == 303
            uploaded = await client.post(
                "/upload", files={"files": ("note.md", b"# Note\n", "text/markdown")}
            )
            assert uploaded.status_code == 303
        remembered = create_app(None, user_settings_path=preferences)
        second = httpx2.ASGITransport(app=remembered)
        async with httpx2.AsyncClient(transport=second, base_url="http://test") as client:
            assert "Obsidian vault" in (await client.get("/")).text

    asyncio.run(exercise())
    rendered = render_markdown("# Safe\n\n<script>alert(1)</script> [[topic]]")
    assert "<script>" not in rendered
    assert "&lt;script&gt;" in rendered
    assert 'href="/wiki/topic"' in rendered


def test_retrieval_hides_irrelevant_pages_and_merge_preserves_them(tmp_path: Path) -> None:
    settings = WorkspaceSettings.create(tmp_path / "workspace", "Test")
    runner = RetrievalRunner()
    service = WikiFirstService(settings, runner_resolver=lambda _name: runner)
    first_source = settings.inbox_dir / "seed.txt"
    first_source.write_text("Alpha knowledge.\n", encoding="utf-8")
    first = service.ingest()
    service.apply(str(first["job_id"]))

    second_source = settings.inbox_dir / "alpha-update.txt"
    second_source.write_text("Alpha updated.\n", encoding="utf-8")
    second = service.ingest()

    assert "alpha-topic.md" in runner.visible_pages[-1]
    assert "unrelated-topic.md" not in runner.visible_pages[-1]
    applied = service.apply(str(second["job_id"]))
    wiki = Path(str(applied["wiki_path"]))
    assert (wiki / "unrelated-topic.md").is_file()
    vault = settings.root / "vault"
    assert (vault / "wiki" / "unrelated-topic.md").is_file()
    assert not (vault / "wiki" / "unrelated-topic.md").stat().st_mode & 0o200
