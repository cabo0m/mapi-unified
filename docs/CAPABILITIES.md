# MAPI capability catalogue

This file is generated from the public workshop registry. Run `mapi-capabilities` after registry changes.

## `memory`

- Purpose: Create, retrieve, relate and govern durable memories.
- Workshop risk: low
- Minimum profile: `reader`
- Recommended first action: `find`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `find` | Find. | `find_memories` | `reader` | `R0` | read | no | no | no | no |
| `gravity_preview` | Gravity preview. | `get_agent_gravity_preview` | `reader` | `R0` | read | no | no | no | no |
| `gravity_shadow` | Gravity shadow. | `get_agent_gravity_shadow` | `reader` | `R0` | read | no | no | no | no |
| `hybrid_search` | Hybrid search. | `hybrid_search_memories` | `reader` | `R0` | read | no | no | no | no |
| `context` | Context. | `build_agent_context` | `reader` | `R0` | read | no | no | no | no |
| `steward_before_action` | Steward before action. | `preview_memory_steward_before_action` | `reader` | `R0` | read | no | no | no | no |
| `steward_after_action` | Steward after action. | `preview_memory_steward_after_action` | `reader` | `R0` | read | no | no | no | no |
| `steward_session_close` | Steward session close. | `preview_memory_steward_session_close` | `reader` | `R0` | read | no | no | no | no |
| `steward_nightly` | Steward nightly. | `preview_memory_steward_nightly` | `reader` | `R0` | read | no | no | no | no |
| `onboarding_status` | Onboarding status. | `get_mapi_onboarding` | `reader` | `R0` | read | no | no | no | no |
| `onboarding_advance` | Onboarding advance. | `advance_mapi_onboarding` | `reader` | `R0` | read | no | no | no | no |
| `onboarding_revise` | Onboarding revise. | `revise_mapi_onboarding` | `reader` | `R0` | read | no | no | no | no |
| `onboarding_skip` | Onboarding skip. | `skip_mapi_onboarding` | `reader` | `R0` | read | no | no | no | no |
| `self_healing_status` | Self healing status. | `get_memory_self_healing_status` | `reader` | `R0` | read | no | no | no | no |
| `self_healing_issue` | Self healing issue. | `get_memory_self_healing_issue` | `reader` | `R0` | read | no | no | no | no |
| `self_healing_propose` | Self healing propose. | `propose_memory_self_healing_resolution` | `agent` | `R1` | write | no | yes | no | no |
| `self_healing_confirm` | Self healing confirm. | `confirm_memory_self_healing_resolution` | `maintainer` | `R2` | read | no | no | no | no |
| `self_snapshot` | Self snapshot. | `get_agent_self_snapshot` | `reader` | `R0` | read | no | no | no | no |
| `commitment_ledger` | Commitment ledger. | `get_agent_commitment_ledger` | `reader` | `R0` | read | no | no | no | no |
| `autobiographical_timeline` | Autobiographical timeline. | `get_agent_autobiographical_timeline` | `reader` | `R0` | read | no | no | no | no |
| `self_capsule` | Self capsule. | `get_agent_self_capsule` | `reader` | `R0` | read | no | no | no | no |
| `self_delta` | Self delta. | `get_agent_self_delta` | `reader` | `R0` | read | no | no | no | no |
| `self_narrative` | Self narrative. | `get_agent_self_narrative` | `reader` | `R0` | read | no | no | no | no |
| `list_page` | List page. | `list_memories_page` | `reader` | `R0` | read | no | no | no | no |
| `recent` | Recent. | `recent_memories` | `reader` | `R0` | read | no | no | no | no |
| `restore_ritual` | Restore ritual. | `get_memory_restore_ritual` | `reader` | `R0` | read | no | no | no | no |
| `compare_modes` | Compare modes. | `compare_memory_modes` | `reader` | `R0` | read | no | no | no | no |
| `forgetting_review` | Forgetting review. | `get_memory_forgetting_review` | `reader` | `R0` | read | no | no | no | no |
| `health_report` | Health report. | `get_memory_health_report` | `reader` | `R0` | read | no | no | no | no |
| `relation_contracts` | Relation contracts. | `get_memory_relation_contracts` | `reader` | `R0` | read | no | no | no | no |
| `relation_preview` | Relation preview. | `preview_memory_relation` | `reader` | `R0` | read | no | no | no | no |
| `relation_apply` | Relation apply. | `apply_memory_relation` | `maintainer` | `R2` | write | no | yes | no | no |
| `relation_rollback_preview` | Relation rollback preview. | `preview_memory_relation_rollback` | `reader` | `R0` | read | no | no | no | no |
| `relation_rollback` | Relation rollback. | `rollback_memory_relation` | `maintainer` | `R2` | write | no | yes | no | no |
| `current_state` | Current state. | `get_memory_current_state` | `reader` | `R0` | read | no | no | no | no |
| `current_state_inventory` | Current state inventory. | `get_memory_current_state_inventory` | `reader` | `R0` | read | no | no | no | no |
| `lifecycle_integrity` | Lifecycle integrity. | `get_memory_lifecycle_integrity_report` | `reader` | `R0` | read | no | no | no | no |
| `lifecycle_remediation_inventory` | Lifecycle remediation inventory. | `get_memory_lifecycle_remediation_inventory` | `reader` | `R0` | read | no | no | no | no |
| `lifecycle_remediation_preview` | Lifecycle remediation preview. | `preview_memory_lifecycle_remediation` | `reader` | `R0` | read | no | no | no | no |
| `pointer_lifecycle_remediation_inventory` | Pointer lifecycle remediation inventory. | `get_memory_pointer_lifecycle_remediation_inventory` | `reader` | `R0` | read | no | no | no | no |
| `pointer_lifecycle_remediation_preview` | Pointer lifecycle remediation preview. | `preview_memory_pointer_lifecycle_remediation` | `reader` | `R0` | read | no | no | no | no |
| `pointer_lifecycle_remediation_execution_preview` | Pointer lifecycle remediation execution preview. | `preview_memory_pointer_lifecycle_remediation_execution` | `reader` | `R0` | read | no | no | no | no |
| `pointer_lifecycle_remediation_execution_apply` | Pointer lifecycle remediation execution apply. | `apply_memory_pointer_lifecycle_remediation_execution` | `admin` | `R3` | write | no | yes | no | no |
| `pointer_lifecycle_remediation_execution_run` | Pointer lifecycle remediation execution run. | `get_memory_pointer_lifecycle_remediation_execution_run` | `reader` | `R0` | read | no | no | no | no |
| `pointer_lifecycle_remediation_execution_rollback_preview` | Pointer lifecycle remediation execution rollback preview. | `preview_memory_pointer_lifecycle_remediation_execution_rollback` | `reader` | `R0` | read | no | no | no | no |
| `pointer_lifecycle_remediation_execution_rollback` | Pointer lifecycle remediation execution rollback. | `rollback_memory_pointer_lifecycle_remediation_execution` | `admin` | `R3` | write | no | yes | no | no |
| `lifecycle_remediation_apply` | Lifecycle remediation apply. | `apply_memory_lifecycle_remediation` | `admin` | `R3` | write | no | yes | no | no |
| `lifecycle_remediation_run` | Lifecycle remediation run. | `get_memory_lifecycle_remediation_run` | `reader` | `R0` | read | no | no | no | no |
| `lifecycle_remediation_rollback_preview` | Lifecycle remediation rollback preview. | `preview_memory_lifecycle_remediation_rollback` | `reader` | `R0` | read | no | no | no | no |
| `lifecycle_remediation_rollback` | Lifecycle remediation rollback. | `rollback_memory_lifecycle_remediation` | `admin` | `R3` | write | no | yes | no | no |
| `consolidation_queue` | Consolidation queue. | `list_memory_consolidation_proposals` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_get` | Consolidation get. | `get_memory_consolidation_proposal` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_approve` | Consolidation approve. | `approve_memory_consolidation_proposal` | `maintainer` | `R2` | write | no | yes | no | no |
| `consolidation_reject` | Consolidation reject. | `reject_memory_consolidation_proposal` | `maintainer` | `R2` | write | no | yes | no | no |
| `consolidation_apply_preview` | Consolidation apply preview. | `preview_apply_memory_consolidation_proposal` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_apply` | Consolidation apply. | `apply_approved_memory_consolidation_proposal` | `maintainer` | `R2` | write | no | yes | no | no |
| `consolidation_apply_runs` | Consolidation apply runs. | `list_memory_consolidation_apply_runs` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_apply_run` | Consolidation apply run. | `get_memory_consolidation_apply_run` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_lifecycle_report` | Consolidation lifecycle report. | `get_memory_consolidation_lifecycle_report` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_apply_rollback_preview` | Consolidation apply rollback preview. | `preview_memory_consolidation_apply_rollback` | `reader` | `R0` | read | no | no | no | no |
| `consolidation_apply_rollback` | Consolidation apply rollback. | `rollback_memory_consolidation_apply_run` | `maintainer` | `R2` | write | no | yes | no | no |
| `consolidation_snapshot_integrity` | Consolidation snapshot integrity. | `get_memory_consolidation_snapshot_integrity_report` | `reader` | `R0` | read | no | no | no | no |
| `supersession_preview` | Supersession preview. | `preview_memory_supersession` | `reader` | `R0` | read | no | no | no | no |
| `supersession_apply` | Supersession apply. | `apply_memory_supersession` | `maintainer` | `R2` | write | no | yes | no | no |
| `supersession_runs` | Supersession runs. | `list_memory_supersession_runs` | `reader` | `R0` | read | no | no | no | no |
| `supersession_run` | Supersession run. | `get_memory_supersession_run` | `reader` | `R0` | read | no | no | no | no |
| `supersession_rollback_preview` | Supersession rollback preview. | `preview_memory_supersession_rollback` | `reader` | `R0` | read | no | no | no | no |
| `supersession_rollback` | Supersession rollback. | `rollback_memory_supersession_run` | `maintainer` | `R2` | write | no | yes | no | no |
| `explain_retrieval` | Explain retrieval. | `explain_retrieval` | `reader` | `R0` | read | no | no | no | no |
| `search_qa_report` | Search qa report. | `search_qa_report` | `reader` | `R0` | read | no | no | no | no |
| `capture_proposal` | Capture proposal. | `propose_memory_capture` | `reader` | `R0` | write | no | yes | no | no |
| `capture_save` | Capture save. | `save_memory_capture_proposal` | `maintainer` | `R2` | write | no | yes | no | no |
| `capture_list` | Capture list. | `list_memory_capture_review_items` | `reader` | `R0` | read | no | no | no | no |
| `capture_get` | Capture get. | `get_memory_capture_review_item` | `reader` | `R0` | read | no | no | no | no |
| `capture_review_decide` | Capture review decide. | `review_memory_capture_item` | `maintainer` | `R2` | read | no | no | no | no |
| `capture_expire` | Capture expire. | `expire_memory_capture_item` | `maintainer` | `R2` | write | no | yes | no | no |
| `capture_reconciliation_preview` | Capture reconciliation preview. | `preview_memory_capture_reconciliation` | `reader` | `R0` | read | no | no | no | no |
| `capture_apply` | Capture apply. | `apply_memory_capture_reconciliation` | `maintainer` | `R2` | write | no | yes | no | no |
| `retention_preview` | Retention preview. | `preview_memory_retention_policy` | `reader` | `R0` | read | no | no | no | no |
| `retention_project_preview` | Retention project preview. | `preview_project_memory_retention` | `reader` | `R0` | read | no | no | no | no |
| `retention_review_save` | Retention review save. | `save_memory_retention_review` | `maintainer` | `R2` | write | no | yes | no | no |
| `retention_review_list` | Retention review list. | `list_memory_retention_reviews` | `reader` | `R0` | read | no | no | no | no |
| `retention_review_get` | Retention review get. | `get_memory_retention_review` | `reader` | `R0` | read | no | no | no | no |
| `retention_review_decide` | Retention review decide. | `decide_memory_retention_review` | `maintainer` | `R2` | write | no | yes | no | no |
| `retention_apply` | Retention apply. | `apply_memory_retention_review` | `maintainer` | `R2` | write | no | yes | no | no |
| `retention_apply_batch` | Retention apply batch. | `apply_memory_retention_batch` | `maintainer` | `R2` | write | no | yes | no | no |
| `retention_rollback_preview` | Retention rollback preview. | `preview_memory_retention_rollback` | `reader` | `R0` | read | no | no | no | no |
| `retention_rollback` | Retention rollback. | `rollback_memory_retention_review` | `maintainer` | `R2` | write | no | yes | no | no |
| `create_from_proposal` | Create from proposal. | `create_memory_from_proposal` | `maintainer` | `R2` | write | no | yes | no | no |
| `project_brief` | Project brief. | `get_project_brief` | `reader` | `R0` | read | no | no | no | no |
| `project_card` | Project card. | `get_project_card` | `reader` | `R0` | read | no | no | no | no |
| `recent_project_changes` | Recent project changes. | `get_recent_project_changes` | `reader` | `R0` | read | no | no | no | no |
| `provenance_backfill_preview` | Provenance backfill preview. | `preview_memory_provenance_backfill` | `reader` | `R0` | read | no | no | no | no |
| `provenance_backfill_apply` | Provenance backfill apply. | `apply_memory_provenance_backfill` | `admin` | `R3` | write | no | yes | no | no |
| `get` | Get. | `get_memory` | `reader` | `R0` | read | no | no | no | no |
| `links` | Links. | `get_memory_links` | `reader` | `R0` | write | no | yes | no | no |
| `save` | Save. | `save_memory` | `agent` | `R1` | write | no | yes | no | no |
| `propose` | Propose. | `propose_memory` | `agent` | `R1` | write | no | yes | no | no |
| `create` | Create. | `create_memory` | `admin` | `R3` | write | no | yes | no | no |
| `admin_write` | Admin write. | `admin_memory_write` | `admin` | `R3` | read | no | no | no | no |
| `create_direct_confirmed` | Create direct confirmed. | `create_memory_direct_confirmed` | `admin` | `R3` | write | no | yes | no | no |
| `recall` | Recall. | `recall_memory` | `agent` | `R1` | write | no | yes | no | no |
| `recall_telemetry` | Recall telemetry. | `get_memory_recall_telemetry` | `reader` | `R0` | write | no | yes | no | no |
| `project_aliases` | Project aliases. | `list_project_key_aliases` | `reader` | `R0` | read | no | no | no | no |
| `upsert_project_alias` | Upsert project alias. | `upsert_project_key_alias` | `maintainer` | `R2` | write | no | yes | no | no |

## `sandman`

- Purpose: Run deterministic and proposal-only memory maintenance.
- Workshop risk: medium
- Minimum profile: `agent`
- Recommended first action: `canonical_status`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `canonical_status` | Canonical status. | `get_sandman_canonical_status` | `agent` | `R0` | read | no | no | no | no |
| `canonical_preview` | Canonical preview. | `preview_sandman_canonical` | `agent` | `R0` | read | no | no | no | no |
| `canonical_runs` | Canonical runs. | `list_sandman_canonical_runs` | `agent` | `R0` | read | no | no | no | no |
| `canonical_run` | Canonical run. | `get_sandman_canonical_run` | `agent` | `R0` | read | no | no | no | no |
| `provider_status` | Provider status. | `get_sandman_provider_v3_status` | `agent` | `R0` | read | no | no | no | no |
| `provider_request_preview` | Provider request preview. | `preview_sandman_provider_request` | `agent` | `R0` | read | no | no | no | no |
| `provider_deterministic_preview` | Provider deterministic preview. | `preview_sandman_provider_deterministic` | `agent` | `R0` | read | no | no | no | no |
| `semantic_shadow_preview` | Semantic shadow preview. | `preview_sandman_gemini_shadow` | `agent` | `R0` | read | yes | no | no | no |
| `semantic_shadow_run` | Semantic shadow run. | `run_sandman_gemini_shadow` | `maintainer` | `R2` | write | yes | yes | no | no |
| `semantic_shadow_list` | Semantic shadow list. | `list_sandman_gemini_shadow_runs` | `agent` | `R0` | read | yes | no | no | no |
| `semantic_shadow_get` | Semantic shadow get. | `get_sandman_gemini_shadow_run` | `agent` | `R0` | read | yes | no | no | no |
| `semantic_evaluation_corpus` | Semantic evaluation corpus. | `get_sandman_semantic_evaluation_corpus` | `agent` | `R0` | read | no | no | no | no |
| `semantic_evaluation` | Semantic evaluation. | `evaluate_sandman_semantic_provider` | `agent` | `R0` | read | no | no | no | no |

## `timeline`

- Purpose: Inspect project, memory and conversation history.
- Workshop risk: low
- Minimum profile: `reader`
- Recommended first action: `search_verbatim`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `archive_conversation` | Archive conversation. | `archive_conversation` | `agent` | `R1` | write | no | yes | no | no |
| `get_conversation` | Get conversation. | `get_conversation` | `reader` | `R0` | read | no | no | no | no |
| `list_conversations` | List conversations. | `list_conversations` | `reader` | `R0` | read | no | no | no | no |
| `search_verbatim` | Search verbatim. | `search_verbatim` | `reader` | `R0` | read | no | no | no | no |
| `reconstruct_day` | Reconstruct day. | `reconstruct_day` | `reader` | `R0` | read | no | no | no | no |

## `conflicts`

- Purpose: Detect conflicts and record guarded review decisions.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `clusters`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `clusters` | Clusters. | `get_conflict_clusters` | `reader` | `R0` | read | no | no | no | no |
| `registry` | Registry. | `get_conflict_registry` | `reader` | `R0` | read | no | no | no | no |
| `report` | Report. | `get_conflict_report` | `reader` | `R0` | read | no | no | no | no |
| `history` | History. | `get_conflict_history` | `reader` | `R0` | read | no | no | no | no |
| `preview_resolution` | Preview resolution. | `preview_conflict_resolution` | `reader` | `R0` | read | no | no | yes | no |
| `record_decision` | Record decision. | `record_conflict_decision` | `maintainer` | `R2` | write | no | yes | no | no |

## `governance`

- Purpose: Inspect quality, queues, ownership and operational health.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `quality_alerts`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `quality_alerts` | Quality alerts. | `get_quality_alerts` | `reader` | `R0` | read | no | no | no | no |
| `operations_dashboard` | Operations dashboard. | `get_mapi_operations_observability` | `reader` | `R0` | read | no | no | no | no |
| `canonical_truth_review` | Canonical truth review. | `get_canonical_truth_review` | `reader` | `R0` | read | no | no | no | no |
| `legacy_graph_audit` | Legacy graph audit. | `get_legacy_graph_audit` | `reader` | `R0` | read | no | no | no | no |
| `queue_dashboard` | Queue dashboard. | `get_operational_queue_dashboard` | `reader` | `R0` | read | no | no | no | no |
| `owner_workload` | Owner workload. | `get_effective_owner_workload` | `reader` | `R0` | read | no | no | no | no |
| `scope_mismatches` | Scope mismatches. | `list_project_scope_mismatches` | `reader` | `R0` | read | no | no | no | no |
| `hygiene_inventory` | Hygiene inventory. | `get_memory_hygiene_inventory` | `reader` | `R0` | read | no | no | no | no |
| `hygiene_preview` | Hygiene preview. | `preview_memory_hygiene` | `reader` | `R0` | read | no | no | no | no |
| `hygiene_apply` | Hygiene apply. | `apply_memory_hygiene` | `admin` | `R3` | write | no | yes | no | no |
| `hygiene_run` | Hygiene run. | `get_memory_hygiene_run` | `reader` | `R0` | read | no | no | no | no |
| `hygiene_rollback_preview` | Hygiene rollback preview. | `preview_memory_hygiene_rollback` | `reader` | `R0` | read | no | no | no | no |
| `hygiene_rollback` | Hygiene rollback. | `rollback_memory_hygiene_run` | `admin` | `R3` | write | no | yes | no | no |
| `doctor` | Doctor. | `get_mapi_doctor_report` | `reader` | `R0` | read | no | no | no | no |
| `recovery_plan` | Recovery plan. | `get_mapi_recovery_plan` | `reader` | `R0` | read | no | no | no | no |
| `transport_status` | Transport status. | `get_mcp_transport_status` | `reader` | `R0` | read | no | no | no | no |
| `runtime_readiness` | Runtime readiness. | `get_runtime_readiness` | `reader` | `R0` | write | no | yes | no | no |
| `private_runtime` | Private runtime. | `get_private_runtime_status` | `reader` | `R0` | read | no | no | no | no |
| `provider_observability` | Provider observability. | `get_sandman_provider_observability` | `reader` | `R0` | read | no | no | no | no |

## `owner_catalog`

- Purpose: Manage responsibility metadata and owner catalogue health.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `health`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `health` | Health. | `get_owner_catalog_health` | `reader` | `R0` | read | no | no | no | no |
| `list_items` | List items. | `list_owner_directory_items` | `reader` | `R0` | read | no | no | no | no |
| `list_mappings` | List mappings. | `list_owner_role_mappings` | `reader` | `R0` | read | no | no | no | no |
| `repair_summary` | Repair summary. | `get_owner_catalog_repair_summary` | `reader` | `R0` | read | no | no | no | no |

## `feature_flags`

- Purpose: Inspect and control feature rollout configuration.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `list`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `list` | List. | `list_feature_flags` | `reader` | `R0` | read | no | no | no | no |
| `get` | Get. | `get_feature_flag` | `reader` | `R0` | read | no | no | no | no |
| `evaluate` | Evaluate. | `evaluate_feature_flag` | `reader` | `R0` | read | no | no | no | no |

## `research_ingest`

- Purpose: Quarantine, review and promote external research.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `list_queue`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `create` | Create. | `create_ingest_item` | `agent` | `R1` | write | no | yes | no | no |
| `list_queue` | List queue. | `list_ingest_queue` | `reader` | `R0` | read | no | no | no | no |
| `get` | Get. | `get_ingest_item` | `reader` | `R0` | read | no | no | no | no |
| `promote` | Promote. | `promote_ingest_item` | `maintainer` | `R2` | write | no | yes | no | no |
| `reject` | Reject. | `reject_ingest_item` | `maintainer` | `R2` | write | no | yes | no | no |

## `semantic`

- Purpose: Use optional vector-based retrieval and embedding status.
- Workshop risk: low
- Minimum profile: `reader`
- Recommended first action: `search`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `search` | Search. | `search_semantic` | `reader` | `R0` | read | no | no | no | no |
| `stats` | Stats. | `get_semantic_embedding_stats` | `reader` | `R0` | read | no | no | no | no |

## `gemma`

- Purpose: Use optional local-model worker capabilities.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `create_job`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `worker_status` | Worker status. | `gemma_worker_status` | `reader` | `R0` | read | yes | no | no | no |
| `create_job` | Create job. | `gemma_worker_create_job` | `maintainer` | `R1` | write | yes | yes | no | no |
| `get_job` | Get job. | `gemma_worker_get_job` | `reader` | `R0` | read | yes | no | no | no |
| `prepare_plan` | Prepare plan. | `gemma_worker_prepare_plan` | `maintainer` | `R1` | read | yes | no | no | no |
| `approve_job` | Approve job. | `gemma_worker_approve_job` | `maintainer` | `R1` | write | yes | yes | no | no |
| `reject_job` | Reject job. | `gemma_worker_reject_job` | `maintainer` | `R1` | write | yes | yes | no | no |
| `cancel_job` | Cancel job. | `gemma_worker_cancel_job` | `maintainer` | `R1` | write | yes | yes | no | no |
| `run_job` | Run job. | `gemma_worker_run_job` | `maintainer` | `R1` | write | yes | yes | no | no |
| `get_report` | Get report. | `gemma_worker_get_report` | `reader` | `R0` | read | yes | no | no | no |
| `run_task` | Run task. | `gemma_worker_run_task` | `maintainer` | `R1` | write | yes | yes | no | no |
| `prepare_task` | Prepare task. | `gemma_worker_prepare_task` | `maintainer` | `R1` | read | yes | no | no | no |
| `report` | Report. | `gemma_worker_report` | `maintainer` | `R1` | read | yes | no | no | no |
| `status` | Status. | `gemma_lms_status` | `reader` | `R0` | read | yes | no | no | no |
| `load` | Load. | `gemma_lms_load` | `maintainer` | `R1` | read | yes | no | no | no |
| `unload` | Unload. | `gemma_lms_unload` | `maintainer` | `R1` | read | yes | no | no | no |
| `ask` | Ask. | `gemma_ask` | `maintainer` | `R1` | read | yes | no | no | no |
| `coding_task` | Coding task. | `gemma_coding_task` | `maintainer` | `R1` | read | yes | no | no | no |

## `memory_linking`

- Purpose: Preview and run deterministic relationship discovery.
- Workshop risk: medium
- Minimum profile: `maintainer`
- Recommended first action: `preview`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `preview` | Preview. | `preview_memory_linking_pass` | `reader` | `R0` | read | no | no | yes | no |
| `run` | Run. | `run_memory_linking_pass` | `maintainer` | `R2` | write | no | yes | no | no |

## `files`

- Purpose: Use project-bound file reads and guarded writes.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `list_file_roots`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `list_file_roots` | List file roots. | `list_project_file_roots` | `reader` | `R0` | read | no | no | no | no |
| `list_directory` | List directory. | `list_project_directory` | `reader` | `R0` | read | no | no | no | no |
| `read_file_text` | Read file text. | `read_project_file_text` | `reader` | `R0` | read | no | no | no | no |
| `preview_file_write` | Preview file write. | `preview_project_file_write` | `agent` | `R1` | read | no | no | yes | no |
| `apply_file_write` | Apply file write. | `apply_project_file_write` | `maintainer` | `R2` | write | no | yes | no | no |
| `list_file_operations` | List file operations. | `list_project_file_operations` | `reader` | `R0` | read | no | no | no | no |
| `preview_file_rollback` | Preview file rollback. | `preview_project_file_rollback` | `agent` | `R1` | read | no | no | yes | no |
| `rollback_file_write` | Rollback file write. | `rollback_project_file_write` | `maintainer` | `R2` | write | no | yes | no | yes |

## `git`

- Purpose: Inspect project Git state and use guarded stage/commit operations.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `git_status`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `list_git_repositories` | List git repositories. | `list_project_git_repositories` | `reader` | `R0` | read | no | no | no | no |
| `git_info` | Git info. | `project_git_info` | `reader` | `R0` | read | no | no | no | no |
| `git_status` | Git status. | `project_git_status` | `reader` | `R0` | read | no | no | no | no |
| `git_diff` | Git diff. | `project_git_diff` | `reader` | `R0` | read | no | no | no | no |
| `git_log` | Git log. | `project_git_log` | `reader` | `R0` | read | no | no | no | no |
| `preview_git_stage` | Preview git stage. | `preview_project_git_stage` | `agent` | `R1` | read | no | no | yes | no |
| `apply_git_stage` | Apply git stage. | `apply_project_git_stage` | `maintainer` | `R2` | write | no | yes | no | no |
| `list_git_stage_operations` | List git stage operations. | `list_project_git_stage_operations` | `reader` | `R0` | read | no | no | no | no |
| `preview_git_stage_rollback` | Preview git stage rollback. | `preview_project_git_stage_rollback` | `agent` | `R1` | read | no | no | yes | no |
| `rollback_git_stage` | Rollback git stage. | `rollback_project_git_stage` | `maintainer` | `R2` | write | no | yes | no | yes |
| `preview_git_commit` | Preview git commit. | `preview_project_git_commit` | `agent` | `R1` | read | no | no | yes | no |
| `apply_git_commit` | Apply git commit. | `apply_project_git_commit` | `maintainer` | `R2` | write | no | yes | no | no |
| `list_git_commit_operations` | List git commit operations. | `list_project_git_commit_operations` | `reader` | `R0` | read | no | no | no | no |
| `preview_git_commit_rollback` | Preview git commit rollback. | `preview_project_git_commit_rollback` | `agent` | `R1` | read | no | no | yes | no |
| `rollback_git_commit` | Rollback git commit. | `rollback_project_git_commit` | `maintainer` | `R2` | write | no | yes | no | yes |

## `commands`

- Purpose: Run operator-approved fixed command recipes.
- Workshop risk: medium
- Minimum profile: `reader`
- Recommended first action: `list_command_recipes`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `list_command_recipes` | List command recipes. | `list_project_command_recipes` | `reader` | `R0` | read | no | no | no | no |
| `preview_command_recipe` | Preview command recipe. | `preview_project_command_recipe` | `agent` | `R1` | read | no | no | yes | no |
| `run_command_recipe` | Run command recipe. | `run_project_command_recipe` | `maintainer` | `R2` | write | no | yes | no | no |
| `list_command_runs` | List command runs. | `list_project_command_runs` | `reader` | `R0` | read | no | no | no | no |

## `admin`

- Purpose: Perform dangerous local database, file and process operations.
- Workshop risk: high
- Minimum profile: `admin`
- Recommended first action: `db_info`

| Action | Purpose | Tool | Access | Risk | Read/write | External model | Mutates data | Preview | Rollback |
|---|---|---|---|---|---|---|---|---|---|
| `db_info` | Db info. | `get_db_info` | `admin` | `R3` | read | no | no | no | no |
| `query_sql` | Query sql. | `query_sql` | `admin` | `R3` | read | no | no | no | no |
| `read_file` | Read file. | `read_file_text` | `admin` | `R3` | read | no | no | no | no |
| `write_file` | Write file. | `write_file_text` | `admin` | `R3` | write | no | yes | no | no |
| `insert_before_marker` | Insert before marker. | `insert_before_marker` | `admin` | `R3` | read | no | no | no | no |
| `insert_after_marker` | Insert after marker. | `insert_after_marker` | `admin` | `R3` | read | no | no | no | no |
| `replace_once` | Replace once. | `replace_once` | `admin` | `R3` | read | no | no | no | no |
| `delete_path` | Delete path. | `delete_path` | `admin` | `R3` | write | no | yes | no | no |
| `run_shell` | Run shell. | `run_shell` | `admin` | `R3` | write | no | yes | no | no |
| `run_powershell` | Run powershell. | `run_powershell` | `admin` | `R3` | write | no | yes | no | no |
| `run_pytest` | Run pytest. | `run_pytest` | `admin` | `R3` | write | no | yes | no | no |
| `git_status` | Git status. | `git_status` | `admin` | `R3` | read | no | no | no | no |
| `git_commit` | Git commit. | `git_commit` | `admin` | `R3` | read | no | no | no | no |
| `git_push` | Git push. | `git_push` | `admin` | `R3` | read | no | no | no | no |
