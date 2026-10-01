---
name: cxas-zendesk-integration
description: Complete enterprise integration between CX Agent Studio (CXAS / GECX) and Zendesk. Covers both (A) Server-Side Zendesk MCP tools (ticket search, status, offline creation, comments) and (B) Live Two-Way Human Escalation from the <chat-messenger> widget to Zendesk with ~2s latency and automatic AI resumption on ticket resolution.
---

# CX Agent Studio & Zendesk Enterprise Integration

Architect and operate a complete hybrid integration between Google Cloud CX Agent Studio (powered by **Gemini 3.0 Flash** / **Gemini 3.1 Pro**) and Zendesk.

This skill provides the architectural foundation, SDK internals, and operational playbooks for two core patterns:
1. **Pillar A: Server-Side Zendesk MCP Connector** — Real-time ticket search, status queries, offline ticket creation, and comment updates via Streamable HTTP MCP (`/mcp`).
2. **Pillar B: Live Two-Way Human Chat Escalation** — Direct bot-to-human handoff from the `<chat-messenger>` widget to a human specialist in Zendesk, delivering specialist replies directly into the customer's chat bubbles in ~2s and resuming the conversational agent when the ticket is marked Solved.

---

## Read This First: Critical Hard-Won Insights

Every critical failure mode in this hybrid architecture is **silent**. Nothing throws an unhandled exception to the console. The symptom is always "nothing happens," which is indistinguishable from the model refusing to invoke a tool.

1. **`registerClientSideFunctionForAllTools` is on `messenger.presenter`, NOT on the DOM element.**
   On the `<chat-messenger>` element it is `undefined`, and optional chaining (`?.`) silently swallows the call.
2. **`chat-messenger-loaded` fires before your application module attaches its listener.**
   Because the widget script is `defer`-loaded in `<head>` and your app script is typically an ES module at the end of `<body>`, registering inside `window.addEventListener('chat-messenger-loaded', ...)` often executes after the event has already fired. Use bounded, idempotent polling at module startup.
3. **The rendered transcript lives in closed shadow roots.**
   Message bubbles are rendered inside closed shadow DOM trees. `innerText`, standard DOM locators, and recursive `shadowRoot` walks return only empty whitespace. Automated tests must spy on `renderCustomText` or assert on `runSession` HTTP response bodies.
4. **The widget composer must be covered, not hidden.**
   The input composer cannot be hidden or disabled via standard CSS due to shadow-DOM boundaries. An overlay composer (`106px` height) must cover the entire input band to prevent simultaneous customer inputs to both the agent and human.
5. **Model Governance Invariant:**
   CX Agent Studio agents must run on **Gemini 3.0 Flash** (`gemini-3-flash-preview`) or **Gemini 3.1 Pro** (`gemini-3.1-pro-preview`). Older generation models are deprecated, strictly forbidden, and will return HTTP 501 errors.

---

## Architecture Overview

```
                          CUSTOMER BROWSER
    ┌─────────────────────────────────────────────────────────┐
    │                                                         │
    │  [ <chat-messenger> ]                                    │
    │         │                                               │
    │         │ (Turn 1..N: AI Conversation)                  │
    │         ▼                                               │
    │     CXAS Agent (Gemini 3.0 Flash)                       │
    │         │                                               │
    │         ├──────────► [ Server-Side MCP Toolset ]        │
    │         │                  │ (search, status, offline)  │
    │         │                  ▼                            │
    │         │            FastAPI Server (/mcp)              │
    │         │                  │                            │
    │         │                  ▼                            │
    │         │            Zendesk API v2                     │
    │         │                                               │
    │         │ (Customer: "I want to talk to a person")      │
    │         ▼                                               │
    │   calls clientFunction: escalate_to_human(summary)      │
    │         │                                               │
    │         ▼                                               │
    │   CxasZendeskBridge (presenter handler in browser)      │
    │         │                                               │
    │         ├─► POST /api/escalation/start                  │
    │         ├─► Swaps composer to 106px overlay             │
    │         └─► Starts 2s poll loop                         │
    │                                                         │
    └─────────────────────┬───────────────────────────────────┘
                          │
                          ▼
            FASTAPI RELAY & MCP SERVICE (Cloud Run)
    ┌─────────────────────────────────────────────────────────┐
    │                                                         │
    │  POST /api/escalation/start                             │
    │    └──► POST /api/v2/tickets.json (tags: live-escalation)│
    │                                                         │
    │  GET /api/escalation/{id}/messages?since={cursor}       │
    │    └──► GET /api/v2/tickets/{id}/comments.json          │
    │         Filters: public==true, author!=requester,       │
    │                  id not in our_comment_ids              │
    │                                                         │
    │  POST /api/escalation/{id}/reply                        │
    │    └──► PUT /api/v2/tickets/{id}.json                   │
    │                                                         │
    └─────────────────────┬───────────────────────────────────┘
                          │
                          ▼
                  ZENDESK INSTANCE
    ┌─────────────────────────────────────────────────────────┐
    │  Ticket #N (Priority: Urgent, Requester: Customer)      │
    │  Specialist replies in Zendesk Agent Workspace          │
    │  Solving ticket signals bot handback                    │
    └─────────────────────────────────────────────────────────┘
```

---

## Build & Integration Workflow

### 1. Backend Service & MCP Server
Deploy the FastAPI backend container (`backend.server:app`) to Cloud Run:
- Exposes Streamable HTTP MCP at `/mcp`.
- Exposes Escalation Relay API at `POST /api/escalation/start`, `GET /api/escalation/{id}/messages`, `POST /api/escalation/{id}/reply`, and `GET /api/escalation`.
- Exposes Specialist Simulator & MCP REST Preview at `POST /api/simulator/escalation/{id}/specialist-reply`, `POST /api/simulator/escalation/{id}/status`, `POST /api/mcp-preview/{tool_name}`, and `GET /api/health`.
- Injects Zendesk API Token from Secret Manager (`zendesk-api-token`).
- Supports zero-credential local testing via `ZENDESK_MOCK_MODE=true`.

### 2. CX Agent Studio Configuration
1. Register `zendesk_mcp_connector` pointing to `${BACKEND_URL}/mcp` (via `scripts/setup_cxas.py`).
2. Register `escalate_to_human` as a `clientFunction` tool (`SYNCHRONOUS`, uppercase schema dialect).
3. Attach both to your support spoke agent.
4. Add the XML `<taskflow>` subtask (`cxas/instructions/support_spoke_prompt.md`) to the spoke instruction prompt.

### 3. Frontend Embedding
1. Embed `<chat-messenger>` and include `cxas-zendesk-bridge.css` and `cxas-zendesk-bridge.js`.
2. Initialize `window.CxasZendeskBridge.init({ apiBase, language, pollIntervalMs, getCustomer })` with authenticated customer profile data.

---

## The Key Gotchas & Anti-Patterns

| Anti-Pattern | Immediate Symptom | Root Cause & Fix |
|---|---|---|
| **Model Narrates Handover** | Bot says "I will connect you" but never invokes tool | Enforce constraint: `escalate_to_human` must be the *first and only* action of the turn. |
| **Collapsed Ticket Identity** | Poller cannot distinguish agent replies from bot | Ticket created without `requester: {name, email}`. When requester is omitted, Zendesk sets author=admin, breaking `author_id != requester_id`. |
| **Echoing Customer Replies** | Customer sees their own reply appear as the specialist | Customer replies forwarded via API share the API credentials. Record `our_comment_ids` and filter them out. |
| **Lost Human Messages** | Messages missed during temporary network blips | Relay advancing its own cursor. The browser MUST own the cursor via `?since={comment_id}`. |
| **Double Input Boxes** | Customer types to bot while specialist is talking | Shadow DOM composer cannot be hidden; cover the full `106px` composer band with an overlay. |

---

## Reference Guides

- [`references/widget-sdk-internals.md`](./references/widget-sdk-internals.md): Decoded SDK mechanics, presenter registry maps, closed shadow roots, and transcript injection APIs.
- [`references/helpdesk-integration.md`](./references/helpdesk-integration.md): Zendesk API details, ticket schema, comment discriminator rules, and rate limits.
- [`references/testing-recipe.md`](./references/testing-recipe.md): Three-layer testing strategy: Playwright E2E, FastAPI contract tests, and conversational evals.
