# `chat-messenger` SDK Internals & Decoded Architecture

This document provides deep technical details decoded directly from the shipping Google Cloud `<chat-messenger>` widget bundle (`https://www.gstatic.com/chat-messenger/sdk/prod/v1.16/chat-messenger.js`) and verified through live browser instrumentation.

None of these mechanics are documented in official CX Agent Studio guide pages: `registerClientSideFunction` appears zero times, and `registerClientSideFunctionForAllTools` is absent from all public documentation.

Always **pin your SDK version** in production HTML.

---

## 1. Client Function Tool Registration

Client function tool execution handlers MUST be registered on **`messenger.presenter`**, NOT on the DOM element.

The live presenter internally maintains two distinct dispatch maps:
```
presenter.ka.i   (Exact Path Map)   Keyed by: `${toolResourcePath}.${lastPathSegment}`
presenter.ka.j   (Name-Only Map)     Keyed by bare tool name: `${toolName}`
```

### Resolution Logic on Tool Execution
When the server emits a tool execution event for a client function, the SDK resolves the handler as:
```javascript
const handler = presenter.ka.i.get(`${resourcePath}.${lastSegment}`) || presenter.ka.j.get(toolName);
```

### Methods Comparison

| Registration Method | Target Map | Recommended? | Key Hazard Avoided |
|---|---|---|---|
| `messenger.registerClientSideFunction(path, lastSegment, fn)` | `presenter.ka.i` | ❌ No | Fragile: fails if project number vs ID differ, or if tool ID is a UUID. |
| `messenger.presenter.registerClientSideFunctionForAllTools(name, fn)` | `presenter.ka.j` | ✅ **Yes** | **Robust**: matches bare function name regardless of project number/ID formats. |

### Correct Implementation Pattern

```javascript
// Ensure registration occurs on presenter
const messenger = document.querySelector('chat-messenger');
if (messenger && messenger.presenter) {
  messenger.presenter.registerClientSideFunctionForAllTools('escalate_to_human', async (args) => {
    console.log('[EscalationBridge] Tool invoked by agent:', args);
    return {
      status: 'SUCCESS',
      ticket_id: '12345'
    };
  });
}
```

---

## 2. Registration Timing & Module Lifecycle

A critical pitfall occurs when waiting for `chat-messenger-loaded`:
- The `<script>` tag loading `chat-messenger.js` is typically placed in `<head>` with `defer`.
- Application logic is typically loaded as an ES module at the end of `<body>`.
- By the time `app.js` executes its `window.addEventListener('chat-messenger-loaded', ...)`, the event has **already fired**.
- The listener never executes, and the tool handler is never registered.

### The Solution: Bounded, Idempotent Polling
Attempt immediate registration on script evaluation. If `messenger.presenter` is not yet available, poll with a fast interval (50ms) up to a bounded timeout (5 seconds), while retaining the event listener as a secondary safeguard:

```javascript
function registerWhenReady(toolName, handler, maxAttempts = 100) {
  let attempts = 0;
  const timer = setInterval(() => {
    attempts++;
    const messenger = document.querySelector('chat-messenger');
    if (messenger && messenger.presenter && typeof messenger.presenter.registerClientSideFunctionForAllTools === 'function') {
      messenger.presenter.registerClientSideFunctionForAllTools(toolName, handler);
      clearInterval(timer);
      console.log(`[Bridge] Successfully registered ${toolName} on attempt ${attempts}`);
    } else if (attempts >= maxAttempts) {
      clearInterval(timer);
      console.error(`[Bridge] Failed to find messenger.presenter within timeout`);
    }
  }, 50);
}
```

---

## 3. Public DOM Injection APIs

The `<chat-messenger>` custom element exposes several public methods that can inject content directly into the conversation transcript without creating new turns on the AI agent:

| Method | Behavior & Use Case |
|---|---|
| `renderCustomText(text, isAgent = true)` | Injects a message bubble into the transcript. If `isAgent === true`, renders on the left with agent styling. If `isAgent === false`, renders on the right as the customer. |
| `renderCustomCard(cards)` | Injects rich custom UI cards. |
| `renderImageMessage(url, type)` | Injects an image thumbnail into the stream. |
| `addUtterance(turn)` / `updateUtterance(id, turn)` | Low-level transcript modification primitives. |

### Specialist Message Rendering
When a new public comment is received from the Zendesk specialist via polling, `cxas-zendesk-bridge.js` injects it using:
```javascript
messenger.renderCustomText(`**${specialistName}:** ${cleanCommentBody}`);
// And for customer replies submitted via the overlay composer:
messenger.renderCustomText(customerReplyText, false);
```

---

## 4. Shadow DOM Isolation & The 106px Input Overlay

### The Closed Shadow Root Problem
Message content inside `<chat-messenger>` is rendered in **closed shadow roots**.
- `document.querySelector('chat-messenger').shadowRoot` exposes only the top-level container (`DIV.wrapper`).
- Inspecting `innerText` on the element returns empty whitespace.
- Playwright locators cannot inspect message contents directly on `<chat-messenger>` (though `<cxas-preview-messenger>` exposes `.getTranscript()` for local/CI tests).

### The Composer Band (106px)
The native composer input cannot be disabled, removed, or styled via CSS variables because it is encapsulated in the shadow DOM.
- If you leave it visible while an escalation is active, the customer can submit messages to the AI assistant while simultaneously chatting with a human specialist.
- To prevent this, `cxas-zendesk-bridge.js` mounts an overlay container (`div.cxas-zd-composer`) positioned precisely over the bottom `106px` input band and attaches a `focusin` trap to redirect keyboard focus to `.cxas-zd-input`.

Measured panel geometry (400×600px panel, anchored `bottom: 24px`):
- Text field bounds: ~92px above panel bottom
- Send button bounds: ~25px to ~60px above panel bottom
- Panel bottom edge: 0px

**Requirement:** The overlay composer (`.cxas-zd-composer`) is **106px high** (`composerHeight: 106`) to completely conceal both the input box and the send button.

---

## 5. Zero-Leak Verification

The widget only renders a turn if `messages.length > 0` in the response payload.
Because `clientFunction` tool executions return structured tool responses rather than assistant text messages, the tool invocation itself produces **zero visible DOM output** until your JavaScript handler or the agent explicitly renders text. This provides structural immunity against accidental JSON or prompt leakage in the UI.
