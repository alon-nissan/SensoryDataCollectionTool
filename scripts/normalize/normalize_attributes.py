#!/usr/bin/env python3
"""Normalize sensory attribute names to a controlled vocabulary."""

import json
import sys
from pathlib import Path

import yaml

ROOT_DIR = Path(__file__).resolve().parent.parent


def load_config():
    with open(ROOT_DIR / "config.yaml") as f:
        return yaml.safe_load(f)


def load_vocabulary() -> dict:
    """Load the attribute vocabulary mapping."""
    config = load_config()
    vocab_path = ROOT_DIR / config["paths"]["vocabulary_file"]
    with open(vocab_path) as f:
        return json.load(f)


def save_vocabulary(vocab: dict):
    """Save updated vocabulary mapping."""
    config = load_config()
    vocab_path = ROOT_DIR / config["paths"]["vocabulary_file"]
    with open(vocab_path, "w") as f:
        json.dump(vocab, f, indent=2, ensure_ascii=False)


def normalize_attributes(paper: dict, interactive: bool = True) -> tuple[dict, list[str]]:
    """Normalize attribute names in a paper JSON.

    Args:
        paper: Complete paper JSON dict
        interactive: If True, prompt user for unknown attribute mappings

    Returns:
        Tuple of (modified paper dict, list of new mappings added)
    """
    vocab = load_vocabulary()
    mappings = vocab.get("mappings", {})
    unmapped = []
    new_mappings = []

    # Collect all attribute names from sensory_data sections
    raw_attributes = set()
    for exp in paper.get("experiments", []):
        sensory_data = exp.get("sensory_data", {})
        _collect_attribute_names(sensory_data, raw_attributes)

    # Normalize each attribute
    for raw_name in sorted(raw_attributes):
        normalized = _normalize_single(raw_name, mappings)
        if normalized is None:
            if interactive:
                normalized = _prompt_user_mapping(raw_name, vocab)
                if normalized:
                    mappings[raw_name.lower()] = normalized
                    new_mappings.append(f"{raw_name} → {normalized}")
                else:
                    unmapped.append(raw_name)
            else:
                unmapped.append(raw_name)

    # Apply normalizations to the paper JSON
    for exp in paper.get("experiments", []):
        if "sensory_data" in exp:
            exp["sensory_data"] = _apply_normalization(exp["sensory_data"], mappings)

    # Update vocabulary
    if new_mappings:
        vocab["mappings"] = mappings
        vocab["unmapped"] = list(set(vocab.get("unmapped", []) + unmapped))
        save_vocabulary(vocab)
        print(f"  📝 Added {len(new_mappings)} new vocabulary mappings")

    if unmapped:
        print(f"  ⚠ {len(unmapped)} unmapped attributes: {unmapped}")

    return paper, new_mappings


def _collect_attribute_names(data: dict | list, names: set, depth: int = 0):
    """Recursively collect attribute-like key names from sensory data."""
    if depth > 5:
        return
    if isinstance(data, dict):
        for key, value in data.items():
            # Skip structural keys
            if key in ("mean", "sd", "sem", "ci_95", "n", "data_source", "unit",
                        "concentrations", "means", "sems", "sds", "scale",
                        "notes", "source", "error_type"):
                continue
            # If the value is a dict with statistical keys, this key is likely an attribute
            if isinstance(value, dict) and any(k in value for k in ("mean", "sd", "sem", "means")):
                names.add(key)
            elif isinstance(value, (dict, list)):
                _collect_attribute_names(value, names, depth + 1)
    elif isinstance(data, list):
        for item in data:
            _collect_attribute_names(item, names, depth + 1)


def _normalize_single(raw_name: str, mappings: dict) -> str | None:
    """Try to normalize a single attribute name."""
    raw_lower = raw_name.lower().strip()

    # Direct mapping
    if raw_lower in mappings:
        return mappings[raw_lower]

    # Try with common suffixes/prefixes removed
    for suffix in [" intensity", " perception", " rating", " score"]:
        stripped = raw_lower.replace(suffix, "").strip()
        if stripped in mappings:
            return mappings[stripped]

    # Try adding common suffixes
    for suffix in ["ness", "ity"]:
        if raw_lower + suffix in mappings:
            return mappings[raw_lower + suffix]

    return None


def _apply_normalization(data: dict, mappings: dict) -> dict:
    """Apply attribute normalization to a sensory_data dict."""
    if not isinstance(data, dict):
        return data

    normalized = {}
    for key, value in data.items():
        norm_key = _normalize_single(key, mappings)
        new_key = norm_key if norm_key else key

        if isinstance(value, dict):
            normalized[new_key] = _apply_normalization(value, mappings)
        else:
            normalized[new_key] = value

    return normalized


def _prompt_user_mapping(raw_name: str, vocab: dict) -> str | None:
    """Interactively ask user to map an unknown attribute."""
    categories = vocab.get("categories", {})

    print(f"\n  Unknown attribute: '{raw_name}'")
    print(f"  Known categories:")
    all_known = []
    for cat, attrs in categories.items():
        print(f"    {cat}: {', '.join(attrs)}")
        all_known.extend(attrs)

    response = input(f"  Map '{raw_name}' to (enter normalized name, or 'skip'): ").strip()

    if response.lower() == "skip" or not response:
        return None

    return response.lower()


def main():
    if len(sys.argv) < 2:
        print("Usage: python normalize_attributes.py <json_path> [--non-interactive]")
        sys.exit(1)

    json_path = Path(sys.argv[1])
    interactive = "--non-interactive" not in sys.argv

    with open(json_path) as f:
        paper = json.load(f)

    paper, new_mappings = normalize_attributes(paper, interactive=interactive)

    # Save normalized version
    with open(json_path, "w") as f:
        json.dump(paper, f, indent=2, ensure_ascii=False)

    print(f"\n✅ Normalized attributes in {json_path.name}")
    if new_mappings:
        for m in new_mappings:
            print(f"  + {m}")


if __name__ == "__main__":
    main()
