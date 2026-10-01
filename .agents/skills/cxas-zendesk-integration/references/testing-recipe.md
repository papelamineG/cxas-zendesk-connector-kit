# Testing Recipe & Quality Verification Protocol

This guide outlines the verification hierarchy for the CXAS Zendesk Connector Kit across three testing layers:
1. **Real-Browser End-to-End (Playwright)** — Verifies user gestures, closed shadow-DOM boundaries, and the live escalation loop.
2. **Relay Service Contract Tests (Pytest / FastAPI)** — Asserts ticket payload shapes, comment discrimination, and error resilience.
3. **Conversational Quality & Tool Trigger Evals** — Asserts that Gemini 3.0 Flash or Gemini 3.1 Pro consistently invokes `escalate_to_human` without leaking internal tokens or narrating tool actions.

---

## 1. Real-Browser End-to-End (Playwright)

Real browser tests verify that the `<chat-messenger>` widget, the JavaScript bridge, the floating overlay composer, and the backend relay function together harmoniously.

### Handling Closed Shadow Roots
Message bubbles live inside closed shadow DOM roots. Standard Playwright locators cannot inspect message contents. Use two robust inspection techniques:

```python
# A. Spy on renderCustomText to verify what messages are injected
page.evaluate("""() => {
    window.__renderedMessages = [];
    const messenger = document.querySelector('chat-messenger');
    if (messenger) {
        const originalRender = messenger.renderCustomText.bind(messenger);
        messenger.renderCustomText = (text, isAgent) => {
            window.__renderedMessages.push({ text, isAgent, timestamp: Date.now() });
            return originalRender(text, isAgent);
        };
    }
}""")

# B. Assert runSession response bodies to ensure zero raw tool leaks
run_session_bodies = []
page.on("response", lambda resp: run_session_bodies.append(resp.text()) if "runSession" in resp.url else None)
```

### Asserting Single Input Focus (106px Overlay)
Verify that the overlay composer (`.cxas-zd-composer`) is strictly the topmost element over the widget's input band, preventing interaction with the native widget input during escalation:

```python
# Check that the element at the center of the composer band belongs to .cxas-zd-composer
topmost_in_overlay = page.evaluate("""() => {
    const messenger = document.querySelector('chat-messenger, cxas-preview-messenger');
    const rect = messenger.getBoundingClientRect();
    const el = document.elementFromPoint(rect.left + rect.width / 2, rect.bottom - 30);
    return Boolean(el && el.closest('.cxas-zd-composer'));
}""")
assert topmost_in_overlay is True, "Expected .cxas-zd-composer overlay on top of the widget input band"
```

### Complete E2E Lifecycle Checklist
- [ ] User sends trigger: *"I want to talk to a human, please"*
- [ ] Model calls `escalate_to_human` without narrating the action
- [ ] Bot responds with a single reassuring sentence
- [ ] Waiting indicator pill (`Connecting you with a specialist…`) pulses in the `.cxas-zd-composer` overlay
- [ ] Overlay composer activates (`106px`) and covers the native input
- [ ] Specialist joins: `👤 {who} has joined the conversation` is rendered
- [ ] Specialist sends reply: `**{who}:** {body}` appears in customer chat bubbles within ~2s
- [ ] Customer replies: message is forwarded to Zendesk and rendered as the user's turn without being echoed back as a specialist message
- [ ] Specialist marks ticket Solved: bot hands back (`{who} has resolved your case. Can I help you with anything else?`), overlay unmounts, and bot resumes

---

## 2. Relay Contract Tests (Pytest)

Test the FastAPI relay routes using `fastapi.testclient.TestClient` against `backend.server:app` (both in `MockZendesk` mode and with an injected `httpx.MockTransport` for live-mode contract verification):

```python
from fastapi.testclient import TestClient
from backend.server import app

client = TestClient(app)

def test_escalation_lifecycle():
    # 1. Start escalation
    start_resp = client.post("/api/escalation/start", json={
        "summary": "Customer needs return help",
        "language": "en",
        "order_id": "ORD-1234",
        "customer_name": "Elena Gómez",
        "customer_email": "elena.gomez@example.com",
    })
    assert start_resp.status_code == 200
    body = start_resp.json()
    assert body["status"] == "SUCCESS"
    ticket_id = body["ticket_id"]

    # 2. Poll for messages (initially empty)
    poll_resp = client.get(f"/api/escalation/{ticket_id}/messages?since=0")
    assert poll_resp.status_code == 200
    assert poll_resp.json()["status"] == "SUCCESS"
    assert poll_resp.json()["messages"] == []

    # 3. Post customer reply
    reply_resp = client.post(f"/api/escalation/{ticket_id}/reply", json={
        "body": "Thank you for joining."
    })
    assert reply_resp.status_code == 200
    assert reply_resp.json()["status"] == "SUCCESS"
    assert reply_resp.json()["comment_id"] is not None
```

### Required Contract Assertions
- **Requester Validation:** Ensure `ticket.requester` contains both `name` and `email`.
- **Comment Exclusion:** Ensure `our_comment_ids` are never returned in `/messages`.
- **Private Note Exclusion:** Ensure `public: false` comments are excluded.
- **Normalization:** Ensure `&nbsp;` and trailing newlines are stripped from message bodies.

---

## 3. Conversational Quality & Tool Trigger Evals

Run automated evaluation turns against CX Agent Studio using **Gemini 3.0 Flash** or **Gemini 3.1 Pro**.

### Positive Trigger Tests (Must Escalate)
- *"Quiero hablar con una persona por favor"*
- *"Can I speak to a real human agent?"*
- *"I am done chatting with a bot, get me a supervisor."*

**Pass Criteria:**
1. `escalate_to_human` is called on the FIRST turn.
2. The summary parameter accurately reflects the customer query.
3. No tool arguments or internal ticket IDs are spoken in the response.

### Negative Trigger Tests (Must NOT Escalate)
- *"What is your return policy?"*
- *"Can you check if ticket #1042 has shipped?"* (Should call `get_ticket_status`, NOT `escalate_to_human`)
- *"I'm slightly confused about the assembly instructions."* (Should answer with product care guidance)

**Pass Criteria:**
1. `escalate_to_human` is NEVER called.
2. Assistant resolves or clarifies inquiry automatically.

---

## 4. Test Execution Commands

```bash
# Run all tests (unit + MCP + Playwright E2E) via Makefile
make test

# Run backend relay & MCP tool contract tests only (or `make test-unit`)
python3 -m pytest tests/test_escalation_relay.py tests/test_zendesk_mcp_tools.py -q

# Run Playwright Chromium E2E browser tests (or `make test-e2e`)
python3 -m pytest tests/test_e2e_browser.py -q

# Dry-run CXAS tool and MCP toolset provisioning (or `make setup-cxas-dry-run`)
python3 scripts/setup_cxas.py --project-id my-project --location us --app-id my-app --backend-url https://example.run.app --dry-run
```
