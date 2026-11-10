"""Bundle the last N days of what the bot saved into one zip, for a review of how it is doing.

Everything under data/ (every book's trades, equity, ideas, decisions, Bub's thoughts and audit,
the learning journal and lessons, readouts, trading-mode changes), config/, and the Bub knob file
~/.ctbot/tuning.json. JSONL logs keep only rows from the window; a file with a date in its name
(journal/2026-11-03.json) is kept only if that date is in the window; other files are current
state and kept whole. Price caches and backtest bars are left out. Read-only.

Keys stay out: no .env, no ~/.ctbot/settings.json or sign-in tokens, nothing named like a token
or secret, and anything that still looks like a key inside a kept file is replaced by [redacted].

    .venv/bin/ctbot-export                     # ctbot-export-<today>.zip, last 14 days
    .venv/bin/ctbot-export --days 30 --out -   # zip on stdout

Standard library only, so it also runs before a deploy: ssh server 'cd ctbot && .venv/bin/python
- --out -' < src/ctbot/export.py > export.zip
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

SKIP_DIRS = {"bars", "bars_cache", "backtests", "options_flow", "options_flow_tradingflow",
             "house_ptr", "astral", "events"}  # caches and fixtures, not decisions
TEXT = {".json", ".jsonl", ".md", ".csv", ".txt", ".toml"}
SECRET_NAME = re.compile(r"oauth|token|secret|credential|password|settings\.json|\.env", re.I)
SECRET_VALUE = [
    (re.compile(r"\b(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[a-z]-[A-Za-z0-9-]{10,})"),
     "[redacted]"),
    (re.compile(r'("?[A-Za-z_]*(?:api_?key|token|secret|password|authorization)"?\s*[:=]\s*)"[^"]*"',
                re.I), r'\1"[redacted]"'),
]
DATE_IN_NAME = re.compile(r"(\d{4}-\d{2}-\d{2})")
MAX_BYTES = 50_000_000


def _redact(text: str) -> str:
    for pattern, repl in SECRET_VALUE:
        text = pattern.sub(repl, text)
    return text


def _row_day(line: str) -> str | None:
    try:
        row = json.loads(line)
    except ValueError:
        return None
    if not isinstance(row, dict):
        return None
    for key in ("ts", "time", "date", "day"):
        v = row.get(key)
        if isinstance(v, str) and re.match(r"\d{4}-\d{2}-\d{2}", v):
            return v[:10]
    return None


def build(root: Path, days: int = 14, home: Path | None = None, now: datetime | None = None) -> bytes:
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(days=days)).date().isoformat()
    sources = [(p, p.relative_to(root).as_posix()) for top in ("data", "config") if (root / top).is_dir()
               for p in sorted((root / top).rglob("*")) if p.is_file()]
    tuning = (home or Path.home()) / ".ctbot" / "tuning.json"
    if tuning.is_file():
        sources.append((tuning, "home/.ctbot/tuning.json"))

    manifest: dict = {"generated": now.isoformat(timespec="seconds"), "days": days, "since": cutoff,
                      "files": {}, "skipped": {}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, arc in sources:
            parts = arc.split("/")
            if SKIP_DIRS & set(parts[1:-1]):
                continue
            if SECRET_NAME.search(path.name):
                manifest["skipped"][arc] = "may hold a key"
                continue
            if path.suffix not in TEXT:
                continue
            if path.suffix != ".jsonl" and path.stat().st_size > MAX_BYTES:
                manifest["skipped"][arc] = f"over {MAX_BYTES // 1_000_000} MB"
                continue
            dated = DATE_IN_NAME.search(path.name)
            if dated and dated.group(1) < cutoff:
                continue
            text = path.read_text(errors="replace")
            if path.suffix == ".jsonl":
                lines = text.splitlines()
                kept = [ln for ln in lines if ln.strip() and (_row_day(ln) or cutoff) >= cutoff]
                if not kept:
                    continue
                text = "\n".join(kept) + "\n"
                if len(text) > MAX_BYTES:  # a log is judged by the days kept, not its whole life
                    manifest["skipped"][arc] = f"over {MAX_BYTES // 1_000_000} MB"
                    continue
                manifest["files"][arc] = {"rows": len(kept), "of": len(lines)}
            else:
                manifest["files"][arc] = {"bytes": len(text)}
            z.writestr(arc, _redact(text))
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--root", type=Path, default=Path("."), help="the ctbot checkout (default: here)")
    ap.add_argument("--out", help="zip path, or - for stdout (default: ctbot-export-<today>.zip)")
    a = ap.parse_args(argv)
    data = build(a.root.resolve(), a.days)
    if a.out == "-":
        sys.stdout.buffer.write(data)
        return 0
    out = Path(a.out or f"ctbot-export-{datetime.now(UTC).date().isoformat()}.zip")
    out.write_bytes(data)
    print(f"wrote {out} ({len(data) // 1024} KB, last {a.days} days)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
