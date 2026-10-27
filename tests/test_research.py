import json
from pathlib import Path

import pytest

from flamingo_bot.config import Settings
from flamingo_bot.parsers import parse_sources
from flamingo_bot.sources import (
    SourceCatalog,
    SourceDefinition,
    SourceRule,
    content_revision,
    resolve_sources,
)


def research_source() -> SourceDefinition:
    return SourceDefinition(
        id="diaspora-zbarkon",
        label="Protest research",
        repository_setting="flamingo_research_dir",
        revision_key="protest-research",
        versioning="content_hash",
        optional=True,
        language="en",
        base_url="https://www.diaspora-zbarkon.com/pulsi/",
        rules=[SourceRule(parser="protest_research", include=["research/day_*_sources.json"])],
    )


def write_index(root: Path, *, day: int = 136) -> Path:
    path = root / "research/day_136_sources.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "day": day,
                "calendar_date": "2026-10-13",
                "research_cutoff": "2026-10-15",
                "local_analysis": {
                    "run_directory": "/private/operator/model-output",
                    "api_key": "SECRET",
                },
                "search_notes": ["Private research-agent instructions must not become evidence"],
                "sources": [
                    {
                        "id": "S1",
                        "outlet": "Example news",
                        "title": "Student protest report",
                        "url": "https://example.org/report",
                        "published_display": "2026-10-14",
                        "event_date": "2026-10-13",
                        "access": "body_opened",
                        "summary": (
                            "The report describes clashes and calls for "
                            "an inquiry into police force."
                        ),
                        "reporting_lineage": "Reporting, not completed responsibility findings.",
                    },
                    {
                        "id": "S2",
                        "outlet": "Unopened lead",
                        "title": "Unverified headline",
                        "url": "https://example.org/lead",
                        "access": "indexed_only_after_access_error",
                        "summary": "This unverified headline must not become established evidence.",
                    },
                    {
                        "outlet": "News24",
                        "title": "Video model input",
                        "url": "https://www.youtube.com/watch?v=video",
                        "use": "Local model input.",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    return path


def test_research_has_public_citations_event_dates_and_no_operator_data(tmp_path: Path) -> None:
    write_index(tmp_path)
    settings = Settings(_env_file=None, flamingo_research_dir=tmp_path)
    resolved = resolve_sources(settings, SourceCatalog(version=1, sources=[research_source()]))
    documents, issues, discovered = parse_sources(resolved)

    assert issues == []
    assert discovered == 1
    assert len(documents) == 1
    document = documents[0]
    assert document.canonical_url == "https://example.org/report"
    assert document.metadata["research_date"] == "2026-10-13"
    assert document.metadata["reported_event_date"] == "2026-10-13"
    assert document.metadata["research_cutoff"] == "2026-10-15"
    assert "Publication as recorded in research: 2026-10-14" in document.text
    assert "not completed responsibility findings" in document.text
    assert len(document.revision) == 64
    assert "SECRET" not in document.model_dump_json()
    assert "/private/operator" not in document.model_dump_json()
    assert "research-agent instructions" not in document.text
    assert "Unverified headline" not in document.text


def test_new_research_format_preserves_evidence_and_excludes_local_inputs(tmp_path: Path) -> None:
    from flamingo_bot.research import read_research_evidence

    index = write_index(tmp_path)
    data = json.loads(index.read_text())
    data["sources"].extend(
        [
            {
                "outlet": "Example report",
                "title": "A later public report",
                "url": "https://example.org/new-format",
                "publication_date_time": "2026-10-14 12:30",
                "event_date_scope": "The earlier protest on 2026-10-12; timing uncertain",
                "access_status": "opened_body",
                "supports": ["An attributed allegation.", "The official response."],
                "attribution_lineage_limitations": ["The inquiry is ongoing."],
            },
            {
                "outlet": "Local model output",
                "url": "outputs/protesta_136/scene_report.md",
                "access_status": "opened_body",
                "supports": ["PRIVATE LOCAL ANALYSIS"],
            },
            {
                "outlet": "Local site code",
                "url": "/home/example/project/data/participation.ts",
                "access_status": "opened_body",
                "supports": ["PRIVATE LOCAL SITE DATA"],
            },
            {
                "outlet": "Search lead",
                "url": "https://example.org/unopened",
                "access_status": "open_failed",
                "supports": ["UNVERIFIED SEARCH LEAD"],
            },
        ]
    )
    index.write_text(json.dumps(data))

    evidence = read_research_evidence(index)
    assert len(evidence) == 2
    original, added = evidence
    assert original.metadata["parser_format"] == "protest_research/2"
    assert added.metadata["parser_format"] == "protest_research/3"
    assert added.metadata["evidence_access"] == "body_read"
    assert added.metadata["reported_event_date"] == ""
    assert "2026-10-14 12:30" in added.text
    assert "2026-10-12; timing uncertain" in added.text
    assert "The official response." in added.text
    assert "The inquiry is ongoing." in added.text
    assert "PRIVATE LOCAL ANALYSIS" not in str(evidence)
    assert "UNVERIFIED SEARCH LEAD" not in str(evidence)


def test_new_research_format_rejects_unknown_invalid_urls(tmp_path: Path) -> None:
    from flamingo_bot.research import read_research_evidence

    index = write_index(tmp_path)
    data = json.loads(index.read_text())
    data["sources"].append(
        {"outlet": "Unknown", "url": "file:///private/notes", "supports": "Not public"}
    )
    index.write_text(json.dumps(data))

    with pytest.raises(ValueError, match="public HTTP"):
        read_research_evidence(index)


def test_research_versions_only_the_explicit_source_indexes(tmp_path: Path) -> None:
    index = write_index(tmp_path)
    definition = research_source()
    before = content_revision(tmp_path, definition)
    (tmp_path / "research/private-model-notes.md").write_text("Unrelated private notes")
    assert content_revision(tmp_path, definition) == before
    data = json.loads(index.read_text())
    data["sources"][0]["summary"] += " Later follow-up remains a separate event."
    index.write_text(json.dumps(data))
    assert content_revision(tmp_path, definition) != before


def test_followup_article_does_not_inherit_its_research_files_incident_date(tmp_path: Path) -> None:
    from flamingo_bot.research import read_research_evidence

    index = write_index(tmp_path)
    data = json.loads(index.read_text())
    data["sources"][0].pop("event_date")
    data["sources"][0]["summary"] = "Next-day reporting describes a separate student protest."
    index.write_text(json.dumps(data))

    evidence = read_research_evidence(index)[0]
    assert "2026-10-13" not in evidence.title
    assert "2026-10-13" not in evidence.text
    assert "Incident date: not specified" in evidence.text
    assert evidence.metadata["research_date"] == "2026-10-13"
    assert evidence.metadata["reported_event_date"] == ""


def test_unconfigured_optional_research_does_not_block_existing_ingestion() -> None:
    assert (
        resolve_sources(
            Settings(_env_file=None), SourceCatalog(version=1, sources=[research_source()])
        )
        == []
    )


def test_research_date_and_filename_must_agree(tmp_path: Path) -> None:
    write_index(tmp_path, day=137)
    resolved = resolve_sources(
        Settings(_env_file=None, flamingo_research_dir=tmp_path),
        SourceCatalog(version=1, sources=[research_source()]),
    )
    documents, issues, _ = parse_sources(resolved)
    assert documents == []
    assert "filename and protest day disagree" in issues[0].reason


def test_research_cannot_change_after_its_revision_is_resolved(tmp_path: Path) -> None:
    index = write_index(tmp_path)
    resolved = resolve_sources(
        Settings(_env_file=None, flamingo_research_dir=tmp_path),
        SourceCatalog(version=1, sources=[research_source()]),
    )
    index.write_text(index.read_text() + "\n")
    _, issues, _ = parse_sources(resolved)
    assert any("changed during parsing" in issue.reason for issue in issues)


def test_research_rejects_an_invalid_date(tmp_path: Path) -> None:
    from flamingo_bot.research import read_research_evidence

    index = write_index(tmp_path)
    data = json.loads(index.read_text())
    data["calendar_date"] = "2026-02-31"
    index.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_research_evidence(index)
