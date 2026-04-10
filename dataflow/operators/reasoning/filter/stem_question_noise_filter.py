"""
StemQuestionNoiseFilter
========================
对 STEM 数据集的三类题型（true_false / multiple_choice / open_ended）
做规则级清洗和质量打标。

质量标签（quality_tag）：
  GOLD         : 通过所有过滤，可直接使用
  SILVER_C3    : 含单一判断子问题，可尝试提取（需人工确认）
  SILVER_M1    : 多选题选项内嵌无换行（已自动修复）
  SILVER_M2    : 多选题 LaTeX aligned 块（已自动修复）
  FILTERED_C1  : RAG/任务型 prompt 混入
  FILTERED_C3  : 多部分解答题（含子问题标号）
  FILTERED_C4  : 命题截断或不完整
  FILTERED_C5  : 缺乏参照上下文的常识题
  FILTERED_C6  : 答案/解题过程混入题目字段
  FILTERED_M2  : 多选题 LaTeX aligned 块无法自动修复
  FILTERED_M3  : 多选题 RAG/任务型 prompt 混入
  FILTERED_M4  : 非中文/英文题目
"""

import re
from typing import Optional

from dataflow.utils.registry import OPERATOR_REGISTRY
from dataflow import get_logger
from dataflow.utils.storage import DataFlowStorage
from dataflow.core import OperatorABC


# ──────────────────────────────────────────────────────────────
# 工具函数（模块级，供分类器复用）
# ──────────────────────────────────────────────────────────────

_WRAPPER_PATTERNS = [
    re.compile(r"题目：\{(.+?)\}", re.DOTALL),
    re.compile(r"题目：「(.+?)」", re.DOTALL),
    re.compile(r"题目：【(.+?)】", re.DOTALL),
    re.compile(r"题目：《(.+?)》", re.DOTALL),
]
_TOPIC_RE = re.compile(r"题目：(.+)", re.DOTALL)


def _extract_core(question: str) -> str:
    """从 wrapper prompt 中提取核心命题。若无 wrapper，原样返回。"""
    for pat in _WRAPPER_PATTERNS:
        m = pat.search(question)
        if m:
            return m.group(1).strip()
    m = _TOPIC_RE.search(question)
    if m:
        return m.group(1).strip()
    return question.strip()


def _normalize_answer_label(text: str, answer_model: str) -> str:
    """从 text / answer_model 字段提取标准化答案标签（True/False/Unknown）。"""
    combined = (str(answer_model) + " " + str(text)).lower()
    if re.search(r"\bfalse\b", combined):
        return "False"
    if re.search(r"\btrue\b", combined):
        return "True"
    if re.search(r"不正确|错误|不对|假|×|✗|错|不成立|不符合|不能|不是|不会|不存在|不满足", combined):
        return "False"
    if re.search(r"正确|对|真|√|✓|成立|可以|能|是|存在|满足|一定|必定", combined):
        return "True"
    return "Unknown"


# ── 截断检测 ──
def _is_truncated(core: str) -> bool:
    if len(core) < 10:
        return True
    if core.count("$") % 2 != 0 and len(core) < 60:
        return True
    if re.search(r"\\(sum|frac|sqrt|int|lim)\s*_?\{[^}]*$", core):
        return True
    return False


# ── C3 检测 ──
_C3_ROMAN_RE = re.compile(
    r"[（(]\s*[ⅠⅡⅢⅣⅤⅥⅦⅧⅨ]\s*[）)]"
    r"|[（(]\s*(?:II|III|IV|VI|VII|VIII|IX|I(?=[）)])|(?:ii|iii|iv|vi|vii|viii|ix))\s*[）)]"
)
_C3_CHINESE_NUM_RE = re.compile(r"（[一二三四五六七八九十]）")
_C3_DIGIT_RE = re.compile(
    r"(?:(?<=\s)|(?<=。)|(?<=；)|(?<=，)|(?<=,)|(?<=\n)|(?:^))[（(][1-9][）)]",
    re.MULTILINE,
)

# ── C5 检测 ──
_C5_NAME_RE = re.compile(
    r"^[小]?(刚|明|红|华|强|李|王|亮|花|燕|军|伟|芳|英|兰|丽)\S{0,15}"
    r"(是|有|的|身高|体重|喝|吃|用|花|每天|一天|长|宽|高)",
    re.MULTILINE,
)
_C5_INVALID_DATE_RE = re.compile(
    r"(1[3-9]|[2-9]\d)月\d+日"
    r"|[02-9]月30日"
    r"|2月2[9-9]日"
    r"|[13-9]月31日"
    r"|[469]月31日"
    r"|11月3[01]日"
    r"|12月3[2-9]日",
)

# ── C6 答案/解题过程混入 ──
_C6_PATTERNS = [
    re.compile(r"将其转换为小数，我们需要"),
    re.compile(r"^解[：:][\s\S]{30,}", re.MULTILINE),
    re.compile(r"<search_result>"),
    re.compile(r"首先，问题是[：:]"),
    re.compile(r"^(Step\s+\d+[：:]|首先|然后|最后)[，,][\s\S]{20,}", re.MULTILINE),
    re.compile(r"解题过程|解题思路|解析："),
]

# ── M1/M2 修复 ──
_M2_ALIGNED_RE = re.compile(r"\\begin\{aligned\}")
_M2_OPTION_RE = re.compile(
    r"_?\{?\(?([ABCD])\)?\}?\s*\\text\{([^}]+)\}",
    re.DOTALL,
)


# ──────────────────────────────────────────────────────────────
# 分类器函数
# ──────────────────────────────────────────────────────────────

def _classify_true_false(record: dict) -> tuple[str, str]:
    q = record.get("question", "")
    core = _extract_core(q)

    if "[AI_Search_Data_with_corner_mark]" in q or "<search_result>" in q:
        return "FILTERED_C1", "RAG搜索任务混入"
    if "请判断以下两个题目是否匹配" in q or "召回的题目" in q:
        return "FILTERED_C1", "题目匹配打分任务"

    if _is_truncated(core):
        return "FILTERED_C4", "命题截断或不完整"

    for pat in _C6_PATTERNS:
        if pat.search(core):
            return "FILTERED_C6", "解题过程或答案混入题目字段"

    roman_parts = _C3_ROMAN_RE.findall(core)
    chinese_num_parts = _C3_CHINESE_NUM_RE.findall(core)
    digit_parts = _C3_DIGIT_RE.findall(core)

    if roman_parts or chinese_num_parts:
        if len(roman_parts) == 1 and not re.search(r"[（(]\s*[ⅡⅢⅣⅤIi][iI]?\s*[）)]", core):
            return "SILVER_C3", "含单一判断子问题，可尝试提取"
        return "FILTERED_C3", "多部分解答题（含罗马/中文数字子问题）"

    if len(digit_parts) >= 2:
        return "FILTERED_C3", "多子问题综合题（含(1)(2)等子问题）"

    if _C5_NAME_RE.search(core) and len(core) < 60:
        if _C5_INVALID_DATE_RE.search(core):
            return "GOLD", "含明确逻辑错误（不存在的日期）"
        return "FILTERED_C5", "缺乏参照上下文的常识题"

    return "GOLD", ""


def _fix_mc_options(question: str) -> tuple[Optional[str], str]:
    """修复多选题格式问题，返回 (修复后的 question 或 None, 状态)"""
    if (
        re.search(r"\bA[.、．]", question)
        and re.search(r"\bB[.、．]", question)
        and "\nA" not in question
        and "\nB" not in question
    ):
        fixed = re.sub(r"\s+([ABCD])[.、．]\s*", r"\n\1. ", question)
        return fixed.strip(), "M1_INLINE_FIXED"

    if _M2_ALIGNED_RE.search(question):
        matches = _M2_OPTION_RE.findall(question)
        if len(matches) >= 3:
            stem = re.sub(r"\$\\begin\{aligned\}[\s\S]*?\\end\{aligned\}\$", "", question).strip()
            options_str = "\n".join(f"{opt}. {text.strip()}" for opt, text in matches)
            return f"{stem}\n{options_str}", "M2_ALIGNED_FIXED"
        return None, "M2_ALIGNED_UNFIXABLE"

    return question, "OK"


def _classify_multiple_choice(record: dict) -> tuple[str, str, str]:
    q = record.get("question", "")
    core = _extract_core(q)

    if "[AI_Search_Data_with_corner_mark]" in q or "<search_result>" in q:
        return "FILTERED_M3", "RAG搜索任务混入", q

    if _is_truncated(core):
        return "FILTERED_C4", "命题截断或不完整", q

    has_cjk = bool(re.search(r"[\u4e00-\u9fff]", core))
    has_latin = bool(re.search(r"[a-zA-Z]", core))
    is_other_lang = (
        not has_cjk
        and not has_latin
        and bool(re.search(r"[\u00C0-\u024F\u0400-\u04FF]", core))
    )
    if is_other_lang:
        return "FILTERED_M4", "非中文/英文题目", q

    fixed, fix_status = _fix_mc_options(core)
    if fix_status == "M2_ALIGNED_UNFIXABLE":
        return "FILTERED_M2", "LaTeX aligned块无法自动修复", q
    if fix_status in ("M1_INLINE_FIXED", "M2_ALIGNED_FIXED"):
        tag = "SILVER_M1" if fix_status == "M1_INLINE_FIXED" else "SILVER_M2"
        return tag, fix_status, fixed

    return "GOLD", "", fixed if fixed else core


def _classify_open_ended(record: dict) -> tuple[str, str]:
    q = record.get("question", "")

    if "[AI_Search_Data_with_corner_mark]" in q or "<search_result>" in q:
        return "FILTERED_C1", "RAG搜索任务混入"
    if "请判断以下两个题目是否匹配" in q or "召回的题目" in q:
        return "FILTERED_C1", "题目匹配打分任务"

    if len(q.strip()) < 10:
        return "FILTERED_C4", "命题截断或不完整"
    if q.count("$") % 2 != 0 and len(q) < 60:
        return "FILTERED_C4", "LaTeX 未闭合且命题极短"

    for pat in _C6_PATTERNS:
        if pat.search(q):
            return "FILTERED_C6", "解题过程或答案混入题目字段"

    return "GOLD", ""


# ──────────────────────────────────────────────────────────────
# DataFlow Operator
# ──────────────────────────────────────────────────────────────

@OPERATOR_REGISTRY.register()
class StemQuestionNoiseFilter(OperatorABC):
    """
    STEM 数据规则清洗算子。

    支持三种题型模式：
    - true_false     : 判断题（C1/C3/C4/C5/C6 过滤）
    - multiple_choice: 多选题（M1/M2/M3/M4 过滤+修复）
    - open_ended     : 解答题（C1/C4/C6 基础过滤，不修改内容）

    每条记录新增字段：
        output_quality_key : GOLD / SILVER_* / FILTERED_*
        output_reason_key  : 过滤原因（GOLD 时为空字符串）
        output_clean_key   : 提取/修复后的核心命题
        output_label_key   : 标准化答案标签（true_false 时为 True/False/Unknown）

    过滤行为（默认）：
        filter_mode=True 时，只保留 GOLD 和 SILVER 样本，去掉 FILTERED_* 记录。
        filter_mode=False 时，保留全部记录（在字段中标注质量标签）。
    """

    _VALID_MODES = ("true_false", "multiple_choice", "open_ended")

    def __init__(
        self,
        mode: str = "true_false",
        filter_mode: bool = True,
    ):
        super().__init__()
        self.logger = get_logger()
        if mode not in self._VALID_MODES:
            raise ValueError(f"mode must be one of {self._VALID_MODES}, got: {mode!r}")
        self.mode = mode
        self.filter_mode = filter_mode

    @staticmethod
    def get_desc(lang: str = "zh") -> str:
        if lang == "zh":
            return (
                "STEM 数据规则清洗算子。\n"
                "支持三种题型：true_false（判断题）、multiple_choice（多选题）、open_ended（解答题）。\n\n"
                "过滤类别：\n"
                "- C1: RAG/任务型 prompt 混入\n"
                "- C3: 多子问题综合题\n"
                "- C4: 命题截断或不完整\n"
                "- C5: 缺乏参照上下文的常识题\n"
                "- C6: 答案/解题过程混入题目字段\n"
                "- M1/M2: 多选题格式问题（自动修复 → SILVER）\n"
                "- M3/M4: 多选题 RAG 混入 / 非中英文\n\n"
                "参数：\n"
                "- mode: 题型 (true_false / multiple_choice / open_ended)\n"
                "- filter_mode: True 时过滤 FILTERED_* 样本，False 时只打标不过滤"
            )
        return (
            "Rule-based noise filter for STEM datasets.\n"
            "Supports true_false, multiple_choice, and open_ended question types.\n"
            "Tags each record with quality_tag (GOLD/SILVER/FILTERED) and filter_reason."
        )

    def _process_record(self, record: dict) -> dict:
        if self.mode == "true_false":
            tag, reason = _classify_true_false(record)
            core = _extract_core(record.get("question", ""))
            label = _normalize_answer_label(
                str(record.get("text", "")),
                str(record.get("answer_model", "")),
            )
        elif self.mode == "multiple_choice":
            tag, reason, core = _classify_multiple_choice(record)
            label = str(record.get("answer_model", ""))
        else:  # open_ended
            tag, reason = _classify_open_ended(record)
            core = record.get("question", "")
            label = str(record.get("answer_model", ""))

        record["quality_tag"] = tag
        record["filter_reason"] = reason
        record["question_clean"] = core
        record["answer_label"] = label
        return record

    def run(
        self,
        storage: DataFlowStorage,
        input_question_key: str = "question",
        output_quality_key: str = "quality_tag",
        output_reason_key: str = "filter_reason",
        output_clean_key: str = "question_clean",
        output_label_key: str = "answer_label",
    ) -> list[str]:
        df = storage.read("dataframe")
        self.logger.info(
            f"[StemQuestionNoiseFilter] mode={self.mode}, 读取到 {len(df)} 条数据"
        )

        records = df.to_dict(orient="records")
        records = [self._process_record(r) for r in records]

        import pandas as pd
        df_out = pd.DataFrame(records)

        total = len(df_out)
        gold = (df_out[output_quality_key].str.startswith("GOLD")).sum()
        silver = (df_out[output_quality_key].str.startswith("SILVER")).sum()
        filtered = (df_out[output_quality_key].str.startswith("FILTERED")).sum()
        self.logger.info(
            f"[StemQuestionNoiseFilter] GOLD={gold}({gold/total:.1%}) "
            f"SILVER={silver}({silver/total:.1%}) "
            f"FILTERED={filtered}({filtered/total:.1%})"
        )

        if self.filter_mode:
            df_out = df_out[~df_out[output_quality_key].str.startswith("FILTERED")]
            self.logger.info(
                f"[StemQuestionNoiseFilter] filter_mode=True，保留 {len(df_out)}/{total} 条"
            )

        storage.write(df_out)
        return [output_quality_key, output_reason_key, output_clean_key, output_label_key]
