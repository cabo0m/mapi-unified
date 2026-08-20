from __future__ import annotations

from typing import Any, Mapping

from mapi_core.sandman.contracts import PROVIDER_RESPONSE_SCHEMA_VERSION, validate_provider_request
from app.sandman.providers.base import PROPOSAL_ONLY_CAPABILITIES


ACTION_PRIORITY = {"duplicate_of": 0, "supersedes": 1, "contradicts": 2, "reinforces": 3, "related_to": 4}
REASONS = {
    "duplicate_of": "Exact local content hash match.",
    "supersedes": "Explicit local supersession pointer.",
    "contradicts": "Explicit allowlisted contradiction link.",
    "reinforces": "Explicit allowlisted reinforcement link.",
    "related_to": "Explicit allowlisted related link.",
}


class DeterministicProvider:
    name = "deterministic"
    kind = "local_rules"
    capabilities = dict(PROPOSAL_ONLY_CAPABILITIES)

    def analyze(self, request: Mapping[str, Any]) -> dict[str, Any]:
        request = validate_provider_request(request)
        allowed = set(request["allowed_actions"])
        candidates = {item["memory_id"]: item for item in request["candidates"]}
        found: dict[tuple[Any, ...], dict[str, Any]] = {}

        if "duplicate_of" in allowed:
            ids = sorted(candidates)
            for index, left_id in enumerate(ids):
                left = candidates[left_id]
                for right_id in ids[index + 1:]:
                    if left["content_sha256"] and left["content_sha256"] == candidates[right_id]["content_sha256"]:
                        self._add(found, "duplicate_of", [right_id], left_id)

        if "supersedes" in allowed:
            for source_id, candidate in sorted(candidates.items()):
                target = candidate.get("supersedes_memory_id")
                if target in candidates and target != source_id:
                    self._add(found, "supersedes", [source_id], target)
                replacement = candidate.get("superseded_by_memory_id")
                if replacement in candidates and replacement != source_id:
                    self._add(found, "supersedes", [replacement], source_id)

        relation_actions = {"contradicts": "contradicts", "reinforces": "reinforces", "related_to": "related_to"}
        for source_id, candidate in sorted(candidates.items()):
            for link in candidate["allowlisted_links"]:
                action = relation_actions[link["relation_type"]]
                if action in allowed and link["target_memory_id"] in candidates and link["target_memory_id"] != source_id:
                    self._add(found, action, [source_id], link["target_memory_id"])

        ordered = sorted(found.values(), key=lambda item: (ACTION_PRIORITY[item["action"]], item["source_memory_ids"], item["target_memory_id"]))
        proposals = ordered[: request["proposal_budget"]]
        for index, proposal in enumerate(proposals, start=1):
            proposal["proposal_id"] = f"det-{index:03d}"
        return {
            "schema_version": PROVIDER_RESPONSE_SCHEMA_VERSION,
            "request_id": request["request_id"],
            "input_fingerprint": request["input_fingerprint"],
            "abstain": not proposals,
            "proposals": proposals,
            "unsupported_metrics": [],
        }

    @staticmethod
    def _add(found: dict[tuple[Any, ...], dict[str, Any]], action: str, sources: list[int], target: int) -> None:
        sources = sorted(set(sources))
        evidence = sorted(set([*sources, target]))
        key = (action, tuple(sources), target)
        found[key] = {
            "proposal_id": "pending",
            "action": action,
            "source_memory_ids": sources,
            "target_memory_id": target,
            "confidence": 1.0,
            "evidence_memory_ids": evidence,
            "reason": REASONS[action],
        }
