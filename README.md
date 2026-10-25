# telemetry-adaptive-sampling
Feature-preserving, error-bounded adaptive sampling for multi-channel telemetry. Reduces flight telemetry by ~90% while bounding reconstruction error and keeping key features.

---

## INTRODUCTION

Flight telemetry is recorded at a fixed frame rate: every channel is sampled every
frame, and every frame carries **all** channels at once. A 100 fps stream with 500
channels at 16 bit produces roughly **4.8 × 10⁸ bit (60 MB) per 10 minutes**, i.e.
about 800 kbit/s of downlink — more than a narrow telemetry link can carry once
other services share the band.

The usual answer is to keep every *n*-th record. It is trivial, it needs no model,
and it is also the reason transient events (ignition, stage separation, maximum
dynamic pressure) get smoothed away: a uniform step does not know where the curve
actually bends.

This project implements an alternative: **choose which records to keep so that the
reconstructed curve stays within a given error bound, while the few points that
carry the flight story survive.**

## PROBLEM TO DETERMINE

Given a multi-channel telemetry recording and an error bound ε, decide

1. **which records to keep** so that linear reconstruction stays within ε of the
   original for *every* channel and *every* record, and
2. **how few** records that takes, without losing the local extrema, trend changes
   and segment endpoints that engineers read the data for.

Three properties make this harder than plain downsampling:

| Constraint | Consequence |
|---|---|
| Telemetry is frame-based | Keeping one record keeps **all** channels; channels cannot be sampled independently |
| Channel ranges differ by orders of magnitude | A single absolute threshold is meaningless across channels |
| Missing segments exist | Interpolation must not bridge a gap |

## METHOD — STEP BY STEP

The whole pipeline lives in [`src/run_analysis.py`](src/run_analysis.py).

**Step 1 — Clean and segment** (`load_raw`, `contiguous_runs`)
Drop fully-empty rows, index records by position (the `t` column is rounded to
whole seconds, so ten frames share one timestamp), and split each channel into
contiguous valid segments. Compute each channel's range `R_c` for normalisation.

**Step 2 — Anchor on key features** (`turning_points`, `adaptive_segment`)
Take segment endpoints, local extrema (first-difference sign changes, with a
0.005·R_c dead band) and global extrema as the initial anchor set.

**Step 3 — Adaptive subdivision** (`adaptive_segment`)
For every interval, fit a chord, measure the normalised deviation
`e_i = |y_i − ŷ_i| / R_c`, and if `max e_i > ε` split at the worst record and
recurse on both halves.

**Step 4 — Merge channels and re-check** (`adaptive_selection`)
Union all per-channel point sets. Because the union changes every channel's
intervals, recompute the error on the shared set and **add points** until all
channels satisfy ε again.

**Step 5 — Score the result** (`sampling_metrics`, `calc_entropy`,
`calc_kl_divergence`)
Retention ratio, max normalised error, normalised RMSE, entropy ratio and KL
divergence — geometry and distribution checked separately.

## RESULTS

Public Starship launch telemetry, OCR'd from the broadcast overlay
(10 fps, 2 672 raw records → 2 516 after cleaning → 2 270 with at least one
valid channel; `v1` range 2 045, `h1` range 39, one contiguous segment each).

| Metric | Fixed step 10 | This method (ε = 0.1 %) |
|---|---:|---:|
| Records kept | 229 | **226** |
| Retention | 10.09 % | 9.96 % |
| Max normalised error | 2.308 % | **0.098 %** |
| Normalised RMSE | 0.356 % | **0.020 %** |

Same data volume, **~24× lower worst-case error**. Loosening the bound to ε = 1 %
drops the count to 127 records (5.59 %) at 0.945 % error — still below what a
uniform sampler achieves at 229 records.

| | |
|---|---|
| ![segments](docs/figures/fig2_segments_features.png) | ![subdivision](docs/figures/fig3_adaptive_subdivision.png) |
| Valid segments and key features | Adaptive subdivision at work |
| ![union](docs/figures/fig4_union_refill.png) | ![tradeoff](docs/figures/fig5_retention_error.png) |
| Multi-channel union and re-fill | Retention vs. error, with the ε = 1 % bound |

Storage of the same cleaned set: CSV 40 097 B · Gzip CSV 10 385 B ·
Parquet/Zstd 13 501 B; the 226-record sample is 3 858 B as CSV, 1 451 B gzipped
(9.6 % / 3.6 % of the full cleaned CSV).

## REPOSITORY STRUCTURE

```
telemetry-adaptive-sampling/
├── src/run_analysis.py          # the whole pipeline: read → sample → score → report
├── data/
│   ├── raw/telemetry-10fps.csv  # not committed — see Data source
│   └── processed/               # cleaned csv / gz / parquet (regenerable)
├── results/
│   ├── report.md                # generated experiment report
│   ├── sampling_comparison.csv  # per-scheme metrics
│   ├── storage_comparison.csv   # format benchmark
│   ├── plots/                   # 6 figures
│   └── sampled/                 # sampled outputs per scheme
├── docs/figures/                # figures used in this README
├── assets/fonts/                # bundled CJK font (SIL OFL 1.1)
├── requirements.txt
└── README.md
```

## HOW TO RUN

```bash
python -m pip install -r requirements.txt
python src/run_analysis.py
```

`numpy`, `scikit-learn` and `matplotlib` are required; `pyarrow` (Parquet) and
`fonttools` (font check) are optional. The script exits with status 2 rather than
overwriting previous results with an incomplete run; `--allow-partial` relaxes
that.

## DATA SOURCE

[chrisjbillington/starship_telemetry](https://github.com/chrisjbillington/starship_telemetry)
— telemetry region of the public launch broadcast, OCR'd at 10 fps. It is **not**
official vehicle telemetry and carries OCR errors. The raw CSV is intentionally
not redistributed here; download it from the upstream repository and place it at
`data/raw/telemetry-10fps.csv`.

## LICENSE

Code: MIT. Bundled font: SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
