# 密钥托管轮换审计器（rotation_audit）

离线灾备演练中的密钥托管轮换审计与最优修复。轮换期间旧代与新代可能同时被要求，
同一人不能在同一份恢复见证里出现两次，托管点之间还可能共享机房 / 电源 / 值班团队，
因此"两代各自可恢复"并不等于"一次双代恢复可完成"。

本实现使用 **Windows 原生 Python 3.12 + 标准库 + CPU**，无第三方依赖。

```powershell
python -m rotation_audit audit EXAMPLES\case_sample.json
python -m rotation_audit repair EXAMPLES\case_sample.json
python -m unittest discover -s tests -v
```

仓库内样例路径为 `examples/case_sample.json`，两条命令都会把 JSON 报告打印到标准输出。

---

## 1. 命令、退出码、重跑

| 命令 | 作用 |
| --- | --- |
| `python -m rotation_audit audit CASE.json` | 基线审计：枚举全部时间段 × 故障组合，给出见证、缺口与首败 |
| `python -m rotation_audit repair CASE.json` | 在已批准动作中求全局最优修复；无解时明确返回 `impossible` |

退出码：

| 码 | audit | repair |
| --- | --- | --- |
| `0` | 所有场景通过 | 找到可行且可证明最优的动作集合 |
| `1` | 至少一个场景失败 | 证明无解（`status: "impossible"`） |
| `2` | 用法错误、文件不可读、输入不合法 | 同左 |

`python -m rotation_audit` 不带参数或多余参数时打印用法并返回 `2`。输入校验错误会写到
标准错误（`error: <路径>: <原因>`），标准输出不会出现半成品 JSON。

重跑：命令是纯函数，同一输入永远得到逐字节相同的输出（见 §7 的确定性测试）。
把 `examples/case_sample.json` 复制一份改名即可作为自建用例的模板。

---

## 2. 输入格式

```jsonc
{
  "format": "rotation-audit-v1",        // 可选；给出时必须等于此值
  "drill": {
    "start": 0, "dual_start": 4, "switch": 5, "dual_end": 6, "end": 10
  },
  "generations": {
    "old": { "label": "旧代", "threshold": 2, "shares": ["old-1", "old-2"] },
    "new": { "label": "新代", "threshold": 2, "shares": ["new-1", "new-2"] }
  },
  "domains": ["site-a", "site-b"],      // 故障域表，可为空
  "budget": 2,                          // b：每段枚举 0..b 个域的故障组合
  "max_actions": 4,                     // k：最多可选动作数
  "copies": [
    {
      "id": "old-a", "generation": "old", "share": "old-1", "custodian": "A",
      "domains": ["site-a"], "intervals": [[0, 10]]
    }
  ],
  "actions": [
    {
      "id": "action-1", "cost": 10,
      "copy": {
        "id": "new-d-1", "generation": "new", "share": "new-1", "custodian": "D",
        "domains": [], "intervals": [[4, 6]]
      }
    }
  ]
}
```

### 字段与规则

* `drill` 五个整数时刻必须满足 `start ≤ dual_start < switch < dual_end ≤ end`。
* 所有区间左闭右开 `[from, until)`；演练 `[start, end)` 之外不判定。
* 时刻归属（三者等价化简后的判定，实现里直接使用化简式）：
  * 旧代要求在 `[start, dual_end)`；
  * 新代要求在 `[dual_start, end)`。
  于是双代窗口 `[dual_start, dual_end)` 内两代同时要求，这正是样例首败落在 `[4,5)` 的原因。
* `shares` 是该代的**分片命名空间**；不同代可以同名，副本的 `share` 必须属于自己那一代。
* `custodian` 是**跨代共用**的命名空间：同一个人在同一份见证里只能出现一次。
* 副本的 `domains` 是它依赖的故障域集合；与当前故障集合**无交集**时副本才可用。
* 同一副本的多个区间取并集；重叠、相邻、相同区间都合法。
* 候选副本 ID 必须与现有副本及其他候选副本互异；`actions[].copy.id` 就是候选副本 ID。
* 标识只允许 `[A-Za-z0-9_-]+`，排序按区分大小写的字典序。
* 整数必须是 JSON 整数：`true` / `false` / `1.0` 一律拒绝。

### 支持范围（超出即拒绝，不会静默截断）

| 项 | 上限 |
| --- | --- |
| 故障域 | 10 |
| 候选动作 | 16 |
| 现有副本 | 60 |
| 每代分片 | 10 |
| 切分后时间段 | 24 |
| `budget` b | 0–2 |
| `max_actions` k | 0–4 |

拒绝的输入还包括：重复 JSON 键、重复副本 / 动作 / 分片 / 故障域声明、未知引用
（另一代的分片、未声明的故障域）、空或倒置或超出演练的区间、非正门槛、非正成本、
布尔型整数、未知字段。

---

## 3. 输出格式

两条命令共享同一份场景空间：分段与故障组合由**所有现有及候选副本**的区间端点决定，
即使某候选最终未被选中，所以 `audit` 与 `repair` 的 `segments` / `faultSets` /
`case.scenarioDigest` 永远一致。

### audit

```jsonc
{
  "command": "audit",
  "case": { "drill": {...}, "generations": {...}, "budget": 0, "maxActions": 4,
            "segments": 4, "faultSets": 1, "scenarios": 4, "scenarioDigest": "…" },
  "segments": [{ "index": 0, "start": 0, "end": 4 }, ...],
  "faultSets": [[], ["site-a"], ["site-b"], ["site-a", "site-b"]],
  "scenarios": [
    {
      "index": 1, "segment": 1, "start": 4, "end": 5,
      "faults": [], "requiredGenerations": ["old", "new"],
      "recoverable": false,
      "copies": { "old": ["old-a", "old-b"], "new": ["new-c"] },
      "shortfall": { "old": 0, "new": 1 }
    }
  ],
  "summary": {
    "passed": false, "scenarioCount": 4, "recoverableScenarios": 2,
    "failedScenarios": 2, "firstFailure": { ... }
  }
}
```

* `scenarios` 的顺序：时间段按起点升序；段内故障集合按（集合大小，域 ID 元组）升序。
* 可恢复时 `copies` 是选中的副本 ID，每代内部升序、旧代在新代前，取所有完整见证里
  这个嵌套列表字典序最小的一组；此时 `shortfall` 为 `{}`。
* 不可恢复时 `copies` 是按同样规则组织的**最大部分见证**，`shortfall` 给出每个要求代
  离门槛还差几份。只输出当期要求的代；某代一份都没选到时保留空列表。
* `firstFailure` 是第一个失败场景的完整条目，全部通过时为 `null`。

### repair

```jsonc
{
  "command": "repair",
  "status": "feasible",              // 或 "impossible"
  "selected": ["action-1"],          // 升序动作 ID
  "cost": 10,
  "actionCount": 1,
  "case": { ... }, "segments": [...], "faultSets": [...],
  "scenarios": [ ... ],              // 选定方案按 audit 同口径生成的完整场景
  "summary": { ... }
}
```

* 目标按字典序：先最小化总成本，再最小化动作个数，最后比较升序动作 ID 列表。
* 基线已全部通过：`selected: []`、`cost: 0`、`actionCount: 0`。
* 无解：`status: "impossible"`、`selected: []`、`cost: null`、`actionCount: 0`，
  并给出 `reason` 与 `firstUnresolvable`；不会把未证明最优的方案当作结果。
  `scenarios` 为空、`summary.passed` 为 `false`。

---

## 4. 场景空间

1. 取五个演练时刻 ∪ 所有副本（现有 + 候选）可用区间的**原始端点**，限制在 `[start, end]`，
   排序后相邻点构成最小非空时间段。实现不合并相邻区间再切分，因此"相邻区间"只在
   可用性上等价，不会额外制造或抹掉切点。
2. 每个时间段枚举故障域表中 0 到 b 个域的全部组合，排序为（大小，域 ID 元组）。
3. 场景 =（时间段，故障集合）。副本在该段可用且其依赖域与故障集合无交集时才可用。

---

## 5. 恢复见证与算法

### 5.1 流模型（唯一的可行性判据）

固定场景与可用副本集合，构造网络

```
source --(threshold_g)--> 代 g --(1)--> (g, 分片) --(1)--> 保管人 --(1)--> sink
```

* 每代源边的容量就是该代门槛；
* `(代, 分片)` 节点容量 1，天然保证同代分片 ID 不重复，也保证同一分片的多个副本最多取一份；
* 保管人汇边容量 1，保证整份见证（跨代）人员不重复。

最大流值 = 任何见证能选到的最大副本数。等于门槛之和 ⇔ 存在完整见证。这解释了
"两代各自能恢复，仍可能因共同保管人而无法完成同一次双代恢复"：两个独立见证的人数
之和可以超过可用人员数，合并后最大流不足。

### 5.2 字典序最小见证

见证写成嵌套列表 `[老代副本ID…, 新代副本ID…]`，每代内部升序。

* **完整见证**：每代长度固定，普通字典序即可判定。从左到右贪心：每一步取"仍属于某个
  最大流"的最小副本。某个副本属于某个最大流 ⇔ 它的边未饱和，或其两端在残量图的同一
  强连通分量中（可推 1 单位流绕环）。这是精确判据，贪心无需回溯。
* **部分见证**：先由流值拿到最大合计份数 F，再枚举各代份数分配（至多 `门槛_old+1` 种），
  对每种分配跑同一贪心，取嵌套列表最小者。因为嵌套列表序里"前缀小于任何扩展"，
  这样得到的正是"先最大化合计份数，再字典序最小"。

一次见证至多 `门槛_old+门槛_new ≤ 20` 次选点，每次一次最大流；流图 ≤ 2+2+20+63 个节点。

### 5.3 repair 的搜索状态、剪枝与最优性

1. **只关心失败场景**：加副本不会让任何副本不可用，可恢复性对动作集合单调，
   所以基线已通过的场景不构成约束。
2. **行为类合并**：`(基线可用位图, 候选可用位图, 要求代)` 相同的场景在任何动作集合下
   表现完全一致，每类只留一个代表。被选中的候选副本只在它自己的可用区间内、且故障域
   与故障集合无交集时才算可用，因此候选的窗口不会污染其他时间段。
3. **最小修复集**：对每个失败类，按大小递增枚举动作子集，收集**包含极小的**可行子集
   （若已有更小的可行子集是它的子集就跳过）。由单调性，一个集合能修好该类 ⇔ 它包含
   其中一个最小修复集。枚举时另有支配剪枝：若更小的集合已经以"可用性超集"失败，
   当前集合不可能成功。
4. **候选缩减**：任何可行解都必须为每个失败类包含一个最小修复集，所以某个最优解一定
   落在所有最小修复集的并集 U 里。
5. **按目标序枚举 U 的子集**：以 `(总成本, 动作个数, 升序动作 ID 列表)` 排序，
   返回第一个对**所有**场景可行者。该序正是题目要求的字典序，枚举又是穷尽的，
   因此首个命中即全局最优；枚举耗尽即证明 `impossible`。

规模：U ≤ 16、k ≤ 4，最多 `C(16,0)+…+C(16,4) = 2517` 个子集；每个子集对所有场景做
一次流判定，且 `(可用位图, 要求代)` 相同的判定有缓存，不会重复建图。

**为何不漏最优解**：第 1 步用单调性排除了"基线已过"的约束；第 2 步只合并真正等价的
场景；第 3 步的最小修复集由单调性保证覆盖所有可行集合；第 4 步由"可行解 ⊇ 每类一个
最小修复集"保证 U 含某个最优解；第 5 步穷尽 U 的全部子集并按目标序取首个。
每一步都是等价变换或超集缩减，因此既不会漏解，也不会把不可证明的方案当作最优。

---

## 6. 样例

`examples/case_sample.json` 对应题目给出的起步样例：`0/4/5/6/10`，无故障，两代门槛各 2；
旧代两枚分片分别只有 A、B 保管，新代两枚分别只有 A、C 保管，四份副本全程可用；
三个候选动作都让新代 A 的分片多一个由 D 保管的副本，前两个同价覆盖 `[4,6)`，
第三个更便宜只覆盖 `[5,6)`。

```
$ python -m rotation_audit audit examples/case_sample.json   # 退出码 1
summary.firstFailure = { start: 4, end: 5, copies: {old:[old-a,old-b], new:[new-c]},
                         shortfall: {old:0, new:1} }

$ python -m rotation_audit repair examples/case_sample.json  # 退出码 0
selected = ["action-1"], cost = 10
```

`action-3` 更便宜但只覆盖 `[5,6)`；`[4,5)` 也需要新代第二份分片，所以它不够。
`action-1` 与 `action-2` 同价同覆盖，按动作 ID 字典序取较小者。

另外两个可直接运行的样例：

| 文件 | 演示 |
| --- | --- |
| `examples/case_shared_domain.json` | 两枚新代分片分别依赖 `site-a` / `site-b`：单个站点故障仍可恢复，双站点同时故障则不可恢复（域对组合必须枚举） |
| `examples/case_no_unified_fix.json` | 两段各有一个便宜修复，但 `k = 1` 下没有统一方案，repair 返回 `impossible` |

---

## 7. 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖：样例断言（首败位置、最优动作）、跨站点共用故障域、单域全过但域对失败、
同一分片的副本不可凑数、跨代共同保管人、两段各有可选动作但 k 限制下无统一方案、
部分见证并列、相邻区间与区间端点切分、输入顺序打乱后输出一致、全部输入校验规则、
CLI 退出码、规模上限快速性。

**穷举对照**：`tests/oracle.py` 是与引擎完全不共享求解代码的独立实现——自己重新切段、
枚举故障组合、枚举**所有**副本选取（同一分片最多一份、人员不重复），用 Hopcroft–Karp
求最大匹配；repair 侧枚举**所有**动作子集。测试用固定种子（`20261121` / `424242`）
生成随机小型演练，逐场景比较 audit 的完整结果，并比较 repair 的 `(成本, 动作数,
动作 ID)`，历史上 400 个随机用例零分歧。
