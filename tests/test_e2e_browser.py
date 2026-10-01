"""
End-to-end browser test: the full human hand-off in a real Chromium.

Starts the FastAPI app (sandbox mode) on an ephemeral port, opens the
workbench, and drives everything on the customer's side with real gestures:

  assistant -> "talk to a human" -> escalate_to_human client function
  -> overlay composer masks the widget input -> customer replies
  -> specialist replies from the simulator console -> reply renders in chat
  -> ticket marked solved -> overlay removed -> the AI assistant answers again.

Only the human on the far side of Zendesk is played by the test (through the
simulator UI / API), exactly as a demo presenter would.

Run:  python -m pytest tests/test_e2e_browser.py -q
"""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

playwright_sync = pytest.importorskip("playwright.sync_api")
sync_playwright = playwright_sync.sync_playwright
PlaywrightTimeoutError = playwright_sync.TimeoutError

ROOT = Path(__file__).resolve().parents[1]
CUSTOMER_WORDS = "Thanks Sarah — when will the replacement table arrive?"
SPECIALIST_WORDS = "Hi Jordan, Sarah here. I've reviewed your case and a replacement ships today at no cost."
SPECIALIST_NAME = "Sarah — Zendesk Agent"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def server():
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZENDESK_")}
    env.update({"ZENDESK_MOCK_MODE": "true", "APP_BRAND": "Acme Support", "PYTHONUNBUFFERED": "1"})
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.server:app", "--host", "127.0.0.1", "--port", str(port),
         "--log-level", "warning"],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                raise RuntimeError("uvicorn exited early:\n" + proc.stdout.read().decode(errors="replace"))
            try:
                if httpx.get(f"{base}/api/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.25)
        else:
            raise RuntimeError("uvicorn did not become healthy within 30s")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


@pytest.fixture
def page(server):
    with sync_playwright() as p:
        browser = p.chromium.launch()
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        pg = context.new_page()
        pg.errors = []
        pg.console_lines = []
        pg.on("pageerror", lambda exc: pg.errors.append(str(exc)))
        pg.on("console", lambda msg: pg.console_lines.append(msg.text))
        pg.goto(f"{server}/", wait_until="domcontentloaded")
        pg.wait_for_selector("#mode-badge[data-mode='mock_simulator']", timeout=10000)
        pg.wait_for_selector("cxas-preview-messenger .cxas-preview-input", timeout=10000)
        yield pg
        context.close()
        browser.close()


def _transcript(page):
    return page.evaluate("() => document.querySelector('cxas-preview-messenger').getTranscript()")


def _wait_for(page, predicate, timeout_s=15.0, interval_ms=200):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        transcript = _transcript(page)
        if predicate(transcript):
            return transcript
        page.wait_for_timeout(interval_ms)
    raise AssertionError(f"Condition not met within {timeout_s}s. Transcript: {_transcript(page)}")


def test_full_escalation_lifecycle_assistant_to_specialist_and_back(page, server):
    preview = page.locator("cxas-preview-messenger")
    assert "Mock Simulator" in page.locator("#mode-badge").inner_text()

    # 1. The customer asks the assistant for a person (real typing + click).
    preview.locator(".cxas-preview-input").fill("I want to talk to a human, please")
    preview.locator(".cxas-preview-send").click()

    # 2. The client function opened a ticket and the overlay composer mounted.
    composer = page.locator(".cxas-zd-composer")
    composer.wait_for(state="visible", timeout=10000)
    state = page.evaluate("() => CxasZendeskBridge.getState()")
    ticket_id = state["escalationId"]
    assert state["active"] is True and ticket_id and ticket_id.isdigit(), state
    assert any(f"escalation ticket opened: {ticket_id}" in line for line in page.console_lines)
    assert "Connecting you with a specialist" in composer.locator(".cxas-zd-pill").inner_text()

    # The overlay is the 106px band at the bottom of the widget...
    widget_box = preview.bounding_box()
    composer_box = composer.bounding_box()
    assert abs(composer_box["height"] - 106) <= 1
    assert abs((composer_box["y"] + composer_box["height"]) - (widget_box["y"] + widget_box["height"])) <= 1
    # ...and it really covers the widget's own input: exactly ONE usable input.
    input_box = preview.locator(".cxas-preview-input").bounding_box()
    covered = page.evaluate(
        "([x, y]) => !!(document.elementFromPoint(x, y) || document.body).closest('.cxas-zd-composer')",
        [input_box["x"] + input_box["width"] / 2, input_box["y"] + input_box["height"] / 2])
    assert covered, "The widget's own input is exposed during the escalation (two input boxes)."
    with pytest.raises(PlaywrightTimeoutError):
        preview.locator(".cxas-preview-send").click(timeout=1500)

    # 3. The customer replies through the overlay; it lands on the ticket.
    composer.locator("input").fill(CUSTOMER_WORDS)
    composer.locator("button").click()
    _wait_for(page, lambda t: any(m["role"] == "user" and m["text"] == CUSTOMER_WORDS for m in t))
    status = httpx.post(f"{server}/api/mcp-preview/get_ticket_status", json={"ticket_id": int(ticket_id)}).json()
    assert status["latest_public_comment"]["body"] == CUSTOMER_WORDS

    # 4. The specialist answers from the simulator console.
    page.wait_for_selector(f"#sim-escalations button[data-escalation-id='{ticket_id}']", timeout=10000)
    page.click(f"#sim-escalations button[data-escalation-id='{ticket_id}']")
    page.fill("#sim-author", SPECIALIST_NAME)
    page.fill("#sim-reply", SPECIALIST_WORDS)
    posted_at = time.time()
    page.click("#sim-send")
    transcript = _wait_for(page, lambda t: any(m["role"] == "specialist" and SPECIALIST_WORDS in m["text"] for m in t))
    delivery_s = time.time() - posted_at
    assert delivery_s < 8.0, f"Specialist reply took {delivery_s:.1f}s — no longer feels live."
    specialist = [m for m in transcript if m["role"] == "specialist"]
    assert SPECIALIST_NAME in specialist[0]["text"], "Reply must be attributed to the specialist by name."
    assert any(m["role"] == "system" and f"{SPECIALIST_NAME} has joined" in m["text"] for m in transcript)
    assert not any(CUSTOMER_WORDS in m["text"] for m in specialist), \
        "The customer's own words came back attributed to the specialist."
    assert f"{SPECIALIST_NAME} · online" in composer.locator(".cxas-zd-pill").inner_text()

    # 5. The specialist solves the ticket; the assistant takes the conversation back.
    page.click("#sim-solve")
    transcript = _wait_for(page, lambda t: any(m["role"] == "assistant" and
                                               f"{SPECIALIST_NAME} has resolved your case" in m["text"] for m in t))
    page.wait_for_selector(".cxas-zd-composer", state="detached", timeout=5000)
    assert page.evaluate("() => CxasZendeskBridge.getState()")["active"] is False

    # The widget's own input works again and the AI assistant answers.
    preview.locator(".cxas-preview-input").fill("One more question about delivery times")
    preview.locator(".cxas-preview-send").click(timeout=3000)
    transcript = _wait_for(page, lambda t: any(m["role"] == "assistant" and "(Preview assistant)" in m["text"] for m in t))

    joined = "\n".join(m["text"] for m in transcript)
    for leak in ("escalate_to_human", "ticket_id", "author_id", "clientFunction"):
        assert leak not in joined, f"Internal detail {leak!r} leaked into the chat."
    assert page.errors == [], f"Uncaught page errors: {page.errors}"


def test_demo_button_escalation_in_spanish_with_api_driven_specialist(page, server):
    page.click("#config summary")
    page.select_option("#cfg-lang", "es")
    page.click("#sim-trigger")

    composer = page.locator(".cxas-zd-composer")
    composer.wait_for(state="visible", timeout=10000)
    assert "Conectando con un especialista" in composer.locator(".cxas-zd-pill").inner_text()
    ticket_id = page.evaluate("() => CxasZendeskBridge.getState()")["escalationId"]

    reply = httpx.post(f"{server}/api/simulator/escalation/{ticket_id}/specialist-reply",
                       json={"body": "Hola Jordan, soy Lucía.", "author_name": "Lucía"}).json()
    assert reply["status"] == "SUCCESS"
    _wait_for(page, lambda t: any(m["role"] == "system" and "Lucía se ha unido" in m["text"] for m in t))

    solved = httpx.post(f"{server}/api/simulator/escalation/{ticket_id}/status", json={"status": "solved"}).json()
    assert solved["ticket_status"] == "solved"
    _wait_for(page, lambda t: any("Lucía ha resuelto tu incidencia" in m["text"] for m in t))
    page.wait_for_selector(".cxas-zd-composer", state="detached", timeout=5000)
    assert page.errors == [], f"Uncaught page errors: {page.errors}"
