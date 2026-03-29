"""
Method C – CoT Chunk Compress Refiner
=======================================
Offline post-processing of long chain-of-thought (CoT) reasoning data.

Core idea
---------
Rather than operating at token level (which destroys coherence) or at the
whole-CoT level (which loses local reasoning signals), this operator works at
an intermediate **chunk** granularity:

1. **Segment** the CoT into chunks at natural reasoning boundaries (blank
   lines, transition keywords such as "Wait", "Actually", "Let me verify").
2. **Classify** each chunk as *core / exploration / verification / transition*
   using an LLM judge.
3. **Compress** each chunk with type-specific aggressiveness via a second LLM
   call:

   * ``core``         → conservative compression (retain ~85%)
   * ``exploration``  → medium compression (retain ~60%)
   * ``verification`` → aggressive compression (retain ~30%) or deletion
   * ``transition``   → deletion (replaced with a brief connector phrase)

4. Optionally, **generate multiple candidate compressions** per chunk and pick
   the shortest one whose extracted answer still matches the ground truth
   (R1-Compress–style inter-chunk search, simplified to per-chunk selection).

References
----------
* R1-Compress (ArXiv 2505.16838) – chunk-level compression + inter-chunk search
* Kimi k1.5 L2S (ArXiv 2501.12599) – industrial CoT refinement practice
"""

import json
import re
from typing import Optional, Union

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.core.prompt import DIYPromptABC, prompt_restrict
from dataflow.prompts.reasoning.cot_clean import (
    CoTChunkClassifyPrompt,
    CoTChunkRefinePrompt,
)
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# ── constants ──────────────────────────────────────────────────────────────

# Keywords that signal reasoning transitions; used to split CoT into chunks.
_TRANSITION_KEYWORDS = [
    # Error / backtrack
    r"\bwait\b", r"\bactually\b", r"\bhmm\b", r"\boops\b",
    r"\bi made an error\b", r"\bthat'?s wrong\b",
    r"\blet me reconsider\b", r"\blet me re-?think\b",
    r"\blet me re-?examine\b", r"\bhold on\b",
    # New approach
    r"\blet me try\b", r"\banother approach\b", r"\balternatively\b",
    r"\ba different (way|method|approach)\b",
    r"\blet'?s think (about this )?differently\b",
    # Verification
    r"\blet me verify\b", r"\blet me check\b", r"\bto confirm\b",
    r"\bdouble-?check\b", r"\blet me make sure\b",
    # Conclusion
    r"\bso,?\b", r"\btherefore,?\b", r"\bthus,?\b",
    r"\bin summary\b", r"\bto summarize\b",
    r"\bthe (final )?answer is\b",
]

# Per-chunk-type target retention ratios (used in the compress prompt).
_TYPE_RATIOS = {
    "core":         "85%",
    "exploration":  "60%",
    "verification": "30%",
    "transition":   "0%",   # deletion — handled specially
}


# ── helpers ────────────────────────────────────────────────────────────────

def _split_into_chunks(cot_text: str, min_chunk_tokens: int = 30) -> list[str]:
    """
    Split *cot_text* into semantically coherent chunks.

    Strategy:
    1. Split on blank lines (paragraph boundaries).
    2. Within each paragraph, further split at transition-keyword sentence
       boundaries (i.e. a keyword at the start of a sentence).
    3. Merge any fragment shorter than *min_chunk_tokens* words into the
       preceding chunk to avoid overly fine granularity.

    Parameters
    ----------
    cot_text : str
    min_chunk_tokens : int
        Minimum word count per chunk before merging.

    Returns
    -------
    list[str]
        Non-empty chunk strings.
    """
    # Step 1: paragraph split
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", cot_text) if p.strip()]

    # Step 2: within-paragraph split on transition keywords (sentence-start)
    transition_pattern = re.compile(
        r"(?<=[.!?])\s+(?=" + "|".join(_TRANSITION_KEYWORDS) + r")",
        re.IGNORECASE,
    )
    raw_chunks: list[str] = []
    for para in paragraphs:
        sub = [s.strip() for s in transition_pattern.split(para) if s.strip()]
        raw_chunks.extend(sub if sub else [para])

    # Step 3: merge short fragments into the preceding chunk
    merged: list[str] = []
    for chunk in raw_chunks:
        if merged and len(chunk.split()) < min_chunk_tokens:
            merged[-1] = merged[-1] + "\n" + chunk
        else:
            merged.append(chunk)

    return merged if merged else [cot_text.strip()]


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


def _parse_chunk_classify_response(response: str) -> dict:
    """
    Extract JSON from the chunk-classify LLM response.

    Returns a dict with at least ``{"type": "...", "key_info": "..."}``.
    Falls back to ``{"type": "core", "key_info": ""}`` on parse error.
    """
    try:
        m = re.search(r"\{.*?\}", response, re.DOTALL)
        if m:
            data = json.loads(m.group())
            if "type" in data:
                return data
    except (json.JSONDecodeError, AttributeError):
        pass
    return {"type": "core", "key_info": ""}


# ── operator ───────────────────────────────────────────────────────────────

@prompt_restrict(
    CoTChunkClassifyPrompt,
    CoTChunkRefinePrompt,
)
@OPERATOR_REGISTRY.register()
class CoTChunkCompressRefiner(OperatorABC):
    """
    Refine long CoT data via chunk-level classification and targeted rewriting.

    The operator splits each CoT into semantically coherent chunks, classifies
    each chunk, and applies type-specific compression via LLM rewriting.
    ``transition`` chunks are deleted outright; other types are compressed with
    varying aggressiveness.

    Parameters
    ----------
    llm_serving : LLMServingABC
        LLM backend used for both classify and compress calls.
    classify_prompt : CoTChunkClassifyPrompt or DIYPromptABC
        Prompt for classifying chunk type.
    refine_prompt : CoTChunkRefinePrompt or DIYPromptABC
        Prompt for compressing a chunk.
    num_candidates : int
        Number of compression candidates to generate per chunk; the shortest
        valid one is kept.  Set to 1 to disable candidate search.  Default: 1.
    min_chunk_tokens : int
        Minimum word count per chunk during segmentation.  Default: 30.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        classify_prompt: Union[CoTChunkClassifyPrompt, DIYPromptABC] = None,
        refine_prompt: Union[CoTChunkRefinePrompt, DIYPromptABC] = None,
        num_candidates: int = 1,
        min_chunk_tokens: int = 30,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.classify_prompt = classify_prompt or CoTChunkClassifyPrompt()
        self.refine_prompt = refine_prompt or CoTChunkRefinePrompt()
        self.num_candidates = max(1, num_candidates)
        self.min_chunk_tokens = min_chunk_tokens

    # ── description ──────────────────────────────────────────────────────

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method C：Chunk 级别分类 + 差异化压缩算子（参考 R1-Compress）。\n"
                "将 CoT 按推理边界切分为 chunk，对每个 chunk 分类（core/exploration/"
                "verification/transition），再按类型以不同压缩比进行 LLM 改写。"
                "transition 类 chunk 直接删除，其余类型保留不同比例的内容。\n"
                "输入参数：\n"
                "  - llm_serving：LLM 服务实例\n"
                "  - classify_prompt：chunk 分类提示词（CoTChunkClassifyPrompt 或 DIY）\n"
                "  - refine_prompt：chunk 压缩提示词（CoTChunkRefinePrompt 或 DIY）\n"
                "  - num_candidates：每个 chunk 生成的候选压缩数，默认 1\n"
                "  - min_chunk_tokens：切分时 chunk 的最小词数，默认 30\n"
                "输出参数：\n"
                "  - output_key 列：压缩后的短 CoT\n"
                "  - output_stats_key 列：每行的压缩统计信息（JSON 字符串）"
            )
        else:
            return (
                "Method C: Chunk-level classification and targeted rewriting "
                "(inspired by R1-Compress).\n"
                "Splits CoT at reasoning boundaries, classifies each chunk as "
                "core / exploration / verification / transition, then applies "
                "type-specific LLM rewriting.  Transition chunks are deleted; "
                "others are compressed at varying ratios.\n"
                "Input parameters:\n"
                "  - llm_serving: LLM backend\n"
                "  - classify_prompt: chunk classification prompt\n"
                "  - refine_prompt: chunk compression prompt\n"
                "  - num_candidates: compression candidates per chunk, default 1\n"
                "  - min_chunk_tokens: minimum words per chunk, default 30\n"
                "Output parameters:\n"
                "  - output_key column: compressed CoT\n"
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

    def _classify_chunks(
        self, problem: str, chunks: list[str]
    ) -> list[dict]:
        """
        Classify all chunks in a single batched LLM call.

        Returns a list of classification dicts (one per chunk) with at least
        ``{"type": str, "key_info": str}``.
        """
        # Build context summaries (rolling last-2 key_infos)
        prompts: list[str] = []
        for i, chunk in enumerate(chunks):
            context = ""
            prompts.append(
                self.classify_prompt.build_prompt(
                    problem=problem,
                    chunk=chunk,
                    context=context,
                )
            )

        try:
            responses = self.llm_serving.generate_from_input(
                user_inputs=prompts
            )
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] Classify call failed: {exc}. "
                "Defaulting all chunks to 'core'."
            )
            return [{"type": "core", "key_info": ""}] * len(chunks)

        results = []
        for resp in responses:
            try:
                results.append(_parse_chunk_classify_response(resp))
            except Exception:
                results.append({"type": "core", "key_info": ""})
        return results

    def _compress_chunk(
        self,
        problem: str,
        chunk: str,
        chunk_type: str,
        context: str,
    ) -> str:
        """
        Compress a single chunk, optionally selecting the best among
        *num_candidates* candidates.

        Returns the compressed text (or empty string for ``transition`` type).
        """
        if chunk_type == "transition":
            return ""  # delete outright

        target_ratio = _TYPE_RATIOS.get(chunk_type, "60%")
        prompt = self.refine_prompt.build_prompt(
            problem=problem,
            chunk=chunk,
            chunk_type=chunk_type,
            context=context,
            target_ratio=target_ratio,
        )

        # Generate num_candidates candidates
        prompts = [prompt] * self.num_candidates
        try:
            candidates = self.llm_serving.generate_from_input(
                user_inputs=prompts
            )
        except Exception as exc:
            self.logger.debug(
                f"[{self.__class__.__name__}] Compress call failed: {exc}. "
                "Keeping original chunk."
            )
            return chunk

        # Filter out empty / error candidates; pick shortest non-empty one
        valid = [c.strip() for c in candidates if c and c.strip()]
        if not valid:
            return chunk

        return min(valid, key=len)

    def _process_single_row(
        self,
        problem: str,
        cot: str,
    ) -> tuple[str, dict]:
        """
        Apply chunk-classify-compress pipeline to a single CoT.

        Returns
        -------
        cleaned_cot : str
        stats : dict
        """
        chunks = _split_into_chunks(cot, self.min_chunk_tokens)

        # ── Phase 1: classify all chunks in one batch ──
        classifications = self._classify_chunks(problem, chunks)

        # ── Phase 2: compress each chunk ──
        result_chunks: list[str] = []
        context_summary: str = ""
        type_counts: dict[str, int] = {
            "core": 0, "exploration": 0, "verification": 0, "transition": 0
        }
        deleted = 0

        for chunk, cls in zip(chunks, classifications):
            chunk_type = cls.get("type", "core")
            key_info = cls.get("key_info", "")

            # Safety: unknown types treated as core
            if chunk_type not in _TYPE_RATIOS:
                chunk_type = "core"

            type_counts[chunk_type] = type_counts.get(chunk_type, 0) + 1

            try:
                compressed = self._compress_chunk(
                    problem=problem,
                    chunk=chunk,
                    chunk_type=chunk_type,
                    context=context_summary,
                )
            except Exception as exc:
                self.logger.debug(
                    f"[{self.__class__.__name__}] Chunk compress failed: {exc}. "
                    "Keeping original."
                )
                compressed = chunk

            if compressed:
                result_chunks.append(compressed)
                # Update rolling context (use key_info if available)
                ctx_snippet = key_info if key_info else compressed[:100]
                context_summary = ctx_snippet
            else:
                deleted += 1

        cleaned_cot = "\n\n".join(result_chunks)

        stats = {
            "original_chunks": len(chunks),
            "output_chunks": len(result_chunks),
            "deleted_chunks": deleted,
            "chunk_types": type_counts,
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
        Execute the chunk-level CoT compression pipeline.

        Parameters
        ----------
        storage : DataFlowStorage
        input_key : str
            Column with raw CoT text (may include ``<think>`` tags).
        output_key : str
            Column to write compressed CoT into.
        output_stats_key : str
            Column to write per-row JSON statistics into.
        problem_key : str or None
            Column with problem text; improves chunk classification quality.

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
