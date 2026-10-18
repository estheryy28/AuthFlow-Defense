from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class EvidenceRef(BaseModel):
    source: str
    quote: str = Field(min_length=4)


class PolicyEvidence(EvidenceRef):
    source: Literal["payment-policy.md"]
    section: Literal["Review record", "Request amendments", "Settlement"]


class CandidateInvariant(BaseModel):
    id: str
    claim: str
    oracle_id: Literal["payment_approval_terms"]
    material_fields: list[str]
    evidence: list[PolicyEvidence] = Field(min_length=3, max_length=3)
    ambiguities: list[str] = []


class PolicyAnalysis(BaseModel):
    invariants: list[CandidateInvariant]


class PolicyEvidenceAudit(BaseModel):
    candidate_id: str
    decision: Literal["PASS", "REVISE", "AMBIGUOUS"]
    reasons: list[str]
    evidence: list[PolicyEvidence]


class Operation(BaseModel):
    action: str
    method: str
    path: str
    reads: list[str]
    writes: list[str]
    evidence: list[EvidenceRef]


class StateTransition(BaseModel):
    action: str
    from_state: str
    to_state: str
    condition: str
    evidence: list[EvidenceRef]


class MapperTransition(BaseModel):
    action: Literal["create", "approve", "edit", "execute"]
    from_state: str
    to_state: str
    condition: str
    evidence_ids: list[str] = Field(min_length=1)


class ApplicationMapperResult(BaseModel):
    state_transitions: list[MapperTransition] = Field(min_length=5, max_length=8)


class ApplicationMap(BaseModel):
    operations: list[Operation]
    state_transitions: list[StateTransition]


class TestStep(BaseModel):
    action: Literal["create", "approve", "edit", "execute"]
    values: dict[str, str | int | Decimal] = Field(default_factory=dict)


class TestPlan(BaseModel):
    title: str
    invariant_id: str
    steps: list[TestStep] = Field(min_length=4, max_length=8)
    expected_violation: str


class ReviewDecision(BaseModel):
    decision: Literal["PASS", "REVISE", "AMBIGUOUS"]
    policy_supported: bool
    plan_tests_invariant: bool
    actions_supported: bool
    fixed_edit_outcome: Literal["approved_to_pending", "incorrect"]
    vulnerable_edit_outcome: Literal["approved_to_approved", "incorrect"]
    reasons: list[str]


class RunRequest(BaseModel):
    mode: Literal["vulnerable", "fixed"] = "vulnerable"
