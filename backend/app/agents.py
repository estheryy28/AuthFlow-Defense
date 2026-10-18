from __future__ import annotations

import json
import os
import re
from typing import Any

import httpx

from .contracts import (
    ApplicationMapperResult,
    PolicyAnalysis,
    PolicyEvidenceAudit,
    ReviewDecision,
    TestPlan,
)

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b-instruct")


def _json_object(value: str) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", value, flags=re.DOTALL)
        if not match:
            raise ValueError("Local model did not return a JSON object")
        result = json.loads(match.group(0))
    if not isinstance(result, dict):
        raise ValueError("Local model response must be a JSON object")
    return result


async def ask_agent(
    role: str,
    instructions: str,
    evidence: str,
    response_schema: dict[str, Any] | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    prompt = (
        f"You are the {role} in a cybersecurity test system.\n"
        "Use only the supplied evidence. Do not invent requirements, routes, fields, or results. "
        "Return one JSON object only, with no Markdown fences.\n\n"
        f"TASK:\n{instructions}\n\nEVIDENCE:\n{evidence}"
    )
    timeout = httpx.Timeout(timeout_seconds, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "stream": False,
                    "format": response_schema or "json",
                    "options": {"temperature": 0},
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"Local model request failed at {OLLAMA_BASE_URL}. Start Ollama and pull "
            f"{OLLAMA_MODEL}. Details: {exc}"
        ) from exc
    content = response.json().get("message", {}).get("content", "")
    if not content:
        raise RuntimeError("Local model returned an empty response")
    return _json_object(content)


async def policy_analyst(policy: str) -> dict[str, Any]:
    sections: dict[str, str] = {}
    for block in re.split(r"(?m)^##\s+", policy)[1:]:
        heading, _, body = block.partition("\n")
        sections[heading.strip()] = body.strip()
    return await ask_agent(
        "Policy Analyst",
        "Extract candidate security invariants from the supplied payment workflow policy. "
        "Look for falsifiable properties that follow from combining policy clauses across "
        "different lifecycle sections. Do not return a paraphrase of one clause as an invariant; "
        "the candidate about payment-term consistency must connect the Review record, Request "
        "amendments, and Settlement sections for the same payment record. Return exactly three "
        "evidence passages, one exact quote from each of those three sections. Do not assume "
        "that document sections themselves are runtime objects: write the claim as a runtime "
        "property. For this fixture, use the precise form 'For the same payment ID, the current "
        "payee, amount, and currency at settlement must equal those recorded in its review event.' "
        "Do not assume that any other unstated rule exists. For this available oracle, "
        "material_fields must contain only the protected payment-term fields payee, amount, and "
        "currency; do not include identity or audit metadata. Cite the three exact quotes from "
        "the three required sections, covering every clause needed for the claim. "
        "Return "
        '{"invariants":[{"id":"...","claim":"...",'
        '"oracle_id":"payment_approval_terms","material_fields":["..."],'
        '"evidence":[{"source":"payment-policy.md","section":"Review record",'
        '"quote":"exact substring"}],'
        '"ambiguities":[]}]} . Quotes must be exact substrings. Include only explicit or '
        "strongly supported cross-section policy rules; label unresolved intent in ambiguities. "
        "The only "
        "valid evidence source name is payment-policy.md.",
        json.dumps({"sections": sections}, ensure_ascii=False),
        PolicyAnalysis.model_json_schema(),
    )


async def policy_evidence_auditor(
    policy: str, candidate: dict[str, Any]
) -> dict[str, Any]:
    sections: dict[str, str] = {}
    for block in re.split(r"(?m)^##\s+", policy)[1:]:
        heading, _, body = block.partition("\n")
        sections[heading.strip()] = body.strip()
    return await ask_agent(
        "Independent Policy Evidence Auditor",
        "Audit the candidate claim; do not rewrite or assume it is correct. Independently check "
        "whether the supplied policy logically supports the claim across all three sections. "
        "The claim is a multi-section inference: a supporting quote may establish one premise "
        "rather than repeat the complete conclusion. Evaluate the premises jointly. In particular, "
        "check whether (1) the Review record defines the exact terms approved, (2) an amendment "
        "can change the current request without changing that earlier review record, and (3) "
        "Settlement uses current terms for requests still marked approved. Do not require one "
        "sentence to state the entire inferred invariant. If a premise or logical bridge is absent, "
        "return AMBIGUOUS rather than assuming it. If the Settlement text explicitly says that "
        "current terms must match the review-event terms for the same payment ID, treat that as "
        "direct support for the candidate and do not call that relation ambiguous. "
        "Return PASS only if the exact claim is supported by one exact policy quote from each "
        "section: Review record, Request amendments, and Settlement. Each quote must be copied "
        "from the body of its named section. Do not use a quote from one section under another "
        "section label. If any section does not support the claim, return AMBIGUOUS or REVISE "
        "and state why. Do not repair the policy or invent an unstated control. Output candidate_id "
        "exactly as supplied.",
        json.dumps(
            {
                "candidate": {
                    "id": candidate.get("id"),
                    "claim": candidate.get("claim"),
                    "oracle_id": candidate.get("oracle_id"),
                    "material_fields": candidate.get("material_fields"),
                },
                "policy_sections": sections,
            },
            ensure_ascii=False,
        ),
        PolicyEvidenceAudit.model_json_schema(),
    )


async def application_mapper(
    openapi_operations: list[dict[str, Any]], code_catalog: list[dict[str, Any]]
) -> dict[str, Any]:
    return await ask_agent(
        "Application Mapper",
        "Map only successful persisted payment state changes. Use from_state for the state just "
        "before the action and to_state for the state after its successful database mutation. The "
        "known persisted statuses are pending, approved, and executed; create starts from absent. "
        "Keep each handler's change separate: an INSERT that creates a pending row is absent to "
        "pending; a different handler that changes pending to approved is pending to approved. Do "
        "not combine sequential endpoints into one transition. A route ID being present is not a "
        "state-change condition. Do not describe validation errors or failed preconditions as "
        "transitions. For edit_payment, "
        "focus on an already-approved row and include both successful outcomes: the branch that "
        "clears approval and changes status, and the branch that leaves approval/status unchanged. "
        "The clearing branch is guarded by `APP_MODE == \"fixed\"`; include that mode predicate "
        "in its condition, not just the current status. The preserving branch is the path where "
        "that predicate is false. Use the actual mode condition in each condition field, including "
        "the condition-false path when no else branch means state is preserved. Return at least five transitions: one for "
        "create, approve, and execute, plus both approved-payment edit outcomes. Do not decide "
        "whether behavior violates policy. For every "
        "transition, select the matching CODE evidence ID from the catalog. Do not return routes, "
        "methods, source quotes, line numbers, or evidence IDs not in the catalog.",
        json.dumps(
            {"openapi_operations": openapi_operations, "code_catalog": code_catalog},
            ensure_ascii=False,
        ),
        ApplicationMapperResult.model_json_schema(),
    )


async def test_planner(
    invariant: dict[str, Any], app_map: dict[str, Any], feedback: str | None = None
) -> dict[str, Any]:
    instructions = (
        "Using only the supplied candidate invariant and mapped application operations, design "
        "the smallest safe, bounded sequence that could falsify the invariant if the application "
        "does not enforce it. Derive the sequence from the invariant and state transitions; do not "
        "assume a vulnerability or follow a preselected exploit recipe. Use only mapped actions "
        "among create, approve, edit, execute. Use only supported payment fields and small "
        "distinguishable demo values (at most USD 500). For the payment-term oracle, create must "
        "set all three material fields payee, amount, and currency. Include exactly one create, "
        "one approval, at least one edit after approval that changes a material field, and one "
        "execute after the edit. Use edit values that differ from the created values, and keep the "
        "currency USD in every step. Never include payment_id or id in values; the harness obtains "
        "the generated ID from the create response. The complete plan must fit this payment API. "
        "Return a JSON object with keys title, invariant_id, steps (each has action and values), "
        "and expected_violation. No arbitrary URLs, shell commands, external targets, or unlisted "
        "operations."
    )
    if feedback:
        instructions += (
            " Revise the prior plan to satisfy this deterministic validation feedback exactly: "
            + feedback
        )
    return await ask_agent(
        "Test Planner",
        instructions,
        json.dumps({"invariant": invariant, "application_map": app_map}, ensure_ascii=False),
        TestPlan.model_json_schema(),
    )


async def skeptical_reviewer(
    policy: str, app_map: dict[str, Any], invariant: dict[str, Any], plan: dict[str, Any]
) -> dict[str, Any]:
    return await ask_agent(
        "Skeptical Reviewer",
        "Independently verify the candidate, test plan, and verified application map. The policy "
        "evidence has already passed exact-source and independent section checks. Return a compact "
        "JSON object with decision (PASS, REVISE, or AMBIGUOUS), policy_supported (boolean), "
        "plan_tests_invariant (boolean), actions_supported (boolean), fixed_edit_outcome, "
        "vulnerable_edit_outcome, and concise reasons. Read the mode labels carefully: the "
        "verified map's required values are fixed_edit_outcome='approved_to_pending' and "
        "vulnerable_edit_outcome='approved_to_approved'. Do not reverse them. Do not "
        "quote policy or repeat source evidence. Check that the candidate is supported by the "
        "policy; that the plan creates a payment with all material terms, approves it, changes at "
        "least one material term while unsettled, and then attempts settlement; and that every "
        "action exists in the mapped API/state transitions. The policy permits amendments to "
        "approved but unsettled payments. The verified map says an approved-payment edit remains "
        "approved in vulnerable mode and returns to pending in fixed mode, while execution uses "
        "current payment terms. The harness—not the plan—compares the persisted approval snapshot "
        "with the ledger after execution; the plan does not need a separate database-read step. Do "
        "not reject the plan solely because its edit occurs after approval or because it contains "
        "no oracle action. PASS if and only if all three booleans are true, and make decision "
        "consistent with them. Return at most three short reasons. Do not report a vulnerability; "
        "live execution has not happened.",
        json.dumps(
            {"policy": policy, "application_map": app_map, "invariant": invariant, "plan": plan},
            ensure_ascii=False,
        ),
        ReviewDecision.model_json_schema(),
        timeout_seconds=35.0,
    )
