"""
Server-side Zendesk MCP tools -- search_tickets, get_ticket_status,
create_offline_ticket, add_ticket_comment.

Each tool is exercised twice:
  * against the built-in sandbox (mock mode), end to end;
  * against an impersonated LIVE Zendesk (``httpx.MockTransport``), asserting the
    exact Zendesk v2 REST calls the tool makes.

Plus the MCP wire protocol itself (``POST /mcp``) and the REST preview route.

Run:  python -m pytest tests/test_zendesk_mcp_tools.py -q
"""
import asyncio
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend import escalation as esc
from backend import zendesk_mcp_tools as tools
from backend.server import app

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in ("ZENDESK_SUBDOMAIN", "ZENDESK_EMAIL", "ZENDESK_API_TOKEN", "ZENDESK_MOCK_MODE"):
        monkeypatch.delenv(name, raising=False)
    esc.reset_for_tests()
    esc.set_live_transport(None)
    yield
    esc.set_live_transport(None)
    esc.reset_for_tests()


@pytest.fixture
def live(monkeypatch):
    """Live mode against a fake Zendesk. ``install(handler)`` returns the call log."""
    monkeypatch.setenv("ZENDESK_SUBDOMAIN", "acme")
    monkeypatch.setenv("ZENDESK_EMAIL", "api@acme.example")
    monkeypatch.setenv("ZENDESK_API_TOKEN", "tok")
    monkeypatch.setenv("ZENDESK_MOCK_MODE", "false")
    calls = []

    def install(handler):
        def _wrapped(request):
            calls.append({
                "method": request.method, "path": request.url.path,
                "params": dict(request.url.params), "headers": dict(request.headers),
                "body": json.loads(request.content) if request.content else None,
            })
            return handler(request)
        esc.set_live_transport(httpx.MockTransport(_wrapped))
        return calls
    return install


def _create(email="pat@example.com", subject="Missing screws", **kw):
    result = run(tools.create_offline_ticket(subject=subject, description="The kit had no screws.",
                                             customer_email=email, **kw))
    assert result["status"] == "SUCCESS", result
    return result["ticket_id"]


# ==========================================================================
# Mock (sandbox) mode
# ==========================================================================

def test_mock_create_offline_ticket_stores_a_tagged_ticket_for_the_customer():
    result = run(tools.create_offline_ticket(
        subject="Missing screws", description="The kit had no screws.", customer_email="pat@example.com",
        customer_name="Pat Lee", priority="high", reference_id="ORD-77"))
    assert result == {"status": "SUCCESS", "ticket_id": 10001, "ticket_status": "new", "priority": "high"}
    ticket = esc.MOCK_ZENDESK.tickets[10001]
    assert ticket["tags"] == ["cxas-offline-ticket", "cxas-chat"]
    assert ticket["external_id"] == "ORD-77"
    requester = esc.MOCK_ZENDESK.users[ticket["requester_id"]]
    assert (requester["name"], requester["email"]) == ("Pat Lee", "pat@example.com")
    assert "Reference: ORD-77" in esc.MOCK_ZENDESK.comments[10001][0]["body"]


@pytest.mark.parametrize("kwargs, code", [
    ({"priority": "critical"}, "INVALID_PRIORITY"),
    ({"customer_email": "nope"}, "INVALID_EMAIL"),
    ({"subject": "  "}, "INVALID_TICKET_CONTENT"),
])
def test_mock_create_offline_ticket_validates_input(kwargs, code):
    args = {"subject": "s", "description": "d", "customer_email": "pat@example.com", **kwargs}
    result = run(tools.create_offline_ticket(**args))
    assert result["status"] == "ERROR" and result["error_code"] == code
    assert "ticket_id" not in result
    assert esc.MOCK_ZENDESK.tickets == {}


def test_mock_search_tickets_finds_only_that_customers_tickets():
    mine = _create("pat@example.com")
    _create("someone.else@example.com")
    result = run(tools.search_tickets("PAT@example.com"))
    assert result["status"] == "SUCCESS"
    assert [t["id"] for t in result["tickets"]] == [mine]
    assert result["tickets"][0]["subject"] == "Missing screws"


def test_mock_search_tickets_filters_by_status():
    first = _create()
    second = _create(subject="Second issue")
    esc.MOCK_ZENDESK.tickets[first]["status"] = "solved"
    assert [t["id"] for t in run(tools.search_tickets("pat@example.com", "solved"))["tickets"]] == [first]
    assert [t["id"] for t in run(tools.search_tickets("pat@example.com", "new"))["tickets"]] == [second]


@pytest.mark.parametrize("email, status, code", [
    ("not-an-email", "", "INVALID_EMAIL"),
    ("pat@example.com status:closed", "", "INVALID_EMAIL"),   # query injection attempt
    ("pat@example.com", "archived", "INVALID_STATUS"),
])
def test_mock_search_tickets_validates_input(email, status, code):
    assert run(tools.search_tickets(email, status))["error_code"] == code


def test_mock_get_ticket_status_returns_latest_public_comment_only():
    tid = _create()
    agent = esc.MOCK_ZENDESK.ensure_agent("Sarah (Zendesk Specialist)")
    esc.MOCK_ZENDESK._add_comment(tid, agent, "We are shipping new screws today.", public=True)
    esc.MOCK_ZENDESK._add_comment(tid, agent, "Internal: charge to warranty.", public=False)
    esc.MOCK_ZENDESK.tickets[tid]["status"] = "pending"
    result = run(tools.get_ticket_status(tid))
    assert result["status"] == "SUCCESS"
    assert result["ticket"]["id"] == tid
    assert result["ticket"]["status"] == "pending"
    assert result["ticket"]["priority"] == "normal"
    assert result["ticket"]["updated_at"]
    assert result["is_resolved"] is False
    assert result["latest_public_comment"]["author_name"] == "Sarah (Zendesk Specialist)"
    assert result["latest_public_comment"]["body"] == "We are shipping new screws today."


def test_mock_get_ticket_status_reports_resolution_and_accepts_string_ids():
    tid = _create()
    esc.MOCK_ZENDESK.tickets[tid]["status"] = "solved"
    assert run(tools.get_ticket_status(f"#{tid}"))["is_resolved"] is True


def test_mock_get_ticket_status_unknown_and_invalid_ids():
    assert run(tools.get_ticket_status(999999))["error_code"] == "TICKET_NOT_FOUND"
    assert run(tools.get_ticket_status("abc"))["error_code"] == "INVALID_TICKET_ID"
    assert run(tools.get_ticket_status(0))["error_code"] == "INVALID_TICKET_ID"


def test_mock_add_ticket_comment_appends_a_public_comment():
    tid = _create()
    result = run(tools.add_ticket_comment(tid, "Here is a photo of the box."))
    assert result["status"] == "SUCCESS" and result["ticket_id"] == tid and result["comment_id"]
    last = esc.MOCK_ZENDESK.comments[tid][-1]
    assert (last["body"], last["public"], last["author_id"]) == (
        "Here is a photo of the box.", True, esc.MOCK_ZENDESK.api_user_id)


def test_mock_add_ticket_comment_with_email_is_authored_by_the_requester():
    tid = _create()
    result = run(tools.add_ticket_comment(tid, "Any update?", customer_email="pat@example.com"))
    assert result["status"] == "SUCCESS"
    assert esc.MOCK_ZENDESK.comments[tid][-1]["author_id"] == esc.MOCK_ZENDESK.tickets[tid]["requester_id"]


def test_mock_add_ticket_comment_refuses_someone_elses_ticket():
    tid = _create("pat@example.com")
    before = len(esc.MOCK_ZENDESK.comments[tid])
    result = run(tools.add_ticket_comment(tid, "Hijack", customer_email="mallory@example.com"))
    assert result["error_code"] == "TICKET_OWNERSHIP_MISMATCH"
    assert "pat@example.com" not in result["message"]
    assert len(esc.MOCK_ZENDESK.comments[tid]) == before


def test_mock_add_ticket_comment_on_closed_ticket_is_a_structured_error():
    tid = _create()
    esc.MOCK_ZENDESK.tickets[tid]["status"] = "closed"
    result = run(tools.add_ticket_comment(tid, "Reopen please"))
    assert result["error_code"] == "ZENDESK_VALIDATION_FAILED"
    assert result["agent_action"]


def test_mock_add_ticket_comment_rejects_empty_text():
    tid = _create()
    assert run(tools.add_ticket_comment(tid, "   "))["error_code"] == "EMPTY_COMMENT"


# ==========================================================================
# Live mode (impersonated Zendesk v2)
# ==========================================================================

def test_live_search_tickets_calls_the_search_api(live):
    calls = live(lambda r: httpx.Response(200, json={"count": 2, "results": [
        {"id": 7, "subject": "A", "status": "open", "priority": "high", "result_type": "ticket",
         "created_at": "c", "updated_at": "u", "tags": ["x"]},
        {"id": 99, "name": "not a ticket", "result_type": "user"},
    ]}))
    result = run(tools.search_tickets("pat@example.com", "open"))
    assert calls[0]["method"] == "GET"
    assert calls[0]["path"] == "/api/v2/search.json"
    assert calls[0]["params"]["query"] == "type:ticket requester:pat@example.com status:open"
    assert calls[0]["headers"]["authorization"] == "Basic " + base64.b64encode(b"api@acme.example/token:tok").decode()
    assert result["tickets"] == [{"id": 7, "subject": "A", "status": "open", "priority": "high",
                                  "created_at": "c", "updated_at": "u", "tags": ["x"]}]


def test_live_get_ticket_status_reads_ticket_and_comments(live):
    def handler(request):
        if request.url.path.endswith("/comments.json"):
            return httpx.Response(200, json={
                "comments": [{"id": 1, "author_id": 5, "body": "Opening", "public": True},
                             {"id": 2, "author_id": 6, "body": "Replacement shipped.\n&nbsp;", "public": True},
                             {"id": 3, "author_id": 6, "body": "internal", "public": False}],
                "users": [{"id": 5, "name": "Pat"}, {"id": 6, "name": "Ana Martínez"}]})
        return httpx.Response(200, json={"ticket": {"id": 42, "subject": "S", "status": "solved",
                                                    "priority": "low", "updated_at": "2026-01-01T00:00:00Z"}})
    calls = live(handler)
    result = run(tools.get_ticket_status(42))
    assert [c["path"] for c in calls] == ["/api/v2/tickets/42.json", "/api/v2/tickets/42/comments.json"]
    assert calls[1]["params"] == {"include": "users"}
    assert result["is_resolved"] is True
    assert result["ticket"]["updated_at"] == "2026-01-01T00:00:00Z"
    assert result["latest_public_comment"]["author_name"] == "Ana Martínez"
    assert result["latest_public_comment"]["body"] == "Replacement shipped."


def test_live_create_offline_ticket_payload(live):
    calls = live(lambda r: httpx.Response(201, json={"ticket": {"id": 501, "status": "new", "priority": "urgent"}}))
    result = run(tools.create_offline_ticket("Refund", "Please refund order 9.", "pat@example.com",
                                             customer_name="Pat", priority="urgent", reference_id="ORD-9"))
    assert result == {"status": "SUCCESS", "ticket_id": 501, "ticket_status": "new", "priority": "urgent"}
    ticket = calls[0]["body"]["ticket"]
    assert calls[0]["method"] == "POST" and calls[0]["path"] == "/api/v2/tickets.json"
    assert ticket["requester"] == {"name": "Pat", "email": "pat@example.com"}
    assert ticket["tags"] == ["cxas-offline-ticket", "cxas-chat"]
    assert ticket["priority"] == "urgent"
    assert ticket["external_id"] == "ORD-9"
    assert ticket["comment"]["public"] is True
    assert ticket["comment"]["body"].startswith("Please refund order 9.")


def test_live_add_ticket_comment_verifies_owner_then_posts_as_requester(live):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"ticket": {"id": 8, "requester_id": 31},
                                             "users": [{"id": 31, "email": "Pat@Example.com"}]})
        return httpx.Response(200, json={"audit": {"events": [{"type": "Comment", "id": 9001}]}})
    calls = live(handler)
    result = run(tools.add_ticket_comment(8, "More details", customer_email="pat@example.com"))
    assert result == {"status": "SUCCESS", "ticket_id": 8, "comment_id": 9001}
    assert calls[0]["params"] == {"include": "users"}
    assert calls[1]["method"] == "PUT"
    assert calls[1]["body"] == {"ticket": {"comment": {"body": "More details", "public": True, "author_id": 31}}}


def test_live_add_ticket_comment_ownership_mismatch_never_writes(live):
    calls = live(lambda r: httpx.Response(200, json={"ticket": {"id": 8, "requester_id": 31},
                                                     "users": [{"id": 31, "email": "owner@example.com"}]}))
    result = run(tools.add_ticket_comment(8, "x", customer_email="pat@example.com"))
    assert result["error_code"] == "TICKET_OWNERSHIP_MISMATCH"
    assert [c["method"] for c in calls] == ["GET"]


@pytest.mark.parametrize("status_code, code", [
    (429, "ZENDESK_RATE_LIMITED"), (401, "ZENDESK_AUTH_FAILED"),
    (404, "TICKET_NOT_FOUND"), (503, "ZENDESK_UNAVAILABLE"),
])
def test_live_failures_are_structured(live, status_code, code):
    live(lambda r: httpx.Response(status_code, json={"error": "x"}))
    for result in (run(tools.search_tickets("pat@example.com")),
                   run(tools.get_ticket_status(1)),
                   run(tools.create_offline_ticket("s", "d", "pat@example.com")),
                   run(tools.add_ticket_comment(1, "c"))):
        assert result["status"] == "ERROR" and result["error_code"] == code and result["agent_action"]
        assert "ticket_id" not in result


def test_live_unreachable_zendesk_is_structured(live):
    def boom(request):
        raise httpx.ConnectTimeout("timed out", request=request)
    live(boom)
    assert run(tools.search_tickets("pat@example.com"))["error_code"] == "ZENDESK_UNREACHABLE"
    assert run(tools.create_offline_ticket("s", "d", "pat@example.com"))["error_code"] == "ZENDESK_UNREACHABLE"


def test_live_mode_missing_credentials_refuses(monkeypatch):
    monkeypatch.setenv("ZENDESK_MOCK_MODE", "false")
    assert run(tools.search_tickets("pat@example.com"))["error_code"] == "ZENDESK_NOT_CONFIGURED"


# ==========================================================================
# MCP protocol (Streamable HTTP at /mcp) + REST preview
# ==========================================================================

def _tool_payload(rpc_response):
    """MCP tool results arrive as a single JSON text content block."""
    content = rpc_response["result"]["content"]
    assert content[0]["type"] == "text"
    return json.loads(content[0]["text"])


def _rpc(c, method, params, path="/mcp", rid=1):
    r = c.post(path, headers=MCP_HEADERS, json={"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    assert r.status_code == 200, r.text
    return r.json()


def test_mcp_endpoint_lists_the_four_tools_with_schemas():
    with TestClient(app) as c:
        listed = _rpc(c, "tools/list", {})["result"]["tools"]
    by_name = {t["name"]: t for t in listed}
    assert set(by_name) == {"search_tickets", "get_ticket_status", "create_offline_ticket", "add_ticket_comment"}
    assert by_name["search_tickets"]["inputSchema"]["required"] == ["customer_email"]
    assert by_name["get_ticket_status"]["inputSchema"]["properties"]["ticket_id"]["type"] == "integer"
    assert set(by_name["create_offline_ticket"]["inputSchema"]["required"]) == {
        "subject", "description", "customer_email"}


def test_mcp_initialize_and_trailing_slash_endpoint():
    with TestClient(app) as c:
        init = _rpc(c, "initialize", {"protocolVersion": "2025-03-26", "capabilities": {},
                                      "clientInfo": {"name": "pytest", "version": "1"}}, path="/mcp/")
    assert init["result"]["serverInfo"]["name"] == "zendesk-connector"


def test_mcp_tools_call_round_trip_through_the_sandbox():
    with TestClient(app) as c:
        created = _rpc(c, "tools/call", {"name": "create_offline_ticket", "arguments": {
            "subject": "Via MCP", "description": "Created over the wire.", "customer_email": "pat@example.com"}})
        payload = _tool_payload(created)
        assert payload["status"] == "SUCCESS"
        status = _rpc(c, "tools/call", {"name": "get_ticket_status",
                                        "arguments": {"ticket_id": payload["ticket_id"]}}, rid=2)
    assert _tool_payload(status)["ticket"]["subject"] == "Via MCP"
    assert status["result"]["isError"] is False


def test_rest_preview_route():
    c = TestClient(app)
    ok = c.post("/api/mcp-preview/create_offline_ticket",
                json={"subject": "s", "description": "d", "customer_email": "pat@example.com"}).json()
    assert ok["status"] == "SUCCESS"
    found = c.post("/api/mcp-preview/search_tickets", json={"customer_email": "pat@example.com"}).json()
    assert found["count"] == 1
    assert c.post("/api/mcp-preview/delete_everything", json={}).status_code == 404
    assert c.post("/api/mcp-preview/search_tickets", json={"bogus": 1}).status_code == 422
