from __future__ import annotations

import copy
import json

import mcp_surface

from mapi_core.memory.agent_self_delta import AGENT_SELF_DELTA_SCHEMA, compare_agent_self_snapshots
from mapi_core.memory.agent_self_model import calculate_agent_self_snapshot_fingerprint


def _self(memory_factory, **overrides):
    values = dict(content="Alpha identity", summary_short="Alpha identity", memory_type="identity", source="pytest", importance_score=0.9, confidence_score=1.0, tags="agent-self,subject:alpha", layer_code="identity", area_code="identity", state_code="validated", scope_code="project", identity_weight=0.9, project_key="alpha-self", entry_type="user_profile", truth_kind="fact")
    values.update(overrides)
    return memory_factory(**values)


def _refingerprint(snapshot):
    snapshot["source_memory_ids"] = sorted({int(item["id"]) for values in snapshot["sections"].values() for item in values})
    snapshot["source_count"] = len(snapshot["source_memory_ids"])
    snapshot["snapshot_fingerprint"] = calculate_agent_self_snapshot_fingerprint(snapshot)
    return snapshot


def test_identical_snapshot_has_no_delta(server, memory_factory):
    _self(memory_factory)
    snap = server.get_agent_self_snapshot(subject_key="alpha", project_key="alpha-self", include_global=False)
    result = server.get_agent_self_delta(from_snapshot_json=json.dumps(snap), to_snapshot_json=json.dumps(snap), subject_key="alpha", project_key="alpha-self")
    assert result["schema"] == AGENT_SELF_DELTA_SCHEMA
    assert result["status"] == "ok"
    assert result["read_only"] is True
    assert result["has_changes"] is False
    assert result["added"] == result["removed"] == result["superseded"] == result["reclassified"] == []
    assert result["unchanged_anchors"]
    assert len(result["delta_fingerprint"]) == 64


def test_delta_detects_reclassification_and_uncertainty(server, memory_factory):
    memory_id = _self(memory_factory)
    before = server.get_agent_self_snapshot(subject_key="alpha", project_key="alpha-self", include_global=False)
    after = copy.deepcopy(before)
    item = next(item for item in after["sections"]["identity"] if int(item["id"]) == memory_id)
    item["confidence_score"] = 0.5
    item["requires_user_confirmation"] = True
    _refingerprint(after)
    result = compare_agent_self_snapshots(before, after)
    assert result["has_changes"] is True
    assert result["reclassified"][0]["memory_id"] == memory_id
    assert "confidence_score" in result["reclassified"][0]["changed_fields"]
    assert [item["memory_id"] for item in result["new_uncertainties"]] == [memory_id]


def test_delta_collapses_supersession_instead_of_add_remove(server, memory_factory):
    old_id = _self(memory_factory, summary_short="old identity")
    before = server.get_agent_self_snapshot(subject_key="alpha", project_key="alpha-self", include_global=False)
    after = copy.deepcopy(before)
    old = next(item for item in after["sections"]["identity"] if int(item["id"]) == old_id)
    after["sections"]["identity"] = [item for item in after["sections"]["identity"] if int(item["id"]) != old_id]
    new = copy.deepcopy(old)
    new["id"] = 900001
    new["summary_short"] = "new identity"
    new["title"] = "new identity"
    new["supersedes_memory_id"] = old_id
    after["sections"]["identity"].append(new)
    _refingerprint(after)
    result = compare_agent_self_snapshots(before, after)
    assert result["added"] == []
    assert result["removed"] == []
    assert result["superseded"][0]["old_memory_id"] == old_id
    assert result["superseded"][0]["new_memory_id"] == 900001


def test_delta_rejects_bad_schema_and_tampered_fingerprint(server, memory_factory):
    _self(memory_factory)
    snap = server.get_agent_self_snapshot(subject_key="alpha", project_key="alpha-self", include_global=False)
    bad_schema = copy.deepcopy(snap); bad_schema["schema"] = "wrong"
    result = server.get_agent_self_delta(from_snapshot_json=json.dumps(bad_schema), subject_key="alpha", project_key="alpha-self")
    assert result["error"] == "incompatible_snapshot_schema"
    tampered = copy.deepcopy(snap); tampered["sections"]["identity"][0]["summary_short"] = "tampered"
    result = server.get_agent_self_delta(from_snapshot_json=json.dumps(tampered), subject_key="alpha", project_key="alpha-self")
    assert result["error"] == "snapshot_fingerprint_mismatch"


def test_memory_workshop_exposes_self_delta_to_reader():
    workshop = mcp_surface.open_workshop_payload("memory", profile="reader")
    action = {item["action"]: item for item in workshop["actions"]}["self_delta"]
    assert action["tool_name"] == "get_agent_self_delta"
    assert action["risk_class"] == "R0"
