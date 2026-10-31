"""Journey construction and controlled mutation generation."""

from __future__ import annotations

from core.journeys import models


def _op(method, path, schema=None):
    return {"method": method, "path": path, "summary": "", "operation_id": None,
            "params": [], "request_schema": schema or {}, "response_types": ["application/json"]}


def _ops():
    return [
        _op("POST", "/api/requests", {"properties": {"title": {"type": "string"}}}),
        _op("POST", "/api/requests/{request_id}/approve"),
        _op("POST", "/api/requests/{request_id}/submit"),
    ]


def test_journey_needs_a_create_and_an_action():
    assert models.build_journey(_ops(), None, "clock") is None
    journey = models.build_journey(_ops(), None, "requests")
    assert journey is not None
    assert [step.name for step in journey.steps] == ["create", "submit", "approve"]


def test_journey_normalises_path_parameters():
    journey = models.build_journey(_ops(), None, "requests")
    assert all("{request_id}" not in step.path for step in journey.steps)
    assert journey.steps[1].path == "/api/requests/{id}/submit"


def test_mutations_cover_skip_repeat_stale_reorder_and_actor():
    journey = models.build_journey(_ops(), None, "requests")
    names = {mutation.name for mutation in models.mutate(journey, ["user", "other"])}
    assert "skip to approve" in names
    assert "repeat approve" in names
    assert "stale id before approve" in names
    assert "reorder actions" in names
    assert "change actor to other" in names


def test_mutation_expectations_are_explicit():
    journey = models.build_journey(_ops(), None, "requests")
    for mutation in models.mutate(journey, ["user"]):
        assert mutation.expectation
        assert isinstance(mutation.meaningful, bool)
