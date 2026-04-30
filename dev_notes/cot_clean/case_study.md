# Case Study: A/C/D 数据的系统性体检（10k 全集 + 典型样本精读）

> 背景：Qwen3-8B 在 5e-6 学习率下，baseline 和实验组都训崩。本文从数据一侧做一次彻底体检，目的是判断**"数据本身是否足够健康"**，以及"方法之间的相对风险差"。
>
> 结论先写在前面：**整体是健康的**（格式合规、答案漂移 0、长度合理），但有**三个系统性的坑**值得正视，其中一个坑（A 方法把 `\boxed{}` 连同末尾推理一起压缩掉）对 SFT 的伤害可能不小。

---

## 一、全集量化扫描（10 维度 × 10k 行）

扫描脚本：`dev_notes/cot_clean/scripts/scan_quality.py`
结果：`dev_notes/cot_clean/scan/scan_report.json`

| 维度 | A | C | D | raw | 说明 |
|------|--:|--:|--:|----:|------|
| `len_mean` (字符) | 10,382 | 14,557 | 23,771 | 27,825 | 三者都压缩了 |
| `len_p50` | 9,935 | 13,921 | 23,118 | 27,221 | |
| `len_p99` | 19,852 | 29,189 | 40,068 | 48,890 | |
| `ans_p50` (answer 区) | 1,206 | 1,206 | 1,206 | 1,206 | **完全对齐**（我们没动答案区） |
| `missing_open_think` | 0 | 0 | 0 | 0 | 格式合规 |
| `missing_close_think` | 0 | 0 | 0 | 0 | |
| `missing_final_answer` | 0 | 0 | 0 | 24 | 24 行 raw 答案区本就空（答案藏在 think） |
| `unclosed_boxed` | 1 | 1 | 2 | 3 | 清洗没有引入新 bug；3 行是 raw 固有 |
| `grew_longer_than_raw` | 0 | 0 | 0 | — | 三法都真的在压缩 |
| `shrunk_below_5pct` | 1 | 0 | 0 | — | A 有 1 行被压过头 |
| `max_5gram_repeat` | 124 | 108 | 151 | 180 | 全是"纯数字 5-gram"误报，非真病 |
| `language_mix` | 5 | — | 5 | 7 | 中英混杂，清洗 **没引入新混杂**，raw 原有 |
| `think_ends_midsentence` | 3260 | 1180 | 868 | 1024 | A 高是扫描误报（`\boxed{X}\n` 不在标点白名单），见下 |

### 误报澄清

扫描脚本里两个指标最初显示 A 有大量"截断问题"，精读后确认都是扫描器的误判：

- `think_ends_midsentence`: A 结尾 `...\boxed{0}\n` 时 `last_char='\n'`，被标记"中途截断"。**实际是正常结尾**。
- `max_5gram_repeat`: 重复 top 都是 `('1','1','1','1','1')` / `('3','6','2','4','2')` 之类的纯数字表达式切词，非真重复。

扔掉这两个指标后，下面是**真正需要关心的问题**。

---

## 二、三个系统性问题

### 问题 1 🔴 A 方法把末尾 `\boxed{}` 连同结论一起改写了（19/10k）

**定位**：24 行题目的最终答案藏在 `<think>` 末尾（`\boxed{...}`），raw 的 answer 区为空。
A 方法对这 24 行中的 **19 行** 把末尾 `\boxed` 整个改成了英文散文；D 方法只有 3 行；C 方法 3 行。

**典型案例 — row 543**（椭圆 + 双曲线最大弦长问题，三小问）

RAW 末尾：
```
**Result**:
- Maximum |AB| + |CD| is **\(\boxed{10}\)**.

**Final Answers**:
(a) Maximum |AB| + |CD| values:
- Parabola: \(\boxed{\dfrac{40}{3}}\)
- Horizontal line: \(\boxed{20}\)
- Hyperbola: \(\boxed{10}\)
```

A 末尾：
```
For the parabola, the maximum |AB| + |CD| is 40/3, while for the line y=2,
symmetry yields a maximum |AB|=8 and |CD|=12, totaling 20.

For part (a), the maximum values of |AB| + |CD| are 40/3 for the
parabola, 20 for the horizontal line, and 10 for the hyperbola.
```

**看出来没**：A 把 `\boxed{40/3}`, `\boxed{20}`, `\boxed{10}` 三个全部改写成了散文。**学生 SFT 时会学到两个矛盾的模板**：绝大多数题目要写 `\boxed{...}`，偶尔又不要写。这种格式随机性对下游任务（尤其做 MATH / GSM8K 评测抽答案）是明确有害的。

D 末尾完整保留了原文 `\boxed{10}`。

**另一个案例 — row 2186**（含 `R(t)=0` 两解证明的建模题）

RAW 末尾是印尼语垃圾链接（数据源污染，R1 幻觉产出的 footer）：
```
ancur%20produsen%20di%20korea.md) ...
```

A：把整段结论改写为"For part (II), the equation R(t) = 0 has exactly two positive solutions, which can be proven by analyzing the function's convexity and using the Intermediate Value Theorem."（**完全丢失 boxed**，只剩英文描述）

D：**原样保留了垃圾链接段**（保持了也丢失了答案，但至少 think 体内还留着 `\boxed{y = 1/e - 1}` 等表达式）

**教训**：
- 数据源本身 26/10k 有"答案藏在 think 里"的布局，A 的激进 step 压缩对这种布局极不友好；
- raw 还有 R1 幻觉 footer 类污染，我们**在清洗前没做源数据过滤**。

**修复建议**（给下一轮实验）：
1. 在 `make_alpaca.py` 的 answer 提取阶段，若 answer 区为空就**强制从 think 末尾把最后一个合法 `\boxed{...}` 挪到 answer 区**（脚本里已有 `_extract_tail_answer` 但似乎没覆盖全部情况）；
2. A 的 prompt 里明确加上："末尾的 `\boxed{}`、`**Final Answer**`、`Hence, ...` 这类结论性句子必须原样保留"；
3. 过滤 raw 里带 `.md)` / `%20` / 大段 URL 的行（R1 幻觉 footer）。

### 问题 2 🟡 D 方法答案区偶有漂移（2/10k）

Q3 发现的 row 6357（raw answer `C`，D 清洗后 None）和 row 7374（raw None，D 清洗后 `(d) \; 32.4\%`）。这是 **`make_alpaca.py` 的尾部 `\boxed{}` 抽取器在边界情况下的不一致**，不是 D 算子本身的问题。

数量小（2/10k），不会影响训练，但应当在下一版 `make_alpaca.py` 里修。

### 问题 3 🟢 raw 固有的语言混杂和污染，清洗没有放大

- `language_mix`: A=5, D=5, raw=7（D 和 A 把中英混杂的内容尽量改成一种语言，所以数量还**低于** raw）
- `unclosed_boxed`: raw=3, A=1, C=1, D=2（三种方法都没引入新 bug）

这说明清洗**过滤/覆盖了**一部分源数据问题，是加分项。

---

## 三、方法间定性对比（精读典型行）

选 3 个有代表性的行，把 A/C/D/raw 四版同时排在一起看。

### Case A. row 0 — 复数二次方程根（多 parts 推导）

*题面*：$z^2+(4-2i)z+(7+bi)=0$，`b` 实参，讨论 $|z_1|, |z_2|, z_1 \bar z_2$ 以及恒等式。

| 版本 | 字符数 | 末段风格 |
|------|-------:|----------|
| RAW  | ~33k | `"Okay, let's tackle this problem step by step. Starting with part (i)..."` — R1 的典型 meta-thinking |
| A    | ~9k  | `"To solve z²+(4−2i)z+(7+bi)=0, apply the quadratic formula with a=1, b=4−2i, c=7+bi."` — 极简、公式化 |
| C    | ~12k | `"Discriminant D = (4 - 2i)² - 4(7 + bi). Expanding (4 - 2i)² gives 16 - 16i - 4 = 12 - 16i..."` — 自然语言、保留推导步骤 |
| D    | ~28k | 基本保留 R1 原话，仅删冗余重复和空过渡 |

观感：**C 可读性最好**，A 信息密度最高但像"公式题解"不像"思考"，D 最接近 R1 原汁原味。

### Case B. row 543 — 椭圆双曲线三问（见问题 1 详述）

核心差异：A 把三个 `\boxed{40/3, 20, 10}` 写成了散文，D 保留。

### Case C. UNN_EXPL 采样在 D 里的体现（行 3777, 8218 等）

D 的 9 类 pattern 分类 + 50/50 MD5 采样的预期是："一半原样保留失败探索，另一半压成 2-3 句摘要"。实测：

- `UNN_EXPL` 保留率 **50.3%**（42,412 原样 / 41,951 压缩），符合采样目标
- 模板化短语 "Considered X, but abandoned Y" 从 v2 prompt 的 107 次降到 v3 的 6 次（本次 `tpl_CONSIDERED_ABANDONED` D=61 / 10k，相对 83k fragment 仅 0.07%）
- `tpl_TRIED_BUT_FAILED`: D=154 / C=808 / A=136 —— **C 反而是重灾区**。C 的 exploration chunk 重写 prompt 没有反模板化约束

**C 的模板化**是个次级问题：`"I tried X but ... did not work"` 重复出现 808 次（8.1% of rows）。这会让 SFT 学生学到"做题时要先 perform an attempt, then say it failed"的说话腔。

---

## 四、与"第一次 SFT 训崩"的归因判断

重要！**训崩最可能不是数据的问题**，因为：

1. 答案区完全对齐（A/C/D answer 与 raw 答案只差 24 行，那 24 行是 raw 本就空）
2. 格式合规（`<think>`/`</think>` 100%，`<answer>` 被正确剥离）
3. 长度合理（p99 均在 50k 字符以内，Qwen3-8B 32k context 完全兜得住）
4. 没有大面积截断、没有大面积重复、没有大面积答案丢失

**但数据有 2 个非零风险项**，对应的训练症状长这样：

| 风险 | 数量 | 可能的训练症状 |
|------|-----:|----------------|
| A 末尾 \boxed 改写 | 19/10k (0.19%) | Eval 时抽不到答案，MATH/GSM8K 分数看似没学到 |
| C 的 "tried ... failed" 模板腔 | 808/10k (8.1%) | 模型输出变得冗长且重复套路，长度偏长 |

如果 baseline（raw）也崩了，说明更大概率是**训练侧超参**的问题：

- **5e-6 对 Qwen3-8B 的 long-CoT SFT 是否太大**？Qwen 官方 cookbook 通常给 1e-5 ~ 2e-5 用于 lora SFT、1e-6 ~ 5e-6 用于全参 math SFT；但 long-CoT 数据方差大，**packing / answer-only loss mask / warmup** 任意一个缺失都可能让 loss 在头几百 step 内爆
- **Qwen3-8B base 还是 thinking 模型？** 如果是 thinking 模型，SFT 对 `<think>` 格式的容忍度与 base 不同
- **Chat template 是否对齐 Qwen3 官方**？我们现在是 `<think>\n...\n</think>\n\n<answer>` 格式，官方 `tokenizer.apply_chat_template` 里的 thinking mode 是否相符需要复查
- **Packing？** 长度 p99 4 万字符 ≈ ~15k token，一条就超普通 8k context，必须 packing 或 truncation

> 详见同目录 `related_work.md`（由背景 agent 产出），里面会有社区在 5e-6 训 long-CoT 的常见坑的文献佐证。

---

## 五、下一轮修复清单（按优先级）

**P0（上训练前必做）**
1. 修 `make_alpaca.py`：answer 区为空时强制从 think 末尾挪 `\boxed{}`（24 行都修，A 受益最大）
2. 确认训练侧超参：学习率（建议先试 1e-6 + 1% warmup）、batch size、packing、answer-only loss mask 是否已开
3. 确认 chat template 与 Qwen3 官方一致

**P1（质量优化）**
4. A 方法 prompt 里加"末尾结论性步骤（含 `\boxed`/`Final Answer`）原样保留"规则
5. C 方法 prompt 里加"禁止 'I tried X but ... did not work' 模板话术"
6. 过滤 raw 里的 URL / `.md)` footer 污染（数据源层）

**P2（可做可不做）**
7. 把 `unclosed_boxed` 那 2-3 行直接 drop（数量小）
8. 修 `make_alpaca.py` 尾部答案抽取的两处边界 bug（row 6357, 7374）

---

## 六、关键数字速览

- **10k / 10k** 格式合规（`<think>`, 空行分隔, 无 `<answer>` tag）
- **0 / 10k** 答案硬漂移（A/C/D answer 与 raw answer 100% 对齐，除 24 行本就空）
- **19 / 24** 是 A 方法在 raw 答案藏 think 的特殊行里丢失 `\boxed`；D 对应是 3 / 24
- **808 / 10k** 是 C 方法的 "tried … failed" 模板腔
- **50.3 / 49.7** 是 D 方法 UNN_EXPL 采样保留 / 压缩的实测比例
- **压缩比** A=37.3% / C=52.3% / D=85.4% （目标 ~1/3, ~1/2, ~85%，全对）

---

## 附：复现本报告所用脚本

```bash
# 1. 全集 10 维度扫描
python dev_notes/cot_clean/scripts/scan_quality.py \
    --dir /data/workspace/cc_workspace/cot_clean_test/results_full/alpaca \
    --out dev_notes/cot_clean/scan

# 2. 异常钻取
python dev_notes/cot_clean/scripts/drill_anomalies.py

# 3. 答案损失专项（Q1-Q4）
python dev_notes/cot_clean/scripts/drill_answer_loss.py
```
