"""
Live escalation relay -- contract tests.

Two layers, deliberately:

  * LIVE-mode contract tests impersonate Zendesk with an ``httpx.MockTransport``
    injected via ``escalation.set_live_transport``. They test OUR logic -- the
    payload shape, the requester identity, the comment discriminator, error
    propagation -- including error paths that cannot be provoked on demand
    against a real Zendesk (429, 401, timeouts).

  * MOCK-mode tests drive the built-in sandbox emulator end to end through the
    FastAPI routes, including the simulator endpoints that play the specialist.

Run:  python -m pytest tests/test_escalation_relay.py -q
"""
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import escalation as esc
from backend.server import app

client = TestClient(app)

VALID = {
    "summary": "The oak table arrived with a chipped corner and the customer wants to talk to a person.",
    "order_id": "ORD-8472",
    "customer_name": "Elena Gómez",
    "customer_email": "elena.gomez@example.com",
    "language": "en",
}

AGENT_ID = 35373849336349      # the API account; specialist replies are authored by it
CUSTOMER_ID = 38964127223837   # the ticket requester


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    """Every test starts in mock mode with no tracked escalations."""
    for name in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN",
                 "ZENDESK_MOCK_MODE", "ALLOW_LIVE_SIMULATOR"):
        monkeypatch.delenv(name, raising=False)
    esc.reset_for_tests()
    esc.set_live_transport(None)
    yield
    esc.set_live_transport(None)
    esc.reset_for_tests()


@pytest.fixture
def live(monkeypatch):
    """Switch to live mode against a fake Zendesk; returns (install, calls)."""
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_EMAIL", "api@acme.example")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "fake-token-for-tests")
    monkeypatch.setenv("ZENDESK_MOCK_MODE", "false")
    calls = []

    def install(handler):
        def _wrapped(request: httpx.Request) -> httpx.Response:
            calls.append({
                "method": request.method,
                "url": str(request.url),
                "path": request.url.path,
                "params": dict(request.url.params),
                "headers": dict(request.headers),
                "body": json.loads(request.content) if request.content else None,
            })
            return handler(request)
        esc.set_live_transport(httpx.MockTransport(_wrapped))
        return calls

    return install


def _respond(status, payload):
    return lambda request: httpx.Response(status, json=payload)


CREATED = {"ticket": {"id": 4242, "status": "new", "requester_id": CUSTOMER_ID, "subject": "x"}}


# ==========================================================================
# Live mode -- what we send Zendesk
# ==========================================================================

def test_start_creates_ticket_and_returns_its_id(live):
    calls = live(_respond(201, CREATED))
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["status"] == "SUCCESS"
    assert body["ticket_id"] == "4242"
    assert body["mode"] == "zendesk_live"
    assert calls[0]["method"] == "POST"
    assert calls[0]["url"] == "https://acme.zendesk.com/api/v2/tickets.json"


def test_requests_use_zendesk_api_token_basic_auth(live):
    calls = live(_respond(201, CREATED))
    client.post("/api/escalation/start", json=VALID)
    expected = base64.b64encode(b"api@acme.example/token:fake-token-for-tests").decode()
    assert calls[0]["headers"]["authorization"] == f"Basic {expected}"


def test_ticket_carries_an_explicit_customer_requester(live):
    """The whole comment discriminator depends on this: without an explicit
    requester, requester == submitter == the API account, and bot and human
    comments become indistinguishable by author."""
    calls = live(_respond(201, CREATED))
    client.post("/api/escalation/start", json=VALID)
    ticket = calls[0]["body"]["ticket"]
    assert ticket["requester"] == {"name": "Elena Gómez", "email": "elena.gomez@example.com"}


def test_ticket_is_urgent_and_tagged_for_the_relay(live):
    calls = live(_respond(201, CREATED))
    client.post("/api/escalation/start", json=VALID)
    ticket = calls[0]["body"]["ticket"]
    assert ticket["priority"] == "urgent"
    assert ticket["tags"] == ["live-escalation", "cxas-chat"]


def test_summary_and_order_are_the_opening_public_comment(live):
    calls = live(_respond(201, CREATED))
    client.post("/api/escalation/start", json=VALID)
    comment = calls[0]["body"]["ticket"]["comment"]
    assert VALID["summary"] in comment["body"]
    assert "ORD-8472" in comment["body"]
    assert comment["public"] is True


def test_order_reference_is_optional(live):
    live(_respond(201, CREATED))
    payload = {k: v for k, v in VALID.items() if k != "order_id"}
    assert client.post("/api/escalation/start", json=payload).json()["status"] == "SUCCESS"


# --------------------------------------------------------------------------
# Live mode -- zero silent failure
# --------------------------------------------------------------------------

@pytest.mark.parametrize("status_code, error_code", [
    (429, "ZENDESK_RATE_LIMITED"),
    (401, "ZENDESK_AUTH_FAILED"),
    (403, "ZENDESK_AUTH_FAILED"),
    (500, "ZENDESK_UNAVAILABLE"),
])
def test_zendesk_failures_surface_as_structured_errors(live, status_code, error_code):
    live(_respond(status_code, {"error": "boom"}))
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["status"] == "ERROR"
    assert body["error_code"] == error_code
    assert body.get("agent_action"), "Errors must carry agent_action recovery guidance."
    assert not body.get("ticket_id"), "A failed escalation must never report a ticket id."


def test_unreachable_zendesk_is_reported(live):
    def _boom(request):
        raise httpx.ConnectError("connection refused", request=request)
    live(_boom)
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["error_code"] == "ZENDESK_UNREACHABLE"
    assert not body.get("ticket_id")


def test_a_response_without_ticket_id_is_not_success(live):
    live(_respond(201, {"ticket": {}}))
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["error_code"] == "ZENDESK_MALFORMED_RESPONSE"
    assert not body.get("ticket_id")


def test_live_mode_with_missing_credentials_refuses_loudly(monkeypatch):
    monkeypatch.setenv("ZENDESK_MOCK_MODE", "false")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "token")
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["status"] == "ERROR"
    assert body["error_code"] == "ZENDESK_NOT_CONFIGURED"
    assert "ZENDESK_SUBDOMAIN" in body["message"]
    assert not body.get("ticket_id")


def test_explicit_live_mode_without_token_does_not_fall_back_to_the_sandbox(monkeypatch):
    monkeypatch.setenv("ZENDESK_MOCK_MODE", "false")
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["error_code"] == "ZENDESK_NOT_CONFIGURED"


def test_missing_required_field_is_rejected():
    assert client.post("/api/escalation/start", json={"language": "en"}).status_code == 422


def test_invalid_customer_email_is_rejected():
    body = client.post("/api/escalation/start", json={**VALID, "customer_email": "not-an-email"}).json()
    assert body["error_code"] == "INVALID_CUSTOMER_EMAIL"


# ==========================================================================
# Live mode -- polling for the specialist's replies (comment discriminator)
# ==========================================================================

def _comment(cid, author_id, body, public=True):
    return {"id": cid, "author_id": author_id, "body": body, "public": public,
            "created_at": "2026-09-07T16:00:00Z"}


@pytest.fixture
def tracked_ticket():
    """A ticket the relay already knows about, as if start_escalation ran."""
    esc._STATE["555"] = {
        "requester_id": CUSTOMER_ID, "last_seen_comment_id": 0, "our_comment_ids": set(),
        "created_at": 0, "language": "en", "ticket_status": "open",
    }
    return "555"


@pytest.fixture
def zendesk_reads(live):
    """Serve canned comment and ticket reads from a fake LIVE Zendesk."""
    def install(comments, ticket_status="open", users=None):
        users = users if users is not None else [
            {"id": AGENT_ID, "name": "Ana Martínez"}, {"id": CUSTOMER_ID, "name": "Elena Gómez"}]

        def handler(request):
            if request.url.path.endswith("/comments.json"):
                return httpx.Response(200, json={"comments": comments, "users": users})
            return httpx.Response(200, json={"ticket": {"id": 555, "status": ticket_status}})
        return live(handler)
    return install


def test_specialist_reply_is_returned_with_their_name(tracked_ticket, zendesk_reads):
    zendesk_reads([_comment(1, CUSTOMER_ID, "My table is chipped."),
                   _comment(2, AGENT_ID, "Hi Elena, I'm Ana. Looking at your case now.")])
    body = client.get(f"/api/escalation/{tracked_ticket}/messages").json()
    assert [m["body"] for m in body["messages"]] == ["Hi Elena, I'm Ana. Looking at your case now."]
    assert body["messages"][0]["author_name"] == "Ana Martínez"


def test_polling_requests_side_loaded_users(tracked_ticket, zendesk_reads):
    calls = zendesk_reads([])
    client.get(f"/api/escalation/{tracked_ticket}/messages")
    comment_call = next(c for c in calls if c["path"].endswith("/comments.json"))
    assert comment_call["params"] == {"include": "users"}


def test_the_customers_own_comments_are_never_echoed_back(tracked_ticket, zendesk_reads):
    zendesk_reads([_comment(1, CUSTOMER_ID, "My table is chipped.")])
    assert client.get(f"/api/escalation/{tracked_ticket}/messages").json()["messages"] == []


def test_private_internal_notes_never_reach_the_customer(tracked_ticket, zendesk_reads):
    zendesk_reads([_comment(2, AGENT_ID, "Internal: check stock in the Lyon warehouse.", public=False),
                   _comment(3, AGENT_ID, "I can confirm the replacement.", public=True)])
    bodies = [m["body"] for m in client.get(f"/api/escalation/{tracked_ticket}/messages").json()["messages"]]
    assert bodies == ["I can confirm the replacement."]


def test_comments_the_relay_wrote_are_not_echoed_as_the_specialist(tracked_ticket, zendesk_reads):
    """Forwarded customer replies share the API account author with the specialist."""
    esc._STATE[tracked_ticket]["our_comment_ids"].add(7)
    zendesk_reads([_comment(7, AGENT_ID, "Forwarded customer message."),
                   _comment(8, AGENT_ID, "Real specialist answer.")])
    body = client.get(f"/api/escalation/{tracked_ticket}/messages").json()
    assert [m["body"] for m in body["messages"]] == ["Real specialist answer."]


def test_a_comment_is_delivered_once_without_a_cursor(tracked_ticket, zendesk_reads):
    zendesk_reads([_comment(2, AGENT_ID, "First answer.")])
    assert len(client.get(f"/api/escalation/{tracked_ticket}/messages").json()["messages"]) == 1
    assert client.get(f"/api/escalation/{tracked_ticket}/messages").json()["messages"] == []


def test_client_supplied_cursor_is_authoritative(tracked_ticket, zendesk_reads):
    """A dropped response must not mark the specialist's words as delivered."""
    zendesk_reads([_comment(9, AGENT_ID, "Still here, Elena.")])
    first = client.get(f"/api/escalation/{tracked_ticket}/messages?since=0").json()
    assert [m["id"] for m in first["messages"]] == [9]
    retry = client.get(f"/api/escalation/{tracked_ticket}/messages?since=0").json()
    assert [m["id"] for m in retry["messages"]] == [9]
    after = client.get(f"/api/escalation/{tracked_ticket}/messages?since=9").json()
    assert after["messages"] == []


def test_specialist_joined_flips_on_the_first_reply(tracked_ticket, zendesk_reads):
    zendesk_reads([_comment(1, CUSTOMER_ID, "Hello.")])
    assert client.get(f"/api/escalation/{tracked_ticket}/messages").json()["specialist_joined"] is False
    zendesk_reads([_comment(1, CUSTOMER_ID, "Hello."), _comment(2, AGENT_ID, "I'm Ana.")])
    assert client.get(f"/api/escalation/{tracked_ticket}/messages").json()["specialist_joined"] is True


@pytest.mark.parametrize("status", ["solved", "closed"])
def test_terminal_ticket_status_is_reported_for_handback(tracked_ticket, zendesk_reads, status):
    zendesk_reads([_comment(2, AGENT_ID, "Resolved.")], ticket_status=status)
    assert client.get(f"/api/escalation/{tracked_ticket}/messages").json()["ticket_status"] == status
    assert esc.get_state(tracked_ticket)["ticket_status"] == status


def test_author_name_falls_back_when_not_side_loaded(tracked_ticket, zendesk_reads):
    zendesk_reads([_comment(2, AGENT_ID, "Hello.")], users=[])
    name = client.get(f"/api/escalation/{tracked_ticket}/messages").json()["messages"][0]["author_name"]
    assert name == "Customer Care"


def test_polling_an_unknown_ticket_is_refused(live):
    live(_respond(200, {}))
    body = client.get("/api/escalation/99999999/messages").json()
    assert body["error_code"] == "UNKNOWN_ESCALATION"


def test_zendesk_failure_while_polling_surfaces_structurally(tracked_ticket, live):
    live(_respond(429, {"error": "Too Many Requests"}))
    body = client.get(f"/api/escalation/{tracked_ticket}/messages").json()
    assert body["error_code"] == "ZENDESK_RATE_LIMITED"
    assert body.get("agent_action")


def test_editor_padding_is_stripped_from_specialist_messages(tracked_ticket, zendesk_reads):
    noisy = dict(_comment(2, AGENT_ID, "hello"))
    noisy["plain_body"] = "hello\n&nbsp;\n&nbsp;"
    zendesk_reads([noisy])
    body = client.get(f"/api/escalation/{tracked_ticket}/messages?since=0").json()["messages"][0]["body"]
    assert body == "hello"


def test_internal_blank_lines_are_collapsed_not_multiplied(tracked_ticket, zendesk_reads):
    noisy = dict(_comment(3, AGENT_ID, "x"))
    del noisy["body"]
    noisy["plain_body"] = "First line&nbsp;\n\n\n\nSecond line\n&nbsp;"
    zendesk_reads([noisy])
    body = client.get(f"/api/escalation/{tracked_ticket}/messages?since=0").json()["messages"][0]["body"]
    assert body == "First line\n\nSecond line"


# ==========================================================================
# Live mode -- the customer replies to the specialist
# ==========================================================================

AUDIT = {"audit": {"events": [{"type": "Comment", "id": 4321}]}}


def test_customer_reply_lands_on_the_ticket_as_a_public_comment(tracked_ticket, live):
    calls = live(_respond(200, AUDIT))
    r = client.post(f"/api/escalation/{tracked_ticket}/reply", json={"body": "When will the new table arrive?"})
    assert r.json() == {"status": "SUCCESS", "comment_id": 4321}
    assert calls[0]["method"] == "PUT"
    assert calls[0]["path"] == f"/api/v2/tickets/{tracked_ticket}.json"
    assert calls[0]["body"]["ticket"]["comment"] == {"body": "When will the new table arrive?", "public": True}


def test_a_forwarded_customer_reply_is_never_echoed_back(tracked_ticket, live):
    live(_respond(200, AUDIT))
    client.post(f"/api/escalation/{tracked_ticket}/reply", json={"body": "Thanks."})
    assert 4321 in esc.get_state(tracked_ticket)["our_comment_ids"]


def test_a_reply_without_a_comment_id_is_flagged(tracked_ticket, live):
    live(_respond(200, {"audit": {"events": []}}))
    body = client.post(f"/api/escalation/{tracked_ticket}/reply", json={"body": "Thanks."}).json()
    assert body["error_code"] == "REPLY_NOT_TRACKED"


def test_a_failed_reply_is_reported_not_swallowed(tracked_ticket, live):
    live(_respond(429, {"error": "Too Many Requests"}))
    body = client.post(f"/api/escalation/{tracked_ticket}/reply", json={"body": "Hello"}).json()
    assert body["error_code"] == "ZENDESK_RATE_LIMITED"


def test_replying_to_an_unknown_escalation_is_refused(live):
    live(_respond(200, AUDIT))
    assert client.post("/api/escalation/98765432/reply", json={"body": "Hi"}).json()["error_code"] == "UNKNOWN_ESCALATION"


def test_an_empty_reply_is_rejected(tracked_ticket):
    assert client.post(f"/api/escalation/{tracked_ticket}/reply", json={"body": "   "}).json()["error_code"] == "EMPTY_REPLY"


# ==========================================================================
# Mode detection + health
# ==========================================================================

def test_health_reports_mock_simulator_without_credentials():
    body = client.get("/api/health").json()
    assert body["mode"] == "mock_simulator"
    assert body["configured"] is True
    assert body["zendesk_subdomain"] is None
    assert body["active_escalations"] == 0


def test_health_reports_live_mode_with_credentials(live):
    body = client.get("/api/health").json()
    assert body["mode"] == "zendesk_live"
    assert body["zendesk_subdomain"] == "acme"


def test_mock_mode_flag_wins_over_configured_credentials(monkeypatch):
    monkeypatch.setenv("ZENDESK_API_TOKEN", "real-looking-token")
    monkeypatch.setenv("ZENDESK_MOCK_MODE", "true")
    assert client.get("/api/health").json()["mode"] == "mock_simulator"


# ==========================================================================
# Mock mode -- the full lifecycle through the sandbox + simulator endpoints
# ==========================================================================

def _start_mock():
    body = client.post("/api/escalation/start", json=VALID).json()
    assert body["status"] == "SUCCESS", body
    assert body["mode"] == "mock_simulator"
    return body["ticket_id"]


def test_mock_ticket_requester_is_the_customer_not_the_api_account():
    tid = _start_mock()
    ticket = esc.MOCK_ZENDESK.tickets[int(tid)]
    requester = esc.MOCK_ZENDESK.users[ticket["requester_id"]]
    assert requester["email"] == "elena.gomez@example.com"
    assert ticket["requester_id"] != esc.MOCK_ZENDESK.api_user_id
    assert ticket["tags"] == ["live-escalation", "cxas-chat"]
    # The opening comment is attributed to the customer, as on real Zendesk.
    assert esc.MOCK_ZENDESK.comments[int(tid)][0]["author_id"] == ticket["requester_id"]


def test_mock_full_lifecycle_reply_specialist_note_and_handback():
    tid = _start_mock()

    def poll(since=0):
        return client.get(f"/api/escalation/{tid}/messages?since={since}").json()

    first = poll()
    assert first["messages"] == [] and first["specialist_joined"] is False

    # The customer replies before anyone joined: forwarded, never echoed back.
    assert client.post(f"/api/escalation/{tid}/reply", json={"body": "Are you there?"}).json()["status"] == "SUCCESS"
    assert poll()["messages"] == []

    # An internal note in the sandbox stays internal.
    note_author = esc.MOCK_ZENDESK.ensure_agent("Sarah (Zendesk Specialist)")
    esc.MOCK_ZENDESK._add_comment(int(tid), note_author, "Internal: VIP customer.", public=False)
    assert poll()["messages"] == []

    # The specialist answers through the simulator.
    sim = client.post(f"/api/simulator/escalation/{tid}/specialist-reply",
                      json={"body": "Hi Elena, Sarah here. A replacement ships today.",
                            "author_name": "Sarah — Zendesk Agent"}).json()
    assert sim["status"] == "SUCCESS"
    delivered = poll()
    assert delivered["specialist_joined"] is True
    assert [(m["author_name"], m["body"]) for m in delivered["messages"]] == [
        ("Sarah — Zendesk Agent", "Hi Elena, Sarah here. A replacement ships today.")]
    assert delivered["ticket_status"] == "open"
    cursor = delivered["messages"][0]["id"]
    assert poll(cursor)["messages"] == []

    # Listed as active until solved.
    listed = client.get("/api/escalation").json()["escalations"]
    assert [e["escalation_id"] for e in listed] == [tid]
    assert listed[0]["specialist_joined"] is True

    solved = client.post(f"/api/simulator/escalation/{tid}/status", json={"status": "solved"}).json()
    assert solved == {"status": "SUCCESS", "escalation_id": tid, "ticket_status": "solved"}
    assert poll(cursor)["ticket_status"] == "solved"
    assert client.get("/api/escalation").json()["escalations"] == []
    ended = client.get("/api/escalation?include_ended=true").json()["escalations"]
    assert ended[0]["ticket_status"] == "solved"


def test_mock_closed_status_is_also_a_handback():
    tid = _start_mock()
    client.post(f"/api/simulator/escalation/{tid}/status", json={"status": "closed"})
    assert client.get(f"/api/escalation/{tid}/messages").json()["ticket_status"] == "closed"


@pytest.mark.parametrize("status", ["open", "pending"])
def test_simulator_can_set_non_terminal_statuses(status):
    tid = _start_mock()
    body = client.post(f"/api/simulator/escalation/{tid}/status", json={"status": status}).json()
    assert body["ticket_status"] == status
    assert client.get("/api/escalation").json()["escalations"][0]["ticket_status"] == status


def test_simulator_rejects_unknown_status_values():
    tid = _start_mock()
    assert client.post(f"/api/simulator/escalation/{tid}/status", json={"status": "deleted"}).status_code == 422


def test_simulator_rejects_unknown_escalations_and_empty_replies():
    assert client.post("/api/simulator/escalation/424242/specialist-reply",
                       json={"body": "hi"}).json()["error_code"] == "UNKNOWN_ESCALATION"
    tid = _start_mock()
    assert client.post(f"/api/simulator/escalation/{tid}/specialist-reply",
                       json={"body": "  "}).json()["error_code"] == "EMPTY_REPLY"


def test_simulator_default_author_name():
    tid = _start_mock()
    client.post(f"/api/simulator/escalation/{tid}/specialist-reply", json={"body": "Hello!"})
    assert client.get(f"/api/escalation/{tid}/messages").json()["messages"][0]["author_name"] == \
        "Sarah (Zendesk Specialist)"


def test_simulator_is_disabled_in_live_mode(tracked_ticket, live):
    calls = live(_respond(200, AUDIT))
    body = client.post(f"/api/simulator/escalation/{tracked_ticket}/specialist-reply",
                       json={"body": "hi"}).json()
    assert body["error_code"] == "SIMULATOR_DISABLED"
    assert calls == [], "A disabled simulator must not touch the real Zendesk."
    assert client.post(f"/api/simulator/escalation/{tracked_ticket}/status",
                       json={"status": "solved"}).json()["error_code"] == "SIMULATOR_DISABLED"


def test_live_simulator_opt_in_posts_a_real_public_comment(tracked_ticket, live, monkeypatch):
    monkeypatch.setenv("ALLOW_LIVE_SIMULATOR", "true")
    calls = live(_respond(200, AUDIT))
    body = client.post(f"/api/simulator/escalation/{tracked_ticket}/specialist-reply",
                       json={"body": "Specialist here."}).json()
    assert body["status"] == "SUCCESS"
    assert calls[0]["body"] == {"ticket": {"comment": {"body": "Specialist here.", "public": True}}}
    # Not recorded as ours, so the poller delivers it as the specialist.
    assert 4321 not in esc.get_state(tracked_ticket)["our_comment_ids"]


def test_frontend_is_served_at_root():
    r = client.get("/")
    assert r.status_code == 200
    assert "cxas-zendesk-bridge.js" in r.text
    assert client.get("/cxas-zendesk-bridge.js").status_code == 200
    assert client.get("/cxas-zendesk-bridge.css").status_code == 200
