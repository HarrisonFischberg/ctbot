"""Append-only research trial registry with a locked final holdout.

Every backtest trial (kept or rejected) is one JSON line in data/research/trials.jsonl, chained
by hash so silent edits or deletions show up (`verify()`). Each line is a run card: when, what
(experiment, strategy, version, params), on which data (fingerprint, periods, split), with which
code (git revision) and the results. The number of trials per experiment feeds the Deflated
Sharpe Ratio, so rejected experiments stay visible and keep counting against the winner.

Holdout rule: the final holdout period may be scored once per (experiment, strategy, params).
A second attempt is refused unless forced, and a forced re-run is recorded as such. Selection
(choosing strategies or parameters) must only use the "dev" and "validation" splits.

Ideas from Vibe-Trading's run cards and hash-chained governance ledger (MIT); code is CTbot's.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_PATH = Path("data/research/trials.jsonl")
GENESIS = "0" * 64
SPLITS = ("dev", "validation", "holdout", "full")


class HoldoutLocked(RuntimeError):
    pass


def code_version() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5)
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               capture_output=True, text=True, timeout=5)
        rev = out.stdout.strip() or "unknown"
        return rev + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _digest(record: dict[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


@dataclass
class Registry:
    path: Path = DEFAULT_PATH

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def _last_hash(self) -> str:
        recs = self.records()
        return recs[-1]["hash"] if recs else GENESIS

    def record(self, *, experiment: str, strategy: str, params: dict[str, Any], split: str,
               periods: dict[str, str], metrics: dict[str, Any], data_fingerprint: str,
               version: str = "1", notes: str = "", force_holdout: bool = False,
               now: datetime | None = None) -> dict[str, Any]:
        if split not in SPLITS:
            raise ValueError(f"split must be one of {SPLITS}")
        ph = params_hash(params)
        if split == "holdout" and self.holdout_used(experiment, strategy, ph) and not force_holdout:
            raise HoldoutLocked(f"holdout already scored for {experiment}/{strategy}/{ph}")
        rec = {
            "ts": (now or datetime.now(UTC)).isoformat(),
            "experiment": experiment, "strategy": strategy, "version": version,
            "params": params, "params_hash": ph, "split": split, "periods": periods,
            "data_fingerprint": data_fingerprint, "code_version": code_version(),
            "metrics": metrics, "notes": notes,
            "holdout_rerun": bool(split == "holdout" and force_holdout and self.holdout_used(experiment, strategy, ph)),
            "prev_hash": self._last_hash(),
        }
        rec["hash"] = _digest(rec)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a") as fh:
            fh.write(json.dumps(rec, sort_keys=True, default=str) + "\n")
        return rec

    def holdout_used(self, experiment: str, strategy: str, ph: str) -> bool:
        return any(r["split"] == "holdout" and r["experiment"] == experiment and r["strategy"] == strategy
                   and r["params_hash"] == ph for r in self.records())

    def trial_count(self, experiment: str) -> int:
        """Distinct (strategy, params) trials ever run in an experiment: the DSR's n_trials."""
        return len({(r["strategy"], r["params_hash"]) for r in self.records() if r["experiment"] == experiment})

    def verify(self) -> bool:
        prev = GENESIS
        for r in self.records():
            if r.get("prev_hash") != prev or _digest(r) != r.get("hash"):
                return False
            prev = r["hash"]
        return True
