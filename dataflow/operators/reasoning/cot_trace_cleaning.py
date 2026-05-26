"""Structure-preserving CoT trace cleaning operators.

This module implements a low-cost Chain-of-Thought cleaning pipeline:

1. Extract a typed step trace from raw CoT with a small LLM.
2. Build a lightweight trajectory graph over the extracted steps.
3. Decide deletions with rules + graph dependency + answer-impact + verifier signals.
4. Reconstruct cleaned CoT and insert minimal local bridges only when needed.
5. Verify the cleaned trace and optionally roll back to the original CoT.

The design intentionally avoids large-span rewriting. Most cleaned tokens are copied
from the original CoT; LLM calls are reserved for structured extraction, uncertain
impact checks, local bridges, and final consistency verification.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from dataflow import get_logger


TRACE_EXTRACTION_SYSTEM_PROMPT = """You are a conservative Chain-of-Thought trace parser.
Your job is to split the given reasoning into semantic steps/chunks and describe each step.
Do not solve the problem again. Do not rewrite the reasoning. Only extract structure.
Return strict JSON that matches the schema.
"""

TRACE_EXTRACTION_USER_TEMPLATE = """Question:\n{question}\n\nFinal answer:\n{answer}\n\nRaw chain-of-thought:\n{cot}\n\nExtract semantic reasoning steps. Each step should preserve an original text span.
Use short, factual claims. Use depends_on to reference previous step ids when clear.
Allowed types: setup, theorem_recall, derivation, computation, assumption, verification,
reflection, transition, dead_end, correction, conclusion, other.
Allowed redundancy_hint: none, pure_transition, repeated_reflection, repeated_check,
failed_attempt, duplicated_derivation, verbose_explanation, meta_commentary.
"""

ANSWER_IMPACT_SYSTEM_PROMPT = """You are a conservative CoT pruning verifier.
Given a candidate reasoning step, decide whether removing it would likely break the
ability of the remaining reasoning to support the final answer. Prefer KEEP when unsure.
Return strict JSON only.
"""

ANSWER_IMPACT_USER_TEMPLATE = """Question:\n{question}\n\nFinal answer:\n{answer}\n\nStep candidate:\n{step_json}\n\nPrevious kept claims:\n{previous_claims}\n\nLater dependent claims:\n{later_claims}\n\nShould this step be deleted from the training CoT? Consider answer impact.
"""

BRIDGE_SYSTEM_PROMPT = """You are a minimal bridge writer for CoT cleaning.
Generate at most one short sentence to connect two adjacent kept reasoning chunks.
Do not introduce new formulas, variables, facts, or conclusions. If no bridge is needed,
return an empty string. Return strict JSON only.
"""

BRIDGE_USER_TEMPLATE = """Question:\n{question}\n\nFinal answer:\n{answer}\n\nPrevious kept chunk:\n{left}\n\nNext kept chunk:\n{right}\n\nDeleted material summary:\n{deleted_summary}\n\nWrite a minimal bridge if needed.
"""

FINAL_VERIFY_SYSTEM_PROMPT = """You are a strict CoT cleaning verifier.
Check whether the cleaned reasoning still supports the same final answer and whether it
introduces any new unsupported claim. Prefer FAIL when unsure. Return strict JSON only.
"""

FINAL_VERIFY_USER_TEMPLATE = """Question:\n{question}\n\nFinal answer:\n{answer}\n\nOriginal CoT:\n{raw_cot}\n\nCleaned CoT:\n{cleaned_cot}\n\nEvaluate whether the cleaned CoT is safe for SFT training.
"""


TRACE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "step_id": {"type": "integer"},
                    "span": {"type": "string"},
                    "type": {"type": "string"},
                    "claim": {"type": "string"},
                    "depends_on": {"type": "array", "items": {"type": "integer"}},
                    "answer_critical": {"type": "string"},
                    "redundancy_hint": {"type": "string"},
                },
                "required": [
                    "step_id",
                    "span",
                    "type",
                    "claim",
                    "depends_on",
                    "answer_critical",
                    "redundancy_hint",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["steps"],
    "additionalProperties": False,
}

BINARY_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["decision", "confidence", "reason"],
    "additionalProperties": False,
}

BRIDGE_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "need_bridge": {"type": "boolean"},
        "bridge": {"type": "string"},
        "new_information_added": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["need_bridge", "bridge", "new_information_added", "reason"],
    "additionalProperties": False,
}

FINAL_VERIFY_JSON_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string"},
        "same_final_answer": {"type": "boolean"},
        "supports_answer": {"type": "boolean"},
        "has_new_unsupported_claim": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": [
        "decision",
        "same_final_answer",
        "supports_answer",
        "has_new_unsupported_claim",
        "reason",
    ],
    "additionalProperties": False,
}


@dataclass
class PruningConfig:
    """Weights and thresholds for conservative pruning."""

    delete_threshold: float = 0.72
    uncertain_low: float = 0.45
    uncertain_high: float = 0.72
    max_delete_ratio: float = 0.40
    protect_answer_critical: bool = True
    protect_first_symbol_intro: bool = True
    use_llm_answer_impact: bool = True
    verifier_keep_on_uncertain: bool = True


PROTECTED_TYPES = {
    "setup",
    "theorem_recall",
    "derivation",
    "computation",
    "assumption",
    "correction",
    "conclusion",
}

HIGH_REDUNDANCY_HINTS = {
    "pure_transition",
    "repeated_reflection",
    "repeated_check",
    "failed_attempt",
    "meta_commentary",
}

MEDIUM_REDUNDANCY_HINTS = {
    "duplicated_derivation",
    "verbose_explanation",
}


class JSONParsingMixin:
    logger = get_logger()

    @staticmethod
    def _safe_json_loads(value: Any, default: Any) -> Any:
        if isinstance(value, (dict, list)):
            return value
        if value is None:
            return default
        text = str(value).strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except Exception:
            pass
        match = re.search(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                pass
        match = re.search(r"(\{.*\}|\[.*\])", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(1))
            except Exception:
                return default
        return default

    @staticmethod
    def _to_json_string(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False)


def _normalize_text(text: Any) -> str:
    if text is None:
        return ""
    if isinstance(text, float) and pd.isna(text):
        return ""
    return str(text)


def _find_span_offsets(cot: str, steps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Best-effort alignment of extracted spans back to raw CoT offsets."""
    cursor = 0
    for step in steps:
        span = _normalize_text(step.get("span"))
        start = cot.find(span, cursor) if span else -1
        if start < 0 and span:
            start = cot.find(span)
        if start < 0:
            start = cursor
            end = cursor
        else:
            end = start + len(span)
            cursor = end
        step["char_start"] = start
        step["char_end"] = end
    return steps


def _fallback_segment(cot: str) -> List[Dict[str, Any]]:
    """Rule-based segmentation fallback when the parser LLM fails."""
    parts = [p.strip() for p in re.split(r"\n{2,}|(?<=\.)\s+(?=(?:Thus|Therefore|Now|Next|Let|We|So)\b)", cot) if p.strip()]
    steps = []
    for i, part in enumerate(parts):
        lower = part.lower()
        if any(x in lower for x in ["check", "verify", "let me", "again"]):
            typ = "verification"
            hint = "repeated_check" if i > 0 else "none"
        elif any(x in lower for x in ["therefore", "thus", "so"]):
            typ = "derivation"
            hint = "none"
        else:
            typ = "other"
            hint = "none"
        steps.append(
            {
                "step_id": i + 1,
                "span": part,
                "type": typ,
                "claim": part[:240],
                "depends_on": [i] if i > 0 else [],
                "answer_critical": "uncertain",
                "redundancy_hint": hint,
            }
        )
    return _find_span_offsets(cot, steps)


def _claim_similarity(a: str, b: str) -> float:
    """Small lexical similarity fallback; cheap enough for rule-level duplicate checks."""
    tok_a = set(re.findall(r"[A-Za-z0-9_\\]+|[\u4e00-\u9fff]", a.lower()))
    tok_b = set(re.findall(r"[A-Za-z0-9_\\]+|[\u4e00-\u9fff]", b.lower()))
    if not tok_a or not tok_b:
        return 0.0
    return len(tok_a & tok_b) / max(1, len(tok_a | tok_b))


def _contains_formula_or_symbol_intro(text: str) -> bool:
    formula_markers = ["=", "\\", "^", "_", "∑", "∫", "≤", "≥", "⇒", "iff", "theorem", "lemma", "定理", "引理"]
    return any(marker in text for marker in formula_markers)


class CoTTraceExtractor(JSONParsingMixin):
    """Extract typed CoT steps/chunks with a small model."""

    def __init__(self, llm_serving=None, system_prompt: str = TRACE_EXTRACTION_SYSTEM_PROMPT):
        self.llm_serving = llm_serving
        self.system_prompt = system_prompt
        self.logger = get_logger()

    def _build_prompt(self, question: str, answer: str, cot: str) -> str:
        return TRACE_EXTRACTION_USER_TEMPLATE.format(question=question, answer=answer, cot=cot)

    def run(
        self,
        storage,
        cot_key: str = "cot",
        question_key: str = "question",
        answer_key: str = "answer",
        output_key: str = "cot_trace",
    ) -> str:
        dataframe = storage.read("dataframe")
        prompts = [
            self._build_prompt(
                _normalize_text(row.get(question_key, "")),
                _normalize_text(row.get(answer_key, "")),
                _normalize_text(row.get(cot_key, "")),
            )
            for _, row in dataframe.iterrows()
        ]
        if self.llm_serving is None:
            raw_outputs = [None] * len(prompts)
        else:
            raw_outputs = self.llm_serving.generate_from_input(
                user_inputs=prompts,
                system_prompt=self.system_prompt,
                json_schema=TRACE_JSON_SCHEMA,
            )

        traces = []
        for raw, (_, row) in zip(raw_outputs, dataframe.iterrows()):
            cot = _normalize_text(row.get(cot_key, ""))
            parsed = self._safe_json_loads(raw, default={"steps": []})
            steps = parsed.get("steps", []) if isinstance(parsed, dict) else []
            if not steps:
                steps = _fallback_segment(cot)
            else:
                steps = _find_span_offsets(cot, steps)
            traces.append(self._to_json_string({"steps": steps}))

        output = dataframe.copy()
        output[output_key] = traces
        return storage.write(output)


class CoTTraceGraphBuilder(JSONParsingMixin):
    """Build a lightweight trajectory graph from extracted CoT steps."""

    def __init__(self):
        self.logger = get_logger()

    def _build_graph(self, trace: Dict[str, Any]) -> Dict[str, Any]:
        steps = trace.get("steps", []) if isinstance(trace, dict) else []
        ids = {int(step.get("step_id", i + 1)) for i, step in enumerate(steps)}
        nodes = []
        edges = []
        dependency_map: Dict[int, List[int]] = {}

        for i, step in enumerate(steps):
            sid = int(step.get("step_id", i + 1))
            deps = []
            for dep in step.get("depends_on", []) or []:
                try:
                    dep_id = int(dep)
                    if dep_id in ids and dep_id != sid:
                        deps.append(dep_id)
                except Exception:
                    continue
            if not deps and i > 0:
                deps = [int(steps[i - 1].get("step_id", i))]
            dependency_map[sid] = sorted(set(deps))
            for dep in dependency_map[sid]:
                edges.append({"source": dep, "target": sid, "type": "depends_on"})
            node = dict(step)
            node["step_id"] = sid
            node["depends_on"] = dependency_map[sid]
            nodes.append(node)

        downstream = {int(node["step_id"]): 0 for node in nodes}
        for edge in edges:
            downstream[int(edge["source"])] = downstream.get(int(edge["source"]), 0) + 1
        for node in nodes:
            node["downstream_degree"] = downstream.get(int(node["step_id"]), 0)
            node["prev_step_id"] = None
            node["next_step_id"] = None
        for idx, node in enumerate(nodes):
            if idx > 0:
                node["prev_step_id"] = nodes[idx - 1]["step_id"]
            if idx + 1 < len(nodes):
                node["next_step_id"] = nodes[idx + 1]["step_id"]

        return {"nodes": nodes, "edges": edges}

    def run(self, storage, trace_key: str = "cot_trace", output_key: str = "cot_trace_graph") -> str:
        dataframe = storage.read("dataframe")
        graphs = []
        for _, row in dataframe.iterrows():
            trace = self._safe_json_loads(row.get(trace_key), default={"steps": []})
            graphs.append(self._to_json_string(self._build_graph(trace)))
        output = dataframe.copy()
        output[output_key] = graphs
        return storage.write(output)


class CoTPruningPlanner(JSONParsingMixin):
    """Choose deletions with rules + graph dependency + optional answer-impact checks."""

    def __init__(self, llm_serving=None, config: Optional[PruningConfig] = None):
        self.llm_serving = llm_serving
        self.config = config or PruningConfig()
        self.logger = get_logger()

    def _rule_score(self, node: Dict[str, Any], previous_nodes: Sequence[Dict[str, Any]]) -> Tuple[float, List[str]]:
        typ = _normalize_text(node.get("type", "other")).lower()
        hint = _normalize_text(node.get("redundancy_hint", "none")).lower()
        answer_critical = _normalize_text(node.get("answer_critical", "uncertain")).lower()
        span = _normalize_text(node.get("span", ""))
        claim = _normalize_text(node.get("claim", ""))
        downstream = int(node.get("downstream_degree", 0) or 0)

        score = 0.0
        reasons: List[str] = []

        if hint in HIGH_REDUNDANCY_HINTS:
            score += 0.55
            reasons.append(f"high_redundancy_hint:{hint}")
        elif hint in MEDIUM_REDUNDANCY_HINTS:
            score += 0.32
            reasons.append(f"medium_redundancy_hint:{hint}")

        if typ in {"transition", "reflection"}:
            score += 0.25
            reasons.append(f"low_content_type:{typ}")
        if typ == "dead_end":
            score += 0.45
            reasons.append("dead_end")

        if downstream == 0 and typ not in {"conclusion", "verification"}:
            score += 0.15
            reasons.append("no_downstream_dependency")
        if answer_critical in {"yes", "true", "critical"} and self.config.protect_answer_critical:
            score -= 0.60
            reasons.append("protected_answer_critical")
        if typ in PROTECTED_TYPES:
            score -= 0.28
            reasons.append(f"protected_type:{typ}")
        if self.config.protect_first_symbol_intro and _contains_formula_or_symbol_intro(span):
            score -= 0.20
            reasons.append("formula_or_symbol_risk")

        max_sim = 0.0
        for prev in previous_nodes:
            max_sim = max(max_sim, _claim_similarity(claim, _normalize_text(prev.get("claim", ""))))
        if max_sim >= 0.72:
            score += 0.25
            reasons.append(f"duplicate_claim_sim:{max_sim:.2f}")

        return max(0.0, min(1.0, score)), reasons

    def _impact_prompts(
        self,
        question: str,
        answer: str,
        nodes: List[Dict[str, Any]],
        candidate_indices: List[int],
    ) -> List[str]:
        prompts = []
        for idx in candidate_indices:
            node = nodes[idx]
            previous_claims = [n.get("claim", "") for n in nodes[max(0, idx - 4):idx]]
            later_claims = [n.get("claim", "") for n in nodes[idx + 1:idx + 5]]
            prompts.append(
                ANSWER_IMPACT_USER_TEMPLATE.format(
                    question=question,
                    answer=answer,
                    step_json=json.dumps(node, ensure_ascii=False),
                    previous_claims=json.dumps(previous_claims, ensure_ascii=False),
                    later_claims=json.dumps(later_claims, ensure_ascii=False),
                )
            )
        return prompts

    def _plan_one(self, question: str, answer: str, graph: Dict[str, Any]) -> Dict[str, Any]:
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        decisions: List[Dict[str, Any]] = []
        candidate_indices = []
        previous_nodes: List[Dict[str, Any]] = []

        for idx, node in enumerate(nodes):
            score, reasons = self._rule_score(node, previous_nodes)
            previous_nodes.append(node)
            decision = "keep"
            if score >= self.config.delete_threshold:
                decision = "delete"
            elif self.config.uncertain_low <= score < self.config.uncertain_high:
                decision = "uncertain"
                candidate_indices.append(idx)
            decisions.append(
                {
                    "step_id": node.get("step_id", idx + 1),
                    "rule_score": score,
                    "decision": decision,
                    "reasons": reasons,
                    "answer_impact": None,
                }
            )

        if self.llm_serving is not None and self.config.use_llm_answer_impact and candidate_indices:
            prompts = self._impact_prompts(question, answer, nodes, candidate_indices)
            raw = self.llm_serving.generate_from_input(
                user_inputs=prompts,
                system_prompt=ANSWER_IMPACT_SYSTEM_PROMPT,
                json_schema=BINARY_JSON_SCHEMA,
            )
            for idx, response in zip(candidate_indices, raw):
                parsed = self._safe_json_loads(response, default={"decision": "keep", "confidence": 0.0, "reason": "parse_failed"})
                answer_decision = _normalize_text(parsed.get("decision", "keep")).lower()
                confidence = float(parsed.get("confidence", 0.0) or 0.0)
                decisions[idx]["answer_impact"] = parsed
                if answer_decision in {"delete", "safe_to_delete"} and confidence >= 0.65:
                    decisions[idx]["decision"] = "delete"
                    decisions[idx]["reasons"].append("llm_answer_impact_safe")
                else:
                    decisions[idx]["decision"] = "keep"
                    decisions[idx]["reasons"].append("llm_answer_impact_keep")
        else:
            for d in decisions:
                if d["decision"] == "uncertain":
                    d["decision"] = "keep" if self.config.verifier_keep_on_uncertain else "delete"
                    d["reasons"].append("uncertain_default")

        delete_count = sum(1 for d in decisions if d["decision"] == "delete")
        max_delete_count = int(len(decisions) * self.config.max_delete_ratio)
        if delete_count > max_delete_count:
            deletable = sorted(
                [d for d in decisions if d["decision"] == "delete"],
                key=lambda x: float(x.get("rule_score", 0.0)),
                reverse=True,
            )
            keep_delete_ids = {d["step_id"] for d in deletable[:max_delete_count]}
            for d in decisions:
                if d["decision"] == "delete" and d["step_id"] not in keep_delete_ids:
                    d["decision"] = "keep"
                    d["reasons"].append("max_delete_ratio_guardrail")

        delete_ids = [d["step_id"] for d in decisions if d["decision"] == "delete"]
        keep_ids = [d["step_id"] for d in decisions if d["decision"] != "delete"]
        return {"delete_step_ids": delete_ids, "keep_step_ids": keep_ids, "decisions": decisions}

    def run(
        self,
        storage,
        graph_key: str = "cot_trace_graph",
        question_key: str = "question",
        answer_key: str = "answer",
        output_key: str = "cot_prune_plan",
    ) -> str:
        dataframe = storage.read("dataframe")
        plans = []
        for _, row in dataframe.iterrows():
            graph = self._safe_json_loads(row.get(graph_key), default={"nodes": [], "edges": []})
            plans.append(
                self._to_json_string(
                    self._plan_one(
                        _normalize_text(row.get(question_key, "")),
                        _normalize_text(row.get(answer_key, "")),
                        graph,
                    )
                )
            )
        output = dataframe.copy()
        output[output_key] = plans
        return storage.write(output)


class CoTBridgeAndReconstructor(JSONParsingMixin):
    """Apply prune plan, copy kept spans, and insert minimal local bridges."""

    def __init__(self, llm_serving=None, min_gap_chars_for_bridge: int = 80):
        self.llm_serving = llm_serving
        self.min_gap_chars_for_bridge = min_gap_chars_for_bridge
        self.logger = get_logger()

    def _needs_bridge(self, left: Dict[str, Any], right: Dict[str, Any], deleted_nodes: List[Dict[str, Any]]) -> bool:
        if not deleted_nodes:
            return False
        deleted_chars = sum(len(_normalize_text(n.get("span", ""))) for n in deleted_nodes)
        if deleted_chars < self.min_gap_chars_for_bridge:
            return False
        left_type = _normalize_text(left.get("type", "")).lower()
        right_type = _normalize_text(right.get("type", "")).lower()
        if left_type in {"transition", "reflection"} or right_type in {"transition", "reflection"}:
            return False
        return True

    def _deleted_groups(self, nodes: List[Dict[str, Any]], delete_ids: set) -> List[Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]], List[Dict[str, Any]]]]:
        groups = []
        i = 0
        while i < len(nodes):
            if nodes[i].get("step_id") not in delete_ids:
                i += 1
                continue
            start = i
            while i < len(nodes) and nodes[i].get("step_id") in delete_ids:
                i += 1
            left = nodes[start - 1] if start - 1 >= 0 else None
            right = nodes[i] if i < len(nodes) else None
            groups.append((left, right, nodes[start:i]))
        return groups

    def _generate_bridges(
        self,
        question: str,
        answer: str,
        bridge_contexts: List[Tuple[Dict[str, Any], Dict[str, Any], List[Dict[str, Any]]]],
    ) -> List[Dict[str, Any]]:
        if self.llm_serving is None or not bridge_contexts:
            return [{"need_bridge": False, "bridge": "", "reason": "llm_disabled"} for _ in bridge_contexts]
        prompts = []
        for left, right, deleted_nodes in bridge_contexts:
            deleted_summary = [n.get("claim", "") for n in deleted_nodes]
            prompts.append(
                BRIDGE_USER_TEMPLATE.format(
                    question=question,
                    answer=answer,
                    left=_normalize_text(left.get("span", "")),
                    right=_normalize_text(right.get("span", "")),
                    deleted_summary=json.dumps(deleted_summary, ensure_ascii=False),
                )
            )
        raw = self.llm_serving.generate_from_input(
            user_inputs=prompts,
            system_prompt=BRIDGE_SYSTEM_PROMPT,
            json_schema=BRIDGE_JSON_SCHEMA,
        )
        return [self._safe_json_loads(x, default={"need_bridge": False, "bridge": "", "reason": "parse_failed"}) for x in raw]

    def _reconstruct_one(self, question: str, answer: str, graph: Dict[str, Any], plan: Dict[str, Any]) -> Dict[str, Any]:
        nodes = graph.get("nodes", []) if isinstance(graph, dict) else []
        delete_ids = set(plan.get("delete_step_ids", []))
        bridge_contexts = []
        bridge_positions = []
        for left, right, deleted_nodes in self._deleted_groups(nodes, delete_ids):
            if left is not None and right is not None and self._needs_bridge(left, right, deleted_nodes):
                bridge_positions.append((left.get("step_id"), right.get("step_id")))
                bridge_contexts.append((left, right, deleted_nodes))
        bridge_outputs = self._generate_bridges(question, answer, bridge_contexts)
        bridge_by_pair = {
            pair: bridge for pair, bridge in zip(bridge_positions, bridge_outputs)
        }

        parts: List[str] = []
        bridges_used: List[Dict[str, Any]] = []
        previous_kept: Optional[Dict[str, Any]] = None
        for node in nodes:
            if node.get("step_id") in delete_ids:
                continue
            if previous_kept is not None:
                pair = (previous_kept.get("step_id"), node.get("step_id"))
                bridge = bridge_by_pair.get(pair)
                if bridge and bridge.get("need_bridge") and not bridge.get("new_information_added", False):
                    text = _normalize_text(bridge.get("bridge", "")).strip()
                    if text:
                        parts.append(text)
                        bridges_used.append({"between": list(pair), "bridge": text, "reason": bridge.get("reason", "")})
            span = _normalize_text(node.get("span", "")).strip()
            if span:
                parts.append(span)
            previous_kept = node

        cleaned = "\n\n".join(parts).strip()
        raw_len = sum(len(_normalize_text(n.get("span", ""))) for n in nodes)
        clean_len = len(cleaned)
        return {
            "cleaned_cot": cleaned,
            "bridges_used": bridges_used,
            "deleted_step_ids": list(delete_ids),
            "copy_ratio_estimate": None if clean_len == 0 else max(0.0, min(1.0, 1.0 - sum(len(b["bridge"]) for b in bridges_used) / max(1, clean_len))),
            "char_reduction_ratio_estimate": None if raw_len == 0 else max(0.0, min(1.0, 1.0 - clean_len / max(1, raw_len))),
        }

    def run(
        self,
        storage,
        graph_key: str = "cot_trace_graph",
        plan_key: str = "cot_prune_plan",
        question_key: str = "question",
        answer_key: str = "answer",
        output_key: str = "cleaned_cot",
        metadata_key: str = "cot_cleaning_metadata",
    ) -> str:
        dataframe = storage.read("dataframe")
        cleaned_values = []
        metadata_values = []
        for _, row in dataframe.iterrows():
            graph = self._safe_json_loads(row.get(graph_key), default={"nodes": [], "edges": []})
            plan = self._safe_json_loads(row.get(plan_key), default={"delete_step_ids": []})
            result = self._reconstruct_one(
                _normalize_text(row.get(question_key, "")),
                _normalize_text(row.get(answer_key, "")),
                graph,
                plan,
            )
            cleaned_values.append(result.pop("cleaned_cot"))
            metadata_values.append(self._to_json_string(result))
        output = dataframe.copy()
        output[output_key] = cleaned_values
        output[metadata_key] = metadata_values
        return storage.write(output)


class CoTCleaningVerifier(JSONParsingMixin):
    """Final safety check. Optionally roll back unsafe cleaned CoTs."""

    def __init__(self, llm_serving=None, rollback_on_fail: bool = True):
        self.llm_serving = llm_serving
        self.rollback_on_fail = rollback_on_fail
        self.logger = get_logger()

    def run(
        self,
        storage,
        cot_key: str = "cot",
        cleaned_cot_key: str = "cleaned_cot",
        question_key: str = "question",
        answer_key: str = "answer",
        output_key: str = "final_cot",
        verification_key: str = "cot_clean_verification",
    ) -> str:
        dataframe = storage.read("dataframe")
        if self.llm_serving is None:
            output = dataframe.copy()
            output[output_key] = output[cleaned_cot_key]
            output[verification_key] = [
                self._to_json_string({"decision": "pass", "reason": "llm_verifier_disabled"})
                for _ in range(len(output))
            ]
            return storage.write(output)

        prompts = []
        for _, row in dataframe.iterrows():
            prompts.append(
                FINAL_VERIFY_USER_TEMPLATE.format(
                    question=_normalize_text(row.get(question_key, "")),
                    answer=_normalize_text(row.get(answer_key, "")),
                    raw_cot=_normalize_text(row.get(cot_key, "")),
                    cleaned_cot=_normalize_text(row.get(cleaned_cot_key, "")),
                )
            )
        raw = self.llm_serving.generate_from_input(
            user_inputs=prompts,
            system_prompt=FINAL_VERIFY_SYSTEM_PROMPT,
            json_schema=FINAL_VERIFY_JSON_SCHEMA,
        )
        final_values = []
        verify_values = []
        for response, (_, row) in zip(raw, dataframe.iterrows()):
            parsed = self._safe_json_loads(response, default={"decision": "fail", "reason": "parse_failed"})
            decision = _normalize_text(parsed.get("decision", "fail")).lower()
            passed = decision in {"pass", "safe", "accept"} and parsed.get("supports_answer", False) and not parsed.get("has_new_unsupported_claim", True)
            if passed or not self.rollback_on_fail:
                final_values.append(_normalize_text(row.get(cleaned_cot_key, "")))
            else:
                final_values.append(_normalize_text(row.get(cot_key, "")))
                parsed["rolled_back_to_original"] = True
            verify_values.append(self._to_json_string(parsed))

        output = dataframe.copy()
        output[output_key] = final_values
        output[verification_key] = verify_values
        return storage.write(output)


__all__ = [
    "CoTTraceExtractor",
    "CoTTraceGraphBuilder",
    "CoTPruningPlanner",
    "CoTBridgeAndReconstructor",
    "CoTCleaningVerifier",
    "PruningConfig",
]
