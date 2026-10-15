"""Regression and CLI UX tests for recent fixes.

Covers:
- pypdf internal warning suppression in extract_pdf_text
- scheduling executable resolution (standalone jobhub vs sibling vs python -m)
- sources catalog output surfacing missing credentials and targets
- resumes add returning created/updated action status
- resumes list supporting both rich table view and --json
- maintenance backup error mentioning --local-only when Notion is unconfigured
"""

import json

from job_search_hub.cli import main
from job_search_hub.library import add_resume
from job_search_hub.pipeline.resume_facts_sync import extract_pdf_text
from job_search_hub.scheduling import _resolve_command, enable


def test_pypdf_warning_suppression(tmp_path, capsys):
    """Ensure non-standard PDFs do not leak pypdf logger warnings to stderr/stdout."""
    # Construct a minimal PDF with an intentionally offset startxref that triggers pypdf warnings
    pdf_path = tmp_path / "warning_prone.pdf"
    pdf_content = (
        b"%PDF-1.4\n"
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n"
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] >>\nendobj\n"
        b"xref\n0 4\n"
        b"0000000000 65535 f \n"
        b"0000000009 00000 n \n"
        b"0000000068 00000 n \n"
        b"0000000125 00000 n \n"
        b"trailer\n<< /Root 1 0 R /Size 4 >>\n"
        b"startxref\n201\n%%EOF\n"
    )
    pdf_path.write_bytes(pdf_content)

    text = extract_pdf_text(pdf_path)
    captured = capsys.readouterr()
    assert "startxref" not in captured.err.lower()
    assert "startxref" not in captured.out.lower()
    assert "object stream" not in captured.err.lower()
    assert "object stream" not in captured.out.lower()
    assert isinstance(text, str)


def test_scheduling_resolve_command(monkeypatch, tmp_path):
    """Ensure _resolve_command prefers standalone jobhub, then sibling, then python -m."""
    # Case 1: jobhub on PATH
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_jobhub = fake_bin / "jobhub"
    fake_jobhub.touch()
    monkeypatch.setattr("shutil.which", lambda cmd: str(fake_jobhub) if cmd == "jobhub" else None)
    cmd = _resolve_command()
    assert cmd == [str(fake_jobhub), "run"]

    # Case 2: jobhub sibling of sys.executable
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    fake_python = fake_bin / "python"
    fake_python.touch()
    monkeypatch.setattr("sys.executable", str(fake_python))
    cmd = _resolve_command()
    assert cmd == [str(fake_jobhub), "run"]

    # Case 3: fallback to python -m job_search_hub run
    fake_jobhub.unlink()
    cmd = _resolve_command()
    assert cmd == [str(fake_python), "-m", "job_search_hub", "run"]


def test_scheduling_darwin_includes_path_env(monkeypatch):
    monkeypatch.setattr("platform.system", lambda: "Darwin")
    res = enable(6, preview=True)
    assert "contents" in res
    assert "PATH" in res["contents"]
    assert "job_search_hub" in json.dumps(res)


def test_sources_catalog_output_surfaces_missing_and_reasons(capsys):
    """jobhub sources catalog must provide actionable reasons for skipped entries."""
    code = main(["sources", "catalog"])
    assert code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    skipped = [item for item in data if item["status"] == "skipped"]
    assert len(skipped) > 0
    for item in skipped:
        assert "reason" in item, f"Missing reason for {item['type']}"
        assert "missing" in item, f"Missing requirements list for {item['type']}"


def test_resumes_add_returns_action_status(tmp_path):
    """add_resume returns action='created' on first add, 'updated' on subsequent."""
    pdf = tmp_path / "test.pdf"
    pdf.write_bytes(b"%PDF-1.4 sample content %%EOF")

    row1 = add_resume(pdf, "Engineer Resume", ["tag1"])
    assert row1["action"] == "created"
    assert row1["title"] == "Engineer Resume"
    assert row1["tags"] == ["tag1"]

    row2 = add_resume(pdf, "Updated Title", ["tag1", "tag2"])
    assert row2["action"] == "updated"
    assert row2["id"] == row1["id"]
    assert row2["title"] == "Updated Title"
    assert row2["tags"] == ["tag1", "tag2"]


def test_resumes_list_rich_table_and_json(tmp_path, capsys):
    """resumes list outputs rich table by default and json with --json."""
    pdf = tmp_path / "sample.pdf"
    pdf.write_bytes(b"%PDF-1.4 sample content %%EOF")
    add_resume(pdf, "Staff SRE", ["devops", "cloud"])

    # Test default output (Rich table)
    code = main(["resumes", "list"])
    assert code == 0
    out_table = capsys.readouterr().out
    assert "Staff SRE" in out_table
    assert "devops, cloud" in out_table

    # Test --json flag
    code = main(["resumes", "list", "--json"])
    assert code == 0
    out_json = capsys.readouterr().out
    parsed = json.loads(out_json)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["title"] == "Staff SRE"


def test_maintenance_backup_without_notion_mentions_local_only(capsys, tmp_path):
    """Running maintenance backup without Notion or --local-only gives actionable guidance."""
    target = tmp_path / "backup.zip"
    code = main(["maintenance", "backup", str(target)])
    assert code == 2
    captured = capsys.readouterr()
    assert "--local-only" in captured.out


def test_notion_isolated_dummy_data_lifecycle():
    """Verify complete isolated Notion workflow with realistic dummy data."""
    from tools.test_notion_e2e_isolated import run_isolated_notion_test

    result = run_isolated_notion_test()
    assert result["status"] == "success"
    assert result["database_counts"]["opportunities"] == 4
    assert result["database_counts"]["applications"] == 3
    assert result["database_counts"]["organizations"] == 3
    assert result["database_counts"]["resume_facts"] == 3
    assert result["database_counts"]["answer_bank"] == 3
