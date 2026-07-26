# Coding Orchestrator 编排实验记录

> 实验时间：2026-07-22 至 2026-07-23
>
> 当前状态：reviewed-Python orchestrator 已成为唯一生产编排路径。下文涉及旧 DSL 的内容仅作为
> 实验历史保留，不代表受支持的 Runtime 架构。
>
> 当前基线模型：`qwen3.5-35b-a3b`（配置 profile：`qwen35`）

## 1. 实验背景

现有语义 DSL 可以表达业务控制流，但复杂任务的 Program 生成存在两个问题：

- 模型缺少该私有 DSL 的先验，生成、校验和修复成本较高；
- 当任务包含集合、条件、循环、聚合和业务断言时，DSL 表达未必比通用语言更简单。

本实验验证另一条规划层路线：把“生成编排”转换成模型更熟悉的“写一个受约束的 Python
函数”。模型负责写业务程序，框架只提供少量语义能力、静态验证、受限执行和诊断修复。

核心假设是：模型的代码生成能力和 Python 先验，能够降低编排生成成本，并提高复杂控制流的
准确率。这里的 Python 是规划表示，不是页面自动化脚本，也不是运行时重构。

## 2. 目标架构结论

Coding Orchestrator 的目标不是设计另一种编排 DSL，而是利用 Coding Agent 已有的代码生成和任务
分解能力，把用户需求实现为一个受限 Python 业务函数：

```text
用户需求
  → Coding Agent 生成 Python Program
      → lookup / acquire / read 建立业务数据流
      → 普通 Python 表达计算、条件、循环和断言
      → ctx.interact 调用一次业务操作
          → Statement React 观察、操作、局部换路并验收
```

目标程序形态如下（这是架构方向，不是回退后原型的当前调用签名）：

```python
def run(ctx):
    scope = ctx.lookup("target", field="name")
    rows = ctx.acquire(scope, fields=["id", "status"])
    assert len(rows) == 1, "expected exactly one target"

    ctx.interact(
        "更新目标记录状态",
        target=rows[0],
        inputs={"status": "approved"},
    )

    return rows[0]["id"]
```

规划层只负责宏观上确定的逻辑：

- 谁负责什么，以及操作哪些业务对象；
- 数据从哪里读取、如何计算、流向哪个后续操作；
- 什么时候分支或循环；
- 操作之间的依赖顺序和最终输出；
- 可由运行时业务数据验证的断言。

Statement React 负责一次业务操作内部的微观环境不确定性：

- 导航、搜索、打开详情或编辑页；
- 点击、输入、选择、滚动、跨页和弹窗处理；
- 保存方式、结果验收和已满足状态的幂等返回；
- 当前 GUI 路径失败后的观察与局部换路。

因此，规划接口不应暴露 `persistence`、`view | ensure | perform`、receipt、
`changed | noop` 或 UI retry policy。保存是否真正发生以及如何证明，由 Statement 封装内部推断和
验收；规划调用成功即返回，最终无法完成则抛出异常，不再要求 `assert ctx.interact(...)`。

Python AST 是最终规划表示，不 lowering 成现有 DSL Program。AST 只用于安全检查、接口约束、
数据流检查和受限执行，不承担编译器式的细节展开。原型仍未修改现有 Interpreter、Statement
executor 或 adapter 的生产执行路径；执行层问题暂时记录，当前实验只验证规划程序质量。

### 2.1 基础能力边界

| 能力 | 规划语义 |
| --- | --- |
| `lookup` | 按业务身份或语义条件建立实体范围 |
| `acquire` | 物化需要过滤、聚合或循环的业务集合 |
| `read` | 读取会参与后续数据流或控制流的业务状态 |
| `compute` | 调用明确提供的外部计算；简单确定性计算优先使用普通 Python |
| `interact` | 通过 Statement React 对一个业务对象执行一次业务操作 |
| `command` | 调用适配器明确提供的确定性平台能力 |

如果一个状态只用于当前业务操作的实现和验收，它留在 `interact` 内部；如果它会影响 Python
后续计算、返回值、分支或循环，就必须通过 `read` 显式进入 Program。

### 2.2 `ctx.interact` 的粒度

`ctx.interact` 不是一个点击、页面步骤或完整需求，而是一个业务操作级 command：

```text
一个业务动词 + 一个主要业务对象 + 一组明确输入 + 一个业务提交结果
```

推荐形式是：

```python
ctx.interact(
    "向商品尺码属性添加选项",
    target=size_attribute,
    inputs={"option": "XXXL"},
)
```

不推荐使用“确保商品支持目标配置”这类需求级描述，因为它可能隐藏多个业务对象、多个提交以及
未显式表达的数据依赖。

出现以下任一情况时应拆成多个 `interact`：

- 主要业务对象或业务责任改变；
- 存在两个独立业务提交；
- 中间结果需要返回 Python；
- 中间状态影响后续业务分支或循环；
- 后一个业务操作显式依赖前一个操作完成。

不能因为换页、搜索、打开编辑器、填写多个相关字段、点击保存、处理弹窗、验收或局部重试而拆分。
若当前状态已经满足同一业务操作，Statement 可以验收后直接成功返回；只有该状态会改变后续
Program 时，才需要 `read` 和 Python 分支。简言之：UI 路径分支属于 Statement，业务控制分支
属于 Program。

### 2.3 失败与重新规划

Statement React 在当前 `interact` 内部完成“观察—尝试—验收—换路”，只重新思考当前业务操作
的 GUI 实现，后续 Python 语句保持不变。这不是残差 Program 重规划，也不需要在 Program 中声明
retry policy。

只有当失败证明业务分解、目标、输入、数据依赖或控制流本身错误时，才由 Coding Agent 修复相应
Python 代码。正常的 UI 路径失败不应触发整个剩余 Program 重写。

## 3. 最小实现

原型代码位于：

- `gui_agent/core/orchestrator/`：模型、规划器、AST 校验、受限沙盒与 Runtime；
- `gui_agent/prompts/tasks/orchestrator/coding.md`：场景无关的代码生成合同；
- `scripts/coding_orchestrator_eval.py`：冻结 fixture、私有 grader 和批量评估；
- `tests/test_coding_orchestrator.py`、`tests/test_coding_orchestrator_eval.py`：机制与评估测试。

当前原型的一次生成包含以下门禁：

1. 模型输出唯一的 `run(ctx)` 函数；
2. AST 校验拒绝未授权语法、调用和属性；
3. 程序在隔离进程内对 fixture capability 执行；
4. 私有 grader 根据 trace、返回值和业务不变量判定语义正确性；
5. 语法、执行或业务校验失败时，携带诊断最多修复一次。

沙盒还对语义字段进行规范化，并兼容常见实体身份别名，但不替模型补业务路线。

2026-07-23 曾把实验接口收敛为 `action + target + inputs`，并让沙盒只记录业务调用意图，不再
模拟 GUI 写入或修改 fixture read state。该版本在冻结未见任务 491、550、768 上为 0/3。

为避免在未证明收益前继续扩大增量，当前代码已经回退到 2026-07-22 获得 7/12 的原型：
`interact(goal, success, target, values, persistence)`、布尔返回值断言和
`explicit_commit` fixture 行为均恢复。该回退只是恢复可比较的实验基线，不代表
`persistence` 已重新成为目标架构的一部分，也没有修改生产 DSL runtime。

## 4. 评估方法

本轮没有直接做五次重复稳定性测试。顺序是：

1. 先用少量已知任务验证能力链能否执行；
2. 冻结一批未参与开发的任务、fixture 和 grader；
3. 每个任务只生成一次，即 `K=1`；
4. 只在整批完成后审计 fixture/grader 是否误拒或误收有效程序；
5. 泛化达到门槛后，才进入同任务多次采样的稳定性测试。

当前试验门槛是：冻结泛化集语义通过率和可执行率达到约 80%，且不能依赖少数任务的偶然通过。
所有模型均未达到该门槛，所以尚未进行稳定性结论测试。

离线规划评估只应判断：

- 是否把需求拆成了正确的业务操作；
- 每次操作的目标、输入和依赖是否正确；
- 数据流、分支、循环、计算、业务断言和返回 shape 是否正确。

它不应判断实际点击了保存、是否出现成功提示、是否产生 receipt、实际写入次数，或者将调用解释成
`changed`/`noop`。这些属于 Statement executor 集成测试和真实 WebArena E2E 的验收范围。

### 原始分数与审计分数

报告中的原始分数是执行当时的 grader 结果。审计分数仅修正以下评估设施问题：

- fixture 没有覆盖另一条同样完整、合法的数据读取路径；
- 业务字段大小写或实体身份别名造成非语义性拒绝；
- grader 对等价但不同的 trace 结构判断过窄；
- fixture 范围过宽导致错误程序被误判为通过。

审计只重放已经生成的源码，不重新调用模型。它不能修复错误业务路线、缺失计算、错误输出格式或
被沙盒拒绝的 Python。

## 5. 前置功能验证

早期聚焦任务包括 WebArena 778、549、62、501 和 543。结果说明以下能力链可以在原型中表达并执行：

- 精确检索、模糊回退、完整集合采集、逐项读取和批量修改；
- 跨页业务步骤；
- 读取当前值后计算并提交；
- 通过 trace 与私有业务断言发现“代码能跑但业务不正确”。

这些任务用于开发能力和 grader，不计入后续未见任务的泛化分数。特别是 778 的早期全部失败暴露出：
仅验证 AST 可执行并不足够，评估必须带有独立于模型源码的业务断言。

## 6. Qwen3.5 未见任务泛化

### 第一批

冻结任务：77、107、183、208、500、704。

| 结果 | 任务 |
| --- | --- |
| 原始通过 | 183、500、704 |
| 审计后通过 | 77、183、500、704 |
| 审计后失败 | 107、208 |

原始为 3/6，审计后为 4/6。任务 77 和 500 的合法读取路径得到兼容；任务 208 原先因 fixture
放宽电话号码范围而被误收，收紧为用户要求的本地区段后改判失败。

### 第二批

冻结任务：11、94、184、203、470、699。

| 结果 | 任务 |
| --- | --- |
| 原始及审计后通过 | 11、94、184 |
| 原始及审计后失败 | 203、470、699 |

第二批为 3/6。两批合计审计后通过 7/12，即 58.3%。它证明方案可以生成多种有效业务程序，
但尚不足以说明机制具备可接受的通用性。

## 7. 同任务模型对比

四个模型使用相同的 12 个 `K=1` 冻结任务和同一能力合同。模型切换只影响规划代码生成；离线
fixture 评估不调用真实 Statement/action runtime。

| 模型 | 推理模式 | 原始通过 | 审计后通过 | 平均调用 | 平均输出 token | 中位耗时 |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Qwen3.5 35B A3B | 非 thinking | 6/12 | 7/12（58.3%） | 1.42 | 816 | 5.10 s |
| DeepSeek V4 Flash | 非 thinking | 3/12 | 5/12（41.7%） | 1.67 | 667 | 10.92 s |
| Qwen3.7 Max 2026-05-20 | thinking | 5/12 | 8/12（66.7%） | 1.58 | 2411 | 33.51 s |
| Qwen3.6 35B A3B | 非 thinking | 3/12 | 5/12（41.7%） | 1.58 | 901 | 9.39 s |

审计后通过任务分别为：

- Qwen3.5：77、183、500、704、11、94、184；
- DeepSeek V4 Flash：77、183、94、184、470；
- Qwen3.7 Max：77、107、183、500、704、11、184、470；
- Qwen3.6：77、183、11、184、470。

Qwen3.7 Max 是唯一成功修复任务 107 日期聚合的模型，但平均输出约为 Qwen3.5 的 3 倍，中位
延迟约为 6.6 倍，仍未达到泛化门槛。按实验时的公开或本地配置价格粗估，Qwen3.7 单批代码生成
成本约为 Qwen3.5 的 33 倍；该估算只用于方向判断，不是严格账单对比。

DeepSeek V4 Flash 倾向把 UI 机械操作过度拆成多个业务步骤；Qwen3.6 没有解决受限 Python 和业务
知识落地问题。当前因此恢复 Qwen3.5 作为默认实验基线；临时的 DeepSeek、Qwen3.7 profile 已移除，
原有 `qwen36` profile 保留。Qwen3.7 更适合作为未来复杂聚合或第二阶段修复候选，而不是当前默认。

## 8. 主要失效模式

### 8.1 受限 Python 与代码先验冲突

任务 107、203 多次生成 `datetime`、`split`、`sort`、lambda、辅助函数等普通 Python 写法，随后被
沙盒拒绝。若实验目标是利用模型的代码先验，过窄的语言子集会抵消 Python 表示的优势。

### 8.2 能力签名合法，但语义参数错误

例如 `ctx.lookup("order", "302")` 在 Python 调用层合法，却把 `302` 绑定成字段名。当前 validator
能检查调用形状，不能充分检查 entity、field、fallback 的语义角色。

### 8.3 Router 和知识没有可靠进入代码

任务 208 已提供电话号码本地区段提示，模型仍扩大范围。任务 699 把 benchmark 所需的 Cart Price
Rule 错选成 Catalog Price Rule。这类错误不是代码语法问题，而是结构化 grounding 和站点知识消费问题。

### 8.4 Statement 边界不稳定

部分模型会把页面内点击拆成多个 `interact`，另一些模型会把一个局部需求中的多个对象、业务操作、
数据筛选和提交全部隐藏在一个 `interact` 中。正确边界不是页面数、点击数、固定 Statement 数量，
也不是宽泛的业务后置条件，而是“对一个主要业务对象执行一个明确业务操作”。

这需要由生成合同和静态检查共同约束：

- 拒绝只描述导航或 UI 机械动作的 `interact`；
- 识别一个调用中包含多个主要对象、多个业务动词或多个提交的情况；
- 要求运行时计算值通过 `inputs` 显式传入；
- 要求会影响后续控制流的状态通过 `read` 显式进入 Python。

### 8.5 输出合同不够强

部分程序完成了数据计算，却返回错误 wrapper、额外字段或错误标识符。返回值需要像 capability
调用一样有明确、可机械验证的 shape。

### 8.6 “有断言”不等于断言有效

模型会生成 `assert len(rows) >= 0`、`condition or True` 等恒真断言。当前 validator 只能证明源码中
存在断言，无法证明它保护了目标身份、集合完整性或计算关系。程序断言应验证规划层可见的业务
不变量，例如目标身份、集合完整性、必要字段、基数和数值转换；它不应重复验证 `interact` 是否成功
或保存是否持久化。独立 grader 仍需识别空洞断言，并验证源码之外的目标语义。

### 8.7 评估 fixture 必须允许等价路线

模型可能从完整列表字段读取，也可能先采集身份再逐项进入详情读取。fixture 和 grader 必须接受
多条同样完整的路线，同时不能因为兼容别名而放宽业务范围。本轮已补充的通用兼容包括：

- 实体身份字段接受 `id`/`ID` 等规范化别名；
- 任务 183、184、470 接受完整列表与详情读取的等价组合；
- 任务 77 的 Pending Reviews 被建模为权威 pending scope；
- 任务 500 的记录状态可从合法读取路径获得。

这些修改只影响离线评估公平性，没有向核心 prompt 注入 WebArena 个案。

## 9. 典型任务的规划边界

### 9.1 WebArena 778：读取后计算再写入

当前价格参与新价格计算，因此必须显式进入 Program：

```python
detail = ctx.read(products_state, target=product, fields=["price"])
new_price = round(detail["price"] * 0.8, 2)
assert new_price < detail["price"], "discount must reduce the price"

ctx.write(
    "更新商品价格",
    target=product,
    values={"price": new_price},
)
```

模型是否真正使用读取到的业务状态构造写入值，是规划质量问题；如何打开编辑页、填写、保存和证明
成功，是 Statement 执行问题。

### 9.2 WebArena 549：有依赖的多个业务操作

跨页本身不是拆分理由。这里需要两个 `interact`，是因为它们针对不同业务对象、具有不同业务责任
和提交边界：

```python
ctx.interact(
    "向商品尺码属性添加选项",
    target=size_attribute,
    inputs={"option": "XXXL"},
)

ctx.interact(
    "为商品创建属性组合",
    target=product,
    inputs={"color": "Green", "size": "XXXL"},
)
```

前一个操作建立后一个操作所需的业务条件；每个操作内部都可以自行跨页、保存和验收。

### 9.3 WebArena 771：集合筛选后逐项操作

集合获取、业务筛选和循环属于 Program，每个成员上的批准属于业务操作：

```python
reviews = ctx.acquire(review_scope, fields=["id", "rating"])
targets = [review for review in reviews if review["rating"] >= 4]

for review in targets:
    ctx.interact(
        "批准商品评论",
        target=review,
        inputs={},
    )
```

若某条评论已经批准，Statement 可以验收后直接返回。离线规划 grader 应检查调用目标集合，不应把
每次调用等同于一次实际数据库写入。若用户要求返回计数，则按输出合同返回；纯修改任务不应擅自
增加结果 wrapper。

## 10. 业务操作级接口复验（已回退）

### 10.1 机制验证

复验先用手写程序覆盖 549、778、771，以及冻结的新任务 491、550、768。当时 55 项
Coding Orchestrator 与 grader 针对性测试通过，说明：

- `ctx.interact(action, target, inputs)` 可以表达单对象和集合级业务操作；
- fixture trace 不需要 persistence、receipt 或模拟写入；
- grader 可以分别验证目标选择、运行时计算、调用输入、业务顺序和输出合同；
- 771 的逐项批准和一次显式批量批准都是合法等价路线；
- 纯 Save/Open/Search 等 UI 机械操作可以由 AST validator 拒绝。

### 10.2 开发集模型验证

开发集使用 549、778、771，各轮均为 Coding-only、`K=1`。三个任务都曾分别生成过语义正确程序，
但同批结果在不同轮次明显波动：

- 首轮原始 2/3，778、771 通过；
- 修正等价 Products 集合 fixture 和 mutation 输出合同后，后续轮次仍出现 778 把不可用的集合
  `size` 当作已读取字段、771 使用合法批量调用但 grader 尚未接受等问题；
- 第三轮经 grader 公平性审计后为 2/3，549、771 通过，778 是真实规划失败。

这说明新接口能够工作，但 Qwen3.5 对数据来源和业务操作边界的遵循尚不稳定。开发集只用于机制
调试，不能作为泛化成绩。

### 10.3 冻结未见任务

在冻结 Prompt、fixture 和 grader 后，对 491、550、768 首次各运行一次：

| 任务 | 原始结果 | 审计结果 | 主要原因 |
| --- | --- | --- | --- |
| 491 | 失败 | 失败 | 修复后仍使用禁止的 `next` 重新解析业务身份，并生成多余读取 |
| 550 | 失败 | 失败 | 把 Statement 内部幂等检查和写后验收展开到 Python，依赖沙盒伪造写入 |
| 768 | 失败 | 失败 | 找到首个目标即 `break`，并把库存直接写为 5，没有使用当前值 7 计算 12 |

未见集原始及审计后均为 0/3。`created_at`、属性 `code/label` 等 fixture 等价字段已在审计后补齐，
但这些修正不能消除源码中的因果错误，因此没有重跑同一批任务。

按照预先约定的门槛，本轮没有进入 `K=5` 稳定性、旧 DSL A/B 或真实 WebArena 执行。

### 10.4 回退版同集对照

回退到 2026-07-22 原型后，保留完全相同的 491、550、768 fixture 和业务判定，再做一次
Coding-only、`K=1` 对照：

| 任务 | 业务操作级接口 | 回退版 | 对照结论 |
| --- | --- | --- | --- |
| 491 | 失败 | 失败 | 两版都错误理解 Sarah Miller 的订单范围；属于业务身份和范围 grounding 失败 |
| 550 | 失败 | 通过 | 回退版借助显式提交语义正确完成新增尺码选项和生成商品配置两个阶段 |
| 768 | 失败 | 严格失败 | 回退版正确读取 7 并写入 12，但找到首个候选即停止，没有证明目标变体唯一 |

因此严格结果为回退版 1/3、业务操作级接口 0/3。若只观察“读取当前库存并构造写入值”这一核心
因果链，回退版在 768 也明显更好；但完整规划仍未满足目标集合的唯一性要求，所以不把它改判为
通过。该对照说明新接口虽然边界更干净，却没有转化为更好的模型程序质量。

## 11. 当前结论

“把编排问题转化为写代码问题”仍是可行的研究方向，但当前原型还不能替换现有规划器。

支持继续探索的证据：

- 同一组基础能力可以表达检索、集合、循环、聚合、条件和业务操作；
- 模型无需学习完整私有 DSL 即可生成可执行程序；
- Python AST 容易做静态验证、隔离执行、诊断定位和源码级局部修复；
- 最佳模型在未见任务上达到 8/12，说明失败不是整体表达能力不足。

目标架构判断仍然成立：

- Python Program 是需求实现函数，负责宏观确定性业务逻辑；
- `ctx.interact` 是业务操作函数，不是点击步骤，也不是完整需求；
- Statement React 负责当前业务操作内部的 GUI 不确定性、局部重试和验收；
- Python 不转回 DSL，也不沿用 DSL 的 persistence、infeasible 或残差重规划概念；
- 离线规划评估与真实执行验收必须分层。

但本轮进一步证明，仅替换表示和收敛 `interact` 接口不足以获得可靠泛化：

- 默认 Qwen3.5 泛化通过率只有 58.3%，最佳模型也只有 66.7%；
- 新接口冻结未见集为 0/3；
- 模型仍会把 Python 写成页面工作流或测试脚本，在 Program 中展开幂等检查和写后验收；
- 模型能读取状态却不一定使用该状态构造写入值；
- 失败集中在代码约束、业务操作粒度、数据依赖和知识 grounding；
- 当前业务断言质量主要由私有 grader 保证；
- 本轮只验证了离线规划与 fixture 执行，没有验证真实浏览器执行成功率。

当前决策是恢复 7/12 的最小原型作为实验基线，不保留业务操作级接口实现，也不进入稳定性或生产
接入。`action + target + inputs` 的架构讨论和失败报告作为后续设计证据保留。下一轮若继续实验，
应先在不扩大运行时的前提下验证 Program/Statement 边界和运行时数据依赖是否真正改善模型程序
质量，再决定是否重新修改接口。Python 不转回 DSL；Qwen3.7 暂记为可能的条件升级模型。

## 12. 下一步实验

优先级按“先修通用机制，再扩大任务或重复采样”排列：

1. 为动态写入建立通用数据依赖检查：写入 `inputs` 必须能追溯到对应 `read` 和 Python 变换；
2. 识别 `interact` 前后的实现级状态检查，禁止 Program 依赖 Statement 的写后 fixture state；
3. 强化身份选择检查，机械拒绝 `break`、`next`、任意首项和未证明唯一性的目标；
4. 将 Router facts、search hint 和站点知识以结构化输入绑定给程序，而不是只作为长文本提示；
5. 检测恒真或与业务目标无关的断言，区分源码业务合同与独立 grader 验收；
6. 先用另一批未见任务做 `K=1`；只有达到门槛后再做 `K=5` 和真实 WebArena；
7. 若采用模型路由，验证“Qwen3.5 首次生成、Qwen3.7 仅处理复杂规划或修复”的成本收益。

## 13. 复现与证据

单批评估命令示例：

```bash
.venv/bin/python scripts/coding_orchestrator_eval.py \
  --tasks 77 107 183 208 500 704 \
  --k 1 \
  --surfaces coding
```

本轮主要本地报告：

| 实验 | 报告路径 |
| --- | --- |
| Qwen3.5 第一批 | `logs/coding_orchestrator_eval/20260722_222522/report.json` |
| Qwen3.5 第二批 | `logs/coding_orchestrator_eval/20260722_224615/report.json` |
| DeepSeek V4 Flash | `logs/coding_orchestrator_eval/20260722_225800/report.json` |
| Qwen3.7 Max | `logs/coding_orchestrator_eval/20260722_230834/report.json` |
| Qwen3.6 | `logs/coding_orchestrator_eval/20260723_083947/report.json` |
| 业务操作级开发集首轮 | `logs/coding_orchestrator_eval/20260723_111456/report.json` |
| 业务操作级开发集第三轮 | `logs/coding_orchestrator_eval/20260723_112207/report.json` |
| 业务操作级冻结未见集 | `logs/coding_orchestrator_eval/20260723_112350/report.json` |
| 回退版同集对照 | `logs/coding_orchestrator_eval/20260723_114553/report.json` |

相关实现与评估测试曾整体通过 140 项；业务操作级接口曾通过 55 项针对性测试。回退后保留三项
冻结对照任务，并通过 51 项针对性回归测试。
报告属于本地生成的运行时证据，不应作为源码提交；需要复核时应从冻结脚本和当前代码重新生成。
