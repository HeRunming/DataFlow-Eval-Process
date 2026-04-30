"""
Progress probe for the 10k full-run pipeline.

Reads results_full/manifest.jsonl to count completed (method, batch) cells,
and tail-parses logs/full_10k.log to show the currently-running batch's
tqdm progress.

Usage: python probe_full_status.py
"""
from __future__ import annotations
import json
import re
import sys
import time
from pathlib import Path

ROOT = Path("/data/workspace/cc_workspace/cot_clean_test")
LOG = ROOT / "logs/full_10k.log"
MANIFEST = ROOT / "results_full/manifest.jsonl"
TOTAL_BATCHES = 20
METHODS = ("A", "C", "D")


def _last_tqdm(text: str) -> str:
    last = ""
    for part in text.replace("\r", "\n").split("\n"):
        part = part.strip()
        if part and "Generating responses" in part:
            last = part
    return last


def main():
    if not LOG.exists():
        sys.exit(f"log not found: {LOG}")

    # Parse manifest — one line per finished batch.
    done_rows: list[dict] = []
    if MANIFEST.exists():
        for line in MANIFEST.read_text().splitlines():
            try:
                done_rows.append(json.loads(line))
            except Exception:
                pass
    done_keys = {(r["method"], r["batch_idx"]): r for r in done_rows}

    print("=" * 72)
    print(f"10k full-run status -- log: {LOG.name}")
    print("=" * 72)

    # Per-method table
    total_wall = 0.0
    total_rows = 0
    for m in METHODS:
        batches_done = [r for r in done_rows if r["method"] == m and r.get("status") == "ok"]
        batches_err  = [r for r in done_rows if r["method"] == m and r.get("status") == "error"]
        n_done = len(batches_done)
        wall = sum(r.get("wall_s", 0) for r in batches_done)
        n_rows = sum(r.get("rows", 0) for r in batches_done)
        retains = [r["avg_retain"] for r in batches_done if r.get("avg_retain") is not None]
        retain = sum(retains)/len(retains) if retains else None
        errs = sum(r.get("errors", 0) for r in batches_done)
        print(
            f"  {m}: {n_done:>2}/{TOTAL_BATCHES} batches  "
            f"{n_rows:>5} rows   wall={wall:>6.0f}s   "
            f"retain={f'{retain:.1%}' if retain else '-':>6}   "
            f"errors={errs}"
            + (f"   [{len(batches_err)} FAILED]" if batches_err else "")
        )
        total_wall += wall
        total_rows += n_rows
    print(f"  TOTAL: {total_rows} row-runs, {total_wall:.0f}s = {total_wall/3600:.2f} h")

    # Currently running batch — parse the tail of the log for the latest
    # "=== X / batch N/M ===" banner.
    text = LOG.read_text(errors="ignore")
    running = re.findall(
        r"=== ([ACD]) / batch (\d+)/(\d+) \(batch_(\d+)\.jsonl\) ===",
        text,
    )
    if running:
        m, cur, total, bi = running[-1]
        # Has this batch been recorded as done?  If so, nothing is running.
        if (m, int(bi) - (0 if cur == str(int(bi) + 1) else 0)) not in done_keys:
            last_tq = _last_tqdm(text.split(f"=== {m} / batch {cur}/{total}")[-1])
            prog_m = re.search(r"(\d+)/(\d+)", last_tq or "")
            prog = f"  {prog_m.group(1)}/{prog_m.group(2)} prompts" if prog_m else ""
            print(f"\n  currently running: {m} batch {cur}/{total}{prog}")

    # Manifest tail — last 3 completed batches with timing.
    if done_rows:
        print("\n  last 3 manifest entries:")
        for r in done_rows[-3:]:
            ts = r.get("wall_s", 0)
            print(
                f"    {r['method']} batch {r['batch_idx']:>2}  "
                f"rows={r.get('rows',0):>4}  wall={ts:>6.1f}s  "
                f"retain={f'{r.get("avg_retain"):.1%}' if r.get('avg_retain') is not None else '-'}  "
                f"errors={r.get('errors',0)}  "
                f"status={r.get('status')}"
            )

    # Projected total wallclock at current speed.
    if total_rows > 0:
        rate_s_per_row = total_wall / total_rows
        remaining_rows = 3 * 10000 - total_rows   # 3 methods
        print(
            f"\n  observed rate: {rate_s_per_row:.1f} s/row  "
            f"remaining: {remaining_rows} row-runs  "
            f"=> ~{remaining_rows * rate_s_per_row / 3600:.1f} h left"
        )


if __name__ == "__main__":
    main()
