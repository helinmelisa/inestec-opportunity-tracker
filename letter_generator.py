"""
Ollama-based motivation letter generator.
Calls the local Ollama server at http://localhost:11434.
"""
from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from typing import Optional

OLLAMA_URL  = "http://localhost:11434"
DEFAULT_MODEL = "llama3.1:8b"

PROFILE_FILE = os.path.join(os.path.dirname(__file__), "profile.json")


def load_profile() -> dict:
    try:
        with open(PROFILE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def ollama_available() -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def list_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=5) as r:
            data = json.loads(r.read())
            return [m["name"] for m in data.get("models", [])]
    except Exception:
        return []


def _build_prompt(profile: dict, opportunity: dict, tone: str = "formal") -> str:
    edu = profile.get("education", [{}])[0]
    exp_entries = profile.get("experience", [])
    exp_lines = []
    for e in exp_entries:
        highlights = "; ".join(e.get("highlights", [])[:4])
        exp_lines.append(f"- {e['role']} at {e['company']} ({e['duration']}): {highlights}")

    thesis = profile.get("bachelor_thesis", {})
    skills = ", ".join(profile.get("skills", [])[:10])
    interests = "; ".join(profile.get("interests", []))

    job_summary = opportunity.get("summary", "")
    job_summary_section = f"\nDetailed description from the listing:\n{job_summary}" if job_summary else ""

    tone_instruction = {
        "formal":     "Write in a formal, professional academic tone.",
        "enthusiastic": "Write in an enthusiastic but professional tone that conveys genuine excitement.",
        "concise":    "Write concisely — 3 tight paragraphs, no fluff.",
    }.get(tone, "Write in a formal, professional academic tone.")

    return f"""You are helping write a motivation letter for a research opportunity. {tone_instruction}

=== APPLICANT PROFILE ===
Name: {profile.get('name', '')}
Education: {edu.get('degree', '')} at {edu.get('institution', '')} ({edu.get('status', '')} {edu.get('year', '')})
Experience:
{chr(10).join(exp_lines)}
Bachelor thesis: {thesis.get('title', '')} — {thesis.get('description', '')}
Key skills: {skills}
Research interests: {interests}

=== OPPORTUNITY ===
Reference: {opportunity.get('ref', '')}
Work Area: {opportunity.get('work_area', '')}
Position: {opportunity.get('position', '')}
Centre: {opportunity.get('centre', '')}
Scientific Advisor: {opportunity.get('advisor', '')}
Deadline: {opportunity.get('deadline', '')}
URL: {opportunity.get('url', '')}{job_summary_section}

=== INSTRUCTIONS ===
Write a motivation letter (3–4 paragraphs, ~350 words) that:
1. Opens with why this specific work area and centre interests the applicant
2. Connects their ACTUAL experience and skills directly to this opportunity's requirements
3. References the applicant's relevant work at Morla Moves and/or their thesis if applicable
4. Closes with a clear expression of intent to contribute

Address it to the Scientific Advisor if provided. Sign off as {profile.get('name', 'the applicant')}.
Do NOT add "Subject:" lines, headers, or placeholders like [date]. Output only the letter body.
"""


def generate_letter(
    opportunity: dict,
    tone: str = "formal",
    model: str = DEFAULT_MODEL,
    profile: Optional[dict] = None,
) -> str:
    """Generate a motivation letter for the given opportunity using Ollama.

    Returns the generated text, or raises RuntimeError if Ollama is unavailable.
    """
    if profile is None:
        profile = load_profile()

    if not ollama_available():
        raise RuntimeError(
            "Ollama is not running. Start it with: ollama serve\n"
            "Or install from: https://ollama.com"
        )

    prompt = _build_prompt(profile, opportunity, tone)

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {
            "temperature": 0.7,
            "num_predict": 800,
        }
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())
            return result.get("response", "").strip()
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise RuntimeError(f"Ollama error {e.code}: {body}")


def generate_letter_stream(
    opportunity: dict,
    tone: str = "formal",
    model: str = DEFAULT_MODEL,
    profile: Optional[dict] = None,
):
    """Generator that yields text chunks as Ollama streams them."""
    if profile is None:
        profile = load_profile()

    if not ollama_available():
        raise RuntimeError("Ollama is not running. Start it with: ollama serve")

    prompt = _build_prompt(profile, opportunity, tone)

    payload = json.dumps({
        "model": model,
        "prompt": prompt,
        "stream": True,
        "options": {"temperature": 0.7, "num_predict": 800},
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=120) as r:
        for line in r:
            line = line.strip()
            if not line:
                continue
            try:
                chunk = json.loads(line)
                token = chunk.get("response", "")
                if token:
                    yield token
                if chunk.get("done"):
                    break
            except json.JSONDecodeError:
                continue
