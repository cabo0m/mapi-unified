from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

POLARIS_ONBOARDING_SCHEMA = "polaris_onboarding.v2"
POLARIS_ONBOARDING_VERSION = 2
ONBOARDING_STATUSES = frozenset({"not_started", "in_progress", "completed", "skipped"})
ONBOARDING_STEPS = (
    "agent_name",
    "user_name",
    "work_context",
    "autonomy_level",
    "memory_policy",
    "memory_exclusions",
    "first_project",
    "summary_confirmation",
)
MEMORY_POLICIES = frozenset({"automatic_important", "ask_when_unsure", "explicit_only"})
AUTONOMY_LEVELS = frozenset({"reactive", "collaborative", "proactive"})

_STEP_QUESTIONS = {
    "agent_name": "Jak chcesz, żebym się nazywał/a? Możesz nadać mi imię albo poprosić, żebym sam/a je wybrał/a.",
    "user_name": "A jak mam zwracać się do Ciebie?",
    "work_context": "Czym się zajmujesz i w czym przede wszystkim mam Ci pomagać?",
    "autonomy_level": (
        "Jak bardzo mam być samodzielny/a? Mogę działać reaktywnie (głównie odpowiadać na polecenia), "
        "współpracująco (proponować kolejne kroki i zwracać uwagę na problemy) albo proaktywnie "
        "(samodzielnie wychwytywać problemy, proponować działania i wracać do ważnych zobowiązań)."
    ),
    "memory_policy": (
        "Jak mam podchodzić do pamięci: zapisywać samodzielnie ważne rzeczy, "
        "pytać gdy nie jestem pewien/pewna, czy zapisywać tylko na wyraźne polecenie?"
    ),
    "memory_exclusions": "Czy są informacje, których nie chcesz, żebym zapisywał/a w trwałej pamięci?",
    "first_project": "Czy chcesz od razu utworzyć pierwszy projekt, czy na razie pracujemy bez projektu?",
}

_AUTONOMY_LABELS = {
    "reactive": "reaktywnie — głównie odpowiadaj na polecenia",
    "collaborative": "współpracująco — proponuj kolejne kroki i sygnalizuj problemy",
    "proactive": "proaktywnie — wychwytuj problemy, proponuj działania i wracaj do ważnych zobowiązań",
}

_MEMORY_POLICY_LABELS = {
    "automatic_important": "samodzielnie zapisuj ważne, trwałe informacje",
    "ask_when_unsure": "pytaj, gdy nie masz pewności, czy coś zapisać",
    "explicit_only": "zapisuj trwałą pamięć tylko na wyraźne polecenie",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def ensure_onboarding_schema(conn: Any) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS polaris_onboarding (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            schema_version INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('not_started','in_progress','completed','skipped')),
            current_step TEXT,
            answers_json TEXT NOT NULL CHECK (json_valid(answers_json)),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT,
            skipped_at TEXT,
            skip_reason TEXT
        )
        """
    )
    now = utc_now_iso()
    conn.execute(
        """
        INSERT OR IGNORE INTO polaris_onboarding (
            id, schema_version, status, current_step, answers_json,
            created_at, updated_at, completed_at, skipped_at, skip_reason
        ) VALUES (1, ?, 'not_started', 'agent_name', '{}', ?, ?, NULL, NULL, NULL)
        """,
        (POLARIS_ONBOARDING_VERSION, now, now),
    )
    conn.execute(
        "UPDATE polaris_onboarding SET schema_version=? WHERE id=1 AND schema_version<?",
        (POLARIS_ONBOARDING_VERSION, POLARIS_ONBOARDING_VERSION),
    )


def _row_to_state(row: Any) -> dict[str, Any]:
    try:
        answers = json.loads(str(row["answers_json"] or "{}"))
    except (json.JSONDecodeError, TypeError):
        answers = {}
    if not isinstance(answers, dict):
        answers = {}
    return {
        "schema_version": int(row["schema_version"] or POLARIS_ONBOARDING_VERSION),
        "status": str(row["status"]),
        "current_step": None if row["current_step"] is None else str(row["current_step"]),
        "answers": answers,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "completed_at": row["completed_at"],
        "skipped_at": row["skipped_at"],
        "skip_reason": row["skip_reason"],
    }


def get_onboarding_state(conn: Any) -> dict[str, Any]:
    ensure_onboarding_schema(conn)
    row = conn.execute("SELECT * FROM polaris_onboarding WHERE id=1").fetchone()
    if row is None:
        raise RuntimeError("polaris_onboarding_state_missing")
    return _row_to_state(row)


def _next_step(step: str) -> str | None:
    try:
        index = ONBOARDING_STEPS.index(step)
    except ValueError as exc:
        raise ValueError("invalid_onboarding_step") from exc
    return ONBOARDING_STEPS[index + 1] if index + 1 < len(ONBOARDING_STEPS) else None


def _normalize_no_exclusions(text: str) -> str | None:
    compact = re.sub(r"[\s.!?]+$", "", text.strip().casefold())
    no_exclusion_answers = {
        "brak",
        "brak wykluczeń",
        "brak wykluczen",
        "bez wykluczeń",
        "bez wykluczen",
        "nie mam wykluczeń",
        "nie mam wykluczen",
        "nic nie wykluczam",
        "niczego nie wykluczam",
        "żadnych",
        "zadnych",
        "none",
        "no exclusions",
    }
    return None if compact in no_exclusion_answers else text


def _normalize_first_project_name(text: str) -> str:
    value = text.strip()
    for pattern in (r"„([^”]+)”", r'"([^"]+)"', r"'([^']+)'"):
        match = re.search(pattern, value)
        if match:
            candidate = match.group(1).strip()
            if candidate:
                return candidate

    named_match = re.search(
        r"(?i)(?:\bo\s+nazwie\b|\bnazwany\b|\bnazwana\b|\bnazwane\b|\bnamed\b|\bcalled\b)\s+(.+)$",
        value,
    )
    if named_match:
        candidate = named_match.group(1).strip().strip(" .,!?:;\"'„”")
        if candidate:
            return candidate
    return value


def _normalize_answer(step: str, value: Any, *, skip: bool) -> Any:
    text = str(value or "").strip()
    if skip:
        if step not in {"work_context", "memory_exclusions", "first_project"}:
            raise ValueError("onboarding_step_cannot_be_skipped")
        return None
    if step == "memory_policy":
        normalized = text.casefold()
        if normalized not in MEMORY_POLICIES:
            raise ValueError("invalid_memory_policy")
        return normalized
    if step == "autonomy_level":
        normalized = text.casefold()
        if normalized not in AUTONOMY_LEVELS:
            raise ValueError("invalid_autonomy_level")
        return normalized
    if step == "summary_confirmation":
        normalized = text.casefold()
        if normalized in {"confirmed", "tak", "yes", "ok", "potwierdzam", "zgadza się", "zgadza sie"}:
            return "confirmed"
        raise ValueError("summary_confirmation_required")
    if step == "memory_exclusions":
        normalized_exclusions = _normalize_no_exclusions(text)
        if normalized_exclusions is None:
            return None
        text = normalized_exclusions
    if step == "first_project":
        text = _normalize_first_project_name(text)
    limits = {
        "agent_name": 80,
        "user_name": 120,
        "work_context": 2000,
        "memory_exclusions": 2000,
        "first_project": 200,
    }
    if not text:
        raise ValueError("onboarding_value_required")
    if len(text) > limits.get(step, 2000):
        raise ValueError("onboarding_value_too_long")
    return text


def advance_onboarding_state(
    conn: Any,
    *,
    step: str,
    value: Any = None,
    skip: bool = False,
) -> dict[str, Any]:
    normalized_step = str(step or "").strip().casefold()
    if normalized_step not in ONBOARDING_STEPS:
        raise ValueError("invalid_onboarding_step")
    state = get_onboarding_state(conn)
    if state["status"] in {"completed", "skipped"}:
        raise ValueError("onboarding_already_finished")
    current = state["current_step"] or ONBOARDING_STEPS[0]
    if normalized_step != current:
        raise ValueError(f"onboarding_step_out_of_order:expected={current}")
    answer = _normalize_answer(normalized_step, value, skip=bool(skip))
    answers = dict(state["answers"])
    answers[normalized_step] = answer
    next_step = _next_step(normalized_step)
    now = utc_now_iso()
    status = "completed" if next_step is None else "in_progress"
    conn.execute(
        """
        UPDATE polaris_onboarding
        SET schema_version=?, status=?, current_step=?, answers_json=?,
            updated_at=?, completed_at=CASE WHEN ?='completed' THEN ? ELSE completed_at END
        WHERE id=1
        """,
        (
            POLARIS_ONBOARDING_VERSION,
            status,
            next_step,
            json.dumps(answers, ensure_ascii=False, sort_keys=True),
            now,
            status,
            now,
        ),
    )
    return get_onboarding_state(conn)


def revise_onboarding_answer_state(
    conn: Any,
    *,
    step: str,
    value: Any = None,
    skip: bool = False,
) -> dict[str, Any]:
    normalized_step = str(step or "").strip().casefold()
    if normalized_step not in ONBOARDING_STEPS[:-1]:
        raise ValueError("invalid_onboarding_revision_step")
    state = get_onboarding_state(conn)
    if state["status"] != "in_progress" or state["current_step"] != "summary_confirmation":
        raise ValueError("onboarding_revision_only_during_summary")
    answer = _normalize_answer(normalized_step, value, skip=bool(skip))
    answers = dict(state["answers"])
    answers[normalized_step] = answer
    now = utc_now_iso()
    conn.execute(
        """
        UPDATE polaris_onboarding
        SET schema_version=?, answers_json=?, updated_at=?
        WHERE id=1
        """,
        (
            POLARIS_ONBOARDING_VERSION,
            json.dumps(answers, ensure_ascii=False, sort_keys=True),
            now,
        ),
    )
    return get_onboarding_state(conn)


def skip_onboarding_state(conn: Any, *, reason: str | None = None) -> dict[str, Any]:
    state = get_onboarding_state(conn)
    if state["status"] == "completed":
        raise ValueError("completed_onboarding_cannot_be_skipped")
    now = utc_now_iso()
    normalized_reason = str(reason or "").strip() or None
    if normalized_reason and len(normalized_reason) > 500:
        raise ValueError("onboarding_skip_reason_too_long")
    conn.execute(
        """
        UPDATE polaris_onboarding
        SET status='skipped', current_step=NULL, updated_at=?, skipped_at=?, skip_reason=?
        WHERE id=1
        """,
        (now, now, normalized_reason),
    )
    return get_onboarding_state(conn)


def persisted_agent_name(conn: Any) -> str | None:
    try:
        state = get_onboarding_state(conn)
    except Exception:
        return None
    value = state["answers"].get("agent_name")
    text = str(value or "").strip()
    return text or None


def _summary_payload(answers: dict[str, Any]) -> dict[str, Any]:
    autonomy = answers.get("autonomy_level")
    memory_policy = answers.get("memory_policy")
    return {
        "assistant_name": answers.get("agent_name"),
        "user_name": answers.get("user_name"),
        "work_context": answers.get("work_context"),
        "autonomy_level": autonomy,
        "autonomy_description": _AUTONOMY_LABELS.get(str(autonomy)),
        "memory_policy": memory_policy,
        "memory_policy_description": _MEMORY_POLICY_LABELS.get(str(memory_policy)),
        "memory_exclusions": answers.get("memory_exclusions"),
        "first_project": answers.get("first_project"),
    }


def _summary_question(answers: dict[str, Any]) -> str:
    summary = _summary_payload(answers)
    exclusions = summary.get("memory_exclusions") or "brak dodatkowych wykluczeń"
    project = summary.get("first_project") or "bez projektu na start"
    context = summary.get("work_context") or "nie podano"
    return (
        "Tak Cię zrozumiałem/am:\n"
        f"• moje imię: {summary.get('assistant_name')}\n"
        f"• mam zwracać się do Ciebie: {summary.get('user_name')}\n"
        f"• kontekst współpracy: {context}\n"
        f"• samodzielność: {summary.get('autonomy_description')}\n"
        f"• pamięć: {summary.get('memory_policy_description')}\n"
        f"• wykluczenia pamięci: {exclusions}\n"
        f"• pierwszy projekt: {project}\n"
        "Czy wszystko się zgadza? Jeśli tak, potwierdź. Jeśli chcesz coś zmienić, wskaż co i podaj poprawną wartość."
    )


def build_onboarding_payload(conn: Any) -> dict[str, Any]:
    state = get_onboarding_state(conn)
    status = state["status"]
    step = state["current_step"]
    required = status in {"not_started", "in_progress"}
    answers = state["answers"]
    next_question = None
    if required:
        next_question = _summary_question(answers) if step == "summary_confirmation" else _STEP_QUESTIONS.get(str(step))

    if required and step == "summary_confirmation":
        assistant_instruction = (
            "Present next_question as the onboarding review. Do not create durable profile memories yet. "
            "If the user confirms the summary, BEFORE replying call run_workshop_action with area='memory', "
            "action='onboarding_advance' and payload {step:'summary_confirmation', value:'confirmed', skip:false}. "
            "That final call atomically commits the reviewed profile to durable memory. If the user corrects a field, "
            "call run_workshop_action with area='memory', action='onboarding_revise' and the corrected onboarding step/value; "
            "then present the updated review again."
        )
    elif required:
        assistant_instruction = (
            "Introduce Polaris briefly and ask exactly next_question. After the user answers, BEFORE replying, "
            "you MUST persist that onboarding answer through the compact MCP surface: call run_workshop_action with "
            "area='memory', action='onboarding_advance' and payload containing the current step, the resolved "
            "answer value and skip=false. These pre-confirmation answers are onboarding draft state, not durable profile "
            "memories. If the user delegates a choice to you, for example asks you to choose your own assistant name, "
            "choose a concrete value that is not an obvious association with Polaris, space, stars or stereotypical "
            "AI-assistant naming. Avoid default names such as Luna, Nova, Atlas, Echo and Nox. Prefer a less predictable "
            "human-like or distinctive name, then save that draft choice before announcing it. Only after the tool succeeds "
            "should you acknowledge the saved answer and ask the next_question returned by the tool. Do not invent answers "
            "the user did not provide or delegate."
        )
    else:
        assistant_instruction = "Onboarding is finished; continue normal work."

    payload = {
        "status": "onboarding_required" if required else status,
        "schema": POLARIS_ONBOARDING_SCHEMA,
        "version": POLARIS_ONBOARDING_VERSION,
        "onboarding_required": required,
        "current_step": step,
        "next_question": next_question,
        "can_skip_entire_onboarding": required and step != "summary_confirmation",
        "memory_policy_options": sorted(MEMORY_POLICIES),
        "autonomy_level_options": sorted(AUTONOMY_LEVELS),
        "product": {
            "name": "Polaris",
            "role": "persistent memory and continuity layer for a personal AI assistant",
            "capabilities": [
                "remember durable facts across chats",
                "keep project context and decisions",
                "retrieve earlier agreements and commitments",
                "maintain a source-linked assistant self-model",
                "separate global user context from project context",
            ],
        },
        "answers": answers,
        "assistant_instruction": assistant_instruction,
        "next_action": (
            {
                "required_before_reply_after_user_answer": True,
                "tool": "run_workshop_action",
                "area": "memory",
                "action": "onboarding_advance",
                "payload_template": {
                    "step": step,
                    "value": "confirmed" if step == "summary_confirmation" else "<resolved user answer or delegated assistant choice>",
                    "skip": False,
                },
                "delegated_choice_rule": (
                    "If the user asks you to choose the assistant name, choose one concrete name and use that exact "
                    "name as value. Do not choose an obvious Polaris/space/AI-assistant association. Avoid Luna, Nova, "
                    "Atlas, Echo and Nox; prefer a less predictable human-like or distinctive name. Save it before "
                    "telling the user the choice."
                    if step == "agent_name"
                    else None
                ),
            }
            if required
            else None
        ),
        "review_summary": _summary_payload(answers) if step == "summary_confirmation" else None,
        "timestamps": {
            "created_at": state["created_at"],
            "updated_at": state["updated_at"],
            "completed_at": state["completed_at"],
            "skipped_at": state["skipped_at"],
        },
    }
    if status == "completed":
        payload["summary"] = _summary_payload(answers)
        payload["user_controls"] = [
            "zapamiętaj to",
            "zapomnij to",
            "co o mnie pamiętasz?",
            "zmień zasady pamięci",
            "zmień swoje imię",
        ]
        payload["completion_note"] = (
            "Polaris przechowuje trwałą pamięć po to, aby asystent mógł zachować ciągłość między rozmowami. "
            "Użytkownik może w każdej chwili poprosić o zapis, odczyt, zmianę albo usunięcie pamięci."
        )
    if status == "skipped":
        payload["skip_reason"] = state["skip_reason"]
    return payload