from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # generate
    from .generate.reasoning_answer_generator import ReasoningAnswerGenerator
    from .generate.reasoning_question_generator import ReasoningQuestionGenerator
    from .generate.reasoning_answer_extraction_qwenmatheval_generator import ReasoningAnswerExtractionQwenMathEvalGenerator
    from .generate.reasoning_pseudo_answer_generator import ReasoningPseudoAnswerGenerator
    from .generate.reasoning_pretrain_format_convert_generator import ReasoningPretrainFormatConvertGenerator
    from .generate.reasoning_question_fusion_generator import ReasoningQuestionFusionGenerator

    # eval
    from .eval.reasoning_category_dataset_evaluator import ReasoningCategoryDatasetEvaluator
    from .eval.reasoning_difficulty_dataset_evaluator import ReasoningDifficultyDatasetEvaluator
    from .eval.reasoning_token_dataset_evaluator import ReasoningTokenDatasetEvaluator
    from .eval.reasoning_question_category_sample_evaluator import ReasoningQuestionCategorySampleEvaluator
    from .eval.reasoning_question_difficulty_sample_evaluator import ReasoningQuestionDifficultySampleEvaluator
    from .eval.reasoning_question_solvable_sample_evaluator import ReasoningQuestionSolvableSampleEvaluator
    
    # filter
    from .filter.reasoning_answer_formatter_filter import ReasoningAnswerFormatterFilter
    from .filter.reasoning_answer_groundtruth_filter import ReasoningAnswerGroundTruthFilter
    from .filter.reasoning_answer_ngram_filter import ReasoningAnswerNgramFilter
    from .filter.reasoning_answer_pipeline_root_filter import ReasoningAnswerPipelineRootFilter
    from .filter.reasoning_answer_token_length_filter import ReasoningAnswerTokenLengthFilter
    from .filter.reasoning_question_filter import ReasoningQuestionFilter
    from .filter.reasoning_answer_model_judge_filter import ReasoningAnswerModelJudgeFilter

    # refine – long-CoT post-processing (offline compression / cleaning)
    from .refine.cot_llm_judge_refiner import CoTLLMJudgeRefiner
    from .refine.cot_llm_judge_refiner_fast import CoTLLMJudgeRefinerFast
    from .refine.cot_monte_carlo_refiner import CoTMonteCarloRefiner
    from .refine.cot_chunk_compress_refiner import CoTChunkCompressRefiner
    from .refine.cot_chunk_compress_refiner_fast import CoTChunkCompressRefinerFast
    from .refine.cot_pattern_refiner import CoTPatternRefiner
    from .refine.cot_pattern_refiner_fast import CoTPatternRefinerFast
    from .refine.cot_math_norm_refiner import CoTMathNormRefiner

    # midtrain_cot – Long-CoT -> Medium-CoT distillation for midtraining
    from .midtrain_cot.cot_midtrain_medium_refiner import CoTMidtrainMediumRefiner

else:
    import sys
    from dataflow.utils.registry import LazyLoader, generate_import_structure_from_type_checking

    cur_path = "dataflow/operators/reasoning/"

    _import_structure = generate_import_structure_from_type_checking(__file__, cur_path)
    sys.modules[__name__] = LazyLoader(__name__, "dataflow/operators/reasoning/", _import_structure)