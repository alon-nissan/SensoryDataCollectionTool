# Sensory Data Extraction Project — Full Plan

## 1. LLM Recommendation

### Primary: Claude API (Sonnet 4.5) via Python

For your specific use case, I recommend **Claude Sonnet 4.5** as the primary extraction engine, with **Claude Opus** reserved for the hardest tasks (figure data extraction). Here's why:

**Why Claude over GPT-4o for this project:**
- **Long context window (200K tokens):** Many scientific papers are 8,000–15,000 tokens. Claude can ingest an entire paper in one pass, which matters because sensory methods sections often reference information scattered across the paper (e.g., "samples described in Section 2.1 were evaluated using the protocol in Section 2.3").
- **Structured output reliability:** Claude excels at following complex JSON schemas with nested objects — critical for your multi-level extraction.
- **PDF/image support:** The Claude API accepts PDF files and images natively. You can send the full PDF (for text/table extraction) AND individual figure screenshots (for chart data extraction) in the same conversation.
- **Cost efficiency:** Sonnet 4.5 is significantly cheaper than Opus while handling 80%+ of your extraction tasks. Reserve Opus for ambiguous cases or figure extraction where precision matters most.

**Why not open-source models:**
- Figure extraction (reading data points from spider plots, bar charts) requires very strong vision capabilities. Open-source VLMs (LLaVA, Qwen2-VL) are improving rapidly but still lag behind Claude/GPT-4o on precise numerical extraction from scientific charts.
- For a 10-paper pilot, API costs will be minimal ($5–15 total). The engineering time saved is worth far more.

### Supporting Tools

| Component | Tool | Purpose |
|-----------|------|---------|
| PDF parsing | **Marker** or **Docling** | Convert PDF → Markdown with preserved table structure |
| Figure extraction | **PyMuPDF (fitz)** | Extract figure images from PDFs programmatically |
| LLM extraction | **Claude Sonnet 4.5 API** | Schema-guided extraction from text, tables, and figures |
| Hard cases | **Claude Opus API** | Complex figures, ambiguous data, validation passes |
| Storage | **SQLite** via Python | Structured relational storage |
| Orchestration | **Python scripts** | Glue everything together |

---

## 2. Finalized Database Schema

### Level 1: `studies`
| Field | Type | Description |
|-------|------|-------------|
| study_id | TEXT (PK) | Unique identifier (e.g., "smith2023") |
| doi | TEXT | Digital Object Identifier |
| title | TEXT | Paper title |
| authors | TEXT | Author list |
| year | INTEGER | Publication year |
| journal | TEXT | Journal name |
| food_category | TEXT | High-level category (dairy, beverages, snacks, etc.) |
| country | TEXT | Country where study was conducted |
| abstract | TEXT | Paper abstract |
| extraction_notes | TEXT | Free-text notes from manual validation |

### Level 2: `experiments`
| Field | Type | Description |
|-------|------|-------------|
| experiment_id | TEXT (PK) | Unique identifier |
| study_id | TEXT (FK) | Links to studies |
| experiment_description | TEXT | Brief description of this experiment's purpose |
| evaluation_method | TEXT | QDA, CATA, hedonic, TDS, gLMS, VAS, etc. |
| panel_type | TEXT | trained / consumer / expert / semi-trained |
| panel_size | INTEGER | Number of panelists |
| panel_demographics | TEXT | Age range, gender distribution, etc. (if reported) |
| panel_training | TEXT | Training protocol description |
| num_sessions | INTEGER | Number of evaluation sessions |
| num_replicates | INTEGER | Number of replicates per panelist |
| presentation_order | TEXT | randomized / Latin square / Williams / balanced incomplete block / other |
| palate_cleanser | TEXT | Water, crackers, etc. |
| rest_interval | TEXT | Time between samples |
| serving_conditions | TEXT | Temperature, volume, vessel, lighting, booth type |
| reference_standards | TEXT | Reference samples used for calibration |
| statistical_methods_json | TEXT | JSON blob: {"tests": [...], "software": "...", "alpha": 0.05, ...} |
| workflow_description | TEXT | Free-text description of the full experimental workflow |

### Level 3: `stimuli`
| Field | Type | Description |
|-------|------|-------------|
| stimulus_id | TEXT (PK) | Unique identifier |
| experiment_id | TEXT (FK) | Links to experiments |
| stimulus_name | TEXT | Name/code as reported in the paper |
| product_type | TEXT | Specific product (e.g., "Greek yogurt", "IPA beer") |
| description | TEXT | Free-text description of the stimulus |
| preparation_method | TEXT | How the stimulus was prepared |
| composition_kv_json | TEXT | JSON key-value pairs: {"sugar_pct": 6.0, "fat_pct": 3.2, ...} |
| brand_source | TEXT | Commercial brand or lab-prepared |
| sample_coding | TEXT | 3-digit codes used, blinding procedure |

### Level 4: `measurements`
| Field | Type | Description |
|-------|------|-------------|
| measurement_id | TEXT (PK) | Unique identifier |
| stimulus_id | TEXT (FK) | Links to stimuli |
| attribute_raw | TEXT | Attribute term exactly as written in paper |
| attribute_normalized | TEXT | Standardized term from controlled vocabulary |
| attribute_category | TEXT | taste / aroma / texture / appearance / hedonic / other |
| scale_type | TEXT | gLMS / VAS / 9-point hedonic / 15-cm line / 0-10 / etc. |
| scale_min | REAL | Lower anchor value |
| scale_max | REAL | Upper anchor value |
| scale_anchors | TEXT | Anchor label descriptions (e.g., "barely detectable" to "strongest imaginable") |
| value_mean | REAL | Reported mean value |
| value_sd | REAL | Standard deviation (if reported) |
| value_se | REAL | Standard error (if reported) |
| value_ci_low | REAL | Confidence interval lower bound |
| value_ci_high | REAL | Confidence interval upper bound |
| value_median | REAL | Median (if reported instead of mean) |
| stat_group_letter | TEXT | Significance grouping (a, b, c, ab, etc.) |
| value_raw_text | TEXT | The exact text from which this value was extracted |
| source_type | TEXT | table / figure / inline_text |
| source_location | TEXT | "Table 2" or "Figure 3" or "p.5, paragraph 2" |
| extraction_confidence | TEXT | high / medium / low (self-assessed by LLM) |

### Level 5: `statistical_outputs`
| Field | Type | Description |
|-------|------|-------------|
| output_id | TEXT (PK) | Unique identifier |
| experiment_id | TEXT (FK) | Links to experiments |
| output_type | TEXT | ANOVA / PCA / regression / correlation / etc. |
| description | TEXT | What this output represents |
| data_json | TEXT | Free-form JSON of the extracted statistical data |
| source_location | TEXT | "Table 4" or "Figure 5" |

### Controlled Vocabulary Table: `attribute_vocabulary`
| Field | Type | Description |
|-------|------|-------------|
| normalized_term | TEXT (PK) | The canonical attribute name |
| category | TEXT | taste / aroma / texture / appearance / hedonic |
| raw_variants | TEXT | JSON list of all raw terms mapped to this canonical term |

---

## 3. Extraction Pipeline Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    INPUT: PDF File                        │
└─────────────┬───────────────────────────────┬────────────┘
              │                               │
              ▼                               ▼
┌─────────────────────┐         ┌─────────────────────────┐
│  STAGE 1: PDF Parse │         │  STAGE 1b: Figure       │
│  (Marker / Docling) │         │  Extraction (PyMuPDF)   │
│  → Markdown + Tables│         │  → PNG images per figure│
└─────────┬───────────┘         └───────────┬─────────────┘
          │                                 │
          ▼                                 ▼
┌─────────────────────┐         ┌─────────────────────────┐
│  STAGE 2a: Metadata │         │  STAGE 2b: Figure Data  │
│  Extraction (Sonnet)│         │  Extraction (Opus)      │
│  → Study, Experiment│         │  → Numerical values     │
│    Stimulus details  │         │    from charts/plots    │
└─────────┬───────────┘         └───────────┬─────────────┘
          │                                 │
          ▼                                 ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 3: Numerical Data Extraction (Sonnet)             │
│  → Measurements from tables + inline text                │
│  → Merge with figure-extracted data                      │
└─────────────────────────┬───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 4: Attribute Normalization + Validation            │
│  → Map raw terms to controlled vocabulary                │
│  → Flag low-confidence extractions                       │
│  → Output structured JSON per paper                      │
└─────────────────────────┬───────────────────────────────┘
                          │
                          ▼
┌─────────────────────────────────────────────────────────┐
│  STAGE 5: Load into SQLite                               │
│  → Insert into relational tables                         │
│  → Manual validation against paper                       │
└─────────────────────────────────────────────────────────┘
```

---

## 4. Working Plan — 10-Paper Pilot

### Phase 0: Setup (Days 1–2)
**Goal:** Environment ready, first paper processed manually as baseline.

- [ ] Set up Python environment with dependencies:
  - `anthropic` (Claude API client)
  - `marker-pdf` or `docling` (PDF parsing)
  - `PyMuPDF` (figure extraction)
  - `sqlite3` (built-in), `pandas`
- [ ] Create SQLite database with schema above
- [ ] Manually extract data from Paper #1 into the database by hand
  - This becomes your **gold standard** for measuring extraction accuracy
  - Time yourself — this tells you the baseline cost of manual extraction
- [ ] Document every ambiguity you encounter (these become prompt engineering targets)

**Deliverables:** Working environment, populated database with 1 paper (manual), list of extraction challenges.

### Phase 1: PDF Parsing Pipeline (Days 3–5)
**Goal:** Reliable text and figure extraction from PDFs.

- [ ] Run Marker/Docling on all 10 PDFs
- [ ] Manually review output for 3 papers:
  - Are tables preserved correctly?
  - Is multi-column layout parsed in the right reading order?
  - Are figure captions associated with the right figures?
- [ ] Run PyMuPDF figure extraction on all 10 PDFs
- [ ] Manually verify extracted figures are complete and correctly cropped
- [ ] Decide: Marker vs Docling — which works better for your specific papers?

**Deliverables:** Markdown + extracted figures for all 10 papers, chosen PDF parser.

### Phase 2: Prompt Engineering — Metadata Extraction (Days 6–10)
**Goal:** Reliable extraction of study, experiment, and stimulus metadata.

- [ ] Write extraction prompts for each schema level:
  - **Prompt A:** Study-level metadata (paper info, food category)
  - **Prompt B:** Experiment-level metadata (panel, session, methods)
  - **Prompt C:** Stimulus details (products, formulations, composition)
- [ ] Use few-shot prompting: include Paper #1 (your gold standard) as an example
- [ ] Test on Papers #2–4, compare against manual reading
- [ ] Iterate on prompts until accuracy is consistently high
- [ ] Key prompt engineering decisions:
  - Send full paper text vs. just Methods section?
  - One mega-prompt vs. separate prompts per level?
  - How to handle missing information (paper doesn't report panel size)?

**Deliverables:** Tested prompts for metadata extraction, accuracy notes per paper.

### Phase 3: Prompt Engineering — Numerical Data from Tables (Days 11–15)
**Goal:** Reliable extraction of sensory scores from tables.

- [ ] Write extraction prompts for measurements:
  - **Prompt D:** Extract all sensory data from tables → JSON matching measurements schema
  - Include scale info, raw terms, statistical group letters
- [ ] Critical challenge: tables in sensory papers are often complex
  - Multi-level headers (sample × attribute × timepoint)
  - Footnotes with statistical significance markers
  - Tables split across pages
- [ ] Test on Papers #2–4, validate against manual reading
- [ ] Build attribute vocabulary as you go:
  - Each new raw term gets mapped to a normalized term
  - Store in `attribute_vocabulary` table

**Deliverables:** Tested prompts for table extraction, initial controlled vocabulary.

### Phase 4: Figure Data Extraction (Days 16–22)
**Goal:** Extract numerical data from figures (hardest task).

- [ ] Categorize figures across all 10 papers:
  - Bar charts → relatively straightforward
  - Spider/radar plots → moderate difficulty
  - PCA biplots → hard (need coordinates)
  - Line graphs → moderate
  - Box plots → moderate
- [ ] Write figure-specific prompts:
  - **Prompt E:** "This is a [bar chart/spider plot/etc.] from a sensory study. Extract all data points..."
  - Include figure caption as context
  - Ask for axis labels, scale, and units explicitly
- [ ] Use Claude Opus for figure extraction (stronger vision capabilities)
- [ ] For each figure, ask the LLM to rate its own confidence
- [ ] Validate ALL figure extractions manually — this is where errors are most likely
- [ ] Document which figure types are reliable vs. unreliable

**Deliverables:** Figure extraction prompts, accuracy assessment per figure type, confidence calibration.

### Phase 5: Integration + Validation (Days 23–28)
**Goal:** Complete extraction for all 10 papers, validated.

- [ ] Run the full pipeline on Papers #5–10
- [ ] For each paper:
  1. Parse PDF → Markdown + figures
  2. Extract metadata (Prompts A, B, C)
  3. Extract table data (Prompt D)
  4. Extract figure data (Prompt E)
  5. Normalize attributes
  6. Load into SQLite
  7. **Manually validate against original paper**
- [ ] Track accuracy metrics:
  - Metadata completeness: % of fields correctly populated
  - Numerical accuracy: % of values matching paper exactly
  - Figure extraction accuracy: % of data points correct
  - False positives: data the LLM hallucinated
  - False negatives: data the LLM missed
- [ ] Build a validation spreadsheet comparing extracted vs. actual values

**Deliverables:** Complete database for 10 papers, accuracy report, validation spreadsheet.

### Phase 6: Retrospective + Scaling Plan (Days 29–30)
**Goal:** Document lessons learned, plan for scaling.

- [ ] Write up findings:
  - What worked well? What broke?
  - Which paper types are easiest/hardest to extract?
  - Which figure types are reliable?
  - Total cost (API calls, time spent)
  - Time per paper: automated extraction vs. manual validation
- [ ] Decide on scaling strategy:
  - Is the pipeline reliable enough to run on 100+ papers with spot-checking?
  - Which components need improvement before scaling?
  - Should you fine-tune a model on your 10-paper gold standard?
- [ ] Prioritize improvements for the next iteration

**Deliverables:** Retrospective document, scaling plan, prioritized improvement list.

---

## 5. Estimated Costs

| Component | Estimated Cost (10 papers) |
|-----------|---------------------------|
| Claude Sonnet API (metadata + tables) | ~$3–8 |
| Claude Opus API (figures + hard cases) | ~$5–15 |
| PDF parsing tools | Free (open source) |
| Your time (manual validation) | ~30–40 hours total |
| **Total API cost** | **~$8–23** |

---

## 6. Key Risks and Mitigations

| Risk | Likelihood | Mitigation |
|------|------------|------------|
| Figure data extraction is inaccurate | High | Use Opus, always validate manually, flag low-confidence extractions |
| Tables span multiple pages, parser breaks them | Medium | Pre-process: manually check parsed output in Phase 1 |
| Attribute vocabulary explodes (too many unique terms) | Medium | Start normalizing from Paper #1, build vocabulary incrementally |
| Papers have non-standard layouts that confuse parser | Medium | Try both Marker and Docling, fall back to image mode |
| LLM hallucinates numerical values | Medium | Cross-reference every extracted value with source location |
| Scale information is ambiguous or unreported | Medium | Add "not_reported" as valid value, flag for manual review |

---

## 7. File Structure

```
sensory-extraction/
├── data/
│   ├── pdfs/                   # Original PDFs
│   ├── parsed/                 # Markdown output from Marker/Docling
│   ├── figures/                # Extracted figure images
│   └── sensory.db              # SQLite database
├── prompts/
│   ├── study_metadata.txt      # Prompt A
│   ├── experiment_metadata.txt # Prompt B
│   ├── stimulus_details.txt    # Prompt C
│   ├── table_extraction.txt    # Prompt D
│   └── figure_extraction.txt   # Prompt E
├── scripts/
│   ├── parse_pdf.py            # PDF → Markdown + figures
│   ├── extract_metadata.py     # Prompts A, B, C
│   ├── extract_tables.py       # Prompt D
│   ├── extract_figures.py      # Prompt E
│   ├── normalize_attributes.py # Raw → normalized mapping
│   ├── load_to_db.py           # JSON → SQLite
│   └── validate.py             # Compare extracted vs. gold standard
├── validation/
│   ├── gold_standard/          # Manually extracted data for comparison
│   └── accuracy_report.md      # Per-paper accuracy metrics
├── requirements.txt
└── README.md
```
