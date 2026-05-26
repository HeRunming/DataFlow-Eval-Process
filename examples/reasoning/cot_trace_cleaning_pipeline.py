"""Example pipeline for structure-preserving CoT trace cleaning.

Input JSON/JSONL columns by default:
- question: the math/reasoning question
- answer: final answer or expected answer
- cot: raw chain-of-thought / rationale to clean

Output columns:
- cot_trace: extracted typed steps
- cot_trace_graph: trajectory graph nodes and dependency edges
- cot_prune_plan: keep/delete decisions and decision reasons
- cleaned_cot: reconstructed CoT before final rollback
- cot_cleaning_metadata: deletion/bridge/reduction metadata
- final_cot: final training CoT after verifier rollback
- cot_clean_verification: final verifier result
"""

from __future__ import annotations

import argparse
import os

from dataflow.operators.reasoning import (
    CoTBridgeAndReconstructor,
    CoTCleaningVerifier,
    CoTPruningPlanner,
    CoTTraceExtractor,
    CoTTraceGraphBuilder,
    PruningConfig,
)
from dataflow.serving import APILLMServing_request
from dataflow.utils.storage import FileStorage


class CoTTraceCleaningPipeline:
    """A conservative CoT cleaning pipeline built from DataFlow operators."""

    def __init__(
        self,
        input_file: str,
        cache_path: str = "./cache/cot_trace_cleaning",
        file_name_prefix: str = "cot_trace_cleaning",
        cache_type: str = "jsonl",
        api_url: str = "https://api.openai.com/v1/chat/completions",
        model_name: str = "gpt-4o-mini",
        max_workers: int = 8,
        delete_threshold: float = 0.72,
        max_delete_ratio: float = 0.40,
        rollback_on_fail: bool = True,
    ):
        self.storage = FileStorage(
            first_entry_file_name=input_file,
            cache_path=cache_path,
            file_name_prefix=file_name_prefix,
            cache_type=cache_type,
        )
        self.llm_serving = APILLMServing_request(
            api_url=api_url,
            model_name=model_name,
            temperature=0.0,
            max_workers=max_workers,
        )
        self.extractor = CoTTraceExtractor(llm_serving=self.llm_serving)
        self.graph_builder = CoTTraceGraphBuilder()
        self.pruning_planner = CoTPruningPlanner(
            llm_serving=self.llm_serving,
            config=PruningConfig(
                delete_threshold=delete_threshold,
                max_delete_ratio=max_delete_ratio,
                use_llm_answer_impact=True,
                verifier_keep_on_uncertain=True,
            ),
        )
        self.reconstructor = CoTBridgeAndReconstructor(llm_serving=self.llm_serving)
        self.verifier = CoTCleaningVerifier(
            llm_serving=self.llm_serving,
            rollback_on_fail=rollback_on_fail,
        )

    def forward(
        self,
        question_key: str = "question",
        answer_key: str = "answer",
        cot_key: str = "cot",
        final_output_key: str = "final_cot",
    ) -> str:
        self.extractor.run(
            storage=self.storage.step(),
            question_key=question_key,
            answer_key=answer_key,
            cot_key=cot_key,
            output_key="cot_trace",
        )
        self.graph_builder.run(
            storage=self.storage.step(),
            trace_key="cot_trace",
            output_key="cot_trace_graph",
        )
        self.pruning_planner.run(
            storage=self.storage.step(),
            graph_key="cot_trace_graph",
            question_key=question_key,
            answer_key=answer_key,
            output_key="cot_prune_plan",
        )
        self.reconstructor.run(
            storage=self.storage.step(),
            graph_key="cot_trace_graph",
            plan_key="cot_prune_plan",
            question_key=question_key,
            answer_key=answer_key,
            output_key="cleaned_cot",
            metadata_key="cot_cleaning_metadata",
        )
        output_path = self.verifier.run(
            storage=self.storage.step(),
            cot_key=cot_key,
            cleaned_cot_key="cleaned_cot",
            question_key=question_key,
            answer_key=answer_key,
            output_key=final_output_key,
            verification_key="cot_clean_verification",
        )
        return output_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input_file", required=True)
    parser.add_argument("--cache_path", default="./cache/cot_trace_cleaning")
    parser.add_argument("--cache_type", default="jsonl")
    parser.add_argument("--api_url", default=os.environ.get("DF_API_URL", "https://api.openai.com/v1/chat/completions"))
    parser.add_argument("--model_name", default=os.environ.get("DF_MODEL_NAME", "gpt-4o-mini"))
    parser.add_argument("--question_key", default="question")
    parser.add_argument("--answer_key", default="answer")
    parser.add_argument("--cot_key", default="cot")
    parser.add_argument("--final_output_key", default="final_cot")
    parser.add_argument("--max_workers", type=int, default=8)
    parser.add_argument("--delete_threshold", type=float, default=0.72)
    parser.add_argument("--max_delete_ratio", type=float, default=0.40)
    args = parser.parse_args()

    pipeline = CoTTraceCleaningPipeline(
        input_file=args.input_file,
        cache_path=args.cache_path,
        cache_type=args.cache_type,
        api_url=args.api_url,
        model_name=args.model_name,
        max_workers=args.max_workers,
        delete_threshold=args.delete_threshold,
        max_delete_ratio=args.max_delete_ratio,
    )
    output_path = pipeline.forward(
        question_key=args.question_key,
        answer_key=args.answer_key,
        cot_key=args.cot_key,
        final_output_key=args.final_output_key,
    )
    print(f"Final output written to: {output_path}")


if __name__ == "__main__":
    main()
