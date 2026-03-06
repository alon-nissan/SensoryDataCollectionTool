# Sensory Data Extraction Project — Plan v2

*Updated after reviewing Wee et al. (2018) and Ben Abu et al. (2018)*

---

## 1. Architecture Overview

### Data Storage: JSON-per-Paper + Thin SQLite Index

After reviewing real papers, we abandoned the rigid relational schema in favor of a flexible two-layer system:

**Layer 1 — JSON files (one per paper):** Rich, human-readable documents capturing everything extracted from the paper. Each JSON follows a consistent top-level skeleton but flexes internally to match the paper's unique data structure. This is the primary data store and the artifact you validate manually.

**Layer 2 — SQLite index (one row per paper):** A thin searchable catalog with ~20 columns extracted from the JSON. Used for filtering and discovery ("show me all gLMS studies on sweeteners published after 2015"). Built programmatically from the JSON files.

### SQLite Index Fields

| Field | Type | Source |
|-------|------|--------|
| study_id | TEXT PK | JSON: study_metadata.study_id |
| doi | TEXT | JSON: study_metadata.doi |
| title | TEXT | JSON: study_metadata.title |
| year | INTEGER | JSON: study_metadata.year |
| journal | TEXT | JSON: study_metadata.journal |
| country | TEXT | JSON: study_metadata.country |
| food_category | TEXT | JSON: study_metadata.food_category |
| num_experiments | INTEGER | count of JSON: experiments[] |
| sensory_methods | TEXT | comma-separated list (e.g., "gLMS, 9-point Likert") |
| scale_types | TEXT | comma-separated list |
| attributes_measured | TEXT | comma-separated list |
| total_stimuli | INTEGER | count across all experiments |
| total_panelists | INTEGER | max panel size across experiments |
| panel_types | TEXT | comma-separated (trained, untrained, consumer) |
| has_dose_response | BOOLEAN | true if dose-response data present |
| has_mixture_stimuli | BOOLEAN | true if any stimuli are mixtures |
| has_figure_data | BOOLEAN | true if data was extracted from figures |
| num_data_gaps | INTEGER | count of null/missing values flagged |
| extraction_date | TEXT | when extraction was performed |
| validation_status | TEXT | pending / validated / issues_found |

### JSON Skeleton (Consistent Across All Papers)

```
{
  "study_metadata": { ... },          // Always present, same fields
  "experiments": [                     // Array of 1+ experiments
    {
      "experiment_id": "...",
      "experiment_description": "...",
      "panel": { ... },               // Always present, flexible internals
      "session_design": { ... },       // Always present, flexible internals
      "scale": { ... },               // Always present, flexible internals
      "stimuli": [ ... ],             // Array of stimulus objects with composition KV
      "sensory_data": { ... },        // FLEXIBLE: adapts to paper's data structure
      "derived_metrics": { ... },     // OPTIONAL: potency, growth rates, model params
      "statistical_outputs": { ... }  // FLEXIBLE: ANOVA, post-hoc, etc.
    }
  ],
  "cross_experiment_data": { ... },   // OPTIONAL: data spanning multiple experiments
  "figure_inventory": [ ... ],        // Catalog of all figures with extraction status
  "extraction_metadata": { ... }      // Provenance, confidence, data gaps
}
```

---

## 2. Data Acquisition: DOI/HTML Publisher Access

### Approach

Instead of parsing PDFs, we access articles via publisher APIs and web scraping to obtain structured HTML/XML. This gives us clean, pre-structured tables and properly ordered text, eliminating the #1 source of extraction errors.

### Publisher-Specific Access Methods

| Publisher | Access Method | Key Required | Notes |
|-----------|-------------|--------------|-------|
| MDPI (e.g., Nutrients) | Direct HTML scraping | No (open access) | Clean HTML, tables as `<table>` elements |
| Elsevier (e.g., Food Chemistry) | Article Retrieval API | Yes (institutional) | Returns XML with rich semantic markup |
| Springer Nature | SpringerLink API | Yes (institutional) | Returns XML/HTML |
| Wiley | Wiley TDM API | Yes (institutional) | Returns XML |
| Taylor & Francis | Web scraping | Institutional IP | HTML behind paywall |
| Open access (any) | Direct HTML scraping | No | Quality varies by publisher |

### Setup Steps

1. Register for API keys using university email:
   - Elsevier: https://dev.elsevier.com/ (create account, request API key)
   - Springer: https://dev.springernature.com/ (register for API key)
   - Wiley: https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining (request TDM token)
2. Verify institutional IP/VPN access for publishers without APIs
3. Install Python packages: `requests`, `beautifulsoup4`, `lxml`
4. Build publisher-specific HTML/XML parsers (one per publisher)

### What This Approach Handles Well

- Table extraction: HTML `<table>` elements parse perfectly into structured data
- Section identification: HTML headings let you target Methods, Results, etc.
- Inline text: Clean paragraphs with proper unicode (no PDF encoding issues)
- Cross-references: links between figures, tables, and text are preserved

### What Still Requires Figure Extraction (VLM)

Regardless of HTML vs. PDF access, figure data extraction requires sending images to a vision-capable LLM. This applies to:
- Dose-response curves (Wee: Figure 1 — the only source of raw sensory means)
- Radar/spider plots (Ben Abu: Figure 2 — raw score profiles)
- Bar charts with error bars (Ben Abu: Figures 4, 5, 6 — relative perception values)
- Box plots (Ben Abu: Figure 3 — sweetness distributions)
- PCA biplots (common in other sensory papers)

Figure images are downloaded from the HTML `<img>` tags alongside their captions.

### PDF as Fallback

Keep the PDF pipeline (Marker/Docling) ready for:
- Papers from publishers without APIs
- Supplementary materials (often PDF-only)
- Older papers without HTML versions
- Validation: compare PDF extraction vs. HTML extraction for quality assessment

---

## 3. LLM Strategy

### Primary: Claude Sonnet 4.5 API

- Metadata extraction (study, experiment, panel, session design)
- Table data extraction from HTML tables
- Inline text numerical data extraction
- Attribute normalization to controlled vocabulary
- Statistical output extraction

### Secondary: Claude Opus (for hard tasks)

- Figure data extraction (sending figure images + captions)
- Ambiguous or complex multi-experiment papers
- Validation passes on Sonnet extractions

### Prompt Architecture

Five core prompts, each producing a section of the JSON:

| Prompt | Input | Output |
|--------|-------|--------|
| A: Study Metadata | Full article text (or abstract + header) | `study_metadata` section |
| B: Experiment Design | Methods section | `experiments[].panel`, `session_design`, `scale` |
| C: Stimuli | Methods + Results sections | `experiments[].stimuli[]` with compositions |
| D: Sensory Data + Derived Metrics | Results section + tables | `sensory_data`, `derived_metrics` |
| E: Figure Extraction | Figure images + captions | Data points, values, confidence ratings |

Each prompt uses the two gold-standard JSONs (Wee and Ben Abu) as few-shot examples.

### Extraction Workflow Per Paper

```
1. Resolve DOI → publisher
2. Fetch HTML/XML via appropriate method
3. Parse HTML → extract sections, tables, figure URLs
4. Download figure images
5. Run Prompt A → study_metadata
6. Run Prompt B → experiment design for each experiment
7. Run Prompt C → stimuli for each experiment
8. Run Prompt D → sensory data + derived metrics (using parsed tables as input)
9. Run Prompt E → figure data (for each figure needing extraction)
10. Assemble complete JSON
11. Run attribute normalization pass
12. Generate SQLite index row
13. Flag data gaps and low-confidence extractions
14. Manual validation
```

---

## 4. Research Questions & Analysis Methods

### Tier 1: Descriptive / Meta-Analytic (achievable with 10-50 papers)

**Q1. Cross-study sweetener benchmarking.** How consistent are sweetness potency and dose-response parameters across labs, scales, and panels? Quantify inter-study variance for common sweeteners like sucrose, aspartame, sucralose.
- *Method:* Random-effects meta-analysis; forest plots; I² heterogeneity.
- *Tools:* `statsmodels` mixed-effects models, `forestplot` package.

**Q2. Methodological landscape.** What scales, panel sizes, training protocols, and statistical methods are most common? How do data reporting practices vary?
- *Method:* Descriptive statistics, frequency analysis, cross-tabulation.
- *Tools:* `pandas`, `matplotlib`/`seaborn` for visualization.

**Q3. Data accessibility audit.** What fraction of published sensory data is in tables vs. figures vs. inline text? How much data is lost if you skip figure extraction?
- *Method:* Count data sources from `extraction_metadata.data_gaps` and `figure_inventory` across papers.
- *Tools:* Simple `pandas` aggregation.

### Tier 2: Predictive Modeling (achievable with 50-200 papers)

**Q4. Sweetness prediction from physicochemical properties.** Given a sweetener's molecular weight, caloric density, glycaemic index, etc., predict its dose-response curve or sweetness potency relative to sucrose.
- *Method:* Gradient-boosted regression (XGBoost/LightGBM); random forest; elastic net. Start with interpretable models for scientific insight.
- *Tools:* `scikit-learn`, `xgboost`, `shap` for feature importance.

**Q5. Taste interaction prediction.** Given compound A at concentration X + compound B at concentration Y, predict perceived bitterness, saltiness, and sweetness of the mixture.
- *Method:* Multi-output regression; compare against classical mixture models (equiratio model). Neural networks if dataset is large enough.
- *Tools:* `scikit-learn` multi-output, `pytorch` for neural approaches.

**Q6. Scale translation.** Learn mapping functions between gLMS, 9-point Likert, VAS, and other scales to normalize data to a common perceptual space.
- *Method:* Paired data from papers that use multiple scales; calibration regression; Bayesian inference for uncertainty.
- *Tools:* `scipy.optimize` for curve fitting, `pymc` for Bayesian models.

### Tier 3: Advanced (200+ papers)

**Q7. Panel reliability prediction.** Predict inter-study variance from methodological features (panel size, training, scale type, presentation order).
- *Method:* Meta-regression with study-level moderators; variance modeling.
- *Tools:* `statsmodels`, `brms` (R) for hierarchical Bayesian models.

**Q8. Sensory space mapping.** Map the full "sensory space" of taste compounds using extracted profiles across many studies.
- *Method:* PCA, UMAP, t-SNE on extracted attribute profiles; clustering.
- *Tools:* `scikit-learn`, `umap-learn`, `plotly` for interactive visualization.

**Q9. Literature gap identification.** Which sweetener/concentration/method combinations have NOT been studied? Where should the field focus next?
- *Method:* Coverage analysis across the stimulus × attribute × method space.
- *Tools:* Custom analysis on the SQLite index.

---

## 5. Working Plan — 10-Paper Pilot (Revised)

### Phase 0: Infrastructure Setup (Days 1-3)

- [ ] Set up Python environment:
  - `anthropic` (Claude API), `requests`, `beautifulsoup4`, `lxml`
  - `pandas`, `sqlite3` (built-in), `json`
  - `Pillow` (image handling for figures)
- [ ] Register for publisher API keys:
  - Elsevier Developer Portal (priority — Ben Abu paper is Elsevier)
  - Springer Nature API (if any of your 10 papers are Springer)
  - Test API access from your university network/VPN
- [ ] Catalog your 10 papers: DOI, publisher, open access status, expected access method
- [ ] Create project directory structure:
  ```
  sensory-extraction/
  ├── data/
  │   ├── html/              # Raw HTML/XML from publishers
  │   ├── figures/            # Downloaded figure images
  │   ├── extractions/        # JSON files (one per paper)
  │   ├── gold_standard/      # Manually validated JSONs
  │   └── sensory_index.db    # SQLite index
  ├── prompts/                # LLM prompt templates (A through E)
  ├── parsers/                # Publisher-specific HTML parsers
  │   ├── mdpi_parser.py
  │   ├── elsevier_parser.py
  │   └── generic_parser.py
  ├── scripts/
  │   ├── fetch_article.py    # DOI → HTML/XML fetcher
  │   ├── extract_figures.py  # Download figure images from HTML
  │   ├── run_extraction.py   # Orchestrate LLM extraction pipeline
  │   ├── build_index.py      # JSON → SQLite index builder
  │   └── validate.py         # Comparison and accuracy tools
  ├── analysis/               # Notebooks for research questions
  ├── vocabulary/
  │   └── attribute_map.json  # Raw term → normalized term mappings
  ├── requirements.txt
  └── README.md
  ```
- [ ] Create SQLite database with index schema
- [ ] Set up the two gold-standard JSONs (Wee and Ben Abu) as validated examples

**Deliverables:** Working environment, API access confirmed, project structure, gold standards in place.

### Phase 1: Publisher Parsers + Article Fetching (Days 4-7)

- [ ] Build MDPI HTML parser:
  - Fetch article HTML from mdpi.com using DOI
  - Extract: sections (intro, methods, results, discussion), tables as structured data, figure image URLs + captions, inline references
  - Test on Wee et al. — verify all 4 tables parse correctly
- [ ] Build Elsevier parser:
  - Authenticate with API key
  - Fetch article XML/HTML from ScienceDirect API
  - Extract same components as MDPI parser
  - Test on Ben Abu et al. — verify section extraction works
- [ ] Build generic fallback parser for other publishers
- [ ] Download all figure images for both test papers
- [ ] Compare parsed HTML tables against the PDF versions — document any differences

**Deliverables:** Working parsers for 2+ publishers, clean structured text + tables + figures for test papers.

### Phase 2: LLM Prompt Engineering (Days 8-14)

- [ ] Write Prompt A (study metadata):
  - Input: article header, abstract, author info
  - Output: `study_metadata` JSON section
  - Test on both papers, compare against gold standard
- [ ] Write Prompt B (experiment design):
  - Input: Methods section text
  - Output: `panel`, `session_design`, `scale` JSON sections
  - Challenge: Ben Abu has 3 experiments — prompt must detect and separate them
- [ ] Write Prompt C (stimuli):
  - Input: Methods + Materials sections, concentration tables
  - Output: `stimuli[]` array with `composition` key-value pairs
  - Challenge: Wee has 16 stimuli with properties table; Ben Abu has mixture stimuli
- [ ] Write Prompt D (sensory data + derived metrics):
  - Input: Results section text + parsed HTML tables
  - Output: `sensory_data` and `derived_metrics` JSON sections
  - Challenge: This is the hardest prompt — must handle dose-response arrays (Wee), raw scores (Ben Abu), relative perceptions (Ben Abu), and power law parameters (Wee)
  - Use the gold-standard JSONs as few-shot examples
- [ ] Write Prompt E (figure extraction):
  - Input: figure image + caption + surrounding text context
  - Output: extracted data points with confidence ratings
  - Use Claude Opus for this prompt
  - Test on Figure 1A (Wee, dose-response curves) and Figure 4a (Ben Abu, bar chart with CI)
  - Validate extracted values against gold standard
- [ ] Run full pipeline on both gold-standard papers
- [ ] Compare LLM output vs. gold standard — compute accuracy metrics

**Deliverables:** 5 tested prompts, accuracy assessment on 2 papers, identified failure modes.

### Phase 3: Scale to Remaining Papers (Days 15-22)

- [ ] Catalog all 10 papers by publisher and access method
- [ ] Fetch HTML/XML for all remaining 8 papers
- [ ] For each paper (in batches of 2-3):
  1. Run publisher parser → structured text + tables + figures
  2. Run Prompts A-D → metadata + sensory data extraction
  3. Run Prompt E → figure extraction (Opus)
  4. Assemble complete JSON
  5. Run attribute normalization
  6. **Manually validate against original paper**
  7. Document errors, gaps, and edge cases
- [ ] Build and maintain attribute vocabulary (`attribute_map.json`):
  - Each new raw term gets mapped to a normalized term
  - Categories: taste, aroma, texture, appearance, hedonic
  - Track frequency of each raw term across papers
- [ ] After each batch, refine prompts based on errors found

**Deliverables:** Validated JSON extractions for all 10 papers, growing attribute vocabulary.

### Phase 4: Build Index + Analysis (Days 23-27)

- [ ] Build SQLite index from all 10 JSON files
- [ ] Write analysis notebooks:
  - **Notebook 1: Data completeness report**
    - How many values extracted per paper?
    - How many nulls / data gaps?
    - What % of data came from tables vs. figures vs. inline text?
    - Which figure types were most/least reliable?
  - **Notebook 2: Extraction accuracy report**
    - Per-paper accuracy (% values matching manual validation)
    - Error categorization: wrong value, missed value, hallucinated value
    - Accuracy by source type (table > inline text > figure?)
    - Accuracy by prompt (A-E)
  - **Notebook 3: Pilot research analysis**
    - Descriptive stats across the 10 papers
    - Attribute vocabulary coverage
    - Preliminary cross-study comparisons (if enough overlap exists)
    - Visualize extracted dose-response curves and taste profiles

**Deliverables:** Populated SQLite index, 3 analysis notebooks, accuracy report.

### Phase 5: Retrospective + Scaling Plan (Days 28-30)

- [ ] Write retrospective document:
  - What worked? What broke?
  - Publisher parser reliability ranking
  - LLM prompt effectiveness ranking
  - Figure extraction reliability by figure type
  - Time per paper: fetching + extraction + validation
  - API costs breakdown
  - Most common data gaps across papers
- [ ] Decide on scaling strategy:
  - Is the pipeline reliable enough for 100+ papers with spot-checking?
  - Which publishers need better parsers?
  - Should you fine-tune prompts per paper "type" (dose-response studies vs. mixture studies)?
  - Would a web UI for manual validation speed things up?
- [ ] Prioritize improvements for next iteration
- [ ] Estimate timeline and cost for scaling to 50 / 100 / 500 papers

**Deliverables:** Retrospective document, scaling plan, prioritized backlog.

---

## 6. Estimated Costs

| Component | Estimated Cost (10 papers) |
|-----------|---------------------------|
| Claude Sonnet API (Prompts A-D) | ~$3-8 |
| Claude Opus API (Prompt E, figures) | ~$5-15 |
| Publisher API access | Free (institutional) |
| Your time (setup + validation) | ~25-35 hours |
| **Total API cost** | **~$8-23** |

---

## 7. Key Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Publisher API key setup takes longer than expected | Medium | Start registration Day 1; have PDF fallback ready |
| Elsevier API returns XML that's hard to parse | Medium | Use `lxml` with XPath; test on Ben Abu paper first |
| Figure extraction accuracy is low (<70%) | High | Use Opus; always validate manually; flag confidence; accept that some values will need human verification |
| HTML structure varies across papers from same publisher | Low-Medium | Build parsers with flexible CSS selectors; test on multiple papers per publisher before scaling |
| Supplementary data needed but only in PDF | Medium | Keep Marker/Docling installed as fallback for supplementary files |
| Attribute vocabulary diverges across diverse food types | Medium | Normalize incrementally; review vocabulary after every 3 papers |
| 30-day timeline is too aggressive | Medium | Phase 3 is the bottleneck (8 papers); can extend to 40 days if needed |

---

## 8. Success Criteria for the Pilot

The 10-paper pilot is successful if:

1. **Extraction completeness:** ≥80% of data points identified in manual review are captured in the JSON (nulls for figure-only data are acceptable if flagged)
2. **Extraction accuracy:** ≥90% of captured numerical values match the paper exactly (or within 5% for figure-extracted values)
3. **Metadata completeness:** ≥95% of fields in `panel`, `session_design`, `scale` sections are populated (where the paper reports them)
4. **Pipeline repeatability:** Running the pipeline twice on the same paper produces identical JSONs
5. **Time efficiency:** Full extraction (fetch + LLM + assembly) takes <30 minutes per paper (excluding manual validation)
6. **Attribute vocabulary:** A working controlled vocabulary with ≥20 normalized terms mapped from raw terms
7. **At least one cross-study analysis** is possible using the extracted data (e.g., comparing sweetness potency values from 2+ papers)

---

## 9. Deliverables Summary

| Deliverable | Format | When |
|-------------|--------|------|
| Gold-standard JSONs (Wee, Ben Abu) | .json files | Day 3 (already drafted) |
| Publisher-specific HTML parsers | Python scripts | Day 7 |
| LLM extraction prompts (A-E) | Text files | Day 14 |
| Validated JSONs for all 10 papers | .json files | Day 22 |
| SQLite index database | .db file | Day 24 |
| Attribute vocabulary | .json mapping file | Day 22 (ongoing) |
| Extraction accuracy report | Jupyter notebook | Day 27 |
| Data completeness report | Jupyter notebook | Day 27 |
| Pilot research analysis | Jupyter notebook | Day 27 |
| Retrospective + scaling plan | Markdown document | Day 30 |
