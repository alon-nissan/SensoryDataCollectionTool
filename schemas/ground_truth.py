#!/usr/bin/env python3
"""Ground truth format for evaluating sensory data extraction quality.

Each GroundTruthRow represents one numeric measurement from a paper, filled in
by a human annotator reading the paper directly (no pipeline pre-filling).
Ground truth CSVs live in data/ground_truth/{paper_id}.csv — one file per paper.
"""

import csv
import json
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Optional

ROOT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT_DIR))

GROUND_TRUTH_DIR = ROOT_DIR / "data" / "ground_truth"

# Valid values for value_type column (mirrors the pipeline schema)
VALUE_TYPE_VALUES = (
    "raw_mean",
    "raw_median",
    "derived_param",
    "threshold",
    "frequency_pct",
    "dominance_rate",
)


@dataclass
class GroundTruthRow:
    """One numeric measurement from a paper, entered by a human annotator.

    Flat and CSV-serialisable — no nesting, no JSON fields.
    Annotators fill these rows in by reading the paper directly.
    """

    paper_id: str
    experiment: str                    # "exp1", "exp2", etc.
    substance: str                     # lowercase, e.g. "sucrose", "citric_acid"
    concentration: Optional[float]     # nullable
    concentration_unit: Optional[str]  # nullable, unit as written: "M", "mM", "% w/v"
    base_matrix: Optional[str]         # nullable, carrier medium: "distilled water"
    attribute: str                     # attribute as written in paper: "sweetness intensity"
    value: Optional[float]             # the numeric measurement
    value_type: str                    # raw_mean | raw_median | derived_param | threshold | frequency_pct | dominance_rate
    error: Optional[float]             # nullable, SD/SE/CI value if reported
    error_type: Optional[str]          # nullable: sd | se | ci95_lower | ci95_upper
    source_location: str               # "Table 2", "Figure 1A", "text p.5"
    notes: Optional[str] = None        # free text, e.g. "mixture: sucrose + citric_acid"


# Ordered list of CSV column names (matches dataclass field order)
_CSV_FIELDNAMES: list[str] = [f.name for f in fields(GroundTruthRow)]


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------

def _to_optional_float(s) -> Optional[float]:
    """Parse a string or number to float, returning None for blank/None."""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _to_optional_str(s) -> Optional[str]:
    """Return stripped string or None for blank/None input."""
    if s is None:
        return None
    s = str(s).strip()
    return s if s else None


# ---------------------------------------------------------------------------
# Load / save
# ---------------------------------------------------------------------------

def load_ground_truth(paper_id: str) -> list[GroundTruthRow]:
    """Load ground truth rows from data/ground_truth/{paper_id}.csv.

    Every row in the file is included — the annotator controls the contents
    by adding or removing rows directly in the CSV.
    """
    path = GROUND_TRUTH_DIR / f"{paper_id}.csv"
    if not path.exists():
        return []

    rows: list[GroundTruthRow] = []
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for raw in reader:
            rows.append(
                GroundTruthRow(
                    paper_id=raw.get("paper_id") or paper_id,
                    experiment=raw.get("experiment") or "",
                    substance=raw.get("substance") or "",
                    concentration=_to_optional_float(raw.get("concentration")),
                    concentration_unit=_to_optional_str(raw.get("concentration_unit")),
                    base_matrix=_to_optional_str(raw.get("base_matrix")),
                    attribute=raw.get("attribute") or "",
                    value=_to_optional_float(raw.get("value")),
                    value_type=raw.get("value_type") or "",
                    error=_to_optional_float(raw.get("error")),
                    error_type=_to_optional_str(raw.get("error_type")),
                    source_location=raw.get("source_location") or "",
                    notes=_to_optional_str(raw.get("notes")),
                )
            )

    return rows


def save_ground_truth(
    paper_id: str,
    rows: list[GroundTruthRow],
    filename: str | None = None,
) -> Path:
    """Save ground truth rows to data/ground_truth/{filename}.csv.

    Defaults to {paper_id}.csv when filename is not provided.
    """
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    fname = filename or f"{paper_id}.csv"
    path = GROUND_TRUTH_DIR / fname

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            d = asdict(row)
            # Write None as empty string so the CSV stays Excel-friendly
            writer.writerow({k: ("" if v is None else v) for k, v in d.items()})

    return path


def generate_blank_template(paper_id: str) -> Path:
    """Write a blank CSV template with column headers only.

    The annotator opens this file and adds one row per numeric measurement
    found in the paper, reading the paper directly without any pipeline input.
    Saved as data/ground_truth/{paper_id}_BLANK.csv.
    """
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    path = GROUND_TRUTH_DIR / f"{paper_id}_BLANK.csv"

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES)
        writer.writeheader()

    return path


# ---------------------------------------------------------------------------
# Conversion from pipeline observations
# ---------------------------------------------------------------------------

def observations_to_ground_truth(
    observations: list[dict],
    paper_id: str,
) -> list[GroundTruthRow]:
    """Convert pipeline observation dicts (from the DB) to GroundTruthRow objects.

    Used internally by evaluate.py to put pipeline output into the same flat
    format as ground truth rows so they can be compared.

    For mixtures (multiple components in components_json), the first component
    is treated as the primary substance and full composition is recorded in notes.
    The experiment field is extracted from experiment_id by stripping the paper_id
    prefix (e.g. "paper123__exp1" → "exp1").
    """
    rows: list[GroundTruthRow] = []

    for obs in observations:
        # Derive short experiment label from experiment_id
        exp_id: str = obs.get("experiment_id") or ""
        experiment = exp_id.split("__", 1)[-1] if "__" in exp_id else exp_id

        # Parse components_json for concentration details
        components = obs.get("components_json")
        if isinstance(components, str) and components:
            try:
                components = json.loads(components)
            except (json.JSONDecodeError, TypeError):
                components = None

        concentration: Optional[float] = None
        concentration_unit: Optional[str] = None
        notes: Optional[str] = None

        if components and isinstance(components, list) and len(components) > 0:
            first = components[0]
            raw_conc = first.get("concentration")
            if raw_conc is not None:
                try:
                    concentration = float(raw_conc)
                except (ValueError, TypeError):
                    concentration = None
            concentration_unit = first.get("unit") or None

            # Record mixture composition in notes
            if len(components) > 1:
                parts = []
                for c in components:
                    sub = c.get("substance") or "?"
                    conc = c.get("concentration")
                    unit = c.get("unit") or ""
                    parts.append(f"{sub} {conc} {unit}".strip() if conc is not None else sub)
                notes = "mixture: " + " + ".join(parts)

        rows.append(
            GroundTruthRow(
                paper_id=paper_id,
                experiment=experiment,
                substance=(obs.get("substance_name") or "").lower().strip(),
                concentration=concentration,
                concentration_unit=concentration_unit,
                base_matrix=obs.get("base_matrix") or None,
                attribute=obs.get("attribute_raw") or obs.get("attribute_normalized") or "",
                value=_to_optional_float(obs.get("value")),
                value_type=obs.get("value_type") or "",
                error=_to_optional_float(obs.get("error_value")),
                error_type=obs.get("error_type") or None,
                source_location=obs.get("source_location") or "",
                notes=notes,
            )
        )

    return rows
