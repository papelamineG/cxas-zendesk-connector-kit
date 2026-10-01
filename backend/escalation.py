"""
Live human escalation -- CX Agent Studio <-> Zendesk relay.

The browser never holds the Zendesk credential. It calls this service, which
holds the token server-side and talks to Zendesk on its behalf.

Why poll a ticket's public comments rather than Zendesk's live-messaging
product: ticket comments work on every Zendesk plan, need no Sunshine /
Switchboard configuration, and reuse a single API token.

Two modes, ONE code path
------------------------
  * Live mode   -- ``ZENDESK_SUBDOMAIN`` + ``ZENDESK_EMAIL`` + ``ZENDESK_API_TOKEN``
                   are set and ``ZENDESK_MOCK_MODE`` is not ``"true"``. Requests go
                   to ``https://{subdomain}.zendesk.com/api/v2/...``.
  * Mock mode   -- ``ZENDESK_MOCK_MODE=true``, or no ``ZENDESK_API_TOKEN`` at all.
                   Requests go to :class:`MockZendesk`, an in-process emulator of
                   the handful of Zendesk v2 endpoints the relay uses, mounted as
                   an ``httpx.MockTransport``.

Because the sandbox is a *transport* rather than a second implementation, the
requester identity, the comment discriminator and the status handback run the
exact same code in both modes. What a customer tests locally in 60 seconds is
what runs against their real Zendesk.

Setting ``ZENDESK_MOCK_MODE=false`` explicitly is a strict opt-in to live mode:
missing credentials then fail loudly with ``ZENDESK_NOT_CONFIGURED`` instead of
silently falling back to the sandbox.

Zero silent failure: every Zendesk failure returns a structured error carrying
``agent_action``. A failed escalation must NEVER report a ticket id.
"""
from __future__ import annotations

import base64
import itertools
import json
import os
import re
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

APP_BRAND = os.getenv("APP_BRAND", "Acme Support")

REQUEST_TIMEOUT_S = float(os.getenv("ZENDESK_TIMEOUT_S", "15"))
ESCALATION_TAGS = ["live-escalation", "cxas-chat"]
TERMINAL_STATUSES = ("solved", "closed")
VALID_STATUSES = ("new", "open", "pending", "hold", "solved", "closed")
MOCK_BASE_URL = "https://sandbox.zendesk.invalid"

_EMAIL_RE = re.compile(r"^[^@\s\"'<>]+@[^@\s\"'<>]+\.[^@\s\"'<>]+$")


# ---------------------------------------------------------------------------
# Configuration (read at call time so tests and operators can flip env vars)
# ---------------------------------------------------------------------------

def zendesk_settings() -> Dict[str, str]:
    return {
        "subdomain": os.getenv("ZENDESK_SUBDOMAIN", "").strip(),
        "email": os.getenv("ZENDESK_EMAIL", "").strip(),
        "token": os.getenv("ZENDESK_API_TOKEN", "").strip(),
        "mock_flag": os.getenv("ZENDESK_MOCK_MODE", "").strip().lower(),
    }


def is_mock_mode() -> bool:
    """Sandbox when explicitly requested, or when no API token is configured."""
    cfg = zendesk_settings()
    if cfg["mock_flag"] == "true":
        return True
    if cfg["mock_flag"] == "false":
        return False
    return not cfg["token"]


def current_mode() -> str:
    return "mock_simulator" if is_mock_mode() else "zendesk_live"


def _config_error() -> Optional[Dict[str, Any]]:
    """Live mode with incomplete credentials must refuse loudly."""
    if is_mock_mode():
        return None
    cfg = zendesk_settings()
    missing = [name for name, key in (("ZENDESK_SUBDOMAIN", "subdomain"),
                                      ("ZENDESK_EMAIL", "email"),
                                      ("ZENDESK_API_TOKEN", "token")) if not cfg[key]]
    if missing:
        return _error(
            "ZENDESK_NOT_CONFIGURED",
            f"Live Zendesk mode is active but {', '.join(missing)} is not set on this service.",
            "Tell the customer a specialist cannot be reached right now. This needs an operator.",
        )
    return None


# ---------------------------------------------------------------------------
# Transport -- the single choke point for every Zendesk call
# ---------------------------------------------------------------------------

# Tests inject an ``httpx.MockTransport`` here to impersonate a LIVE Zendesk.
_LIVE_TRANSPORT: Optional[Any] = None


def set_live_transport(transport: Optional[Any]) -> None:
    """Override the transport used in live mode (``None`` restores real HTTP)."""
    global _LIVE_TRANSPORT
    _LIVE_TRANSPORT = transport


def _auth_header() -> str:
    """Zendesk API-token auth: '<email>/token:<token>' as HTTP basic."""
    cfg = zendesk_settings()
    raw = f"{cfg['email']}/token:{cfg['token']}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


def _client_kwargs(asynchronous: bool = False) -> Dict[str, Any]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if is_mock_mode():
        return {
            "base_url": MOCK_BASE_URL,
            "headers": headers,
            "timeout": REQUEST_TIMEOUT_S,
            "transport": httpx.MockTransport(MOCK_ZENDESK.handle),
        }
    headers["Authorization"] = _auth_header()
    kwargs: Dict[str, Any] = {
        "base_url": f"https://{zendesk_settings()['subdomain']}.zendesk.com",
        "headers": headers,
        "timeout": REQUEST_TIMEOUT_S,
    }
    if _LIVE_TRANSPORT is not None:
        kwargs["transport"] = _LIVE_TRANSPORT
    return kwargs


def _zendesk_request(method: str, path: str, json_body: Optional[dict] = None,
                     params: Optional[dict] = None) -> httpx.Response:
    """Synchronous Zendesk call. Raises ``httpx.HTTPError`` on transport failure."""
    with httpx.Client(**_client_kwargs()) as client:
        return client.request(method, path, json=json_body, params=params)


async def _zendesk_request_async(method: str, path: str, json_body: Optional[dict] = None,
                                 params: Optional[dict] = None) -> httpx.Response:
    """Async twin of :func:`_zendesk_request`, used by the MCP tools."""
    async with httpx.AsyncClient(**_client_kwargs(asynchronous=True)) as client:
        return await client.request(method, path, json=json_body, params=params)


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------

def _error(code: str, message: str, agent_action: str) -> Dict[str, Any]:
    return {"status": "ERROR", "error_code": code, "message": message, "agent_action": agent_action}


def _safe_json(response: httpx.Response) -> Dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _classify_failure(response: httpx.Response) -> Dict[str, Any]:
    """Turn a non-success Zendesk response into a structured, actionable error."""
    body = (response.text or "")[:400]
    code = response.status_code
    if code == 429:
        return _error("ZENDESK_RATE_LIMITED", f"Zendesk rate limit hit (HTTP 429): {body}",
                      "Tell the customer you could not reach a specialist right now and offer to follow up by email.")
    if code in (401, 403):
        return _error("ZENDESK_AUTH_FAILED", f"Zendesk rejected our credentials (HTTP {code}): {body}",
                      "Tell the customer a specialist cannot be reached right now. Do not retry; this needs an operator.")
    if code == 404:
        return _error("TICKET_NOT_FOUND", f"Zendesk could not find that ticket (HTTP 404): {body}",
                      "Ask the customer to double-check the ticket number, or offer to open a new ticket.")
    if code == 422:
        return _error("ZENDESK_VALIDATION_FAILED", f"Zendesk rejected the request (HTTP 422): {body}",
                      "Tell the customer the request could not be completed and offer to open a new ticket instead.")
    return _error("ZENDESK_UNAVAILABLE", f"Zendesk returned HTTP {code}: {body}",
                  "Tell the customer you could not reach a specialist right now and offer to follow up by email.")


def _unreachable(exc: Exception, context: str) -> Dict[str, Any]:
    return _error("ZENDESK_UNREACHABLE", f"Could not reach Zendesk {context}: {exc}",
                  "Tell the customer you could not reach a specialist right now and offer to follow up by email.")


def is_valid_email(value: str) -> bool:
    return bool(value) and bool(_EMAIL_RE.match(value.strip()))


# ---------------------------------------------------------------------------
# Relay state
# ---------------------------------------------------------------------------

# Per-ticket relay state. In-memory by design: losing it on restart only ends an
# in-flight escalation. `last_seen_comment_id` and `our_comment_ids` tell the
# specialist's replies apart from our own writes.
_STATE: Dict[str, Dict[str, Any]] = {}
_STATE_LOCK = threading.Lock()


def get_state(ticket_id: str) -> Optional[Dict[str, Any]]:
    with _STATE_LOCK:
        return _STATE.get(str(ticket_id))


def _subject(customer_name: str, order_id: Optional[str]) -> str:
    suffix = f" — {order_id}" if order_id else ""
    return f"Chat escalation — {customer_name}{suffix}"


def _comment_body(summary: str, order_id: Optional[str], language: str) -> str:
    lines = [summary, ""]
    if order_id:
        lines.append(f"Order: {order_id}")
    lines.append(f"Conversation language: {language}")
    lines.append(f"Escalated from the {APP_BRAND} AI assistant (CX Agent Studio).")
    lines.append("")
    lines.append("The customer asked to speak to a person. They are waiting in the live chat —")
    lines.append("a public reply on this ticket reaches them in the chat window within seconds.")
    return "\n".join(lines)


def start_escalation(summary: str, customer_name: str, customer_email: str,
                     language: str = "en", order_id: Optional[str] = None) -> Dict[str, Any]:
    """Open the escalation ticket and begin tracking it.

    The requester is set explicitly to the customer. Without that, requester,
    submitter and comment author are all the API account, and the specialist's
    replies become indistinguishable from our own writes.
    """
    config_error = _config_error()
    if config_error:
        return config_error
    if not (summary or "").strip():
        return _error("INVALID_SUMMARY", "The escalation summary is empty.",
                      "Summarise the customer's issue in one sentence before escalating.")
    if not is_valid_email(customer_email):
        return _error("INVALID_CUSTOMER_EMAIL", f"{customer_email!r} is not a valid email address.",
                      "Ask the customer for a valid email address before escalating.")

    payload = {
        "ticket": {
            "subject": _subject(customer_name, order_id),
            "requester": {"name": customer_name, "email": customer_email},
            "comment": {"body": _comment_body(summary, order_id, language), "public": True},
            "tags": list(ESCALATION_TAGS),
            "priority": os.getenv("ZENDESK_ESCALATION_PRIORITY", "urgent"),
        }
    }

    try:
        response = _zendesk_request("POST", "/api/v2/tickets.json", json_body=payload)
    except httpx.HTTPError as exc:
        return _unreachable(exc, "to open the escalation")

    if response.status_code not in (200, 201):
        return _classify_failure(response)

    ticket = _safe_json(response).get("ticket") or {}
    ticket_id = ticket.get("id")
    if not ticket_id:
        return _error(
            "ZENDESK_MALFORMED_RESPONSE",
            f"Zendesk accepted the request but returned no ticket id: {response.text[:300]}",
            "Tell the customer you could not reach a specialist right now and offer to follow up by email.",
        )

    ticket_id = str(ticket_id)
    with _STATE_LOCK:
        _STATE[ticket_id] = {
            "requester_id": ticket.get("requester_id"),
            "last_seen_comment_id": 0,
            "our_comment_ids": set(),
            "created_at": time.time(),
            "customer_name": customer_name,
            "customer_email": customer_email,
            "language": language,
            "summary": summary,
            "order_id": order_id,
            "subject": ticket.get("subject"),
            "ticket_status": ticket.get("status", "new"),
            "specialist_joined": False,
            "mode": current_mode(),
        }

    return {"status": "SUCCESS", "ticket_id": ticket_id, "escalation_id": ticket_id,
            "ticket_status": ticket.get("status", "new"), "mode": current_mode()}


def _clean_comment_body(comment: Dict[str, Any]) -> str:
    """The text to show the customer, with the agent editor's padding removed.

    Zendesk's agent workspace pads ``plain_body`` with non-breaking-space lines
    (body ``'hello'`` vs plain_body ``'hello\\n&nbsp;\\n&nbsp;'``). ``body`` is
    preferred and both are normalised.
    """
    text = comment.get("body") or comment.get("plain_body") or ""
    text = text.replace("&nbsp;", " ").replace("\xa0", " ")
    lines = [line.rstrip() for line in text.splitlines()]
    out: List[str] = []
    blanks = 0
    for line in lines:
        if line.strip():
            blanks = 0
            out.append(line)
        else:
            blanks += 1
            if blanks == 1 and out:      # keep one blank line, drop runs
                out.append("")
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out).strip()


_FALLBACK_NAMES = {"en": "Customer Care", "es": "Atención al Cliente",
                   "fr": "Service Client", "de": "Kundenservice"}


def _fallback_name(language: str) -> str:
    """Shown only if Zendesk did not side-load the author. Never a numeric id."""
    return _FALLBACK_NAMES.get(str(language or "en").lower()[:2], _FALLBACK_NAMES["en"])


def _is_from_the_specialist(comment: Dict[str, Any], requester_id: Any,
                            our_comment_ids: set) -> bool:
    """Is this comment something the customer should see as a reply?

      * not public            -- internal notes are for the agent, never the customer
      * authored by requester -- the customer's own words; echoing them back is a bug
      * one of ours           -- forwarded customer replies are authored by the SAME
                                 API account as the specialist, so the relay
                                 remembers what it wrote.
    """
    if not comment.get("public"):
        return False
    if requester_id is not None and comment.get("author_id") == requester_id:
        return False
    if comment.get("id") in our_comment_ids:
        return False
    return True


def poll_messages(ticket_id: str, since: Optional[int] = None,
                  language: Optional[str] = None) -> Dict[str, Any]:
    """Return specialist replies after ``since``, plus the ticket's status.

    **The caller's cursor wins.** The relay cannot know whether its response
    reached the browser or was rendered. The browser passes ``since`` and only
    advances it after a successful render, making delivery at-least-once with a
    client-side acknowledgement. The server-side cursor is only a fallback for
    callers that omit ``since``.
    """
    config_error = _config_error()
    if config_error:
        return config_error
    state = get_state(ticket_id)
    if state is None:
        return _error(
            "UNKNOWN_ESCALATION",
            f"No active escalation is being tracked for ticket {ticket_id}.",
            "Tell the customer the conversation could not be resumed and offer to follow up by email.",
        )
    language = language or state.get("language") or "en"

    try:
        comments_response = _zendesk_request(
            "GET", f"/api/v2/tickets/{ticket_id}/comments.json", params={"include": "users"})
        if comments_response.status_code != 200:
            return _classify_failure(comments_response)
        ticket_response = _zendesk_request("GET", f"/api/v2/tickets/{ticket_id}.json")
        if ticket_response.status_code != 200:
            return _classify_failure(ticket_response)
    except httpx.HTTPError as exc:
        return _error(
            "ZENDESK_UNREACHABLE",
            f"Could not reach Zendesk while polling: {exc}",
            "Tell the customer the specialist connection dropped and offer to follow up by email.",
        )

    payload = _safe_json(comments_response)
    names = {u.get("id"): u.get("name") for u in (payload.get("users") or [])}
    ticket_status = (_safe_json(ticket_response).get("ticket") or {}).get("status")

    with _STATE_LOCK:
        tracked = _STATE.get(str(ticket_id))
        if tracked is None:
            return _error(
                "UNKNOWN_ESCALATION",
                f"Escalation {ticket_id} stopped being tracked mid-poll.",
                "Tell the customer the conversation could not be resumed and offer to follow up by email.",
            )
        requester_id = tracked["requester_id"]
        our_comment_ids = tracked["our_comment_ids"]
        # `since` from the caller is authoritative; see the docstring.
        last_seen = since if since is not None else tracked["last_seen_comment_id"]

        fresh = []
        highest = tracked["last_seen_comment_id"]
        specialist_has_replied = False

        for comment in payload.get("comments") or []:
            if not _is_from_the_specialist(comment, requester_id, our_comment_ids):
                continue
            specialist_has_replied = True
            comment_id = comment.get("id") or 0
            if comment_id <= last_seen:
                continue
            highest = max(highest, comment_id)
            fresh.append({
                "id": comment_id,
                # Read the name from Zendesk so chat and ticket never disagree.
                "author_name": names.get(comment.get("author_id")) or _fallback_name(language),
                "body": _clean_comment_body(comment),
                "created_at": comment.get("created_at"),
            })

        tracked["last_seen_comment_id"] = highest
        tracked["specialist_joined"] = tracked.get("specialist_joined") or specialist_has_replied
        if ticket_status:
            tracked["ticket_status"] = ticket_status

    return {
        "status": "SUCCESS",
        "messages": fresh,
        # "specialist", never "agent": "agent" is reserved for the CXAS agent.
        "specialist_joined": specialist_has_replied,
        "ticket_status": ticket_status,
    }


def _comment_id_from_audit(response: httpx.Response) -> Optional[int]:
    for event in ((_safe_json(response).get("audit") or {}).get("events") or []):
        if event.get("type") == "Comment":
            return event.get("id")
    return None


def post_customer_reply(ticket_id: str, body: str) -> Dict[str, Any]:
    """Forward the customer's message to the specialist, as a public comment.

    The relay authenticates as the API account, which is also the author of the
    specialist's replies, so the id of every comment written here is recorded
    and excluded from what is later delivered back to the customer.
    """
    config_error = _config_error()
    if config_error:
        return config_error
    text = (body or "").strip()
    if not text:
        return _error("EMPTY_REPLY", "Refusing to post an empty comment.",
                      "Ask the customer to type a message before sending.")

    if get_state(ticket_id) is None:
        return _error(
            "UNKNOWN_ESCALATION",
            f"No active escalation is being tracked for ticket {ticket_id}.",
            "Tell the customer the conversation could not be resumed and offer to follow up by email.",
        )

    try:
        response = _zendesk_request(
            "PUT", f"/api/v2/tickets/{ticket_id}.json",
            json_body={"ticket": {"comment": {"body": text, "public": True}}},
        )
    except httpx.HTTPError as exc:
        return _error("ZENDESK_UNREACHABLE",
                      f"Could not reach Zendesk to deliver the customer's message: {exc}",
                      "Tell the customer their message could not be delivered and to try again.")

    if response.status_code not in (200, 201):
        return _classify_failure(response)

    comment_id = _comment_id_from_audit(response)
    if comment_id is None:
        # Not fatal for delivery, but the customer would see it echoed back.
        return _error(
            "REPLY_NOT_TRACKED",
            "Zendesk accepted the comment but returned no comment id; it cannot be "
            "excluded from the poll and may be echoed back to the customer.",
            "The message reached the specialist. Do not resend it.",
        )

    with _STATE_LOCK:
        tracked = _STATE.get(str(ticket_id))
        if tracked is not None:
            tracked["our_comment_ids"].add(comment_id)

    return {"status": "SUCCESS", "comment_id": comment_id}


def list_active_escalations(include_ended: bool = False) -> List[Dict[str, Any]]:
    """Escalations the relay is tracking, newest first (for the simulator console)."""
    with _STATE_LOCK:
        snapshot = {tid: dict(st) for tid, st in _STATE.items()}
    out = []
    for tid, st in snapshot.items():
        status = st.get("ticket_status")
        if is_mock_mode():
            fresh = MOCK_ZENDESK.ticket_status(int(tid)) if tid.isdigit() else None
            status = fresh or status
        if not include_ended and status in TERMINAL_STATUSES:
            continue
        out.append({
            "escalation_id": tid,
            "ticket_id": tid,
            "customer_name": st.get("customer_name"),
            "customer_email": st.get("customer_email"),
            "summary": st.get("summary"),
            "language": st.get("language"),
            "order_id": st.get("order_id"),
            "ticket_status": status,
            "specialist_joined": bool(st.get("specialist_joined")),
            "created_at": st.get("created_at"),
        })
    out.sort(key=lambda e: e.get("created_at") or 0, reverse=True)
    return out


# ---------------------------------------------------------------------------
# Simulator helpers (play the human on the far side of Zendesk)
# ---------------------------------------------------------------------------

def _simulator_guard() -> Optional[Dict[str, Any]]:
    if is_mock_mode() or os.getenv("ALLOW_LIVE_SIMULATOR", "").lower() == "true":
        return None
    return _error(
        "SIMULATOR_DISABLED",
        "The specialist simulator is disabled in live Zendesk mode. Reply from the Zendesk "
        "agent workspace, or set ALLOW_LIVE_SIMULATOR=true for a supervised demo.",
        "Use the Zendesk agent workspace to reply to this ticket.",
    )


def simulate_specialist_reply(escalation_id: str, body: str,
                              author_name: str = "Sarah (Zendesk Specialist)") -> Dict[str, Any]:
    """Post a public reply on the ticket as if a Zendesk specialist wrote it.

    In mock mode the comment is authored by a sandbox specialist user named
    ``author_name``. In live mode (only with ``ALLOW_LIVE_SIMULATOR=true``) it is
    a real public comment authored by the API account.
    """
    guard = _simulator_guard() or _config_error()
    if guard:
        return guard
    text = (body or "").strip()
    if not text:
        return _error("EMPTY_REPLY", "Refusing to post an empty specialist reply.",
                      "Type a reply before sending.")
    if get_state(escalation_id) is None:
        return _error("UNKNOWN_ESCALATION", f"No active escalation is being tracked for {escalation_id}.",
                      "Pick an active escalation from the list.")
    comment: Dict[str, Any] = {"body": text, "public": True}
    if is_mock_mode():
        comment["author_id"] = MOCK_ZENDESK.ensure_agent(author_name or "Zendesk Specialist")
    try:
        response = _zendesk_request("PUT", f"/api/v2/tickets/{escalation_id}.json",
                                    json_body={"ticket": {"comment": comment}})
    except httpx.HTTPError as exc:
        return _unreachable(exc, "to post the simulated reply")
    if response.status_code not in (200, 201):
        return _classify_failure(response)
    return {"status": "SUCCESS", "escalation_id": str(escalation_id),
            "comment_id": _comment_id_from_audit(response), "author_name": author_name}


def simulate_ticket_status(escalation_id: str, status: str = "solved") -> Dict[str, Any]:
    """Change the ticket status as a specialist would (``solved`` triggers handback)."""
    guard = _simulator_guard() or _config_error()
    if guard:
        return guard
    if status not in VALID_STATUSES:
        return _error("INVALID_STATUS", f"{status!r} is not a Zendesk ticket status.",
                      f"Use one of: {', '.join(VALID_STATUSES)}.")
    if get_state(escalation_id) is None:
        return _error("UNKNOWN_ESCALATION", f"No active escalation is being tracked for {escalation_id}.",
                      "Pick an active escalation from the list.")
    try:
        response = _zendesk_request("PUT", f"/api/v2/tickets/{escalation_id}.json",
                                    json_body={"ticket": {"status": status}})
    except httpx.HTTPError as exc:
        return _unreachable(exc, "to update the ticket status")
    if response.status_code not in (200, 201):
        return _classify_failure(response)
    new_status = (_safe_json(response).get("ticket") or {}).get("status", status)
    with _STATE_LOCK:
        tracked = _STATE.get(str(escalation_id))
        if tracked is not None:
            tracked["ticket_status"] = new_status
    return {"status": "SUCCESS", "escalation_id": str(escalation_id), "ticket_status": new_status}


# ---------------------------------------------------------------------------
# MockZendesk -- in-process emulator of the Zendesk v2 endpoints we use
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class MockZendesk:
    """A small, faithful-enough Zendesk v2 sandbox.

    Emulated: POST/GET/PUT ``/api/v2/tickets[/{id}].json``,
    GET ``/api/v2/tickets/{id}/comments.json``, GET ``/api/v2/search.json`` and
    GET ``/api/v2/users/me.json``. Like real Zendesk, supplying ``requester``
    makes the customer the author of the opening comment, and replies posted by
    the API token are authored by the API account.
    """

    API_USER_NAME = "API Integration"

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._ticket_ids = itertools.count(10001)
            self._comment_ids = itertools.count(50001)
            self._user_ids = itertools.count(800001)
            self.users: Dict[int, Dict[str, Any]] = {}
            self.tickets: Dict[int, Dict[str, Any]] = {}
            self.comments: Dict[int, List[Dict[str, Any]]] = {}
            self.api_user_id = self._new_user(f"{APP_BRAND} {self.API_USER_NAME}",
                                              "api@sandbox.invalid", "admin")

    # -- users -------------------------------------------------------------
    def _new_user(self, name: str, email: Optional[str], role: str) -> int:
        uid = next(self._user_ids)
        self.users[uid] = {"id": uid, "name": name, "email": email, "role": role}
        return uid

    def ensure_customer(self, name: str, email: str) -> int:
        with self._lock:
            for user in self.users.values():
                if user["role"] == "end-user" and (user["email"] or "").lower() == email.lower():
                    return user["id"]
            return self._new_user(name or email, email, "end-user")

    def ensure_agent(self, name: str) -> int:
        with self._lock:
            for user in self.users.values():
                if user["role"] == "agent" and user["name"] == name:
                    return user["id"]
            return self._new_user(name, None, "agent")

    def ticket_status(self, ticket_id: int) -> Optional[str]:
        with self._lock:
            ticket = self.tickets.get(ticket_id)
            return ticket["status"] if ticket else None

    # -- HTTP --------------------------------------------------------------
    @staticmethod
    def _json(status: int, payload: Dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json=payload)

    def _not_found(self) -> httpx.Response:
        return self._json(404, {"error": "RecordNotFound", "description": "Not found"})

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method.upper()
        params = dict(request.url.params.multi_items())
        body: Dict[str, Any] = {}
        if request.content:
            try:
                body = json.loads(request.content.decode("utf-8"))
            except ValueError:
                return self._json(400, {"error": "InvalidJSON"})

        with self._lock:
            if path == "/api/v2/tickets.json" and method == "POST":
                return self._create_ticket(body)
            if path == "/api/v2/search.json" and method == "GET":
                return self._search(params.get("query", ""))
            if path == "/api/v2/users/me.json" and method == "GET":
                return self._json(200, {"user": self.users[self.api_user_id]})
            match = re.fullmatch(r"/api/v2/tickets/(\d+)(/comments)?\.json", path)
            if match:
                ticket_id = int(match.group(1))
                if ticket_id not in self.tickets:
                    return self._not_found()
                if match.group(2) and method == "GET":
                    return self._list_comments(ticket_id, params)
                if not match.group(2) and method == "GET":
                    return self._get_ticket(ticket_id, params)
                if not match.group(2) and method == "PUT":
                    return self._update_ticket(ticket_id, body)
        return self._json(404, {"error": "InvalidEndpoint", "description": f"{method} {path}"})

    def _ticket_json(self, ticket_id: int) -> Dict[str, Any]:
        return dict(self.tickets[ticket_id])

    def _create_ticket(self, body: Dict[str, Any]) -> httpx.Response:
        data = body.get("ticket") or {}
        comment = data.get("comment") or {}
        if not data.get("subject") and not comment.get("body"):
            return self._json(422, {"error": "RecordInvalid", "details": {"base": ["subject or comment required"]}})
        requester = data.get("requester") or {}
        if requester.get("email"):
            requester_id = self.ensure_customer(requester.get("name", ""), requester["email"])
        else:
            requester_id = self.api_user_id
        ticket_id = next(self._ticket_ids)
        now = _now_iso()
        self.tickets[ticket_id] = {
            "id": ticket_id,
            "subject": data.get("subject") or (comment.get("body") or "")[:60],
            "status": data.get("status") or "new",
            "priority": data.get("priority") or "normal",
            "tags": list(data.get("tags") or []),
            "external_id": data.get("external_id"),
            "requester_id": requester_id,
            "submitter_id": requester_id,
            "created_at": now,
            "updated_at": now,
        }
        self.comments[ticket_id] = []
        if comment.get("body"):
            self._add_comment(ticket_id, requester_id, comment["body"], bool(comment.get("public", True)))
        return self._json(201, {"ticket": self._ticket_json(ticket_id)})

    def _add_comment(self, ticket_id: int, author_id: int, body: str, public: bool) -> int:
        comment_id = next(self._comment_ids)
        self.comments[ticket_id].append({
            "id": comment_id, "type": "Comment", "author_id": author_id, "body": body,
            "plain_body": body, "public": public, "created_at": _now_iso(),
        })
        self.tickets[ticket_id]["updated_at"] = _now_iso()
        return comment_id

    def _get_ticket(self, ticket_id: int, params: Dict[str, str]) -> httpx.Response:
        payload: Dict[str, Any] = {"ticket": self._ticket_json(ticket_id)}
        if "users" in params.get("include", ""):
            payload["users"] = [self.users[self.tickets[ticket_id]["requester_id"]]]
        return self._json(200, payload)

    def _list_comments(self, ticket_id: int, params: Dict[str, str]) -> httpx.Response:
        comments = [dict(c) for c in self.comments[ticket_id]]
        payload: Dict[str, Any] = {"comments": comments, "count": len(comments)}
        if "users" in params.get("include", ""):
            ids = {c["author_id"] for c in comments}
            payload["users"] = [self.users[uid] for uid in ids if uid in self.users]
        return self._json(200, payload)

    def _update_ticket(self, ticket_id: int, body: Dict[str, Any]) -> httpx.Response:
        ticket = self.tickets[ticket_id]
        data = body.get("ticket") or {}
        if ticket["status"] == "closed":
            return self._json(422, {"error": "RecordInvalid",
                                    "description": "Closed tickets cannot be updated"})
        events: List[Dict[str, Any]] = []
        comment = data.get("comment") or {}
        if comment.get("body"):
            author_id = comment.get("author_id") or self.api_user_id
            if author_id not in self.users:
                return self._json(422, {"error": "RecordInvalid", "description": "Unknown author"})
            cid = self._add_comment(ticket_id, author_id, comment["body"], bool(comment.get("public", True)))
            events.append({"id": cid, "type": "Comment", "public": bool(comment.get("public", True)),
                           "author_id": author_id})
            if self.users[author_id]["role"] != "end-user" and ticket["status"] == "new":
                ticket["status"] = "open"
        if data.get("status"):
            if data["status"] not in VALID_STATUSES:
                return self._json(422, {"error": "RecordInvalid", "description": "Invalid status"})
            ticket["status"] = data["status"]
        if data.get("priority"):
            ticket["priority"] = data["priority"]
        ticket["updated_at"] = _now_iso()
        return self._json(200, {"ticket": self._ticket_json(ticket_id),
                                "audit": {"id": next(self._comment_ids), "events": events}})

    def _search(self, query: str) -> httpx.Response:
        tokens = dict(t.split(":", 1) for t in query.split() if ":" in t)
        if tokens.get("type") != "ticket":
            return self._json(200, {"results": [], "count": 0})
        results = []
        for ticket in self.tickets.values():
            requester = self.users.get(ticket["requester_id"], {})
            if "requester" in tokens and (requester.get("email") or "").lower() != tokens["requester"].lower():
                continue
            if "status" in tokens and ticket["status"] != tokens["status"]:
                continue
            results.append({**ticket, "result_type": "ticket"})
        results.sort(key=lambda t: (t["updated_at"], t["id"]), reverse=True)
        return self._json(200, {"results": results, "count": len(results)})


MOCK_ZENDESK = MockZendesk()


def reset_for_tests() -> None:
    """Forget every tracked escalation and wipe the sandbox."""
    with _STATE_LOCK:
        _STATE.clear()
    MOCK_ZENDESK.reset()
