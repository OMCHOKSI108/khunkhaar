"""Evidence indexing: every artifact is hash-recorded, attributed where possible, and protected
while its finding is still open."""

from __future__ import annotations

import hashlib

from core import retention as policy
from core.findings import models as finding_models
from core.findings import store as findings
from core.metadata import store
from core.shared import evidence as ev
from core.shared import paths


def _finding(project_id: str, evidence_path: str) -> finding_models.Finding:
    return finding_models.Finding(
        project_id=project_id, feature="leave-approval", category="concurrency",
        title="leave balance double-spent",
        description="two concurrent approvals both debited the balance",
        severity="high", status="confirmed",
        steps=["approve the same request twice"],
        evidence={"files": [evidence_path], "kind": "verification"},
    )


def _save(project_id: str, evidence_path: str):
    finding = _finding(project_id, evidence_path)
    findings.add_finding(finding, actor="verifier", note="independent verification 2/2")
    return finding


def test_write_json_registers_the_artifact(kh_root):
    store.init()
    target = ev.write_json(ev.evidence_root("probe"), "result.json", {"a": 1},
                           project_id="alpha-00000000", run_id="phase2-20261030-120000",
                           kind="probe")
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM evidence_refs").fetchone()
    assert row is not None
    assert row["path"] == ev.relative(target)
    assert row["project_id"] == "alpha-00000000"
    assert row["run_id"] == "phase2-20261030-120000"
    assert row["sha256"] == hashlib.sha256(target.read_bytes()).hexdigest()
    assert row["retention_class"] == "probe"


def test_evidence_writing_survives_a_broken_index(kh_root):
    """The artifact is the deliverable; a metadata failure must not lose it."""
    target = ev.write_json(ev.evidence_root("orphan"), "note.json", {"ok": True},
                           project_id=None, run_id=None, kind="probe")
    assert target.is_file()


def test_index_all_backfills_and_attributes(kh_root):
    store.init()
    # qa_runs.project_id is a foreign key, so the registry row has to exist first.
    store.upsert_project("alpha-00000000", "alpha", str(kh_root / "alpha"),
                         str(kh_root / "p.json"), ["python"], ["fastapi"], "web-app")
    store.start_run("phase2-20261030-120000", "alpha-00000000", "phase2:all")
    directory = ev.evidence_root("verification", "alpha-00000000", "phase2-20261030-120000")
    artifact = ev.write_bytes(directory, "trace.zip", b"binary", project_id=None, run_id=None)
    # Deliberately un-indexed: index_all() has to find it from disk alone.
    with store.connect() as conn:
        conn.execute("DELETE FROM evidence_refs")
    result = policy.index_all()
    assert result["added"] == 1
    assert result["attributed_to_project"] == 1
    with store.connect() as conn:
        row = conn.execute("SELECT project_id, run_id FROM evidence_refs").fetchone()
    assert (row["project_id"], row["run_id"]) == ("alpha-00000000", "phase2-20261030-120000")
    assert artifact.is_file()


def test_retention_never_touches_evidence_of_an_open_finding(kh_root):
    store.init()
    artifact = ev.write_json(ev.evidence_root("verification"), "proof.json", {"x": 1},
                             project_id="alpha-00000000", run_id="phase2-20261030-120000")
    _save("alpha-00000000", ev.relative(artifact))

    scheduled = policy.plan(days=0)
    assert scheduled["candidates"] == []
    result = policy.apply(confirm=True, days=0)
    assert result["deleted"] == []
    assert artifact.is_file()


def test_retention_deletes_only_unprotected_evidence(kh_root):
    store.init()
    keep = ev.write_json(ev.evidence_root("verification"), "kept.json", {"k": 1},
                         project_id="alpha-00000000", run_id="phase2-20261030-120000")
    loose = ev.write_json(ev.evidence_root("probe"), "loose.json", {"l": 1},
                          project_id="alpha-00000000", run_id="phase2-20261030-120000")
    _save("alpha-00000000", ev.relative(keep))

    dry = policy.apply(confirm=False, days=0)
    assert dry["dry_run"] is True and len(dry["would_delete"]) == 1
    assert keep.is_file() and loose.is_file()

    applied = policy.apply(confirm=True, days=0)
    assert [d["path"] for d in applied["deleted"]] == [ev.relative(loose)]
    assert not loose.exists() and keep.is_file()
    with store.connect() as conn:
        rows = [r["path"] for r in conn.execute("SELECT path FROM evidence_refs")]
    assert ev.relative(keep) in rows and ev.relative(loose) not in rows


def test_reports_and_outside_paths_are_never_candidates(kh_root):
    store.init()
    report = ev.write_json(paths.reports_dir("phase3"), "summary.json", {"r": 1},
                           project_id="alpha-00000000", run_id="phase2-20261030-120000")
    store.add_evidence_ref("alpha-00000000", "phase2-20261030-120000", "report",
                           "data/leak.json", 2, retention_class="probe")
    store.add_evidence_ref("alpha-00000000", "phase2-20261030-120000", "probe",
                           "../../etc/passwd", 2, retention_class="probe")
    result = policy.plan(days=0)
    paths_planned = {c["path"] for c in result["candidates"]}
    assert "data/leak.json" not in paths_planned
    assert "../../etc/passwd" not in paths_planned
    assert ev.relative(report) not in paths_planned
    assert report.is_file()


def test_windows_from_config_is_non_destructive_by_default(kh_root):
    days, by_class = policy.windows_from_config()
    assert days is None
    assert by_class == {}
