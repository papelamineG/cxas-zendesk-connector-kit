"""CX Agent Studio <-> Zendesk connector kit backend.

Modules:
  * escalation         -- live human-escalation relay (ticket + comment polling),
                          with a built-in in-memory Zendesk v2 sandbox emulator.
  * zendesk_mcp_tools  -- server-side MCP tools (FastMCP) for asynchronous
                          ticket lookup / creation / follow-up.
  * server             -- the unified FastAPI application.
"""

__version__ = "1.0.0"
