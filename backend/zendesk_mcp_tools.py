"""
Server-side Zendesk MCP tools for CX Agent Studio.

These four tools are what the CXAS agent calls *server-to-server* through the
``zendesk_mcp_connector`` MCP toolset (Streamable HTTP at ``/mcp``). They cover
the asynchronous half of support -- "what's the status of my ticket?", "open a
follow-up ticket", "add this to my ticket" -- while the live human hand-off goes
through the ``escalate_to_human`` client function and the relay in
:mod:`backend.escalation`.

Every tool is a plain ``async`` function (directly callable from tests and the
``/api/mcp-preview`` REST route) that is also registered on the
``FastMCP("zendesk-connector")`` instance below. All Zendesk traffic goes through
:func:`backend.escalation._zendesk_request_async`, so the same functions work
against the built-in sandbox (mock mode) and a real Zendesk (live mode).

Zero silent failure: every failure returns ``{"status": "ERROR", "error_code",
"message", "agent_action"}`` so the agent always knows what to tell the customer.
"""
from __future__ import annotations

import os
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from backend import escalation as esc

OFFLINE_TICKET_TAGS = ["cxas-offline-ticket", "cxas-chat"]
VALID_PRIORITIES = ("low", "normal", "high", "urgent")
SEARCH_RESULT_LIMIT = 25


def _transport_security() -> TransportSecuritySettings:
    """DNS-rebinding protection is opt-in via ``MCP_ALLOWED_HOSTS``.

    FastMCP auto-enables a localhost-only Host allowlist when bound to
    127.0.0.1, which would reject Cloud Run's ``*.run.app`` Host header. Behind
    Cloud Run / a load balancer the platform terminates the connection, so the
    allowlist is disabled unless the operator lists hosts explicitly.
    """
    hosts = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    if not hosts:
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    origins = [o.strip() for o in os.getenv("MCP_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                     allowed_hosts=hosts, allowed_origins=origins)


mcp = FastMCP(
    "zendesk-connector",
    instructions=(
        f"Zendesk ticket tools for the {esc.APP_BRAND} assistant. Use search_tickets / "
        "get_ticket_status to answer questions about existing tickets, create_offline_ticket "
        "when no live specialist is needed or available, and add_ticket_comment to add "
        "information to an existing ticket. Never invent ticket ids."
    ),
    stateless_http=True,      # Cloud Run friendly: any instance can serve any request
    json_response=True,
    streamable_http_path="/mcp",
    transport_security=_transport_security(),
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _call(method: str, path: str, json_body: Optional[dict] = None,
                params: Optional[dict] = None, context: str = "") -> Any:
    """Run one Zendesk request; return the response or a structured error dict."""
    try:
        return await esc._zendesk_request_async(method, path, json_body=json_body, params=params)
    except httpx.HTTPError as exc:
        return esc._unreachable(exc, context or "for the ticket tools")


def _ticket_summary(ticket: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": ticket.get("id"),
        "subject": ticket.get("subject"),
        "status": ticket.get("status"),
        "priority": ticket.get("priority"),
        "created_at": ticket.get("created_at"),
        "updated_at": ticket.get("updated_at"),
        "tags": ticket.get("tags") or [],
    }


def _invalid_email(value: str) -> Dict[str, Any]:
    return esc._error("INVALID_EMAIL", f"{value!r} is not a valid email address.",
                      "Ask the customer to confirm the email address on their account.")


def _invalid_ticket_id(value: Any) -> Dict[str, Any]:
    return esc._error("INVALID_TICKET_ID", f"{value!r} is not a valid ticket number.",
                      "Ask the customer for the numeric ticket number from their confirmation email.")


def _coerce_ticket_id(ticket_id: Any) -> Optional[int]:
    try:
        value = int(str(ticket_id).strip().lstrip("#"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

async def search_tickets(customer_email: str, status: str = "") -> Dict[str, Any]:
    """Find a customer's Zendesk tickets by their email address.

    Args:
        customer_email: Email address of the ticket requester.
        status: Optional status filter: new, open, pending, hold, solved or closed.

    Returns:
        {"status": "SUCCESS", "count": int, "tickets": [{id, subject, status, priority,
        created_at, updated_at, tags}]} newest first, or a structured error.
    """
    if (err := esc._config_error()):
        return err
    email = (customer_email or "").strip()
    if not esc.is_valid_email(email):
        return _invalid_email(customer_email)
    status = (status or "").strip().lower()
    if status and status not in esc.VALID_STATUSES:
        return esc._error("INVALID_STATUS", f"{status!r} is not a Zendesk ticket status.",
                          f"Use one of: {', '.join(esc.VALID_STATUSES)}, or leave it empty.")

    query = f"type:ticket requester:{email}"
    if status:
        query += f" status:{status}"
    response = await _call("GET", "/api/v2/search.json",
                           params={"query": query, "sort_by": "updated_at", "sort_order": "desc"},
                           context="to search tickets")
    if isinstance(response, dict):
        return response
    if response.status_code != 200:
        return esc._classify_failure(response)

    results = [r for r in (esc._safe_json(response).get("results") or [])
               if r.get("result_type", "ticket") == "ticket"]
    tickets = [_ticket_summary(r) for r in results[:SEARCH_RESULT_LIMIT]]
    return {"status": "SUCCESS", "count": len(results), "tickets": tickets}


async def get_ticket_status(ticket_id: int) -> Dict[str, Any]:
    """Get a ticket's status, priority, last update and latest public comment.

    Args:
        ticket_id: The numeric Zendesk ticket id.

    Returns:
        {"status": "SUCCESS", "ticket": {...}, "is_resolved": bool,
        "latest_public_comment": {author_name, body, created_at} | None}, or an error.
    """
    if (err := esc._config_error()):
        return err
    tid = _coerce_ticket_id(ticket_id)
    if tid is None:
        return _invalid_ticket_id(ticket_id)

    ticket_response = await _call("GET", f"/api/v2/tickets/{tid}.json", context="to read the ticket")
    if isinstance(ticket_response, dict):
        return ticket_response
    if ticket_response.status_code != 200:
        return esc._classify_failure(ticket_response)
    comments_response = await _call("GET", f"/api/v2/tickets/{tid}/comments.json",
                                    params={"include": "users"}, context="to read ticket comments")
    if isinstance(comments_response, dict):
        return comments_response
    if comments_response.status_code != 200:
        return esc._classify_failure(comments_response)

    ticket = esc._safe_json(ticket_response).get("ticket") or {}
    payload = esc._safe_json(comments_response)
    names = {u.get("id"): u.get("name") for u in (payload.get("users") or [])}
    public: List[Dict[str, Any]] = [c for c in (payload.get("comments") or []) if c.get("public")]
    latest = None
    if public:
        last = public[-1]
        latest = {
            "author_name": names.get(last.get("author_id")) or esc._fallback_name("en"),
            "body": esc._clean_comment_body(last),
            "created_at": last.get("created_at"),
        }
    return {
        "status": "SUCCESS",
        "ticket": _ticket_summary(ticket),
        "is_resolved": ticket.get("status") in esc.TERMINAL_STATUSES,
        "latest_public_comment": latest,
    }


async def create_offline_ticket(subject: str, description: str, customer_email: str,
                                customer_name: str = "Valued Customer", priority: str = "normal",
                                reference_id: str = "") -> Dict[str, Any]:
    """Open an asynchronous follow-up ticket (no live specialist needed).

    Args:
        subject: Short ticket subject.
        description: Full description of the customer's issue.
        customer_email: Requester email; the customer receives Zendesk's updates here.
        customer_name: Requester display name.
        priority: low, normal, high or urgent.
        reference_id: Optional order / case reference, stored as the ticket external_id.

    Returns:
        {"status": "SUCCESS", "ticket_id": int, "ticket_status": str, "priority": str},
        or a structured error. A failed call NEVER reports a ticket id.
    """
    if (err := esc._config_error()):
        return err
    if not (subject or "").strip() or not (description or "").strip():
        return esc._error("INVALID_TICKET_CONTENT", "Both subject and description are required.",
                          "Ask the customer to describe the issue, then try again.")
    email = (customer_email or "").strip()
    if not esc.is_valid_email(email):
        return _invalid_email(customer_email)
    priority = (priority or "normal").strip().lower()
    if priority not in VALID_PRIORITIES:
        return esc._error("INVALID_PRIORITY", f"{priority!r} is not a Zendesk priority.",
                          f"Use one of: {', '.join(VALID_PRIORITIES)}.")

    body_lines = [description.strip(), ""]
    if reference_id:
        body_lines.append(f"Reference: {reference_id}")
    body_lines.append(f"Created by the {esc.APP_BRAND} AI assistant (CX Agent Studio).")
    ticket: Dict[str, Any] = {
        "subject": subject.strip(),
        "requester": {"name": (customer_name or "Valued Customer").strip(), "email": email},
        "comment": {"body": "\n".join(body_lines), "public": True},
        "tags": list(OFFLINE_TICKET_TAGS),
        "priority": priority,
    }
    if reference_id:
        ticket["external_id"] = str(reference_id)

    response = await _call("POST", "/api/v2/tickets.json", json_body={"ticket": ticket},
                           context="to create the ticket")
    if isinstance(response, dict):
        return response
    if response.status_code not in (200, 201):
        return esc._classify_failure(response)
    created = esc._safe_json(response).get("ticket") or {}
    if not created.get("id"):
        return esc._error("ZENDESK_MALFORMED_RESPONSE",
                          f"Zendesk accepted the request but returned no ticket id: {response.text[:300]}",
                          "Tell the customer the ticket could not be confirmed and offer to try again.")
    return {"status": "SUCCESS", "ticket_id": created["id"],
            "ticket_status": created.get("status", "new"), "priority": created.get("priority", priority)}


async def add_ticket_comment(ticket_id: int, comment_body: str, customer_email: str = "") -> Dict[str, Any]:
    """Append a public comment from the customer to an existing ticket.

    Args:
        ticket_id: The numeric Zendesk ticket id.
        comment_body: The text to add.
        customer_email: Optional. When given, the ticket's requester must match it
            and the comment is authored as that requester; otherwise it is posted
            publicly by the integration account.

    Returns:
        {"status": "SUCCESS", "ticket_id": int, "comment_id": int | None}, or an error.
    """
    if (err := esc._config_error()):
        return err
    tid = _coerce_ticket_id(ticket_id)
    if tid is None:
        return _invalid_ticket_id(ticket_id)
    text = (comment_body or "").strip()
    if not text:
        return esc._error("EMPTY_COMMENT", "Refusing to add an empty comment.",
                          "Ask the customer what they would like to add to the ticket.")

    comment: Dict[str, Any] = {"body": text, "public": True}
    email = (customer_email or "").strip()
    if email:
        if not esc.is_valid_email(email):
            return _invalid_email(customer_email)
        lookup = await _call("GET", f"/api/v2/tickets/{tid}.json", params={"include": "users"},
                             context="to verify the ticket owner")
        if isinstance(lookup, dict):
            return lookup
        if lookup.status_code != 200:
            return esc._classify_failure(lookup)
        data = esc._safe_json(lookup)
        requester_id = (data.get("ticket") or {}).get("requester_id")
        requester = next((u for u in (data.get("users") or []) if u.get("id") == requester_id), {})
        if (requester.get("email") or "").lower() != email.lower():
            # Do not reveal who owns the ticket.
            return esc._error("TICKET_OWNERSHIP_MISMATCH",
                              "That ticket does not belong to the email address provided.",
                              "Ask the customer to confirm the ticket number and the email on their account.")
        comment["author_id"] = requester_id

    response = await _call("PUT", f"/api/v2/tickets/{tid}.json", json_body={"ticket": {"comment": comment}},
                           context="to add the comment")
    if isinstance(response, dict):
        return response
    if response.status_code not in (200, 201):
        return esc._classify_failure(response)
    return {"status": "SUCCESS", "ticket_id": tid, "comment_id": esc._comment_id_from_audit(response)}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

TOOLS: Dict[str, Callable[..., Awaitable[Dict[str, Any]]]] = {
    "search_tickets": search_tickets,
    "get_ticket_status": get_ticket_status,
    "create_offline_ticket": create_offline_ticket,
    "add_ticket_comment": add_ticket_comment,
}

# structured_output=False: results go out as one JSON text block, which every MCP
# client (CXAS included) consumes, instead of FastMCP's {"result": ...} wrapper.
for _name, _fn in TOOLS.items():
    mcp.add_tool(_fn, name=_name, description=(_fn.__doc__ or "").strip(), structured_output=False)
