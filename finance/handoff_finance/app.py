from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Literal

from dotenv import dotenv_values
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .airwallex import AirwallexClient, AirwallexError
from .engine import Planner, improve
from .fixtures import build_case, cases
from .model_client import OpenAIPlanner
from .models import InvoiceDraft, InvoiceRequest
from .rules import validate_invoice

try:
    from .telemetry import trace_scope
except ImportError:  # pragma: no cover - only supports partial local checkouts
    trace_scope = None


ROOT = Path(__file__).resolve().parents[2]
STATIC_INDEX = ROOT / "finance" / "static" / "index.html"
DEFAULT_EVALUATION_DIR = ROOT / "finance" / "runs"
TERMINAL_STATES = {"completed", "failed", "cancelled"}


class StrictInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProposalInput(StrictInput):
    objective: str = Field(min_length=1, max_length=2_000)
    case_name: str = Field(default="showcase", min_length=1, max_length=100)
    fault_injection: StrictBool = False


class CreateInvoiceInput(StrictInput):
    proposal_id: str = Field(min_length=1, max_length=100)


class VoiceStatusInput(StrictInput):
    connected: StrictBool
    room_name: str = Field(min_length=1, max_length=200)
    human_microphones: StrictInt = Field(ge=0, le=100)
    model: str = Field(min_length=1, max_length=200)
    error: str | None = Field(default=None, max_length=500)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _default_config() -> dict[str, Any]:
    env_file = ROOT / ".env.local"
    local = dotenv_values(env_file) if env_file.exists() else {}

    def configured(name: str) -> bool:
        return bool(os.getenv(name) or local.get(name))

    return {
        "openai_ready": configured("OPENAI_API_KEY"),
        "langfuse_ready": configured("LANGFUSE_PUBLIC_KEY") and configured("LANGFUSE_SECRET_KEY"),
        "livekit_configured": configured("LIVEKIT_URL") and configured("LIVEKIT_TOKEN"),
        "evaluation_dir": DEFAULT_EVALUATION_DIR,
    }


def _airwallex_client() -> AirwallexClient:
    bundled = ROOT / ".tools" / "airwallex"
    cli_path = os.getenv("AIRWALLEX_CLI_PATH") or shutil.which("airwallex")
    return AirwallexClient(cli_path=cli_path or bundled)


class FinanceState:
    def __init__(
        self,
        *,
        planner_factory: Callable[[], Planner],
        airwallex_client: AirwallexClient,
        config: dict[str, Any],
    ) -> None:
        self.planner_factory = planner_factory
        self.airwallex = airwallex_client
        self.config = config
        self.proposals: dict[str, dict[str, Any]] = {}
        self.requests: dict[str, InvoiceRequest] = {}
        self.current_id: str | None = None
        self.events: list[dict[str, Any]] = []
        self.voice_status: dict[str, Any] | None = None
        self.voice_updated_at: float | None = None
        self.create_lock = asyncio.Lock()
        self.demo_mapping: dict[str, str] | None = None
        self.billing_writes: dict[str, dict[str, Any]] = {}

    def event(self, proposal_id: str | None, event_type: str, message: str) -> None:
        self.events.append(
            {
                "id": uuid.uuid4().hex,
                "proposal_id": proposal_id,
                "type": event_type,
                "message": message,
                "time": _now(),
            }
        )
        self.events = self.events[-100:]

    def public(self, proposal_id: str) -> dict[str, Any]:
        return copy.deepcopy(self.proposals[proposal_id])

    def require(self, proposal_id: str) -> dict[str, Any]:
        if proposal_id not in self.proposals:
            raise HTTPException(status_code=404, detail="proposal_not_found")
        return self.proposals[proposal_id]

    def require_current(self, proposal_id: str) -> dict[str, Any]:
        proposal = self.require(proposal_id)
        if self.current_id != proposal_id or proposal["status"] == "cancelled":
            raise HTTPException(status_code=409, detail="proposal_not_current")
        return proposal

    def new_proposal(
        self,
        *,
        objective: str,
        case_name: str,
        fault_injection: bool,
        request: InvoiceRequest | None = None,
        approved: bool = False,
    ) -> dict[str, Any]:
        if self.current_id and self.proposals[self.current_id]["status"] in {
            "checking",
            "creating",
        }:
            raise HTTPException(status_code=409, detail="proposal_in_progress")
        try:
            source_request = request or build_case(case_name)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="unknown_case") from exc

        if self.current_id:
            current = self.proposals[self.current_id]
            if current["status"] not in TERMINAL_STATES:
                current["status"] = "cancelled"
                current["error"] = "superseded_by_new_proposal"
                self.event(current["id"], "proposal_cancelled", "Superseded by a new proposal.")

        proposal_id = uuid.uuid4().hex
        if request is None:
            proposed = source_request.proposed if fault_injection else None
            source_request = source_request.model_copy(
                update={"request_id": proposal_id, "objective": objective, "proposed": proposed},
                deep=True,
            )
            if self.demo_mapping:
                customer_id = self.demo_mapping["customer_id"]
                product_id = self.demo_mapping["product_id"]
                source_request = source_request.model_copy(
                    update={
                        "customer_id": customer_id,
                        "product_id": product_id,
                        "contracts": [
                            contract.model_copy(
                                update={"customer_id": customer_id, "product_id": product_id}
                            )
                            for contract in source_request.contracts
                        ],
                        "fulfilment": source_request.fulfilment.model_copy(
                            update={"customer_id": customer_id, "product_id": product_id}
                        ),
                        "proposed": (
                            source_request.proposed.model_copy(
                                update={"customer_id": customer_id, "product_id": product_id}
                            )
                            if source_request.proposed
                            else None
                        ),
                    },
                    deep=True,
                )
        else:
            source_request = source_request.model_copy(
                update={"request_id": proposal_id, "objective": objective}, deep=True
            )

        proposal = {
            "id": proposal_id,
            "status": "pending",
            "objective": objective,
            "case_name": case_name,
            "fault_injection": fault_injection,
            "approved": approved,
            "decision": None,
            "invoice": None,
            "error": None,
            "request": source_request.model_dump(mode="json"),
            "created_at": _now(),
            "updated_at": _now(),
        }
        self.proposals[proposal_id] = proposal
        self.requests[proposal_id] = source_request
        self.current_id = proposal_id
        self.event(proposal_id, "proposal_created", "Invoice workflow proposed.")
        return proposal


async def _run_checks(state: FinanceState, proposal: dict[str, Any]) -> dict[str, Any]:
    proposal["status"] = "checking"
    proposal["updated_at"] = _now()
    state.event(proposal["id"], "approval_recorded", "Human workflow approval recorded.")
    try:
        planner = state.planner_factory()
        decision = await improve(state.requests[proposal["id"]], planner)
    except Exception:
        proposal["status"] = "needs_review"
        proposal["error"] = "provider_not_configured"
        proposal["updated_at"] = _now()
        state.event(proposal["id"], "checks_failed", "The model provider is not configured.")
        return state.public(proposal["id"])

    decision_json = decision.model_dump(mode="json")
    proposal["decision"] = decision_json
    proposal["error"] = decision.error
    selected = decision.selected
    verified = selected is not None and validate_invoice(state.requests[proposal["id"]], selected).valid
    proposal["status"] = "ready" if verified else "needs_review"
    proposal["updated_at"] = _now()
    state.event(
        proposal["id"],
        "checks_completed",
        "Selected invoice passed every source-backed check."
        if verified
        else "The evidence or proposed invoice needs human review.",
    )
    return state.public(proposal["id"])


def _latest_evaluation(config: dict[str, Any]) -> dict[str, Any]:
    explicit = config.get("evaluation_path")
    if explicit:
        candidates = [Path(explicit)]
    else:
        directory = Path(config.get("evaluation_dir", DEFAULT_EVALUATION_DIR))
        candidates = sorted(directory.glob("evaluation-*.json"), key=lambda path: path.stat().st_mtime)
    if not candidates or not candidates[-1].exists():
        return {"available": False, "report": None, "error": "report_not_found"}
    try:
        report = json.loads(candidates[-1].read_text())
    except (OSError, json.JSONDecodeError):
        return {"available": False, "report": None, "error": "report_unreadable"}
    return {"available": True, "report": report, "error": None}


def create_app(
    *,
    planner_factory: Callable[[], Planner] | None = None,
    airwallex_client: AirwallexClient | None = None,
    config: dict[str, Any] | None = None,
) -> FastAPI:
    resolved_config = {**_default_config(), **(config or {})}
    resolved_config["telemetry_enabled"] = (
        bool(resolved_config.get("telemetry_enabled", True)) if config is None else bool(config.get("telemetry_enabled", False))
    )
    state = FinanceState(
        planner_factory=planner_factory or OpenAIPlanner,
        airwallex_client=airwallex_client or _airwallex_client(),
        config=resolved_config,
    )
    application = FastAPI(title="Handoff Finance", version="0.1.0")
    application.state.finance = state

    @application.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_INDEX)

    @application.get("/health")
    @application.get("/v1/health")
    async def health() -> dict[str, Any]:
        try:
            airwallex = await state.airwallex.readiness()
        except (FileNotFoundError, PermissionError):
            airwallex = {
                "ready": False,
                "authenticated": False,
                "environment": "unknown",
                "error": "cli_not_found",
            }
        except Exception:
            airwallex = {
                "ready": False,
                "authenticated": False,
                "environment": "unknown",
                "error": "readiness_failed",
            }

        age = None if state.voice_updated_at is None else time.monotonic() - state.voice_updated_at
        stale = age is None or age > 15
        voice = copy.deepcopy(state.voice_status) or {
            "connected": False,
            "room_name": "handoff-finance",
            "human_microphones": 0,
            "model": "unreported",
            "error": "no_heartbeat",
        }
        voice["configured"] = bool(state.config.get("livekit_configured"))
        voice["stale"] = stale
        if stale:
            voice["connected"] = False
        return {
            "ok": True,
            "services": {
                "openai": {"ready": bool(state.config.get("openai_ready"))},
                "airwallex": airwallex,
                "langfuse": {"ready": bool(state.config.get("langfuse_ready"))},
                "livekit": voice,
            },
        }

    @application.get("/v1/cases")
    async def fixture_cases() -> list[dict[str, Any]]:
        return [
            {
                "name": name,
                "objective": request.objective,
                "review_expected": name != "showcase",
                "synthetic": True,
            }
            for name, request in cases().items()
        ]

    @application.get("/v1/evaluation/latest")
    async def evaluation_latest() -> dict[str, Any]:
        return _latest_evaluation(state.config)

    @application.get("/v1/proposals/current")
    async def current_proposal() -> dict[str, Any] | None:
        return state.public(state.current_id) if state.current_id else None

    @application.get("/v1/proposals/{proposal_id}")
    async def get_proposal(proposal_id: str) -> dict[str, Any]:
        state.require(proposal_id)
        return state.public(proposal_id)

    @application.post("/v1/proposals")
    async def propose(body: ProposalInput) -> dict[str, Any]:
        proposal = state.new_proposal(
            objective=body.objective,
            case_name=body.case_name,
            fault_injection=body.fault_injection,
        )
        return state.public(proposal["id"])

    @application.post("/v1/proposals/{proposal_id}/approve")
    async def approve(proposal_id: str) -> dict[str, Any]:
        proposal = state.require_current(proposal_id)
        if proposal["status"] != "pending":
            if proposal["approved"]:
                return state.public(proposal_id)
            raise HTTPException(status_code=409, detail="proposal_not_pending")
        proposal["approved"] = True
        return await _run_checks(state, proposal)

    @application.post("/v1/proposals/{proposal_id}/cancel")
    async def cancel(proposal_id: str) -> dict[str, Any]:
        proposal = state.require_current(proposal_id)
        if proposal["status"] not in {"pending", "ready", "needs_review"}:
            raise HTTPException(status_code=409, detail="proposal_cannot_be_cancelled")
        proposal["status"] = "cancelled"
        proposal["error"] = "cancelled_by_human"
        proposal["updated_at"] = _now()
        state.event(proposal_id, "proposal_cancelled", "Invoice workflow cancelled.")
        return state.public(proposal_id)

    @application.post("/v1/improve")
    async def improve_direct(request: InvoiceRequest) -> dict[str, Any]:
        proposal = state.new_proposal(
            objective=request.objective,
            case_name="direct",
            fault_injection=request.proposed is not None,
            request=request,
            approved=True,
        )
        return await _run_checks(state, proposal)

    @application.post("/v1/invoices/create")
    async def create_invoice(body: CreateInvoiceInput) -> dict[str, Any]:
        async with state.create_lock:
            proposal = state.require_current(body.proposal_id)
            if proposal["status"] in {"completed", "failed"} and proposal["invoice"] is not None:
                return state.public(body.proposal_id)
            if not proposal["approved"] or proposal["status"] != "ready":
                raise HTTPException(status_code=409, detail="approved_verified_proposal_required")
            billing_reference = state.requests[body.proposal_id].billing_reference
            previous_write = state.billing_writes.get(billing_reference)
            if previous_write and previous_write["proposal_id"] != body.proposal_id:
                raise HTTPException(status_code=409, detail="billing_reference_already_written")
            decision = proposal.get("decision") or {}
            selected_data = decision.get("selected")
            if not isinstance(selected_data, dict):
                raise HTTPException(status_code=409, detail="verified_selection_required")
            try:
                selected = InvoiceDraft.model_validate(selected_data)
            except Exception as exc:
                raise HTTPException(status_code=409, detail="stored_selection_invalid") from exc
            gate = validate_invoice(state.requests[body.proposal_id], selected)
            if not gate.valid:
                proposal["status"] = "needs_review"
                proposal["error"] = "server_revalidation_failed"
                state.event(body.proposal_id, "revalidation_failed", "Stored selection failed revalidation.")
                raise HTTPException(status_code=409, detail="server_revalidation_failed")

            proposal["status"] = "creating"
            proposal["updated_at"] = _now()
            state.event(body.proposal_id, "invoice_creating", "Creating an Airwallex sandbox draft.")
            trace = None
            if trace_scope is not None and state.config.get("telemetry_enabled"):
                trace = trace_scope(
                    "create-verified-invoice",
                    {"proposal_id": body.proposal_id, "request_id": body.proposal_id},
                    metadata={
                        "request_id": body.proposal_id,
                        "proposal_id": body.proposal_id,
                        "decision_trace_id": decision.get("trace_id"),
                    },
                    session_id=body.proposal_id,
                )
            try:
                if trace is None:
                    result = await state.airwallex.create_verified_invoice(
                        state.requests[body.proposal_id], selected
                    )
                else:
                    with trace as parent:
                        with trace_scope(
                            "run-airwallex-invoice-workflow", {"proposal_id": body.proposal_id}
                        ) as create_span:
                            result = await state.airwallex.create_verified_invoice(
                                state.requests[body.proposal_id], selected
                            )
                            create_span.set_output(result)
                        with trace_scope(
                            "summarize-airwallex-readback", {"proposal_id": body.proposal_id}
                        ) as readback_span:
                            readback_span.set_output(
                                {
                                    "invoice_id": result.get("invoice_id"),
                                    "verified": result.get("verified") is True,
                                    "stage": result.get("stage"),
                                }
                            )
                        parent.set_output(result)
                    result["creation_trace_status"] = parent.status
                    result["creation_trace_id"] = parent.trace_id
                    result["creation_trace_url"] = parent.trace_url
            except Exception:
                result = {
                    "status": "failed",
                    "invoice_id": None,
                    "state": None,
                    "stage": "adapter_failed",
                    "verified": False,
                    "checks": {},
                    "error": "airwallex_adapter_failed",
                    "environment": "sandbox",
                }

            proposal["invoice"] = result
            if result.get("verified") is True or result.get("invoice_id") or result.get("status") in {
                "unknown",
                "partial",
            }:
                state.billing_writes[billing_reference] = {
                    "proposal_id": body.proposal_id,
                    "invoice_id": result.get("invoice_id"),
                    "stage": result.get("stage"),
                }
            proposal["status"] = "completed" if result.get("verified") is True else "failed"
            proposal["error"] = result.get("error")
            proposal["updated_at"] = _now()
            state.event(
                body.proposal_id,
                "invoice_verified" if result.get("verified") is True else "invoice_failed",
                "Sandbox draft readback matches the approved invoice."
                if result.get("verified") is True
                else "Sandbox draft was not fully verified.",
            )
            return state.public(body.proposal_id)

    @application.post("/v1/demo/setup")
    async def setup_demo() -> dict[str, Any]:
        try:
            mapping = await state.airwallex.ensure_demo_objects()
        except (AirwallexError, FileNotFoundError, PermissionError) as exc:
            code = exc.code if isinstance(exc, AirwallexError) else "cli_not_found"
            raise HTTPException(status_code=503, detail=code) from exc
        state.demo_mapping = dict(mapping)
        state.event(None, "demo_setup_verified", "Synthetic Airwallex customer and product verified.")
        return {"verified": True, "mapping": mapping, "environment": "sandbox"}

    @application.post("/v1/voice/status")
    async def voice_status(body: VoiceStatusInput) -> dict[str, Any]:
        state.voice_status = body.model_dump()
        state.voice_updated_at = time.monotonic()
        state.event(
            state.current_id,
            "voice_connected" if body.connected else "voice_disconnected",
            "Voice bridge heartbeat received.",
        )
        return {**body.model_dump(), "stale": False}

    @application.get("/v1/activity")
    async def activity() -> dict[str, Any]:
        current = state.public(state.current_id) if state.current_id else None
        return {
            "current_status": current["status"] if current else None,
            "current_proposal_id": state.current_id,
            "events": copy.deepcopy(state.events),
        }

    return application


app = create_app()
