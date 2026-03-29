"""
Method D – CoT Thinking-Pattern Refiner
=========================================
Offline post-processing of long chain-of-thought (CoT) reasoning data.

Core idea
---------
Inspired by the *Think Wisely, Not Lengthily* framework (ArXiv 2505.21765),
this operator classifies each reasoning fragment into one of **nine Thinking
Pattern types** and then applies a tailored action (keep / compress / delete):

  ┌─────────────────────────────┬──────────────┬─────────────────────────────┐
  │ Pattern type                │ Action       │ Rationale                   │
  ├─────────────────────────────┼──────────────┼─────────────────────────────┤
  │ CORE_REASONING              │ keep         │ New sub-result / key step   │
  │ NECESSARY_VERIFICATION      │ keep         │ Discovers an error          │
  │ COMPUTATION                 │ keep         │ Arithmetic / algebra        │
  │ CONCLUSION                  │ keep         │ States an answer            │
  │ NECESSARY_EXPLORATION       │ compress     │ Informative dead-end        │
  │ REDUNDANT_VERIFICATION      │ compress     │ Re-checks established result│
  │ PREAMBLE                    │ compress     │ Restates problem            │
  │ TRANSITION                  │ compress     │ Filler language             │
  │ UNNECESSARY_EXPLORATION     │ delete       │ Unused dead-end path        │
  └─────────────────────────────┴──────────────┴─────────────────────────────┘

Key finding from the literature: removing *harmful* patterns (especially
UNNECESSARY_EXPLORATION) can *improve* answer accuracy while reducing length,
because these fragments sometimes mislead the model into incorrect conclusions.

References
----------
* Think Wisely, Not Lengthily (ArXiv 2505.21765)
* Not All Thoughts Are Equal  (ArXiv 2505.11827)
"""

import json
import re
from typing import Optional, Union

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.core.prompt import DIYPromptABC, prompt_restrict
from dataflow.prompts.reasoning.cot_clean import (
    CoTPatternClassifyPrompt,
    CoTPatternRefinePrompt,
)
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# ── constants ──────────────────────────────────────────────────────────────

# Default action for each Thinking Pattern type.
_PATTERN_ACTION = {
    "CORE_REASONING":          "keep",
    "NECESSARY_VERIFICATION":  "keep",
    "COMPUTATION":             "keep",
    "CONCLUSION":              "keep",
    "NECESSARY_EXPLORATION":   "compress",
    "REDUNDANT_VERIFICATION":  "compress",
    "PREAMBLE":                "compress",
    "TRANSITION":              "compress",
    "UNNECESSARY_EXPLORATION": "delete",
}

# All recognised pattern type strings.
_ALL_PATTERN_TYPES = set(_PATTERN_ACTION.keys())


# ── helpers ────────────────────────────────────────────────────────────────

def _split_into_fragments(cot_text: str) -> list[str]:
    """
    Split a CoT into reasonably sized fragments for pattern classification.

    Uses blank-line boundaries first; falls back to single-newline splitting
    if that gives fewer than 2 fragments.  Empty fragments are discarded.
    """
    frags = [f.strip() for f in re.split(r"\n{2,}", cot_text) if f.strip()]
    if len(frags) > 1:
        return frags
    frags = [f.strip() for f in cot_text.split("\n") if f.strip()]
    return frags if frags else [cot_text.strip()]


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


def _parse_pattern_classify_response(response: str) -> dict:
    """
    Extract JSON from the pattern-classify LLM response.

    Returns a dict with keys ``type``, ``key_information``, ``recommendation``.
    Falls back to ``{"type": "CORE_REASONING", "key_information": "",
    "recommendation": "keep"}`` on parse error.
    """
    default = {
        "type": "CORE_REASONING",
        "key_information": "",
        "recommendation": "keep",
    }
    try:
        m = re.search(r"\{.*?\}", response, re.DOTALL)
        if m:
            data = json.loads(m.group())
            # Validate and normalise the type field
            if "type" in data:
                ptype = data["type"].upper()
                if ptype not in _ALL_PATTERN_TYPES:
                    ptype = "CORE_REASONING"
                data["type"] = ptype
                # Derive action from our canonical table if not provided
                data["recommendation"] = _PATTERN_ACTION.get(ptype, "keep")
                return data
    except (json.JSONDecodeError, AttributeError):
        pass
    return default


# ── operator ───────────────────────────────────────────────────────────────

@prompt_restrict(
    CoTPatternClassifyPrompt,
    CoTPatternRefinePrompt,
)
@OPERATOR_REGISTRY.register()
class CoTPatternRefiner(OperatorABC):
    """
    Refine long CoT data using fine-grained Thinking Pattern classification.

    The operator fragments each CoT, classifies every fragment into one of nine
    Thinking Pattern types, then:

    * **keeps** high-value patterns verbatim,
    * **compresses** medium-value patterns into a shorter form via LLM rewriting,
    * **deletes** low-value or harmful patterns outright.

    Unlike simpler binary redundancy filtering, this operator can also *correct*
    data quality by removing misleading exploration paths.

    Parameters
    ----------
    llm_serving : LLMServingABC
        LLM backend used for classify and refine calls.
    classify_prompt : CoTPatternClassifyPrompt or DIYPromptABC
        Prompt for Thinking Pattern classification.
    refine_prompt : CoTPatternRefinePrompt or DIYPromptABC
        Prompt for compressing fragments with action="compress".
    action_overrides : dict[str, str] or None
        Optional per-type action overrides to fine-tune behaviour.
        Example: ``{"REDUNDANT_VERIFICATION": "delete"}`` for more aggressive
        compression.  Keys must be valid pattern type strings.
    min_fragments_to_keep : int
        Hard minimum number of fragments always retained.  Default: 2.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        classify_prompt: Union[CoTPatternClassifyPrompt, DIYPromptABC] = None,
        refine_prompt: Union[CoTPatternRefinePrompt, DIYPromptABC] = None,
        action_overrides: Optional[dict] = None,
        min_fragments_to_keep: int = 2,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.classify_prompt = classify_prompt or CoTPatternClassifyPrompt()
        self.refine_prompt = refine_prompt or CoTPatternRefinePrompt()
        self.min_fragments_to_keep = min_fragments_to_keep

        # Build effective action table (defaults + overrides)
        self.action_table: dict[str, str] = dict(_PATTERN_ACTION)
        if action_overrides:
            for ptype, action in action_overrides.items():
                ptype_upper = ptype.upper()
                if ptype_upper in _ALL_PATTERN_TYPES and action in ("keep", "compress", "delete"):
                    self.action_table[ptype_upper] = action
                else:
                    self.logger.warning(
                        f"[{self.__class__.__name__}] Ignoring invalid "
                        f"action_override: {ptype!r} → {action!r}"
                    )

    # ── description ──────────────────────────────────────────────────────

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method D：Thinking Pattern 九分类 + 差异化处理算子（参考 Think Wisely 框架）。\n"
                "将 CoT 分片后，对每个片段分类为九种思维模式之一，按照"
                "「保留/压缩/删除」三种策略处理：核心推理/必要验证/计算/结论保留，"
                "必要探索/冗余验证/铺垫/过渡压缩，无效探索直接删除。"
                "删除无效探索不仅减少 token，还可能提升答案准确率。\n"
                "输入参数：\n"
                "  - llm_serving：LLM 服务实例\n"
                "  - classify_prompt：模式分类提示词（CoTPatternClassifyPrompt 或 DIY）\n"
                "  - refine_prompt：压缩提示词（CoTPatternRefinePrompt 或 DIY）\n"
                "  - action_overrides：覆盖默认动作的字典，如 {'REDUNDANT_VERIFICATION': 'delete'}\n"
                "  - min_fragments_to_keep：至少保留的片段数，默认 2\n"
                "输出参数：\n"
                "  - output_key 列：清洗后的短 CoT\n"
                "  - output_stats_key 列：每行的压缩统计信息（JSON 字符串）"
            )
        else:
            return (
                "Method D: Fine-grained Thinking Pattern classification and "
                "differential handling (inspired by Think Wisely framework).\n"
                "Fragments the CoT and classifies each fragment into one of nine "
                "Thinking Pattern types, then keeps, compresses, or deletes based "
                "on the pattern.  Removing UNNECESSARY_EXPLORATION can improve "
                "answer accuracy while reducing length.\n"
                "Input parameters:\n"
                "  - llm_serving: LLM backend\n"
                "  - classify_prompt: pattern classification prompt\n"
                "  - refine_prompt: compression prompt\n"
                "  - action_overrides: dict to override default actions per type\n"
                "  - min_fragments_to_keep: hard minimum retained fragments, default 2\n"
                "Output parameters:\n"
                "  - output_key column: cleaned CoT\n"
                "  - output_stats_key column: per-row JSON statistics"
            )

    # ── validation ───────────────────────────────────────────────────────

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(
                f"[{self.__class__.__name__}] Missing required column(s): {missing}"
            )

    # ── core processing ──────────────────────────────────────────────────

    def _classify_all_fragments(
        self,
        problem: str,
        fragments: list[str],
    ) -> list[dict]:
        """
        Classify all fragments in one batched LLM call.

        A rolling *context* string (last accepted key_information values) is
        prepended to each classify prompt to improve judgment quality.

        Returns a list of classification dicts aligned with *fragments*.
        """
        prompts: list[str] = []
        context_parts: list[str] = []

        for frag in fragments:
            context = (
                "\n".join(context_parts[-3:]) if context_parts else ""
            )
            prompts.append(
                self.classify_prompt.build_prompt(
                    problem=problem,
                    fragment=frag,
                    context=context,
                )
            )
            # Optimistically add snippet to context (corrected post-response)
            context_parts.append(frag[:80])

        try:
            responses = self.llm_serving.generate_from_input(
                user_inputs=prompts
            )
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] Classify call failed: {exc}. "
                "Defaulting all fragments to CORE_REASONING/keep."
            )
            return [
                {"type": "CORE_REASONING", "key_information": "", "recommendation": "keep"}
                for _ in fragments
            ]

        results = []
        for resp in responses:
            try:
                results.append(_parse_pattern_classify_response(resp))
            except Exception:
                results.append(
                    {"type": "CORE_REASONING", "key_information": "", "recommendation": "keep"}
                )
        return results

    def _compress_fragment(
        self,
        problem: str,
        fragment: str,
        pattern_type: str,
        key_information: str,
        context: str,
    ) -> str:
        """
        Compress a single fragment via the refine prompt.

        Returns the compressed text.  On LLM failure returns the original.
        """
        prompt = self.refine_prompt.build_prompt(
            problem=problem,
            fragment=fragment,
            pattern_type=pattern_type,
            key_information=key_information,
            context=context,
        )
        try:
            responses = self.llm_serving.generate_from_input(
                user_inputs=[prompt]
            )
            compressed = responses[0].strip() if responses and responses[0].strip() else fragment
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] Compress call failed: {exc}. "
                "Keeping original fragment."
            )
            compressed = fragment
        return compressed

    def _process_single_row(
        self,
        problem: str,
        cot: str,
    ) -> tuple[str, dict]:
        """
        Apply Thinking Pattern classification and differential handling to a
        single CoT.

        Returns
        -------
        cleaned_cot : str
        stats : dict
        """
        fragments = _split_into_fragments(cot)

        # ── Phase 1: classify all fragments ──
        classifications = self._classify_all_fragments(problem, fragments)

        # Resolve effective actions using the action table (may differ from
        # the LLM's own recommendation if action_overrides were specified).
        for cls in classifications:
            ptype = cls.get("type", "CORE_REASONING")
            cls["effective_action"] = self.action_table.get(ptype, "keep")

        # ── Phase 2: build compress batch ──
        compress_batch: list[tuple[int, str, str, str]] = []
        # (original_index, fragment, pattern_type, key_information)

        for i, (frag, cls) in enumerate(zip(fragments, classifications)):
            if cls["effective_action"] == "compress":
                compress_batch.append((
                    i,
                    frag,
                    cls.get("type", "PREAMBLE"),
                    cls.get("key_information", ""),
                ))

        # Build compress prompts and call LLM in one batch
        compressed_texts: dict[int, str] = {}
        if compress_batch:
            context_summary = ""
            compress_prompts = []
            for orig_i, frag, ptype, key_info in compress_batch:
                compress_prompts.append(
                    self.refine_prompt.build_prompt(
                        problem=problem,
                        fragment=frag,
                        pattern_type=ptype,
                        key_information=key_info,
                        context=context_summary,
                    )
                )
                context_summary = key_info[:80] if key_info else frag[:80]

            try:
                compress_responses = self.llm_serving.generate_from_input(
                    user_inputs=compress_prompts
                )
                for (orig_i, frag, _, _), resp in zip(compress_batch, compress_responses):
                    compressed_texts[orig_i] = resp.strip() if resp.strip() else frag
            except Exception as exc:
                self.logger.debug(
                    f"[{self.__class__.__name__}] Batch compress call failed: {exc}. "
                    "Keeping compressible fragments as-is."
                )
                for orig_i, frag, _, _ in compress_batch:
                    compressed_texts[orig_i] = frag

        # ── Phase 3: assemble result ──
        result_frags: list[str] = []
        pattern_type_counts: dict[str, int] = {}
        action_counts: dict[str, int] = {"keep": 0, "compress": 0, "delete": 0}

        for i, (frag, cls) in enumerate(zip(fragments, classifications)):
            ptype = cls.get("type", "CORE_REASONING")
            action = cls["effective_action"]

            pattern_type_counts[ptype] = pattern_type_counts.get(ptype, 0) + 1
            action_counts[action] = action_counts.get(action, 0) + 1

            if action == "delete":
                # Safety: never delete if we'd fall below the minimum
                survivors_after = sum(
                    1 for j, c in enumerate(classifications)
                    if j >= i and c["effective_action"] != "delete"
                )
                if len(result_frags) + survivors_after >= self.min_fragments_to_keep:
                    continue  # delete
                # Force-keep
                result_frags.append(frag)
            elif action == "compress":
                result_frags.append(compressed_texts.get(i, frag))
            else:  # keep
                result_frags.append(frag)

        cleaned_cot = "\n\n".join(result_frags)

        stats = {
            "original_fragments": len(fragments),
            "output_fragments": len(result_frags),
            "action_counts": action_counts,
            "pattern_type_counts": pattern_type_counts,
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
        Execute the Thinking-Pattern CoT refinement pipeline.

        Parameters
        ----------
        storage : DataFlowStorage
        input_key : str
            Column with raw CoT text (may include ``<think>`` tags).
        output_key : str
            Column to write the cleaned CoT into.
        output_stats_key : str
            Column to write per-row JSON statistics into.
        problem_key : str or None
            Column with problem text; used as context for classification.

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

            if not cot.strip():
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
