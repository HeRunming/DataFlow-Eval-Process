# Long-CoT 压缩 / 蒸馏 相关工作调研

> 背景：我们把 DeepSeek-R1 风格的长推理轨迹（平均 27,840 字符）用 3 种方法压缩成 SFT 数据给 Qwen3-8B 学习。  
> Method A（Judge-and-Compress，压到 34.6%）、Method C（Chunk Rewrite，压到 49.2%）、Method D（Pattern-Aware + 多样性采样，压到 85.6%）。  
> 输出格式：Qwen3-native Alpaca，`<think>压缩后 CoT</think>\n\n<答案>`。  
> 初次 SFT 在 **lr = 5e-6** 下 baseline 和三组实验都训崩。本文档用于核对「方法是否漏了已知陷阱」以及「训崩是否和压缩本身有关」。

调研日期：2026-04-30。  
检索原则：优先 arXiv 主条目、官方仓库/技术报告；不引用营销博客。

---

## 1. TokenSkip: Controllable CoT Compression (Xia et al., 2025)

- **论文**：*TokenSkip: Controllable Chain-of-Thought Compression in LLMs*，arXiv:2502.12067（PolyU + NUS）。
- **核心做法**：在 CoT sequence 上做 **token-level 重要性打分**（基于 LLMLingua-2 类似的语义贡献），按可调比例直接删除次要 token，再用 LoRA 做短-CoT 的 continued training，让模型学会「内化」删除策略。
- **关键陷阱 / 发现**：
  - 低压缩比时 token 删除对准确率影响极小；**越接近 "0.5× 压缩" 越接近 performance cliff**。Qwen2.5-14B-Instruct GSM8K 上 313→181 token（~40% 压缩）掉 <0.4 pp。
  - 过度压缩时模型出现 "跳步" 和错误归因，和我们 Method A（34.6%）的激进比例高度相关。
- **与我们方法对照**：
  - 更好：我们在 step/chunk 粒度做语义改写，保留逻辑连词；TokenSkip 裸删 token 会破坏语法流。
  - 更差：TokenSkip 有**可调压缩比超参数**（训练数据里混合不同压缩度），我们是一刀切。
  - 他们有而我们没有：**同一 query 对应多个压缩比**的混合训练，这是防止学坏的标配。
- **可引用结论**：TokenSkip on Qwen2.5-14B-Instruct GSM8K：313 → 181 tokens (−42%)，accuracy 掉 <0.4 pp。超过 ~50% 压缩开始掉点。

---

## 2. CoT-Valve: Length-Compressible CoT Tuning (Ma et al., 2025, NUS)

- **论文**：arXiv:2502.09601。
- **核心做法**：发现 "参数空间存在一个方向，沿它移动可以控制 CoT 长度"。构造**同一 query 的 progressively shorter 版本**，训练 LoRA 旋钮，实现推理时连续调长度。
- **关键陷阱 / 发现**：
  - **GSM8K**：741 → 225 token (−70%)，95.07% → 94.92%（几乎无损）。
  - **AIME**：6827 → 4629 token，只多错 1 题。
  - 最关键：**必须有"同题多长度"配对数据**才能学到平滑 valve；只给一种压缩比 + 一份数据会让模型 collapse 到「要么啰嗦要么跳步」。
- **与我们方法对照**：
  - 我们的 A/C/D 每个 query 只有一条轨迹的一种压缩版本，相当于把模型推到 valve 的一个固定档位；若和原 long-CoT 混训可缓解但没做。
  - 他们没有 fragment 分类、prompt-based 压缩，而是直接用 length annotation + 参数方向；更轻，但依赖可用的短轨迹数据。
- **可引用结论**：length-controlled 蒸馏在 GSM8K 上可压到 30% 长度且几乎无损，核心是 **配对数据 + 连续控制**。

---

## 3. LightThinker: Step-by-Step Online Compression (Zhang et al., ZJU + Ant, 2025)

- **论文**：arXiv:2502.15589。
- **核心做法**：**在线/动态**压缩——模型生成过程中把中间 thought 压成若干 "gist tokens"，丢掉原始文本，用专门的 attention mask 控制谁能看到什么。并引入 **Dep 指标** 衡量对历史 token 的依赖。
- **关键陷阱 / 发现**：
  - 训练同时包含 "何时压、压成什么" 两个信号；只学其一会崩。
  - 峰值显存 / 推理时间下降，accuracy 基本持平，但**压缩太激进时对长链条数学题效果变差**（Dep 高的任务）。
- **与我们方法对照**：
  - 我们是**离线数据端**压缩，对模型最友好但最死板；LightThinker 是**模型端**。
  - 他们隐式解决了「压缩数据与模型分布不匹配」的问题，因为压缩是学到的；我们纯靠 teacher LLM 写，没有 learnability 保证。
- **可引用结论**：gist-token 方案显示**单纯"压缩生成文本"比"压缩训练数据"更稳**；如果我们 SFT 崩了，可能是「模型被迫模仿一个它天然产生不了的分布」。

---

## 4. C3oT: Conditional Compressed CoT (Kang et al., AAAI 2025)

- **论文**：arXiv:2412.11664。
- **核心做法**：**compressor + 长短 CoT 联合训练 + conditioned inference**。训练时模型同时看长版和短版 CoT，推理时根据条件生成短 CoT。
- **关键陷阱 / 发现**：
  - 明确引用先前结论："shortening reasoning steps, even preserving key info, diminishes LLM abilities"。
  - C3oT 的解法是「**不要直接训短 CoT，要让模型知道它是从哪个长 CoT 压出来的**」——这个对照训练信号是保留能力的关键。
  - 可以做到 ≥50% 长度减少而不掉点，覆盖算术/常识推理。
- **与我们方法对照**：
  - 我们只训练压缩后的 CoT，**没有把原始长 CoT 作为对照数据一起丢进 SFT**。这是一个重要缺失。
  - C3oT / CoT-Valve / s1.1 的共同点都是 **"长+短 pair"** 的训练。

---

## 5. O1-Pruner: Length-Harmonizing Fine-Tuning (Luo et al., SYSU, 2025)

- **论文**：arXiv:2501.12570。
- **核心做法**：不是 SFT，而是 **RL-style 的 length reward**：reference model 先 sample 得到长度基线，再用 PPO loss 鼓励更短且不掉 accuracy。
- **关键发现**：
  - Marco-o1-7B / MATH：932 → 554 tokens (−40.5%)，accuracy 73.4 → 76.8（**反而涨**）。
  - QwQ-32B-Preview / MATH：1717 → 1121 tokens，88.2 → 89.3。
  - **"越长越对"是伪命题**：更短的 CoT 常常 accuracy 更高，因为长 CoT 里充满了自干扰。
- **与我们方法对照**：
  - 我们走的是 **SFT 模仿** 路径；O1-Pruner 明确指出「纯 SFT 教短 CoT 会出现 length disharmony——简单题过长、难题不够」，并用 RL 来解。
  - 我们没有任何 length-adaptive 机制，Method D 的"50/50 采样"虽然多样化但不基于问题难度。
- **可引用结论**：shorter CoT can actually raise accuracy；length-harmony 要对 **per-instance** 动态调整，不是整库一刀切。

---

## 6. TALE: Token-Budget-Aware LLM Reasoning (Han et al., 2024)

- **论文**：arXiv:2412.18547（TALE，有 TALE-EP prompt 版和 TALE-PT post-train 版）。
- **核心做法**：(1) 预估题目所需 token 预算；(2) 把预算放进 prompt；(3) 二分搜索最优预算。训练变体把预算内化。
- **关键发现**：
  - **Token Elasticity**：预算设得太低会**反弹超过预算**——模型会"焦虑"地多写。临界点附近性能反而恶化。
  - GSM8K：318 → 77 tokens (≈76% 压缩)，accuracy 掉 2–5 pp。
- **与我们方法对照**：
  - 我们没做难度感知——AIME 级别难题和 GSM8K 级别简单题都用同一压缩比；Method A 34.6% 对简单题可能过度、对难题可能不够。
  - Token Elasticity 预警：我们 Method A 压缩到 34.6% 已经进入危险区。
- **可引用结论**：**压缩比不能是常数，必须按题目难度调整**，否则简单题和难题同时受损。

---

## 7. s1 / s1.1: Simple Test-Time Scaling (Muennighoff et al., Stanford, 2025)

- **论文**：arXiv:2501.19393。
- **核心做法**：仅 **1,000** 条高质量长 CoT 数据 SFT Qwen2.5-32B-Instruct；推理时用 "budget forcing"（添加 "Wait" 续推或强制截断）。
- **关键陷阱 / SFT 超参（GitHub `train/sft.sh`）**：
  | 超参 | 值 |
  |---|---|
  | learning_rate | **1e-5** |
  | epochs | 5 |
  | warmup_ratio | 0.05 |
  | lr_scheduler | cosine |
  | weight_decay | 1e-4 |
  | adam_beta2 | 0.95 |
  | block_size (max_seq_len) | 32,768 (s1) / 20,000 (s1.1) |
  | batch size | 1/device（FSDP，全局 16 H100） |
- **和我们对照**：
  - 我们用的是 **5e-6**——比 s1 推荐的 1e-5 **低一档**，按理说更稳；baseline 都崩意味着问题**不在学习率过高**，而可能在：
    - sequence 太长未 pack / attention 数值不稳
    - `<think>` token 没正确处理（loss mask / special token id）
    - gradient checkpoint 关了导致 OOM-adjacent 数值问题
  - **s1 明确用 cosine + 5% warmup**；我们如果 warmup 少于 5% 或线性降不到位，长序列早期梯度爆炸风险高。
  - s1 用 beta2 = 0.95（非默认 0.999），**对长序列梯度尖峰更鲁棒**，这是一个常被忽视的细节。
- **s1.1 区别**：数据集换成 DeepSeek-R1 traces（s1 原版用 Gemini），block size 从 32k 降到 20k（OOM 教训）。
- **可引用结论**：1,000 条长 CoT 足够在 32B 上训出 o1-preview 级别；但 Qwen2.5-32B 基础模型 + lr=1e-5 + β2=0.95 + 32k block 是它验证过的稳定配方。

---

## 8. LIMO: Less Is More for Reasoning (Ye et al., 2025)

- **论文**：arXiv:2502.03387。
- **核心做法**：假说"推理能力靠预训练已具备，SFT 只需少量高质量示例"。用 817 条精选示例 SFT，Qwen2.5-32B-Instruct AIME24 从 6.5% 升到 **56.7%**，MATH500 达到 **94.8%**。
- **关键陷阱 / 发现**：
  - **数据质量 ≫ 数据量**；注入低质/被破坏的 CoT 立刻摧毁效果。
  - 和 s1 一起证明：**SFT 长 CoT 对"示例结构 + 逻辑完整性"极度敏感**——我们的压缩有可能在追求短度时破坏了这个结构。
- **与我们方法对照**：
  - 我们没有 per-sample quality filter；Method A/C/D 只保证"压缩比达标"，没人检查"压缩后这条 CoT 是否还能独立走通推理"。
  - **硬伤**：如果压缩后推理链的某一步失去 necessary premise，模型 SFT 时就在学一个"自洽性破损"的样本。

---

## 9. R1-Compress: Chunk Compression + Search (Wang et al., 2025)

- **论文**：arXiv:2505.16838（**Method C 的思想来源**）。
- **核心做法**：两阶段——(1) 把 long CoT 切成 manageable chunk；(2) 每个 chunk 单独 LLM 改写；(3) 跨 chunk 做 **search**（beam-like）挑选 "短 + coherent" 的组合；(4) 拿结果 SFT。
- **关键数字**：MATH500 92.4%，比 long-CoT baseline 仅掉 0.6 pp，token 减少约 20%。
- **与我们 Method C 对照**：
  - 相同：chunk-level 切分 + 分类改写。
  - **我们缺失的关键步骤**：**inter-chunk search**。R1-Compress 会对每个 chunk 生成多个候选压缩版，再用 search 选出**整体 coherent** 的；我们只有一次性改写，一旦上下文不连贯就直接污染数据。
  - R1-Compress 只压到 ~80% 长度（20% 减少），**远比我们 Method C 的 49.2% 保守**。这很重要——他们拿到 92.4% MATH500 的前提是相对保守的压缩比。
  - 基础模型：他们用的是 Qwen2.5-Instruct 系列，不是 Qwen3；Qwen3 的 `<think>` 原生格式可能需要额外兼容。
- **可引用结论**：chunk 级压缩要配**全局 search 保一致性**，否则 chunk 间会出现逻辑断裂；且**稳妥压缩比 ~20%**，我们 49.2% 的 Method C 已经 2× 激进。

---

## 10. Small Models Struggle to Learn from Strong Reasoners (Li et al., UW + Ohio State, 2025)

- **论文**：arXiv:2502.12143。
- **核心发现**：
  - "Small Model Learnability Gap"——**≤3B 模型在长 CoT / 大 teacher 蒸馏上系统性反向收益**。
  - Qwen2.5-3B 用 Mix-Long（20% long + 80% short）比纯 long-CoT 在 MATH 高 **≈8 pp**；纯长 CoT 反而掉点。
  - 7B+ 模型一般能从长 CoT 获益，但**临界区（3B–8B）行为不稳定**。
- **与我们对照**：
  - 我们用 **Qwen3-8B**，刚好在临界区边缘；Qwen3-8B 虽然比 7B 基线强，但**依然可能表现出 learnability gap**。
  - 他们的结论直接支持 **Method D 的"保留大部分原文 + 少量压缩"思路**（85.6%），以及 **C3oT / CoT-Valve 的"长短混训"**。
  - **我们三组方法都没做长短混训**，Baseline 也是纯压缩数据——这和论文"应该 mix 20/80"的建议相悖。
- **可引用结论**：对 ≤8B 模型，**要混入短 CoT**，建议比例 ~20% long / 80% short 或至少 50/50。

---

## 11. PART: Information-Preserving Antidistillation (Ding et al., MSRA, 2025)

- **论文**：arXiv:2510.11545。**反向证据，非常重要**。
- **核心做法**：故意**扰动** reasoning trace 而不改变信息量——(1) 删除 self-talk；(2) **重排 sub-conclusions**。作者目的是防蒸馏，但结果对我们是警钟。
- **关键数字**：32B 学生在 AIME2024 上从 54.17 → 46.88（**−13.5%**），仅仅因为 trace 被"无损信息地改写"。
- **与我们对照**：
  - 我们的 Method A/C **本质上就是 PART 所设计的攻击操作**：改写 + 重组 + 删除 "redundant" 语句。
  - **即使我们保留了信息**，只要改变了 trace 的 surface/structural form，学生模型 AIME 级别就可能掉 10+ pp。
  - Method D 保守（85.6%）因此最接近原分布，这和 PART 结论一致——**表面形式 >> 信息量**。
- **可引用结论**：**"信息保留 = 性能保留" 是错的**。trace 的 surface form 本身是可学信号；任何重写即使无损信息，都可能导致显著下降。

---

## 12. Towards Widening The Distillation Bottleneck (Yin et al., 2025)

- **论文**：arXiv:2503.01461。
- **核心做法**：MCTS 构造 diverse CoT 树 + fine-grained DPO。
- **关键发现**：long CoT 蒸馏的 **"over-thinking bias"**——学生会学到 teacher 里无意义的反思、自我怀疑模式，导致推理末端出现"已经解出来又否定自己"。
- **与我们对照**：
  - 我们 Method A/C 试图删掉 "redundant/exploration" 步骤，方向上对；但**删得对不对**没有验证，唯一的评判者是 LLM（Judge），没有下游性能反馈。
  - Yin 建议用 **DPO 级别的负样本**让模型"知道什么是 over-thinking"，我们只有正样本。
- **可引用结论**：纯 SFT 学压缩 CoT 容易退化；混合 DPO/preference 信号更稳。

---

## 13. Qwen3 Technical Report (Alibaba, 2025)

- **论文**：arXiv:2505.09388。
- **关键信息**：
  - Qwen3 post-training 的 **Stage 1 "Long-CoT Cold Start"** 用 long-CoT SFT，之后再 Stage 2 Reasoning RL，**不是直接把 long-CoT 当最终目标**。
  - `<think>...</think>` 是**原生特殊 token**，不是普通文本——tokenizer 对它们有独立 id；如果 SFT 数据里 `<think>` 用字符串而非对应的 special token 包裹，模型会 in-context 学到"预测字符串"而不是 "进入 thinking mode"。
  - Qwen3 支持 `enable_thinking=True/False`；dual-mode 的训练意味着 **loss mask 必须区分 thinking 段和 answer 段**。
  - Thinking budget 是**推理时机制**，不是训练时；SFT 不需要它，但需要保持 `<think>` 结构规范。
- **与我们对照**：
  - 我们 prompt 里写的是 `<think>压缩后 CoT</think>\n\n<答案>`——必须确认：
    1. `<think>` / `</think>` 是否走 special token id（`151667`, `151668`）；
    2. tokenize 前是否被 chat template 再包一层（Qwen3 chat template 会自动管理 think 段）；
    3. pad token 是否正确，Qwen3 的 pad_id 需要对齐。
  - **我们没有提到 loss mask**。Qwen3 官方示例里，在 thinking 模式下 `<think>...</think>` 段通常**计入 loss**（因为是模型要学的），但如果我们手写 chat template 双写了 `<think>` 或者 Alpaca 模板把 `<think>` 当普通文本，token 化可能错位。

---

## 14. DeepSeek-R1 Distill 系列 SFT 经验

- **文献**：DeepSeek-R1 Report (arXiv:2501.12948) + OpenCodeReasoning (arXiv:2504.01943) 的 LR ablation。
- **关键经验**：
  - DeepSeek-R1-Distill-Qwen-7B 官方推荐 lr **5e-6**，但 OpenCodeReasoning 报告 **4e-5 > 5e-6**，doubling LR ≈ +10 pp LiveCodeBench。这是**和"小 LR 稳定"直觉相反**的结果。
  - 对于已经 instruction-tuned 的基础模型，**conservative LR 反而学不进去 reasoning style**。
  - 标准配方：cosine + 10% warmup，bs=1–2/dev，grad accum 2–3，32k context on 7B。
- **与我们对照**：
  - 我们 5e-6 崩了，**不能直接推论"LR 太高"**——也可能是 **LR 太低 + 长序列**，导致模型没学到 thinking 结构却已经进入过拟合段。
  - 建议 ablation：1e-5 / 2e-5 / 4e-5 三点试跑。

---

# 「我们可能踩到的坑」清单（按概率高→低）

## 🚨 P0（最可能，症状即 baseline + 实验组一起崩）

### 坑 1. `<think>` special token 处理错误
- **症状**：SFT loss 前几步 drop fast 然后 NaN 或 plateau；推理时模型不产生 `<think>` 或一直不停；生成结果里出现裸 `<think>` 文字。
- **证据**：Qwen3 Report (2505.09388) 明确 `<think>` / `</think>` 是 special token id。
- **修复**：
  1. 打印 tokenizer 对 `"<think>"` 的分词结果，确认是**单个 id**（151667/151668）而不是多个 token。
  2. 改用官方 chat template（`apply_chat_template` with `add_generation_prompt=False`）生成训练样本，而不是手拼 `<think>...</think>`。
  3. 检查 Alpaca 模板是否破坏了 think 区域。

### 坑 2. Loss mask / packing 配置错误
- **症状**：baseline 和实验组同等崩溃；loss 曲线前期正常，后期突刺。
- **证据**：arXiv:2512.21002 & 社区一致结论——CoT-heavy SFT 必须正确 mask；packing 时边界处理错容易让 cross-sample 梯度污染。
- **修复**：
  1. 确认 attention_mask 在 packing 边界是 block-diagonal；
  2. labels 在 instruction 段是否设 -100；
  3. 对 `<think>` 段的 loss 是否正确计入。

## 🔥 P1（高概率，压缩方法本身的问题）

### 坑 3. 学生模型无法学到 "被改写的 CoT 分布"（PART 效应）
- **症状**：训练 loss 正常下降，但下游 MATH/AIME acc 反而低于未 SFT 的 Qwen3-8B-Base。
- **证据**：arXiv:2510.11545——仅仅删 self-talk + 重排 sub-conclusion 就能让 32B 学生 AIME 掉 13.5%；我们的 Method A 改写更激进。
- **修复**：做 **"original long CoT vs. compressed CoT"** 的 A/B SFT，观察是否只有原始 CoT 才能学得住。

### 坑 4. 压缩比对"题目难度"不敏感
- **症状**：简单题 over-compress 导致跳步；难题 under-compress 导致 token 仍爆。
- **证据**：TALE (2412.18547) Token Elasticity；O1-Pruner (2501.12570) length disharmony。
- **修复**：按 problem difficulty（如 teacher trace 的 token 长度或已有难度标签）分桶，每桶独立压缩比。

### 坑 5. 没有"长短混训"
- **症状**：Qwen3-8B 在临界区，纯 compressed SFT 导致推理能力倒退。
- **证据**：arXiv:2502.12143 (Mix Distillation 20/80)、C3oT、CoT-Valve。
- **修复**：
  1. 最小改动：在 SFT 数据里 **混入 20% 原始长 CoT** 作对照信号；
  2. 中等：构造同题长短对，loss 里加 "same-question consistency" term。

### 坑 6. Chunk 级改写缺少跨 chunk 搜索（Method C 的 gap）
- **症状**：样本里会有"上下文断裂"——前 chunk 把变量 x 定义了，后 chunk 改写时丢了。
- **证据**：R1-Compress (2505.16838) 明确把 inter-chunk search 作为第二阶段核心。
- **修复**：
  1. 对 Method C 每个 chunk 生成 2–3 个候选压缩，用 perplexity 或"与前 chunk 的 NLI consistency"选最优；
  2. 或者降低压缩比到 ~20%（R1-Compress 的稳妥区间）。

## 🟡 P2（中概率，超参 / 工程）

### 坑 7. 学习率 5e-6 可能偏低（不是偏高！）
- **症状**：loss 在最初几百步下降极慢，之后陷入平台；表现像"没学进去"而不是"学炸了"。
- **证据**：s1 用 1e-5；OpenCodeReasoning 发现 **4e-5 > 5e-6**（+10 pp LiveCodeBench）。
- **修复**：试 1e-5 / 2e-5；同时把 `adam_beta2` 从默认 0.999 降到 **0.95**（s1 做法）。

### 坑 8. warmup 不够 / schedule 不对
- **症状**：前 200 步内出现 loss spike 或 NaN。
- **证据**：长上下文需要 **5%–10% warmup + cosine**；每增加 1024 token 建议降 LR ~18%。
- **修复**：warmup_ratio=0.05，lr_scheduler=cosine，grad_clip=1.0，显式指定 β2=0.95。

### 坑 9. max_seq_length 和 block_size 未对齐数据长度分布
- **症状**：样本被中途截断，`</think>` 或 answer 缺失 → labels 失真 → loss 反向崩。
- **证据**：s1.1 把 block_size 从 32k 降到 20k 是 OOM 修复；但截断会让"答案段丢失"致命。
- **修复**：绘制 token 长度分布；确认 `max_length ≥ 99-percentile`；对超长样本 drop 而不是截断。

### 坑 10. 压缩后 CoT 自洽性破损（LLM judge 没兜底）
- **症状**：某些样本 SFT 时 loss 正常，但推理时在"对应题型"上格外差。
- **证据**：LIMO、Widening Distillation Bottleneck——数据质量敏感度极高。
- **修复**：用 verifier（小 LLM 或 teacher 自己）对压缩后 CoT 做 **"能否独立 re-derive 原答案"** 检查，失败的样本剔除或回退到原版。

## 🟢 P3（低概率但值得一查）

### 坑 11. Qwen3-8B instruction-tuned 基底不是"blank slate"
- Qwen3-8B 本身已经经过 long-CoT cold start + reasoning RL；再 SFT 压缩版相当于"拿低质 trace 覆写高质能力"。
- **修复**：对比在 **Qwen3-8B-Base** 上 SFT 的效果，排除"覆盖已训好 reasoning"的风险。

### 坑 12. Alpaca 格式和 Qwen3 原生 chat template 不兼容
- 若我们用 Alpaca instruction/input/output，模板里的特殊分隔符和 `<think>` 互相干扰。
- **修复**：改用 Qwen3 官方 chat template（ChatML 风格）+ `apply_chat_template(..., enable_thinking=True)` 生成训练样本。

---

# 建议的下一步实验（按优先级）

1. **诊断先于修复**：重开一次 baseline 训练，logging 打开 per-step grad norm、param norm、`<think>` 附近的 token id → 定位"崩"是 NaN / plateau / diverge 中的哪一种。
2. **验证 token pipeline**：对 1 条样本端到端 tokenize → decode，确认 `<think>` 是 special id；labels mask 在 instruction 和 answer 段正确。
3. **LR 扫描**：`[5e-6, 1e-5, 2e-5]` + β2=0.95 + 5% warmup + cosine，看是否只是 LR 问题。
4. **原始长 CoT baseline**：用未压缩数据同样超参 SFT；如果**原始数据也崩**，问题在工程栈而非压缩方法。
5. **Mix 实验**：20% 原始长 + 80% Method D；或 50/50。参照 arXiv:2502.12143。
6. **保守压缩**：把 Method C 压缩比从 49.2% 改到 80% (≈R1-Compress)，验证是否立刻恢复。
7. **verifier 过滤**：对 Method A 数据做 "re-derive" 检查，剔除破损样本（LIMO 启示）。

---

# 读过的论文清单

1. TokenSkip — arXiv:2502.12067
2. CoT-Valve — arXiv:2502.09601
3. LightThinker — arXiv:2502.15589
4. C3oT — arXiv:2412.11664 (AAAI 2025)
5. O1-Pruner — arXiv:2501.12570
6. TALE — arXiv:2412.18547
7. s1 / s1.1 — arXiv:2501.19393（含 GitHub 训练脚本超参）
8. LIMO — arXiv:2502.03387
9. R1-Compress — arXiv:2505.16838（我们 Method C 的原论文）
10. Small Models Struggle to Learn from Strong Reasoners — arXiv:2502.12143
11. PART (Antidistillation) — arXiv:2510.11545
12. Widening The Distillation Bottleneck — arXiv:2503.01461
13. Qwen3 Technical Report — arXiv:2505.09388
14. DeepSeek-R1 Distill 经验 / OpenCodeReasoning — arXiv:2501.12948 + 2504.01943
15. (参考) AutoL2S — arXiv:2505.22662
16. (参考) SimpleRL-Zoo — arXiv:2503.18892
17. (参考) Distilling the Essence (Sequence Truncation) — arXiv:2512.21002

未找到/不存在：**"Condor" 作为 CoT 压缩方法没有可靠 arXiv 源**（可能是你方向 3 里的误记；搜出来是与主题无关的结果），已跳过。

---

# 核心 takeaway（一句话版本）

> 如果 **baseline 也崩**，问题大概率在 **token/loss pipeline（坑 1–2）** 而非压缩方法；如果只有压缩实验组崩而 baseline 正常，那按 **P1 的坑 3–6** 排查（PART 效应、难度感知缺失、长短不混训、chunk 断裂）。**`5e-6` 本身未必过高——s1/OpenCodeReasoning 证据都指向 1e-5~4e-5 是更合适的起点，但前提是 token pipeline 正确**。
