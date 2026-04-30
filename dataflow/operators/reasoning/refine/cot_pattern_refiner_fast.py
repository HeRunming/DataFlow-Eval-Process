"""
CoTPatternRefinerFast - flattened, merged-call variant of Method D.
===================================================================

Same three wins as ``CoTLLMJudgeRefinerFast``:

1.  Every fragment in the dataframe is classified in a single LLM batch.
    No per-row barrier.
2.  Classification and compression are merged via
    ``CoTPatternClassifyCompressPrompt``: when the action is "compress",
    the LLM emits the rewrite in the same JSON, so we save one round-trip
    on every compressible fragment.  ("keep" uses the original fragment;
    "delete" drops it.)
3.  ``min_chars_to_clean`` bypasses short CoTs.

Because ``UNNECESSARY_EXPLORATION`` fragments are deleted, the Fast
variant's call volume is strictly lower than the reference implementation
even before the merge: we only classify (and the same JSON carries any
needed compression).
"""

from __future__ import annotations

import json
import re
from typing import Optional, Union

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.core.prompt import DIYPromptABC, prompt_restrict
from dataflow.prompts.reasoning.cot_clean import (
    CoTPatternClassifyCompressPrompt,
)
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# --------------------------------------------------------------------------- #


# Three tunable action tables.  The "balanced" profile is the new default
# for this operator: it preserves failed-path narrative (summarised, not
# erased) so the distilled CoT still carries information about which
# approaches were tried and why they did not work.  That matters for
# training setups that use these CoTs as positive examples or as DPO
# positives: if every dead end disappears, the model loses the signal
# that the correct path was chosen *after* considering alternatives.
_PRESETS: dict[str, dict[str, str]] = {
    # Strongest compression, matches the pre-v2 behaviour.  Safe for
    # purely-distillation corpora where the "thinking pattern" is not a
    # training signal.
    "aggressive": {
        "CORE_REASONING":          "keep",
        "NECESSARY_VERIFICATION":  "keep",
        "COMPUTATION":             "keep",
        "CONCLUSION":              "keep",
        "NECESSARY_EXPLORATION":   "compress",
        "REDUNDANT_VERIFICATION":  "compress",
        "PREAMBLE":                "compress",
        "TRANSITION":              "compress",
        "UNNECESSARY_EXPLORATION": "delete",
    },
    # NEW DEFAULT.  The only difference from "aggressive" is that failed
    # explorations are summarised rather than deleted, so the narrative
    # "I tried X and it did not work" survives in one sentence.
    "balanced": {
        "CORE_REASONING":          "keep",
        "NECESSARY_VERIFICATION":  "keep",
        "COMPUTATION":             "keep",
        "CONCLUSION":              "keep",
        "NECESSARY_EXPLORATION":   "compress",
        "REDUNDANT_VERIFICATION":  "compress",
        "PREAMBLE":                "compress",
        "TRANSITION":              "compress",
        "UNNECESSARY_EXPLORATION": "compress",
    },
    # Minimal compression.  Every fragment is at least compress-kept;
    # only the pure-noise TRANSITION class is allowed to disappear.
    # Use for DPO positive construction where maximum diversity matters.
    "conservative": {
        "CORE_REASONING":          "keep",
        "NECESSARY_VERIFICATION":  "keep",
        "COMPUTATION":             "keep",
        "CONCLUSION":              "keep",
        "NECESSARY_EXPLORATION":   "keep",
        "REDUNDANT_VERIFICATION":  "compress",
        "PREAMBLE":                "compress",
        "TRANSITION":              "compress",
        "UNNECESSARY_EXPLORATION": "compress",
    },
}

# Kept for backward compatibility with code that imported the old constant.
_PATTERN_ACTION = _PRESETS["balanced"]
_ALL_PATTERN_TYPES = set(_PATTERN_ACTION.keys())


# --------------------------------------------------------------------------- #


def _split_into_fragments(cot_text: str, min_fragment_chars: int = 400) -> list[str]:
    """Split CoT into fragments, merging short adjacent ones.

    See ``_split_cot_into_steps`` in ``cot_llm_judge_refiner_fast`` for the
    rationale.  ``min_fragment_chars=0`` disables merging.
    """
    raw = [f.strip() for f in re.split(r"\n{2,}", cot_text) if f.strip()]
    if len(raw) <= 1:
        raw = [f.strip() for f in cot_text.split("\n") if f.strip()]
    if not raw:
        return [cot_text.strip()]
    if min_fragment_chars <= 0:
        return raw

    merged: list[str] = []
    for piece in raw:
        if merged and len(merged[-1]) < min_fragment_chars:
            merged[-1] = merged[-1] + "\n\n" + piece
        else:
            merged.append(piece)
    if len(merged) >= 2 and len(merged[-1]) < min_fragment_chars:
        tail = merged.pop()
        merged[-1] = merged[-1] + "\n\n" + tail
    return merged


def _parse_cot_from_r1(text: str) -> tuple[str, str]:
    m = re.search(
        r"<think>(.*?)</think>\s*<answer>(.*?)</answer>", text, re.DOTALL
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r"<think>(.*?)</think>(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ""


def _parse_pattern_classify_compress_response(response: str) -> dict:
    default = {
        "type": "CORE_REASONING",
        "key_information": "",
        "recommendation": "keep",
        "compressed": "",
    }
    if not response:
        return default
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return default
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return default
    ptype = str(data.get("type", "CORE_REASONING")).upper()
    if ptype not in _ALL_PATTERN_TYPES:
        ptype = "CORE_REASONING"
    # Prefer the canonical action table; ignore recommendation mismatches.
    action = _PATTERN_ACTION[ptype]
    return {
        "type": ptype,
        "key_information": str(data.get("key_information", "") or ""),
        "recommendation": action,
        "compressed": str(data.get("compressed", "") or "").strip(),
    }


# --------------------------------------------------------------------------- #


@prompt_restrict(CoTPatternClassifyCompressPrompt)
@OPERATOR_REGISTRY.register()
class CoTPatternRefinerFast(OperatorABC):
    """Fast variant of :class:`CoTPatternRefiner`."""

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        classify_compress_prompt: Union[
            CoTPatternClassifyCompressPrompt, DIYPromptABC
        ] = None,
        preset: str = "balanced",
        action_overrides: Optional[dict] = None,
        min_fragments_to_keep: int = 2,
        min_chars_to_clean: int = 2000,
        min_fragment_chars: int = 400,
        unn_expl_keep_ratio: float = 0.5,
        sampling_seed: int = 0xC07C,
    ):
        """
        Parameters
        ----------
        unn_expl_keep_ratio : float
            Fraction of UNNECESSARY_EXPLORATION fragments to keep verbatim
            (instead of replacing them with the LLM's compressed summary).
            This is a style-diversity guard: if every failed exploration is
            squashed into the LLM's preferred phrasing, the training data
            ends up with a characteristic formulaic template that the model
            could memorise.  By routing a sampled half to keep-as-is we
            preserve the original R1 exploration language on that half.
            ``0.0`` = always compress (v2 behaviour), ``1.0`` = always keep.
            Only used when the effective action for UNNECESSARY_EXPLORATION
            is ``compress`` (i.e. in the "balanced" preset and similar).
        sampling_seed : int
            Seed used for the deterministic per-(row, fragment) sampling
            decision so reruns are reproducible.
        """
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.classify_compress_prompt = (
            classify_compress_prompt or CoTPatternClassifyCompressPrompt()
        )
        self.min_fragments_to_keep = min_fragments_to_keep
        self.min_chars_to_clean = min_chars_to_clean
        self.min_fragment_chars = min_fragment_chars
        self.unn_expl_keep_ratio = max(0.0, min(1.0, float(unn_expl_keep_ratio)))
        self.sampling_seed = int(sampling_seed)

        # Choose the starting action table from the named preset, then
        # apply any per-type overrides on top.  Unknown presets fall back
        # to "balanced" with a warning so typos don't silently disable
        # protections.
        if preset not in _PRESETS:
            self.logger.warning(
                f"[{self.__class__.__name__}] unknown preset {preset!r}; "
                f"falling back to 'balanced'. Valid: {list(_PRESETS)}"
            )
            preset = "balanced"
        self.preset = preset
        self.action_table: dict[str, str] = dict(_PRESETS[preset])

        if action_overrides:
            for k, v in action_overrides.items():
                ku = k.upper()
                if ku in _ALL_PATTERN_TYPES and v in ("keep", "compress", "delete"):
                    self.action_table[ku] = v
                else:
                    self.logger.warning(
                        f"[{self.__class__.__name__}] Ignoring invalid "
                        f"action_override: {k!r} -> {v!r}"
                    )

    # ------------------------------------------------------------------ #

    def _should_keep_unn_expl(self, row_idx: int, frag_idx: int) -> bool:
        """Deterministic sampling for UNN_EXPL keep-vs-compress decision.

        Uses a hash of (seed, row_idx, frag_idx) reduced to [0, 1) and
        compares against ``self.unn_expl_keep_ratio``.  The same row+frag
        index will always land on the same side across reruns, so the
        output is reproducible.
        """
        if self.unn_expl_keep_ratio <= 0.0:
            return False
        if self.unn_expl_keep_ratio >= 1.0:
            return True
        # Python's built-in hash() is salted per-process; use a stable
        # algorithm instead.
        import hashlib
        h = hashlib.md5(
            f"{self.sampling_seed}:{row_idx}:{frag_idx}".encode()
        ).digest()
        # Take first 8 bytes as a uint64, normalise to [0, 1)
        u = int.from_bytes(h[:8], "big") / 2 ** 64
        return u < self.unn_expl_keep_ratio

    # ------------------------------------------------------------------ #

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method D Fast：Thinking Pattern 九分类（全量扁平化 + 合并调用版）。\n"
                "输入：长 CoT；把全表 fragment 一次性 flatten 到 LLM，一次 JSON 回复"
                "同时给出类型与压缩结果，无需第二次调用。\n"
                "输入参数：\n"
                "  - llm_serving（推荐 APILLMServing_pool）\n"
                "  - classify_compress_prompt\n"
                "  - action_overrides：覆盖默认 keep/compress/delete 动作\n"
                "  - min_fragments_to_keep（默认 2）\n"
                "  - min_chars_to_clean（默认 2000）\n"
            )
        return (
            "Method D Fast: flattened, merged-call Thinking Pattern refiner.\n"
        )

    # ------------------------------------------------------------------ #

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(
                f"[{self.__class__.__name__}] Missing required column(s): {missing}"
            )

    # ------------------------------------------------------------------ #

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "cot",
        output_key: str = "cot_cleaned",
        output_stats_key: str = "cot_clean_stats",
        problem_key: Optional[str] = None,
    ) -> list[str]:
        self.input_key = input_key
        dataframe = storage.read("dataframe")
        self._validate_dataframe(dataframe)

        # Phase 1: per-row preprocess + global prompt batch.
        per_row: list[dict] = []
        flat_prompts: list[str] = []

        for row_idx, row in dataframe.iterrows():
            raw_text = str(row[input_key]) if pd.notna(row[input_key]) else ""
            cot, answer_suffix = _parse_cot_from_r1(raw_text)
            problem = (
                str(row[problem_key])
                if (
                    problem_key
                    and problem_key in dataframe.columns
                    and pd.notna(row[problem_key])
                )
                else ""
            )
            record = {
                "row_idx": row_idx,
                "raw_text": raw_text,
                "cot": cot,
                "answer_suffix": answer_suffix,
                "problem": problem,
                "fragments": [],
                "results": [],  # list[dict] aligned with fragments
                "status": "processed",
            }
            if not cot.strip():
                record["status"] = "skipped_empty"
                per_row.append(record)
                continue
            if (
                self.min_chars_to_clean > 0
                and len(cot) < self.min_chars_to_clean
            ):
                record["status"] = "skipped_short"
                per_row.append(record)
                continue

            frags = _split_into_fragments(cot, self.min_fragment_chars)
            record["fragments"] = frags
            per_row.append(record)

            for frag in frags:
                flat_prompts.append(
                    self.classify_compress_prompt.build_prompt(
                        problem=problem, fragment=frag
                    )
                )

        self.logger.info(
            f"[{self.__class__.__name__}] rows={len(per_row)} "
            f"flat_prompts={len(flat_prompts)} "
            f"skipped_empty={sum(1 for r in per_row if r['status'] == 'skipped_empty')} "
            f"skipped_short={sum(1 for r in per_row if r['status'] == 'skipped_short')}"
        )

        # Phase 2: one global LLM batch.
        flat_responses: list[str] = []
        if flat_prompts:
            try:
                flat_responses = self.llm_serving.generate_from_input(
                    user_inputs=flat_prompts
                )
            except Exception as exc:
                self.logger.warning(
                    f"[{self.__class__.__name__}] global batch failed: {exc}"
                )
                flat_responses = [None] * len(flat_prompts)

        # Phase 3: redistribute results.
        cursor = 0
        for record in per_row:
            n = len(record["fragments"])
            if n == 0:
                continue
            slice_ = flat_responses[cursor : cursor + n]
            cursor += n
            record["results"] = [
                _parse_pattern_classify_compress_response(r) for r in slice_
            ]
            # Apply action_overrides.
            for res in record["results"]:
                ptype = res["type"]
                res["effective_action"] = self.action_table.get(
                    ptype, res["recommendation"]
                )

        # Phase 4: assemble.
        cleaned_cots: list[str] = []
        stats_list: list[str] = []

        for record in per_row:
            if record["status"] == "skipped_empty":
                cleaned_cots.append(record["raw_text"])
                stats_list.append(json.dumps({"skipped": True}))
                continue
            if record["status"] == "skipped_short":
                cleaned_cots.append(record["raw_text"])
                stats_list.append(
                    json.dumps(
                        {
                            "skipped": True,
                            "reason": "short",
                            "original_chars": len(record["cot"]),
                        }
                    )
                )
                continue

            frags = record["fragments"]
            results = record["results"]
            out_frags: list[str] = []
            pattern_counts: dict[str, int] = {}
            action_counts = {"keep": 0, "compress": 0, "delete": 0}
            # Stats specific to UNN_EXPL sampled-mixed behaviour.
            unn_expl_kept = 0
            unn_expl_compressed = 0

            for i, (frag, res) in enumerate(zip(frags, results)):
                ptype = res["type"]
                action = res["effective_action"]

                # Sampled mixed-mode guard: when the configured action for
                # UNN_EXPL is "compress", we flip a deterministic coin to
                # keep ``unn_expl_keep_ratio`` of them verbatim.  This
                # preserves R1's original exploration language on that
                # half and protects the training set from phrase templates
                # the LLM prefers.
                if (
                    ptype == "UNNECESSARY_EXPLORATION"
                    and action == "compress"
                    and self._should_keep_unn_expl(record["row_idx"], i)
                ):
                    action = "keep"
                    unn_expl_kept += 1
                elif ptype == "UNNECESSARY_EXPLORATION" and action == "compress":
                    unn_expl_compressed += 1

                pattern_counts[ptype] = pattern_counts.get(ptype, 0) + 1
                action_counts[action] = action_counts.get(action, 0) + 1

                if action == "delete":
                    survivors_after = sum(
                        1 for j, r in enumerate(results)
                        if j >= i and r["effective_action"] != "delete"
                    )
                    if len(out_frags) + survivors_after >= self.min_fragments_to_keep:
                        continue
                    out_frags.append(frag)  # force-keep
                elif action == "compress":
                    out_frags.append(res["compressed"] or frag)
                else:
                    out_frags.append(frag)

            cleaned_cot = "\n\n".join(out_frags)
            stats = {
                "original_fragments": len(frags),
                "output_fragments": len(out_frags),
                "action_counts": action_counts,
                "pattern_type_counts": pattern_counts,
                "unn_expl_kept": unn_expl_kept,
                "unn_expl_compressed": unn_expl_compressed,
                "original_chars": len(record["cot"]),
                "output_chars": len(cleaned_cot),
            }

            raw_text = record["raw_text"]
            if "<think>" in raw_text:
                if record["answer_suffix"]:
                    full = (
                        f"<think>\n{cleaned_cot}\n</think>\n"
                        f"<answer>\n{record['answer_suffix']}\n</answer>"
                    )
                else:
                    full = f"<think>\n{cleaned_cot}\n</think>"
            else:
                full = cleaned_cot

            cleaned_cots.append(full)
            stats_list.append(json.dumps(stats, ensure_ascii=False))

        # Detect rows that hit the LLM classification-parse fallback.  Sign:
        # virtually every fragment is CORE_REASONING and the action is keep,
        # i.e. the row was passed through almost unchanged.  The exact bug
        # this guards against is what we saw on D_old rows 11-18, where the
        # entire classify batch raised and the old code returned an
        # all-CORE_REASONING list without surfacing the failure.
        suspect = []
        for record, stats_s in zip(per_row, stats_list):
            if record["status"] != "processed":
                continue
            try:
                s = json.loads(stats_s)
            except Exception:
                continue
            orig = s.get("original_fragments") or 0
            if orig < 5:
                continue
            pattern_counts = s.get("pattern_type_counts") or {}
            action_counts = s.get("action_counts") or {}
            core_pct = pattern_counts.get("CORE_REASONING", 0) / orig
            keep_pct = action_counts.get("keep", 0) / orig
            if core_pct > 0.95 and keep_pct > 0.95:
                suspect.append(record["row_idx"])
        if suspect:
            self.logger.warning(
                f"[{self.__class__.__name__}] Silent fallback suspected on "
                f"{len(suspect)}/{len(per_row)} rows (>95% CORE_REASONING + "
                f"keep, likely LLM classify-batch error). "
                f"Example row indices: {suspect[:10]}"
            )

        dataframe[output_key] = cleaned_cots
        dataframe[output_stats_key] = stats_list

        output_file = storage.write(dataframe)
        self.logger.info(
            f"[{self.__class__.__name__}] Cleaned CoT saved to {output_file}"
        )
        return [output_key, output_stats_key]
