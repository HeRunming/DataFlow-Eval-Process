"""
Fast CoT refiner A/B validation — Taiji custom-protocol edition
================================================================

Compares the reference and Fast variants of Methods A / C / D on a
configurable slice of ``dataflow_reasoningmath_10k.jsonl``, using the
Taiji HMAC-SHA1 custom protocol for LLM calls.

Reports, per variant:
  * total wallclock
  * number of ``generate_from_input`` calls + total prompts submitted
  * per-prompt latency P50 / P95 (measured inside a serving wrapper)
  * error count (empty / None responses)
  * avg compression ratio + mean char length before/after

Required environment (export before running):
  APP_ID    — Taiji APP_ID
  APP_KEY   — Taiji APP_KEY

Optional env overrides:
  AB_N_ROWS         — number of rows to use (default 500)
  AB_MAX_WORKERS    — concurrency cap (default 100, the Taiji limit)
  AB_MODEL_MARKER   — model marker (default Gemini 3.1 flash-lite)
  AB_METHODS        — comma-separated method letters (default "A,C,D")
  AB_VARIANTS       — comma-separated variants (default "old,fast")

Run with:
    conda activate dataflow
    export APP_ID=...   APP_KEY=...
    python run_fast_ab_validation.py
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any

# Make sure the cc_workspace DataFlow (with the Fast operators + the
# pooled Taiji serving) wins over any system install.
_CC_DATAFLOW = Path("/data/workspace/cc_workspace/DataFlow").resolve()
sys.path.insert(0, str(_CC_DATAFLOW))

import pandas as pd

from dataflow.serving import (
    TaijiCustomLLMServing,
    TaijiCustomLLMServing_pool,
)
from dataflow.utils.storage import FileStorage
from dataflow.operators.reasoning import (
    CoTChunkCompressRefiner,
    CoTChunkCompressRefinerFast,
    CoTLLMJudgeRefiner,
    CoTLLMJudgeRefinerFast,
    CoTPatternRefiner,
    CoTPatternRefinerFast,
)

# ─── Config ─────────────────────────────────────────────────────────────────
_HERE       = Path(__file__).parent
DATA_PATH   = _HERE / "dataflow_reasoningmath_10k.jsonl"
CACHE_ROOT  = _HERE / "cache_ab_validation"
RESULTS_DIR = _HERE / "results_ab_validation"

MODEL_MARKER = os.environ.get(
    "AB_MODEL_MARKER",
    "api_naci_default_gemini-3.1-flash-lite-preview",
)
# Taiji limit: max 100 concurrent connections.
MAX_WORKERS = int(os.environ.get("AB_MAX_WORKERS", "100"))
N_ROWS      = int(os.environ.get("AB_N_ROWS", "500"))
THINKING_LEVEL = os.environ.get("AB_THINKING_LEVEL", "high")
ENABLE_THINKING = os.environ.get("AB_ENABLE_THINKING", "true").lower() == "true"

METHODS     = os.environ.get("AB_METHODS",  "A,C,D").split(",")
VARIANTS    = os.environ.get("AB_VARIANTS", "old,fast").split(",")

# The default A/B matrix in run order.  Each entry is
# ``(variant, method, n_rows_override_or_None)``.
#
# For finer control, set ``AB_CELLS`` to a semicolon-separated list of
# ``variant:method[:rows]`` triples.  Example:
#     AB_CELLS="old:A:30;fast:A:30;fast:C:30;old:D:30;fast:D:30"
# Cells with an explicit rows value use that; cells without fall back to
# ``AB_N_ROWS``.
_cells_env = os.environ.get("AB_CELLS", "").strip()
if _cells_env:
    METHODS_TO_RUN: list[tuple[str, str, int]] = []
    for chunk in _cells_env.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = chunk.split(":")
        if len(parts) < 2 or len(parts) > 3:
            raise SystemExit(f"bad AB_CELLS entry: {chunk!r}")
        v, m = parts[0], parts[1]
        r = int(parts[2]) if len(parts) == 3 else N_ROWS
        METHODS_TO_RUN.append((v, m, r))
else:
    METHODS_TO_RUN = [(v, m, N_ROWS) for v in VARIANTS for m in METHODS]


# ═══════════════════════════════════════════════════════════════════════════
# Counting wrapper                                                           #
# ═══════════════════════════════════════════════════════════════════════════


class CountingServingWrapper:
    """Delegates to ``inner`` while recording per-call and per-prompt stats."""

    def __init__(self, inner: Any):
        self.inner = inner
        self.call_count = 0
        self.prompt_count = 0
        self.error_count = 0
        self.call_records: list[tuple[float, int]] = []
        self.per_prompt_latencies: list[float] = []

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def generate_from_input(self, user_inputs, *args, **kwargs):
        start = time.time()
        results = self.inner.generate_from_input(user_inputs, *args, **kwargs)
        elapsed = time.time() - start
        self.call_count += 1
        n = len(user_inputs)
        self.prompt_count += n
        self.call_records.append((elapsed, n))
        if n > 0:
            per = elapsed / n
            self.per_prompt_latencies.extend([per] * n)
        # Taiji returns "" (not None) on failure.
        self.error_count += sum(1 for r in results if not r)
        return results

    def generate_from_conversations(self, conversations, *args, **kwargs):
        start = time.time()
        results = self.inner.generate_from_conversations(
            conversations, *args, **kwargs
        )
        elapsed = time.time() - start
        self.call_count += 1
        n = len(conversations)
        self.prompt_count += n
        self.call_records.append((elapsed, n))
        if n > 0:
            per = elapsed / n
            self.per_prompt_latencies.extend([per] * n)
        self.error_count += sum(1 for r in results if not r)
        return results

    def snapshot(self) -> dict:
        lats = self.per_prompt_latencies
        p95 = (
            statistics.quantiles(lats, n=20)[-1]
            if len(lats) >= 20
            else (max(lats) if lats else 0.0)
        )
        return {
            "call_count":    self.call_count,
            "prompt_count":  self.prompt_count,
            "error_count":   self.error_count,
            "p50_latency_s": statistics.median(lats) if lats else 0.0,
            "p95_latency_s": p95,
            "batch_sizes":   [n for _, n in self.call_records],
        }


# ═══════════════════════════════════════════════════════════════════════════
# Setup helpers                                                              #
# ═══════════════════════════════════════════════════════════════════════════


def prepare_input_slice(path: Path, n_rows: int) -> str:
    CACHE_ROOT.mkdir(parents=True, exist_ok=True)
    out = CACHE_ROOT / f"input_{n_rows}.jsonl"
    if out.exists():
        return str(out)

    rows = []
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f):
            if i >= n_rows:
                break
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
    print(
        f"  prepared {len(df)} rows, CoT avg chars: "
        f"{int(df['cot'].str.len().mean()):,}"
    )
    return str(out)


def build_serving(variant: str) -> CountingServingWrapper:
    """Return the wrapped Taiji serving object for the given variant.

    Both variants honour the same ``MAX_WORKERS`` so comparisons are
    apples-to-apples.  Thinking mode matches the reference test.py setting
    (includeThoughts=True, thinkingLevel=high).
    """
    common = dict(
        model_marker=MODEL_MARKER,
        max_workers=MAX_WORKERS,
        max_retries=5,
        enable_thinking=ENABLE_THINKING,
        thinking_level=THINKING_LEVEL,
        retain_thinking=False,
    )
    if variant == "fast":
        inner = TaijiCustomLLMServing_pool(**common)
    else:
        inner = TaijiCustomLLMServing(**common)
    return CountingServingWrapper(inner)


def build_op(variant: str, method: str, serving):
    if method == "A":
        if variant == "fast":
            return CoTLLMJudgeRefinerFast(
                llm_serving=serving, min_steps_to_keep=2
            )
        return CoTLLMJudgeRefiner(
            llm_serving=serving, min_steps_to_keep=2
        )
    if method == "C":
        if variant == "fast":
            return CoTChunkCompressRefinerFast(
                llm_serving=serving, num_candidates=1, min_chunk_tokens=30
            )
        return CoTChunkCompressRefiner(
            llm_serving=serving, num_candidates=1, min_chunk_tokens=30
        )
    if method == "D":
        if variant == "fast":
            return CoTPatternRefinerFast(
                llm_serving=serving, min_fragments_to_keep=2
            )
        return CoTPatternRefiner(
            llm_serving=serving, min_fragments_to_keep=2
        )
    raise ValueError(f"unknown method {method!r}")


def make_storage(tag: str, input_file: str) -> FileStorage:
    cache = CACHE_ROOT / tag
    cache.mkdir(parents=True, exist_ok=True)
    storage = FileStorage(
        first_entry_file_name=input_file,
        cache_path=str(cache),
        file_name_prefix="dataflow_cache",
        cache_type="jsonl",
    )
    storage.step()
    return storage


def compute_run_stats(output_file: Path) -> dict:
    df = pd.read_json(output_file, lines=True)
    orig_chars, out_chars = [], []
    skipped_short = skipped_empty = errors = 0
    for _, row in df.iterrows():
        try:
            s = json.loads(row.get("cot_clean_stats") or "{}")
        except Exception:
            continue
        if s.get("skipped") and s.get("reason") == "short":
            skipped_short += 1
            continue
        if s.get("skipped"):
            skipped_empty += 1
            continue
        if s.get("error"):
            errors += 1
            continue
        if "original_chars" in s and "output_chars" in s:
            orig_chars.append(s["original_chars"])
            out_chars.append(s["output_chars"])
    if orig_chars:
        ratios = [o / max(1, i) for i, o in zip(orig_chars, out_chars)]
        return {
            "valid":            len(orig_chars),
            "skipped_short":    skipped_short,
            "skipped_empty":    skipped_empty,
            "errors":           errors,
            "avg_orig_chars":   int(sum(orig_chars) / len(orig_chars)),
            "avg_out_chars":    int(sum(out_chars) / len(out_chars)),
            "avg_retain_ratio": sum(ratios) / len(ratios),
        }
    return {
        "valid":         0,
        "skipped_short": skipped_short,
        "skipped_empty": skipped_empty,
        "errors":        errors,
    }


# ═══════════════════════════════════════════════════════════════════════════
# Main                                                                       #
# ═══════════════════════════════════════════════════════════════════════════


def main():
    if not os.environ.get("APP_ID") or not os.environ.get("APP_KEY"):
        raise SystemExit(
            "APP_ID / APP_KEY environment variables are required."
        )

    RESULTS_DIR.mkdir(exist_ok=True)

    # Prepare one input slice per distinct row count referenced in the
    # cell list.
    distinct_ns = sorted({n for _, _, n in METHODS_TO_RUN})
    input_files: dict[int, str] = {
        n: prepare_input_slice(DATA_PATH, n) for n in distinct_ns
    }

    print("=" * 72)
    print(
        f"  Taiji A/B validation  -  model={MODEL_MARKER}  "
        f"workers={MAX_WORKERS}"
    )
    print(f"  cells: {METHODS_TO_RUN}")
    print("=" * 72)

    report_rows: list[dict] = []

    for variant, method, n_rows in METHODS_TO_RUN:
        tag = f"{method}_{variant}_{n_rows}"
        print(f"\n  [{tag}]")
        serving = build_serving(variant)
        op = build_op(variant, method, serving)
        storage = make_storage(tag, input_files[n_rows])

        t0 = time.time()
        try:
            op.run(
                storage=storage,
                input_key="cot",
                output_key="cot_cleaned",
                output_stats_key="cot_clean_stats",
                problem_key="problem",
            )
            wall = time.time() - t0
            serv_snap = serving.snapshot()
            out_file = CACHE_ROOT / tag / "dataflow_cache_step1.jsonl"
            run_snap = compute_run_stats(out_file)
            report_rows.append({
                "variant":      variant,
                "method":       method,
                "n_rows":       n_rows,
                "wall_seconds": round(wall, 1),
                **serv_snap,
                **run_snap,
            })
            print(
                f"    wall {wall:.1f}s  calls={serv_snap['call_count']}  "
                f"prompts={serv_snap['prompt_count']}  "
                f"errors={serv_snap['error_count']}  "
                f"p50={serv_snap['p50_latency_s']:.3f}s  "
                f"p95={serv_snap['p95_latency_s']:.3f}s"
            )
            if run_snap.get("valid"):
                print(
                    f"    valid={run_snap['valid']}  "
                    f"skipped_short={run_snap.get('skipped_short', 0)}  "
                    f"retain_ratio={run_snap['avg_retain_ratio']:.1%}  "
                    f"{run_snap['avg_orig_chars']:,} -> "
                    f"{run_snap['avg_out_chars']:,} chars"
                )
        except Exception as exc:
            import traceback
            traceback.print_exc()
            report_rows.append(
                {"variant": variant, "method": method, "n_rows": n_rows,
                 "error": str(exc)}
            )
        finally:
            try:
                serving.cleanup()
            except Exception:
                pass

    report_path = RESULTS_DIR / "ab_report.json"
    with open(report_path, "w") as f:
        json.dump(report_rows, f, indent=2, ensure_ascii=False)
    print(f"\n  report -> {report_path}")

    md_path = RESULTS_DIR / "ab_report.md"
    write_markdown_report(report_rows, md_path)
    print(f"  markdown -> {md_path}")


def write_markdown_report(rows: list[dict], out: Path):
    lines = ["# Fast CoT Refiner A/B validation (Taiji)\n"]
    lines.append(
        f"model={MODEL_MARKER}, max_workers={MAX_WORKERS}\n"
    )
    lines.append(
        "| method | variant | rows | wall s | s/row | calls | prompts | "
        "errors | p50 s | p95 s | retain | orig→out chars |"
    )
    lines.append("|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|")
    for r in rows:
        n_rows = r.get("n_rows", "?")
        if "error" in r:
            lines.append(
                f"| {r['method']} | {r['variant']} | {n_rows} | **FAILED** "
                f"| | | | | | | | {r['error'][:80]} |"
            )
            continue
        retain = r.get("avg_retain_ratio")
        chars = (
            f"{r.get('avg_orig_chars', 0):,} → {r.get('avg_out_chars', 0):,}"
            if r.get("avg_orig_chars")
            else "-"
        )
        retain_s = f"{retain:.1%}" if retain is not None else "-"
        per_row = (
            r['wall_seconds'] / max(1, n_rows)
            if isinstance(n_rows, int)
            else 0.0
        )
        lines.append(
            f"| {r['method']} | {r['variant']} | {n_rows} | "
            f"{r['wall_seconds']} | {per_row:.1f} | "
            f"{r['call_count']} | {r['prompt_count']} | {r['error_count']} | "
            f"{r['p50_latency_s']:.3f} | {r['p95_latency_s']:.3f} | "
            f"{retain_s} | {chars} |"
        )

    lines.append("\n## Speedups (per-row, normalises different cell sizes)\n")
    by_method: dict[str, dict[str, dict]] = {}
    for r in rows:
        if "error" in r:
            continue
        by_method.setdefault(r["method"], {})[r["variant"]] = r
    for m, variants in sorted(by_method.items()):
        if "old" in variants and "fast" in variants:
            old = variants["old"]
            fast = variants["fast"]
            old_w_pr = old["wall_seconds"] / max(1, old.get("n_rows", 1))
            fast_w_pr = fast["wall_seconds"] / max(1, fast.get("n_rows", 1))
            old_c = old["call_count"] or 1
            fast_c = fast["call_count"] or 1
            old_p = old["prompt_count"] or 1
            fast_p = fast["prompt_count"] or 1
            old_c_pr = old_c / max(1, old.get("n_rows", 1))
            fast_c_pr = fast_c / max(1, fast.get("n_rows", 1))
            lines.append(
                f"- **Method {m}**: wall **{old_w_pr / fast_w_pr:.2f}×** per "
                f"row ({old_w_pr:.1f}s → {fast_w_pr:.1f}s), "
                f"calls/row {old_c_pr:.2f}→{fast_c_pr:.2f} "
                f"({old_c_pr / max(1e-6, fast_c_pr):.1f}×), "
                f"prompts {old_p}→{fast_p}"
            )
    out.write_text("\n".join(lines))


if __name__ == "__main__":
    main()
