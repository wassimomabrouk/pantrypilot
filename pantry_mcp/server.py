"""PantryVault — a local MCP server that owns ALL personal data for PantryPilot.

Design principle (security-by-architecture):
    The LLM agents never touch the database directly. Every read/write goes
    through the narrow, validated tool surface defined below, over MCP stdio.
    Data (pantry contents, dietary restrictions & allergies — i.e. health
    data — meal plans, shopping lists) lives in a local SQLite file and never
    leaves the user's machine.

Security features implemented here:
    1. Local-only storage  — SQLite file on disk, no network I/O at all.
    2. Input validation    — type/length/bounds checks on every tool argument
                             before anything reaches the database.
    3. Parameterized SQL   — no string interpolation; immune to SQL injection
                             even if the model produces adversarial arguments.
    4. Audit logging       — every tool invocation is recorded (tool name +
                             timestamp) so the user can inspect what the
                             agents did with their data.
    5. Narrow tool surface — tools do one thing; agents are additionally
                             restricted per-agent via ADK `tool_filter`
                             allowlists (see pantry_concierge/agent.py).

Run standalone for testing:  python -m pantry_mcp.server
(ADK spawns it automatically over stdio in normal operation.)
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Storage location: project-local by default, overridable via env var.
# ---------------------------------------------------------------------------
_DEFAULT_DB = Path(__file__).resolve().parent.parent / "data" / "pantry.db"
DB_PATH = Path(os.environ.get("PANTRY_DB", _DEFAULT_DB))

# Validation limits (defense-in-depth against prompt-injected garbage input).
MAX_NAME_LEN = 80
MAX_TEXT_LEN = 4000
MAX_QUANTITY = 10_000
ALLOWED_UNITS = {"g", "kg", "ml", "l", "piece", "pack", "can", "jar", "bottle"}
_NAME_RE = re.compile(r"^[\w\s\-&().,'%/]+$", re.UNICODE)

mcp = FastMCP("pantry-vault")


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def _conn() -> sqlite3.Connection:
    """Open a short-lived connection (WAL mode so several agent processes
    spawned by ADK can share the file safely)."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    return conn


def _init_db() -> None:
    with _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS pantry (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL COLLATE NOCASE,
                quantity REAL NOT NULL,
                unit TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(name, unit)
            );
            CREATE TABLE IF NOT EXISTS preferences (
                key TEXT PRIMARY KEY,        -- e.g. 'allergies', 'diet',
                value TEXT NOT NULL          --      'dislikes', 'budget_eur',
            );                               --      'household_size', 'stores'
            CREATE TABLE IF NOT EXISTS meal_plans (
                id INTEGER PRIMARY KEY,
                week_label TEXT NOT NULL,
                plan_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS shopping_lists (
                id INTEGER PRIMARY KEY,
                list_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY,
                ts TEXT NOT NULL,
                tool TEXT NOT NULL
            );
            """
        )


def _audit(tool: str) -> None:
    """Record that a tool was invoked. We deliberately do NOT log arguments,
    so the audit trail itself never duplicates sensitive values."""
    with _conn() as c:
        c.execute(
            "INSERT INTO audit_log (ts, tool) VALUES (?, ?)",
            (time.strftime("%Y-%m-%d %H:%M:%S"), tool),
        )


# ---------------------------------------------------------------------------
# Validation helpers — every tool argument passes through one of these.
# ---------------------------------------------------------------------------
def _valid_name(name: str) -> str:
    name = (name or "").strip()
    if not name or len(name) > MAX_NAME_LEN or not _NAME_RE.match(name):
        raise ValueError(
            f"Invalid item name (1-{MAX_NAME_LEN} chars, letters/numbers/basic punctuation)."
        )
    return name


def _valid_quantity(q: float) -> float:
    try:
        q = float(q)
    except (TypeError, ValueError):
        raise ValueError("Quantity must be a number.")
    if not (0 < q <= MAX_QUANTITY):
        raise ValueError(f"Quantity must be between 0 and {MAX_QUANTITY}.")
    return q


def _valid_unit(unit: str) -> str:
    unit = (unit or "").strip().lower()
    if unit not in ALLOWED_UNITS:
        raise ValueError(f"Unit must be one of: {sorted(ALLOWED_UNITS)}")
    return unit


def _valid_text(text: str, label: str) -> str:
    text = (text or "").strip()
    if not text or len(text) > MAX_TEXT_LEN:
        raise ValueError(f"{label} must be 1-{MAX_TEXT_LEN} characters.")
    return text


# ---------------------------------------------------------------------------
# Tools: pantry inventory
# ---------------------------------------------------------------------------
@mcp.tool()
def list_pantry() -> str:
    """List every item currently in the pantry with quantity and unit."""
    _audit("list_pantry")
    with _conn() as c:
        rows = c.execute(
            "SELECT name, quantity, unit, updated_at FROM pantry ORDER BY name"
        ).fetchall()
    if not rows:
        return "The pantry is empty."
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
def add_pantry_item(name: str, quantity: float, unit: str) -> str:
    """Add an item to the pantry, or increase its quantity if it already
    exists. Units: g, kg, ml, l, piece, pack, can, jar, bottle."""
    _audit("add_pantry_item")
    name, quantity, unit = _valid_name(name), _valid_quantity(quantity), _valid_unit(unit)
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    with _conn() as c:
        c.execute(
            """INSERT INTO pantry (name, quantity, unit, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(name, unit)
               DO UPDATE SET quantity = quantity + excluded.quantity,
                             updated_at = excluded.updated_at""",
            (name, quantity, unit, now),
        )
    return f"Added {quantity} {unit} of {name}."


@mcp.tool()
def consume_pantry_item(name: str, quantity: float, unit: str) -> str:
    """Reduce the quantity of a pantry item (e.g. after cooking). Removes the
    item entirely when the quantity reaches zero."""
    _audit("consume_pantry_item")
    name, quantity, unit = _valid_name(name), _valid_quantity(quantity), _valid_unit(unit)
    with _conn() as c:
        row = c.execute(
            "SELECT quantity FROM pantry WHERE name = ? AND unit = ?", (name, unit)
        ).fetchone()
        if row is None:
            return f"'{name}' ({unit}) is not in the pantry."
        new_q = row["quantity"] - quantity
        if new_q <= 0:
            c.execute("DELETE FROM pantry WHERE name = ? AND unit = ?", (name, unit))
            return f"Removed {name} from the pantry (used up)."
        c.execute(
            "UPDATE pantry SET quantity = ?, updated_at = ? WHERE name = ? AND unit = ?",
            (new_q, time.strftime("%Y-%m-%d %H:%M:%S"), name, unit),
        )
    return f"{name}: {new_q} {unit} remaining."


@mcp.tool()
def remove_pantry_item(name: str, unit: str) -> str:
    """Delete an item from the pantry completely (e.g. expired food)."""
    _audit("remove_pantry_item")
    name, unit = _valid_name(name), _valid_unit(unit)
    with _conn() as c:
        cur = c.execute("DELETE FROM pantry WHERE name = ? AND unit = ?", (name, unit))
    return f"Removed {name}." if cur.rowcount else f"'{name}' was not in the pantry."


# ---------------------------------------------------------------------------
# Tools: preferences (contains sensitive data — allergies are health data)
# ---------------------------------------------------------------------------
@mcp.tool()
def get_preferences() -> str:
    """Get the user's stored preferences: allergies, diet, dislikes,
    weekly budget (EUR), household size, preferred stores."""
    _audit("get_preferences")
    with _conn() as c:
        rows = c.execute("SELECT key, value FROM preferences").fetchall()
    if not rows:
        return "No preferences stored yet."
    return json.dumps({r["key"]: r["value"] for r in rows}, ensure_ascii=False)


@mcp.tool()
def set_preference(key: str, value: str) -> str:
    """Store or update one preference. Allowed keys: allergies, diet,
    dislikes, budget_eur, household_size, stores."""
    _audit("set_preference")
    allowed = {"allergies", "diet", "dislikes", "budget_eur", "household_size", "stores"}
    key = (key or "").strip().lower()
    if key not in allowed:  # closed key set: agents cannot invent new fields
        raise ValueError(f"Preference key must be one of: {sorted(allowed)}")
    value = _valid_text(value, "Preference value")
    with _conn() as c:
        c.execute(
            "INSERT INTO preferences (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
    return f"Preference '{key}' saved."


# ---------------------------------------------------------------------------
# Tools: meal plans & shopping lists
# ---------------------------------------------------------------------------
@mcp.tool()
def save_meal_plan(week_label: str, plan_json: str) -> str:
    """Persist a weekly meal plan. `plan_json` must be a JSON string mapping
    days to meals, e.g. {"Monday": {"dinner": "Couscous", "ingredients": [...]}}"""
    _audit("save_meal_plan")
    week_label = _valid_text(week_label, "Week label")[:40]
    plan_json = _valid_text(plan_json, "Plan JSON")
    try:
        json.loads(plan_json)  # reject malformed JSON before it hits the DB
    except json.JSONDecodeError:
        raise ValueError("plan_json is not valid JSON.")
    with _conn() as c:
        c.execute(
            "INSERT INTO meal_plans (week_label, plan_json, created_at) VALUES (?, ?, ?)",
            (week_label, plan_json, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
    return f"Meal plan for '{week_label}' saved."


@mcp.tool()
def get_latest_meal_plan() -> str:
    """Retrieve the most recently saved meal plan."""
    _audit("get_latest_meal_plan")
    with _conn() as c:
        row = c.execute(
            "SELECT week_label, plan_json, created_at FROM meal_plans "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.dumps(dict(row), ensure_ascii=False) if row else "No meal plan saved yet."


@mcp.tool()
def save_shopping_list(list_json: str) -> str:
    """Persist a shopping list. `list_json` must be a JSON string, e.g.
    [{"item": "tomatoes", "quantity": 500, "unit": "g", "store": "Rewe"}]"""
    _audit("save_shopping_list")
    list_json = _valid_text(list_json, "List JSON")
    try:
        json.loads(list_json)
    except json.JSONDecodeError:
        raise ValueError("list_json is not valid JSON.")
    with _conn() as c:
        c.execute(
            "INSERT INTO shopping_lists (list_json, created_at) VALUES (?, ?)",
            (list_json, time.strftime("%Y-%m-%d %H:%M:%S")),
        )
    return "Shopping list saved."


@mcp.tool()
def get_latest_shopping_list() -> str:
    """Retrieve the most recently saved shopping list."""
    _audit("get_latest_shopping_list")
    with _conn() as c:
        row = c.execute(
            "SELECT list_json, created_at FROM shopping_lists ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return json.dumps(dict(row), ensure_ascii=False) if row else "No shopping list saved yet."


# ---------------------------------------------------------------------------
# Tools: transparency
# ---------------------------------------------------------------------------
@mcp.tool()
def get_audit_log(limit: int = 20) -> str:
    """Show the most recent tool invocations, so the user can verify exactly
    what the agents did with their data."""
    limit = max(1, min(int(limit), 200))  # bound the query size
    with _conn() as c:
        rows = c.execute(
            "SELECT ts, tool FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return json.dumps([dict(r) for r in rows], ensure_ascii=False)


_init_db()

if __name__ == "__main__":
    # ADK's McpToolset spawns this file as a subprocess and talks MCP
    # over stdio — no ports, no network exposure.
    mcp.run(transport="stdio")
