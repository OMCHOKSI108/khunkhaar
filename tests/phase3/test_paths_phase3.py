"""Phase 3 directory helpers: everything derives from the root and refuses traversal."""

from __future__ import annotations

import pytest

from core.shared import paths

NO_ARG_GETTERS = (paths.git_intelligence_dir, paths.regression_dir, paths.monitoring_dir,
                  paths.intelligence_dir, paths.global_knowledge_dir,
                  paths.dependency_graphs_dir, paths.execution_history_dir,
                  paths.dashboard_dir, paths.dashboard_secrets_dir)


def test_new_directories_are_under_the_root(kh_root):
    root = kh_root.resolve()
    for getter in NO_ARG_GETTERS:
        directory = getter()
        assert str(directory.resolve()).startswith(str(root)), directory
        assert directory.is_dir(), directory


def test_project_scoped_directories_are_isolated(kh_root):
    root = paths.kh_root()
    alpha = paths.project_knowledge_dir("alpha-00000000")
    beta = paths.project_knowledge_dir("beta-00000000")
    assert alpha != beta
    assert alpha.name == "alpha-00000000"
    assert alpha.parent == root / "intelligence" / "project-knowledge"
    assert paths.project_state_dir("alpha-00000000") == (
        root / "data" / "project-state" / "alpha-00000000")


@pytest.mark.parametrize("value", ["leave-approval", "F-a7a00b40a7", "alpha-00000000"])
def test_slug_segment_keeps_valid_identifiers(kh_root, value):
    assert paths.slug_segment(value) == value


@pytest.mark.parametrize("value", ["a/b", "a b", "ticket#42"])
def test_slug_segment_neutralises_separators_instead_of_escaping(kh_root, value):
    slugified = paths.slug_segment(value)
    assert "/" not in slugified
    assert (paths.kh_root() / slugified).resolve().is_relative_to(paths.kh_root().resolve())


def test_slug_segment_normalises_display_names(kh_root):
    assert paths.slug_segment("Leave Approval") == "Leave-Approval"


@pytest.mark.parametrize("value", ["", "   ", "..", "../up", "../../etc/passwd",
                                   ".hidden", "x/../../y", "a/../../b"])
def test_slug_segment_refuses_anything_that_could_escape(kh_root, value):
    with pytest.raises(ValueError):
        paths.slug_segment(value)


def test_slug_segment_result_always_stays_inside_the_root(kh_root):
    root = kh_root.resolve()
    for value in ("alpha-00000000", "Leave Approval", "a.b_c-d"):
        candidate = (root / paths.slug_segment(value)).resolve()
        assert str(candidate).startswith(str(root)), candidate
