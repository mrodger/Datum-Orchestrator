"""Skill library — Hermes-style SKILL.md management.

Skills are markdown files in /app/skills/. Each describes a reusable procedure
the orchestrator can inject into drone prompts. Skills evolve based on
dispatch outcome quality.
"""

from __future__ import annotations

import os
from pathlib import Path

SKILLS_DIR = Path(os.environ.get("SKILLS_DIR", "/app/skills"))


def list_skills() -> list[dict]:
    """Return Level 0 skill index: name + first line (description)."""
    skills = []
    if not SKILLS_DIR.exists():
        return skills

    for f in sorted(SKILLS_DIR.glob("*.md")):
        first_line = ""
        with open(f) as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith("#"):
                    first_line = line
                    break
        skills.append({"name": f.stem, "description": first_line, "path": str(f)})

    return skills


def load_skill(name: str) -> str | None:
    """Load full SKILL.md content by name."""
    path = SKILLS_DIR / f"{name}.md"
    if not path.exists():
        return None
    return path.read_text()


def select_skills_for_task(description: str) -> list[str]:
    """Simple keyword-based skill selection.

    Phase 1: match skill names/descriptions against task description.
    Phase 3 will replace this with LLM-based selection using outcome scores.
    """
    skills = list_skills()
    selected = []

    desc_lower = description.lower()
    for s in skills:
        # Check if skill name or description keywords appear in task
        name_words = s["name"].replace("-", " ").split()
        if any(w in desc_lower for w in name_words):
            selected.append(s["name"])

    return selected


def render_skills_context(skill_names: list[str]) -> str:
    """Render selected skills into a context block for prompt injection."""
    blocks = []
    for name in skill_names:
        content = load_skill(name)
        if content:
            blocks.append(f"### Skill: {name}\n\n{content}")

    if not blocks:
        return ""

    return "## Available Skills\n\n" + "\n\n---\n\n".join(blocks)
