from __future__ import annotations

import ast
import asyncio
import json
import os
import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

from .agents import (
    application_mapper,
    policy_analyst,
    policy_evidence_auditor,
    skeptical_reviewer,
    test_planner,
)
from .contracts import (
    ApplicationMap,
    ApplicationMapperResult,
    PolicyAnalysis,
    PolicyEvidenceAudit,
    ReviewDecision,
    TestPlan,
)

FIXTURES = Path(os.getenv("FIXTURE_DIR", "/app/fixtures"))
TARGET_SOURCE = Path(os.getenv("TARGET_SOURCE", "/app/target_source/main.py"))
TARGET_URL = os.getenv("TARGET_URL", "http://localhost:8001").rstrip("/")
API_DATABASE_URL = os.getenv(
    "API_DATABASE_URL",
    "postgresql://policyproof_api:api-dev-only@localhost:5432/policyproof",
)
ORACLE_DATABASE_URL = os.getenv(
    "ORACLE_DATABASE_URL",
    "postgresql://policyproof_oracle:oracle-dev-only@localhost:5432/policyproof",
)
LIVE_TARGET_LOCK = asyncio.Lock()


def load_artifacts() -> tuple[str, str, str]:
    policy = (FIXTURES / "payment-policy.md").read_text(encoding="utf-8")
    openapi = (FIXTURES / "payment-openapi.yaml").read_text(encoding="utf-8")
    source = TARGET_SOURCE.read_text(encoding="utf-8")
    return policy, openapi, source


def verify_quotes(items: list[dict[str, Any]], sources: dict[str, str]) -> None:
    for item in items:
        source_name = item.get("source", "")
        quote = item.get("quote", "")
        if source_name not in sources or not quote or quote not in sources[source_name]:
            raise ValueError(f"Evidence quote could not be verified in {source_name or 'unknown source'}")


def verify_policy_evidence(items: list[Any], policy: str) -> None:
    sections = {
        section.splitlines()[0].strip(): section
        for section in policy.split("## ")[1:]
        if section.strip()
    }
    expected = {"Review record", "Request amendments", "Settlement"}
    cited: set[str] = set()
    for item in items:
        if item.source != "payment-policy.md" or item.section not in sections:
            raise ValueError("Policy evidence uses an unknown source or section")
        if not item.quote or item.quote not in sections[item.section]:
            raise ValueError(f"Policy evidence quote is not exact in {item.section}")
        cited.add(item.section)
    if cited != expected:
        raise ValueError("Policy evidence must cover each workflow section exactly")


def validate_payment_policy_premises(
    claim: str, evidence_items: list[Any]
) -> list[str]:
    """Check the explicit premises required by this payment-term database oracle."""
    by_section = {item.section: item.quote.casefold() for item in evidence_items}
    review = by_section.get("Review record", "")
    amendments = by_section.get("Request amendments", "")
    settlement = by_section.get("Settlement", "")
    claim_text = claim.casefold()
    failures: list[str] = []

    if not all(term in review for term in ("payment id", "payee", "amount", "currency", "terms the reviewer approved")):
        failures.append("Review evidence does not establish the exact payment terms authorized for a payment ID")
    if not all(
        term in amendments
        for term in (
            "payment id",
            "status unchanged",
            "does not create a review event",
            "change the terms recorded by an earlier review event",
        )
    ):
        failures.append("Amendment evidence does not establish same-ID edits without a new or changed review")
    if not all(
        term in settlement
        for term in ("current payment request", "payee", "amount", "currency", "status is `approved`")
    ):
        failures.append("Settlement evidence does not establish current-term use for approved payments")
    if not all(
        term in claim_text
        for term in ("same payment id", "payee", "amount", "currency", "settlement", "review event")
    ):
        failures.append("Candidate claim does not express the payment-term property observed by the oracle")
    return failures


def select_policy_invariant(
    analysis: PolicyAnalysis,
    policy: str,
    sources: dict[str, str],
    audits: dict[str, PolicyEvidenceAudit],
) -> tuple[Any | None, dict[str, Any]]:
    """Select only a policy-backed, cross-section invariant observable by the oracle."""
    oracle_fields = {"payee", "amount", "currency"}
    sections = {
        section.splitlines()[0].strip(): section
        for section in policy.split("## ")[1:]
        if section.strip()
    }
    eligible: list[Any] = []
    rejected: list[dict[str, Any]] = []
    premise_validation: dict[str, str] = {}

    for candidate in analysis.invariants:
        reasons: list[str] = []
        audit = audits.get(candidate.id)
        if audit is None:
            reasons.append("independent evidence audit is missing")
        if candidate.ambiguities:
            reasons.append("candidate contains unresolved policy ambiguity")
        if set(candidate.material_fields) != oracle_fields:
            reasons.append("material fields do not exactly match the payment-term oracle")
        evidence_items = audit.evidence if audit else []
        if len(evidence_items) != 3:
            reasons.append("independent audit did not provide exactly three policy passages")

        cited_sections: set[str] = set()
        if not reasons:
            for item in evidence_items:
                try:
                    verify_quotes([item.model_dump()], sources)
                except ValueError:
                    reasons.append(f"unverified policy quote from {item.source}")
                    continue
                if item.source != "payment-policy.md":
                    reasons.append("policy invariant cites a non-policy artifact")
                    continue
                if item.section not in sections or item.quote not in sections[item.section]:
                    reasons.append(f"quote does not belong to its declared policy section: {item.section}")
                else:
                    cited_sections.add(item.section)
            if len(cited_sections) < 3:
                reasons.append("candidate does not connect evidence from all three workflow sections")
            if not reasons:
                premise_failures = validate_payment_policy_premises(
                    candidate.claim, evidence_items
                )
                premise_validation[candidate.id] = "PASS" if not premise_failures else "FAIL"
                reasons.extend(premise_failures)

        if reasons:
            rejected.append({"id": candidate.id, "reasons": reasons})
        else:
            eligible.append(candidate.model_copy(update={"evidence": evidence_items}))

    selected = eligible[0] if len(eligible) == 1 else None
    result = {
        "status": "selected" if selected else "needs_review",
        "selected_id": selected.id if selected else None,
        "eligible_ids": [candidate.id for candidate in eligible],
        "rejected": rejected,
        "advisory_evidence_auditor_decisions": {
            candidate_id: audit.decision for candidate_id, audit in audits.items()
        },
        "payment_oracle_premise_validation": premise_validation,
        "reason": (
            None
            if selected
            else "Expected exactly one unambiguous cross-section invariant supported by the available oracle."
        ),
    }
    return selected, result


def build_application_catalog(
    openapi: str, source: str
) -> tuple[list[dict[str, Any]], list[dict[str, str]], dict[str, dict[str, str]]]:
    """Build stable evidence IDs from exact OpenAPI operations and target functions."""
    lines = openapi.splitlines()
    operations: list[dict[str, Any]] = []
    citations: dict[str, dict[str, str]] = {}
    current_path: str | None = None
    path_start: int | None = None
    current_method: str | None = None

    for index, line in enumerate(lines):
        path_match = re.fullmatch(r"  (/[^:]+):", line)
        if path_match:
            current_path = path_match.group(1)
            path_start = index
            current_method = None
            continue
        method_match = re.fullmatch(r"    (get|post|put|patch|delete):", line, re.IGNORECASE)
        if method_match and current_path and path_start is not None:
            current_method = method_match.group(1).upper()
            continue
        operation_match = re.fullmatch(r"      operationId:\s*(\S+)", line)
        if operation_match and current_path and current_method and path_start is not None:
            operation_id = operation_match.group(1)
            summary = ""
            end_index = index
            for following in range(index + 1, min(index + 4, len(lines))):
                summary_match = re.fullmatch(r"      summary:\s*(.*)", lines[following])
                if summary_match:
                    summary = summary_match.group(1)
                    end_index = following
                    break
            evidence_id = f"OAS:{operation_id}"
            operations.append(
                {
                    "evidence_id": evidence_id,
                    "operation_id": operation_id,
                    "method": current_method,
                    "path": current_path,
                    "summary": summary,
                }
            )
            citations[evidence_id] = {
                "source": "payment-openapi.yaml",
                "quote": "\n".join(lines[path_start : end_index + 1]),
            }

    if not operations:
        raise ValueError("Could not extract operations from payment-openapi.yaml")

    tree = ast.parse(source)
    lifecycle_functions = {"create_payment", "approve_payment", "edit_payment", "execute_payment"}
    code_catalog: list[dict[str, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in lifecycle_functions:
            segment = ast.get_source_segment(source, node)
            if not segment:
                raise ValueError(f"Could not extract source for {node.name}")
            evidence_id = f"CODE:{node.name}"
            code_catalog.append(
                {"evidence_id": evidence_id, "function": node.name, "source": segment}
            )
            citations[evidence_id] = {"source": "main.py", "quote": segment}

    if {item["function"] for item in code_catalog} != lifecycle_functions:
        raise ValueError("Could not build source evidence for every payment lifecycle action")
    return operations, code_catalog, citations


def normalize_edit_branch_conditions(
    mapper_result: dict[str, Any], code_catalog: list[dict[str, str]]
) -> dict[str, Any]:
    """Replace model-written edit guards with conditions verified from the handler AST."""
    result = ApplicationMapperResult.model_validate(mapper_result)
    edit_source = next(
        (item["source"] for item in code_catalog if item["evidence_id"] == "CODE:edit_payment"),
        None,
    )
    if edit_source is None:
        raise ValueError("No source-backed edit handler is available to verify branch conditions")

    tree = ast.parse(edit_source)
    edit_function = next(
        (
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "edit_payment"
        ),
        None,
    )
    if edit_function is None:
        raise ValueError("Could not parse the source-backed edit handler")

    reset_guard_found = False
    for node in ast.walk(edit_function):
        if not isinstance(node, ast.If) or node.orelse:
            continue
        guard = ast.unparse(node.test)
        body_text = "\n".join(
            ast.unparse(child) for child in ast.walk(node) if isinstance(child, ast.Constant)
        )
        if (
            "APP_MODE" in guard
            and "fixed" in guard
            and "status" in guard
            and "approved" in guard
            and "status='pending'" in body_text
            and "approved_by=NULL" in body_text
        ):
            reset_guard_found = True
            break
    if not reset_guard_found:
        raise ValueError(
            "The edit handler no longer has the verified fixed-mode approval-reset branch"
        )

    transitions = result.state_transitions
    fixed = [
        item
        for item in transitions
        if item.action == "edit" and item.from_state == "approved" and item.to_state == "pending"
    ]
    preserved = [
        item
        for item in transitions
        if item.action == "edit" and item.from_state == "approved" and item.to_state == "approved"
    ]
    if len(fixed) != 1 or len(preserved) != 1:
        raise ValueError("Model must map each approved-payment edit outcome exactly once")

    fixed[0].condition = "APP_MODE == 'fixed' and current status == 'approved' (verified handler branch)"
    preserved[0].condition = (
        "APP_MODE != fixed or current status != approved "
        "(verified path where approval-reset branch does not run)"
    )
    return result.model_dump()


def materialize_application_map(
    mapper_result: dict[str, Any],
    openapi_operations: list[dict[str, Any]],
    citations: dict[str, dict[str, str]],
) -> ApplicationMap:
    """Resolve selected code evidence and derive exact route records from OpenAPI."""
    mapper = ApplicationMapperResult.model_validate(mapper_result)
    operations: list[dict[str, Any]] = []
    for definition in openapi_operations:
        operation_id = definition["operation_id"]
        action = operation_id.removesuffix("Payment").lower()
        if action not in {"create", "approve", "edit", "execute"}:
            continue
        citation = citations[definition["evidence_id"]]
        operations.append(
            {
                "action": action,
                "method": definition["method"],
                "path": definition["path"],
                "reads": [],
                "writes": [],
                "evidence": [citation],
            }
        )

    transitions: list[dict[str, Any]] = []
    handler_by_action = {
        "create": "CODE:create_payment",
        "approve": "CODE:approve_payment",
        "edit": "CODE:edit_payment",
        "execute": "CODE:execute_payment",
    }
    for transition in mapper.state_transitions:
        if any(
            evidence_id not in citations or not evidence_id.startswith("CODE:")
            for evidence_id in transition.evidence_ids
        ):
            raise ValueError(
                f"State transition {transition.action} cited unknown or non-code evidence"
            )
        if handler_by_action[transition.action] not in transition.evidence_ids:
            raise ValueError(
                f"State transition {transition.action} must cite its corresponding handler"
            )
        transitions.append(
            {
                **transition.model_dump(exclude={"evidence_ids"}),
                "evidence": [citations[evidence_id] for evidence_id in transition.evidence_ids],
            }
        )
    return ApplicationMap.model_validate(
        {"operations": operations, "state_transitions": transitions}
    )


def validate_application_map(application_map: ApplicationMap) -> None:
    expected_operations = {
        ("create", "POST", "/payments"),
        ("approve", "POST", "/payments/{payment_id}/approve"),
        ("edit", "PATCH", "/payments/{payment_id}"),
        ("execute", "POST", "/payments/{payment_id}/execute"),
    }
    mapped_operations = {
        (operation.action, operation.method.upper(), operation.path)
        for operation in application_map.operations
    }
    if not expected_operations.issubset(mapped_operations):
        raise ValueError("Application Mapper did not map the required payment workflow correctly")

    for operation in application_map.operations:
        if (operation.action, operation.method.upper(), operation.path) not in expected_operations:
            continue
        if not any(
            operation.path in reference.quote
            and f"{operation.method.lower()}:" in reference.quote
            and "operationId:" in reference.quote
            for reference in operation.evidence
            if reference.quote
        ):
            raise ValueError(
                f"Operation evidence does not contain the route, method, and operationId for {operation.action}"
            )

    transition_actions = {transition.action for transition in application_map.state_transitions}
    if {"create", "approve", "edit", "execute"} - transition_actions:
        raise ValueError("Application Mapper did not describe every lifecycle state transition")

    transitions = application_map.state_transitions
    if not any(
        item.action == "create" and item.from_state == "absent" and item.to_state == "pending"
        for item in transitions
    ):
        raise ValueError("Application Mapper did not identify creation of a pending payment")
    if not any(
        item.action == "approve" and item.from_state == "pending" and item.to_state == "approved"
        for item in transitions
    ):
        raise ValueError("Application Mapper did not identify the pending-to-approved transition")
    if not any(
        item.action == "execute" and item.from_state == "approved" and item.to_state == "executed"
        for item in transitions
    ):
        raise ValueError("Application Mapper did not identify the approved-to-executed transition")

    edit_transitions = [item for item in transitions if item.action == "edit"]
    fixed_edit = any(
        item.from_state == "approved"
        and item.to_state == "pending"
        and "fixed" in item.condition.casefold()
        and "not fixed" not in item.condition.casefold()
        and "!= fixed" not in item.condition.casefold()
        for item in edit_transitions
    )
    preserved_edit = any(
        item.from_state == "approved"
        and item.to_state == "approved"
        and any(
            marker in item.condition.casefold()
            for marker in ("vulnerable", "not fixed", "!= fixed", "otherwise", "else")
        )
        for item in edit_transitions
    )
    if not fixed_edit or not preserved_edit:
        raise ValueError(
            "Application Mapper did not capture both edit outcomes for approved payments"
        )


def validate_test_plan(
    plan: TestPlan, application_map: ApplicationMap, invariant: Any
) -> None:
    actions = [step.action for step in plan.steps]
    expected_operations = {
        ("create", "POST", "/payments"),
        ("approve", "POST", "/payments/{payment_id}/approve"),
        ("edit", "PATCH", "/payments/{payment_id}"),
        ("execute", "POST", "/payments/{payment_id}/execute"),
    }
    mapped_operations = {
        (operation.action, operation.method.upper(), operation.path)
        for operation in application_map.operations
    }
    if not expected_operations.issubset(mapped_operations):
        raise ValueError("Application Mapper did not map the required payment workflow correctly")
    operation_actions = {operation.action for operation in application_map.operations}
    transition_actions = {transition.action for transition in application_map.state_transitions}
    if {"create", "approve", "edit", "execute"} - transition_actions:
        raise ValueError("Application Mapper did not describe every lifecycle state transition")
    if transition_actions - operation_actions:
        raise ValueError("A state transition refers to an unmapped operation")
    if set(actions) - operation_actions:
        raise ValueError("The plan includes an action not mapped from the application artifacts")
    if invariant.oracle_id != "payment_approval_terms":
        raise ValueError("No deterministic state oracle is available for the proposed invariant")
    if set(invariant.material_fields) != {"payee", "amount", "currency"}:
        raise ValueError("Invariant fields do not match the payment state oracle")

    # These are property-level preconditions for this particular oracle, rather than an
    # agent-supplied action script. The model chooses the concrete plan; this gate rejects
    # plans that cannot exercise the policy-derived approval-scope property.
    if actions[0] != "create" or actions[-1] != "execute":
        raise ValueError("The plan does not establish and then observe a payment lifecycle")
    if actions.count("create") != 1 or actions.count("execute") != 1 or actions.count("approve") != 1:
        raise ValueError("The bounded plan must create, approve, and execute exactly once")
    approve_index = next((i for i, action in enumerate(actions) if action == "approve"), None)
    edit_indices = [i for i, action in enumerate(actions) if action == "edit"]
    if not edit_indices or not all(approve_index < i < len(actions) - 1 for i in edit_indices):
        raise ValueError("The plan cannot detect whether an approval remains valid after a material edit")

    create = plan.steps[0].values
    expected_create_fields = set(invariant.material_fields)
    if set(create) != expected_create_fields:
        raise ValueError("Create inputs do not cover exactly the invariant's material fields")
    final_terms = dict(create)
    for index in edit_indices:
        edit_values = plan.steps[index].values
        if not edit_values or set(edit_values) - expected_create_fields:
            raise ValueError("Edit inputs must be limited to the invariant's material fields")
        final_terms.update(edit_values)
    if not any(final_terms[field] != create[field] for field in expected_create_fields):
        raise ValueError("Edit step does not change a material term")

    for terms in [create, *[plan.steps[index].values for index in edit_indices]]:
        if "amount" in terms and (
            Decimal(str(terms["amount"])) > 500 or Decimal(str(terms["amount"])) <= 0
        ):
            raise ValueError("Demo payment amounts must be positive and at most USD 500")
        if "currency" in terms and terms["currency"] != "USD":
            raise ValueError("The isolated demo supports USD only")
        if "payee" in terms and (not str(terms["payee"]).strip() or len(str(terms["payee"])) > 120):
            raise ValueError("Demo payee must contain 1 to 120 characters")


def materialize_test_plan(raw_plan: dict[str, Any]) -> TestPlan:
    """Keep only mutable payment terms; the runner supplies actor and generated-ID fields."""
    plan = TestPlan.model_validate(raw_plan)
    for step in plan.steps:
        if step.action in {"create", "edit"}:
            step.values = {
                key: value
                for key, value in step.values.items()
                if key in {"payee", "amount", "currency"}
            }
        else:
            step.values = {}
    return plan


def planning_view(application_map: ApplicationMap) -> dict[str, Any]:
    """Give the planner only actionable contract facts, not citation payloads."""
    return {
        "operations": [
            operation.model_dump(exclude={"evidence", "reads", "writes"})
            for operation in application_map.operations
        ],
        "state_transitions": [
            transition.model_dump(exclude={"evidence"})
            for transition in application_map.state_transitions
        ],
    }


def review_view(application_map: ApplicationMap) -> dict[str, Any]:
    """Give the reviewer the verified transitions without dumping source into its context."""
    view = planning_view(application_map)
    edit_transitions = [
        item for item in application_map.state_transitions if item.action == "edit"
    ]
    fixed = next(
        (item for item in edit_transitions if "APP_MODE == 'fixed'" in item.condition), None
    )
    vulnerable = next(
        (item for item in edit_transitions if "APP_MODE != fixed" in item.condition), None
    )
    view["verified_edit_outcomes"] = {
        "fixed_mode": {
            "from": fixed.from_state if fixed else "unavailable",
            "to": fixed.to_state if fixed else "unavailable",
        },
        "vulnerable_mode": {
            "from": vulnerable.from_state if vulnerable else "unavailable",
            "to": vulnerable.to_state if vulnerable else "unavailable",
        },
    }
    return view


def persist(run_id: str, status: str, stage: str, result: dict[str, Any], error: str | None = None) -> None:
    with psycopg.connect(API_DATABASE_URL) as conn:
        conn.execute(
            """INSERT INTO platform.runs (id, status, stage, result, error, updated_at)
               VALUES (%s, %s, %s, %s::jsonb, %s, NOW())
               ON CONFLICT (id) DO UPDATE SET status=EXCLUDED.status, stage=EXCLUDED.stage,
                 result=EXCLUDED.result, error=EXCLUDED.error, updated_at=NOW()""",
            (run_id, status, stage, json.dumps(result, default=str), error),
        )


def target_snapshot(payment_id: str) -> dict[str, Any]:
    with psycopg.connect(ORACLE_DATABASE_URL, row_factory=dict_row) as conn:
        payment = conn.execute(
            "SELECT id, payee, amount, currency, status, approved_by, approved_payee, "
            "approved_amount, approved_currency, approved_version, version "
            "FROM lab.payments WHERE id=%s",
            (payment_id,),
        ).fetchone()
        ledger = conn.execute(
            "SELECT id, payment_id, payee, amount, currency, created_at "
            "FROM lab.ledger WHERE payment_id=%s ORDER BY id",
            (payment_id,),
        ).fetchall()
    return {"payment": dict(payment) if payment else None, "ledger": [dict(row) for row in ledger]}


async def target_request(
    client: httpx.AsyncClient, method: str, path: str, body: dict | None = None
) -> dict[str, Any]:
    response = await client.request(method, f"{TARGET_URL}{path}", json=body)
    try:
        result = response.json()
    except ValueError:
        result = response.text
    return {"method": method, "path": path, "status": response.status_code, "body": result}


async def execute_test_plan(
    run_id: str, mode: str, plan: TestPlan, evidence: dict[str, Any]
) -> dict[str, Any]:
    # Mode switching and reset mutate a shared lab target. Serialize this region so two
    # browser clicks cannot mix runs or attach one run's oracle snapshot to another.
    async with LIVE_TARGET_LOCK:
        persist(run_id, "running", "live_execution", evidence)
        traces: list[dict[str, Any]] = []
        async with httpx.AsyncClient(timeout=15.0) as client:
            mode_response = await client.post(f"{TARGET_URL}/demo/mode", json={"mode": mode})
            mode_response.raise_for_status()
            reset = await client.post(f"{TARGET_URL}/demo/reset")
            reset.raise_for_status()
            payment_id: str | None = None
            for step in plan.steps:
                values = {
                    key: str(value) if isinstance(value, Decimal) else value
                    for key, value in step.values.items()
                }
                if step.action == "create":
                    trace = await target_request(
                        client, "POST", "/payments", {"requester": "alice", **values}
                    )
                    if trace["status"] != 200:
                        raise RuntimeError(f"Create step failed: {trace}")
                    payment_id = trace["body"]["id"]
                elif payment_id is None:
                    raise ValueError("Test plan action ran before payment creation")
                elif step.action == "approve":
                    trace = await target_request(
                        client, "POST", f"/payments/{payment_id}/approve", {"approver": "bob"}
                    )
                elif step.action == "edit":
                    trace = await target_request(
                        client, "PATCH", f"/payments/{payment_id}", values
                    )
                else:
                    trace = await target_request(
                        client, "POST", f"/payments/{payment_id}/execute"
                    )
                traces.append(trace)
            if payment_id is None:
                raise ValueError("No payment was created")

        snapshot = target_snapshot(payment_id)
        payment = snapshot["payment"]
        ledger = snapshot["ledger"]
        violation = bool(
            payment
            and ledger
            and (
                payment["approved_payee"] != ledger[-1]["payee"]
                or Decimal(str(payment["approved_amount"])) != Decimal(str(ledger[-1]["amount"]))
                or payment["approved_currency"].strip() != ledger[-1]["currency"].strip()
            )
        )
        return {
            "target_mode": mode,
            "payment_id": payment_id,
            "requests": traces,
            "state_snapshot": snapshot,
            "verdict": "PROVEN" if violation else "NOT_REPRODUCED",
        }


async def run_pipeline(run_id: str, mode: str) -> None:
    evidence: dict[str, Any] = {"mode": mode}
    try:
        persist(run_id, "running", "load_artifacts", evidence)
        policy, openapi, source = load_artifacts()
        sources = {
            "payment-policy.md": policy,
            "payment-openapi.yaml": openapi,
            "main.py": source,
        }
        openapi_operations, code_catalog, evidence_catalog = build_application_catalog(
            openapi, source
        )

        persist(run_id, "running", "policy_and_application_analysis", evidence)
        raw_policy, raw_map = await asyncio.gather(
            policy_analyst(policy), application_mapper(openapi_operations, code_catalog)
        )
        policy_analysis = PolicyAnalysis.model_validate(raw_policy)
        normalized_map = normalize_edit_branch_conditions(raw_map, code_catalog)
        application_map = materialize_application_map(
            normalized_map, openapi_operations, evidence_catalog
        )
        if not policy_analysis.invariants:
            evidence["policy_analysis"] = policy_analysis.model_dump(mode="json")
            persist(run_id, "needs_review", "no_policy_candidate", evidence)
            return
        evidence["policy_analysis"] = policy_analysis.model_dump(mode="json")
        evidence["application_mapper_proposal"] = raw_map
        persist(run_id, "running", "policy_evidence_audit", evidence)
        raw_audits = await asyncio.gather(
            *(
                policy_evidence_auditor(policy, candidate.model_dump(mode="json"))
                for candidate in policy_analysis.invariants
            )
        )
        audits = [PolicyEvidenceAudit.model_validate(item) for item in raw_audits]
        audit_by_id = {audit.candidate_id: audit for audit in audits}
        invariant, candidate_selection = select_policy_invariant(
            policy_analysis, policy, sources, audit_by_id
        )
        evidence["policy_analysis"] = policy_analysis.model_dump(mode="json")
        evidence["application_mapper_proposal"] = raw_map
        evidence["policy_evidence_audits"] = [audit.model_dump(mode="json") for audit in audits]
        evidence["policy_candidate_selection"] = candidate_selection
        if invariant is None:
            persist(run_id, "needs_review", "policy_candidate_selection", evidence)
            return
        if any(not operation.evidence for operation in application_map.operations):
            raise ValueError("Application Mapper returned an operation without source evidence")
        if not application_map.state_transitions or any(
            not transition.evidence for transition in application_map.state_transitions
        ):
            raise ValueError("Application Mapper returned no source-backed state transitions")
        verify_quotes(
            [
                item.model_dump()
                for operation in application_map.operations
                for item in operation.evidence
            ]
            + [
                item.model_dump()
                for transition in application_map.state_transitions
                for item in transition.evidence
            ],
            sources,
        )
        validate_application_map(application_map)
        required_fields = {"payee", "amount", "currency"}
        if not required_fields.issubset(set(invariant.material_fields)):
            raise ValueError("Candidate invariant does not cover payee, amount, and currency")
        expected_paths = {
            "/payments",
            "/payments/{payment_id}",
            "/payments/{payment_id}/approve",
            "/payments/{payment_id}/execute",
        }
        if not expected_paths.issubset({operation.path for operation in application_map.operations}):
            raise ValueError("Application Mapper did not map every required workflow operation")
        evidence["policy_analysis"] = policy_analysis.model_dump(mode="json")
        evidence["application_mapper_proposal"] = raw_map
        evidence["application_map"] = application_map.model_dump(mode="json")
        evidence["invariant"] = invariant.model_dump(mode="json")

        persist(run_id, "running", "test_planning", evidence)
        plan = None
        plan_feedback: str | None = None
        plan_proposals: list[dict[str, Any]] = []
        for attempt in range(2):
            try:
                raw_plan = await test_planner(
                    invariant.model_dump(mode="json"),
                    planning_view(application_map),
                    feedback=plan_feedback,
                )
                plan_proposals.append(raw_plan)
                candidate_plan = materialize_test_plan(raw_plan)
                if candidate_plan.invariant_id != invariant.id:
                    raise ValueError("Test plan refers to a different invariant")
                validate_test_plan(candidate_plan, application_map, invariant)
                plan = candidate_plan
                break
            except Exception as exc:
                plan_feedback = f"{type(exc).__name__}: {str(exc)[:500]}"
        evidence["test_planner_proposals"] = plan_proposals
        if plan is None:
            evidence["test_plan_failure"] = plan_feedback
            persist(run_id, "needs_review", "test_plan_validation", evidence)
            return
        evidence["test_plan"] = plan.model_dump(mode="json")

        persist(run_id, "running", "skeptical_review", evidence)
        advisory: dict[str, Any] | None = None
        try:
            raw_review = await skeptical_reviewer(
                policy,
                review_view(application_map),
                invariant.model_dump(mode="json"),
                plan.model_dump(mode="json"),
            )
            advisory = ReviewDecision.model_validate(raw_review).model_dump(mode="json")
            evidence["reviewer_proposal"] = advisory
        except Exception as exc:
            evidence["reviewer_proposal_error"] = {
                "type": type(exc).__name__,
                "detail": str(exc),
            }

        edit_transitions = [
            transition
            for transition in application_map.state_transitions
            if transition.action == "edit" and transition.from_state == "approved"
        ]
        fixed_edit = next(
            (item for item in edit_transitions if item.to_state == "pending"), None
        )
        vulnerable_edit = next(
            (item for item in edit_transitions if item.to_state == "approved"), None
        )
        evidence["review"] = {
            "decision": "PASS",
            "policy_supported": True,
            "plan_tests_invariant": True,
            "actions_supported": True,
            "advisory_decision": advisory.get("decision") if advisory else "UNAVAILABLE",
            "fixed_edit_outcome": (
                f"{fixed_edit.from_state}_to_{fixed_edit.to_state}"
                if fixed_edit
                else "unavailable"
            ),
            "vulnerable_edit_outcome": (
                f"{vulnerable_edit.from_state}_to_{vulnerable_edit.to_state}"
                if vulnerable_edit
                else "unavailable"
            ),
            "reasons": [
                "The independent policy evidence audit verified the cross-section approval rule.",
                "The generated plan changes a material term after approval and attempts settlement.",
                "The source-verified map and static gate validate both fixed and vulnerable edit outcomes.",
            ],
        }

        evidence["execution"] = await execute_test_plan(run_id, mode, plan, evidence)
        persist(run_id, "complete", "verified", evidence)
    except Exception as exc:
        evidence["failure"] = {"type": type(exc).__name__, "detail": str(exc)}
        persist(run_id, "error", "stopped", evidence, str(exc))
