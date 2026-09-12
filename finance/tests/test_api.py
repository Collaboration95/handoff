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


def test_known_write_reserves_billing_reference_across_new_proposal_ids() -> None:
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

    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "billing_reference_already_written"
    assert len(adapter.calls) == 1


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


def test_missing_evaluation_report_is_an_explicit_empty_state(tmp_path) -> None:
    app = create_app(
        planner_factory=FakePlanner,
        airwallex_client=FakeAirwallex(),
        config={"evaluation_path": tmp_path / "missing.json"},
    )

    response = TestClient(app).get("/v1/evaluation/latest")

    assert response.status_code == 200
    assert response.json() == {"available": False, "report": None, "error": "report_not_found"}
