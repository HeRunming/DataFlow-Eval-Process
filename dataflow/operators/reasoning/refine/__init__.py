"""
dataflow/operators/reasoning/refine
=====================================
Operators for **offline post-processing** of long chain-of-thought (CoT) data.

All four operators in this package accept a dataframe column containing
DeepSeek-R1-style ``<think>…</think>`` (or plain CoT) text and produce a
shorter, cleaner version of that reasoning chain.

Operator summary
----------------
CoTLLMJudgeRefiner
    Method A – LLM-Judge step-level filtering.
    Classifies each reasoning step as necessary / redundant / compressible and
    applies the corresponding action.

CoTMonteCarloRefiner
    Method B – Monte Carlo step-importance scoring.
    Estimates the increase in answer-correctness probability when each step is
    included; drops steps below the importance threshold.

CoTChunkCompressRefiner
    Method C – Chunk-level classification and targeted rewriting (R1-Compress style).
    Splits the CoT at reasoning boundaries, classifies chunks as
    core / exploration / verification / transition, and applies
    type-specific LLM compression.

CoTMathNormRefiner
    Rule-based LaTeX / math formula normaliser (zero LLM calls).
    Applies a configurable set of regex transformations only inside math
    regions to standardise notation output by Method A or Method D.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cot_llm_judge_refiner import CoTLLMJudgeRefiner
    from .cot_llm_judge_refiner_fast import CoTLLMJudgeRefinerFast
    from .cot_monte_carlo_refiner import CoTMonteCarloRefiner
    from .cot_chunk_compress_refiner import CoTChunkCompressRefiner
    from .cot_chunk_compress_refiner_fast import CoTChunkCompressRefinerFast
    from .cot_pattern_refiner import CoTPatternRefiner
    from .cot_pattern_refiner_fast import CoTPatternRefinerFast
    from .cot_math_norm_refiner import CoTMathNormRefiner

else:
    import sys
    from dataflow.utils.registry import LazyLoader, generate_import_structure_from_type_checking

    cur_path = "dataflow/operators/reasoning/refine/"
    _import_structure = generate_import_structure_from_type_checking(__file__, cur_path)
    sys.modules[__name__] = LazyLoader(__name__, cur_path, _import_structure)
