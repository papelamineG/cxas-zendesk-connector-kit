#!/usr/bin/env python3
"""
Provision the CX Agent Studio side of the Zendesk connector, idempotently.

Creates or updates, in an existing CX Agent Studio (CES) app:

  1. ``escalate_to_human``      -- a ``clientFunction`` tool. The browser bridge
                                   (cxas-zendesk-bridge.js) registers the handler by
                                   name via presenter.registerClientSideFunctionForAllTools.
  2. ``zendesk_mcp_connector``  -- an ``mcpToolset`` pointing at ``{backend_url}/mcp``
                                   (Streamable HTTP; CXAS does not support SSE servers).

Uses ``gcloud auth print-access-token`` and the CES v1beta REST API:
  https://ces.googleapis.com/v1beta/projects/{project}/locations/{location}/apps/{app}/tools
  https://ces.googleapis.com/v1beta/projects/{project}/locations/{location}/apps/{app}/toolsets

Idempotent: GET by id -> PATCH (with updateMask) when it exists, POST ?toolId= /
?toolsetId= when it does not. ``--dry-run`` prints the requests without sending.

After running, attach both to your agent(s) in the CX Agent Studio console (or
via update_agent) and add an instruction such as:
  "If the customer explicitly asks to speak to a person, call {@TOOL: escalate_to_human}
   with a one-sentence summary of their issue in their language."

Example:
  python scripts/setup_cxas.py --project-id my-proj --location us \\
      --app-id 1234abcd-... --backend-url https://zendesk-connector-xyz.run.app
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

API_ROOT = "https://ces.googleapis.com/v1beta"
TOOL_ID = "escalate_to_human"
TOOLSET_ID = "zendesk_mcp_connector"


def escalate_tool_body() -> Dict[str, Any]:
    """The clientFunction tool. Identity is deliberately NOT a parameter: the
    browser reads it from the signed-in profile so the model cannot invent a
    requester on a real ticket."""
    return {
        "displayName": TOOL_ID,
        "clientFunction": {
            "name": TOOL_ID,
            "description": (
                "Hand the conversation to a live human specialist through Zendesk. Call this ONLY when "
                "the customer explicitly asks to speak to a person. Do not describe or announce the tool "
                "call; say only a brief reassuring line. Provide a one-sentence summary of the customer's "
                "issue in their own language. If it returns status ERROR, follow agent_action."
            ),
            "parameters": {
                "type": "OBJECT",
                "required": ["summary", "language"],
                "properties": {
                    "summary": {"type": "STRING",
                                "description": "One sentence describing the customer's issue, in their language."},
                    "order_id": {"type": "STRING", "description": "Order reference if known, else omit."},
                    "language": {"type": "STRING", "description": "Conversation language: en, es, fr or de."},
                },
            },
            "response": {
                "type": "OBJECT",
                "properties": {
                    "status": {"type": "STRING"},
                    "ticket_id": {"type": "STRING"},
                    "error_code": {"type": "STRING"},
                    "agent_action": {"type": "STRING"},
                },
            },
        },
    }


def mcp_toolset_body(backend_url: str, auth: str) -> Dict[str, Any]:
    mcp_toolset: Dict[str, Any] = {"serverAddress": backend_url.rstrip("/") + "/mcp"}
    if auth == "service-agent-id-token":
        # CXAS sends a Google-signed ID token from its service agent; pair with a
        # Cloud Run service that requires authentication (roles/run.invoker).
        mcp_toolset["apiAuthentication"] = {"serviceAgentIdTokenAuthConfig": {}}
    return {
        "displayName": TOOLSET_ID,
        "description": "Zendesk ticket tools: search_tickets, get_ticket_status, "
                       "create_offline_ticket, add_ticket_comment.",
        "executionType": "SYNCHRONOUS",
        "mcpToolset": mcp_toolset,
    }


class CesClient:
    def __init__(self, project: str, location: str, app: str, token: Optional[str], dry_run: bool):
        self.parent = f"projects/{project}/locations/{location}/apps/{app}"
        self.project = project
        self.token = token
        self.dry_run = dry_run

    def request(self, method: str, path: str, body: Optional[dict] = None) -> Tuple[int, Dict[str, Any]]:
        url = f"{API_ROOT}/{path}"
        if self.dry_run:
            print(f"[dry-run] {method} {url}")
            if body is not None:
                print(json.dumps(body, indent=2))
            return 0, {}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method, headers={
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "x-goog-user-project": self.project,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read().decode("utf-8") or "{}"
                return resp.status, json.loads(raw)
        except urllib.error.HTTPError as err:
            raw = err.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(raw)
            except ValueError:
                payload = {"error": {"message": raw[:500]}}
            return err.code, payload
        except (urllib.error.URLError, TimeoutError) as err:
            # DNS / connection / socket-timeout failures carry no HTTP status.
            reason = getattr(err, "reason", None) or err
            raise SystemExit(f"ERROR: could not reach {API_ROOT} ({method} {path}): {reason}\n"
                             "Check network access / proxy settings and try again.") from err

    def upsert(self, collection: str, resource_id: str, body: Dict[str, Any], update_mask: str) -> str:
        """Create ``collection/resource_id`` or patch it. Returns 'created' / 'updated' / 'planned'."""
        name = f"{self.parent}/{collection}/{resource_id}"
        id_param = "toolId" if collection == "tools" else "toolsetId"
        if self.dry_run:
            print(f"[dry-run] GET {API_ROOT}/{name}  (create if 404, else patch)")
            self.request("POST", f"{self.parent}/{collection}?{id_param}={resource_id}", body)
            self.request("PATCH", f"{name}?updateMask={update_mask}", body)
            return "planned"
        status, existing = self.request("GET", name)
        if status == 200:
            patch = dict(body)
            if existing.get("etag"):
                patch["etag"] = existing["etag"]
            status, payload = self.request("PATCH", f"{name}?updateMask={update_mask}", patch)
            _raise_for(status, payload, f"update {collection}/{resource_id}")
            return "updated"
        if status != 404:
            _raise_for(status, existing, f"read {collection}/{resource_id}")
        status, payload = self.request("POST", f"{self.parent}/{collection}?{id_param}={resource_id}", body)
        _raise_for(status, payload, f"create {collection}/{resource_id}")
        return "created"


def _raise_for(status: int, payload: Dict[str, Any], what: str) -> None:
    if 200 <= status < 300:
        return
    message = (payload.get("error") or {}).get("message") or json.dumps(payload)[:500]
    raise SystemExit(f"ERROR: could not {what}: HTTP {status}: {message}")


def access_token() -> str:
    try:
        out = subprocess.run(["gcloud", "auth", "print-access-token"], check=True,
                             capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise SystemExit("ERROR: gcloud is not installed or not on PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"ERROR: gcloud auth print-access-token failed: {exc.stderr.strip()}\n"
                         "Run `gcloud auth login` first.") from exc
    token = out.stdout.strip()
    if not token:
        raise SystemExit("ERROR: gcloud returned an empty access token.")
    return token


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--project-id", required=True, help="Google Cloud project that owns the CXAS app.")
    parser.add_argument("--location", default="us", help="CXAS app location (e.g. us, eu). Default: us.")
    parser.add_argument("--app-id", required=True, help="CXAS app ID (UUID from the app URL).")
    parser.add_argument("--backend-url", required=True,
                        help="Public HTTPS base URL of this connector (the MCP endpoint is {url}/mcp).")
    parser.add_argument("--auth", choices=["service-agent-id-token", "none"], default="service-agent-id-token",
                        help="How CXAS authenticates to the MCP server. Default: service-agent-id-token.")
    parser.add_argument("--skip-tool", action="store_true", help="Do not create/update escalate_to_human.")
    parser.add_argument("--skip-toolset", action="store_true", help="Do not create/update the MCP toolset.")
    parser.add_argument("--dry-run", action="store_true", help="Print the API calls without sending them.")
    args = parser.parse_args(argv)
    if not args.backend_url.startswith("https://") and not args.dry_run:
        parser.error("--backend-url must be a public https:// URL reachable by CX Agent Studio.")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    token = None if args.dry_run else access_token()
    ces = CesClient(args.project_id, args.location, args.app_id, token, args.dry_run)
    print(f"App: {ces.parent}")
    if not args.skip_tool:
        result = ces.upsert("tools", TOOL_ID, escalate_tool_body(), "displayName,clientFunction")
        print(f"  tools/{TOOL_ID}: {result}")
    if not args.skip_toolset:
        result = ces.upsert("toolsets", TOOLSET_ID, mcp_toolset_body(args.backend_url, args.auth),
                            "displayName,description,executionType,mcpToolset")
        print(f"  toolsets/{TOOLSET_ID}: {result}  -> {args.backend_url.rstrip('/')}/mcp")
    print("\nNext: attach both to your agent in CX Agent Studio and redeploy the web widget channel.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
