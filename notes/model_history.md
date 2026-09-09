# Excitingness Model: History

Model: Ridge regression, alpha 30, vote-weighted, symmetric features only.
Train: World Cup matches, IMDb episode rating as label.
Predict: Premier League, no labels available.
Training rows: 154 (168 labelled, minus 14 extra-time).

---

## Evaluation

Sources:
- World Cup: IMDb episode ratings. 30-seed averaged, out-of-fold, 5-fold CV.
- Premier League: 12 expert-nominated matches from the "best match of 2025/26"
  shortlist. Vote shares ignored, all 12 weighted equally.

Components, 1/3 each, mapped to [0,1], higher better:

| Component | Measures | Transform |
|---|---|---|
| nRMSE | score calibration, all matches, WC | `clip((0.90 − RMSE)/0.25, 0, 1)` |
| Spearman | rank, all matches, WC | as-is |
| PL meanPct | rank of the 12 nominated matches, PL | `mean(1 − (rank−1)/400)` |

Caveats:
- nRMSE bounds 0.65/0.90 are hand-set. Ordering robust, margin size is not.
- nRMSE, Spearman and rank-agreement correlate ~0.98. Effectively one WC axis
  plus one independent PL check.

---

## Feature-set history

Same 154 matches for every row (64 WC2022 + 104 WC2026, minus 14 extra-time).
Only the feature set varies.

| # | Feature set | #f | WC RMSE | Spearman | PL meanPct | COMBINED | pred↔goals |
|---|---|---|---|---|---|---|---|
| 0 | goals-only, baseline | 1 | 0.866 | 0.494 | 0.939 | 0.524 | 1.000 |
| 1 | all xG features | 22 | 0.878 | 0.485 | 0.872 | 0.481 | 0.809 |
| 2 | all xG + chasing | 26 | 0.870 | 0.488 | 0.907 | 0.505 | 0.791 |
| 3 | forward-selected 8 | 8 | 0.807 | 0.558 | 0.918 | 0.616 | 0.825 |
| 4 | + big chances | 8 | 0.805 | 0.565 | 0.915 | 0.620 | 0.814 |
| 5 | + final5_swing | 8 | 0.795 | 0.578 | 0.915 | 0.638 | 0.788 |
| 6 | + goal_lead_changes | 9 | 0.803 | 0.577 | 0.926 | 0.631 | 0.806 |
| 7 | + team strength, shipped | 11 | 0.693 | 0.723 | 0.928 | 0.827 | 0.681 |

**0. goals-only**: baseline. Best PL meanPct of any row (10 of the 12 nominated
matches are high-scoring). Second-worst RMSE and Spearman. Produces 120 distinct
scores across 154 matches, so every 0-0 gets the same value. Worst bucket is
low-rated matches, RMSE 1.024.

**1. all xG features**: every symmetric xG-window feature. Only row below
baseline. 22 correlated features on 154 rows.

**2. + chasing**: trailing-team pressure from reconstructed score state at each
shot. Slight gain, still baseline level.

**3. forward-selected 8**: greedy CV-RMSE selection. First clear win over
baseline.

**4. + big chances**: `big_chances_60-75`, count of xG ≥ 0.3 shots in the
60-75' window, replacing `xg_per_shot`. Marginal.

**5. + final5_swing**: count of lead changes in the final 5 minutes plus all
stoppage time, on a unified true-minute. Replaced a boolean "any lead change
after 75'". Won 28/30 CV seeds.

**6. + goal_lead_changes**: count of lead changes. +0.32 correlation alone,
collinear with total goals and late swings, lowers COMBINED.

**7. + team strength**: `avg_strength`, `gap_strength`, `upset`. FIFA world
ranking for WC, preseason expected finish for PL, both as within-competition
percentiles. Shipped.

---

## Shipped model

Features and standardised coefficients, by absolute size:

| Feature | Coef |
|---|---|
| total_goals | +0.395 |
| avg_strength | −0.352 |
| upset | +0.202 |
| chasing_xg | +0.200 |
| big_chances_60-75 | +0.180 |
| final5_swing_count | +0.145 |
| xg_absdiff_30-45 | +0.117 |
| xg_absdiff_75-90plus | −0.060 |
| xg_absdiff_0-15 | +0.048 |
| gap_strength | −0.039 |
| goal_diff_abs | −0.034 |

`avg_strength` negative: lower percentile = stronger teams.

---

## Findings

- Goal count explains ~22% of rating variance on WC data.
- Total xG nearly as predictive as goals. Rescues the high-chance 0-0.
- Margin matters more than goal count. Holding total goals fixed, matches decided
  by 1 goal or drawn rate well above those won by 2+:
  3 goals, 7.21 close vs 6.45 blowout; 4 goals, 7.67 vs 6.29; 5 goals, 7.73 vs
  6.62. A 2-2 beats a 3-1, and a 1-2 beats a 3-0.
- Chance imbalance early rates higher, imbalance late rates lower.
- A late goal that changes the result matters; a late goal that doesn't, doesn't.
- Team strength and upsets carry weight no xG feature captured.

---

## Rejected

| Tried | Result |
|---|---|
| XGBoost, LightGBM | lose to Ridge on CV and on transfer |
| Lasso, ElasticNet | tie or lose. Signal is dense and correlated |
| 50 pure-noise columns | CV RMSE 0.79 → 0.97 |
| Red cards | ~13 of 154 matches have one. Too few events. Opt-in only |
| goals − xG | collinear with total goals |
| lead changes | collinear with goals and late swings |
| plain late-goal count | collinear with total goals |

Binding constraint is data, not model class. The two changes that moved the
metric were more labels (21 knockout matches, 147 → 168) and one orthogonal
signal (team strength).

---

## Limitations

- No PL labels. Predictions unvalidated. Ranking more reliable than the score.
- Predictions compressed toward the mean.
- Domain shift: international tournament football → league football.
- `PL_EXPECTED` in `predict_excitingness.py` needs an entry per season. Missing
  teams fall back to mid-table strength. An Elo feed would remove this.
- WC2026 data here is a simulated tournament. Team-strength ratings are real.

---

## Files

| File | Purpose |
|---|---|
| `predict_excitingness.py` | scorer. `python3 predict_excitingness.py --fetch` |
| `nbs/isdb_excitingness_model.ipynb` | model, comparison table, optional experiments |
| `model_comparison_metrics.csv` | full table with extra diagnostics |
| `metric_progression.png` | progression chart |
| `pipeline/build_pl_frontend.py` | turns `outputs/pl_excitingness.csv` into the site's PL gameweek JSON |
| `nbs/train_final_model.ipynb` | fits *only* the shipped model above and saves it — run this, not the comparison notebook, when you just need to (re)produce `models/excitingness_model.joblib` |
| `models/excitingness_model.joblib` | the saved shipped model — bundles the fitted model, `FEATURE_SET`, and metadata (algorithm, training data, coefficients, when it was trained). Gitignored (`*.joblib`); regenerate with the notebook above. Load it with `predict_excitingness.py --model models/excitingness_model.joblib` to skip retraining |
