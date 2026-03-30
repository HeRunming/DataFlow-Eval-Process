"""
CoT 清洗算子实验脚本
======================
数据集  : dataflow_reasoningmath_10k.jsonl
          字段: instruction (题目补充), input (题目正文), output (含 <think> 的长 CoT)

测试内容:
  - 从 10k 数据中抽取 N_SAMPLES 条（默认 20 条）作为测试子集
  - 分别运行四种算子：
      A. CoTLLMJudgeRefiner   —— LLM-Judge 步骤级过滤
      B. CoTMonteCarloRefiner —— Monte Carlo 重要性评分
      C. CoTChunkCompressRefiner —— Chunk 级别分类 + 差异化压缩
      D. CoTPatternRefiner    —— Thinking Pattern 九分类处理
  - 输出每个算子的压缩统计，将结果写入 results/ 目录

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
如何提供 API URL 和 Key（三步走）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

第一步：设置 API Key（安全起见，DataFlow 只从环境变量读取 Key）
  export DF_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxx"

第二步：设置 API endpoint（默认是 OpenAI 官方；换成你自己的地址）
  export DF_API_URL="https://api.deepseek.com/v1/chat/completions"
  # 或本地 vLLM:
  # export DF_API_URL="http://localhost:8000/v1/chat/completions"

第三步：设置模型名（API 调用时的 model 字段）
  export DF_MODEL_NAME="deepseek-chat"

然后运行：
  python run_cot_clean_test.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Method B 特别说明
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Method B (Monte Carlo) 需要数据集中有标准答案列来计算「答案正确率」。
本数据集（dataflow_reasoningmath_10k.jsonl）没有独立的 answer 列，
因此 Method B 当前配置 answer_key=None，会自动 skip MC 评分（保留所有步骤）。

如果你有包含答案列的数据集，修改脚本中 run_method_b 里的：
  answer_key = "your_answer_column_name"
即可启用完整的 MC 评分流程。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import random
import sys
import time
from pathlib import Path

import pandas as pd

# ─── 确保能找到 DataFlow 仓库 ──────────────────────────────────────────────
_REPO = Path(__file__).resolve().parent.parent / "DataFlow"
if _REPO.exists() and str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# ─── 用户配置（通过环境变量传入，不要把 key 写在代码里）─────────────────────
USER_CONFIG = {
    # API endpoint，兼容所有 OpenAI-compatible 接口
    "api_url":   os.environ.get("DF_API_URL",    "https://api.openai.com/v1/chat/completions"),
    # 环境变量名：DataFlow 从此变量名对应的环境变量里读取 API Key
    "key_name":  "DF_API_KEY",
    # 模型名称（传给 API 的 model 字段）
    "model":     os.environ.get("DF_MODEL_NAME", "gpt-4o"),
    # 并发线程数
    "max_workers": 4,
}

# ─── 实验参数 ─────────────────────────────────────────────────────────────
DATA_PATH   = Path(__file__).parent / "dataflow_reasoningmath_10k.jsonl"
RESULTS_DIR = Path(__file__).parent / "results"
CACHE_DIR   = Path(__file__).parent / "cache"

N_SAMPLES   = 20       # 抽取条数（调大可做更充分测试，建议先用 20 验证）
RANDOM_SEED = 42
MC_SAMPLES  = 4        # Method B 的 Monte Carlo 采样次数（测试阶段设小）

# 选择要运行的方案（注释掉不需要的方案）
RUN_METHODS = ["A", "B", "C", "D"]


# ═══════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════

def load_sample(path: Path, n: int, seed: int) -> pd.DataFrame:
    """随机抽取 n 条数据，统一字段名，返回 DataFrame。"""
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    random.seed(seed)
    sampled = random.sample(rows, min(n, len(rows)))
    df = pd.DataFrame(sampled)
    # output → cot（算子读取的列名）
    if "output" in df.columns:
        df = df.rename(columns={"output": "cot"})
    # instruction + input → problem（给算子作为问题上下文）
    if "instruction" in df.columns and "input" in df.columns:
        df["problem"] = (df["instruction"].fillna("").str.strip()
                         + "\n"
                         + df["input"].fillna("").str.strip())
    elif "input" in df.columns:
        df["problem"] = df["input"]
    else:
        df["problem"] = ""
    return df


def build_llm(temperature: float = 0.0):
    """构建 APILLMServing_request 实例。"""
    from dataflow.serving.api_llm_serving_request import APILLMServing_request
    return APILLMServing_request(
        api_url             = USER_CONFIG["api_url"],
        key_name_of_api_key = USER_CONFIG["key_name"],
        model_name          = USER_CONFIG["model"],
        temperature         = temperature,
        max_workers         = USER_CONFIG["max_workers"],
    )


def run_operator(op, df: pd.DataFrame, method_tag: str,
                 input_key: str = "cot", problem_key: str = "problem") -> pd.DataFrame:
    """
    将 df 写成临时 jsonl，用 FileStorage + step() 调用算子，返回结果 DataFrame。

    DataFlow 的 FileStorage 使用方式：
      storage = FileStorage(first_entry_file_name=input_file, cache_path=..., ...)
      storage.step()   ← 必须调用，将内部步骤计数器从 -1 推进到 0
      op.run(storage, ...)   ← 算子内部调用 storage.read() 读 step0，storage.write() 写 step1
      结果文件路径 = {cache_path}/{file_name_prefix}_step1.{cache_type}
    """
    from dataflow.utils.storage import FileStorage

    method_cache = CACHE_DIR / method_tag
    method_cache.mkdir(parents=True, exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)

    # 写入输入文件
    in_file = str(method_cache / "input.jsonl")
    df.to_json(in_file, orient="records", lines=True, force_ascii=False)

    storage = FileStorage(
        first_entry_file_name = in_file,
        cache_path            = str(method_cache),
        file_name_prefix      = "dataflow_cache",
        cache_type            = "jsonl",
    )
    # step() 是必须调用的——将 operator_step 从 -1 推进到 0，
    # 之后 read() 读 step0（即 first_entry_file_name），write() 写 step1
    storage.step()

    op.run(
        storage         = storage,
        input_key       = input_key,
        output_key      = "cot_cleaned",
        output_stats_key= "cot_clean_stats",
        problem_key     = problem_key,
    )

    # 读取 step1 输出文件
    out_file = str(method_cache / "dataflow_cache_step1.jsonl")
    result = pd.read_json(out_file, lines=True)
    return result


def print_stats(method: str, df: pd.DataFrame):
    """打印压缩统计摘要。"""
    col = "cot_clean_stats"
    if col not in df.columns:
        print(f"  [Method {method}] 无统计列（可能算子出错）")
        return
    orig_chars, out_chars, skipped = [], [], 0
    for _, row in df.iterrows():
        try:
            s = json.loads(row.get(col) or "{}")
            if s.get("skipped"):
                skipped += 1
                continue
            if "original_chars" in s and "output_chars" in s:
                orig_chars.append(s["original_chars"])
                out_chars.append(s["output_chars"])
        except Exception:
            pass
    if orig_chars:
        avg_orig  = sum(orig_chars) // len(orig_chars)
        avg_out   = sum(out_chars)  // len(out_chars)
        avg_ratio = sum(o / max(i, 1) for i, o in zip(orig_chars, out_chars)) / len(orig_chars)
        print(f"  ✓ 平均字符数: {avg_orig:,} → {avg_out:,}  "
              f"(保留率 {avg_ratio:.1%} / 压缩率 {1-avg_ratio:.1%})  "
              f"[{len(orig_chars)} 条有效, {skipped} 条 skipped]")
    else:
        print(f"  (全部 skipped 或无统计数据，共 {skipped} 条 skipped)")


def save_results(method: str, df: pd.DataFrame):
    out = RESULTS_DIR / f"method_{method}_results.jsonl"
    df.to_json(str(out), orient="records", lines=True, force_ascii=False)
    print(f"  → 已保存: {out}")


# ═══════════════════════════════════════════════════════════════════════════
# 各方案入口
# ═══════════════════════════════════════════════════════════════════════════

def run_method_a(df: pd.DataFrame) -> pd.DataFrame:
    """Method A: LLM-Judge 步骤级过滤"""
    from dataflow.operators.reasoning.refine.cot_llm_judge_refiner import CoTLLMJudgeRefiner
    print("\n[Method A] LLM-Judge 步骤级过滤")
    op = CoTLLMJudgeRefiner(
        llm_serving       = build_llm(temperature=0.0),
        min_steps_to_keep = 2,
    )
    result = run_operator(op, df, method_tag="A")
    print_stats("A", result)
    save_results("A", result)
    return result


def run_method_b(df: pd.DataFrame) -> pd.DataFrame:
    """
    Method B: Monte Carlo 重要性评分

    注意：本数据集无标准答案列，answer_key=None 时算子会自动 skip MC 评分
    （保留全部步骤，统计中 skipped=True）。
    如需真正运行 MC 评分，将 answer_key 改为你的答案列名，并确保数据集有该列。
    """
    from dataflow.operators.reasoning.refine.cot_monte_carlo_refiner import CoTMonteCarloRefiner
    print("\n[Method B] Monte Carlo 重要性评分")
    print("  提示：当前 answer_key=None，MC 评分会被 skip（无答案列用于验证正确率）。")
    print("        若有答案列，修改 answer_key='your_answer_col' 以启用完整 MC 评分。")
    op = CoTMonteCarloRefiner(
        llm_serving          = build_llm(temperature=0.8),  # 高温度保证 MC 多样性
        mc_samples           = MC_SAMPLES,
        importance_threshold = 0.0,
        per_token_value      = False,
        min_steps_to_keep    = 2,
        answer_key           = None,   # ← 改为答案列名以启用 MC 评分
    )
    result = run_operator(op, df, method_tag="B")
    print_stats("B", result)
    save_results("B", result)
    return result


def run_method_c(df: pd.DataFrame) -> pd.DataFrame:
    """Method C: Chunk 级别分类 + 差异化压缩（R1-Compress 风格）"""
    from dataflow.operators.reasoning.refine.cot_chunk_compress_refiner import CoTChunkCompressRefiner
    print("\n[Method C] Chunk 级别压缩 (R1-Compress 风格)")
    op = CoTChunkCompressRefiner(
        llm_serving      = build_llm(temperature=0.3),
        num_candidates   = 1,    # 1=不做候选搜索（快）；改为 3 以启用候选择优
        min_chunk_tokens = 30,
    )
    result = run_operator(op, df, method_tag="C")
    print_stats("C", result)
    save_results("C", result)
    return result


def run_method_d(df: pd.DataFrame) -> pd.DataFrame:
    """Method D: Thinking Pattern 九分类 + 差异化处理（Think Wisely 风格）"""
    from dataflow.operators.reasoning.refine.cot_pattern_refiner import CoTPatternRefiner
    print("\n[Method D] Thinking Pattern 分类 (Think Wisely 风格)")
    op = CoTPatternRefiner(
        llm_serving           = build_llm(temperature=0.0),
        min_fragments_to_keep = 2,
        # 激进模式：将冗余验证也删除
        # action_overrides = {"REDUNDANT_VERIFICATION": "delete"},
    )
    result = run_operator(op, df, method_tag="D")
    print_stats("D", result)
    save_results("D", result)
    return result


# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    # ── 检查 API Key ──
    api_key = os.environ.get(USER_CONFIG["key_name"])
    if not api_key:
        print(f"\n[错误] 未找到环境变量 {USER_CONFIG['key_name']}！")
        print("请先在终端执行以下命令，然后重新运行本脚本：\n")
        print(f"  export {USER_CONFIG['key_name']}='your-api-key-here'")
        print(f"  export DF_API_URL='https://your-endpoint/v1/chat/completions'")
        print(f"  export DF_MODEL_NAME='your-model-name'")
        print()
        sys.exit(1)

    print("=" * 62)
    print("  CoT 清洗算子实验")
    print("=" * 62)
    print(f"  数据集 : {DATA_PATH.name}  ({DATA_PATH.stat().st_size // 1024 // 1024} MB)")
    print(f"  抽样数 : {N_SAMPLES} 条（随机种子 {RANDOM_SEED}）")
    print(f"  API    : {USER_CONFIG['api_url']}")
    print(f"  模型   : {USER_CONFIG['model']}")
    print(f"  方案   : {', '.join(RUN_METHODS)}")
    print("=" * 62)

    print("\n加载数据...")
    df = load_sample(DATA_PATH, N_SAMPLES, RANDOM_SEED)
    print(f"  已抽取 {len(df)} 条，CoT 平均字符数: {int(df['cot'].str.len().mean()):,}")

    runners = {
        "A": run_method_a,
        "B": run_method_b,
        "C": run_method_c,
        "D": run_method_d,
    }

    for method in RUN_METHODS:
        t0 = time.time()
        try:
            runners[method](df.copy())
            print(f"  ⏱  Method {method} 耗时 {time.time() - t0:.1f}s")
        except Exception as exc:
            import traceback
            print(f"\n  [ERROR] Method {method} 运行失败: {exc}")
            traceback.print_exc()

    print("\n" + "=" * 62)
    print("  实验完成！结果保存在 results/ 目录。")
    print("=" * 62)


if __name__ == "__main__":
    main()
