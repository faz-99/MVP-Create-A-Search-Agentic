from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
import re

SKILLS_DIR = Path(__file__).resolve().parent.parent / "src" / "skills"
ALWAYS_ON_SKILL_NAMES: tuple[str, ...] = ("resolve-request")
_FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(?P<frontmatter>.*?)\n---\s*\n?(?P<body>.*)$", re.DOTALL)
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_OVERRIDE_PATTERN = re.compile(
    r"\buse\s+skills?\s*:\s*(?P<skills>[^\n.;]+)",
    re.IGNORECASE,
)
_TOP_ROUTED_SKILLS = 2
_MINIMUM_ROUTING_SCORE = 2


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    keywords: tuple[str, ...]
    body: str
    path: Path


def _parse_frontmatter(raw_text: str) -> tuple[dict[str, str | list[str]], str]:
    match = _FRONTMATTER_PATTERN.match(raw_text)
    if not match:
        return {}, raw_text.strip()

    frontmatter_text = match.group("frontmatter")
    body = match.group("body").strip()
    lines = frontmatter_text.splitlines()
    parsed: dict[str, str | list[str]] = {}
    index = 0

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in line:
            index += 1
            continue

        if line.startswith((" ", "\t")):
            index += 1
            continue

        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()

        if not value:
            list_items: list[str] = []
            index += 1
            while index < len(lines):
                block_line = lines[index]
                stripped_block_line = block_line.strip()
                if not stripped_block_line:
                    index += 1
                    continue
                if not block_line.startswith((" ", "\t")):
                    break
                if stripped_block_line.startswith("- "):
                    list_items.append(stripped_block_line[2:].strip().strip('"\''))
                    index += 1
                    continue
                break

            if list_items:
                parsed[key] = list_items
                continue

        if value in {">", ">-", "|", "|-"}:
            block_lines: list[str] = []
            index += 1
            while index < len(lines):
                block_line = lines[index]
                if block_line.startswith((" ", "\t")):
                    block_lines.append(block_line.strip())
                    index += 1
                    continue
                if not block_line.strip():
                    block_lines.append("")
                    index += 1
                    continue
                break

            joiner = "\n" if value.startswith("|") else " "
            parsed[key] = joiner.join(part for part in block_lines if part).strip()
            continue

        parsed[key] = value.strip('"\'')
        index += 1

    return parsed, body


def _normalize_keywords(value: str | list[str] | None) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, list):
        return tuple(item.strip().lower() for item in value if item.strip())
    if not value.strip():
        return ()
    return tuple(
        item.strip().lower()
        for item in value.split(",")
        if item.strip()
    )

from typing import List
def get_skills(specific_skills: List[str] | None = None) -> dict[str, SkillDefinition]:
    if not SKILLS_DIR.exists():
        return {}
    skill_definitions: dict[str, SkillDefinition] = {}
    for skill_path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        raw_text = skill_path.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(raw_text)
        name_value = metadata.get("name", skill_path.parent.name)
        
        if specific_skills is None or name_value in specific_skills:
            description_value = metadata.get("description", "")
            name = str(name_value).strip()
            description = str(description_value).strip()
            keywords = _normalize_keywords(metadata.get("keywords"))
            skill_definitions[name] = SkillDefinition(
                name=name,
                description=description,
                keywords=keywords,
                body=body,
                path=skill_path,
            )
    return skill_definitions    

@lru_cache(maxsize=1)
def load_skills() -> dict[str, SkillDefinition]:
    if not SKILLS_DIR.exists():
        return {}

    skill_definitions: dict[str, SkillDefinition] = {}
    for skill_path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        raw_text = skill_path.read_text(encoding="utf-8")
        metadata, body = _parse_frontmatter(raw_text)
        name_value = metadata.get("name", skill_path.parent.name)
        description_value = metadata.get("description", "")
        name = str(name_value).strip()
        description = str(description_value).strip()
        keywords = _normalize_keywords(metadata.get("keywords"))
        skill_definitions[name] = SkillDefinition(
            name=name,
            description=description,
            keywords=keywords,
            body=body,
            path=skill_path,
        )
    return skill_definitions


def _tokenize(text: str) -> set[str]:
    return {token for token in _TOKEN_PATTERN.findall(text.lower()) if len(token) > 1}


def _normalize_skill_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def _build_skill_aliases(skills: dict[str, SkillDefinition]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for skill in skills.values():
        alias_candidates = {
            skill.name,
            skill.name.replace("-", " "),
            skill.path.parent.name,
        }
        for alias in alias_candidates:
            normalized_alias = _normalize_skill_key(alias)
            if normalized_alias:
                aliases[normalized_alias] = skill.name
    return aliases


def get_explicit_skill_overrides(user_input: str) -> list[str]:
    skills = load_skills()
    if not skills:
        return []

    aliases = _build_skill_aliases(skills)
    selected_names: list[str] = []
    for match in _OVERRIDE_PATTERN.finditer(user_input):
        raw_skills = match.group("skills")
        for raw_name in re.split(r",|\band\b", raw_skills, flags=re.IGNORECASE):
            normalized_name = _normalize_skill_key(raw_name)
            resolved_name = aliases.get(normalized_name)
            if not resolved_name or resolved_name in selected_names:
                continue
            selected_names.append(resolved_name)
    return selected_names


def _score_skill(user_input: str, skill: SkillDefinition) -> int:
    lowered_input = user_input.lower()
    input_tokens = _tokenize(user_input)
    if not input_tokens:
        return 0

    score = 0
    name_phrase = skill.name.replace("-", " ")
    if skill.name in lowered_input or name_phrase in lowered_input:
        score += 5

    for keyword in skill.keywords:
        keyword_tokens = _tokenize(keyword)
        if keyword in lowered_input:
            score += 4 + len(keyword_tokens)
            continue
        if keyword_tokens and keyword_tokens <= input_tokens:
            score += 2 + len(keyword_tokens)

    metadata_tokens = _tokenize(f"{skill.name} {skill.description}")
    score += len(input_tokens & metadata_tokens)

    heading_tokens = _tokenize(" ".join(line for line in skill.body.splitlines() if line.startswith("#")))
    score += len(input_tokens & heading_tokens)

    return score


def get_skill_routing_debug(user_input: str) -> list[dict[str, object]]:
    skills = load_skills()
    lowered_input = user_input.lower()
    input_tokens = _tokenize(user_input)
    debug_rows: list[dict[str, object]] = []
    explicit_overrides = set(get_explicit_skill_overrides(user_input))

    active_skill_names = {skill.name for skill in get_active_skills(user_input)}
    for skill in skills.values():
        matched_keywords = [
            keyword
            for keyword in skill.keywords
            if keyword in lowered_input or (_tokenize(keyword) and _tokenize(keyword) <= input_tokens)
        ]
        debug_rows.append(
            {
                "skill": skill.name,
                "always_on": skill.name in ALWAYS_ON_SKILL_NAMES,
                "manual_override": skill.name in explicit_overrides,
                "selected": skill.name in active_skill_names,
                "score": _score_skill(user_input, skill),
                "matched_keywords": ", ".join(matched_keywords),
                "keywords": ", ".join(skill.keywords),
            }
        )

    return sorted(debug_rows, key=lambda row: (bool(row["selected"]), int(row["score"])), reverse=True)


def get_active_skills(user_input: str) -> list[SkillDefinition]:
    skills = load_skills()
    selected_names: list[str] = [
        name for name in ALWAYS_ON_SKILL_NAMES if name in skills
    ]
    explicit_overrides = get_explicit_skill_overrides(user_input)
    for skill_name in explicit_overrides:
        if skill_name not in selected_names:
            selected_names.append(skill_name)

    routed_candidates = [
        skill
        for skill in skills.values()
        if skill.name not in ALWAYS_ON_SKILL_NAMES and skill.name not in explicit_overrides
    ]
    scored_candidates = sorted(
        (
            (_score_skill(user_input, skill), skill.name)
            for skill in routed_candidates
        ),
        reverse=True,
    )

    for score, skill_name in scored_candidates[:_TOP_ROUTED_SKILLS]:
        if score < _MINIMUM_ROUTING_SCORE:
            continue
        selected_names.append(skill_name)

    return [skills[name] for name in selected_names if name in skills]


def build_skill_system_message(user_input: str) -> str | None:
    active_skills = get_active_skills(user_input)
    if not active_skills:
        return None

    skill_sections = [
        (
            f"## Skill: {skill.name}\n"
            f"Description: {skill.description or 'No description provided.'}\n\n"
            f"{skill.body.strip()}"
        ).strip()
        for skill in active_skills
    ]
    return (
        "Apply the following repository skills as high-priority operating instructions for this request. "
        "Always-on governance skills appear first, followed by any routed task skills.\n\n"
        + "\n\n".join(skill_sections)
    )


def get_always_on_skills() -> list[SkillDefinition]:
    skills = load_skills()
    return [skills[name] for name in ALWAYS_ON_SKILL_NAMES if name in skills]


def build_always_on_skill_system_message() -> str | None:
    active_skills = get_always_on_skills()
    if not active_skills:
        return None

    skill_sections = [
        (
            f"## Skill: {skill.name}\n"
            f"Description: {skill.description or 'No description provided.'}\n\n"
            f"{skill.body.strip()}"
        ).strip()
        for skill in active_skills
    ]
    return (
        "Apply the following repository governance skills as high-priority operating instructions for every request.\n\n"
        + "\n\n".join(skill_sections)
    )
