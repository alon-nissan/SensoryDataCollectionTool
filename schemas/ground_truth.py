#!/usr/bin/env python3
"""Ground truth format for evaluating sensory data extraction quality.

Each GroundTruthRow represents one numeric measurement from a paper, filled in
by a human annotator reading the paper directly (no pipeline pre-filling).
Ground truth CSVs live in data/ground_truth/{paper_id}.csv — one file per paper.

CSV format is single-file, Excel-friendly, no JSON fields. Two component slots
(substance_1/2 + concentration_1/2 + unit_1/2) handle mixtures.
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
    Two component slots (substance_1/2 + concentration_1/2 + unit_1/2) cover
    single substances and binary mixtures. Larger mixtures note extras in the
    notes field.
    """

    paper_id: str
    experiment: str                      # "exp1", "exp2", etc.
    measurement_domain: str              # "sensory" or "psychological"
    panel_label: Optional[str]           # short label, e.g. "exp1_full"
    panel_size: Optional[int]            # n (nullable)
    panel_demographics: Optional[str]    # free text, e.g. "n=30, age=22±3, sex=60% F"
    substance_1: str                     # primary substance name
    concentration_1: Optional[float]     # nullable
    unit_1: Optional[str]               # nullable, e.g. "mM", "% w/v"
    substance_2: Optional[str]          # second component (mixture); blank if not a mixture
    concentration_2: Optional[float]    # nullable
    unit_2: Optional[str]              # nullable
    base_matrix: Optional[str]          # nullable, carrier medium: "distilled water"
    is_control: bool                    # whether this is a control stimulus
    attribute: str                      # attribute as written in paper: "sweetness intensity"
    value: Optional[float]              # the numeric measurement
    value_type: str                     # raw_mean | raw_median | derived_param | threshold | frequency_pct | dominance_rate
    error: Optional[float]             # nullable, SD/SE/CI value if reported
    error_type: Optional[str]          # nullable: sd | se | ci95_lower | ci95_upper
    source_type: Optional[str]         # "table" / "figure" / "text"
    source_location: str               # "Table 2", "Figure 1A", "text p.5"
    notes: Optional[str] = None        # free text, e.g. extra mixture components


# Ordered list of CSV column names (matches dataclass field order)
_CSV_FIELDNAMES: list[str] = [f.name for f in fields(GroundTruthRow)]


# ---------------------------------------------------------------------------
# Demographics formatting helper
# ---------------------------------------------------------------------------

def _format_demographics(attributes_json: str | None) -> str:
    """Format panels.attributes_json as a human-readable flat string.

    The JSON structure is expected to be:
        {
            "demographics": {"n": 30, "age": "22±3", "sex": "60% F"},
            "sensory_traits": {"training": "40h"},
            "recruitment": {...}
        }

    Returns a compact string like "n=30, age=22±3, sex=60% F, training=40h".
    Returns "" if the JSON is null, blank, or unparseable.
    No JSON characters appear in the output.
    """
    if not attributes_json:
        return ""
    try:
        data = json.loads(attributes_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return ""

    if not isinstance(data, dict):
        return ""

    # Flatten the nested sections we care about in display order
    parts: list[str] = []
    for section in ("demographics", "sensory_traits"):
        section_data = data.get(section)
        if not isinstance(section_data, dict):
            continue
        for key, val in section_data.items():
            if val is None or val == "":
                continue
            parts.append(f"{key}={val}")

    return ", ".join(parts)


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


def _to_optional_int(s) -> Optional[int]:
    """Parse a string or number to int, returning None for blank/None."""
    if s is None:
        return None
    s = str(s).strip()
    if s == "":
        return None
    try:
        return int(s)
    except (ValueError, TypeError):
        return None


def _to_bool(s) -> bool:
    """Parse 'TRUE' / 'true' / '1' → True, anything else → False."""
    if isinstance(s, bool):
        return s
    if s is None:
        return False
    return str(s).strip().upper() in ("TRUE", "1", "YES")


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
                    measurement_domain=raw.get("measurement_domain") or "sensory",
                    panel_label=_to_optional_str(raw.get("panel_label")),
                    panel_size=_to_optional_int(raw.get("panel_size")),
                    panel_demographics=_to_optional_str(raw.get("panel_demographics")),
                    substance_1=raw.get("substance_1") or "",
                    concentration_1=_to_optional_float(raw.get("concentration_1")),
                    unit_1=_to_optional_str(raw.get("unit_1")),
                    substance_2=_to_optional_str(raw.get("substance_2")),
                    concentration_2=_to_optional_float(raw.get("concentration_2")),
                    unit_2=_to_optional_str(raw.get("unit_2")),
                    base_matrix=_to_optional_str(raw.get("base_matrix")),
                    is_control=_to_bool(raw.get("is_control")),
                    attribute=raw.get("attribute") or "",
                    value=_to_optional_float(raw.get("value")),
                    value_type=raw.get("value_type") or "",
                    error=_to_optional_float(raw.get("error")),
                    error_type=_to_optional_str(raw.get("error_type")),
                    source_type=_to_optional_str(raw.get("source_type")),
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
    None → empty string; bool → "TRUE" / "FALSE" for Excel-friendliness.
    """
    GROUND_TRUTH_DIR.mkdir(parents=True, exist_ok=True)
    fname = filename or f"{paper_id}.csv"
    path = GROUND_TRUTH_DIR / fname

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDNAMES, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            d = asdict(row)
            serialised = {}
            for k, v in d.items():
                if v is None:
                    serialised[k] = ""
                elif isinstance(v, bool):
                    serialised[k] = "TRUE" if v else "FALSE"
                else:
                    serialised[k] = v
            writer.writerow(serialised)

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

    Each obs dict is expected to come from a LEFT JOIN of observations + panels,
    so it will include: all observations.* columns plus panel_label_raw
    (panels.panel_label), panel_size, and panels_attributes_json.

    Components are spread across the two substance/concentration/unit slots.
    If there are more than 2 components, extra ones are noted in the notes field.
    """
    rows: list[GroundTruthRow] = []

    for obs in observations:
        # Derive short experiment label from experiment_id
        exp_id: str = obs.get("experiment_id") or ""
        prefix = f"{paper_id}__"
        experiment = exp_id[len(prefix):] if exp_id.startswith(prefix) else exp_id

        # Derive short panel label (strip paper_id__ prefix)
        panel_label_raw: Optional[str] = obs.get("panel_label_raw") or None
        if panel_label_raw and panel_label_raw.startswith(prefix):
            panel_label = panel_label_raw[len(prefix):]
        else:
            panel_label = panel_label_raw

        # Panel demographics from attributes_json
        panel_demographics_str = _format_demographics(obs.get("panels_attributes_json"))
        panel_demographics: Optional[str] = panel_demographics_str if panel_demographics_str else None

        # Panel size
        panel_size: Optional[int] = _to_optional_int(obs.get("panel_size"))

        # Parse components_json
        components = obs.get("components_json")
        if isinstance(components, str) and components:
            try:
                components = json.loads(components)
            except (json.JSONDecodeError, TypeError):
                components = None

        # Substance / concentration / unit slots
        substance_1: str = (obs.get("substance_name") or "").lower().strip()
        concentration_1: Optional[float] = None
        unit_1: Optional[str] = None
        substance_2: Optional[str] = None
        concentration_2: Optional[float] = None
        unit_2: Optional[str] = None
        notes: Optional[str] = None

        if components and isinstance(components, list) and len(components) > 0:
            first = components[0]
            substance_1 = (first.get("substance") or substance_1 or "").lower().strip()
            raw_conc = first.get("concentration")
            if raw_conc is not None:
                try:
                    concentration_1 = float(raw_conc)
                except (ValueError, TypeError):
                    concentration_1 = None
            unit_1 = first.get("unit") or None

            if len(components) >= 2:
                second = components[1]
                substance_2 = (second.get("substance") or "").lower().strip() or None
                raw_conc2 = second.get("concentration")
                if raw_conc2 is not None:
                    try:
                        concentration_2 = float(raw_conc2)
                    except (ValueError, TypeError):
                        concentration_2 = None
                unit_2 = second.get("unit") or None

            if len(components) > 2:
                extra_parts = []
                for c in components[2:]:
                    sub = c.get("substance") or "?"
                    conc = c.get("concentration")
                    unit = c.get("unit") or ""
                    extra_parts.append(
                        f"{sub}@{conc} {unit}".strip() if conc is not None else sub
                    )
                notes = "extra components: " + "; ".join(extra_parts)

        rows.append(
            GroundTruthRow(
                paper_id=paper_id,
                experiment=experiment,
                measurement_domain=(obs.get("measurement_domain") or "sensory").lower(),
                panel_label=panel_label,
                panel_size=panel_size,
                panel_demographics=panel_demographics,
                substance_1=substance_1,
                concentration_1=concentration_1,
                unit_1=unit_1,
                substance_2=substance_2,
                concentration_2=concentration_2,
                unit_2=unit_2,
                base_matrix=obs.get("base_matrix") or None,
                is_control=bool(obs.get("is_control") or False),
                attribute=obs.get("attribute_raw") or obs.get("attribute_normalized") or "",
                value=_to_optional_float(obs.get("value")),
                value_type=obs.get("value_type") or "",
                error=_to_optional_float(obs.get("error_value")),
                error_type=obs.get("error_type") or None,
                source_type=obs.get("source_type") or None,
                source_location=obs.get("source_location") or "",
                notes=notes,
            )
        )

    return rows
