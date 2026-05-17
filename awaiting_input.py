from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .tools import Runtime

logger = logging.getLogger("hermes_opencode.awaiting_input")


_REGEX_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\?\s*$", re.MULTILINE), "trailing question mark"),
    (re.compile(r"\bwhich (?:option|approach|path|one|do you|would you|should)\b", re.IGNORECASE), "which-option phrase"),
    (re.compile(r"\bshould I\b", re.IGNORECASE), "should-I phrase"),
    (re.compile(r"\bwould you (?:prefer|like|want)\b", re.IGNORECASE), "would-you-prefer phrase"),
    (re.compile(r"\b(?:let|tell) me know\b", re.IGNORECASE), "let/tell-me-know phrase"),
    (re.compile(r"\bplease (?:confirm|clarify|advise|specify|let me know)\b", re.IGNORECASE), "please-confirm phrase"),
    (re.compile(r"\bwaiting (?:for|on) (?:your|the user|human|input|confirmation)\b", re.IGNORECASE), "explicit-wait phrase"),
    (re.compile(r"\b(?:y/n|yes/no)\b", re.IGNORECASE), "y/n phrase"),
    (re.compile(r"\bawait(?:ing)? (?:your )?(?:input|answer|decision|response)\b", re.IGNORECASE), "awaiting-input phrase"),
    (re.compile(r"\b(?:option|alternative)\s+[ABab1-9](?:[:\.\)])", re.MULTILINE | re.IGNORECASE), "labeled-option enumeration"),
)


@dataclass
class AwaitingInputCheck:
    awaiting: bool
    confidence: Literal["high", "medium", "low"]
    reason: str
    source: Literal["regex", "llm", "regex-fallback", "regex-no-llm"]
    last_assistant_text: str


def regex_check(text: str) -> tuple[bool, str]:
    for pattern, label in _REGEX_PATTERNS:
        if pattern.search(text):
            return True, label
    return False, "no awaiting-input regex matched"


def build_classifier_messages(text: str, max_chars: int) -> list[dict[str, str]]:
    snippet = text if len(text) <= max_chars else "... (truncated head) ...\n" + text[-max_chars:]
    system = (
        "You classify whether an AI coding assistant's most recent message is "
        "waiting for the human user to respond before proceeding, OR is a "
        "terminal statement that does not require human input. Reply with "
        "compact JSON exactly matching this schema and nothing else:\n"
        '{"awaiting": <true|false>, "confidence": "<high|medium|low>", "reason": "<one short sentence>"}'
        "\n"
        "Mark awaiting=true when the assistant: asks a question, proposes "
        "options to choose from, requests confirmation, requests clarification, "
        "stalls pending user input, or describes a plan and explicitly invites "
        "the user to direct the next step. Mark awaiting=false for: "
        "completion summaries, status reports, tool-call narration, and "
        "rhetorical questions inside a longer self-directed monologue."
    )
    user = f"Assistant message:\n---\n{snippet}\n---"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_classifier_response(content: str) -> tuple[bool, str, str]:
    try:
        match = re.search(r"\{.*?\}", content, re.DOTALL)
        if not match:
            raise ValueError("no JSON object in classifier response")
        data = json.loads(match.group(0))
        awaiting = bool(data.get("awaiting"))
        confidence = str(data.get("confidence", "low")).lower()
        if confidence not in {"high", "medium", "low"}:
            confidence = "low"
        reason = str(data.get("reason", "")).strip() or "(no reason)"
        return awaiting, confidence, reason
    except (ValueError, TypeError, json.JSONDecodeError) as e:
        raise ValueError(f"classifier response unparseable: {e}; content={content[:200]!r}") from e


async def check(runtime: "Runtime", text: str) -> AwaitingInputCheck:
    cfg = runtime.config
    text = text or ""
    if not text.strip():
        return AwaitingInputCheck(
            awaiting=False, confidence="high",
            reason="empty assistant text",
            source="regex", last_assistant_text=text,
        )

    regex_hit, regex_reason = regex_check(text)

    if not cfg.classifier_enabled:
        return AwaitingInputCheck(
            awaiting=regex_hit,
            confidence="medium" if regex_hit else "high",
            reason=regex_reason,
            source="regex-no-llm",
            last_assistant_text=text,
        )

    try:
        from agent.auxiliary_client import async_call_llm  # type: ignore
    except ImportError:
        logger.info("auxiliary_client not importable; using regex result")
        return AwaitingInputCheck(
            awaiting=regex_hit,
            confidence="low",
            reason=f"{regex_reason} (no auxiliary client)",
            source="regex-fallback",
            last_assistant_text=text,
        )

    messages = build_classifier_messages(text, cfg.classifier_max_input_chars)
    try:
        resp = await async_call_llm(
            task=cfg.classifier_task_name,
            messages=messages,
            max_tokens=cfg.classifier_max_output_tokens,
            temperature=0.0,
            timeout=cfg.classifier_timeout_sec,
        )
    except Exception as e:
        logger.info("classifier call failed (%s); using regex result", e)
        return AwaitingInputCheck(
            awaiting=regex_hit,
            confidence="low",
            reason=f"{regex_reason} (llm error: {e!r})",
            source="regex-fallback",
            last_assistant_text=text,
        )

    content = _extract_content(resp)
    if content is None:
        return AwaitingInputCheck(
            awaiting=regex_hit,
            confidence="low",
            reason=f"{regex_reason} (no content in llm response)",
            source="regex-fallback",
            last_assistant_text=text,
        )

    try:
        awaiting, confidence, reason = parse_classifier_response(content)
    except ValueError as e:
        logger.info("classifier parse failed: %s", e)
        return AwaitingInputCheck(
            awaiting=regex_hit,
            confidence="low",
            reason=f"{regex_reason} (llm parse error)",
            source="regex-fallback",
            last_assistant_text=text,
        )

    return AwaitingInputCheck(
        awaiting=awaiting,
        confidence=confidence,  # type: ignore[arg-type]
        reason=reason,
        source="llm",
        last_assistant_text=text,
    )


def _extract_content(resp: object) -> str | None:
    choices = getattr(resp, "choices", None)
    if not choices:
        return None
    first = choices[0]
    message = getattr(first, "message", None) or (first.get("message") if isinstance(first, dict) else None)
    if message is None:
        return None
    content = getattr(message, "content", None) or (message.get("content") if isinstance(message, dict) else None)
    if isinstance(content, str):
        return content
    return None


def to_dict(check: AwaitingInputCheck) -> dict[str, object]:
    return asdict(check)
