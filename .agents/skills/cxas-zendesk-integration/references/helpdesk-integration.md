# Zendesk Helpdesk API Integration & Polling Architecture

This document specifies the server-side integration between the FastAPI relay connector and the Zendesk Support REST API v2.

---

## 1. Ticket Creation Payload & Identity Schema

When an escalation begins (`POST /api/escalation/start`), the relay issues a `POST` request to `/api/v2/tickets.json`.

```json
{
  "ticket": {
    "subject": "Chat escalation — Elena Gómez — ORD-8472",
    "requester": {
      "name": "Elena Gómez",
      "email": "elena.gomez@example.com"
    },
    "comment": {
      "body": "Customer requesting human assistance regarding return status.\n\nOrder: ORD-8472\nConversation language: en\nEscalated from the Acme Support AI assistant (CX Agent Studio).\n\nThe customer asked to speak to a person. They are waiting in the live chat —\na public reply on this ticket reaches them in the chat window within seconds.",
      "public": true
    },
    "tags": [
      "live-escalation",
      "cxas-chat"
    ],
    "priority": "urgent"
  }
}
```

### The Critical Requester Invariant
- **Why `requester` is mandatory:** If a ticket is created without an explicit `requester` object, Zendesk assigns the API administrator account as the requester, submitter, and comment author.
- **The Consequence of Omitting:** When the admin account is the requester, bot writes and human specialist writes share the exact same user ID, making it impossible to separate them programmatically.
- **Data Source:** Take customer name and email from the frontend authenticated session state (`getCustomer()`), NOT from LLM parameters. This prevents the LLM from hallucinating email addresses or customer names onto tickets.

---

## 2. Comment Discriminator: Filtering Human Replies

During active polling (`GET /api/escalation/{escalation_id}/messages?since={cursor}`), the relay fetches ticket comments via `GET /api/v2/tickets/{id}/comments.json?include=users`.

A comment is delivered to the customer if and only if **all three** conditions are met:

| Condition | Rationale |
|---|---|
| `comment.public === true` | Internal notes (`public === false`) are private specialist communications; never expose them to customers. |
| `comment.author_id !== ticket.requester_id` | Prevents echoing the customer's own initial issue description or subsequent customer messages back to them. |
| `comment.id not in our_comment_ids` | When the customer submits a reply from the web chat, the relay submits that reply via the Zendesk API using the API credentials. Because that reply shares the API account's author ID, checking author ID alone is insufficient. The relay maintains an in-memory set of comment IDs that it submitted on behalf of the customer, and excludes them. |

> ⚠️ **Warning:** Never use `requester_id !== submitter_id` as a check. When you pass `requester: {name, email}`, Zendesk sets BOTH `requester_id` and `submitter_id` to the newly created/matched end-user ID.

---

## 3. Author Name Side-Loading

When querying `/api/v2/tickets/{id}/comments.json`, append `?include=users`.

Zendesk returns an array of user objects:
```json
{
  "comments": [...],
  "users": [
    { "id": 3537384933, "name": "Ana Martínez", "role": "agent" }
  ]
}
```

The relay maps `comment.author_id` to `user.name`. This enables the chat widget to display a human persona (e.g. `👤 Ana Martínez has joined the conversation` and `**Ana Martínez:** Hello! How can I help with your order today?`) without hardcoding staff names in code.

---

## 4. Message Body Normalization

Zendesk's Agent Workspace WYSIWYG editor pads `plain_body` with non-breaking spaces and blank lines.
- Example raw output: `"Hello there\n&nbsp;\n&nbsp;"`
- If rendered directly, this causes unsightly blank white boxes under message bubbles.

**Normalization Pipeline (`_clean_comment_body`):**
1. Prefer `comment.body` (or fallback to `comment.plain_body`).
2. Replace all occurrences of `&nbsp;` and `\u00a0` with spaces.
3. Collapse consecutive blank lines (`\n\n+`) into a single blank line.
4. Strip trailing whitespace per line and trim leading/trailing blank lines.

---

## 5. Forwarding Customer Replies

When the customer enters a reply into the overlay composer (`POST /api/escalation/{escalation_id}/reply`), the relay executes:
`PUT /api/v2/tickets/{id}.json`
```json
{
  "ticket": {
    "comment": {
      "body": "Customer's message text",
      "public": true
    }
  }
}
```

The response includes an audit event containing the newly created comment ID:
```json
{
  "audit": {
    "events": [
      {
        "id": 91823741823,
        "type": "Comment",
        "body": "Customer's message text"
      }
    ]
  }
}
```
The relay immediately adds the `Comment` event's `id` to its internal `our_comment_ids` tracking set and returns `{"status": "SUCCESS", "comment_id": 91823741823}`.

---

## 6. Client-Side Cursor Ack Architecture

The server must **never** advance the authoritative polling cursor on its own when `since` is supplied.
- If the server advances the cursor when sending an HTTP response, but that response is dropped by a mobile network blip before reaching the browser, that specialist message is permanently lost to the customer.
- **Rule:** The browser owns the cursor. The browser requests `GET /api/escalation/{id}/messages?since=104928`, and only updates its local cursor to `104929` AFTER the message has been successfully painted into the DOM via `renderCustomText`.

---

## 7. Rate Budgets & Production Limits

| Quota / Constraint | Threshold | Design Mitigation |
|---|---|---|
| **Zendesk API Rate Limit** | 200 req/min (Entry) / 700+ req/min (Enterprise) | A 2-second poll consumes 60 req/min per active conversation (comments + ticket status). |
| **Ticket Updates per User** | 30 updates / 10 min / user / ticket | Sufficient for customer chat sessions; if exceeded, Zendesk returns HTTP 429 (`ZENDESK_RATE_LIMITED`). |
| **Relay Memory Persistence** | In-memory session dictionary | Deploy Cloud Run with `--max-instances=1` or configure Redis / Spanner backend for multi-instance deployments. |

---

## 8. Mock Specialist Simulator Mode (`MockZendesk`)

For automated CI/CD testing, local evaluation, or developer onboarding without an active Zendesk account, set `ZENDESK_MOCK_MODE=true` (or leave `ZENDESK_API_TOKEN` unset).

Because the sandbox (`MockZendesk` in `backend/escalation.py`) is mounted as an `httpx.MockTransport` rather than a separate code path, both live mode and mock mode execute the exact same requester identity, comment discriminator, and status handback logic:
- `POST /api/escalation/start` opens a ticket in `MockZendesk` (numeric IDs starting at `"10001"`).
- `GET /api/escalation` lists active escalations for the Sandbox Workbench console.
- `POST /api/simulator/escalation/{escalation_id}/specialist-reply` (`{"body": "...", "author_name": "Sarah (Zendesk Specialist)"}`) posts a public specialist comment on the ticket.
- `POST /api/simulator/escalation/{escalation_id}/status` (`{"status": "solved"}`) updates the ticket status, triggering automatic handback to the AI assistant on the next poll.
