"""
Prompt templates for Long-CoT post-processing (cleaning/compression) operators.

These prompts support four methods for offline compression of DeepSeek-R1-style
<think>...</think> reasoning chains:

  A. LLM-Judge step-level filtering   (CoTStepJudgePrompt)
  B. Monte Carlo completion helper     (CoTMCCompletionPrompt)
  C. Chunk-level rewriting             (CoTChunkRefinePrompt)
  D. Thinking-pattern classification   (CoTPatternClassifyPrompt / CoTPatternRefinePrompt)

All prompts follow the DataFlow PromptABC interface and are registered via
PROMPT_REGISTRY so they can be discovered by the LazyLoader mechanism.
"""

from dataflow.utils.registry import PROMPT_REGISTRY
from dataflow.core.prompt import PromptABC


# ─────────────────────────────────────────────────────────────────────────────
# A. Step-level LLM-Judge  (used by CoTLLMJudgeRefiner)
# ─────────────────────────────────────────────────────────────────────────────

@PROMPT_REGISTRY.register()
class CoTStepJudgePrompt(PromptABC):
    """
    Prompt for judging whether a single reasoning step is necessary, redundant,
    or compressible, given the problem and a summary of prior steps.

    build_prompt returns:
        A prompt string requesting a JSON response with keys
        ``{"label": "necessary|redundant|compressible", "reason": "<str>"}``
    """

    def __init__(self):
        pass

    def build_prompt(self, problem: str, step: str, context: str = "") -> str:
        """
        Args:
            problem: The original question/problem statement.
            step:    The current reasoning step to evaluate.
            context: A brief summary of reasoning steps that came before this one.
                     May be empty for the first step.

        Returns:
            A prompt string for the LLM judge.
        """
        context_block = (
            f"\nSummary of reasoning so far:\n{context}\n"
            if context.strip()
            else "\n(This is the first reasoning step.)\n"
        )

        return (
            "You are reviewing a single step extracted from a chain-of-thought "
            "reasoning trace.\n"
            f"\nProblem:\n{problem}"
            f"{context_block}"
            f"\nCurrent step to evaluate:\n<step>\n{step}\n</step>\n\n"
            "Classify this step as exactly one of:\n"
            '- "necessary": Introduces new information, a new sub-result, or a '
            "key logical transition required to reach the final answer.\n"
            '- "redundant": Repeats already-established information, re-verifies '
            "something already verified, or is a dead-end whose conclusion is "
            "never used.\n"
            '- "compressible": Contains useful information but is over-explained; '
            "can be summarised in one sentence without information loss.\n\n"
            "Return ONLY a JSON object with no extra text:\n"
            '{"label": "necessary|redundant|compressible", "reason": "<one sentence>"}'
        )


@PROMPT_REGISTRY.register()
class CoTStepCompressPrompt(PromptABC):
    """
    Prompt for compressing a single 'compressible' reasoning step into one
    concise sentence while preserving all intermediate results and key logic.

    build_prompt returns a prompt string; the LLM response is the compressed text.
    """

    def __init__(self):
        pass

    def build_prompt(self, step: str) -> str:
        """
        Args:
            step: The reasoning step text to compress.

        Returns:
            A prompt string asking for a one-sentence compression.
        """
        return (
            "Compress the following reasoning step into ONE concise sentence.\n"
            "Rules:\n"
            "  • Preserve ALL intermediate results, numerical values, and "
            "logical conclusions.\n"
            "  • Remove redundant re-statements, excessive hedging language, "
            "and repeated explanations.\n"
            "  • Do NOT add any new information.\n"
            "  • Output only the compressed sentence, no preamble.\n\n"
            f"Original step:\n{step}\n\n"
            "Compressed (one sentence):"
        )


# ─────────────────────────────────────────────────────────────────────────────
# B. Monte Carlo completion helper  (used by CoTMonteCarloRefiner)
# ─────────────────────────────────────────────────────────────────────────────

@PROMPT_REGISTRY.register()
class CoTMCCompletionPrompt(PromptABC):
    """
    Prompt used to generate Monte Carlo completions from a partial CoT prefix.

    Given the problem and a prefix of reasoning steps, asks the LLM to finish
    the reasoning and state the final answer.  The proportion of completions
    that yield the correct answer is used as P(correct | prefix).

    build_prompt returns a prompt string; the LLM response is a full completion
    ending with a clearly marked final answer.
    """

    def __init__(self):
        pass

    def build_prompt(self, problem: str, cot_prefix: str) -> str:
        """
        Args:
            problem:    The original question/problem statement.
            cot_prefix: The reasoning steps seen so far (may be empty for
                        the "no-context" baseline).

        Returns:
            A prompt string that asks the LLM to complete the reasoning.
        """
        prefix_block = (
            f"\nReasoning so far:\n{cot_prefix}\n"
            if cot_prefix.strip()
            else "\n(No prior reasoning provided — start fresh.)\n"
        )

        return (
            "You are given a problem and partial reasoning. "
            "Continue the reasoning chain and arrive at the final answer.\n"
            f"\nProblem:\n{problem}"
            f"{prefix_block}"
            "\nContinue the reasoning from where it left off and clearly state "
            "your final answer at the end using the format:\n"
            "Final Answer: <your answer>"
        )


# ─────────────────────────────────────────────────────────────────────────────
# C. Chunk-level rewriting  (used by CoTChunkCompressRefiner)
# ─────────────────────────────────────────────────────────────────────────────

@PROMPT_REGISTRY.register()
class CoTChunkClassifyPrompt(PromptABC):
    """
    Prompt for classifying a reasoning chunk into one of four types:
    core / exploration / verification / transition.

    These types drive the per-chunk compression aggressiveness in Method C.
    """

    def __init__(self):
        pass

    def build_prompt(self, problem: str, chunk: str, context: str = "") -> str:
        """
        Args:
            problem: The original question/problem statement.
            chunk:   The chunk of reasoning text to classify.
            context: Brief summary of reasoning chunks already processed.

        Returns:
            A prompt string requesting a JSON classification.
        """
        context_block = (
            f"\nPrior reasoning summary:\n{context}\n"
            if context.strip()
            else ""
        )

        return (
            "You are classifying a chunk of reasoning from a chain-of-thought trace.\n"
            f"\nProblem:\n{problem}"
            f"{context_block}"
            f"\nChunk to classify:\n<chunk>\n{chunk}\n</chunk>\n\n"
            "Classify it as exactly one of:\n"
            '- "core":         Derives a new intermediate result or makes a key '
            "logical step toward the final answer.\n"
            '- "exploration":  Tries an approach; may or may not succeed. '
            "Includes dead-end paths.\n"
            '- "verification": Checks or re-derives a prior result.\n'
            '- "transition":   Connective language, restatements, or planning '
            "with no new mathematical content.\n\n"
            "Return ONLY a JSON object:\n"
            '{"type": "core|exploration|verification|transition", '
            '"key_info": "<key result or conclusion, or empty string>"}'
        )


@PROMPT_REGISTRY.register()
class CoTChunkRefinePrompt(PromptABC):
    """
    Prompt for rewriting a single reasoning chunk at a target compression ratio.

    Used by CoTChunkCompressRefiner after each chunk has been classified.
    The target_ratio is a human-readable percentage string, e.g. "50%".
    """

    def __init__(self):
        pass

    def build_prompt(
        self,
        problem: str,
        chunk: str,
        chunk_type: str,
        context: str = "",
        target_ratio: str = "60%",
    ) -> str:
        """
        Args:
            problem:      The original question/problem statement.
            chunk:        The chunk text to compress/rewrite.
            chunk_type:   One of core / exploration / verification / transition.
            context:      Brief summary of preceding compressed chunks (for
                          continuity).
            target_ratio: Target retention ratio as a percentage string,
                          e.g. "70%" means keep ~70% of original tokens.

        Returns:
            A prompt string whose LLM response is the compressed chunk text.
        """
        context_block = (
            f"\nPreceding reasoning (summary):\n{context}\n"
            if context.strip()
            else ""
        )

        type_guidance = {
            "core": (
                "This is a CORE reasoning chunk. Preserve all intermediate "
                "results and logical steps. Only remove over-elaboration."
            ),
            "exploration": (
                "This is an EXPLORATION chunk. If the explored path was "
                "abandoned, keep only the final conclusion ('tried X, it did "
                "not work'). If successful, keep the key steps."
            ),
            "verification": (
                "This is a VERIFICATION chunk. If the verification confirms "
                "an already-established result, compress to a single sentence. "
                "If it discovers an error, preserve the error-detection logic."
            ),
            "transition": (
                "This is a TRANSITION chunk. Replace it with a single brief "
                "connecting phrase or delete it entirely if unnecessary."
            ),
        }.get(chunk_type, "Compress while preserving key information.")

        return (
            "Compress the following reasoning chunk.\n"
            f"\nProblem:\n{problem}"
            f"{context_block}"
            f"\nChunk to compress (type: {chunk_type}):\n<chunk>\n{chunk}\n</chunk>\n\n"
            f"Guidance: {type_guidance}\n\n"
            f"Target: retain approximately {target_ratio} of the original tokens.\n"
            "Rules:\n"
            "  • Preserve ALL numerical values and intermediate conclusions.\n"
            "  • The output must flow naturally from the preceding reasoning.\n"
            "  • Do NOT introduce new information.\n"
            "  • Output only the compressed text, no preamble or explanation.\n\n"
            "Compressed chunk:"
        )


# ─────────────────────────────────────────────────────────────────────────────
# D. Thinking-pattern classification  (used by CoTPatternRefiner)
# ─────────────────────────────────────────────────────────────────────────────

@PROMPT_REGISTRY.register()
class CoTPatternClassifyPrompt(PromptABC):
    """
    Prompt for fine-grained classification of a reasoning fragment into one of
    nine Thinking Pattern types (from the Think Wisely framework).

    Returned JSON includes the type, a brief description of the key information
    carried, and the recommended action (keep / compress / delete).
    """

    # Human-readable descriptions for each pattern type, used in the prompt.
    PATTERN_DESCRIPTIONS = {
        "CORE_REASONING":          "Derives a new intermediate result or makes a key logical step.",
        "NECESSARY_EXPLORATION":   "Explores a path that is abandoned but the failure is informative (rules out a case).",
        "UNNECESSARY_EXPLORATION": "Explores a dead-end path whose conclusion is never used.",
        "NECESSARY_VERIFICATION":  "Discovers an error in a previous step or confirms a non-obvious result.",
        "REDUNDANT_VERIFICATION":  "Re-verifies something already established without new insight.",
        "PREAMBLE":                "Restates the problem or plans without introducing new content.",
        "TRANSITION":              "Connecting language, hedging, or filler with minimal semantic content.",
        "COMPUTATION":             "Executes arithmetic or algebraic calculation steps.",
        "CONCLUSION":              "States a final or intermediate answer.",
    }

    def __init__(self):
        pass

    def build_prompt(self, problem: str, fragment: str, context: str = "") -> str:
        """
        Args:
            problem:  The original question/problem statement.
            fragment: The reasoning fragment to classify.
            context:  Brief summary of reasoning that precedes this fragment.

        Returns:
            A prompt string requesting a detailed JSON classification.
        """
        context_block = (
            f"\nReasoning so far (summary):\n{context}\n"
            if context.strip()
            else "\n(No prior reasoning.)\n"
        )

        type_lines = "\n".join(
            f'- "{k}": {v}' for k, v in self.PATTERN_DESCRIPTIONS.items()
        )

        return (
            "Analyze the following reasoning fragment and classify it into "
            "one of the Thinking Pattern types.\n"
            f"\nProblem:\n{problem}"
            f"{context_block}"
            f"\nFragment to classify:\n<fragment>\n{fragment}\n</fragment>\n\n"
            "Pattern types:\n"
            f"{type_lines}\n\n"
            "Recommended action mapping:\n"
            "  CORE_REASONING, NECESSARY_VERIFICATION, COMPUTATION, CONCLUSION "
            "→ keep\n"
            "  NECESSARY_EXPLORATION, REDUNDANT_VERIFICATION, PREAMBLE, "
            "TRANSITION → compress\n"
            "  UNNECESSARY_EXPLORATION → delete\n\n"
            "Return ONLY a JSON object (no extra text):\n"
            "{\n"
            '  "type": "<one of the types above>",\n'
            '  "key_information": "<key result or conclusion this fragment '
            'contributes, or empty string>",\n'
            '  "recommendation": "keep|compress|delete"\n'
            "}"
        )


@PROMPT_REGISTRY.register()
class CoTPatternRefinePrompt(PromptABC):
    """
    Prompt for rewriting / compressing a reasoning fragment that has been
    classified as 'compress' by CoTPatternClassifyPrompt.

    For 'delete' fragments this prompt is not called; for 'keep' fragments the
    original text is used unchanged.
    """

    def __init__(self):
        pass

    def build_prompt(
        self,
        problem: str,
        fragment: str,
        pattern_type: str,
        key_information: str,
        context: str = "",
    ) -> str:
        """
        Args:
            problem:         The original question/problem statement.
            fragment:        The reasoning fragment to compress.
            pattern_type:    The classified Thinking Pattern type.
            key_information: The key result/conclusion this fragment carries
                             (from the classify step).
            context:         Brief summary of preceding compressed fragments.

        Returns:
            A prompt string whose LLM response is the compressed fragment text.
        """
        context_block = (
            f"\nPreceding reasoning (summary):\n{context}\n"
            if context.strip()
            else ""
        )

        type_guidance = {
            "NECESSARY_EXPLORATION": (
                "This fragment explores a path that was ultimately abandoned. "
                "Compress it to a single sentence of the form: "
                "'Tried [approach], but it led to [obstacle/contradiction], "
                "so this path was abandoned.'"
            ),
            "REDUNDANT_VERIFICATION": (
                "This fragment re-verifies an already-established result. "
                "Replace it with at most one short sentence confirming the "
                "result, e.g. 'Verified: [result].'"
            ),
            "PREAMBLE": (
                "This fragment restates the problem or plans without new "
                "content. Reduce it to one sentence or remove it entirely."
            ),
            "TRANSITION": (
                "This is connective/filler language. Compress to a single "
                "brief transition phrase, or remove entirely."
            ),
        }.get(
            pattern_type,
            "Compress while preserving all key information.",
        )

        key_info_block = (
            f"\nKey information to preserve: {key_information}\n"
            if key_information.strip()
            else ""
        )

        return (
            "Compress the following reasoning fragment.\n"
            f"\nProblem:\n{problem}"
            f"{context_block}"
            f"\nFragment (pattern type: {pattern_type}):\n"
            f"<fragment>\n{fragment}\n</fragment>\n"
            f"{key_info_block}"
            f"\nGuidance: {type_guidance}\n\n"
            "Rules:\n"
            "  • Preserve all numerical values and key conclusions listed above.\n"
            "  • Do NOT introduce new information.\n"
            "  • Output only the compressed text, no preamble.\n\n"
            "Compressed fragment:"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Combined judge + compress prompts (used by the Fast variants A / D).
#
# Rationale: the two-phase (classify then compress) design issues two LLM
# round-trips per compressible step/fragment.  Because the compress rewrite
# is short and stateless, we can ask the judge to emit it in the SAME
# response, cutting call count by up to 50% on A and D.  The judge is still
# asked to output strict JSON so downstream parsing is unambiguous.
# ─────────────────────────────────────────────────────────────────────────────


@PROMPT_REGISTRY.register()
class CoTStepJudgeCompressPrompt(PromptABC):
    """
    Combined step-level prompt used by ``CoTLLMJudgeRefinerFast``.

    Asks the judge to both label the step (necessary / redundant /
    compressible) AND, if compressible, emit the one-sentence compression
    in the same JSON response.

    Response schema:
        {
          "label":      "necessary|redundant|compressible",
          "reason":     "<one sentence>",
          "compressed": "<one sentence compression, only if label==compressible>"
        }

    The ``compressed`` field is ignored for non-compressible labels.
    """

    def __init__(self):
        pass

    def build_prompt(self, problem: str, step: str) -> str:
        return (
            "You are reviewing a single step extracted from a chain-of-thought "
            "reasoning trace.  Classify the step AND, if it is compressible, "
            "emit a one-sentence compression in the SAME JSON response.\n"
            f"\nProblem:\n{problem}\n"
            f"\nStep to evaluate:\n<step>\n{step}\n</step>\n\n"
            "Labels:\n"
            '  "necessary"    - Introduces a new sub-result, numerical value, '
            'or a key logical transition required to reach the final answer.\n'
            '  "redundant"    - Repeats already-established information, '
            're-verifies something already verified, or is a dead-end whose '
            'conclusion is never used.\n'
            '  "compressible" - Contains useful information but is '
            'over-explained; it can be summarised in one sentence without '
            'information loss.\n\n'
            "Rules for the \"compressed\" field (only when label == \"compressible\"):\n"
            "  * One sentence, same language as the original step.\n"
            "  * Preserve ALL numerical values and intermediate conclusions.\n"
            "  * Do NOT introduce new information.\n\n"
            "Return ONLY a JSON object, no extra text:\n"
            "{\n"
            '  "label":      "necessary|redundant|compressible",\n'
            '  "reason":     "<one sentence>",\n'
            '  "compressed": "<one sentence or empty string>"\n'
            "}"
        )


@PROMPT_REGISTRY.register()
class CoTPatternClassifyCompressPrompt(PromptABC):
    """
    Combined nine-type pattern prompt used by ``CoTPatternRefinerFast``.

    The same JSON also carries a compressed rewrite when the action is
    "compress", removing the need for a second LLM call on those fragments.

    Response schema:
        {
          "type":            "<one of the nine pattern types>",
          "key_information": "<brief>",
          "recommendation":  "keep|compress|delete",
          "compressed":      "<compressed text, only if recommendation=='compress'>"
        }

    For ``keep`` the original fragment is used; for ``delete`` the fragment
    is dropped.  Only the ``compress`` case consumes the ``compressed``
    field.
    """

    PATTERN_DESCRIPTIONS = {
        "CORE_REASONING":          "Derives a new intermediate result or makes a key logical step.",
        "NECESSARY_EXPLORATION":   "Explores a path that is abandoned but the failure is informative (rules out a case).",
        "UNNECESSARY_EXPLORATION": "Explores a dead-end path whose conclusion is never used.",
        "NECESSARY_VERIFICATION":  "Discovers an error in a previous step or confirms a non-obvious result.",
        "REDUNDANT_VERIFICATION":  "Re-verifies something already established without new insight.",
        "PREAMBLE":                "Restates the problem or plans without introducing new content.",
        "TRANSITION":              "Connecting language, hedging, or filler with minimal semantic content.",
        "COMPUTATION":             "Executes arithmetic or algebraic calculation steps.",
        "CONCLUSION":              "States a final or intermediate answer.",
    }

    def __init__(self):
        pass

    def build_prompt(self, problem: str, fragment: str) -> str:
        type_lines = "\n".join(
            f'- "{k}": {v}' for k, v in self.PATTERN_DESCRIPTIONS.items()
        )
        return (
            "Analyze the reasoning fragment and, in the SAME JSON response, "
            "classify it AND produce a faithful summary of what the fragment "
            "contributes.  Downstream code decides whether to keep, compress, "
            "or drop the fragment, so ALWAYS fill the \"compressed\" field "
            "with a summary that could replace the original if needed.\n"
            f"\nProblem:\n{problem}\n"
            f"\nFragment:\n<fragment>\n{fragment}\n</fragment>\n\n"
            "Pattern types:\n"
            f"{type_lines}\n\n"
            "Rules for the \"compressed\" field:\n"
            "  * CORE_REASONING / COMPUTATION / CONCLUSION / "
            "NECESSARY_VERIFICATION -> one faithful sentence that "
            "preserves EVERY numerical value, intermediate result, and "
            "logical operator.\n"
            "  * NECESSARY_EXPLORATION and UNNECESSARY_EXPLORATION -> "
            "2-3 sentences that keep a real trace of the attempt.  You "
            "MUST include:\n"
            "    (i) the concrete action taken (name the approach AND "
            "write at least one equation, identity, substitution, or "
            "theorem it used);\n"
            "    (ii) how far the attempt progressed before it stalled "
            "(e.g. \"reached x⁴ + 16x² = (8+2b)², which has no rational "
            "factorisation\"; \"got (p² - q²)² = 256 + (16+4b)², no "
            "closed form for p, q\");\n"
            "    (iii) the SPECIFIC reason it was abandoned, not a "
            "generic \"too complex / inconclusive\".\n"
            "    DO NOT use the template \"Considered X, but abandoned "
            "Y\" -- write in the same natural tone as the original "
            "fragment, varying wording across fragments.  Short "
            "declarative sentences are fine; templated phrasing is not.\n"
            "  * REDUNDANT_VERIFICATION -> one short sentence, e.g. "
            "\"Re-verified: [result].\"\n"
            "  * PREAMBLE -> one short sentence stating the setup, e.g. "
            "\"Setting up part (ii).\"\n"
            "  * TRANSITION -> a brief connecting phrase (\"Next,\", "
            "\"Therefore,\", etc.).\n"
            "  * Preserve every numerical value and key conclusion the "
            "fragment carries.\n\n"
            "The \"recommendation\" field is advisory only; put whichever "
            "of keep|compress|delete best matches the fragment.\n\n"
            "Return ONLY JSON, no extra text:\n"
            "{\n"
            '  "type":            "<PATTERN_TYPE>",\n'
            '  "key_information": "<brief key result or conclusion>",\n'
            '  "recommendation":  "keep|compress|delete",\n'
            '  "compressed":      "<non-empty compressed text>"\n'
            "}"
        )

