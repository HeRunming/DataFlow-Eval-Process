# CoT 清洗算子 Case Study Report
> 数据集：`dataflow_reasoningmath_10k.jsonl`（随机抽取 20 条，seed=42）
> 模型：gpt-5.1，API: http://123.129.219.111:3000
> 时间：2026-03-30

---

## 一、总体压缩统计

| Method | 描述 | 平均保留率 | 平均压缩率 | 有效条数 | 耗时 |
|--------|------|-----------|-----------|---------|------|
| **A** | LLM-Judge 步骤级过滤 | 58.8% | **41.2%** | 20/20 | ~417s |
| **B** | Monte Carlo 重要性评分 | — | — | 0/20 (answer_key=None，skip) | — |
| **C** | Chunk 级别分类 + 差异化压缩 | 69.6% | **30.4%** | 20/20 | ~1989s |
| **D** | Thinking Pattern 九分类处理 | 91.2% | **8.8%** | 20/20 | ~502s |

**原始平均字符数**：29,432 chars（含 `<think>` 标签内容）

---

## 二、Case Study：三条典型样本深度对比

选取三条样本，分别代表短（~14k chars）、中（~32k chars）、长（~53k chars）三种 CoT 长度，逐一分析四种方法的处理策略与效果。

---

### Case 1：短 CoT — 等比数列问题（Row 9）

**原题摘要**：给定单调递增等比数列 `{aₙ}`，满足 `a₂+a₃+a₄=28` 且 `a₃+2` 是 `a₂,a₄` 的等差中项，求通项公式及参数 m 的范围。

**原始 CoT 长度**：13,725 chars，161 个逻辑片段

#### 各方法处理结果

| Method | 输出 chars | 压缩率 | 关键统计 |
|--------|-----------|--------|---------|
| 原始 | 13,725 | — | 161 steps |
| **A** | 11,145 | **19%** | 保留 114 / 删除 29 / 压缩 18 steps |
| **C** | 12,339 | **10%** | 17 chunks → 16 chunks（1 transition 删除）|
| **D** | 13,725 | **0%** | 161 fragments 全保留（131 keep / 30 compress，但字数未变） |

#### 样本对比（冒头段落）

**原始**：
```
Okay, let's try to solve this problem. It's about a geometric sequence that's
monotonically increasing. The problem has two parts. Let's tackle part (1) first.

First, the sequence is geometric, so all terms can be written in terms of the
first term a₁ and the common ratio r. Since it's monotonically increasing and
geometric, the common ratio r must be greater than 1...
```

**Method A**（去掉了"Okay, let's try to solve this problem"这类 preamble，合并了开场分析）：
```
Since the sequence is geometric and monotonically increasing, all terms can be
written in terms of the first term a₁ and common ratio r, and r must satisfy
r > 1 because r between 0 and 1 would make the sequence decreasing and r = 1
would make it constant.
```

**Method C**（保留叙述风格，但删除了 transition chunk，用 LaTeX 公式重新排版）：
```
The sequence is geometric, so each term can be written in terms of the first
term a₁ and common ratio r. Since it is monotonically increasing and geometric,
the common ratio must satisfy r > 1...
Given a₂ + a₃ + a₄ = 28. Write these terms as a₂ = a₁r, a₃ = a₁r², a₄ = a₁r³. Then
a₁r + a₁r² + a₁r³ = 28
⟹ a₁r(1 + r + r²) = 28.   (1)
```

**Method D**（基本原文保留，仅将 31 处 compress 类片段轻微改写，但字数几乎不变）：
```
Let's solve part (1) of this geometric sequence problem first.
First, the sequence is geometric, so all terms can be written in terms of the
first term a₁ and the common ratio r...
（几乎与原文一致）
```

#### 小结

- **A** 在短 CoT 上表现最激进（去掉 preamble 和 redundant 开场），压缩 19%，语义连贯性好。
- **C** 以 chunk 视角整理排版，引入 LaTeX 公式对齐，输出更工整，但压缩率偏低（10%），因为短文本 chunk 多数被判为 core。
- **D** 在此例几乎无效：压缩率 0%。161 个 fragment 中 PREAMBLE(4)+TRANSITION(25) 共 29 个本应 compress，但 LLM 生成的压缩版本字数与原文持平，没有实质缩减。

---

### Case 2：中等 CoT — 抛物线问题（Row 14）

**原题摘要**：抛物线 `y=ax²+bx+c` 与 `y=2x²` 形状相同，过点 P(1,3)，与直线 `y=x+1` 相切于点 Q，求 (a) 抛物线方程、(b) Q 点坐标及围成面积。

**原始 CoT 长度**：32,143 chars，224 个逻辑片段，含大量试错、验算与重算

#### 各方法处理结果

| Method | 输出 chars | 压缩率 | 关键统计 |
|--------|-----------|--------|---------|
| 原始 | 32,143 | — | 224 steps，含多次重算 |
| **A** | 14,280 | **56%** | 保留 147 / 删除 57 / 压缩 20 steps |
| **C** | 22,022 | **32%** | 54 chunks：9 core / 36 exploration / 7 verification / 2 transition |
| **D** | 24,972 | **22%** | 保留 156 / 压缩 68 / 删除 0 fragments |

#### 深度分析

此题是最能体现方法差异的案例，因为原始 CoT 含有**大量重复验算**（反复代入验证 a=2/-2 两种情形）和**无效探索**（尝试错误的切线条件后放弃）。

**Method A 的激进删除（压缩 56%）**：

A 判断每个 step 是否"必要"（necessary / redundant / compressible），对此题的大量重复验算毫不留情地删掉，最终保留约一半。但存在一个风险：Result 中末尾出现"Let me check with a=2, b=-3+2√2..."，说明 A 保留了部分验算片段但截断了结论，**输出末尾不完整**，整体逻辑链有些断裂。

**Method C 的 chunk 分类（压缩 32%）**：

54 个 chunk 中 36 个被判为 exploration（反复尝试的路径），被以 60% 比例压缩。2 个 transition chunk 直接删除。输出末尾保留了完整的问题讨论结论（"since tangency gives only one intersection point, the enclosed area would be zero..."），**逻辑完整**。缺点是 exploration chunk 压缩不够彻底，保留率仍有 69%。

**Method D 的保守处理（压缩 22%）**：

片段分类统计：CORE(51) + COMPUTATION(69) + NECESSARY_VERIFICATION(24) + NECESSARY_EXPLORATION(19) + CONCLUSION(12) + PREAMBLE(8) + TRANSITION(39) + REDUNDANT_VERIFICATION(2)。值得注意的是：
- 删除数 = 0：D 对此题没有删除任何内容，全靠"compress"来缩减
- UNNECESSARY_EXPLORATION = 0：D 认为所有探索路径都是"必要探索"，未删除任何死路

输出末尾同样保留了完整分析，**逻辑连贯性最好**，但压缩效果最弱。

---

### Case 3：长 CoT — 区间集合证明题（Row 1）

**原题摘要**：给定两组区间集合 A（含 2m-1 个区间，两两有公共内点）和 B（每个 A 区间内至少含两个不相交 B 区间），证明存在某 B 区间被至少 m 个 A 区间包含。

**原始 CoT 长度**：52,834 chars，244 个逻辑片段，含大量不同证明路径的探索

#### 各方法处理结果

| Method | 输出 chars | 压缩率 | 关键统计 |
|--------|-----------|--------|---------|
| 原始 | 52,834 | — | 244 steps，多次尝试不同证明策略 |
| **A** | 26,843 | **49%** | 保留 68 / 删除 110 / 压缩 66 steps |
| **C** | 46,317 | **12%** | 139 chunks：28 core / 106 exploration / 0 verification / 5 transition |
| **D** | 48,775 | **8%** | 保留 158 / 压缩 83 / 删除 3 fragments（3 UNNECESSARY_EXPLORATION）|

#### 深度分析

此题是数学竞赛风格的证明题，原始 CoT 包含多条证明路线的反复尝试——先尝试直接计数，后尝试 pigeonhole，最终收敛于正确证明。

**Method A（压缩 49%）**：

大胆删除 110 个 step（占总数 45%），但末尾出现"If an A_i has both B intervals outside the intersection region, those B intervals fall in [left_i, left_max)..."——**证明的最后收尾步骤被截断**，结论不完整。这是 step 级别过滤最大的风险：在长 CoT 中，若多个证明路径交织，单步判断"是否冗余"容易误删关键转折步。

**Method C（压缩 12%）**：

139 个 chunk 中 106 个（76%）被分类为 exploration——这与此题的特点高度吻合（大量探索性推理），说明 C 的 chunk 分类器对证明题感知准确。但 exploration 类的压缩目标是 60%，实际压缩效果不理想，最终只去掉 12%。末尾结论完整：

> "Therefore, to keep the total number of B intervals as small as possible, the same B intervals must be reused within the common intersection segment [left_max, c]. This forces at least one B interval to lie in that overlap and be contained in every A interval."

**结论清晰完整**，是三种方法中对此题处理质量最好的。

**Method D（压缩 8%）**：

分类结果丰富：CORE(111) + NECESSARY_EXPLORATION(35) + TRANSITION(33) + NECESSARY_VERIFICATION(22) + COMPUTATION(21) + PREAMBLE(14) + CONCLUSION(4) + REDUNDANT_VERIFICATION(1) + **UNNECESSARY_EXPLORATION(3)**。

D 对此题删除了 3 个 UNNECESSARY_EXPLORATION 片段，是全部样本中唯一真正触发"删除"动作的场景。末尾结论也完整，逻辑连贯，但整体压缩率仅 8%。

---

## 三、方法横向比较总结

### 3.1 压缩效果

```
Method A  ████████████████████████  41.2%（最激进）
Method C  ████████████████          30.4%
Method D  █████                      8.8%（最保守）
```

### 3.2 各方法核心策略与适用场景

| 维度 | Method A（LLM-Judge 步骤过滤）| Method C（Chunk 分类压缩）| Method D（Thinking Pattern 九分类）|
|------|-------------------------------|---------------------------|-------------------------------------|
| **粒度** | step（单行/句子级别）| chunk（段落/语义块级别）| fragment（自然段/双换行级别）|
| **删除策略** | 激进，redundant step 直接删 | 仅删 transition chunk | 仅删 UNNECESSARY_EXPLORATION |
| **压缩策略** | 对 compressible step 单句总结 | 按类型设定目标比例（30%~85%）| 对 compress 类 fragment 改写 |
| **逻辑完整性** | ⚠️ 长 CoT 有断尾风险 | ✅ chunk 保持语义块完整 | ✅ fragment 完整，删除极保守 |
| **格式优化** | ❌ 保持原文格式 | ✅ 会引入 LaTeX 公式排版 | ❌ 接近原文格式 |
| **擅长场景** | 短~中 CoT，含明显冗余重复 | 中~长 CoT，多探索路径 | 任意长度，需要细粒度语义分析 |
| **不擅长场景** | 长证明题（断尾风险）| 短 CoT（chunk 数少，core 比例高）| 压缩率偏低，不适合激进压缩需求 |
| **LLM 调用次数** | O(N_steps × 2) | O(N_chunks × 2) | O(N_frags × 2)，分 classify + compress 两轮 |
| **速度（20条）** | ~417s | ~1989s（因串行 per-row）| ~502s |

### 3.3 输出质量维度

| 维度 | Method A | Method C | Method D |
|------|---------|---------|---------|
| 数学正确性 | ✅（保留了关键计算步骤）| ✅ | ✅ |
| 论证完整性 | ⚠️ 长 CoT 有截断风险 | ✅ | ✅ |
| 可读性 | 中（流水步骤风格）| 高（公式对齐，段落清晰）| 高（接近原文）|
| 压缩激进度 | 高 | 中 | 低 |
| 探索路径保留 | 少 | 部分（按类型压缩）| 多（精细区分必要/不必要）|

---

## 四、大规模实验建议

### 4.1 优先验证的核心问题

在扩展到大规模实验之前，建议先通过小批量（100~500条）验证以下问题：

**Q1：压缩后的 CoT 是否影响下游模型的推理准确率？**
- Method A 删除最多，理论上若删掉的都是真正冗余，准确率不受影响甚至提升
- 参考文献（Think Wisely）指出删除 UNNECESSARY_EXPLORATION 可能提升准确率
- **建议**：对 Method A/D 输出用另一个强模型验证答案，与原始 CoT 做对比

**Q2：Method C 的耗时问题是否可优化？**
- 20条耗时 1989s，是 A（417s）的 4.8 倍，是 D（502s）的 4 倍
- 主因：Method C 当前是**逐行串行处理**，每行内部 chunk 才并行
- **建议**：在 `run()` 层也并行化（多行同时处理），可大幅提速

**Q3：Method B 在有标准答案时的效果如何？**
- 本次实验因数据集无独立 answer 列而全部 skip
- **建议**：找一个含 answer 列的数据集（如 MATH、GSM8K CoT 数据）重新测试 Method B

### 4.2 大规模实验参数推荐配置

```python
# 激进压缩任务（最大化压缩率，可接受轻微质量损失）
method = "A"
min_steps_to_keep = 3       # 适当提高，避免过度删除
max_workers = 200

# 均衡压缩任务（保持较好质量，适度压缩）
method = "C"
num_candidates = 1          # 不做候选搜索（快）
min_chunk_tokens = 50       # 适当提高，避免过细切分

# 精细分析任务（最高质量，适合对训练数据质量要求极高的场景）
method = "D"
action_overrides = {"REDUNDANT_VERIFICATION": "delete"}  # 可适当加大删除力度
min_fragments_to_keep = 3
```

### 4.3 数据集选择建议

| 数据集类型 | 推荐方法 | 理由 |
|-----------|---------|------|
| 数学计算题（短 CoT，<15k chars）| A 或 C | 冗余重复多，适合激进压缩 |
| 数学证明题（长 CoT，>30k chars）| C 或 D | 需保留探索路径逻辑结构 |
| 带标准答案的数据集 | B（完整配置）| 可用 MC 采样验证 step 重要性 |
| 混合难度数据集 | A + D 组合 | A 做粗过滤，D 做精细分类 |

### 4.4 评估指标建议

除字符/token 压缩率外，大规模实验应增加以下指标：

1. **答案正确率**：用 math-verify 或另一个 LLM 验证压缩后 CoT 导出的答案是否正确
2. **关键步骤保留率**：人工标注一批 "关键步骤"，检查各方法的保留情况
3. **下游训练效果**：将压缩后 CoT 数据微调一个小模型，对比与原始数据的差异
4. **压缩一致性**：对同一题目多次运行，检查输出稳定性（Method A 用 temperature=0 稳定，C/D 可能有波动）

---

## 五、当前已知问题与限制

| 问题 | 影响 | 建议处理 |
|------|------|---------|
| Method A 长 CoT 断尾风险 | 末尾结论可能被截断 | 增加"结尾保护"：末尾 N 个 step 强制 keep |
| Method C 串行处理慢 | 大规模实验成本高 | 在 `run()` 层增加多行并行 |
| Method D compress 后字数不减 | 短 CoT 压缩率接近 0% | 在 compress prompt 中加字数上限约束 |
| Method B answer_key=None 全 skip | 无法测试 MC 评分效果 | 补充含答案列的测试数据集 |
| API 不稳定（502/connection aborted）| 增加重试次数和耗时 | 在实验侧加全局重试逻辑，或切换更稳定的 API |

---

*生成时间：2026-03-30 | 基于 20 条 dataflow_reasoningmath_10k.jsonl 数据*
