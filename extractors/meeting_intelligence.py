from __future__ import annotations

import json
import re


DOMAIN_GLOSSARY = {
    "scotty": "SCTE",
    "scotty 35": "SCTE-35",
    "scotty 104": "SCTE-104",
    "scuddy": "SCTE",
    "dash 40": "ST 2110-40",
}


def short_text(value: str, limit: int = 220) -> str:
    text = " ".join((value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "..."


def apply_glossary(value: str) -> str:
    text = value
    for wrong, right in DOMAIN_GLOSSARY.items():
        text = re.sub(rf"\b{re.escape(wrong)}\b", right, text, flags=re.IGNORECASE)
    return text


def clean_vid2kb_line(value: str) -> str:
    text = value.strip().lstrip("-").strip()
    text = re.sub(r"^`?\d{2}:\d{2}:\d{2}(?:\.\d+)?`?\s*", "", text)
    text = re.sub(r"^speech:\s*", "", text, flags=re.IGNORECASE)
    text = re.split(r"\s+\|\s+visual:\s+", text, maxsplit=1, flags=re.IGNORECASE)[0]
    return short_text(apply_glossary(text), 260)


def compact_meeting_lines(lines: list[str], limit: int = 8) -> list[str]:
    chunks = []
    current = ""
    for line in lines:
        if not line:
            continue
        candidate = f"{current} {line}".strip() if current else line
        if current and (len(candidate) > 220 or current.endswith((".", "?", "!"))):
            chunks.append(current)
            current = line
        else:
            current = candidate
        if len(chunks) >= limit:
            break
    if current and len(chunks) < limit:
        chunks.append(current)
    return chunks


def candidate_actions(transcript: str, limit: int = 12) -> list[str]:
    candidates = []
    patterns = ("action", "follow", "need", "needs", "issue", "problem", "question", "confirm", "verify", "check", "workaround", "todo")
    for raw_line in transcript.splitlines():
        line = raw_line.strip("- ").strip()
        if not line or line.lower().startswith(("source video:", "provenance:")):
            continue
        if any(pattern in line.lower() for pattern in patterns):
            candidates.append(clean_vid2kb_line(line))
        if len(candidates) >= limit:
            break
    return candidates


def overview_summary(overview: str, limit: int = 10) -> str:
    lines = []
    in_summary = False
    for raw_line in overview.splitlines():
        line = raw_line.strip()
        if line.lower() == "## summary":
            in_summary = True
            continue
        if in_summary and line.startswith("## "):
            break
        if in_summary and line.startswith("- "):
            cleaned = clean_vid2kb_line(line)
            if cleaned:
                lines.append(cleaned)
        if len(lines) >= limit:
            break
    if not lines:
        lines = [clean_vid2kb_line(line) for line in overview.splitlines() if line.startswith("- ")][:limit]
    return "Meeting draft from vid2kb. Review before saving as final account context.\n" + "\n".join(f"- {line}" for line in lines if line)


def build_extraction_prompt(packet: dict) -> str:
    return (
        "You are extracting TAM Console meeting intelligence from bounded evidence.\n"
        "Ignore greetings, filler, false starts, transcript fragments, and unsupported guesses.\n"
        "Apply domain corrections such as Scotty/Scuddy -> SCTE, SCTE 35 -> SCTE-35, SCTE 104 -> SCTE-104, dash 40 -> ST 2110-40.\n"
        "Prefer transcript evidence for topics and actions. Use visual evidence only for screen context, not as customer statements.\n"
        "Return strict JSON with: topics, decisions, action_items, open_questions, risks, customer_requests, "
        "environment_references, ticket_references, health_impact, health_reasoning, evidence.\n"
        "Each action item must include task, owner, due_date, confidence, and evidence.\n\n"
        + json.dumps(packet, indent=2, sort_keys=True)
    )
