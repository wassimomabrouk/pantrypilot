"""Security callbacks for PantryPilot.

`redact_pii_before_model` runs as an ADK `before_model_callback` on every
agent: user-typed text is scanned for common PII patterns (email addresses,
phone numbers, IBANs) and redacted *before* the request is sent to the LLM.

Rationale: a concierge agent receives free-form chat. Users paste things
without thinking ("my flatmate anna.mueller@web.de is coming for dinner").
Nothing about meal planning requires that data, so we strip it at the
boundary — the model never sees it, and it can never end up in logs or
model-side telemetry.
"""

from __future__ import annotations

import re
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse

# Deliberately conservative patterns: false positives are acceptable here
# (a redacted token never breaks meal planning), false negatives are worse.
_PII_PATTERNS = [
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b[A-Z]{2}\d{2}[ ]?(?:\d[ ]?){10,28}\b"), "[REDACTED_IBAN]"),
    (re.compile(r"(?<!\d)(?:\+?\d[ \-()]?){9,15}(?!\d)"), "[REDACTED_PHONE]"),
]


def _scrub(text: str) -> str:
    for pattern, token in _PII_PATTERNS:
        text = pattern.sub(token, text)
    return text


def redact_pii_before_model(
    callback_context: CallbackContext, llm_request: LlmRequest
) -> Optional[LlmResponse]:
    """Redact PII from all user-authored parts of the outgoing request.

    Returning None lets the (now sanitized) request proceed to the model.
    """
    for content in llm_request.contents or []:
        if content.role != "user":
            continue  # only sanitize user input; tool outputs are our own data
        for part in content.parts or []:
            if getattr(part, "text", None):
                part.text = _scrub(part.text)
    return None
