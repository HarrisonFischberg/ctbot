# Qlib evaluation for CTBOT (commit 54355232, clone /home/claude/refs/qlib)

## Verdict
Don't add qlib as a runtime dependency. Treat it as a reference and port small, well-defined pieces as
pure pandas (MIT allows this with attribution). The full stack is heavy: a 1.1 GB venv with 192 packages,
mlflow, cvxpy and gym. It defaults to China-market settings, and its bundled data is disabled or stale.

## License / packaging
- MIT (`LICENSE`, Copyright Microsoft). Porting code is fine if we keep the copyright notice in ported files.
- `pyproject.toml`: requires-python >=3.8. A prebuilt cp311 wheel exists (`pyqlib==0.9.7` on PyPI; the clone is newer main).
  Cython extensions: `qlib/data/_libs/rolling.pyx`, `expanding.pyx`.
- The install pulls mlflow 3.17, cvxpy, gym 0.26 (unmaintained, warns under NumPy 2), lightgbm 4.7, jupyter bits,
  fastapi/uvicorn and redis-lock. Torch, xgboost and catboost are optional and their models are skipped
  if those packages are missing.

## Smoke test (all offline, synthetic data, ~15 s runtime)
Env: `uv venv -p /usr/bin/python3.11 /home/claude/refs/envs/qlib && uv pip install -p .../bin/python pyqlib` (26 s).
Resolved: pyqlib 0.9.7, pandas 3.0.6, numpy 2.4.6, lightgbm 4.7.0, mlflow 3.17.1.
Files: /home/claude/refs/qlib_smoke/{gen.py,smoke.py,csv/,qdata/}
1. `gen.py`: generated synthetic GBM random-walk OHLCV+vwap+factor for 30 symbols (SYN00..29) × 650 business days
   (2022-01-03..2024-06-28). **Mocked:** all market data (no CN/US dataset downloaded).
2. `python /home/claude/refs/qlib/scripts/dump_bin.py dump_all --data_path csv --qlib_dir qdata --freq day
   --date_field_name date --symbol_field_name symbol --include_fields open,high,low,close,volume,factor,vwap`
   finished in 5.7 s. Output: calendars/day.txt (650 lines), instruments/all.txt, features/*/*.day.bin.
3. `smoke.py` (`qlib.init(provider_uri="./qdata", region=REG_US)`):
   - Expression engine `D.features(...)` with Ref/Mean/Std/Corr/Log/Rank: OK, frame (40, 6).
   - `Alpha158` handler + `DatasetH` (train 2022-03..2023-06, valid 2023-H2, test 2024-H1): train frame (10470, 159),
     158 features + LABEL0.
   - `LGBModel` (50 rounds, early stop) fit/predict: OK, 3870 test preds. Test RankIC mean 0.0085 (≈0, as expected on noise).
   - `LinearModel(estimator="ridge")`: OK.
   - `backtest()` with `TopkDropoutStrategy(topk=5,n_drop=1)` + `SimulatorExecutor`, close fills, 5/15 bps costs,
     benchmark=SYN00, 128 days: OK at ~135 it/s.
   - `risk_analysis` on excess-minus-cost returns: ann_ret 0.25, IR 1.09, MDD -0.11. This is noise and means nothing.
     On a pure synthetic N(0.0005, 0.01) series it gave IR -0.83 (that random draw happened to be negative).
- **Breakage found:** with mlflow 3.17, `LGBModel.fit` crashes. qlib's default `./mlruns` FileStore is now refused
  ("filesystem tracking backend is in maintenance mode"). Workaround: `qlib.init(..., exp_manager={"class":
  "MLflowExpManager","module_path":"qlib.workflow.expm","kwargs":{"uri":"sqlite:///mlflow.db",...}})`
  or `MLFLOW_ALLOW_FILE_STORE=true`. The recorder also shells out to `git diff`, which prints noise outside a repo.
  This shows qlib's mlflow coupling breaks even for plain model fitting.

## Review
- **Intraday:** a 1min frequency exists (`qlib/utils/time.py` has US_TIME 9:30-16:00, so US minute calendars
  are supported). Handlers are in `qlib/contrib/data/highfreq_{handler,processor,provider}.py` with examples in `examples/highfreq/`.
  The Yahoo collector (`scripts/data_collector/yahoo/collector.py`) can only fetch the last month of 1min data.
  Design is offline batch: bins are rebuilt or appended via `dump_bin.py dump_update` (`DumpDataUpdate`).
  There is no streaming or real-time feed, so it's a poor fit for intraday freshness.
- **Data freshness:** README:233 says the official dataset is "disabled temporarily". README:282 says the offline
  bundle can't be incrementally updated, so you must re-collect from Yahoo. Treat all qlib-hosted data as unavailable.
- **Pipeline:** `scripts/dump_bin.py` turns CSV/parquet into a per-field float32 `.bin` plus calendars and instruments.
  The provider config and cache are in `qlib/config.py` and `qlib/data/data.py` (Local*Provider). Collectors live in `scripts/data_collector/*`.
- **Features:** Alpha158 formulas are in `qlib/contrib/data/loader.py:61` (`Alpha158DL.get_feature_config`).
  The handler/label is at `qlib/contrib/data/handler.py:98`. The label is `Ref($close,-2)/Ref($close,-1)-1`
  (T+1 to T+2 return, which avoids lookahead from same-bar fills).
  Groups: KBAR(9), price ratios, ROC/MA/STD/BETA/RSQR/RESI/MAX/MIN/QTLU/QTLD/RANK/RSV/IMAX/IMIN/IMXD/CORR/CORD/
  CNTP/CNTN/CNTD/SUMP/SUMN/SUMD/VMA/VSTD/WVMA/VSUMP/VSUMN/VSUMD, each over windows 5/10/20/30/60.
  Alpha360 is at `loader.py:4`: raw 60-day lags of OHLCV/vwap normalized by the current close.
  Operators are in `qlib/data/ops.py`.
  The default handler processors are RobustZScoreNorm, Fillna and CSRankNorm on the label (`handler.py`).
- **Models:** `qlib/contrib/model/gbdt.py:16` (LGBModel), `linear.py:17` (OLS/ridge/lasso/NNLS), xgboost, catboost,
  double_ensemble and about 25 pytorch_* models (LSTM, GRU, Transformer, TRA, HIST...).
  All of them are coupled to `DatasetH` and the `R` recorder.
- **Portfolio:** `qlib/contrib/strategy/signal_strategy.py:74` `TopkDropoutStrategy`
  (topk, n_drop, method_sell/buy, hold_thresh, forbid_all_trade_at_limit, which is a China limit-up/down concept).
  The same file has WeightStrategyBase and EnhancedIndexingStrategy (cvxpy optimizer, `qlib/contrib/strategy/optimizer/`).
  The backtest engine is `qlib/backtest/` (exchange, executor, account, position).
- **Evaluation:** `qlib/contrib/evaluate.py:26` `risk_analysis` returns mean, std, annualized_return, information_ratio
  and max_drawdown. **Caveat:** `freq="day"` annualizes with 238 (China trading days) and minute uses 240×238.
  For US markets, pass `N=252` (or 252×390 for minute data). It uses no risk-free rate, and the default is arithmetic (mode="sum").
  `indicator_analysis` (same file) covers fill/price-advantage metrics.
- **Walk-forward:** `qlib/workflow/task/gen.py:142` `RollingGen(step=40, rtype=ROLL_EX expanding | ROLL_SD sliding)`.
  `qlib/contrib/rolling/base.py:24` `Rolling` (offline rolling retrain) and `ddgda.py` (meta-learning data reweighting)
  build on it. Online serving is in `qlib/workflow/online/`. It depends heavily on task configs, mlflow and the recorder.

## What is realistic to reuse in CTBOT (ranked value / effort / risk)
1. **Port about 20-40 Alpha158 formulas as pure pandas** (KBAR, ROC, MA, STD, MAX/MIN, QTLU/QTLD, RSV, CORR/CORD,
   CNTP/CNTN, SUMP/SUMN, VMA/VSTD/WVMA). Value: high. Effort: low (1-2 days; each is a groupby-rolling one-liner).
   Risk: low. Use qlib's formula strings as the spec. Golden-test our pandas output against `D.features` on the same synthetic bins
   using this venv. Keep the T+1 to T+2 label convention.
2. **Port the TopkDropout rebalance logic** (about 80 lines in `generate_trade_decision`: rank by score, keep holdings,
   sell the n_drop worst, buy the best non-held to refill topk, respect hold_thresh). Value: high (turnover control).
   Effort: low. Risk: low. Drop the China limit-up/down handling.
3. **Fix the risk_analysis math** (mean/std/IR/MDD; about 30 lines) with N=252 and optional geometric mode, plus daily
   RankIC/ICIR. Value: medium. Effort: trivial. Risk: none.
4. **Cross-sectional ranking model with walk-forward**: re-implement the RollingGen idea (expanding or sliding window,
   step N days, a gap ≥ label horizon) around plain LightGBM or ridge, with CSRank/robust-zscore preprocessing.
   Value: medium-high. Effort: medium (3-5 days with leakage tests). Risk: medium (overfitting and leakage; needs a
   purged/embargoed split). Add `lightgbm` only (~5 MB wheel). Don't add pyqlib.
5. **Use qlib offline as a research harness** in this separate venv (dump CTBOT's cached daily bars via dump_bin, run
   Alpha158+LGB+TopkDropout backtests). Value: medium for research. Effort: low. Risk: low because it's isolated.
   Watch the 238-day scaler and mlflow FileStore breakage, and keep it out of CTBOT's dependency tree.
6. **Not recommended:** taking qlib as a runtime dependency, its intraday/high-freq stack, RL execution, the pytorch
   models, or its online serving. The weight (1.1 GB, mlflow/gym/cvxpy) and China-centric defaults bring
   pandas/numpy/mlflow version-drift risk (seen in this smoke test) without paper-trading benefit.

No trades, accounts, credentials or network data sources were used. /home/claude/ctbot was not modified.
