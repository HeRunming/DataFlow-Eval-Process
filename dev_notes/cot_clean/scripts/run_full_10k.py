"""
Full 10k CoT clean pipeline with per-batch checkpointing.
==========================================================

Runs A_fast + C_fast + D_fast on the whole
``dataflow_reasoningmath_10k.jsonl`` using the pooled Taiji serving.

Design
------
* The 10k rows are split into ``BATCH_SIZE``-row batches (default 500).
  Each batch is a single ``op.run()`` whose output is cached via
  ``FileStorage`` under ``cache_full/<method>/batch_XXX/``.  If the
  script is restarted, already-finished batches are skipped.
* A top-level manifest ``results_full/manifest.jsonl`` records one line
  per (method, batch) with wallclock, prompts, errors, retain stats.
* At the end, per-method outputs are concatenated into
  ``results_full/method_<M>_10k.jsonl`` — the file you'd hand off to
  training.
* The script prints an estimated wallclock before launching, based on
  the measured per-row times from the 30-row A/B
  (A_fast ≈ 6.6 s/row, D_fast ≈ 8.4 s/row, C_fast ≈ 9.2 s/row).
  The estimate assumes current Gemini capacity; scale up if the load
  shifts.

Usage
-----
    export APP_ID=... APP_KEY=...
    python run_full_10k.py --dry-run    # print plan + estimate, do nothing
    python run_full_10k.py              # actually launch
    python run_full_10k.py --methods A,C  # subset of methods
    python run_full_10k.py --batch-size 250
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

_HERE = Path(__file__).parent
sys.path.insert(0, str(Path("/data/workspace/cc_workspace/DataFlow").resolve()))

import pandas as pd

from dataflow.operators.reasoning import (
    CoTChunkCompressRefinerFast,
    CoTLLMJudgeRefinerFast,
    CoTPatternRefinerFast,
)
from dataflow.serving import TaijiCustomLLMServing_pool
from dataflow.utils.storage import FileStorage


# ─── Config ─────────────────────────────────────────────────────────────────

DATA_PATH = _HERE / "dataflow_reasoningmath_10k.jsonl"
CACHE_ROOT = _HERE / "cache_full"
RESULTS_DIR = _HERE / "results_full"
MANIFEST = RESULTS_DIR / "manifest.jsonl"

MODEL_MARKER = os.environ.get(
    "FULL_MODEL_MARKER",
    "api_naci_default_gemini-3.1-flash-lite-preview",
)
MAX_WORKERS = int(os.environ.get("FULL_MAX_WORKERS", "100"))
THINKING_LEVEL = os.environ.get("FULL_THINKING_LEVEL", "high")

# Measured per-row times from the latest re-runs (v2 / v3 on 30 rows):
#   * A_fast  6.6 s/row (v1, unchanged)
#   * C_fast 10.2 s/row (v2)
#   * D_fast 10.6 s/row (v3 sampled-mixed UNN_EXPL, slower than v2 because
#     compressed summaries went from 1 sentence to 2-3 sentences).
# Conservative x1.2 safety margin for 10k because longer total wallclock
# sees more tail-latency outliers.
_SEC_PER_ROW = {"A": 6.6 * 1.2, "C": 10.2 * 1.2, "D": 10.6 * 1.2}


# ─── Input preparation (once) ───────────────────────────────────────────────

def prepare_full_input() -> Path:
    """Write a normalised jsonl (problem + cot columns) ready for DataFlow."""
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    out = CACHE_ROOT / "input_10k.jsonl"
    if out.exists():
        return out
    rows = []
    with open(DATA_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    df = pd.DataFrame(rows)
    if "output" in df.columns:
        df = df.rename(columns={"output": "cot"})
    if "instruction" in df.columns and "input" in df.columns:
        df["problem"] = (
            df["instruction"].fillna("").str.strip()
            + "\n"
            + df["input"].fillna("").str.strip()
        )
    elif "input" in df.columns:
        df["problem"] = df["input"]
    else:
        df["problem"] = ""
    df.to_json(out, orient="records", lines=True, force_ascii=False)
    return out


def split_into_batches(input_file: Path, batch_size: int) -> list[Path]:
    """Materialise ``input_file`` into ``cache_full/batches/batch_XXX.jsonl``.

    Idempotent: if the batches already exist with the right count, reuse them.
    """
    batch_dir = CACHE_ROOT / "batches"
    batch_dir.mkdir(parents=True, exist_ok=True)
    df = pd.read_json(input_file, lines=True)
    n_batches = (len(df) + batch_size - 1) // batch_size
    paths: list[Path] = []
    for b in range(n_batches):
        p = batch_dir / f"batch_{b:04d}.jsonl"
        if not p.exists():
            sub = df.iloc[b * batch_size : (b + 1) * batch_size]
            sub.to_json(p, orient="records", lines=True, force_ascii=False)
        paths.append(p)
    return paths


# ─── Operator factory ───────────────────────────────────────────────────────

def build_serving() -> TaijiCustomLLMServing_pool:
    return TaijiCustomLLMServing_pool(
        model_marker=MODEL_MARKER,
        max_workers=MAX_WORKERS,
        max_retries=5,
        enable_thinking=True,
        thinking_level=THINKING_LEVEL,
        retain_thinking=False,
    )


def build_op(method: str, serving):
    if method == "A":
        return CoTLLMJudgeRefinerFast(
            llm_serving=serving,
            min_steps_to_keep=2,
            min_chars_to_clean=2000,
            min_step_chars=400,
        )
    if method == "C":
        return CoTChunkCompressRefinerFast(
            llm_serving=serving,
            num_candidates=1,
            min_chunk_tokens=30,
            min_chars_to_clean=2000,
        )
    if method == "D":
        # v3 sampled-mixed UNN_EXPL: half of UNN_EXPL fragments kept
        # verbatim (preserves R1's native exploration language), the
        # other half replaced with 2-3 sentence trace summaries that
        # include the actual equation/attempt and the specific failure
        # point.  See run_v3_recheck.py for the validation.
        return CoTPatternRefinerFast(
            llm_serving=serving,
            preset="balanced",
            unn_expl_keep_ratio=0.5,
            sampling_seed=0xC07C,
            min_fragments_to_keep=2,
            min_chars_to_clean=2000,
            min_fragment_chars=400,
        )
    raise ValueError(f"unknown method {method!r}")


# ─── Manifest bookkeeping ───────────────────────────────────────────────────

def load_manifest() -> set[tuple[str, int]]:
    """Return set of (method, batch_idx) already completed."""
    done: set[tuple[str, int]] = set()
    if MANIFEST.exists():
        with open(MANIFEST) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                    if rec.get("status") == "ok":
                        done.add((rec["method"], rec["batch_idx"]))
                except Exception:
                    continue
    return done


def append_manifest(record: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with open(MANIFEST, "a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── Per-batch runner ───────────────────────────────────────────────────────

def run_batch(
    method: str,
    batch_idx: int,
    batch_file: Path,
    serving,
) -> dict:
    """Run one operator × batch, return a manifest record."""
    tag_dir = CACHE_ROOT / method / f"batch_{batch_idx:04d}"
    tag_dir.mkdir(parents=True, exist_ok=True)
    storage = FileStorage(
        first_entry_file_name=str(batch_file),
        cache_path=str(tag_dir),
        file_name_prefix="dataflow_cache",
        cache_type="jsonl",
    )
    storage.step()

    op = build_op(method, serving)
    t0 = time.time()
    try:
        op.run(
            storage=storage,
            input_key="cot",
            output_key="cot_cleaned",
            output_stats_key="cot_clean_stats",
            problem_key="problem",
        )
        elapsed = time.time() - t0
        out_file = tag_dir / "dataflow_cache_step1.jsonl"
        # Quick aggregate on the output
        df = pd.read_json(out_file, lines=True)
        rets = []
        errs = skipped = 0
        for _, r in df.iterrows():
            try:
                s = json.loads(r["cot_clean_stats"])
            except Exception:
                continue
            if s.get("skipped"):
                skipped += 1
                continue
            if s.get("error"):
                errs += 1
                continue
            if "original_chars" in s and s["original_chars"]:
                rets.append(s["output_chars"] / s["original_chars"])
        return {
            "method":    method,
            "batch_idx": batch_idx,
            "rows":      len(df),
            "wall_s":    round(elapsed, 1),
            "skipped":   skipped,
            "errors":    errs,
            "avg_retain": round(sum(rets) / len(rets), 4) if rets else None,
            "status":    "ok",
            "out_file":  str(out_file),
        }
    except Exception as exc:
        return {
            "method":    method,
            "batch_idx": batch_idx,
            "rows":      0,
            "wall_s":    round(time.time() - t0, 1),
            "status":    "error",
            "error":     str(exc),
        }


# ─── Final concatenation ────────────────────────────────────────────────────

def concat_per_method(method: str, n_batches: int) -> Path:
    parts = []
    for b in range(n_batches):
        f = CACHE_ROOT / method / f"batch_{b:04d}" / "dataflow_cache_step1.jsonl"
        if f.exists():
            parts.append(pd.read_json(f, lines=True))
    if not parts:
        raise RuntimeError(f"no output batches found for method {method}")
    merged = pd.concat(parts, ignore_index=True)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"method_{method}_10k.jsonl"
    merged.to_json(out, orient="records", lines=True, force_ascii=False)
    return out


# ─── Main ───────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--methods", default="A,C,D",
                    help="comma-separated subset of A,C,D")
    ap.add_argument("--batch-size", type=int, default=500)
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan + estimate only")
    args = ap.parse_args()

    methods = [m.strip().upper() for m in args.methods.split(",") if m.strip()]
    for m in methods:
        if m not in {"A", "C", "D"}:
            raise SystemExit(f"unknown method {m!r}")

    input_file = prepare_full_input()
    n_rows = sum(1 for _ in open(input_file))
    batches = split_into_batches(input_file, args.batch_size)
    n_batches = len(batches)

    # Estimate
    est_hours = {}
    total_est = 0.0
    for m in methods:
        sec = _SEC_PER_ROW[m] * n_rows
        est_hours[m] = sec / 3600
        total_est += sec / 3600

    print("=" * 72)
    print(f"  Full 10k pipeline plan")
    print("=" * 72)
    print(f"  input rows   : {n_rows}")
    print(f"  batch size   : {args.batch_size}  (-> {n_batches} batches)")
    print(f"  methods      : {methods}")
    print(f"  model        : {MODEL_MARKER}")
    print(f"  workers      : {MAX_WORKERS}")
    print(f"  thinking     : high")
    print(f"  estimate:")
    for m, h in est_hours.items():
        print(f"    method {m}: ~{h:.1f} h  (at {_SEC_PER_ROW[m]:.1f} s/row)")
    print(f"    TOTAL     : ~{total_est:.1f} h (sequential)")
    print(f"  cache root   : {CACHE_ROOT}")
    print(f"  results dir  : {RESULTS_DIR}")
    print(f"  manifest     : {MANIFEST}")
    print("=" * 72)

    # Resume support
    done = load_manifest()
    if done:
        print(f"  manifest shows {len(done)} already-done (method, batch) cells, "
              f"these will be skipped.")

    if args.dry_run:
        print("\n  [dry-run] not launching; rerun without --dry-run to start.")
        return

    serving = build_serving()
    try:
        for m in methods:
            for b_idx, batch_file in enumerate(batches):
                if (m, b_idx) in done:
                    continue
                print(
                    f"\n  === {m} / batch {b_idx + 1}/{n_batches} "
                    f"({batch_file.name}) ==="
                )
                rec = run_batch(m, b_idx, batch_file, serving)
                append_manifest(rec)
                print(f"    -> {rec}")
            # Concatenate on the fly after each method finishes.
            try:
                out = concat_per_method(m, n_batches)
                print(f"\n  concatenated method {m}: {out}")
            except Exception as exc:
                print(f"  (concat for method {m} failed: {exc})")
    finally:
        try:
            serving.cleanup()
        except Exception:
            pass


if __name__ == "__main__":
    main()
