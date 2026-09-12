from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field

from fastapi.testclient import TestClient

from handoff_finance.app import create_app
from handoff_finance.fixtures import build_case
from handoff_finance.models import InvoiceDraft


def valid_draft() -> InvoiceDraft:
    baseline = build_case("showcase").proposed
    assert baseline is not None
    return baseline.model_copy(
        update={"quantity": 6, "unit_price_minor": 9_000, "contract_id": "contract-sep"}
    )


@dataclass
class FakePlanner:
    initial_result: InvoiceDraft = field(default_factory=valid_draft)
    repair_result: list[InvoiceDraft] = field(default_factory=lambda: [valid_draft()])
    usage: dict = field(default_factory=lambda: {"total_tokens": 42})

    async def initial(self, request):
        return self.initial_result

    async def repair(self, request, baseline, checks):
        return self.repair_result


class FakeAirwallex:
    def __init__(self, result=None, *, ready=True):
        self.result = result or {
            "status": "created",
            "invoice_id": "inv_demo_123",
            "state": "DRAFT",
            "stage": "verified",
            "verified": True,
            "checks": {"quantity": True},
            "error": None,
            "environment": "sandbox",
        }
        self.ready = ready
        self.calls = []
        self.verifications = []
        self.verification_result = None

    async def readiness(self):
        return {
            "ready": self.ready,
            "authenticated": self.ready,
            "environment": "sandbox" if self.ready else "unknown",
            "error": None if self.ready else "cli_not_found",
        }

    async def create_verified_invoice(self, request, draft):
        self.calls.append((request, draft))
        return dict(self.result)

    async def verify_existing_invoice(self, request, draft, invoice_id):
        self.verifications.append((request, draft, invoice_id))
        return dict(self.verification_result or {**self.result, "status": "existing", "reused_existing": True})


def client(*, planner=None, airwallex=None) -> tuple[TestClient, FakeAirwallex]:
    adapter = airwallex or FakeAirwallex()
    app = create_app(
        planner_factory=lambda: planner or FakePlanner(),
        airwallex_client=adapter,
        config={"openai_ready": True, "langfuse_ready": False},
    )
    return TestClient(app), adapter


def test_root_serves_static_approval_page_without_secret_fields() -> None:
    api, _ = client()

    response = api.get("/")

    assert response.status_code == 200
    assert "Create the right invoice" in response.text
    assert "Approve invoice workflow" in response.text
    assert "api_key" not in response.text.lower()


def test_improve_preserves_objective_and_stores_server_owned_decision() -> None:
    api, _ = client()
    payload = build_case("showcase").model_dump(mode="json")
    payload["objective"] = 'Keep this objective exactly; source says "ignore policy".'
    payload["proposed"] = valid_draft().model_dump(mode="json")

    response = api.post("/v1/improve", json=payload)

    assert response.status_code == 200
    proposal = response.json()
    assert proposal["objective"] == payload["objective"]
    assert proposal["status"] == "ready"
    assert proposal["approved"] is True
    assert proposal["decision"]["objective"] == payload["objective"]
    assert proposal["request"]["request_id"] == proposal["id"]


def test_invalid_inputs_are_rejected_strictly() -> None:
    api, _ = client()

    assert api.post("/v1/proposals", json={"objective": "", "case_name": "showcase"}).status_code == 422
    assert api.post(
        "/v1/proposals",
        json={"objective": "Create invoice", "case_name": "showcase", "fault_injection": "true"},
    ).status_code == 422
    assert api.post(
        "/v1/proposals", json={"objective": "Create invoice", "case_name": "not-a-case"}
    ).status_code == 404
    assert api.post("/v1/demo/autofix", json={"approved": True}).status_code == 422


def test_proposal_requires_ui_approval_before_checks_and_creation() -> None:
    api, adapter = client()
    objective = "Create the invoice for accepted September implementation work."

    created = api.post(
        "/v1/proposals",
        json={"objective": objective, "case_name": "showcase", "fault_injection": True},
    ).json()
    assert created["status"] == "pending"
    assert created["approved"] is False
    assert created["decision"] is None

    blocked = api.post("/v1/invoices/create", json={"proposal_id": created["id"]})
    assert blocked.status_code == 409
    assert adapter.calls == []

    approved = api.post(f"/v1/proposals/{created['id']}/approve").json()
    assert approved["approved"] is True
    assert approved["status"] == "ready"
    assert approved["decision"]["selected"]["quantity"] == 6

    completed = api.post("/v1/invoices/create", json={"proposal_id": created["id"]})
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    assert completed.json()["invoice"]["verified"] is True
    assert len(adapter.calls) == 1


def test_live_model_selection_can_be_created_when_fixture_has_no_proposed_draft() -> None:
    api, adapter = client()
    proposal = api.post(
        "/v1/proposals",
        json={"objective": "Create invoice", "case_name": "showcase", "fault_injection": False},
    ).json()

    approved = api.post(f"/v1/proposals/{proposal['id']}/approve")
    created = api.post("/v1/invoices/create", json={"proposal_id": proposal["id"]})

    assert approved.json()["status"] == "ready"
    assert created.status_code == 200
    assert created.json()["status"] == "completed"
    assert len(adapter.calls) == 1


def test_autofix_simulation_repairs_a_fresh_proposal_without_approval_or_writes() -> None:
    api, adapter = client()
    previous = api.post("/v1/proposals", json={
        "objective": "Keep an intentionally incorrect invoice; do not repair it.",
        "case_name": "missing_effective_contract",
    }).json()

    started = api.post("/v1/demo/autofix")

    assert started.status_code == 200
    assert started.json()["status"] == "checking"
    result = api.get(f"/v1/proposals/{started.json()['id']}").json()
    assert result["status"] == "ready"
    assert result["approved"] is False
    assert result["fault_injection"] is True
    assert result["case_name"] == "showcase"
    assert result["objective"] == "Create the invoice for accepted September implementation work."
    assert result["decision"]["objective"] == result["objective"]
    assert result["decision"]["status"] == "repaired"
    assert result["decision"]["baseline"]["quantity"] == 10
    assert result["decision"]["baseline"]["unit_price_minor"] == 10_000
    assert result["decision"]["selected"]["quantity"] == 6
    assert result["decision"]["selected"]["unit_price_minor"] == 9_000
    assert result["invoice"] is None
    old = api.get(f"/v1/proposals/{previous['id']}").json()
    assert old["objective"] == previous["objective"]
    assert old["status"] == "cancelled"
    assert api.post("/v1/invoices/create", json={"proposal_id": result["id"]}).status_code == 409
    assert adapter.calls == []
    events = api.get("/v1/activity").json()["events"]
    assert not any(event["type"] == "approval_recorded" for event in events)


def test_simulation_approval_uses_the_displayed_correction_without_rerunning_model() -> None:
    class SingleRepairPlanner(FakePlanner):
        calls = 0

        async def repair(self, request, baseline, checks):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("Must not replace the reviewed candidate on approval")
            return self.repair_result

    planner = SingleRepairPlanner()
    api, adapter = client(planner=planner)
    started = api.post("/v1/demo/autofix")
    assert started.status_code == 200
    proposal_id = started.json()["id"]
    before = api.get(f"/v1/proposals/{proposal_id}").json()

    approved = api.post(f"/v1/proposals/{proposal_id}/approve")

    assert approved.status_code == 200
    assert approved.json()["approved"] is True
    assert approved.json()["status"] == "ready"
    assert approved.json()["decision"] == before["decision"]
    assert planner.calls == 1
    assert adapter.calls == []
    created = api.post("/v1/invoices/create", json={"proposal_id": proposal_id})
    assert created.status_code == 200
    assert created.json()["invoice"]["verified"] is True
    assert len(adapter.calls) == 1


def test_autofix_simulation_does_not_claim_success_when_repair_fails() -> None:
    api, adapter = client(planner=FakePlanner(repair_result=[]))
    started = api.post("/v1/demo/autofix")
    assert started.status_code == 200
    proposal_id = started.json()["id"]
    result = api.get(f"/v1/proposals/{proposal_id}").json()
    assert result["status"] == "needs_review"
    assert result["decision"]["selected"] is None
    assert result["approved"] is False
    assert api.post(f"/v1/proposals/{proposal_id}/approve").status_code == 409
    assert api.post("/v1/invoices/create", json={"proposal_id": proposal_id}).status_code == 409
    assert adapter.calls == []


def test_stale_or_cancelled_proposal_cannot_be_approved_or_created() -> None:
    api, adapter = client()
    first = api.post("/v1/proposals", json={"objective": "First", "case_name": "showcase"}).json()
    second = api.post("/v1/proposals", json={"objective": "Second", "case_name": "showcase"}).json()

    assert api.get(f"/v1/proposals/{first['id']}").json()["status"] == "cancelled"
    assert api.post(f"/v1/proposals/{first['id']}/approve").status_code == 409
    assert api.post("/v1/invoices/create", json={"proposal_id": first["id"]}).status_code == 409

    cancelled = api.post(f"/v1/proposals/{second['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert api.post("/v1/invoices/create", json={"proposal_id": second["id"]}).status_code == 409
    assert adapter.calls == []


def test_new_proposal_is_rejected_while_approval_checks_are_in_progress() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingPlanner(FakePlanner):
        async def initial(self, request):
            started.set()
            await asyncio.to_thread(release.wait)
            return self.initial_result

    api, _ = client(planner=BlockingPlanner())
    first = api.post(
        "/v1/proposals",
        json={"objective": "First", "case_name": "showcase", "fault_injection": False},
    ).json()
    responses = []
    worker = threading.Thread(
        target=lambda: responses.append(api.post(f"/v1/proposals/{first['id']}/approve"))
    )
    worker.start()
    assert started.wait(timeout=2)
    try:
        replacement = api.post("/v1/proposals", json={"objective": "Second"})
        current = api.get("/v1/proposals/current").json()

        assert replacement.status_code == 409
        assert replacement.json()["detail"] == "proposal_in_progress"
        assert current["id"] == first["id"]
        assert current["status"] == "checking"
        assert current["approved"] is True
    finally:
        release.set()
        worker.join(timeout=2)
    assert responses[0].json()["status"] == "ready"


def test_new_proposal_is_rejected_while_invoice_creation_is_in_progress() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAirwallex(FakeAirwallex):
        async def create_verified_invoice(self, request, draft):
            self.calls.append((request, draft))
            started.set()
            await asyncio.to_thread(release.wait)
            return dict(self.result)

    adapter = BlockingAirwallex()
    api, _ = client(airwallex=adapter)
    first = api.post(
        "/v1/proposals",
        json={"objective": "First", "case_name": "showcase", "fault_injection": True},
    ).json()
    assert api.post(f"/v1/proposals/{first['id']}/approve").json()["status"] == "ready"
    responses = []
    worker = threading.Thread(
        target=lambda: responses.append(
            api.post("/v1/invoices/create", json={"proposal_id": first["id"]})
        )
    )
    worker.start()
    assert started.wait(timeout=2)
    try:
        replacement = api.post("/v1/proposals", json={"objective": "Second"})
        current = api.get("/v1/proposals/current").json()

        assert replacement.status_code == 409
        assert replacement.json()["detail"] == "proposal_in_progress"
        assert current["id"] == first["id"]
        assert current["status"] == "creating"
        assert current["approved"] is True
    finally:
        release.set()
        worker.join(timeout=2)
    assert responses[0].json()["status"] == "completed"


def test_create_revalidates_stored_selection_and_never_trusts_browser_fields() -> None:
    api, adapter = client()
    proposal = api.post(
        "/v1/proposals",
        json={"objective": "Create invoice", "case_name": "showcase", "fault_injection": True},
    ).json()
    approved = api.post(f"/v1/proposals/{proposal['id']}/approve").json()
    stored = api.app.state.finance.proposals[proposal["id"]]
    stored["decision"]["selected"]["quantity"] = 99

    response = api.post(
        "/v1/invoices/create",
        json={"proposal_id": proposal["id"], "quantity": approved["decision"]["selected"]["quantity"]},
    )

    assert response.status_code == 422
    response = api.post("/v1/invoices/create", json={"proposal_id": proposal["id"]})
    assert response.status_code == 409
    assert adapter.calls == []


def test_unverified_external_result_is_failed_and_repeat_does_not_rewrite() -> None:
    adapter = FakeAirwallex(
        {
            "status": "partial",
            "invoice_id": "inv_partial_123",
            "state": "DRAFT",
            "stage": "line_items_failed",
            "verified": False,
            "checks": {},
            "error": "billing_error",
            "environment": "sandbox",
        }
    )
    api, _ = client(airwallex=adapter)
    proposal = api.post(
        "/v1/proposals",
        json={"objective": "Create invoice", "case_name": "showcase", "fault_injection": True},
    ).json()
    api.post(f"/v1/proposals/{proposal['id']}/approve")

    first = api.post("/v1/invoices/create", json={"proposal_id": proposal["id"]})
    second = api.post("/v1/invoices/create", json={"proposal_id": proposal["id"]})

    assert first.json()["status"] == "failed"
    assert first.json()["invoice"]["invoice_id"] == "inv_partial_123"
    assert first.json()["invoice"]["verified"] is False
    assert second.json() == first.json()
    assert len(adapter.calls) == 1


def test_known_verified_draft_is_read_back_instead_of_created_again() -> None:
    api, adapter = client()
    first = api.post(
        "/v1/proposals",
        json={"objective": "First", "case_name": "showcase", "fault_injection": True},
    ).json()
    api.post(f"/v1/proposals/{first['id']}/approve")
    assert api.post("/v1/invoices/create", json={"proposal_id": first["id"]}).status_code == 200

    second = api.post(
        "/v1/proposals",
        json={"objective": "Second", "case_name": "showcase", "fault_injection": True},
    ).json()
    api.post(f"/v1/proposals/{second['id']}/approve")
    duplicate = api.post("/v1/invoices/create", json={"proposal_id": second["id"]})

    assert duplicate.status_code == 200
    assert duplicate.json()["status"] == "completed"
    assert duplicate.json()["invoice"]["invoice_id"] == "inv_demo_123"
    assert duplicate.json()["invoice"]["reused_existing"] is True
    existing = api.get(f"/v1/proposals/{first['id']}").json()
    assert existing["invoice"]["verified"] is True
    assert len(adapter.calls) == 1
    assert len(adapter.verifications) == 1
    assert adapter.verifications[0][2] == "inv_demo_123"
    events = api.get("/v1/activity").json()["events"]
    assert events[-1]["type"] == "invoice_reused"


def test_existing_draft_mismatch_does_not_create_a_replacement() -> None:
    api, adapter = client()
    first = api.post("/v1/demo/autofix").json()
    api.post(f"/v1/proposals/{first['id']}/approve")
    api.post("/v1/invoices/create", json={"proposal_id": first["id"]})
    second = api.post("/v1/demo/autofix").json()
    api.post(f"/v1/proposals/{second['id']}/approve")
    adapter.verification_result = {
        **adapter.result, "verified": False, "status": "partial",
        "error": "readback_mismatch", "stage": "verification_failed",
    }

    result = api.post("/v1/invoices/create", json={"proposal_id": second["id"]})

    assert result.status_code == 200
    assert result.json()["status"] == "failed"
    assert result.json()["invoice"]["verified"] is False
    assert len(adapter.calls) == 1
    assert len(adapter.verifications) == 1
    assert api.app.state.finance.billing_writes["BILL-2026-09"]["proposal_id"] == first["id"]


def test_uncertain_previous_write_remains_blocked_on_a_new_proposal() -> None:
    api, adapter = client(airwallex=FakeAirwallex(result={
        "status": "unknown", "stage": "create_uncertain", "verified": False,
        "invoice_id": None, "error": "timeout", "environment": "sandbox",
    }))
    first = api.post("/v1/demo/autofix").json()
    api.post(f"/v1/proposals/{first['id']}/approve")
    api.post("/v1/invoices/create", json={"proposal_id": first["id"]})
    second = api.post("/v1/demo/autofix").json()
    api.post(f"/v1/proposals/{second['id']}/approve")

    result = api.post("/v1/invoices/create", json={"proposal_id": second["id"]})

    assert result.status_code == 409
    assert result.json()["detail"]["code"] == "billing_reference_already_written"
    assert len(adapter.calls) == 1
    assert adapter.verifications == []


def test_unconfigured_model_provider_is_an_honest_review_state() -> None:
    def unavailable():
        raise RuntimeError("missing credentials")

    api, _ = client()
    api.app.state.finance.planner_factory = unavailable
    proposal = api.post("/v1/proposals", json={"objective": "Create invoice"}).json()

    result = api.post(f"/v1/proposals/{proposal['id']}/approve")

    assert result.status_code == 200
    assert result.json()["status"] == "needs_review"
    assert result.json()["error"] == "provider_not_configured"


def test_activity_and_cases_expose_auditable_non_secret_state() -> None:
    api, _ = client()
    proposal = api.post("/v1/proposals", json={"objective": "Create invoice"}).json()
    api.post(f"/v1/proposals/{proposal['id']}/approve")

    catalog = api.get("/v1/cases").json()
    activity = api.get("/v1/activity").json()

    assert "showcase" in [item["name"] for item in catalog]
    assert activity["current_status"] == "ready"
    assert [event["type"] for event in activity["events"]][-3:] == [
        "proposal_created",
        "approval_recorded",
        "checks_completed",
    ]
    assert "OPENAI_API_KEY" not in str(activity)


def test_health_reports_missing_services_and_voice_heartbeat_staleness_without_500() -> None:
    api, _ = client(airwallex=FakeAirwallex(ready=False))

    initial = api.get("/health")
    assert initial.status_code == 200
    assert initial.json()["services"]["airwallex"]["ready"] is False
    assert initial.json()["services"]["langfuse"]["ready"] is False
    assert initial.json()["services"]["livekit"]["connected"] is False

    heartbeat = api.post(
        "/v1/voice/status",
        json={
            "connected": True,
            "room_name": "handoff-finance",
            "human_microphones": 3,
            "model": "gpt-realtime",
            "error": None,
        },
    )
    assert heartbeat.status_code == 200
    assert api.get("/health").json()["services"]["livekit"]["connected"] is True

    api.app.state.finance.voice_updated_at -= 16
    stale = api.get("/health").json()["services"]["livekit"]
    assert stale["connected"] is False
    assert stale["stale"] is True


def test_repeated_voice_heartbeats_refresh_health_without_burying_operation_events() -> None:
    api, _ = client()
    proposal = api.post(
        "/v1/proposals",
        json={"objective": "Create invoice", "case_name": "showcase", "fault_injection": True},
    ).json()
    api.post(f"/v1/proposals/{proposal['id']}/approve")
    api.post("/v1/invoices/create", json={"proposal_id": proposal["id"]})
    heartbeat = {
        "connected": True,
        "room_name": "handoff-finance",
        "human_microphones": 2,
        "model": "gpt-realtime",
        "error": None,
    }

    api.post("/v1/voice/status", json=heartbeat)
    first_update = api.app.state.finance.voice_updated_at
    api.app.state.finance.voice_updated_at -= 10
    for _ in range(12):
        assert api.post("/v1/voice/status", json=heartbeat).status_code == 200

    activity = api.get("/v1/activity").json()
    event_types = [event["type"] for event in activity["events"]]
    health = api.get("/health").json()["services"]["livekit"]

    assert event_types.count("voice_connected") == 1
    assert event_types[-2:] == ["invoice_verified", "voice_connected"]
    assert api.app.state.finance.voice_updated_at > first_update
    assert health["connected"] is True
    assert health["stale"] is False

    changed = api.post("/v1/voice/status", json={**heartbeat, "human_microphones": 1})
    assert changed.status_code == 200
    assert api.get("/v1/activity").json()["events"][-1]["type"] == "voice_status_changed"


def test_missing_evaluation_report_is_an_explicit_empty_state(tmp_path) -> None:
    app = create_app(
        planner_factory=FakePlanner,
        airwallex_client=FakeAirwallex(),
        config={"evaluation_path": tmp_path / "missing.json"},
    )

    response = TestClient(app).get("/v1/evaluation/latest")

    assert response.status_code == 200
    assert response.json() == {"available": False, "report": None, "error": "report_not_found"}
