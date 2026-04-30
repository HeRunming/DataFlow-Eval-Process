"""
CoT 清洗算子实验 Pipeline
=========================
数据集  : dataflow_reasoningmath_10k.jsonl
          字段: instruction (题目补充), input (题目正文), output (含 <think> 的长 CoT)

测试内容:
  - 从 10k 数据中抽取 N_SAMPLES 条（默认 20 条）作为测试子集
  - 分别运行四种算子（Pipeline 风格，类似 reasoning_math_pipeline.py）：
      A. CoTLLMJudgeRefiner       —— LLM-Judge 步骤级过滤
      B. CoTMonteCarloRefiner     —— Monte Carlo 重要性评分
      C. CoTChunkCompressRefiner  —— Chunk 级别分类 + 差异化压缩
      D. CoTPatternRefiner        —— Thinking Pattern 九分类处理
  - 输出每个算子的压缩统计，将结果写入 results/ 目录

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
如何提供 API URL 和 Key（三步走）
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

第一步：设置 API Key（安全起见，DataFlow 只从环境变量读取 Key）
  export DF_API_KEY="sk-xxxxxxxxxxxxxxxxxxxxxxxx"

第二步：设置 API endpoint（默认是 OpenAI 官方；换成你自己的地址）
  export DF_API_URL="http://123.129.219.111:3000/v1/chat/completions"

第三步：设置模型名（API 调用时的 model 字段）
  export DF_MODEL_NAME="gpt-5.1"

然后运行：
  python run_cot_clean_test.py

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
并发说明
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
MAX_WORKERS 控制 APILLMServing_request 的线程并发数。
当 API 支持高并发（200-400）时，可将 MAX_WORKERS 调大以充分利用带宽。
默认设为 200，可通过环境变量 DF_MAX_WORKERS 覆盖。

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Method B 特别说明
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Method B (Monte Carlo) 需要数据集中有标准答案列来计算「答案正确率」。
本数据集（dataflow_reasoningmath_10k.jsonl）没有独立的 answer 列，
因此 Method B 当前配置 answer_key=None，会自动 skip MC 评分（保留所有步骤）。

如果你有包含答案列的数据集，修改 CoTCleanPipeline.__init__ 里 method_b 的：
  answer_key = "your_answer_column_name"
即可启用完整的 MC 评分流程。
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import json
import os
import random
import time
from pathlib import Path

import pandas as pd

from dataflow.utils.storage import FileStorage
from dataflow.serving import APILLMServing_request
from dataflow.operators.reasoning import (
    CoTLLMJudgeRefiner,
    CoTMonteCarloRefiner,
    CoTChunkCompressRefiner,
    CoTPatternRefiner,
)

# ─── 全局配置（通过环境变量传入，不要把 key 写在代码里）────────────────────────
API_URL     = os.environ.get("DF_API_URL",      "https://api.openai.com/v1/chat/completions")
API_KEY_VAR = "DF_API_KEY"                       # DataFlow 从此环境变量名读取 Key
MODEL_NAME  = os.environ.get("DF_MODEL_NAME",   "gpt-4o")
MAX_WORKERS = int(os.environ.get("DF_MAX_WORKERS", "200"))  # 并发线程数，默认 200

# ─── 实验参数 ──────────────────────────────────────────────────────────────
_HERE       = Path(__file__).parent
DATA_PATH   = _HERE / "dataflow_reasoningmath_10k.jsonl"
RESULTS_DIR = _HERE / "results"
CACHE_DIR   = _HERE / "cache"

N_SAMPLES   = 20    # 抽取条数（调大可做更充分测试，建议先用 20 验证）
RANDOM_SEED = 42
MC_SAMPLES  = 4     # Method B 的 Monte Carlo 采样次数（测试阶段设小）

# 选择要运行的方案（注释掉不需要的方案）
RUN_METHODS = ["A", "B", "C", "D"]


# ═══════════════════════════════════════════════════════════════════════════
# Pipeline 类（类似 ReasoningMath_APIPipeline 风格）
# ═══════════════════════════════════════════════════════════════════════════

class CoTCleanPipeline:
    """
    CoT 清洗实验 Pipeline。

    对标 dataflow/statics/pipelines/api_pipelines/reasoning_math_pipeline.py 风格：
      - __init__ 中统一声明 llm_serving、storage、各算子实例
      - forward(method) 中按需调用对应算子
    """

    def __init__(self, input_file: str):
        # ── LLM Serving（所有方案共用，节省连接开销）──
        self.llm_serving_greedy = APILLMServing_request(
            api_url             = API_URL,
            key_name_of_api_key = API_KEY_VAR,
            model_name          = MODEL_NAME,
            temperature         = 0.0,
            max_workers         = MAX_WORKERS,
        )
        self.llm_serving_sampling = APILLMServing_request(
            api_url             = API_URL,
            key_name_of_api_key = API_KEY_VAR,
            model_name          = MODEL_NAME,
            temperature         = 0.8,   # Method B MC 采样需要多样性
            max_workers         = MAX_WORKERS,
        )
        self.llm_serving_chunk = APILLMServing_request(
            api_url             = API_URL,
            key_name_of_api_key = API_KEY_VAR,
            model_name          = MODEL_NAME,
            temperature         = 0.3,   # Method C chunk 压缩
            max_workers         = MAX_WORKERS,
        )

        # ── 算子实例 ──
        self.method_a = CoTLLMJudgeRefiner(
            llm_serving       = self.llm_serving_greedy,
            min_steps_to_keep = 2,
        )
        self.method_b = CoTMonteCarloRefiner(
            llm_serving          = self.llm_serving_sampling,
            mc_samples           = MC_SAMPLES,
            importance_threshold = 0.0,
            per_token_value      = False,
            min_steps_to_keep    = 2,
            answer_key           = None,   # ← 改为答案列名以启用完整 MC 评分
        )
        self.method_c = CoTChunkCompressRefiner(
            llm_serving      = self.llm_serving_chunk,
            num_candidates   = 1,          # 1=不做候选搜索（快）；改为 3 启用候选择优
            min_chunk_tokens = 30,
        )
        self.method_d = CoTPatternRefiner(
            llm_serving           = self.llm_serving_greedy,
            min_fragments_to_keep = 2,
        )

        # ── 为每个 method 维护独立的 storage（避免 step 计数互相污染）──
        self._input_file = input_file
        self._storages: dict[str, FileStorage] = {}

    def _get_storage(self, method_tag: str) -> FileStorage:
        """按 method 返回独立 FileStorage，并推进到 step0 等待算子读取。"""
        if method_tag not in self._storages:
            cache_dir = str(CACHE_DIR / method_tag)
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            storage = FileStorage(
                first_entry_file_name = self._input_file,
                cache_path            = cache_dir,
                file_name_prefix      = "dataflow_cache",
                cache_type            = "jsonl",
            )
            storage.step()   # operator_step: -1 → 0，算子 read() 读 step0
            self._storages[method_tag] = storage
        return self._storages[method_tag]

    def forward(self, methods: list[str] | None = None):
        """
        运行指定 method 列表（默认 RUN_METHODS）。
        每个 method 独立跑，互不影响。
        """
        RESULTS_DIR.mkdir(exist_ok=True)
        methods = methods or RUN_METHODS

        ops = {
            "A": (self.method_a, "[Method A] LLM-Judge 步骤级过滤"),
            "B": (self.method_b, "[Method B] Monte Carlo 重要性评分"),
            "C": (self.method_c, "[Method C] Chunk 级别压缩 (R1-Compress 风格)"),
            "D": (self.method_d, "[Method D] Thinking Pattern 分类 (Think Wisely 风格)"),
        }

        for method in methods:
            op, label = ops[method]
            print(f"\n{label}")
            if method == "B":
                print("  提示：当前 answer_key=None，MC 评分会被 skip（无答案列用于验证正确率）。")

            t0 = time.time()
            try:
                storage = self._get_storage(method)
                op.run(
                    storage          = storage,
                    input_key        = "cot",
                    output_key       = "cot_cleaned",
                    output_stats_key = "cot_clean_stats",
                    problem_key      = "problem",
                )

                # 读取结果并打印统计
                out_file = str(CACHE_DIR / method / "dataflow_cache_step1.jsonl")
                result = pd.read_json(out_file, lines=True)
                _print_stats(method, result)
                _save_results(method, result)
                print(f"  ⏱  耗时 {time.time() - t0:.1f}s")

            except Exception as exc:
                import traceback
                print(f"\n  [ERROR] Method {method} 运行失败: {exc}")
                traceback.print_exc()


# ═══════════════════════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════════════════════

def prepare_input(path: Path, n: int, seed: int) -> str:
    """
    随机抽取 n 条数据，统一字段名（output→cot, instruction+input→problem），
    写入临时 jsonl 并返回路径。
    """
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    random.seed(seed)
    sampled = random.sample(rows, min(n, len(rows)))
    df = pd.DataFrame(sampled)

    if "output" in df.columns:
        df = df.rename(columns={"output": "cot"})
    if "instruction" in df.columns and "input" in df.columns:
        df["problem"] = (df["instruction"].fillna("").str.strip()
                         + "\n"
                         + df["input"].fillna("").str.strip())
    elif "input" in df.columns:
        df["problem"] = df["input"]
    else:
        df["problem"] = ""

    CACHE_DIR.mkdir(exist_ok=True)
    input_file = str(CACHE_DIR / "input.jsonl")
    df.to_json(input_file, orient="records", lines=True, force_ascii=False)
    print(f"  已抽取 {len(df)} 条，CoT 平均字符数: {int(df['cot'].str.len().mean()):,}")
    return input_file


def _print_stats(method: str, df: pd.DataFrame):
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


def _save_results(method: str, df: pd.DataFrame):
    out = RESULTS_DIR / f"method_{method}_results.jsonl"
    df.to_json(str(out), orient="records", lines=True, force_ascii=False)
    print(f"  → 已保存: {out}")


# ═══════════════════════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════════════════════

def main():
    # ── 检查 API Key ──
    if not os.environ.get(API_KEY_VAR):
        print(f"\n[错误] 未找到环境变量 {API_KEY_VAR}！")
        print("请先在终端执行以下命令，然后重新运行本脚本：\n")
        print(f"  export {API_KEY_VAR}='your-api-key-here'")
        print(f"  export DF_API_URL='https://your-endpoint/v1/chat/completions'")
        print(f"  export DF_MODEL_NAME='your-model-name'")
        print()
        import sys; sys.exit(1)

    print("=" * 62)
    print("  CoT 清洗算子实验 Pipeline")
    print("=" * 62)
    print(f"  数据集    : {DATA_PATH.name}  ({DATA_PATH.stat().st_size // 1024 // 1024} MB)")
    print(f"  抽样数    : {N_SAMPLES} 条（随机种子 {RANDOM_SEED}）")
    print(f"  API       : {API_URL}")
    print(f"  模型      : {MODEL_NAME}")
    print(f"  并发线程  : {MAX_WORKERS}")
    print(f"  方案      : {', '.join(RUN_METHODS)}")
    print("=" * 62)

    print("\n加载数据...")
    input_file = prepare_input(DATA_PATH, N_SAMPLES, RANDOM_SEED)

    pipeline = CoTCleanPipeline(input_file=input_file)
    pipeline.forward(methods=RUN_METHODS)

    print("\n" + "=" * 62)
    print("  实验完成！结果保存在 results/ 目录。")
    print("=" * 62)


if __name__ == "__main__":
    main()
