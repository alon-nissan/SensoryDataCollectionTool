#!/usr/bin/env python3
"""Utility for generating deterministic paper IDs from DOIs."""

import re


def doi_to_paper_id(doi: str) -> str:
    """Convert a DOI to a filesystem/DB-safe paper_id.
    
    Examples:
        10.1016/j.foodqual.2018.01.001 → 10_1016_j_foodqual_2018_01_001
        10.3390/nu10111632 → 10_3390_nu10111632
    """
    # Strip URL prefix if present
    doi = doi.strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    
    # Replace non-alphanumeric chars with underscores, collapse multiples
    paper_id = re.sub(r'[^a-zA-Z0-9]', '_', doi)
    paper_id = re.sub(r'_+', '_', paper_id)
    paper_id = paper_id.strip('_').lower()
    return paper_id


def paper_id_from_filename(filename: str) -> str:
    """Generate a paper_id from a filename when no DOI is available.
    
    Examples:
        smith2019.html → smith2019
        Green_et_al_2010.pdf → green_et_al_2010
    """
    from pathlib import Path
    stem = Path(filename).stem
    # Normalize: lowercase, replace spaces/special chars
    paper_id = re.sub(r'[^a-zA-Z0-9]', '_', stem)
    paper_id = re.sub(r'_+', '_', paper_id)
    return paper_id.strip('_').lower()
