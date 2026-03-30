# DataFlow 协同开发规范与问题记录

> 本文件记录在 cc 分支协同开发过程中的：
> 1. 开发规范与约定（团队达成一致的规范）
> 2. 已遇到的问题及解决方案（避免重复踩坑）
> 3. 重要决策与背景

---

## 一、开发规范

### 1.1 分支与版本管理

- **当前工作分支**：`cc`（来自 `https://github.com/HeRunming/DataFlow.git`）
- 主仓库参考：`https://github.com/OpenDCAI/DataFlow`（`main` 分支）
- 开发新特性请从 `cc` 分支新建特性分支，PR 合并回 `cc`

### 1.2 算子开发规范

#### 必须遵守

1. **继承 `OperatorABC`**，调用 `super().__init__()` 以初始化 `self.logger`
2. **注册算子**：`@OPERATOR_REGISTRY.register()` 装饰器加在类定义上
3. **`run()` 参数命名约定**（Pipeline 编译依赖此规则）：
   - 输入 key 参数以 `input_` 开头，值为 DataFrame 列名字符串
   - 输出 key 参数以 `output_` 开头，值为 DataFrame 列名字符串
   - `storage` 参数必须存在（第一个参数或关键字参数），类型为 `DataFlowStorage`
4. **`run()` 必须返回输出 key 列表**，如 `return ['output_col1', 'output_col2']`
5. **`run()` 必须调用 `storage.read()` 和 `storage.write()`**
6. **在 `__init__.py` 的 `TYPE_CHECKING` 块声明导入**，否则 LazyLoader 无法发现算子

#### 强烈建议

7. 实现 `@staticmethod get_desc(lang: str = "zh")` 方法，支持 `zh`/`en` 两种语言，供 WebUI 展示
8. 在 `__init__` 中用 `self.logger.info(f"Initializing {self.__class__.__name__}...")` 记录初始化日志
9. 在 `run()` 中用 `self.logger.info(...)` 记录关键步骤

#### 文件命名约定

- Filter 算子文件名：`xxx_filter.py`，类名：`XxxFilter`
- Refine 算子文件名：`xxx_refiner.py`，类名：`XxxRefiner`
- Eval（sample 级别）：`xxx_sample_evaluator.py`，类名：`XxxSampleEvaluator`
- Eval（dataset 级别）：`xxx_dataset_evaluator.py`，类名：`XxxDatasetEvaluator`
- Generate 算子：`xxx_generator.py`，类名：`XxxGenerator`

### 1.3 Pipeline 开发规范

1. 继承合适的 Pipeline 基类：
   - 普通：`PipelineABC`
   - 需要分批：`BatchedPipelineABC`
   - 流式分批：`StreamBatchedPipelineABC`
2. 所有算子在 `__init__` 中实例化为**成员变量**（`compile()` 依赖此机制）
3. `forward()` 中每个算子调用使用独立的 `storage.step()` 副本
4. 推荐使用 `LazyFileStorage` 替代 `FileStorage` 以获得原子落盘和更好的中断安全性
5. Pipeline 文件建议放在 `dataflow/example/` 对应子目录或项目根目录下

### 1.4 Prompt 开发规范

1. 继承 `PromptABC`，注册 `@PROMPT_REGISTRY.register()`
2. 实现 `build_prompt(self, ...) -> str` 方法
3. 若算子需要限制 Prompt 类型，使用 `@prompt_restrict(...)` 装饰算子类
4. 用户自定义 Prompt 继承 `DIYPromptABC`，可绕过 `@prompt_restrict` 白名单

### 1.5 LLM Serving 使用规范

1. **API key 必须通过环境变量注入**，禁止硬编码在代码中
   - 推荐环境变量名：`DF_API_KEY`（也可自定义）
   - 设置方式：`os.environ["DF_API_KEY"] = "..."` 或在 shell 中 `export DF_API_KEY=...`
2. 算子中持有 `llm_serving` 对象的成员变量**必须命名为 `self.llm_serving`**（Pipeline 通过 `isinstance` 检测此字段管理 Serving 生命周期）
3. 不要在算子内手动调用 `serving.cleanup()`，由 Pipeline 自动管理

### 1.6 代码风格

- Python 3.10+，可使用 `X | Y` 类型注解语法
- 使用 type hints
- 日志使用 `self.logger`（`get_logger()` 获取），不使用 `print()`（开发调试可临时用 `print`）
- 异常处理中记录详细日志

---

### 1.7 算子复用原则（避免重复造轮子）

> **核心原则：开发新算子前，必须先检查仓库中是否已有功能相同或相近的算子，优先复用，而非重复实现。**

#### 检查已有算子的方法

1. **查阅各模块 `__init__.py` 的 `TYPE_CHECKING` 块**，里面列出了该模块所有已注册算子：
   - `dataflow/operators/general_text/__init__.py`
   - `dataflow/operators/text_sft/__init__.py`
   - `dataflow/operators/reasoning/__init__.py`
   - `dataflow/operators/core_text/__init__.py`
   - 等等……

2. **通过 OPERATOR_REGISTRY 动态查询**（运行时）：
   ```python
   from dataflow.utils.registry import OPERATOR_REGISTRY
   OPERATOR_REGISTRY._get_all()          # 加载所有算子
   print(OPERATOR_REGISTRY.get_obj_map()) # 查看所有已注册算子名
   ```

3. **查阅 DATAFLOW_KNOWLEDGE_BASE.md 的第八节**，有按模块分类的算子功能概述。

#### 复用而非重复的场景示例

| 需求 | 不要重写 | 复用 |
|------|---------|------|
| 按字数过滤文本 | ❌ 新写 `TextLengthFilter` | ✅ 复用 `WordNumberFilter` |
| 按字符数过滤 | ❌ 新写 | ✅ 复用 `CharNumberFilter` |
| 去除 HTML 标签 | ❌ 新写 | ✅ 复用 `HtmlEntityRefiner` |
| 推理答案生成 | ❌ 新写 LLM 调用 | ✅ 复用 `ReasoningAnswerGenerator` |
| SFT 数据质量评估 | ❌ 新写 | ✅ 复用 `AlpagasusSampleEvaluator` / `DeitaQualitySampleEvaluator` 等 |

#### 合理的新增算子场景

以下情形才真正需要新建算子：
- 现有算子的功能完全不覆盖新需求（不同输入格式、不同处理逻辑）
- 需要在新领域（如化学、医学）引入领域专用规则
- 对已有算子做了**本质不同**的改动（不是改改参数或 prompt 就能解决的）

---

### 1.8 算子健壮性与容错规范

> **算子，尤其是依赖 LLM 输出的算子，必须做好充分的容错处理。模型输出永远无法保证完全符合格式要求，任何一次解析失败都不应导致整批数据的结果丢失。**

#### 核心原则

1. **逐条容错，不允许一条失败导致整批崩溃**：对每个 LLM 返回值单独 try/except
2. **失败时记录日志，保留现场**：使用 `self.logger.warning()` 记录原始输出和错误信息
3. **失败时给出合理默认值**：根据算子类型决定——跳过该条（filter/generate），或返回 0/None（eval）
4. **类型前置检查**：先检查 `response is None` 和 `isinstance(response, str)`，再做正则解析

#### 标准容错模板

**模板 A：generate/filter 类算子——解析失败则跳过该条**
```python
# 参考：CondorGenerator.parse_generated_responses()
#        AgenticRAGWidthQAGenerator.run()
valid_rows = []
for idx, response in enumerate(responses):
    try:
        if not isinstance(response, str) or response is None:
            self.logger.warning(f"[Skipped] idx={idx}: response is not a valid string: {type(response)}")
            continue

        # --- 正则/JSON 解析 ---
        result = json.loads(self._clean_json_block(response))
        if "required_field" not in result:
            self.logger.warning(f"[Skipped] idx={idx}: missing 'required_field' in result: {result}")
            continue

        valid_rows.append(result)

    except (json.JSONDecodeError, KeyError) as e:
        self.logger.warning(f"[Error] idx={idx}: parse failed: {e} | raw: {response[:200]}")
        continue
    except Exception as e:
        self.logger.warning(f"[Error] idx={idx}: unexpected error: {e}")
        continue
```

**模板 B：eval 类算子——解析失败则返回默认值（0/None）**
```python
# 参考：PromptedEvaluator._parse_scores()
def _parse_scores(self, outputs: list[str]) -> list[int]:
    results = []
    for idx, out in enumerate(outputs):
        score = 0   # 默认值
        try:
            if out is None:
                results.append(score)
                continue
            text = str(out).strip()
            match = re.search(r"\d+", text)
            if match:
                val = int(match.group())
                if 1 <= val <= 5:   # 范围校验
                    score = val
        except Exception:
            score = 0  # 确保异常时也有兜底值
        results.append(score)
    return results
```

**模板 C：JSON 清理辅助函数（清除 Markdown 代码块标记）**
```python
# 参考：AgenticRAGWidthQAGenerator._clean_json_block()
def _clean_json_block(self, item: str) -> str:
    """去除模型输出中可能包裹的 ```json ... ``` 代码块标记"""
    return item.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
```

**模板 D：多层级正则降级策略**
```python
# 参考：Text2SQLCoTVotingGenerator 的 SQL 提取
def _extract_target(self, response: str) -> str:
    if not isinstance(response, str):
        return ""
    # 层级 1：最严格匹配
    blocks = re.findall(r"```sql\s*(.*?)\s*```", response, re.DOTALL | re.IGNORECASE)
    if blocks:
        return blocks[-1].strip()
    # 层级 2：次级匹配
    blocks = re.findall(r"```\s*(SELECT.*?)\s*```", response, re.DOTALL | re.IGNORECASE)
    if blocks:
        return blocks[-1].strip()
    # 层级 3：宽松匹配
    match = re.search(r"(SELECT\b.*)", response, re.DOTALL | re.IGNORECASE)
    if match:
        return match.group(1).strip()
    # 层级 4：降级为空字符串
    return ""
```

#### 必须容错的场景

| 场景 | 应对方式 |
|------|---------|
| `response is None`（API 超时/失败） | 前置 `if response is None` 检查，跳过或返回默认值 |
| `response` 类型不对 | `isinstance(response, str)` 检查 |
| JSON 解析失败 | `try/except json.JSONDecodeError` |
| 正则匹配为空 | `if match:` 判断，配合降级策略 |
| 字段缺失 | `if "field" not in result:` 检查 |
| 数值范围异常 | 范围校验（如 `1 <= val <= 5`） |
| 模型输出含代码块标记 | 用 `_clean_json_block()` 预处理 |

---

### 1.9 推理模型（CoT 模型）输出处理规范

> **背景**：DeepSeek-R1 等带有 CoT 推理过程的模型，其 API 响应中包含 `reasoning_content` 字段（独立的推理链）。DataFlow 的 Serving 层会自动将其包装为 `<think>...</think>\n<answer>...</answer>` 格式返回给算子。如果算子不做处理，写入数据集的内容将包含思维链，而非干净的答案/问题。**

#### Serving 层的行为（已内置）

`APILLMServing_request`、`LiteLLMServing`、`LocalHostLLMAPIServing_vllm` 均实现了 `format_response()`，其逻辑为：

```
如果 API 返回了 reasoning_content（DeepSeek R1 等模型）：
    → 返回 "<think>{reasoning}</think>\n<answer>{content}</answer>"
否则：
    → 直接返回 content
```

因此，**算子收到的 response 可能有两种格式**：
1. 普通模型：直接的文本输出
2. CoT 模型（如 DeepSeek R1）：`<think>...</think>\n<answer>...</answer>` 格式

#### 何时需要剥离 CoT

| 算子类型 | 是否需要剥离 CoT | 原因 |
|---------|----------------|------|
| **问题生成**（QuestionGenerator） | ✅ **必须剥离** | 数据集里要的是干净的问题，不是推理过程 |
| **答案/代码生成**（写入普通 answer 字段） | ✅ **必须剥离** | 最终答案不应包含推理链 |
| **推理数据生成**（写入 `generated_cot` 字段） | ❌ **不要剥离** | CoT 本身就是训练目标，需要完整保留 |
| **评估算子**（数值打分） | ✅ **必须剥离** | 只需要从 answer 部分提取分数 |
| **分类/判别算子** | ✅ **必须剥离** | 只需要从 answer 部分提取结论 |

#### 标准 CoT 剥离函数

```python
# 参考：ReasoningQuestionGenerator._parse_response()
import re

def _strip_cot(self, response: str) -> str:
    """
    剥离 DeepSeek R1 等 CoT 模型输出中的 <think>...</think> 部分。
    - 若 response 符合 <think>...</think><answer>...</answer> 格式，返回 <answer> 内容
    - 否则原样返回（兼容普通模型的直接输出）
    """
    if not isinstance(response, str) or response is None:
        return response
    pattern = r"<think>.*?</think>\s*<answer>(.*?)</answer>"
    match = re.search(pattern, response, re.DOTALL)
    if match:
        return match.group(1).strip()
    # 兼容普通模型（无 <think> 标签）：原样返回
    return response
```

**在算子 `run()` 中应用**：

```python
def run(self, storage, input_key, output_key="new_question"):
    dataframe = storage.read("dataframe")
    prompts = [self._build_prompt(row[input_key]) for _, row in dataframe.iterrows()]
    responses = self.llm_serving.generate_from_input(prompts)

    # ✅ 必须在写入前剥离 CoT
    clean_responses = [self._strip_cot(r) for r in responses]

    dataframe[output_key] = clean_responses
    storage.write(dataframe)
    return [output_key]
```

#### 何时保留完整 CoT 输出

若算子明确需要保留 CoT（如 `ReasoningAnswerGenerator` 生成的 `generated_cot` 字段），则直接写入 response 即可，无需剥离：

```python
# ReasoningAnswerGenerator 的做法：直接保留完整输出（含思维链）
dataframe[self.output_key] = answers   # answers 包含 <think>...</think>
```

#### 注意事项

- **正则要用 `re.DOTALL`**：`<think>` 内容可能跨多行，必须加 `re.DOTALL` 否则 `.` 不匹配换行符
- **容错优先**：剥离函数内部不要抛异常，用 `if match:` 判断后返回原文作为兜底
- **不要在 Serving 层修改此行为**：Serving 的 `format_response()` 逻辑已经统一，不要为了某个算子去改它；在算子内部按需剥离

---

## 二、已知问题与解决方案

### Issue #001：算子参数 key 命名不一致导致 Pipeline 编译警告

**症状**：`OperatorNode._get_keys_from_kwargs()` 中打印红色警告：
```
Warning: Unexpected key 'xxx' in operator YyyOperator
```

**原因**：`run()` 中使用了不以 `input_` 或 `output_` 开头的非 `storage` 参数（如 `threshold`, `model_name` 等配置参数）。

**说明**：这**不是错误**，只是警告。配置参数（非 key 参数）是正常的，不影响运行。但可在 `run()` 参数设计时尽量将"数据列名"参数用 `input_`/`output_` 命名，"算法配置"参数放在 `__init__` 中。

**最佳实践**：
```python
# 推荐：列名参数在 run() 中，配置参数在 __init__() 中
class GoodFilter(OperatorABC):
    def __init__(self, min_len=10):   # ← 配置放 __init__
        self.min_len = min_len
    def run(self, storage, input_key="text", output_key="label"):  # ← 列名在 run
        ...
```

---

### Issue #002：LazyLoader 无法找到算子类

**症状**：`KeyError: No object named 'MyNewFilter' found in 'operators' registry!`

**原因**：新增算子类未在对应模块 `__init__.py` 的 `TYPE_CHECKING` 块中声明。

**解决方案**：在 `dataflow/operators/<module>/__init__.py` 的 `TYPE_CHECKING` 块中添加：
```python
if TYPE_CHECKING:
    from .filter.my_new_filter import MyNewFilter
```

---

### Issue #003：Pipeline 编译时 KeyError（输入 key 不存在）

**症状**：
```
KeyError: Key Matching Error in following Operators during pipeline.compile():
- Input key 'instruction' in `op2` (class <SomeFilter>) does not match any output keys...
```

**原因**：Pipeline 中某算子的 `input_key` 引用了一个在上游算子 `output_key` 或初始数据集列名中不存在的字段。

**解决方案**：
1. 检查初始数据文件（`first_entry_file_name`）的列名
2. 检查上游算子 `run()` 的返回列表是否包含该 key
3. 确保 `run()` 中 `output_key` 参数的值与 `storage.write(df)` 时写入 df 的列名一致
4. 注意：返回列表中的 key 必须与 `run()` 参数名**对应的值**一致，而不是参数名本身

---

### Issue #004：`storage.step()` 忘记调用

**症状**：
```
ValueError: You must call storage.step() before reading or writing data.
```

**原因**：`storage.operator_step` 初始值为 `-1`，在调用 `read()`/`write()` 前必须先调用 `step()` 递增计数器。

**解决方案**：在 Pipeline `forward()` 中，每个算子调用时使用 `storage.step()` 传入副本：
```python
def forward(self):
    storage = FileStorage("input.jsonl", cache_path="./cache")
    self.op1.run(storage=storage.step(), input_key="text")
    self.op2.run(storage=storage.step(), input_key="text")
```

---

### Issue #005：`DummyStorage.get_keys_from_dataframe()` 抛出异常

**症状**：`AttributeError` 或 `TypeError`

**原因**：`DummyStorage` 未实现 `get_keys_from_dataframe()` 抽象方法（`DataFlowStorage` 要求实现）。

**说明**：`DummyStorage` 主要供 `BatchWrapper` 内部使用，不适合直接在 Pipeline 编译的 `_build_operator_nodes_graph()` 中使用（该方法会调用 `get_keys_from_dataframe()`）。

**解决方案**：Pipeline 中请使用 `FileStorage` 或 `LazyFileStorage`。

---

### Issue #006：多个算子共享同一个 Serving 时的生命周期问题

**说明**：Pipeline 使用引用计数管理 Serving 生命周期。若多个算子共享同一个 `LLMServingABC` 实例，Pipeline 会在最后一个使用该 Serving 的算子执行完毕后才调用 `cleanup()`。

**注意事项**：
- 不要在算子中手动调用 `serving.cleanup()`
- 若需要不同算子使用不同 Serving，在 `__init__` 中实例化不同对象即可
- Pipeline 切换 Serving 时（上一个 Serving 引用计数归零时）自动调用 `cleanup()`

---

### Issue #007：`SFTGeneratorSeed` 文件中 `@prompt_restrict` 装饰位置错误

**现象**：在 `sft_generator_from_seed.py` 中，`@prompt_restrict(SFTGeneratorSeedPrompt)` 装饰器放在了一个独立函数 `extract_json_object` 上方（而非紧贴类定义），导致装饰器实际上**没有**应用到 `SFTGeneratorSeed` 类上。

**代码位置**：`dataflow/operators/text_sft/generate/sft_generator_from_seed.py`

**状态**：已发现（截至 2026-03-29），待修复。

**正确写法**：
```python
@prompt_restrict(SFTGeneratorSeedPrompt)
@OPERATOR_REGISTRY.register()
class SFTGeneratorSeed(OperatorABC):
    ...
```
或
```python
@OPERATOR_REGISTRY.register()
@prompt_restrict(SFTGeneratorSeedPrompt)
class SFTGeneratorSeed(OperatorABC):
    ...
```

---

## 三、开发经验与最佳实践

### 3.1 调试 Pipeline 编译

```python
pipeline = MyPipeline()
pipeline.compile()
# 编译后查看 key 追踪情况
for i, keys in enumerate(pipeline.accumulated_keys):
    print(f"After step {i}: {keys}")
# 可视化 DAG（需要 pyvis）
pipeline.draw_graph()
```

### 3.2 快速测试算子（不走 Pipeline）

```python
from dataflow.utils.storage import FileStorage
from dataflow.operators.general_text import WordNumberFilter

op = WordNumberFilter(min_words=5, max_words=100)
storage = FileStorage("test_data.jsonl", cache_path="./test_cache")
storage.step()  # step 0：读取输入文件
op.run(storage, input_key="text", output_key="word_count")
# 查看结果
import pandas as pd
result = pd.read_json("./test_cache/dataflow_cache_step_step1.jsonl", lines=True)
print(result)
```

### 3.3 DummyStorage 用于单元测试

```python
from dataflow.utils.storage import DummyStorage
import pandas as pd

storage = DummyStorage()
storage.set_data(pd.DataFrame({"text": ["hello world", "foo bar baz"]}))
storage.operator_step = 0  # 手动设置步骤

op = WordNumberFilter(min_words=2, max_words=10)
op.run(storage, input_key="text")
result = storage.read()
print(result)
```

### 3.4 LazyFileStorage 推荐配置

```python
from dataflow.utils.storage import LazyFileStorage

storage = LazyFileStorage(
    first_entry_file_name="input.jsonl",
    cache_path="./cache",
    file_name_prefix="pipeline_cache",
    cache_type="jsonl",
    save_on_exit=True,      # 进程退出时自动 flush
    flush_all_steps=False   # 只保留最新步骤（节省磁盘）
)
```

### 3.5 环境变量配置模板

```bash
# .env 文件（使用 python-dotenv 加载，或在脚本中 os.environ 设置）
export DF_API_KEY=sk-xxxxxxxxxxxx
export DF_LOGGING_LEVEL=INFO   # DEBUG / INFO / WARNING / ERROR
```

---

## 四、待办与开发计划

> 此部分记录已知需要完成但尚未完成的工作

- [ ] **修复** `sft_generator_from_seed.py` 中 `@prompt_restrict` 装饰位置错误（Issue #007）
- [ ] `dataflow init operator` CLI 命令尚未实现（目前输出 "not implemented yet"）
- [ ] `dataflow init pipeline` CLI 命令尚未实现
- [ ] `dataflow init prompt` CLI 命令尚未实现

---

## 五、版本变更记录

| 日期 | 事件 |
|------|------|
| 2026-03-29 | 初始化协同开发规范文件，基于 cc 分支 v1.0.10 |
| 2026-03-29 | 新增 §1.7 算子复用原则、§1.8 算子健壮性与容错规范、§1.9 推理模型 CoT 输出处理规范 |

---

*最后更新：2026-03-29*
