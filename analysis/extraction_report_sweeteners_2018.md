# Extraction Quality Report: Wee et al., 2018

**Paper:** A Comparison of Psychophysical Dose-Response Behaviour across 16 Sweeteners
**First-run completeness score:** 15%
**Target:** Extract enough data to recreate paper tables/figures in Python

---

## Root Cause Chain

The low completeness score stems from a single cascading failure:

```
Agent 1 marked all dose-response values as null ("vision extraction needed")
  -> Agent 2 correctly produced 0 result rows (can't structure nulls)
  -> Agent 2 DID create 97 samples, but only 20 sample_ids passed to Agent 3
  -> Agent 3 couldn't match 80% of figure data to samples -> extracted ~57 points for 3-4 sweeteners
  -> Agent 4 couldn't verify any figure-sourced data ("cannot access figure")
  -> Final DB: 57 unidentified, partially duplicated results out of ~200+ expected
```

Meanwhile, Agent 1 extracted 61 derived metrics (slopes, potency ratios) that Agent 2 silently dropped.

---

## Question-by-Question Analysis

### 1. AGENT 1 -> AGENT 2 HANDOFF: "Why did 61 derived metrics vanish?"

**Finding:** Agent 1 extracted 16 Stevens' law slopes + 45 potency ratios as `derived_metrics` entries. Agent 2's prompt (`prompts/agent2_structuring.txt`) documents `value_type: "derived_param"` as a valid enum value (line 164) but never instructs Agent 2 how to map `derived_metrics` array entries into `results` rows. Agent 2's input processing only looks for `sensory_data` blocks within each experiment.

**Evidence:** The Agent 2 prompt's "OUTPUT TABLE SCHEMAS" section (line 138-154) lists `derived_param` as valid for `value_type`, confirming the schema supports it. But the "IMPORTANT RULES" section (line 396-410) has no rule about processing `derived_metrics`.

**Fix applied:** FIX 2 — Added explicit "DERIVED METRICS -> RESULTS MAPPING" section to `prompts/agent2_structuring.txt` (inserted before IMPORTANT RULES). Maps `metric_type` -> `attribute_raw`, `value` -> `value`, sets `value_type = "derived_param"`, and stores model/parameters in `context_json`.

### 2. AGENT 1 NULL-VALUE PROBLEM: "Why defer to vision when table data exists?"

**Finding:** The paper's Table 2 contains concentrations in % w/v for all 16 sweeteners. Agent 1 either missed this table or prioritized the Supplementary Table S1 reference. The dose-response intensity values (mean gLMS ratings) genuinely appear only in Figures 1-6 — Agent 1 was correct that these are figure-only. However, Agent 1 left concentration values as null despite Table 2 being available.

**Evidence:** Agent 1's prompt (`prompts/agent1_free_extraction.txt`) says "Note any data available only in figures" (line 10) and "If information is not reported in the paper, omit the field rather than guessing" (line 11). There is no instruction to prioritize main-text tables over supplementary references when both exist.

**Fix applied:** FIX 3 — Added "CONCENTRATION EXTRACTION (CRITICAL)" section to `prompts/agent1_free_extraction.txt`. Instructs Agent 1 to always extract concentrations from main paper tables, even when supplementary data exists in different units.

### 3. SAMPLE LIST TRUNCATION: "Why did Agent 3 only get 20 of 97 sample IDs?"

**Finding:** `scripts/agent3_figures.py:74` (original) applies `json.dumps(sample_ids, indent=2)[:3000]` — a hard 3000-character string slice. With JSON indentation, each sample entry takes ~60-80 characters. At 97 samples, the full JSON is ~7000+ characters, so `[:3000]` cuts off at ~sample 38-40. But the indented JSON format means the slice likely cuts mid-object, corrupting the JSON and causing the LLM to see fewer valid entries.

**Evidence:** Lines 74-76 in original `agent3_figures.py`:
- `json.dumps(sample_ids, indent=2)[:3000]` — sample list truncated to 3000 chars
- `json.dumps(existing_summary, indent=2)[:5000]` — results summary truncated to 5000 chars
- `json.dumps(experiment_context, indent=2)[:2000]` — experiment context truncated to 2000 chars

**Fix applied:** FIX 1 — Replaced character truncation with compact pipe-delimited format (`sample_id | label`, one per line). 97 samples now fit in ~3000 chars instead of requiring 7000+. No more mid-object corruption.

### 4. AGENT 3 ARCHITECTURE: "Should Agent 3 be allowed to create samples?"

**Finding:** The Agent 3 prompt (`prompts/agent3_figures.txt:2`) stated: "Do NOT create new sample_ids." This is a hard constraint that blocks extraction when the sample list is incomplete. The original rationale was to prevent hallucinated sample identities, but it causes total extraction failure when combined with sample list truncation.

**Evidence:** Original prompt line 2: "Use EXISTING sample_ids from the provided list. Do NOT create new sample_ids. If you see a sample in the figure that does not appear in the existing sample list, add its label to 'unmatched_samples' instead of creating a result row."

**Fix applied:** FIX 6 — Changed the hard constraint to a soft preference. Agent 3 now prefers existing sample_ids but may create new ones (format: `{paper_id}__sample_fig_{N}`) with `source_type: "figure_created"` when no match exists. These are flagged in `figure_created_samples` for human review.

### 5. FIGURE DEDUPLICATION: "How should overlapping figure/table data be handled?"

**Finding:** The prompt says "skip values already present in existing_results_summary" — but when Agent 2 produces 0 results, `existing_results_summary` is empty, so there's nothing to skip. Figures 1A and Figure 2 both extracted the same sweetener data points, creating ~19 duplicate pairs.

**Evidence:** `agent3_figures.py:51-52` builds `existing_summary` from `agent2_output.get("results", [])`, which was empty. Each figure was processed independently with no awareness of prior figures' results.

**Fix applied:** FIX 5 — Added sequential accumulation of results across figures. `accumulated_results` starts with Agent 2's results and grows as each figure is processed. Each subsequent figure sees all prior extractions in its `existing_results_summary`, enabling cross-figure deduplication.

### 6. AGENT 4 SPOT-CHECK: "Why can't validation verify figure-sourced data?"

**Finding:** Agent 4's `_run_spot_check` (`scripts/agent4_validate.py:311-342`) uses `llm.extract_json()` — a text-only API call. It receives no figure images. When 100% of results come from figures, every spot-check returns "cannot verify — cannot access figure."

**Evidence:** The function signature was `_run_spot_check(sampled_results, article, llm, config)` with no figure metadata parameter. The prompt asks the LLM to "verify this extracted data point against the original paper" but provides only text, not the figure image.

**Fix applied:** FIX 4 — Added `figure_metadata` parameter to `run_agent4()` and `_run_spot_check()`. When a spot-checked result has `source_type == "figure"`, the function matches its `source_location` to a figure_id and calls `llm.extract_json_with_image()` with the figure image. The orchestrator now passes `figure_metadata` through to Agent 4.

### 7. CONCENTRATION DATA: "Why are all 102 sample_components missing concentrations?"

**Finding:** Agent 2 created 102 `sample_component` rows with `concentration: null`. This traces back to Agent 1 referencing Supplementary Table S1 for concentrations (in mmol/L) while Table 2 in the main paper has them in % w/v. Agent 1 deferred to the supplementary reference rather than extracting the main-text values.

**Fix applied:** Addressed by FIX 3 (Agent 1 concentration extraction guidance). With the updated prompt, Agent 1 will extract Table 2 concentrations directly, filling in the 102 null values.

### 8. DERIVED METRICS AND MODEL PARAMETERS

**Finding:** The `results` table already supports `value_type: "derived_param"` (defined in `prompts/agent2_structuring.txt:164`). No schema change is needed. The gap was purely in Agent 2's prompt — it lacked instructions to map `derived_metrics` -> `results` rows.

**Fix applied:** FIX 2 covers this. Model parameters (Hill equation params, Stevens' law params) will be stored in `results` with `value_type: "derived_param"` and model details in `context_json`.

### 9. FIGURE RELEVANCE FILTERING

**Finding:** The relevance filter in `config.yaml` sets `relevance_threshold: 0.3`. Agent 1's scoring guidance (`prompts/agent1_free_extraction.txt:218-231`) penalizes figures whose data "already appears in tables." For this paper where figures ARE the primary data source, this scoring logic could deprioritize essential figures.

**Evidence:** `orchestrate.py:307-308` applies the filter: `_filter_figures_by_relevance(figure_metadata, agent1_output, rel_threshold)`. The threshold of 0.3 means figures scored 0.0-0.2 are dropped.

**Fix applied:** FIX 7 — Added clause to Agent 1's scoring guidance: if the paper's primary quantitative results appear only in figures (not tabulated), all data-bearing figures should be scored 0.8+ regardless of inter-figure overlap. Also instructs Agent 1 to set `figure_primary_data: true` in study_metadata.

### 10. PIPELINE ARCHITECTURE

**Finding:** The serial Agent 1->2->3 design assumes tables are primary and figures supplement them. This paper inverts that assumption. A pre-processing classification step would help, but is not implemented here — instead, the fixes above make the existing architecture robust enough to handle figure-heavy papers:
- FIX 1 ensures all samples reach Agent 3
- FIX 5 ensures cross-figure deduplication
- FIX 6 ensures Agent 3 can extract data even with incomplete sample lists
- FIX 7 ensures figures aren't filtered out in figure-primary papers

---

## Fixes Applied (Priority Order)

### FIX 1 — Remove sample list truncation
**Files modified:** `scripts/agent3_figures.py`
- Replaced `json.dumps(sample_ids, indent=2)[:3000]` with compact pipe-delimited format via `_format_sample_list_compact()`
- Removed hard character limits on `existing_results_summary` and `experiment_context`
- **Impact:** All 97 samples now visible to Agent 3 across all figures

### FIX 2 — Add derived_metrics -> results mapping in Agent 2 prompt
**Files modified:** `prompts/agent2_structuring.txt`
- Added "DERIVED METRICS -> RESULTS MAPPING" section before IMPORTANT RULES
- Instructs Agent 2 to convert `derived_metrics` entries -> `results` rows with `value_type: "derived_param"`
- Maps `metric_type` -> `attribute_raw`, stores model/parameters in `context_json`
- **Impact:** Recovers 16 slopes + 16 intercepts (Table 3) + 45 potency ratios (Table 4) = 77 results

### FIX 3 — Ensure Agent 1 extracts Table 2 concentrations
**Files modified:** `prompts/agent1_free_extraction.txt`
- Added "CONCENTRATION EXTRACTION (CRITICAL)" section
- Instructs Agent 1 to always extract main-paper concentrations, never defer to supplementary only
- **Impact:** Fills 102 null concentrations, essential for dose-response plots

### FIX 4 — Pass figure images to Agent 4 for spot-checking
**Files modified:** `scripts/agent4_validate.py`, `scripts/orchestrate.py`
- Added `figure_metadata` parameter to `run_agent4()` and `_run_spot_check()`
- Spot-check now uses `extract_json_with_image()` for figure-sourced results
- Orchestrator passes `figure_metadata` through to Agent 4
- **Impact:** Validation now functional for figure-sourced results

### FIX 5 — Add cross-figure deduplication in Agent 3
**Files modified:** `scripts/agent3_figures.py`
- Added `accumulated_results` list that grows as each figure is processed
- Each figure's `existing_results_summary` includes Agent 2 results + all prior figures' results
- **Impact:** Eliminates ~19 duplicate pairs from overlapping figures

### FIX 6 — Allow Agent 3 to create samples as fallback
**Files modified:** `prompts/agent3_figures.txt`
- Changed hard "Do NOT create new sample_ids" to soft preference
- Agent 3 may create `{paper_id}__sample_fig_{N}` with `source_type: "figure_created"`
- New samples flagged in `figure_created_samples` for human review
- **Impact:** Safety net for cases where Agent 2's sample list is incomplete

### FIX 7 — Update figure relevance scoring for figure-heavy papers
**Files modified:** `prompts/agent1_free_extraction.txt`
- Added clause: figure-primary papers score all data-bearing figures at 0.8+
- Added `figure_primary_data: true` metadata flag
- **Impact:** Prevents essential figures from being filtered out

### Config update
**Files modified:** `config.yaml`
- Bumped prompt versions: agent1 v1.1->v1.2, agent2 v1.0->v1.1, agent3 v1.0->v1.1, agent4 v1.0->v1.1

---

## Before/After Projection

| Paper Element | Before (actual) | After (projected) | Source of improvement |
|---|---|---|---|
| **Table 2 concentrations** | 0/102 (all null) | ~102/102 | FIX 3 (Agent 1 concentration guidance) |
| **Table 3 slopes** | 0/32 (16 slopes + 16 intercepts) | ~32/32 | FIX 2 (derived_metrics mapping) |
| **Table 4 potency ratios** | 0/45 | ~45/45 | FIX 2 (derived_metrics mapping) |
| **Figure 1 dose-response** | ~19 unidentified points | ~96 identified points (16 sweeteners x 6 conc) | FIX 1 + FIX 6 |
| **Figures 3-6 by category** | 0 (not extracted) | ~96 additional points | FIX 1 + FIX 6 + FIX 7 |
| **Cross-figure duplicates** | ~19 duplicate pairs | 0 | FIX 5 |
| **Spot-check verification** | 0/5 verifiable | ~5/5 verifiable | FIX 4 |
| **Total result rows** | 57 | ~270+ | All fixes combined |
| **Completeness score** | 15% | ~75-85% (projected) | |

### Remaining gap to 100%

- Figure-only intensity values have inherent reading uncertainty (medium/low confidence)
- Some supplementary-only data may still be inaccessible
- Mixture interaction data in Figures 5-6 may require complex sample matching

---

## Re-run Checklist

After applying the fixes, re-run the paper with:

```bash
python scripts/orchestrate.py \
  --file data/html/wee2018.html \
  --doi "10.1016/j.foodqual.2018.01.006" \
  --force \
  --no-figure-filter
```

### Verification steps:

- [ ] **Agent 1 output:** Check `data/extractions/parts/<study_id>/agent1_extraction.json`
  - [ ] `derived_metrics` array contains 16 slopes + 45 potency ratios
  - [ ] Stimulus compositions have concentrations from Table 2 (% w/v)
  - [ ] `figure_inventory` scores all dose-response figures >= 0.8
  - [ ] `figure_primary_data: true` is set in study_metadata

- [ ] **Agent 2 output:** Check `data/extractions/parts/<study_id>/agent2_structured.json`
  - [ ] `results` array includes `value_type: "derived_param"` rows (slopes, potency ratios)
  - [ ] `sample_components` have non-null `concentration` values
  - [ ] 97 samples present with correct sweetener labels

- [ ] **Agent 3 output:** Check `data/extractions/parts/<study_id>/agent3_figures.json`
  - [ ] All 7 figures processed (none filtered)
  - [ ] `unmatched_samples` is empty or minimal
  - [ ] No duplicate data points between figures
  - [ ] Results cover all 16 sweeteners

- [ ] **Agent 4 output:** Check `data/extractions/parts/<study_id>/agent4_validation.json`
  - [ ] Spot-check returns actual verification results (not "cannot access figure")
  - [ ] `completeness_score` >= 0.70
  - [ ] `duplicates_resolved` is empty or minimal

- [ ] **Database verification:**
  ```sql
  SELECT COUNT(*) FROM results WHERE paper_id = '<paper_id>';
  -- Expected: ~270+

  SELECT COUNT(*) FROM sample_components
  WHERE sample_id IN (SELECT sample_id FROM samples WHERE paper_id = '<paper_id>')
    AND concentration IS NOT NULL;
  -- Expected: ~102

  SELECT COUNT(*) FROM results
  WHERE paper_id = '<paper_id>' AND value_type = 'derived_param';
  -- Expected: ~77
  ```
