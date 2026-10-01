"""
Unified FastAPI application for the CXAS <-> Zendesk connector kit.

Routes
------
  /mcp                                         Streamable-HTTP MCP endpoint (FastMCP)
  POST /api/escalation/start                   open a live escalation ticket
  GET  /api/escalation/{id}/messages?since=    specialist replies + ticket status
  POST /api/escalation/{id}/reply              forward the customer's reply
  GET  /api/escalation                         active escalations (simulator console)
  POST /api/simulator/escalation/{id}/specialist-reply
  POST /api/simulator/escalation/{id}/status
  POST /api/mcp-preview/{tool_name}            call an MCP tool over plain REST
  GET  /api/health                             mode + configuration summary
  /                                            the demo workbench (frontend/)

Run:  uvicorn backend.server:app --host 0.0.0.0 --port 8080
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from fastapi import Body, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.routing import Route

from backend import __version__
from backend import escalation as esc
from backend.zendesk_mcp_tools import TOOLS, mcp

FRONTEND_DIR = Path(os.getenv("FRONTEND_DIR", Path(__file__).resolve().parent.parent / "frontend"))


class _McpEndpoint:
    """ASGI shim that always delegates to the *current* session manager.

    ``StreamableHTTPSessionManager.run()`` is single-use per instance, so each
    application lifespan builds a fresh manager (see ``lifespan``). Routing
    through this shim keeps ``/mcp`` valid across restarts of the lifespan
    (e.g. several ``TestClient`` contexts in one test session).
    """

    async def __call__(self, scope, receive, send):
        await mcp.session_manager.handle_request(scope, receive, send)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    # FastMCP has no public API to rebuild its single-use session manager.
    mcp._session_manager = None
    mcp.streamable_http_app()    # lazily creates mcp.session_manager with our settings
    async with mcp.session_manager.run():
        yield


app = FastAPI(
    title="CXAS Zendesk Connector Kit",
    description=f"Live human escalation relay + server-side Zendesk MCP tools for {esc.APP_BRAND}.",
    version=__version__,
    lifespan=lifespan,
)

_origins = [o.strip() for o in os.getenv("CORS_ALLOW_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
)

# CXAS requires the server URL to end in /mcp or /mcp/; serve both without a redirect.
_mcp_endpoint = _McpEndpoint()
app.router.routes.append(Route("/mcp", endpoint=_mcp_endpoint))
app.router.routes.append(Route("/mcp/", endpoint=_mcp_endpoint))


# ---------------------------------------------------------------------------
# Live escalation relay
# ---------------------------------------------------------------------------

class EscalationStartRequest(BaseModel):
    summary: str = Field(..., description="One sentence describing the customer's issue, in their language.")
    customer_name: str = Field(..., description="Customer's display name, used as the ticket requester.")
    customer_email: str = Field(..., description="Customer's email, used as the ticket requester.")
    language: str = Field("en", description="Conversation language code (en, es, fr, de).")
    order_id: Optional[str] = Field(None, description="Order reference, when already known.")


class EscalationReplyRequest(BaseModel):
    body: str = Field(..., description="What the customer typed.")


@app.post("/api/escalation/start")
def api_escalation_start(req: EscalationStartRequest) -> Dict[str, Any]:
    """Open a live escalation ticket for a customer who asked for a person."""
    return esc.start_escalation(summary=req.summary, customer_name=req.customer_name,
                                customer_email=req.customer_email, language=req.language,
                                order_id=req.order_id)


@app.get("/api/escalation")
def api_escalation_list(include_ended: bool = False) -> Dict[str, Any]:
    """Escalations the relay is tracking (powers the specialist simulator console)."""
    items = esc.list_active_escalations(include_ended=include_ended)
    return {"status": "SUCCESS", "mode": esc.current_mode(), "escalations": items}


@app.get("/api/escalation/{escalation_id}/messages")
def api_escalation_messages(escalation_id: str, since: Optional[int] = None,
                            language: Optional[str] = None) -> Dict[str, Any]:
    """Specialist replies after ``since`` (the browser's authoritative cursor) + ticket status."""
    return esc.poll_messages(escalation_id, since=since, language=language)


@app.post("/api/escalation/{escalation_id}/reply")
def api_escalation_reply(escalation_id: str, req: EscalationReplyRequest) -> Dict[str, Any]:
    """Deliver the customer's message to the specialist handling this escalation."""
    return esc.post_customer_reply(escalation_id, req.body)


# ---------------------------------------------------------------------------
# Simulator (sandbox / demo)
# ---------------------------------------------------------------------------

class SpecialistReplyRequest(BaseModel):
    body: str
    author_name: str = "Sarah (Zendesk Specialist)"


class TicketStatusRequest(BaseModel):
    status: Literal["new", "open", "pending", "hold", "solved", "closed"] = "solved"


@app.post("/api/simulator/escalation/{escalation_id}/specialist-reply")
def api_simulator_reply(escalation_id: str, req: SpecialistReplyRequest) -> Dict[str, Any]:
    return esc.simulate_specialist_reply(escalation_id, req.body, author_name=req.author_name)


@app.post("/api/simulator/escalation/{escalation_id}/status")
def api_simulator_status(escalation_id: str, req: TicketStatusRequest) -> Dict[str, Any]:
    return esc.simulate_ticket_status(escalation_id, req.status)


@app.post("/api/mcp-preview/{tool_name}")
async def api_mcp_preview(tool_name: str, args: Dict[str, Any] = Body(default_factory=dict)) -> Dict[str, Any]:
    """Invoke one of the server-side MCP tools over plain REST (demo UI / curl)."""
    tool = TOOLS.get(tool_name)
    if tool is None:
        raise HTTPException(status_code=404, detail=f"Unknown tool {tool_name!r}. Known: {sorted(TOOLS)}")
    try:
        return await tool(**args)
    except TypeError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid arguments for {tool_name}: {exc}") from exc


@app.get("/api/health")
def api_health() -> Dict[str, Any]:
    settings = esc.zendesk_settings()
    mode = esc.current_mode()
    config_error = esc._config_error()
    return {
        "status": "ok" if not config_error else "misconfigured",
        "mode": mode,
        "brand": esc.APP_BRAND,
        "zendesk_subdomain": (settings["subdomain"] or None) if mode == "zendesk_live" else None,
        "configured": config_error is None,
        "config_error": config_error["message"] if config_error else None,
        "active_escalations": len(esc.list_active_escalations()),
        "mcp_endpoint": "/mcp",
        "mcp_tools": sorted(TOOLS),
        "version": __version__,
    }


# Static frontend last, so every API route above wins.
if FRONTEND_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
