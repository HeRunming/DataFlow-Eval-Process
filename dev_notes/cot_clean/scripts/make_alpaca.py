"""
Convert Method A/C/D cleaned-CoT outputs to Alpaca format adapted to the
Qwen3-native thinking format.

Qwen3-native shape (matches the official chat template):

    <think>
    <reasoning>
    </think>

    <final answer>

Differences from DeepSeek-R1 shape currently in our cot_cleaned column:
  * We KEEP   <think>...</think>  (Qwen3 tokeniser has these as special tokens)
  * We STRIP  <answer>...</answer> wrappers (not a Qwen3 convention)
  * We ensure exactly one blank line between </think> and the final answer

The input column becomes the Alpaca "input" verbatim; "instruction" is left
empty as requested.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path("/data/workspace/cc_workspace/cot_clean_test/results_full")
OUT_DIR = ROOT / "alpaca"
OUT_DIR.mkdir(parents=True, exist_ok=True)

METHODS = ["A", "C", "D"]

# Also emit a Qwen3-format version of the *uncleaned* raw R1 dataset so the
# user has a baseline with identical formatting to the cleaned methods.
RAW_SRC = Path(
    "/data/workspace/cc_workspace/cot_clean_test/dataflow_reasoningmath_10k.jsonl"
)
RAW_OUT = OUT_DIR / "method_raw_10k_alpaca.jsonl"


# Match <think>...</think> (optionally) followed by <answer>...</answer>.
# re.DOTALL so the body can span lines; re.IGNORECASE to be safe.
_FULL_RE = re.compile(
    r"<think>\s*(.*?)\s*</think>\s*<answer>\s*(.*?)\s*</answer>\s*",
    re.DOTALL | re.IGNORECASE,
)

# Fallback when only <think>...</think> is present and the answer is inside
# the think block (the 24/10k cases).
_THINK_ONLY_RE = re.compile(
    r"<think>\s*(.*?)\s*</think>\s*(.*)",
    re.DOTALL | re.IGNORECASE,
)


def to_qwen3(cot_cleaned: str) -> str:
    """Transform R1-style <think>/<answer> output into Qwen3-native shape.

    Returns a string of the form

        <think>\n<reasoning>\n</think>\n\n<final_answer>

    Always emits Unix newlines, always inserts exactly one blank line
    between </think> and the final answer.  If neither pattern matches
    (should not happen on our data but we are defensive), returns the
    input unchanged.
    """
    if not cot_cleaned:
        return cot_cleaned

    m = _FULL_RE.search(cot_cleaned)
    if m:
        reasoning = m.group(1).strip()
        answer = m.group(2).strip()
        return f"<think>\n{reasoning}\n</think>\n\n{answer}"

    m = _THINK_ONLY_RE.search(cot_cleaned)
    if m:
        reasoning = m.group(1).strip()
        trailer = m.group(2).strip()
        # If there is no trailing text at all, the final answer lives
        # inside the reasoning (e.g. the R1 sample ended inside <think>).
        # Qwen3's chat template still expects something after </think>:
        # try to lift the last \boxed{...} or last paragraph out.
        if not trailer:
            trailer = _extract_tail_answer(reasoning)
        return f"<think>\n{reasoning}\n</think>\n\n{trailer}"

    # Neither tag: pass through.  The chat template expects <think>...</think>
    # always present, but if the upstream data lacks it we do not invent one.
    return cot_cleaned


_BOXED_RE = re.compile(r"\\boxed\{")


def _extract_tail_answer(reasoning: str) -> str:
    """Best-effort pull a final answer out of the reasoning tail.

    Priority:
      1. Last ``\\boxed{...}`` (with balanced braces) anywhere in the
         reasoning -- return just that ``\\boxed{...}`` expression.
      2. Last non-empty line of the reasoning.
      3. Empty string if nothing found (lets caller decide).
    """
    # Try \boxed{...} with balanced braces.
    last_start = -1
    for m in _BOXED_RE.finditer(reasoning):
        last_start = m.start()
    if last_start >= 0:
        i = last_start + len("\\boxed{")
        depth = 1
        j = i
        while j < len(reasoning) and depth > 0:
            c = reasoning[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            j += 1
        if depth == 0:
            return reasoning[last_start:j]  # include \boxed{ ... }

    # Otherwise, last non-empty line.
    for line in reversed(reasoning.splitlines()):
        line = line.strip()
        if line:
            return line
    return ""


def convert(src: Path, dst: Path, cot_field: str) -> tuple[Path, int, int, int]:
    """Return (out_path, n_ok, n_skip, n_no_tag) for the conversion.

    Reads from ``src`` jsonl (each record must have ``input`` and
    ``cot_field`` keys) and writes Qwen3-format Alpaca to ``dst``.
    """
    n_ok = n_skip = n_no_tag = 0
    with open(src, encoding="utf-8") as fin, open(dst, "w", encoding="utf-8") as fout:
        for line in fin:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            q = rec.get("input") or ""
            raw = rec.get(cot_field) or ""
            if not q.strip() or not raw.strip():
                n_skip += 1
                continue
            out = to_qwen3(raw)
            if "<think>" not in out:
                n_no_tag += 1
            fout.write(json.dumps(
                {"instruction": "", "input": q, "output": out},
                ensure_ascii=False,
            ) + "\n")
            n_ok += 1
    return dst, n_ok, n_skip, n_no_tag


if __name__ == "__main__":
    # Cleaned methods (A / C / D) -- cot_field is cot_cleaned
    for m in METHODS:
        src = ROOT / f"method_{m}_10k.jsonl"
        if not src.exists():
            print(f"  [skip] {src} not found")
            continue
        dst = OUT_DIR / f"method_{m}_10k_alpaca.jsonl"
        dst, n_ok, n_skip, n_no_tag = convert(src, dst, cot_field="cot_cleaned")
        size_mb = dst.stat().st_size / (1024 * 1024)
        tag_warn = f"  [WARN] {n_no_tag} rows without <think>" if n_no_tag else ""
        print(
            f"  Method {m}: wrote {n_ok} rows (+{n_skip} skipped)  "
            f"-> {dst}  ({size_mb:.1f} MB){tag_warn}"
        )

    # Raw uncleaned R1 data -- cot_field is the vanilla output column.
    if RAW_SRC.exists():
        dst, n_ok, n_skip, n_no_tag = convert(RAW_SRC, RAW_OUT, cot_field="output")
        size_mb = dst.stat().st_size / (1024 * 1024)
        tag_warn = f"  [WARN] {n_no_tag} rows without <think>" if n_no_tag else ""
        print(
            f"  Method raw: wrote {n_ok} rows (+{n_skip} skipped)  "
            f"-> {dst}  ({size_mb:.1f} MB){tag_warn}"
        )
    else:
        print(f"  [skip] raw source {RAW_SRC} not found")
