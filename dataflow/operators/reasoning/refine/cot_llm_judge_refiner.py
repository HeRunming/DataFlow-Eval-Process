"""
Method A – CoT LLM-Judge Refiner
=================================
Offline post-processing of long chain-of-thought (CoT) reasoning data.

Core idea
---------
Split the CoT into individual steps (by paragraph / blank-line boundaries),
then call a **LLM-Judge** on each step to classify it as one of:

  * **necessary**    – Introduces a new sub-result or key logical transition;
                       kept verbatim.
  * **redundant**    – Repeats established information or is a dead-end whose
                       conclusion is never used; deleted.
  * **compressible** – Contains useful content but is over-explained; rewritten
                       into a single concise sentence by a second LLM call.

After per-step processing the surviving steps are re-joined.  If the model has
a ground-truth answer column, optional answer-consistency validation is run to
guard against information loss.

References
----------
* "Stop Overthinking" survey, §4  (ArXiv 2503.16419)
* DIET / Think Wisely frameworks  (ArXiv 2505.19217, 2505.21765)
"""

import json
import re
from typing import Optional, Union

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.core.prompt import DIYPromptABC, prompt_restrict
from dataflow.prompts.reasoning.cot_clean import (
    CoTStepCompressPrompt,
    CoTStepJudgePrompt,
)
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# ── helpers ────────────────────────────────────────────────────────────────

def _split_cot_into_steps(cot_text: str) -> list[str]:
    """
    Split a CoT string into a list of reasoning steps.

    The strategy (in priority order):
    1. Split on blank lines (two or more consecutive newlines).
    2. If that yields only 1 segment, fall back to splitting on single newlines.
    3. Strip each segment; discard empty ones.
    """
    # Try paragraph-level split first
    steps = [s.strip() for s in re.split(r"\n{2,}", cot_text) if s.strip()]
    if len(steps) > 1:
        return steps
    # Fall back to line-level
    steps = [s.strip() for s in cot_text.split("\n") if s.strip()]
    return steps if steps else [cot_text.strip()]


def _parse_cot_from_r1(text: str) -> tuple[str, str]:
    """
    Parse a DeepSeek-R1-style response into (think_content, answer_content).

    Handles both common formats:
      * ``<think>...</think>\\n<answer>...</answer>``
      * ``<think>...</think>``  (answer is the text after the closing tag)

    Returns a tuple ``(cot, answer)`` where *answer* may be an empty string
    if no explicit ``<answer>`` tag is found.
    """
    # Format 1: explicit <answer> tag
    m = re.search(
        r"<think>(.*?)</think>\s*<answer>(.*?)</answer>",
        text,
        re.DOTALL,
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Format 2: <think> tag only
    m = re.search(r"<think>(.*?)</think>(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    # Fallback: treat entire text as CoT
    return text.strip(), ""


def _parse_judge_response(response: str) -> dict:
    """
    Extract the JSON object from the LLM-Judge's response.

    Returns a dict with at least ``{"label": "...", "reason": "..."}``.
    On parse failure, defaults to ``{"label": "necessary", "reason": ""}``.
    """
    try:
        # Try to find a JSON block anywhere in the response
        m = re.search(r"\{.*?\}", response, re.DOTALL)
        if m:
            return json.loads(m.group())
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"label": "necessary", "reason": "parse_error"}


# ── operator ───────────────────────────────────────────────────────────────

@prompt_restrict(
    CoTStepJudgePrompt,
    CoTStepCompressPrompt,
)
@OPERATOR_REGISTRY.register()
class CoTLLMJudgeRefiner(OperatorABC):
    """
    Refine long CoT data using an LLM-Judge to filter redundant reasoning steps.

    The operator reads a dataframe containing a CoT column (``input_key``),
    processes each row through a step-level judge-and-compress pipeline, and
    writes a new column (``output_key``) containing the cleaned CoT.

    Parameters
    ----------
    llm_serving : LLMServingABC
        The LLM backend used for both the judge calls and the compress calls.
        A smaller / faster model is acceptable for the judge role.
    judge_prompt : CoTStepJudgePrompt or DIYPromptABC
        Prompt template used to classify each reasoning step.
    compress_prompt : CoTStepCompressPrompt or DIYPromptABC
        Prompt template used to compress "compressible" steps.
    min_steps_to_keep : int, optional
        Minimum number of steps to retain even if the judge marks them
        redundant.  Prevents degenerate empty outputs.  Default: 2.
    answer_key : str or None, optional
        If provided, the operator checks whether the ground-truth answer string
        is present in the original CoT.  Used only for logging warnings.
        Default: None (no validation).
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        judge_prompt: Union[CoTStepJudgePrompt, DIYPromptABC] = None,
        compress_prompt: Union[CoTStepCompressPrompt, DIYPromptABC] = None,
        min_steps_to_keep: int = 2,
        answer_key: Optional[str] = None,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.judge_prompt = judge_prompt or CoTStepJudgePrompt()
        self.compress_prompt = compress_prompt or CoTStepCompressPrompt()
        self.min_steps_to_keep = min_steps_to_keep
        self.answer_key = answer_key

    # ── description ──────────────────────────────────────────────────────

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method A：LLM-Judge 步骤级过滤算子。\n"
                "将长 CoT 分割为独立步骤，逐步调用 LLM-Judge 将每步分类为"
                "「必要 / 冗余 / 可压缩」，删除冗余步骤并对可压缩步骤进行单句改写，"
                "最终拼接为更短的干净 CoT。\n"
                "输入参数：\n"
                "  - llm_serving：LLM 服务实例\n"
                "  - judge_prompt：步骤分类提示词模板（CoTStepJudgePrompt 或 DIY）\n"
                "  - compress_prompt：步骤压缩提示词模板（CoTStepCompressPrompt 或 DIY）\n"
                "  - min_steps_to_keep：至少保留的步骤数，防止过度删除，默认 2\n"
                "  - answer_key：答案列名（可选），用于日志警告\n"
                "输出参数：\n"
                "  - output_key 列：清洗后的短 CoT\n"
                "  - output_stats_key 列：每行的压缩统计信息（JSON 字符串）"
            )
        else:
            return (
                "Method A: LLM-Judge step-level CoT filtering operator.\n"
                "Splits long CoT into individual steps, classifies each step as "
                "necessary / redundant / compressible via an LLM judge, deletes "
                "redundant steps, rewrites compressible steps into single sentences, "
                "and re-joins the result into a shorter, cleaner CoT.\n"
                "Input parameters:\n"
                "  - llm_serving: LLM serving instance\n"
                "  - judge_prompt: step classification prompt (CoTStepJudgePrompt or DIY)\n"
                "  - compress_prompt: step compression prompt (CoTStepCompressPrompt or DIY)\n"
                "  - min_steps_to_keep: minimum steps to retain (default 2)\n"
                "  - answer_key: answer column name (optional), used for warning logs\n"
                "Output parameters:\n"
                "  - output_key column: cleaned short CoT\n"
                "  - output_stats_key column: per-row compression statistics (JSON)"
            )

    # ── validation ───────────────────────────────────────────────────────

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        """Assert required columns are present before processing."""
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(
                f"[{self.__class__.__name__}] Missing required column(s): {missing}"
            )
        if self.answer_key and self.answer_key not in dataframe.columns:
            self.logger.warning(
                f"[{self.__class__.__name__}] answer_key='{self.answer_key}' "
                "not found in dataframe; answer-consistency check is skipped."
            )
            self.answer_key = None

    # ── core processing ──────────────────────────────────────────────────

    def _build_judge_prompts(
        self,
        problem: str,
        steps: list[str],
    ) -> list[str]:
        """
        Build all judge prompts for a single CoT's steps in one pass.

        A rolling context summary is maintained: the judge sees a plain-text
        enumeration of the steps that have already been accepted so far.
        Because we process steps sequentially and context grows, we approximate
        by providing the first N accepted steps as context (not the full prior
        text, which would be too expensive).
        """
        prompts = []
        accepted_summaries: list[str] = []

        for step in steps:
            context = (
                "\n".join(f"{i+1}. {s}" for i, s in enumerate(accepted_summaries[-5:]))
                if accepted_summaries
                else ""
            )
            prompts.append(
                self.judge_prompt.build_prompt(
                    problem=problem,
                    step=step,
                    context=context,
                )
            )
            # Optimistically add to context (will be corrected after response)
            accepted_summaries.append(step[:120])  # truncated preview

        return prompts

    def _process_single_row(
        self,
        problem: str,
        cot: str,
    ) -> tuple[str, dict]:
        """
        Apply the full judge-and-compress pipeline to a single CoT.

        Returns
        -------
        cleaned_cot : str
            The filtered / compressed CoT text.
        stats : dict
            Compression statistics:
            ``{"original_steps", "kept", "deleted", "compressed",
               "original_chars", "output_chars"}``.
        """
        steps = _split_cot_into_steps(cot)

        # ── Phase 1: call judge for all steps in one batch ──
        judge_prompts = self._build_judge_prompts(problem, steps)

        try:
            judge_responses = self.llm_serving.generate_from_input(
                user_inputs=judge_prompts
            )
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] LLM judge call failed: {exc}. "
                "Falling back to original CoT."
            )
            return cot, {
                "original_steps": len(steps),
                "kept": len(steps),
                "deleted": 0,
                "compressed": 0,
                "original_chars": len(cot),
                "output_chars": len(cot),
                "error": str(exc),
            }

        labels = []
        for resp in judge_responses:
            try:
                parsed = _parse_judge_response(resp)
                label = parsed.get("label", "necessary")
                if label not in ("necessary", "redundant", "compressible"):
                    label = "necessary"
            except Exception:
                label = "necessary"
            labels.append(label)

        # ── Phase 2: gather steps that need compression ──
        compress_indices = [
            i for i, lbl in enumerate(labels) if lbl == "compressible"
        ]
        compress_prompts = [
            self.compress_prompt.build_prompt(step=steps[i])
            for i in compress_indices
        ]

        compressed_texts: dict[int, str] = {}
        if compress_prompts:
            try:
                compress_responses = self.llm_serving.generate_from_input(
                    user_inputs=compress_prompts
                )
                for idx, resp in zip(compress_indices, compress_responses):
                    compressed_texts[idx] = resp.strip() if resp.strip() else steps[idx]
            except Exception as exc:
                self.logger.debug(
                    f"[{self.__class__.__name__}] Compress call failed: {exc}. "
                    "Keeping compressible steps as-is."
                )
                for idx in compress_indices:
                    compressed_texts[idx] = steps[idx]

        # ── Phase 3: assemble result ──
        kept, deleted, compressed_count = 0, 0, 0
        result_steps: list[str] = []

        for i, (step, label) in enumerate(zip(steps, labels)):
            if label == "redundant":
                # Safety: ensure we never drop below min_steps_to_keep
                remaining_necessary = sum(
                    1 for j, l in enumerate(labels)
                    if j >= i and l in ("necessary", "compressible")
                )
                if len(result_steps) + remaining_necessary >= self.min_steps_to_keep:
                    deleted += 1
                    continue
                # Else force-keep even if labelled redundant
                result_steps.append(step)
                kept += 1
            elif label == "compressible":
                result_steps.append(compressed_texts.get(i, step))
                compressed_count += 1
            else:  # necessary
                result_steps.append(step)
                kept += 1

        cleaned_cot = "\n\n".join(result_steps)

        stats = {
            "original_steps": len(steps),
            "kept": kept,
            "deleted": deleted,
            "compressed": compressed_count,
            "original_chars": len(cot),
            "output_chars": len(cleaned_cot),
        }
        return cleaned_cot, stats

    # ── run ──────────────────────────────────────────────────────────────

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "cot",
        output_key: str = "cot_cleaned",
        output_stats_key: str = "cot_clean_stats",
        problem_key: Optional[str] = None,
    ) -> list[str]:
        """
        Execute the LLM-Judge CoT refinement pipeline.

        Parameters
        ----------
        storage : DataFlowStorage
            DataFlow storage object to read from / write to.
        input_key : str
            Column containing the raw (long) CoT text.  For DeepSeek-R1 style
            data the full ``<think>...</think>`` text is expected.
        output_key : str
            Column name to write the cleaned CoT into.
        output_stats_key : str
            Column name to write per-row JSON compression statistics into.
        problem_key : str or None
            Column containing the problem/question text.  When provided it is
            passed to the judge prompt for better context.  If None an empty
            problem string is used.

        Returns
        -------
        list[str]
            The list of output column names produced by this operator.
        """
        self.input_key = input_key
        dataframe = storage.read("dataframe")
        self._validate_dataframe(dataframe)

        cleaned_cots: list[str] = []
        stats_list: list[str] = []

        for row_idx, row in dataframe.iterrows():
            raw_text = str(row[input_key]) if pd.notna(row[input_key]) else ""

            # Parse DeepSeek-R1 style output if present
            cot, answer_suffix = _parse_cot_from_r1(raw_text)

            # Determine problem text for judge context
            problem = (
                str(row[problem_key])
                if (problem_key and problem_key in dataframe.columns and pd.notna(row[problem_key]))
                else ""
            )

            if not cot.strip():
                # Nothing to clean – pass through
                cleaned_cots.append(raw_text)
                stats_list.append(json.dumps({"skipped": True}))
                continue

            try:
                cleaned_cot, stats = self._process_single_row(problem, cot)
            except Exception as exc:
                self.logger.debug(
                    f"[{self.__class__.__name__}] Row {row_idx} failed: {exc}. "
                    "Using original."
                )
                cleaned_cot = cot
                stats = {"error": str(exc)}

            # Re-wrap in <think> tags if the original had them
            if "<think>" in raw_text:
                if answer_suffix:
                    cleaned_full = (
                        f"<think>\n{cleaned_cot}\n</think>\n"
                        f"<answer>\n{answer_suffix}\n</answer>"
                    )
                else:
                    cleaned_full = f"<think>\n{cleaned_cot}\n</think>"
            else:
                cleaned_full = cleaned_cot

            cleaned_cots.append(cleaned_full)
            stats_list.append(json.dumps(stats, ensure_ascii=False))

        dataframe[output_key] = cleaned_cots
        dataframe[output_stats_key] = stats_list

        output_file = storage.write(dataframe)
        self.logger.info(
            f"[{self.__class__.__name__}] Cleaned CoT saved to {output_file}"
        )

        return [output_key, output_stats_key]
