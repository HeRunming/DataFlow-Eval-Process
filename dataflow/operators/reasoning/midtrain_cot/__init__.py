"""
dataflow/operators/reasoning/midtrain_cot
=========================================

Midtrain-oriented Long-CoT -> Medium-CoT distillation operators.

Unlike the legacy ``reasoning.refine`` operators that mainly delete or compress
local steps/chunks, this package targets midtrain data construction: preserve
reasoning capability signals while controlling the length distribution.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .cot_midtrain_medium_refiner import CoTMidtrainMediumRefiner

else:
    import sys
    from dataflow.utils.registry import LazyLoader, generate_import_structure_from_type_checking

    cur_path = "dataflow/operators/reasoning/midtrain_cot/"
    _import_structure = generate_import_structure_from_type_checking(__file__, cur_path)
    sys.modules[__name__] = LazyLoader(__name__, cur_path, _import_structure)
