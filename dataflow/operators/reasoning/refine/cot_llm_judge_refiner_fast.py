"""
CoTLLMJudgeRefinerFast - flattened, merged-call variant of Method A.
====================================================================

Differences vs. ``CoTLLMJudgeRefiner`` (the slow reference implementation):

1.  Prompts are flattened across the **entire dataframe** before the first
    LLM call, so a single long-lived thread pool can stay saturated from
    start to finish.  The old class issued two ``generate_from_input``
    calls per row (``10_000 * 2 = 20_000`` barriers on a 10k dataset).

2.  Judge and compress are merged into a single JSON response.  When the
    judge labels a step ``compressible`` it also emits the one-sentence
    compression in the same call, cutting the call count by up to 50% on
    real data (roughly one third of steps are usually compressible).

3.  The broken "optimistic rolling context" is removed.  The old code
    attached a truncated preview of the preceding step to every judge
    prompt but never corrected it for deletions, so the context was both
    noisy and expensive.  A/B measurements on 200 rows (reported in the
    accompanying validation note) show no retention-rate change with
    context dropped; prompts are ~10% shorter.

4.  A ``min_chars_to_clean`` threshold lets very short CoTs pass through
    unchanged.  The long-CoT dataset has a heavy left tail; skipping
    rows below ~2k chars typically removes 10-30% of the call volume
    with no compression loss (short CoTs have nothing to compress).

The public contract (column names, stats JSON shape, ``<think>`` re-
wrapping) matches ``CoTLLMJudgeRefiner`` exactly so downstream consumers
do not need to change.
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
    CoTStepJudgeCompressPrompt,
)
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# --------------------------------------------------------------------------- #
# Helpers (mirrored from the reference implementation so we stay a drop-in    #
# replacement).                                                               #
# --------------------------------------------------------------------------- #


def _split_cot_into_steps(cot_text: str, min_step_chars: int = 400) -> list[str]:
    """Split a CoT into reasoning steps, merging very short adjacent steps.

    DeepSeek-R1 output tends to use many paragraph breaks (``\\n\\n``), so a
    naive split can produce 200-500 one-sentence "steps" per CoT, ballooning
    the per-row LLM call count.  We merge consecutive fragments until each
    step is at least ``min_step_chars`` characters (roughly ~80-100 tokens
    for English, ~200-400 tokens for Chinese), matching the "150-400 token"
    guidance from the post-processing deep-dive doc.

    ``min_step_chars=0`` disables merging (legacy behaviour).
    """
    raw = [s.strip() for s in re.split(r"\n{2,}", cot_text) if s.strip()]
    if len(raw) <= 1:
        raw = [s.strip() for s in cot_text.split("\n") if s.strip()]
    if not raw:
        return [cot_text.strip()]

    if min_step_chars <= 0:
        return raw

    merged: list[str] = []
    for piece in raw:
        if merged and len(merged[-1]) < min_step_chars:
            merged[-1] = merged[-1] + "\n\n" + piece
        else:
            merged.append(piece)
    # Also fold a too-short tail into the previous step.
    if len(merged) >= 2 and len(merged[-1]) < min_step_chars:
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


def _parse_judge_compress_response(response: str) -> dict:
    """Extract ``{"label": ..., "compressed": ...}`` from the LLM reply.

    Falls back to ``{"label": "necessary", "compressed": ""}`` on any
    parse failure so the original step text is preserved.  The non-greedy
    ``\\{.*?\\}`` regex used in the old class can truncate at the first
    nested closing brace; we instead try a greedy match and let
    ``json.loads`` validate.
    """
    if not response:
        return {"label": "necessary", "compressed": ""}
    # Strip markdown code fences if the model wrapped the JSON.
    stripped = response.strip()
    stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
    stripped = re.sub(r"\s*```$", "", stripped)
    # Find the outermost JSON object.
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return {"label": "necessary", "compressed": ""}
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return {"label": "necessary", "compressed": ""}
    label = str(data.get("label", "necessary")).lower()
    if label not in ("necessary", "redundant", "compressible"):
        label = "necessary"
    compressed = str(data.get("compressed", "") or "").strip()
    return {"label": label, "compressed": compressed}


# --------------------------------------------------------------------------- #
# Operator                                                                    #
# --------------------------------------------------------------------------- #


@prompt_restrict(CoTStepJudgeCompressPrompt)
@OPERATOR_REGISTRY.register()
class CoTLLMJudgeRefinerFast(OperatorABC):
    """Fast variant of :class:`CoTLLMJudgeRefiner`.

    Parameters
    ----------
    llm_serving : LLMServingABC
        Backend that honours ``generate_from_input``.  For best throughput
        use :class:`APILLMServing_pool`.
    judge_compress_prompt : CoTStepJudgeCompressPrompt or DIYPromptABC
        Combined judge+compress prompt template.
    min_steps_to_keep : int
        Guard against pathological over-deletion.  Default 2.
    min_chars_to_clean : int
        CoTs whose ``<think>`` content is shorter than this are passed
        through unchanged.  Default 2000.  Set to 0 to disable.
    answer_key : str or None
        Optional column used only for logging warnings; never feeds the
        judge.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        judge_compress_prompt: Union[CoTStepJudgeCompressPrompt, DIYPromptABC] = None,
        min_steps_to_keep: int = 2,
        min_chars_to_clean: int = 2000,
        min_step_chars: int = 400,
        answer_key: Optional[str] = None,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.judge_compress_prompt = judge_compress_prompt or CoTStepJudgeCompressPrompt()
        self.min_steps_to_keep = min_steps_to_keep
        self.min_chars_to_clean = min_chars_to_clean
        self.min_step_chars = min_step_chars
        self.answer_key = answer_key

    # ------------------------------------------------------------------ #

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method A Fast：LLM-Judge 步骤级过滤算子（全量扁平化 + 合并调用版）。\n"
                "与 CoTLLMJudgeRefiner 相同语义，但把所有 row 的 step 一次性 flatten "
                "交给 LLM serving，并把 judge+compress 合并为单次调用。\n"
                "输入参数：\n"
                "  - llm_serving：推荐配合 APILLMServing_pool 使用\n"
                "  - judge_compress_prompt：合并的分类+改写提示词\n"
                "  - min_steps_to_keep：至少保留的步骤数，默认 2\n"
                "  - min_chars_to_clean：短 CoT 跳过阈值（字符），默认 2000\n"
                "  - answer_key：仅用于警告日志\n"
                "输出参数：\n"
                "  - output_key 列：清洗后的短 CoT\n"
                "  - output_stats_key 列：每行的压缩统计 JSON"
            )
        return (
            "Method A Fast: flattened, merged-call LLM-Judge refiner.\n"
            "Same semantics as CoTLLMJudgeRefiner but flattens steps across "
            "the whole dataframe into a single LLM batch and merges the "
            "judge+compress round-trips into one JSON response.\n"
            "Inputs:\n"
            "  - llm_serving (APILLMServing_pool recommended)\n"
            "  - judge_compress_prompt (combined template)\n"
            "  - min_steps_to_keep (default 2)\n"
            "  - min_chars_to_clean (default 2000, skip short CoTs)\n"
            "  - answer_key (warn-log only)\n"
        )

    # ------------------------------------------------------------------ #

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(
                f"[{self.__class__.__name__}] Missing required column(s): {missing}"
            )
        if self.answer_key and self.answer_key not in dataframe.columns:
            self.logger.warning(
                f"[{self.__class__.__name__}] answer_key='{self.answer_key}' "
                "not in dataframe; answer-consistency check skipped."
            )
            self.answer_key = None

    # ------------------------------------------------------------------ #
    # Core pipeline                                                      #
    # ------------------------------------------------------------------ #

    def _preprocess_rows(
        self,
        dataframe: pd.DataFrame,
        input_key: str,
        problem_key: Optional[str],
    ) -> tuple[
        list[dict],   # per-row record with parsed CoT / problem / reason
        list[str],    # flat prompt list
        list[tuple[int, int]],  # (row_idx, step_idx) aligned with prompts
    ]:
        """First pass: parse every row, decide whether to skip, and build
        the flat list of prompts for the whole dataframe."""
        per_row: list[dict] = []
        flat_prompts: list[str] = []
        flat_ids: list[tuple[int, int]] = []

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
                "steps": [],
                "labels": [],
                "compressed_by_step": {},
                "status": "processed",  # or "skipped_empty" / "skipped_short"
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

            steps = _split_cot_into_steps(cot, self.min_step_chars)
            record["steps"] = steps
            per_row.append(record)

            start = len(flat_prompts)
            for step in steps:
                flat_prompts.append(
                    self.judge_compress_prompt.build_prompt(
                        problem=problem, step=step
                    )
                )
                flat_ids.append((row_idx, len(flat_ids) - start))

        return per_row, flat_prompts, flat_ids

    def _assemble_row(self, record: dict) -> tuple[str, dict]:
        cot = record["cot"]
        steps = record["steps"]
        labels = record["labels"]
        compressed_by_step = record["compressed_by_step"]

        kept = deleted = compressed_count = 0
        out_steps: list[str] = []

        for i, (step, label) in enumerate(zip(steps, labels)):
            if label == "redundant":
                remaining = sum(
                    1 for j, l in enumerate(labels)
                    if j >= i and l in ("necessary", "compressible")
                )
                if len(out_steps) + remaining >= self.min_steps_to_keep:
                    deleted += 1
                    continue
                out_steps.append(step)
                kept += 1
            elif label == "compressible":
                out_steps.append(compressed_by_step.get(i) or step)
                compressed_count += 1
            else:  # necessary
                out_steps.append(step)
                kept += 1

        cleaned_cot = "\n\n".join(out_steps)
        stats = {
            "original_steps": len(steps),
            "kept": kept,
            "deleted": deleted,
            "compressed": compressed_count,
            "original_chars": len(cot),
            "output_chars": len(cleaned_cot),
        }
        return cleaned_cot, stats

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

        # Phase 1: parse + build flat prompt batch.
        per_row, flat_prompts, flat_ids = self._preprocess_rows(
            dataframe, input_key, problem_key
        )

        self.logger.info(
            f"[{self.__class__.__name__}] rows={len(per_row)} "
            f"flat_prompts={len(flat_prompts)} "
            f"skipped_empty={sum(1 for r in per_row if r['status'] == 'skipped_empty')} "
            f"skipped_short={sum(1 for r in per_row if r['status'] == 'skipped_short')}"
        )

        # Phase 2: one big LLM call for the whole dataframe.
        flat_responses: list[str] = []
        if flat_prompts:
            try:
                flat_responses = self.llm_serving.generate_from_input(
                    user_inputs=flat_prompts
                )
            except Exception as exc:
                self.logger.warning(
                    f"[{self.__class__.__name__}] global batch failed: "
                    f"{exc}. Falling back to original CoT for all rows."
                )
                flat_responses = [None] * len(flat_prompts)

        # Phase 3: redistribute per-step results back to rows.
        cursor = 0
        for record in per_row:
            n_steps = len(record["steps"])
            if n_steps == 0:
                continue
            row_responses = flat_responses[cursor : cursor + n_steps]
            cursor += n_steps
            labels: list[str] = []
            compressed_by_step: dict[int, str] = {}
            for i, resp in enumerate(row_responses):
                parsed = _parse_judge_compress_response(resp)
                label = parsed["label"]
                labels.append(label)
                if label == "compressible" and parsed["compressed"]:
                    compressed_by_step[i] = parsed["compressed"]
            record["labels"] = labels
            record["compressed_by_step"] = compressed_by_step

        # Phase 4: assemble final column values.
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

            try:
                cleaned_cot, stats = self._assemble_row(record)
            except Exception as exc:
                self.logger.debug(
                    f"[{self.__class__.__name__}] row {record['row_idx']} "
                    f"assembly failed: {exc}"
                )
                cleaned_cot = record["cot"]
                stats = {"error": str(exc)}

            raw_text = record["raw_text"]
            answer_suffix = record["answer_suffix"]
            if "<think>" in raw_text:
                if answer_suffix:
                    full = (
                        f"<think>\n{cleaned_cot}\n</think>\n"
                        f"<answer>\n{answer_suffix}\n</answer>"
                    )
                else:
                    full = f"<think>\n{cleaned_cot}\n</think>"
            else:
                full = cleaned_cot

            cleaned_cots.append(full)
            stats_list.append(json.dumps(stats, ensure_ascii=False))

        # Detect rows that likely hit the LLM-error / parse-error fallback.
        # In Method A that fallback sets every step's label to "necessary"
        # with 0 deletes and 0 compressions, so the row looks unchanged.
        suspect = []
        for record, stats_s in zip(per_row, stats_list):
            if record["status"] != "processed":
                continue
            try:
                s = json.loads(stats_s)
            except Exception:
                continue
            orig = s.get("original_steps") or 0
            if orig < 3:
                continue  # too short to judge
            kept = s.get("kept", 0)
            if kept == orig and s.get("deleted", 0) == 0 and s.get("compressed", 0) == 0:
                suspect.append(record["row_idx"])
        if suspect:
            self.logger.warning(
                f"[{self.__class__.__name__}] Silent fallback suspected on "
                f"{len(suspect)}/{len(per_row)} rows (every step labelled "
                f"'necessary'). Example row indices: {suspect[:10]}"
            )

        dataframe[output_key] = cleaned_cots
        dataframe[output_stats_key] = stats_list

        output_file = storage.write(dataframe)
        self.logger.info(
            f"[{self.__class__.__name__}] Cleaned CoT saved to {output_file}"
        )
        return [output_key, output_stats_key]
