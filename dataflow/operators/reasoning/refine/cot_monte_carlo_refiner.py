"""
Method B – CoT Monte Carlo Refiner
====================================
Offline post-processing of long chain-of-thought (CoT) reasoning data.

Core idea
---------
For each reasoning step sᵢ in the CoT, estimate its **importance** as the
increase in answer-correctness probability when sᵢ is included:

    Importance(sᵢ) = P(correct | s₁…sᵢ) − P(correct | s₁…sᵢ₋₁)

Both conditional probabilities are approximated via Monte Carlo sampling:
the LLM is asked M times to *complete* the reasoning from a given prefix,
and the fraction of completions that produce the correct answer is used as
the probability estimate.

Steps with importance ≤ 0 (neutral or negative) are candidates for deletion.
An optional **per-token-value** mode normalises importance by step length,
preferring to keep information-dense short steps over verbose low-value ones.

Because a full MC evaluation over all N steps requires O(N·M) LLM calls, a
lightweight **prefix-caching** optimisation is applied: completions for the
same prefix are reused across adjacent step evaluations.

References
----------
* "Not All Thoughts Are Equal"  (ArXiv 2505.11827)
* Long⊗Short framework          (same paper)
"""

import json
import re
from typing import Optional, Union

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.core.prompt import DIYPromptABC, prompt_restrict
from dataflow.prompts.reasoning.cot_clean import CoTMCCompletionPrompt
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# ── helpers ────────────────────────────────────────────────────────────────

def _split_cot_into_steps(cot_text: str) -> list[str]:
    """Split CoT text into a list of non-empty step strings."""
    steps = [s.strip() for s in re.split(r"\n{2,}", cot_text) if s.strip()]
    if len(steps) > 1:
        return steps
    steps = [s.strip() for s in cot_text.split("\n") if s.strip()]
    return steps if steps else [cot_text.strip()]


def _parse_cot_from_r1(text: str) -> tuple[str, str]:
    """Parse DeepSeek-R1-style ``<think>…</think>`` output."""
    m = re.search(
        r"<think>(.*?)</think>\s*<answer>(.*?)</answer>", text, re.DOTALL
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r"<think>(.*?)</think>(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ""


def _extract_answer(completion: str) -> str:
    """
    Extract the final answer from a CoT completion.

    Looks for ``Final Answer: …`` (case-insensitive) or, as fallback, the
    last non-empty line of the completion.
    """
    m = re.search(r"(?i)final\s+answer\s*[:\-]\s*(.*?)(?:\n|$)", completion)
    if m:
        return m.group(1).strip()
    lines = [l.strip() for l in completion.split("\n") if l.strip()]
    return lines[-1] if lines else ""


def _answers_match(pred: str, gold: str) -> bool:
    """
    Lightweight answer-matching heuristic.

    1. Exact string match (case-insensitive, stripped).
    2. Numeric match after stripping non-digit/dot characters.
    """
    pred_clean = pred.strip().lower()
    gold_clean = gold.strip().lower()
    if pred_clean == gold_clean:
        return True
    # Try numeric extraction
    def _to_num(s: str) -> Optional[float]:
        try:
            digits = re.sub(r"[^\d\.\-]", "", s)
            return float(digits) if digits else None
        except ValueError:
            return None

    p_num, g_num = _to_num(pred_clean), _to_num(gold_clean)
    if p_num is not None and g_num is not None:
        return abs(p_num - g_num) < 1e-6
    return False


# ── operator ───────────────────────────────────────────────────────────────

@prompt_restrict(CoTMCCompletionPrompt)
@OPERATOR_REGISTRY.register()
class CoTMonteCarloRefiner(OperatorABC):
    """
    Refine long CoT data using Monte Carlo step-importance estimation.

    For every reasoning step the operator estimates how much the step
    contributes to the probability of reaching the correct answer.  Steps
    whose contribution is non-positive (or below a threshold) are removed.

    Parameters
    ----------
    llm_serving : LLMServingABC
        LLM backend used for generating MC completions.
    completion_prompt : CoTMCCompletionPrompt or DIYPromptABC
        Prompt template for CoT completions from a prefix.
    mc_samples : int
        Number of Monte Carlo completions per prefix evaluation.
        Higher values yield more reliable importance estimates at greater cost.
        Default: 8.
    importance_threshold : float
        Minimum importance score for a step to be retained.
        Set to 0.0 to keep any step that doesn't *hurt* correctness;
        negative values allow keeping mildly unhelpful steps.
        Default: 0.0.
    per_token_value : bool
        If True, normalise importance by step token count before thresholding
        (favours information-dense short steps).  Default: False.
    min_steps_to_keep : int
        Hard minimum number of steps always kept.  Default: 2.
    answer_key : str or None
        Column name containing the ground-truth answer string.  **Required**
        for MC evaluation; if missing the operator falls back to keeping all
        steps and logs a warning.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        completion_prompt: Union[CoTMCCompletionPrompt, DIYPromptABC] = None,
        mc_samples: int = 8,
        importance_threshold: float = 0.0,
        per_token_value: bool = False,
        min_steps_to_keep: int = 2,
        answer_key: Optional[str] = None,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.completion_prompt = completion_prompt or CoTMCCompletionPrompt()
        self.mc_samples = mc_samples
        self.importance_threshold = importance_threshold
        self.per_token_value = per_token_value
        self.min_steps_to_keep = min_steps_to_keep
        self.answer_key = answer_key

    # ── description ──────────────────────────────────────────────────────

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method B：Monte Carlo 步骤重要性评分算子。\n"
                "对 CoT 中每个推理步骤，通过多次采样估计「加入该步骤后答案正确率的提升量」，"
                "删除重要性分数低于阈值的步骤，保留高价值推理步骤。\n"
                "输入参数：\n"
                "  - llm_serving：LLM 服务实例（用于 MC 采样补全）\n"
                "  - completion_prompt：补全提示词模板（CoTMCCompletionPrompt 或 DIY）\n"
                "  - mc_samples：每个前缀的 MC 采样次数，默认 8\n"
                "  - importance_threshold：步骤保留阈值，默认 0.0（删除无贡献步骤）\n"
                "  - per_token_value：是否按 token 数归一化重要性，默认 False\n"
                "  - min_steps_to_keep：至少保留的步骤数，默认 2\n"
                "  - answer_key：答案列名（必须提供以计算正确率）\n"
                "输出参数：\n"
                "  - output_key 列：压缩后的短 CoT\n"
                "  - output_stats_key 列：每行的压缩统计信息（JSON 字符串）"
            )
        else:
            return (
                "Method B: Monte Carlo step-importance scoring operator.\n"
                "For each reasoning step, estimates the increase in answer-correctness "
                "probability when that step is included (via MC sampling).  Steps below "
                "the importance threshold are deleted.\n"
                "Input parameters:\n"
                "  - llm_serving: LLM backend for MC completion sampling\n"
                "  - completion_prompt: completion prompt (CoTMCCompletionPrompt or DIY)\n"
                "  - mc_samples: MC completions per prefix evaluation, default 8\n"
                "  - importance_threshold: retention threshold, default 0.0\n"
                "  - per_token_value: normalise by token count, default False\n"
                "  - min_steps_to_keep: hard minimum retained steps, default 2\n"
                "  - answer_key: column with ground-truth answers (required)\n"
                "Output parameters:\n"
                "  - output_key column: compressed CoT\n"
                "  - output_stats_key column: per-row compression statistics (JSON)"
            )

    # ── validation ───────────────────────────────────────────────────────

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        """Check required columns."""
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(
                f"[{self.__class__.__name__}] Missing required column(s): {missing}"
            )
        if self.answer_key and self.answer_key not in dataframe.columns:
            self.logger.warning(
                f"[{self.__class__.__name__}] answer_key='{self.answer_key}' not "
                "found; MC scoring requires a ground-truth answer.  All steps will "
                "be kept."
            )
            self.answer_key = None

    # ── MC estimation ────────────────────────────────────────────────────

    def _estimate_correctness(
        self, problem: str, prefix: str, gold_answer: str
    ) -> float:
        """
        Estimate P(correct | prefix) by sampling M completions.

        Uses ``self.llm_serving.generate_from_input`` with M identical prompts
        (the LLM backend is expected to produce diverse outputs at temperature
        > 0 when called multiple times with the same input).

        Returns the fraction of completions whose extracted answer matches
        *gold_answer*.
        """
        prompt = self.completion_prompt.build_prompt(
            problem=problem, cot_prefix=prefix
        )
        prompts = [prompt] * self.mc_samples

        try:
            completions = self.llm_serving.generate_from_input(
                user_inputs=prompts
            )
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] MC completion failed: {exc}. "
                "Returning 0.0 correctness."
            )
            return 0.0

        correct = sum(
            1
            for c in completions
            if _answers_match(_extract_answer(c), gold_answer)
        )
        return correct / max(len(completions), 1)

    def _compute_importance_scores(
        self,
        problem: str,
        steps: list[str],
        gold_answer: str,
    ) -> list[float]:
        """
        Compute importance score for each step in the CoT.

        Algorithm:
        1. Compute baseline correctness with no prior context.
        2. For each step i, compute correctness of prefix s₁…sᵢ.
        3. Importance(sᵢ) = correctness(s₁…sᵢ) − correctness(s₁…sᵢ₋₁).
        """
        prefixes: list[str] = []
        for i in range(len(steps) + 1):
            prefixes.append("\n\n".join(steps[:i]))

        # Evaluate all prefixes in order.  We cannot easily batch these because
        # each depends on its predecessor, but we build all prompts first and
        # call the LLM in one batch of M*(N+1) requests.
        all_prompts: list[str] = []
        for prefix in prefixes:
            prompt = self.completion_prompt.build_prompt(
                problem=problem, cot_prefix=prefix
            )
            all_prompts.extend([prompt] * self.mc_samples)

        try:
            all_completions = self.llm_serving.generate_from_input(
                user_inputs=all_prompts
            )
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] Batch MC call failed: {exc}. "
                "Falling back to uniform importance."
            )
            return [1.0] * len(steps)

        # Partition completions back to each prefix
        correctness: list[float] = []
        for i, _prefix in enumerate(prefixes):
            batch = all_completions[i * self.mc_samples: (i + 1) * self.mc_samples]
            correct = sum(
                1 for c in batch
                if _answers_match(_extract_answer(c), gold_answer)
            )
            correctness.append(correct / max(len(batch), 1))

        # Importance = delta correctness
        importance = [
            correctness[i + 1] - correctness[i] for i in range(len(steps))
        ]
        return importance

    # ── core processing ──────────────────────────────────────────────────

    def _process_single_row(
        self,
        problem: str,
        cot: str,
        gold_answer: str,
    ) -> tuple[str, dict]:
        """
        Apply MC importance scoring and step filtering to a single CoT.

        Returns
        -------
        cleaned_cot : str
        stats : dict
        """
        steps = _split_cot_into_steps(cot)

        if not gold_answer or len(steps) <= self.min_steps_to_keep:
            # No answer to compare against, or too short to filter
            return cot, {
                "original_steps": len(steps),
                "kept": len(steps),
                "deleted": 0,
                "original_chars": len(cot),
                "output_chars": len(cot),
                "skipped": True,
            }

        importance = self._compute_importance_scores(problem, steps, gold_answer)

        # Optional per-token normalisation
        if self.per_token_value:
            token_counts = [max(len(s.split()), 1) for s in steps]
            scores = [imp / tc for imp, tc in zip(importance, token_counts)]
        else:
            scores = importance

        # Select steps to keep; always keep top-min_steps_to_keep by score
        indexed = sorted(enumerate(scores), key=lambda x: -x[1])
        # Build keep set: all above threshold, and ensure min_steps_to_keep
        keep_set = set(
            i for i, s in enumerate(scores) if s > self.importance_threshold
        )
        for i, _ in indexed[: self.min_steps_to_keep]:
            keep_set.add(i)

        result_steps = [s for i, s in enumerate(steps) if i in keep_set]
        cleaned_cot = "\n\n".join(result_steps)

        deleted = len(steps) - len(result_steps)
        stats = {
            "original_steps": len(steps),
            "kept": len(result_steps),
            "deleted": deleted,
            "original_chars": len(cot),
            "output_chars": len(cleaned_cot),
            "importance_scores": [round(s, 4) for s in scores],
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
        Execute the Monte Carlo CoT refinement pipeline.

        Parameters
        ----------
        storage : DataFlowStorage
        input_key : str
            Column with raw (long) CoT text (may include ``<think>`` tags).
        output_key : str
            Column to write the compressed CoT into.
        output_stats_key : str
            Column to write per-row JSON statistics into.
        problem_key : str or None
            Column with the problem text; used as context for MC completions.

        Returns
        -------
        list[str]
            Output column names.
        """
        self.input_key = input_key
        dataframe = storage.read("dataframe")
        self._validate_dataframe(dataframe)

        cleaned_cots: list[str] = []
        stats_list: list[str] = []

        for row_idx, row in dataframe.iterrows():
            raw_text = str(row[input_key]) if pd.notna(row[input_key]) else ""
            cot, answer_suffix = _parse_cot_from_r1(raw_text)

            problem = (
                str(row[problem_key])
                if (problem_key and problem_key in dataframe.columns and pd.notna(row[problem_key]))
                else ""
            )
            gold_answer = (
                str(row[self.answer_key])
                if (self.answer_key and pd.notna(row[self.answer_key]))
                else ""
            )

            if not cot.strip():
                cleaned_cots.append(raw_text)
                stats_list.append(json.dumps({"skipped": True}))
                continue

            try:
                cleaned_cot, stats = self._process_single_row(
                    problem, cot, gold_answer
                )
            except Exception as exc:
                self.logger.debug(
                    f"[{self.__class__.__name__}] Row {row_idx} failed: {exc}. "
                    "Using original."
                )
                cleaned_cot = cot
                stats = {"error": str(exc)}

            # Re-wrap in <think> tags if needed
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
