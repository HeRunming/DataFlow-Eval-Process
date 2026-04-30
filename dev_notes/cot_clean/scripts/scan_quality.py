"""
Scan A/C/D/raw Alpaca files for data-quality risks that could crash SFT.

Ten dimensions:
  1. length distribution (chars / think chars / answer chars)
  2. think/answer structure (missing <think>, missing close, missing final answer)
  3. answer loss (raw has \\boxed{X} in answer, cleaned has no boxed or different X)
  4. boxed bracket balance (unterminated \\boxed)
  5. template phrases ("considered ... abandoned", "let me verify", ...)
  6. repeated n-grams inside think (5-gram repeated >= 3 times)
  7. language mix (CJK chars inside English output / vice versa)
  8. abrupt truncation (think ends mid-sentence, no closing tag)
  9. logical-break cue words right after compressed regions
 10. overall length vs raw: rows that GREW (should not happen) or shrank > 95%

Writes scan_report.json + scan_flags.jsonl (one row per input id x method
with the boolean flags set).

Usage:
    python scan_quality.py \
        --dir /data/workspace/cc_workspace/cot_clean_test/results_full/alpaca \
        --out /data/workspace/cc_workspace/DataFlow/dev_notes/cot_clean/scan
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

METHODS = ["method_A", "method_C", "method_D", "method_raw"]

CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
ASCII_LETTER_RE = re.compile(r"[A-Za-z]")
BOXED_RE = re.compile(r"\\boxed\s*\{")
CLOSE_THINK_RE = re.compile(r"</think\s*>", re.I)
OPEN_THINK_RE = re.compile(r"<think\s*>", re.I)

TEMPLATE_PATTERNS = [
    (re.compile(r"considered\s+\w[\w\s,]{0,40}?,?\s*(?:but|and)\s+abandoned", re.I), "CONSIDERED_ABANDONED"),
    (re.compile(r"(?:i|we)\s+(?:tried|attempted)\b[^.]{0,80}?\bbut\b[^.]{0,60}?\b(?:did not work|failed|abandon)", re.I), "TRIED_BUT_FAILED"),
    (re.compile(r"(?:let me|i(?:'ll| will))\s+verify", re.I), "LET_ME_VERIFY"),
    (re.compile(r"(?:let me|i(?:'ll| will))\s+(?:double[- ]check|reconsider)", re.I), "RECONSIDER"),
    (re.compile(r"in\s+summary,\s+", re.I), "IN_SUMMARY"),
    (re.compile(r"\bwait[,.]", re.I), "WAIT"),
    (re.compile(r"\bhmm[,.]", re.I), "HMM"),
]

TRUNC_CUES = [",", "that", "which", "and", "so", "because", "if", "when", "then", "but", "以", "的", "是"]


def last_char(s: str) -> str:
    s = s.strip()
    return s[-1] if s else ""


def count_unclosed_boxed(s: str) -> int:
    # count "\\boxed{" opens vs matching "}" with nesting.
    count = 0
    i = 0
    while True:
        m = BOXED_RE.search(s, i)
        if not m:
            break
        # walk balanced braces
        depth = 1
        j = m.end()
        closed = False
        while j < len(s):
            c = s[j]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    closed = True
                    break
            j += 1
        if not closed:
            count += 1
        i = j + 1
    return count


def max_repeated_ngram(tokens: list[str], n: int = 5) -> int:
    if len(tokens) < n:
        return 0
    c = Counter(tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1))
    return c.most_common(1)[0][1]


def extract_think_answer(output: str) -> tuple[str, str, dict]:
    flags: dict[str, bool] = {}
    open_m = OPEN_THINK_RE.search(output)
    close_m = CLOSE_THINK_RE.search(output)
    flags["missing_open_think"] = open_m is None
    flags["missing_close_think"] = close_m is None
    if open_m and close_m:
        think = output[open_m.end():close_m.start()]
        answer = output[close_m.end():].strip()
    elif close_m:
        think = output[:close_m.start()]
        answer = output[close_m.end():].strip()
    elif open_m:
        think = output[open_m.end():]
        answer = ""
    else:
        think = output
        answer = ""
    flags["missing_final_answer"] = len(answer) < 4
    return think, answer, flags


def boxed_payload(text: str) -> str | None:
    m = BOXED_RE.search(text)
    if not m:
        return None
    depth = 1
    j = m.end()
    start = j
    while j < len(text) and depth > 0:
        c = text[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        j += 1
    if depth == 0:
        return text[start:j - 1].strip()
    return None


def scan_row(output: str) -> dict:
    think, answer, flags = extract_think_answer(output)

    flags["len_chars"] = len(output)
    flags["think_chars"] = len(think)
    flags["answer_chars"] = len(answer)

    # unclosed boxed
    unc = count_unclosed_boxed(output)
    flags["unclosed_boxed"] = unc > 0
    flags["unclosed_boxed_count"] = unc

    # final boxed payload in answer
    flags["answer_boxed_payload"] = boxed_payload(answer)

    # templates
    for pat, name in TEMPLATE_PATTERNS:
        flags[f"tpl_{name}"] = bool(pat.search(think))

    # language mix
    cjk = len(CJK_RE.findall(output))
    eng = len(ASCII_LETTER_RE.findall(output))
    flags["cjk_chars"] = cjk
    flags["eng_chars"] = eng
    # "mixed" means both sides non-trivial AND minority >= 2% of majority
    if cjk > 5 and eng > 50:
        ratio = min(cjk, eng) / max(cjk, eng)
        flags["language_mix"] = ratio >= 0.02 and cjk >= 10
    else:
        flags["language_mix"] = False

    # truncation: think ends without ., ?, !, 。, etc., and no closing think tag
    last = last_char(think)
    flags["think_ends_midsentence"] = last not in ".!?。？！》）)\"'” 」" and len(think) > 100

    # n-gram repeat inside think (word-level)
    toks = re.findall(r"\w+", think.lower())
    flags["max_5gram_repeat"] = max_repeated_ngram(toks, 5)
    flags["max_10gram_repeat"] = max_repeated_ngram(toks, 10)

    # after-compression logical-break cue:  "therefore" or "thus" immediately after compressed chunk boundary
    # approx: count of "therefore," / "thus," preceded by a sentence without derivation
    flags["dangling_therefore"] = bool(re.search(r"(?:^|\.\s+)(?:Therefore|Thus|Hence),", think))

    return flags


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    per_method: dict[str, list[dict]] = {m: [] for m in METHODS}
    answers_by_row: dict[int, dict[str, str | None]] = defaultdict(dict)

    for method in METHODS:
        path = Path(args.dir) / f"{method}_10k_alpaca.jsonl"
        with path.open() as f:
            for i, line in enumerate(f):
                if args.limit and i >= args.limit:
                    break
                r = json.loads(line)
                flags = scan_row(r.get("output", ""))
                flags["row"] = i
                flags["method"] = method
                per_method[method].append(flags)
                answers_by_row[i][method] = flags.get("answer_boxed_payload")

    # aggregate
    report: dict[str, dict] = {}
    for method, rows in per_method.items():
        n = len(rows)
        agg = {"n": n}

        def count(key: str) -> int:
            return sum(1 for r in rows if r.get(key))

        agg["missing_open_think"] = count("missing_open_think")
        agg["missing_close_think"] = count("missing_close_think")
        agg["missing_final_answer"] = count("missing_final_answer")
        agg["unclosed_boxed"] = count("unclosed_boxed")
        agg["language_mix"] = count("language_mix")
        agg["think_ends_midsentence"] = count("think_ends_midsentence")
        agg["dangling_therefore"] = count("dangling_therefore")

        for _, name in TEMPLATE_PATTERNS:
            agg[f"tpl_{name}"] = count(f"tpl_{name}")

        # length stats
        lens = sorted(r["len_chars"] for r in rows)
        think_lens = sorted(r["think_chars"] for r in rows)
        ans_lens = sorted(r["answer_chars"] for r in rows)

        def pct(arr, p):
            return arr[int(len(arr) * p)] if arr else 0

        agg["len_p10"] = pct(lens, 0.1)
        agg["len_p50"] = pct(lens, 0.5)
        agg["len_p90"] = pct(lens, 0.9)
        agg["len_p99"] = pct(lens, 0.99)
        agg["len_mean"] = sum(lens) // max(1, n)
        agg["think_p50"] = pct(think_lens, 0.5)
        agg["think_p90"] = pct(think_lens, 0.9)
        agg["ans_p50"] = pct(ans_lens, 0.5)
        agg["ans_p10"] = pct(ans_lens, 0.1)
        # repeat n-gram
        repeats = sorted(r["max_5gram_repeat"] for r in rows)
        agg["5gram_repeat_p99"] = pct(repeats, 0.99)
        repeats10 = sorted(r["max_10gram_repeat"] for r in rows)
        agg["10gram_repeat_p99"] = pct(repeats10, 0.99)
        agg["max_5gram_repeat"] = max(repeats) if repeats else 0
        agg["max_10gram_repeat"] = max(repeats10) if repeats10 else 0

        report[method] = agg

    # answer-drift: compare A/C/D boxed payload to raw
    drift = {m: 0 for m in ("method_A", "method_C", "method_D")}
    missing_boxed_raw = 0
    for row_id, by_method in answers_by_row.items():
        raw_ans = by_method.get("method_raw")
        if raw_ans is None:
            missing_boxed_raw += 1
            continue
        for m in drift:
            if by_method.get(m) != raw_ans:
                drift[m] += 1
    report["answer_drift_vs_raw"] = drift
    report["raw_no_boxed"] = missing_boxed_raw

    # grew-longer than raw
    raw_by_row = {r["row"]: r["len_chars"] for r in per_method["method_raw"]}
    for m in ("method_A", "method_C", "method_D"):
        grew = sum(1 for r in per_method[m] if r["len_chars"] > raw_by_row.get(r["row"], 0))
        shrunk_95 = sum(1 for r in per_method[m] if r["len_chars"] < 0.05 * raw_by_row.get(r["row"], 1))
        report[m]["grew_longer_than_raw"] = grew
        report[m]["shrunk_below_5pct"] = shrunk_95

    (out_dir / "scan_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))

    # flat flags for drilldown
    with (out_dir / "scan_flags.jsonl").open("w") as f:
        for method in METHODS:
            for flags in per_method[method]:
                # drop heavy
                slim = {k: v for k, v in flags.items() if k != "answer_boxed_payload"}
                f.write(json.dumps(slim, ensure_ascii=False) + "\n")

    # print summary
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
