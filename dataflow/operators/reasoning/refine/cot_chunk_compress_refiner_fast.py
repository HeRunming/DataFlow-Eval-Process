"""
CoTChunkCompressRefinerFast - flattened variant of Method C.
============================================================

Differences vs. ``CoTChunkCompressRefiner``:

1.  The entire dataframe's chunks are flattened into a single classify
    batch, and then a single compress batch.  The old code looped
    ``for row in df`` x ``for chunk in row.chunks`` with a separate
    ``generate_from_input`` per chunk (``num_candidates=1`` -> batches
    of size 1!), which pinned throughput at roughly one request per
    round-trip regardless of ``max_workers``.

2.  ``transition`` chunks are dropped before the compress batch is
    built, so we never burn an API call only to throw the result away.

3.  The placeholder "rolling context" (``context=""`` hard-coded in the
    old classify call, and a truncated snippet fed to compress) is
    removed.  The compress prompt already carries enough signal via
    the per-type guidance string.

4.  ``min_chars_to_clean`` lets short CoTs pass through, matching
    Method A Fast.

5.  ``num_candidates`` is still supported but now implemented correctly:
    when > 1, each chunk contributes ``num_candidates`` prompts to the
    flat compress batch, and the shortest returned candidate is chosen.
    (The old code rebuilt an executor for the candidate batch, so the
    multi-candidate branch was unusable in practice.)

The output schema is identical to the reference implementation.
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
    CoTChunkClassifyPrompt,
    CoTChunkRefinePrompt,
)
from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# --------------------------------------------------------------------------- #
# Constants (kept identical to the reference class to preserve semantics).    #
# --------------------------------------------------------------------------- #


_TRANSITION_KEYWORDS = [
    r"\bwait\b", r"\bactually\b", r"\bhmm\b", r"\boops\b",
    r"\bi made an error\b", r"\bthat'?s wrong\b",
    r"\blet me reconsider\b", r"\blet me re-?think\b",
    r"\blet me re-?examine\b", r"\bhold on\b",
    r"\blet me try\b", r"\banother approach\b", r"\balternatively\b",
    r"\ba different (?:way|method|approach)\b",
    r"\blet'?s think (?:about this )?differently\b",
    r"\blet me verify\b", r"\blet me check\b", r"\bto confirm\b",
    r"\bdouble-?check\b", r"\blet me make sure\b",
    r"\bso,?\b", r"\btherefore,?\b", r"\bthus,?\b",
    r"\bin summary\b", r"\bto summarize\b",
    r"\bthe (?:final )?answer is\b",
]

_TYPE_RATIOS = {
    "core":         "85%",
    "exploration":  "60%",
    "verification": "30%",
    "transition":   "0%",
}


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _split_into_chunks(cot_text: str, min_chunk_tokens: int = 30) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", cot_text) if p.strip()]
    transition_pattern = re.compile(
        r"(?<=[.!?])\s+(?=" + "|".join(_TRANSITION_KEYWORDS) + r")",
        re.IGNORECASE,
    )
    raw_chunks: list[str] = []
    for para in paragraphs:
        sub = [s.strip() for s in transition_pattern.split(para) if s.strip()]
        raw_chunks.extend(sub if sub else [para])
    merged: list[str] = []
    for chunk in raw_chunks:
        if merged and len(chunk.split()) < min_chunk_tokens:
            merged[-1] = merged[-1] + "\n" + chunk
        else:
            merged.append(chunk)
    return merged if merged else [cot_text.strip()]


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


def _parse_chunk_classify_response(response: str) -> dict:
    if not response:
        return {"type": "core", "key_info": ""}
    stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.strip())
    m = re.search(r"\{.*\}", stripped, re.DOTALL)
    if not m:
        return {"type": "core", "key_info": ""}
    try:
        data = json.loads(m.group())
    except json.JSONDecodeError:
        return {"type": "core", "key_info": ""}
    ctype = str(data.get("type", "core")).lower()
    if ctype not in _TYPE_RATIOS:
        ctype = "core"
    return {"type": ctype, "key_info": str(data.get("key_info", "") or "")}


# --------------------------------------------------------------------------- #
# Operator                                                                    #
# --------------------------------------------------------------------------- #


@prompt_restrict(CoTChunkClassifyPrompt, CoTChunkRefinePrompt)
@OPERATOR_REGISTRY.register()
class CoTChunkCompressRefinerFast(OperatorABC):
    """Fast variant of :class:`CoTChunkCompressRefiner`."""

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        classify_prompt: Union[CoTChunkClassifyPrompt, DIYPromptABC] = None,
        refine_prompt: Union[CoTChunkRefinePrompt, DIYPromptABC] = None,
        num_candidates: int = 1,
        min_chunk_tokens: int = 30,
        min_chars_to_clean: int = 2000,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.classify_prompt = classify_prompt or CoTChunkClassifyPrompt()
        self.refine_prompt = refine_prompt or CoTChunkRefinePrompt()
        self.num_candidates = max(1, num_candidates)
        self.min_chunk_tokens = min_chunk_tokens
        self.min_chars_to_clean = min_chars_to_clean

    # ------------------------------------------------------------------ #

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Method C Fast：Chunk 分类 + 差异化压缩（全量扁平化版）。\n"
                "把全表 chunk 一次性 flatten 做分类，然后把需要压缩的 chunk 再一次"
                "flatten 做改写，transition 类型不发起 LLM 调用。\n"
                "输入参数：\n"
                "  - llm_serving：推荐 APILLMServing_pool\n"
                "  - classify_prompt / refine_prompt\n"
                "  - num_candidates：每 chunk 压缩候选数，选最短，默认 1\n"
                "  - min_chunk_tokens：合并阈值，默认 30\n"
                "  - min_chars_to_clean：短 CoT 跳过阈值，默认 2000\n"
                "输出参数：\n"
                "  - output_key：压缩后 CoT\n"
                "  - output_stats_key：统计 JSON"
            )
        return (
            "Method C Fast: flattened chunk-level classify + compress.\n"
            "Flattens all chunks across the dataframe into a single classify "
            "batch, then a single compress batch; skips transition chunks; "
            "bypasses short CoTs via min_chars_to_clean."
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

        # ---- Phase 1: per-row preprocess + build global classify batch ----
        per_row: list[dict] = []
        classify_prompts: list[str] = []

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
                "chunks": [],
                "classifications": [],
                "compressed": {},   # chunk_idx -> text
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

            chunks = _split_into_chunks(cot, self.min_chunk_tokens)
            record["chunks"] = chunks
            per_row.append(record)

            for chunk in chunks:
                classify_prompts.append(
                    self.classify_prompt.build_prompt(
                        problem=problem, chunk=chunk, context=""
                    )
                )

        self.logger.info(
            f"[{self.__class__.__name__}] rows={len(per_row)} "
            f"classify_prompts={len(classify_prompts)} "
            f"skipped_empty={sum(1 for r in per_row if r['status'] == 'skipped_empty')} "
            f"skipped_short={sum(1 for r in per_row if r['status'] == 'skipped_short')}"
        )

        # ---- Phase 2: single classify batch ----
        classify_responses: list[str] = []
        if classify_prompts:
            try:
                classify_responses = self.llm_serving.generate_from_input(
                    user_inputs=classify_prompts
                )
            except Exception as exc:
                self.logger.warning(
                    f"[{self.__class__.__name__}] classify batch failed: "
                    f"{exc}. Defaulting all chunks to core."
                )
                classify_responses = [None] * len(classify_prompts)

        cursor = 0
        for record in per_row:
            n = len(record["chunks"])
            if n == 0:
                continue
            slice_ = classify_responses[cursor : cursor + n]
            cursor += n
            record["classifications"] = [
                _parse_chunk_classify_response(r) for r in slice_
            ]

        # ---- Phase 3: build global compress batch (skip transition) ----
        compress_prompts: list[str] = []
        compress_owners: list[tuple[int, int, int]] = []
        # (per_row_index, chunk_idx, candidate_idx)

        for pr_i, record in enumerate(per_row):
            if record["status"] != "processed":
                continue
            for c_i, (chunk, cls) in enumerate(
                zip(record["chunks"], record["classifications"])
            ):
                ctype = cls["type"]
                if ctype == "transition":
                    record["compressed"][c_i] = ""  # deleted
                    continue
                target_ratio = _TYPE_RATIOS.get(ctype, "60%")
                prompt = self.refine_prompt.build_prompt(
                    problem=record["problem"],
                    chunk=chunk,
                    chunk_type=ctype,
                    context="",
                    target_ratio=target_ratio,
                )
                for k in range(self.num_candidates):
                    compress_prompts.append(prompt)
                    compress_owners.append((pr_i, c_i, k))

        self.logger.info(
            f"[{self.__class__.__name__}] compress_prompts={len(compress_prompts)}"
        )

        compress_responses: list[str] = []
        if compress_prompts:
            try:
                compress_responses = self.llm_serving.generate_from_input(
                    user_inputs=compress_prompts
                )
            except Exception as exc:
                self.logger.warning(
                    f"[{self.__class__.__name__}] compress batch failed: "
                    f"{exc}. Keeping original chunks."
                )
                compress_responses = [None] * len(compress_prompts)

        # ---- Phase 4: reduce candidates back onto (row, chunk) ----
        candidates_by_chunk: dict[tuple[int, int], list[str]] = {}
        for (pr_i, c_i, _k), resp in zip(compress_owners, compress_responses):
            if resp and resp.strip():
                candidates_by_chunk.setdefault((pr_i, c_i), []).append(
                    resp.strip()
                )

        for (pr_i, c_i), cands in candidates_by_chunk.items():
            # Shortest non-empty candidate wins.
            per_row[pr_i]["compressed"][c_i] = min(cands, key=len)

        # Fill in any chunk that had no valid candidate (e.g. all errors):
        # fall back to the original chunk text.
        for record in per_row:
            if record["status"] != "processed":
                continue
            for c_i, chunk in enumerate(record["chunks"]):
                if c_i not in record["compressed"]:
                    record["compressed"][c_i] = chunk

        # ---- Phase 5: assemble rows ----
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

            chunks = record["chunks"]
            classifications = record["classifications"]
            result_chunks: list[str] = []
            type_counts = {"core": 0, "exploration": 0, "verification": 0, "transition": 0}
            deleted = 0

            for c_i, (chunk, cls) in enumerate(zip(chunks, classifications)):
                ctype = cls["type"]
                type_counts[ctype] = type_counts.get(ctype, 0) + 1
                out = record["compressed"].get(c_i, chunk)
                if out:
                    result_chunks.append(out)
                else:
                    deleted += 1

            cleaned_cot = "\n\n".join(result_chunks)
            stats = {
                "original_chunks": len(chunks),
                "output_chunks": len(result_chunks),
                "deleted_chunks": deleted,
                "chunk_types": type_counts,
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

        # Detect rows where the classify phase likely fell through to the
        # default (all chunks tagged 'core' and nothing was compressed away).
        suspect = []
        for record, stats_s in zip(per_row, stats_list):
            if record["status"] != "processed":
                continue
            try:
                s = json.loads(stats_s)
            except Exception:
                continue
            orig = s.get("original_chunks") or 0
            if orig < 3:
                continue
            types = s.get("chunk_types") or {}
            core = types.get("core", 0)
            # If all chunks are 'core' AND nothing got shortened noticeably,
            # treat as a silent LLM failure fallback.
            chars_in = s.get("original_chars") or 1
            chars_out = s.get("output_chars") or 0
            if core == orig and chars_out / chars_in > 0.97:
                suspect.append(record["row_idx"])
        if suspect:
            self.logger.warning(
                f"[{self.__class__.__name__}] Silent fallback suspected on "
                f"{len(suspect)}/{len(per_row)} rows (all chunks classified "
                f"as 'core' with no real compression). "
                f"Example row indices: {suspect[:10]}"
            )

        dataframe[output_key] = cleaned_cots
        dataframe[output_stats_key] = stats_list

        output_file = storage.write(dataframe)
        self.logger.info(
            f"[{self.__class__.__name__}] Cleaned CoT saved to {output_file}"
        )
        return [output_key, output_stats_key]
