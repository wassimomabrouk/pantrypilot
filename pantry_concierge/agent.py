"""PantryPilot agent definitions (Google ADK).

Architecture:

    user ⇄ pantry_concierge (root orchestrator, no data tools)
              ├── pantry_manager      — inventory CRUD
              ├── meal_planner        — weekly plans + preferences
              └── shopping_assistant  — plan − pantry = shopping list

    every specialist ⇄ PantryVault MCP server (stdio, local SQLite)

Security decisions visible in this file:
- Least privilege: each specialist receives an *allowlist* of MCP tools via
  `tool_filter`; e.g. the shopping assistant can read the meal plan but can
  never modify the pantry or preferences.
- The root agent has NO data tools at all — it can only delegate.
- `redact_pii_before_model` scrubs emails/phones/IBANs from user input
  before any model call (see callbacks.py).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

from .callbacks import redact_pii_before_model
from . import prompts

MODEL = os.environ.get("PANTRYPILOT_MODEL", "gemini-2.5-flash")

# Absolute path so `adk web` / `adk run` work from any working directory.
_MCP_SERVER = Path(__file__).resolve().parent.parent / "pantry_mcp" / "server.py"


def _vault_tools(allowed: list[str]) -> McpToolset:
    """Connect to the PantryVault MCP server over stdio, exposing ONLY the
    tools in `allowed` to the requesting agent (least-privilege principle)."""
    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,          # same interpreter, no PATH tricks
                args=[str(_MCP_SERVER)],
            ),
            timeout=30,
        ),
        tool_filter=allowed,
    )


# --------------------------------------------------------------------------
# Specialist agents
# --------------------------------------------------------------------------
pantry_manager = LlmAgent(
    name="pantry_manager",
    model=MODEL,
    description="Tracks kitchen inventory: add, consume, remove, and list pantry items.",
    instruction=prompts.PANTRY_MANAGER_INSTRUCTION,
    tools=[
        _vault_tools(
            ["list_pantry", "add_pantry_item", "consume_pantry_item", "remove_pantry_item"]
        )
    ],
    before_model_callback=redact_pii_before_model,
)

meal_planner = LlmAgent(
    name="meal_planner",
    model=MODEL,
    description=(
        "Creates weekly meal plans that respect allergies, diet, budget and "
        "current pantry contents; manages food preferences."
    ),
    instruction=prompts.MEAL_PLANNER_INSTRUCTION,
    tools=[
        _vault_tools(
            ["get_preferences", "set_preference", "list_pantry",
             "save_meal_plan", "get_latest_meal_plan"]
        )
    ],
    before_model_callback=redact_pii_before_model,
)

shopping_assistant = LlmAgent(
    name="shopping_assistant",
    model=MODEL,
    description=(
        "Builds store-grouped shopping lists from the current meal plan "
        "minus what is already in the pantry."
    ),
    instruction=prompts.SHOPPING_ASSISTANT_INSTRUCTION,
    # Read-mostly allowlist: this agent can never alter pantry or preferences.
    tools=[
        _vault_tools(
            ["get_latest_meal_plan", "list_pantry", "get_preferences",
             "save_shopping_list", "get_latest_shopping_list"]
        )
    ],
    before_model_callback=redact_pii_before_model,
)

# --------------------------------------------------------------------------
# Root orchestrator — no data tools, delegation only.
# --------------------------------------------------------------------------
root_agent = LlmAgent(
    name="pantry_concierge",
    model=MODEL,
    description="Personal kitchen concierge that coordinates pantry, planning and shopping specialists.",
    instruction=prompts.ROOT_INSTRUCTION,
    sub_agents=[pantry_manager, meal_planner, shopping_assistant],
    before_model_callback=redact_pii_before_model,
)
