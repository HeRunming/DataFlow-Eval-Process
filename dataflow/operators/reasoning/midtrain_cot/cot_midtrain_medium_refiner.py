"""
CoTMidtrainMediumRefiner
========================

Midtrain-oriented Long-CoT -> Medium-CoT distillation.

This operator is intentionally different from the legacy local compression
operators under ``reasoning.refine``:

* It treats a long CoT as a training trace, not just text to shorten.
* It first extracts a structured reasoning skeleton.
* It rewrites the skeleton into a medium-length trace under a target length
  budget.
* It verifies answer equivalence, reasoning sufficiency, and style risks before
  accepting the rewritten trace.

The intended use case is building midtrain data where the downstream model's
CoT should become clearer and less overlong without losing difficult reasoning
capability.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Optional, Union

import pandas as pd

from dataflow import get_logger
from dataflow.core import LLMServingABC, OperatorABC
from dataflow.core.prompt import DIYPromptABC, PromptABC, prompt_restrict
from dataflow.utils.registry import OPERATOR_REGISTRY, PROMPT_REGISTRY
from dataflow.utils.storage import DataFlowStorage


# --------------------------------------------------------------------------- #
# Utilities                                                                   #
# --------------------------------------------------------------------------- #


def _parse_cot_from_r1(text: str) -> tuple[str, str]:
    """Parse DeepSeek-R1-style ``<think>...</think>`` output.

    Returns ``(cot, answer_suffix)``.  If no tags are found, the whole string is
    treated as reasoning text and ``answer_suffix`` is empty.
    """
    m = re.search(r"<think>(.*?)</think>\s*<answer>(.*?)</answer>", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r"<think>(.*?)</think>(.*)", text, re.DOTALL)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return text.strip(), ""


def _extract_json_object(response: Optional[str]) -> dict[str, Any]:
    """Extract a JSON object from a model response.

    The parser is deliberately conservative: on any parse failure it returns an
    empty dict, which lets the caller fall back to the original CoT.
    """
    if not response:
        return {}
    text = response.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return {}
    try:
        obj = json.loads(m.group())
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _rough_token_count(text: str) -> int:
    """Cheap token proxy for routing and stats.

    It is not tokenizer-exact, but is stable and cheap enough for data pipeline
    control.  ASCII-heavy text is approximated by word/punctuation units; CJK
    text is approximated more character-wise.
    """
    if not text:
        return 0
    ascii_units = re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", text)
    cjk_chars = re.findall(r"[\u4e00-\u9fff]", text)
    # Avoid double-counting CJK too aggressively: roughly 1 CJK char ~= 1 token.
    return max(1, len(ascii_units) + max(0, len(cjk_chars) - len(ascii_units) // 4))


def _choose_target_budget(original_tokens: int, problem_type: str = "") -> tuple[int, int]:
    """Choose a medium-CoT target token range from original length and task type."""
    p = (problem_type or "").lower()
    if any(k in p for k in ("proof", "geometry", "olympiad", "number_theory")):
        floor = 640
    elif any(k in p for k in ("algebra", "calculus", "combinatorics")):
        floor = 448
    else:
        floor = 288

    if original_tokens < 800:
        lo = max(128, min(original_tokens, floor // 2))
        hi = max(lo + 64, int(original_tokens * 0.9))
    elif original_tokens < 2000:
        lo = max(floor, int(original_tokens * 0.45))
        hi = max(lo + 128, int(original_tokens * 0.70))
    elif original_tokens < 5000:
        lo = max(floor, int(original_tokens * 0.28))
        hi = max(lo + 192, int(original_tokens * 0.48))
    else:
        lo = max(floor, min(1200, int(original_tokens * 0.18)))
        hi = max(lo + 256, min(2200, int(original_tokens * 0.34)))

    return int(lo), int(hi)


def _wrap_like_original(raw_text: str, cleaned_cot: str, answer_suffix: str) -> str:
    if "<think>" in raw_text:
        if answer_suffix:
            return f"<think>\n{cleaned_cot}\n</think>\n<answer>\n{answer_suffix}\n</answer>"
        return f"<think>\n{cleaned_cot}\n</think>"
    return cleaned_cot


@dataclass
class _VerificationResult:
    accepted: bool
    reason: str
    answer_equivalent: bool = False
    sufficient: bool = False
    style_ok: bool = True
    risk: str = "unknown"


# --------------------------------------------------------------------------- #
# Prompt classes                                                              #
# --------------------------------------------------------------------------- #


@PROMPT_REGISTRY.register()
class CoTReasoningSkeletonPrompt(PromptABC):
    """Extract a structured reasoning skeleton from an overlong CoT."""

    def build_prompt(self, problem: str, long_cot: str, answer: str = "") -> str:
        answer_block = f"\nKnown final answer:\n{answer}\n" if answer.strip() else ""
        return (
            "You are preparing chain-of-thought data for LLM midtraining.\n"
            "Extract the reasoning skeleton from an overlong reasoning trace.\n"
            "Do NOT solve the problem from scratch; only summarize the original trace.\n\n"
            f"Problem:\n{problem}\n"
            f"{answer_block}"
            f"Original long reasoning trace:\n<long_cot>\n{long_cot}\n</long_cot>\n\n"
            "Return ONLY JSON with this schema:\n"
            "{\n"
            '  "problem_type": "algebra|geometry|number_theory|proof|calculation|other",\n'
            '  "final_answer": "final answer copied from the trace if available",\n'
            '  "key_steps": [\n'
            "    {\n"
            '      "id": "s1",\n'
            '      "role": "setup|derivation|case_split|computation|verification|useful_failed_attempt|pure_failed_attempt|conclusion",\n'
            '      "content": "short faithful description of the step",\n'
            '      "equations": ["important equations, substitutions, inequalities, numeric values"],\n'
            '      "depends_on": ["ids of prerequisite steps"],\n'
            '      "keep_reason": "why this step matters for learning the solution, or why it can be compressed"\n'
            "    }\n"
            "  ],\n"
            '  "discardable_noise": ["self-talk, repeated checks, generic planning, template filler"],\n'
            '  "compression_risks": ["facts or equations that must not be dropped"]\n'
            "}\n\n"
            "Guidelines:\n"
            "- Preserve nontrivial failed attempts only when they explain why a path was abandoned or rule out a case.\n"
            "- Mark purely verbose or repeated verification as discardable noise.\n"
            "- Keep every important variable definition, equation, case distinction, and final conclusion.\n"
        )


@PROMPT_REGISTRY.register()
class CoTMediumRewritePrompt(PromptABC):
    """Rewrite a skeleton into a medium-length training trace."""

    def build_prompt(
        self,
        problem: str,
        skeleton_json: str,
        target_min_tokens: int,
        target_max_tokens: int,
        language_hint: str = "same as original",
    ) -> str:
        return (
            "You are converting an overlong chain-of-thought into a medium-length training trace.\n"
            "The output will be used for LLM midtraining, so preserve reasoning ability signals, not just the answer.\n\n"
            f"Problem:\n{problem}\n\n"
            f"Reasoning skeleton JSON:\n{skeleton_json}\n\n"
            f"Target length: {target_min_tokens}-{target_max_tokens} approximate tokens.\n"
            f"Language: {language_hint}.\n\n"
            "Rewrite rules:\n"
            "1. Preserve all essential definitions, equations, case splits, intermediate results, and nontrivial transformations.\n"
            "2. Compress failed attempts only if they teach why a path was abandoned; delete generic wandering.\n"
            "3. Remove self-talk such as 'wait', 'let me think', 'maybe', and repeated checking unless it corrects an actual error.\n"
            "4. Do not introduce a new solution path that was not supported by the skeleton.\n"
            "5. Do not make the reasoning too terse: the final answer must be derivable from the trace.\n"
            "6. Avoid formulaic templates such as repeated 'Tried X, but...' or 'Verified: ...'. Use natural mathematical prose.\n"
            "7. Preserve the final answer exactly when it is present in the skeleton.\n\n"
            "Return ONLY JSON:\n"
            "{\n"
            '  "medium_cot": "the rewritten medium-length reasoning trace",\n'
            '  "final_answer": "final answer",\n'
            '  "dropped_content_summary": "what was removed and why",\n'
            '  "risk": "low|medium|high"\n'
            "}\n"
        )


@PROMPT_REGISTRY.register()
class CoTMediumVerifyPrompt(PromptABC):
    """Verify whether a medium CoT is safe to accept for midtrain data."""

    def build_prompt(
        self,
        problem: str,
        original_answer: str,
        skeleton_json: str,
        medium_cot: str,
        medium_answer: str = "",
    ) -> str:
        return (
            "You are a strict verifier for midtraining chain-of-thought data.\n"
            "Check whether the medium reasoning trace is faithful to the original reasoning skeleton and safe to train on.\n\n"
            f"Problem:\n{problem}\n\n"
            f"Original/final answer reference:\n{original_answer}\n\n"
            f"Original reasoning skeleton:\n{skeleton_json}\n\n"
            f"Candidate medium reasoning trace:\n<medium_cot>\n{medium_cot}\n</medium_cot>\n\n"
            f"Candidate final answer:\n{medium_answer}\n\n"
            "Evaluate these criteria:\n"
            "- answer_equivalent: the candidate final answer is mathematically/semantically equivalent to the reference.\n"
            "- sufficient: the candidate reasoning contains enough steps to derive the answer without a major missing bridge.\n"
            "- formula_safe: important equations, values, variables, and case distinctions from the skeleton are not corrupted.\n"
            "- style_ok: the trace is concise and natural, not repetitive or template-like.\n\n"
            "Return ONLY JSON:\n"
            "{\n"
            '  "answer_equivalent": true,\n'
            '  "sufficient": true,\n'
            '  "formula_safe": true,\n'
            '  "style_ok": true,\n'
            '  "risk": "low|medium|high",\n'
            '  "reason": "brief explanation of any risk"\n'
            "}\n"
        )


# --------------------------------------------------------------------------- #
# Operator                                                                    #
# --------------------------------------------------------------------------- #


@prompt_restrict(
    CoTReasoningSkeletonPrompt,
    CoTMediumRewritePrompt,
    CoTMediumVerifyPrompt,
)
@OPERATOR_REGISTRY.register()
class CoTMidtrainMediumRefiner(OperatorABC):
    """Distill long CoT into verifier-gated medium CoT for midtraining.

    Parameters
    ----------
    llm_serving:
        LLM backend used for extraction, rewrite, and verification.
    min_chars_to_clean:
        CoTs shorter than this threshold are passed through unchanged.
    accept_medium_risk:
        If true, accepts verifier ``risk == medium`` when hard checks pass.
        Default false: only low-risk candidates are accepted.
    fallback_mode:
        ``original`` returns the original CoT on verification failure;
        ``skeleton`` emits a conservative skeleton summary when rewrite fails.
    """

    def __init__(
        self,
        llm_serving: LLMServingABC = None,
        skeleton_prompt: Union[CoTReasoningSkeletonPrompt, DIYPromptABC] = None,
        rewrite_prompt: Union[CoTMediumRewritePrompt, DIYPromptABC] = None,
        verify_prompt: Union[CoTMediumVerifyPrompt, DIYPromptABC] = None,
        min_chars_to_clean: int = 2000,
        accept_medium_risk: bool = False,
        fallback_mode: str = "original",
        force_verify: bool = True,
    ):
        self.logger = get_logger()
        self.llm_serving = llm_serving
        self.skeleton_prompt = skeleton_prompt or CoTReasoningSkeletonPrompt()
        self.rewrite_prompt = rewrite_prompt or CoTMediumRewritePrompt()
        self.verify_prompt = verify_prompt or CoTMediumVerifyPrompt()
        self.min_chars_to_clean = min_chars_to_clean
        self.accept_medium_risk = accept_medium_risk
        self.fallback_mode = fallback_mode if fallback_mode in {"original", "skeleton"} else "original"
        self.force_verify = force_verify

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "Midtrain CoT：Long-CoT 到 Medium-CoT 的蒸馏算子。\n"
                "流程为：抽取 reasoning skeleton -> 按动态长度预算重写 medium CoT -> "
                "答案等价/推理充分性/公式安全/style 风险验证 -> 通过才接受，否则回退原始 CoT。\n"
                "适用于希望降低下游模型 CoT 长度、但不牺牲推理能力的 midtrain 数据构造。"
            )
        return (
            "Midtrain CoT: Long-CoT to Medium-CoT distillation. Extracts a reasoning skeleton, "
            "rewrites under a dynamic length budget, verifies answer/sufficiency/formula/style safety, "
            "and falls back on failure."
        )

    def _validate_dataframe(self, dataframe: pd.DataFrame):
        missing = [c for c in [self.input_key] if c not in dataframe.columns]
        if missing:
            raise ValueError(f"[{self.__class__.__name__}] Missing required column(s): {missing}")

    def _call_one(self, prompt: str) -> str:
        responses = self.llm_serving.generate_from_input(user_inputs=[prompt])
        if not responses:
            return ""
        return responses[0] or ""

    def _build_skeleton_fallback(self, skeleton: dict[str, Any]) -> str:
        steps = skeleton.get("key_steps") or []
        if not isinstance(steps, list):
            return ""
        lines: list[str] = []
        for step in steps:
            if not isinstance(step, dict):
                continue
            content = str(step.get("content", "")).strip()
            equations = step.get("equations") or []
            eq_text = ""
            if isinstance(equations, list) and equations:
                eq_text = " Key equations: " + "; ".join(str(e) for e in equations[:3])
            if content:
                lines.append(content + eq_text)
        final_answer = str(skeleton.get("final_answer", "")).strip()
        if final_answer:
            lines.append(f"Therefore, the final answer is {final_answer}.")
        return "\n\n".join(lines)

    def _verify_candidate(
        self,
        problem: str,
        original_answer: str,
        skeleton_json: str,
        medium_cot: str,
        medium_answer: str,
    ) -> _VerificationResult:
        if not self.force_verify:
            return _VerificationResult(True, "verification_disabled", True, True, True, "unknown")

        prompt = self.verify_prompt.build_prompt(
            problem=problem,
            original_answer=original_answer,
            skeleton_json=skeleton_json,
            medium_cot=medium_cot,
            medium_answer=medium_answer,
        )
        try:
            obj = _extract_json_object(self._call_one(prompt))
        except Exception as exc:
            return _VerificationResult(False, f"verify_exception: {exc}")

        ans = bool(obj.get("answer_equivalent", False))
        suff = bool(obj.get("sufficient", False))
        formula_safe = bool(obj.get("formula_safe", False))
        style_ok = bool(obj.get("style_ok", True))
        risk = str(obj.get("risk", "high")).lower()
        reason = str(obj.get("reason", ""))
        risk_ok = risk == "low" or (self.accept_medium_risk and risk == "medium")
        accepted = ans and suff and formula_safe and style_ok and risk_ok
        return _VerificationResult(
            accepted=accepted,
            reason=reason or ("accepted" if accepted else "verification_failed"),
            answer_equivalent=ans,
            sufficient=suff,
            style_ok=style_ok,
            risk=risk,
        )

    def _process_row(
        self,
        raw_text: str,
        problem: str,
        answer: str,
    ) -> tuple[str, dict[str, Any]]:
        cot, answer_suffix = _parse_cot_from_r1(raw_text)
        original_tokens = _rough_token_count(cot)

        if not cot.strip():
            return raw_text, {"skipped": True, "reason": "empty"}
        if self.min_chars_to_clean > 0 and len(cot) < self.min_chars_to_clean:
            return raw_text, {
                "skipped": True,
                "reason": "short",
                "original_chars": len(cot),
                "original_tokens_rough": original_tokens,
            }

        reference_answer = answer.strip() or answer_suffix.strip()

        # Stage 1: skeleton extraction.
        skeleton_prompt = self.skeleton_prompt.build_prompt(
            problem=problem,
            long_cot=cot,
            answer=reference_answer,
        )
        try:
            skeleton_obj = _extract_json_object(self._call_one(skeleton_prompt))
        except Exception as exc:
            return raw_text, {"accepted": False, "fallback": "original", "error": f"skeleton_exception: {exc}"}

        if not skeleton_obj or not skeleton_obj.get("key_steps"):
            return raw_text, {"accepted": False, "fallback": "original", "error": "skeleton_parse_failed"}

        skeleton_json = json.dumps(skeleton_obj, ensure_ascii=False)
        problem_type = str(skeleton_obj.get("problem_type", ""))
        target_min, target_max = _choose_target_budget(original_tokens, problem_type)

        # Stage 2: medium rewrite.
        rewrite_prompt = self.rewrite_prompt.build_prompt(
            problem=problem,
            skeleton_json=skeleton_json,
            target_min_tokens=target_min,
            target_max_tokens=target_max,
        )
        try:
            rewrite_obj = _extract_json_object(self._call_one(rewrite_prompt))
        except Exception as exc:
            rewrite_obj = {}
            self.logger.debug(f"[{self.__class__.__name__}] rewrite failed: {exc}")

        medium_cot = str(rewrite_obj.get("medium_cot", "")).strip()
        medium_answer = str(rewrite_obj.get("final_answer", "")).strip()
        rewrite_risk = str(rewrite_obj.get("risk", "unknown"))

        if not medium_cot:
            if self.fallback_mode == "skeleton":
                medium_cot = self._build_skeleton_fallback(skeleton_obj)
                medium_answer = str(skeleton_obj.get("final_answer", "")).strip()
            else:
                return raw_text, {"accepted": False, "fallback": "original", "error": "rewrite_parse_failed"}

        # Stage 3: verifier-gated acceptance.
        verify = self._verify_candidate(
            problem=problem,
            original_answer=reference_answer or str(skeleton_obj.get("final_answer", "")),
            skeleton_json=skeleton_json,
            medium_cot=medium_cot,
            medium_answer=medium_answer,
        )

        output_tokens = _rough_token_count(medium_cot)
        stats: dict[str, Any] = {
            "accepted": verify.accepted,
            "fallback": None if verify.accepted else "original",
            "original_chars": len(cot),
            "output_chars": len(medium_cot) if verify.accepted else len(cot),
            "original_tokens_rough": original_tokens,
            "output_tokens_rough": output_tokens if verify.accepted else original_tokens,
            "target_min_tokens": target_min,
            "target_max_tokens": target_max,
            "problem_type": problem_type,
            "rewrite_risk": rewrite_risk,
            "verify_risk": verify.risk,
            "answer_equivalent": verify.answer_equivalent,
            "sufficient": verify.sufficient,
            "style_ok": verify.style_ok,
            "verify_reason": verify.reason,
        }

        if verify.accepted:
            return _wrap_like_original(raw_text, medium_cot, answer_suffix), stats
        return raw_text, stats

    def run(
        self,
        storage: DataFlowStorage,
        input_key: str = "cot",
        output_key: str = "cot_midtrain_medium",
        output_stats_key: str = "cot_midtrain_medium_stats",
        problem_key: Optional[str] = None,
        answer_key: Optional[str] = None,
    ) -> list[str]:
        self.input_key = input_key
        dataframe = storage.read("dataframe")
        self._validate_dataframe(dataframe)

        outputs: list[str] = []
        stats_list: list[str] = []

        for row_idx, row in dataframe.iterrows():
            raw_text = str(row[input_key]) if pd.notna(row[input_key]) else ""
            problem = (
                str(row[problem_key])
                if problem_key and problem_key in dataframe.columns and pd.notna(row[problem_key])
                else ""
            )
            answer = (
                str(row[answer_key])
                if answer_key and answer_key in dataframe.columns and pd.notna(row[answer_key])
                else ""
            )
            try:
                out, stats = self._process_row(raw_text, problem, answer)
            except Exception as exc:
                self.logger.debug(f"[{self.__class__.__name__}] row {row_idx} failed: {exc}")
                out, stats = raw_text, {"accepted": False, "fallback": "original", "error": str(exc)}
            outputs.append(out)
            stats_list.append(json.dumps(stats, ensure_ascii=False))

        dataframe[output_key] = outputs
        dataframe[output_stats_key] = stats_list
        output_file = storage.write(dataframe)
        self.logger.info(f"[{self.__class__.__name__}] saved to {output_file}")
        return [output_key, output_stats_key]
