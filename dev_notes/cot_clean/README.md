# Long-CoT 清洗：A / C / D 三法总览

> 把 10,000 条 DeepSeek-R1 风格的数学推理轨迹压缩成 Qwen3 可直接 SFT 的数据。
> 同一输入、同一模型（Taiji / Gemini-3.1-flash-lite，thinking=high），三条流水线并行跑 60 批，0 错误。

本目录是 `dataflow/operators/reasoning/refine/` 里三个 CoT 清洗算子 `*_fast.py` 的配套说明、运行脚本与示例输出。算子源码里顶部 docstring 已经写清了各自改动点，这里做一页总览。

## 目录

```
dev_notes/cot_clean/
├── README.md                  # 本文件
├── scripts/
│   ├── run_full_10k.py        # 10k 数据串行跑 A/C/D，带 500 行 checkpoint 与 resume
│   ├── run_fast_ab_validation.py  # A/B 验证矩阵，对比 old/fast 及参数组
│   ├── probe_full_status.py   # 实时读 manifest + tqdm 尾行估算剩余时间
│   └── make_alpaca.py         # 清洗输出 → Qwen3-native Alpaca (<think>…</think>\n\n<final>)
└── sample_outputs/            # 10k 跑完后各方法前 20 行（Alpaca 格式）
    ├── method_A_sample20.jsonl
    ├── method_C_sample20.jsonl
    ├── method_D_sample20.jsonl
    └── method_raw_sample20.jsonl
```

完整 10k 结果与 cache 留在 `/data/workspace/cc_workspace/cot_clean_test/results_full/`，没有搬进仓库。

## 算子位置

| 方法 | Fast 算子 | 慢参考版 | Prompt |
|------|-----------|----------|--------|
| **A** | `dataflow/operators/reasoning/refine/cot_llm_judge_refiner_fast.py` → `CoTLLMJudgeRefinerFast` | `cot_llm_judge_refiner.py` | `dataflow/prompts/reasoning/cot_clean.py` → `CoTStepJudgeCompressPrompt` |
| **C** | `cot_chunk_compress_refiner_fast.py` → `CoTChunkCompressRefinerFast` | `cot_chunk_compress_refiner.py` | `CoTChunkClassifyPrompt`, `CoTChunkRefinePrompt` |
| **D** | `cot_pattern_refiner_fast.py` → `CoTPatternRefinerFast` | `cot_pattern_refiner.py` | `CoTPatternClassifyCompressPrompt` |

Serving（都在 `dataflow/serving/`）：
- `taiji_custom_llm_serving.py` / `taiji_custom_llm_serving_pool.py`：Taiji 定制 HMAC-SHA1 协议，池化版带持久 ThreadPoolExecutor
- `api_llm_serving_pool.py`：通用 OpenAI 协议的池化封装

## 三个方法在做什么

### Method A — Judge-and-Compress（步骤级激进压缩）

**思路**：把 CoT 切成 reasoning step，每步请 LLM 打一个三选一的标签：
- `necessary` → 原样保留
- `redundant` → 直接删除
- `compressible` → 一句话重写（判分和压缩合并成一次 JSON 调用）

**关键参数**：
- `min_step_chars=400`：相邻 `\n\n` 切出的小段若 < 400 字符与下一段合并，防止 R1 把一行文字也切成独立 step
- `min_chars_to_clean=2000`：低于阈值的短 CoT 整体跳过
- `max_workers=100`：Taiji 定制协议的最大并发

**风格**：产出最短、最公式化，适合显存紧、学生模型小的蒸馏场景。

### Method C — Chunk Rewrite（段落块平衡重写）

**思路**：按转折词（wait / actually / hmm / let me reconsider …）切 chunk，分类为：
- `core`：推理主干，保留
- `exploration`：尝试性推导，压缩
- `verification`：结果核对，按需保留或压缩
- `transition`：纯过渡/情绪，**直接丢弃，不发起 LLM 调用**

**关键参数**：
- `num_candidates`：> 1 时每个 chunk 产多个候选，取最短一版（flattened 正确实现，慢版在多候选分支不可用）
- 动作表基于 chunk 类型路由到不同的重写 prompt（定向重写而非通用压缩）

**风格**：语言最自然，适合 chat-style SFT。

### Method D — Pattern-Aware + 多样性采样（最保守，保多样性）

**思路**：把 CoT 切成更细粒度的 fragment，用 9 类细标签分类：
- 保留类：`CORE_REASONING`、`NECESSARY_VERIFICATION`、`COMPUTATION`、`CONCLUSION`
- 压缩类：`NECESSARY_EXPLORATION`、`REDUNDANT_VERIFICATION`、`PREAMBLE`、`TRANSITION`
- 关键类：`UNNECESSARY_EXPLORATION` —— **一半原样保留 / 一半压成 2-3 句失败轨迹摘要**

**多样性采样（本次新意）**：
- 每个 UNN_EXPL 片段用 `MD5(row_idx, frag_idx, seed=0xC07C)` 决定保留还是压缩，确定性复现
- `unn_expl_keep_ratio=0.5` 控制保留比例
- 压缩 prompt 强制包含具体方程 + 具体放弃原因，明令禁止"Considered X, but abandoned Y"模板话术
- 效果：模板化 "Considered…abandoned" 从 107 降到 6 次

**三种预设**（`preset=` 参数）：
- `aggressive`：UNN_EXPL → delete（pre-v2 行为，纯蒸馏场景用）
- `balanced`（默认）：UNN_EXPL → compress + 采样保留，推理能力 SFT 用
- `conservative`：只删 TRANSITION，其它全保/压

**风格**：产出最长、最接近 R1 原生气口，保留了"尝试-失败-换路"的推理信号。

## 基础设施

三法共享：

- **Flattened batching**：把整个 dataframe 的 prompt 打平到 1-2 次 LLM 调用。旧版是 `for row in df: for step in row.steps: generate(...)` 这样 10k × N 的串行 barrier，Fast 版把所有行的 prompt 串成一个 flat batch 再用持久 ThreadPool 并发。
- **Merged judge+compress**（A 和 D）：一次 JSON 出分类 + 重写，省掉约一半的 round-trip。
- **持久化 ThreadPoolExecutor + 重试**：`api_llm_serving_pool.py` / `taiji_custom_llm_serving_pool.py`，连接错误自动 retry，最多 `max_workers=100`。
- **短 CoT 跳过**：`min_chars_to_clean=2000` 阈值下的整行直接放行。
- **Step/Chunk 粗化**：相邻小段合并到目标字符数，避免 R1 的 `\n\n` 风格炸出几百个独立 step。
- **Silent-fallback 检测**：如果一整行全部 fragment 被打成 `CORE_REASONING`（典型的 LLM 异常被吞的症状），记 warning。先前 D_old 有 8/30 行中招。

## 性能（10,000 行跑完）

| 指标 | RAW | **A** | **C** | **D** |
|------|----:|------:|------:|------:|
| 平均字符数 | 27,840 | **10,382** | **14,557** | **23,771** |
| 保留率 | 100% | **34.6%** | **49.2%** | **85.6%** |
| 墙钟 (h) | — | 12.08 | 19.41 | 12.12 |
| 批次成功率 | — | 20/20 | 20/20 | 20/20 |
| 错误数 | — | 0 | 0 | 0 |

合计 43.6 小时 / 30,000 行次，单配额跑完，60/60 批全绿。

**单行加速**（fast vs slow 在同一 10 行样本上）：
- A：52.5s → 6.6s，**7.91×**
- D：52.5s → 6.6s（含采样和合并 JSON），**4.25×**
- C：比例类似，慢版多候选分支不可用作参考，实测 flatten 后吞吐 ×5-10

## 各方法内部统计

**Method A（按 step 裁）**
84.2% 压缩、9.7% 原样保留、6.1% 删除

**Method C（按 chunk 分类）**
exploration 46.7% / core 37.5% / transition 9.6% / verification 6.2%

**Method D（9 类 pattern 分布）**
CORE_REASONING 32.1% / UNN_EXPL 17.8% / NEC_EXPL 17.3% / COMPUTATION 17.3% / NEC_VERIF 9.7% / PREAMBLE 3.3% / CONCLUSION 2.0% / RED_VERIF 0.3% / TRANSITION 0.2%

UNN_EXPL 采样实测：**42,412 原样 / 41,951 压缩（保留率 50.3%，对齐 50% 目标）**

## 产出物格式

Qwen3-native Alpaca（`instruction=""`，`input=题面`，`output=<think>清洗后 CoT</think>\n\n<最终答案>`）：

- `method_A_10k_alpaca.jsonl`：最短、最激进
- `method_C_10k_alpaca.jsonl`：最自然、平衡
- `method_D_10k_alpaca.jsonl`：最保守、多样性最好
- `method_raw_10k_alpaca.jsonl`：未清洗基线，格式对齐用

示例见 `sample_outputs/`。完整 10k 在 `/data/workspace/cc_workspace/cot_clean_test/results_full/alpaca/`。

## 怎么复现

```bash
# 1. 环境
cd DataFlow
pip install -e .
# Taiji APP_ID / APP_KEY 已硬编码在 taiji_custom_llm_serving*.py 的默认值里，
# 参见 /data/workspace/test.py

# 2. 跑完整 10k（会自动 resume 已完成的 batch）
cd dev_notes/cot_clean/scripts
python run_full_10k.py \
    --input /path/to/dataflow_reasoningmath_10k.jsonl \
    --output-dir ./results_full \
    --methods A C D \
    --batch-size 500 \
    --max-workers 100

# 3. 进度探测（另一个终端）
python probe_full_status.py ./results_full

# 4. 转 Alpaca
python make_alpaca.py \
    --input ./results_full/method_A_10k.jsonl \
    --raw-input /path/to/dataflow_reasoningmath_10k.jsonl \
    --output ./results_full/alpaca/method_A_10k_alpaca.jsonl

# 5. 快速 A/B 验证（跑前先冒烟）
AB_CELLS="A_fast:20,C_fast:20,D_fast:20" python run_fast_ab_validation.py
```

## 怎么选

| 场景 | 推荐 |
|------|------|
| 显存紧、学生模型小、纯蒸馏 | **A** |
| 做 chat-style SFT，要求语言自然 | **C** |
| 做推理能力 SFT，要保留 R1 探索气口 | **D**（本次主力产物） |
| 消融 / baseline | raw |

## 相关文档

- `dev_notes/COT_CLEAN_CASE_STUDY.md`：案例对比（复数根问题等）的详细拆解
- `dev_notes/DATAFLOW_DEV_NOTES.md`：DataFlow 框架本身的开发笔记
- `dev_notes/DATAFLOW_KNOWLEDGE_BASE.md`：LazyLoader / FileStorage / LLMServingABC 等核心抽象
- 在线案例展示：https://longcot-cleaning.pages.woa.com （内网公开）
