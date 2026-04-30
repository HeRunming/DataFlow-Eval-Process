"""
Follow-up drilldown:

Q1. Method A / C / D -- how many rows have answer region with NO \\boxed{}
    at all after cleaning, compared to raw?
Q2. For rows where raw HAS a \\boxed{} in its answer region, do A/C/D drop
    it?  (Genuine answer-loss.)
Q3. For rows where raw has a \\boxed{} anywhere in the output (incl. inside
    <think>), does the cleaned version preserve the LAST boxed payload
    exactly?  (Covers row 2186-type cases where the boxed lives in think.)
"""
import json
import re
from pathlib import Path

ALP = Path("/data/workspace/cc_workspace/cot_clean_test/results_full/alpaca")
METHODS = ["method_A", "method_C", "method_D", "method_raw"]


def parse(output: str):
    m1 = re.search(r"<think\s*>", output, re.I)
    m2 = re.search(r"</think\s*>", output, re.I)
    if m1 and m2:
        return output[m1.end():m2.start()], output[m2.end():].strip()
    return output, ""


BOXED_RE = re.compile(r"\\boxed\s*\{")


def last_boxed_payload(s: str) -> str | None:
    """Return the payload of the LAST \\boxed{} with balanced braces."""
    last = None
    i = 0
    while True:
        m = BOXED_RE.search(s, i)
        if not m:
            break
        depth = 1
        j = m.end()
        start = j
        while j < len(s) and depth > 0:
            if s[j] == "{":
                depth += 1
            elif s[j] == "}":
                depth -= 1
            j += 1
        if depth == 0:
            last = s[start:j - 1].strip()
        i = j + 1
    return last


def load(method: str):
    return [json.loads(l) for l in (ALP / f"{method}_10k_alpaca.jsonl").open()]


data = {m: load(m) for m in METHODS}

# Q1: how many answer regions lack any \boxed{}
print("Q1: rows where ANSWER region has no \\boxed{}")
for m in METHODS:
    n = 0
    for r in data[m]:
        _, ans = parse(r["output"])
        if not BOXED_RE.search(ans):
            n += 1
    print(f"  {m}: {n}")

# Q2: rows where raw answer region has \boxed and cleaned does not
print("\nQ2: rows where raw ANSWER has \\boxed but cleaned does NOT (hard answer-loss)")
for m in ["method_A", "method_C", "method_D"]:
    bad = []
    for row in range(10000):
        _, raw_ans = parse(data["method_raw"][row]["output"])
        if not BOXED_RE.search(raw_ans):
            continue
        _, clean_ans = parse(data[m][row]["output"])
        if not BOXED_RE.search(clean_ans):
            bad.append(row)
    print(f"  {m}: {len(bad)} rows  examples={bad[:10]}")

# Q3: compare LAST boxed payload anywhere in the whole output
print("\nQ3: LAST \\boxed payload mismatch (compared anywhere in output)")
mismatches_by_m: dict[str, list[tuple[int, str, str]]] = {}
for m in ["method_A", "method_C", "method_D"]:
    mm: list[tuple[int, str, str]] = []
    for row in range(10000):
        raw = last_boxed_payload(data["method_raw"][row]["output"])
        clean = last_boxed_payload(data[m][row]["output"])
        if raw != clean:
            mm.append((row, str(raw)[:100], str(clean)[:100]))
    mismatches_by_m[m] = mm
    print(f"  {m}: {len(mm)} mismatches")
    for row, r, c in mm[:10]:
        print(f"    row {row}:  raw={r!r}  clean={c!r}")

# Dump the mismatches for later inspection
out_path = Path("/data/workspace/cc_workspace/DataFlow/dev_notes/cot_clean/scan/answer_drift.json")
out_path.write_text(json.dumps({m: mm for m, mm in mismatches_by_m.items()}, indent=2, ensure_ascii=False))
print(f"\nSaved mismatches -> {out_path}")

# Q4: cross-reference  A-drops-boxed-in-answer AND raw-has-boxed-somewhere
print("\nQ4: A's ans has no \\boxed but raw had one in think (unrecoverable)")
for m in ["method_A"]:
    cases = []
    for row in range(10000):
        raw_think, raw_ans = parse(data["method_raw"][row]["output"])
        clean_think, clean_ans = parse(data[m][row]["output"])
        raw_has_any = last_boxed_payload(data["method_raw"][row]["output"])
        if raw_has_any is None:
            continue
        if BOXED_RE.search(clean_ans):
            continue
        # clean ans has no boxed but raw had one somewhere
        cases.append((row, raw_has_any[:80]))
    print(f"  {m}: {len(cases)}")
    for r, p in cases[:12]:
        print(f"    row {r}: raw last-boxed = {p!r}")
