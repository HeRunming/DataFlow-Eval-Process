"""
Drill into the anomalies surfaced by scan_quality.py.

1. Method A think-ends-midsentence rows: read the original raw think and the
   A-compressed think side by side, for 8 random rows flagged.
2. Method D top 5 rows by 5-gram repeat -- is it a genuine R1 artifact
   (kept verbatim by diversity sampling) or introduced by the compressor?
3. Rows where raw has no \\boxed{} payload -- what does the final answer
   look like, and how did A/C/D handle it?
4. Rows where unclosed_boxed == True in any method.
5. Rows where language_mix == True.
"""
import json
import random
import re
from pathlib import Path

SCAN = Path("/data/workspace/cc_workspace/DataFlow/dev_notes/cot_clean/scan")
ALP = Path("/data/workspace/cc_workspace/cot_clean_test/results_full/alpaca")

random.seed(7)

# Load flags
flags_by_row: dict[tuple[str, int], dict] = {}
with (SCAN / "scan_flags.jsonl").open() as f:
    for line in f:
        r = json.loads(line)
        flags_by_row[(r["method"], r["row"])] = r


def load_outputs(method: str) -> list[dict]:
    return [json.loads(l) for l in (ALP / f"{method}_10k_alpaca.jsonl").open()]


def get_think_and_answer(output: str):
    import re
    m1 = re.search(r"<think\s*>", output, re.I)
    m2 = re.search(r"</think\s*>", output, re.I)
    think = output[m1.end():m2.start()] if m1 and m2 else output
    answer = output[m2.end():].strip() if m2 else ""
    return think, answer


def short(s: str, n: int = 280) -> str:
    s = s.strip()
    if len(s) <= n:
        return s
    return s[:n // 2] + " … " + s[-n // 2:]


raw_outputs = load_outputs("method_raw")
a_outputs = load_outputs("method_A")
c_outputs = load_outputs("method_C")
d_outputs = load_outputs("method_D")


# ---- 1. A midsentence rows
print("=" * 80)
print("1. Method A rows flagged 'think_ends_midsentence' (sample 8)")
print("=" * 80)
a_mid = [row for (m, row), fl in flags_by_row.items() if m == "method_A" and fl.get("think_ends_midsentence")]
random.shuffle(a_mid)
for row in a_mid[:8]:
    r_think, _ = get_think_and_answer(raw_outputs[row]["output"])
    a_think, _ = get_think_and_answer(a_outputs[row]["output"])
    print(f"\n--- row {row} ---")
    print(f"[A last 240 chars] {repr(a_think[-240:])}")
    print(f"[raw last 240 chars] {repr(r_think[-240:])}")


# ---- 2. D top 5 by 5-gram repeat
print("\n" + "=" * 80)
print("2. Method D top 5 rows by 5-gram repeat")
print("=" * 80)
d_rows = [(fl["max_5gram_repeat"], row, fl["max_10gram_repeat"]) for (m, row), fl in flags_by_row.items() if m == "method_D"]
d_rows.sort(reverse=True)
for rep5, row, rep10 in d_rows[:5]:
    d_think, _ = get_think_and_answer(d_outputs[row]["output"])
    r_think, _ = get_think_and_answer(raw_outputs[row]["output"])
    r_fl = flags_by_row[("method_raw", row)]
    print(f"\n--- row {row}, D 5gram={rep5}, D 10gram={rep10}, raw 5gram={r_fl['max_5gram_repeat']} ---")
    # find the repeated 5-gram
    toks = re.findall(r"\w+", d_think.lower())
    from collections import Counter
    c = Counter(tuple(toks[i:i + 5]) for i in range(len(toks) - 4))
    top = c.most_common(3)
    print(f"    top 5-grams in D think: {top}")


# ---- 3. raw_no_boxed rows, A/C/D handling
print("\n" + "=" * 80)
print("3. Rows where raw has no \\boxed{} final answer  (sample 6)")
print("=" * 80)
no_boxed_rows = []
for row in range(len(raw_outputs)):
    _, raw_ans = get_think_and_answer(raw_outputs[row]["output"])
    if not re.search(r"\\boxed\s*\{", raw_ans):
        no_boxed_rows.append(row)
print(f"total no-boxed in raw answer region = {len(no_boxed_rows)}")
random.shuffle(no_boxed_rows)
for row in no_boxed_rows[:6]:
    _, raw_ans = get_think_and_answer(raw_outputs[row]["output"])
    _, a_ans = get_think_and_answer(a_outputs[row]["output"])
    _, d_ans = get_think_and_answer(d_outputs[row]["output"])
    print(f"\n--- row {row} ---")
    print(f"[raw ans] {short(raw_ans, 300)}")
    print(f"[A   ans] {short(a_ans, 200)}")
    print(f"[D   ans] {short(d_ans, 200)}")


# ---- 4. unclosed_boxed anywhere
print("\n" + "=" * 80)
print("4. Rows with unclosed_boxed in any method")
print("=" * 80)
for (m, row), fl in flags_by_row.items():
    if fl.get("unclosed_boxed"):
        print(f"  {m} row {row}, count={fl.get('unclosed_boxed_count')}")


# ---- 5. language_mix rows
print("\n" + "=" * 80)
print("5. Language-mix rows")
print("=" * 80)
for (m, row), fl in flags_by_row.items():
    if fl.get("language_mix"):
        out = {"method_A": a_outputs, "method_C": c_outputs, "method_D": d_outputs, "method_raw": raw_outputs}[m][row]["output"]
        cjk_snippet = next(iter(re.findall(r".{0,30}[\u4e00-\u9fff]{2,}.{0,30}", out)), "(none)")
        print(f"  {m} row {row} cjk={fl['cjk_chars']} eng={fl['eng_chars']}  snippet: {repr(cjk_snippet)}")
