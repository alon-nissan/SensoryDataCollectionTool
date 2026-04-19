#!/usr/bin/env python3
"""Substance resolution: deterministic alias lookup with LLM fallback."""

import json
import sys
from pathlib import Path

import yaml
from rich.console import Console

ROOT_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT_DIR))

from scripts.db.db import (
    get_db, resolve_substance_by_alias, resolve_substance_by_name,
    resolve_substance_by_cas, insert_substance, add_substance_alias,
)
from scripts.llm_extract import LLMClient

console = Console()


def resolve_substance(conn, name: str, cas_number: str = None,
                      llm: LLMClient = None, config: dict = None) -> int:
    """Resolve a substance name to a substance_id using the 6-step workflow.

    Steps:
        1. Exact alias lookup
        2. Exact normalized_name lookup
        3. CAS number lookup
        4. LLM-based matching (if LLM provided)
        5. Create new substance
        6. Add alias

    Args:
        conn: Database connection
        name: Substance name as found in the paper
        cas_number: CAS number if known
        llm: LLMClient for LLM-based matching (optional)
        config: Config dict (optional)

    Returns:
        substance_id (int)
    """
    clean_name = name.lower().strip()

    # Step 1: Exact alias lookup
    substance_id = resolve_substance_by_alias(conn, clean_name)
    if substance_id:
        return substance_id

    # Step 2: Exact normalized_name lookup
    substance_id = resolve_substance_by_name(conn, clean_name)
    if substance_id:
        # Add this name as a new alias for faster future lookups
        add_substance_alias(conn, clean_name, substance_id)
        return substance_id

    # Step 3: CAS number lookup
    if cas_number:
        substance_id = resolve_substance_by_cas(conn, cas_number)
        if substance_id:
            add_substance_alias(conn, clean_name, substance_id)
            return substance_id

    # Step 4: LLM-based matching
    if llm:
        existing = _get_existing_substances(conn)
        if existing:
            substance_id = _llm_match_substance(
                llm, clean_name, cas_number, existing, config
            )
            if substance_id:
                add_substance_alias(conn, clean_name, substance_id)
                return substance_id

    # Step 5: Create new substance
    normalized = _normalize_substance_name(clean_name)

    # Check if normalized form already exists (handles minor variations)
    substance_id = resolve_substance_by_name(conn, normalized)
    if substance_id:
        add_substance_alias(conn, clean_name, substance_id)
        return substance_id

    new_id = insert_substance(conn, {
        "normalized_name": normalized,
        "cas_number": cas_number,
        "category": _guess_category(normalized),
    })

    # Step 6: Add alias
    add_substance_alias(conn, clean_name, new_id)
    if clean_name != normalized:
        add_substance_alias(conn, normalized, new_id)

    return new_id


def create_stimulus_for_paper(conn, paper_id: str, substance_id: int,
                               original_name: str, supplier: str = None,
                               purity: str = None, form: str = None,
                               details: dict = None, seq: int = 1) -> str:
    """Create a paper-specific stimulus entry linked to a substance.

    Returns:
        stimulus_id string
    """
    # Get normalized name for ID generation
    row = conn.execute(
        "SELECT normalized_name FROM substances WHERE substance_id = ?",
        (substance_id,)
    ).fetchone()
    norm_name = row["normalized_name"] if row else "unknown"

    stimulus_id = f"{paper_id}__{norm_name}_{seq}"

    insert_stimulus(conn, {
        "stimulus_id": stimulus_id,
        "paper_id": paper_id,
        "substance_id": substance_id,
        "original_name": original_name,
        "supplier": supplier,
        "purity": purity,
        "form": form,
        "details_json": details,
    })

    return stimulus_id


def _get_existing_substances(conn) -> list[dict]:
    """Get all existing substances for LLM matching context."""
    rows = conn.execute(
        "SELECT substance_id, normalized_name, cas_number, category FROM substances"
    ).fetchall()
    return [dict(r) for r in rows]


def _llm_match_substance(llm: LLMClient, name: str, cas: str | None,
                          existing: list[dict], config: dict = None) -> int | None:
    """Ask LLM to match a substance name against existing substances."""
    model = llm.get_model("agent2")  # Use agent2 model for structuring tasks

    existing_summary = json.dumps(existing[:100], indent=2)  # Limit context

    prompt = (
        f"A sensory science paper mentions a substance called '{name}'"
        + (f" (CAS: {cas})" if cas else "") +
        f".\n\nHere are the existing substances in our database:\n{existing_summary}\n\n"
        f"Is this the same as any existing substance? Consider alternate names, "
        f"abbreviations, and trade names.\n\n"
        f"Return JSON: {{\"match_found\": true/false, \"substance_id\": <id or null>, "
        f"\"confidence\": \"high\"/\"medium\"/\"low\", \"reasoning\": \"...\"}}"
    )

    try:
        result = llm.extract_json(prompt, model=model)
        if result.get("match_found") and result.get("confidence") in ("high", "medium"):
            return result.get("substance_id")
    except Exception:
        pass

    return None


def _normalize_substance_name(name: str) -> str:
    """Normalize a substance name to canonical form."""
    import re

    # Lowercase and strip
    name = name.lower().strip()

    # Common substitutions
    replacements = {
        "reb a": "rebaudioside_a",
        "rebiana": "rebaudioside_a",
        "rebaudioside a": "rebaudioside_a",
        "reb m": "rebaudioside_m",
        "ace-k": "acesulfame_k",
        "acesulfame k": "acesulfame_k",
        "acesulfame potassium": "acesulfame_k",
        "aspartame": "aspartame",
        "neotame": "neotame",
        "sucralose": "sucralose",
        "saccharin": "saccharin",
        "stevia": "stevioside",
        "monk fruit": "mogroside_v",
        "luo han guo": "mogroside_v",
        "kcl": "potassium_chloride",
        "nacl": "sodium_chloride",
        "msg": "monosodium_glutamate",
    }

    if name in replacements:
        return replacements[name]

    # General normalization: spaces → underscores, remove special chars
    name = re.sub(r'[^\w\s]', '', name)
    name = re.sub(r'\s+', '_', name)
    return name


def _guess_category(normalized_name: str) -> str:
    """Guess substance category from name. Returns best guess or 'other'."""
    categories = {
        "saccharide": ["sucrose", "glucose", "fructose", "lactose", "maltose",
                        "trehalose", "galactose"],
        "polyol": ["sorbitol", "xylitol", "erythritol", "mannitol", "maltitol",
                    "isomalt", "lactitol"],
        "non_nutritive_synthetic": ["sucralose", "aspartame", "acesulfame_k",
                                      "saccharin", "neotame", "cyclamate",
                                      "advantame"],
        "non_nutritive_natural": ["stevioside", "rebaudioside_a", "rebaudioside_m",
                                    "mogroside_v", "thaumatin", "brazzein",
                                    "glycyrrhizin"],
        "salt": ["sodium_chloride", "potassium_chloride", "calcium_chloride",
                  "magnesium_chloride"],
        "acid": ["citric_acid", "malic_acid", "tartaric_acid", "acetic_acid",
                  "hydrochloric_acid", "phosphoric_acid", "lactic_acid"],
        "bitter": ["caffeine", "quinine", "denatonium", "propylthiouracil",
                    "phenylthiocarbamide"],
        "umami": ["monosodium_glutamate", "inosine_monophosphate",
                   "guanosine_monophosphate"],
    }

    for category, names in categories.items():
        if normalized_name in names:
            return category

    return "other"


def seed_common_substances(config: dict = None):
    """Seed the database with common sensory science substances."""
    if config is None:
        with open(ROOT_DIR / "config.yaml") as f:
            config = yaml.safe_load(f)

    conn = get_db(config)

    # Common sweeteners with CAS numbers
    substances = [
        {"normalized_name": "sucrose", "cas_number": "57-50-1", "molecular_weight": 342.3, "category": "saccharide"},
        {"normalized_name": "glucose", "cas_number": "50-99-7", "molecular_weight": 180.16, "category": "saccharide"},
        {"normalized_name": "fructose", "cas_number": "57-48-7", "molecular_weight": 180.16, "category": "saccharide"},
        {"normalized_name": "lactose", "cas_number": "63-42-3", "molecular_weight": 342.3, "category": "saccharide"},
        {"normalized_name": "sucralose", "cas_number": "56038-13-2", "molecular_weight": 397.64, "category": "non_nutritive_synthetic"},
        {"normalized_name": "aspartame", "cas_number": "22839-47-0", "molecular_weight": 294.3, "category": "non_nutritive_synthetic"},
        {"normalized_name": "acesulfame_k", "cas_number": "55589-62-3", "molecular_weight": 201.24, "category": "non_nutritive_synthetic"},
        {"normalized_name": "saccharin", "cas_number": "81-07-2", "molecular_weight": 183.18, "category": "non_nutritive_synthetic"},
        {"normalized_name": "stevioside", "cas_number": "57817-89-7", "molecular_weight": 804.87, "category": "non_nutritive_natural"},
        {"normalized_name": "rebaudioside_a", "cas_number": "58543-16-1", "molecular_weight": 967.01, "category": "non_nutritive_natural"},
        {"normalized_name": "erythritol", "cas_number": "149-32-6", "molecular_weight": 122.12, "category": "polyol"},
        {"normalized_name": "xylitol", "cas_number": "87-99-0", "molecular_weight": 152.15, "category": "polyol"},
        {"normalized_name": "sorbitol", "cas_number": "50-70-4", "molecular_weight": 182.17, "category": "polyol"},
        {"normalized_name": "sodium_chloride", "cas_number": "7647-14-5", "molecular_weight": 58.44, "category": "salt"},
        {"normalized_name": "potassium_chloride", "cas_number": "7447-40-7", "molecular_weight": 74.55, "category": "salt"},
        {"normalized_name": "citric_acid", "cas_number": "77-92-9", "molecular_weight": 192.12, "category": "acid"},
        {"normalized_name": "caffeine", "cas_number": "58-08-2", "molecular_weight": 194.19, "category": "bitter"},
        {"normalized_name": "quinine", "cas_number": "130-95-0", "molecular_weight": 324.42, "category": "bitter"},
        {"normalized_name": "monosodium_glutamate", "cas_number": "142-47-2", "molecular_weight": 169.11, "category": "umami"},
    ]

    # Common aliases
    aliases_map = {
        "sucrose": ["sugar", "table sugar", "cane sugar", "beet sugar"],
        "glucose": ["dextrose", "d-glucose", "grape sugar", "blood sugar"],
        "fructose": ["fruit sugar", "d-fructose", "levulose"],
        "sucralose": ["splenda"],
        "aspartame": ["equal", "nutrasweet"],
        "acesulfame_k": ["ace-k", "acesulfame potassium", "sunett"],
        "saccharin": ["sweet'n low", "sodium saccharin"],
        "stevioside": ["stevia", "stevia extract"],
        "rebaudioside_a": ["reb a", "rebiana", "rebaudioside a"],
        "sodium_chloride": ["nacl", "salt", "table salt", "common salt"],
        "potassium_chloride": ["kcl"],
        "citric_acid": ["citrate"],
        "caffeine": ["1,3,7-trimethylxanthine"],
        "monosodium_glutamate": ["msg", "sodium glutamate"],
    }

    for sub_data in substances:
        name = sub_data["normalized_name"]
        # Check if already exists
        existing = resolve_substance_by_name(conn, name)
        if existing:
            continue

        sub_id = insert_substance(conn, sub_data)
        add_substance_alias(conn, name, sub_id)

        # Add all aliases
        for alias in aliases_map.get(name, []):
            add_substance_alias(conn, alias, sub_id)

    conn.close()
    console.print(f"[green]✓ Seeded {len(substances)} common substances with aliases[/green]")


if __name__ == "__main__":
    seed_common_substances()
