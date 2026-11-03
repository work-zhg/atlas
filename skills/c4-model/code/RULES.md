# Level 4：Code

## 这张图回答什么

把 Component 图上的一个 component 打开，看它在代码里是怎么实现的：由哪些类、接口、枚举、函数组成，它们之间怎么协作。它把粗粒度的 component 映射到真实的代码元素上，连通「架构」和「代码」两个世界。

读者是开发团队内部的开发者。

**这一层可选，一般不推荐。** 这些细节就在代码里，IDE 随时能生成，长期维护的文档尤其不必画。只在两种情况下画：

- 最复杂、最重要的 component；
- 当作模板，说明一种在代码库里反复使用的模式。

## 范围与元素

| 项 | 内容 |
|---|---|
| Scope | 一个 component，画成包框，左上角标签页写包名或模块名 |
| Primary elements | 这个 component 里的代码元素：类、接口、抽象类、枚举、函数 |
| Supporting elements | 一般不画，component 外的类型（如入参、返回值）只出现在成员签名里。例外：本 component 实现的外部接口、继承的外部基类，要讲清依赖倒置或扩展点时，可以画成外部元素（`external: true`），放在包框外 |

### 画多少细节

- **限制范围**：只画一个 component 的内部。用类图描述整个应用，结果只会是一堆重叠的框和线，跟代码本身结构好不好无关。
- **只画要讲的事需要的**：属性和方法只保留和要讲的事相关的那些，不要每个字段、每个方法都列。自动生成时尤其要克制。
- 纯数据结构、工具函数、常量，和要讲的事无关就不画。

### 代码元素怎么对应（Python）

| 代码 | `kind` |
|---|---|
| 普通类、dataclass、pydantic 模型 | `class` |
| `typing.Protocol`、只有抽象方法的 ABC | `interface` |
| 有部分实现的 ABC 基类 | `abstract` |
| `enum.Enum`、`Literal` 取值集合 | `enum` |
| 模块级函数（只画架构上重要的） | `function`，签名写在 `methods` 里 |

- 可见性：Python 用命名约定，`_` 开头的记 `-`（private），其余记 `+`（public）。只在包内使用的可以记 `~`（package）。
- 签名按语言习惯写，如 `renew_thread_lock(thread_id, owner) -> bool`。参数太长时只写参数名。
- 包框写模块路径，如 `atlas_agent.stream.relay`。

### 按领域画（DDD，可选）

用户要按 DDD 领域模型看一个 component 时，把它拆成几个领域，每个领域一张图，说明这个领域对外提供什么能力。

- **先定两件事再动手**：怎么拆领域；画现状映射（每个元素都是真实代码，用构造型标角色）还是目标模型（理想的领域模型，代码里可能还没有）。拿不准就问用户。
- **构造型**：每个元素写 `stereotype`，取值用 应用服务、对外接口、聚合根、实体、值对象、领域服务、策略、仓储、工厂、防腐层、扩展点、领域事件。
- **对外入口**：每张图有一个 «应用服务» 或 «对外接口» 元素，放在上方，它的 public 方法就是这个领域的对外能力；再用 `capabilities` 逐条写清能力、入口、调用方和说明。
- **说明文字**：`overview` 用一段话说明领域做什么、不管什么、给谁用；`decisions` 逐条写设计决策和理由，回答「为什么这么设计」，理由要落到具体约束（并发、外部系统的限制、规则散落的位置），不写空话；本领域的每个元素写 `responsibility`，说明它负责什么。
- **领域之间**：别的领域的元素画成外部元素，`source` 写「X 领域」，只列被用到的方法。不要在两个领域间画双向依赖。
- **目标模型**：`description` 写明是目标领域模型；每个元素写 `source`，说明现在由哪段代码承担，代码里没有对应的写「无：…」，不编类名。
- **文件名**：`code-<component 短名>-<领域短名>.html`；标题在 component 名后加领域名，如「代码图：Agent 平台 - Agent 服务 - 沙箱 - 通用供给」。

### 表结构与 ER 图（可选）

这个 component 或领域有自己的数据表时，用 `tables` 写表结构。图下方会多出「ER 图」和「表结构」两节；不要再把表画成 `class` 放进类图。

- **只写本领域拥有的表**：别的领域的表、只在内存或缓存里的数据（如 Redis 键、进程内缓存）不写。
- **和类图对上**：表的 `description` 写它对应哪个聚合根或实体；仓储元素的 `responsibility` 写它读写哪几张表。
- **引用**：列的 `references` 写成「表.列」，ER 图按它画连线；被引用的列应是 PK 或 UK。项目不建外键约束时，在 `schemaNote` 里写明连线是逻辑引用。
- **键**：`key` 取 PK、UK、FK，一列只写一个；复合唯一写在 `indexes` 里。
- **约定**：主键、类型写法、是否建外键按项目已有的约定，在 `schemaNote` 里写一次，不在每张表里重复。
- **ER 图的端点**：被引用的一端，列不可空画「有且只有一个」，可空画「零或一个」；被引用的行可能已经不在（如日志指向已删除的实例）时，给列写 `referenceOptional: true`，也画「零或一个」。引用的一端，列是 UK 画「零或一个」，否则画「零或多个」。可为空的列在 ER 图里用斜体。

## 关系

用 UML 类图的线型，`type` 取值：

| `type` | 画成 | 含义 | `description` |
|---|---|---|---|
| `implements` | 虚线 + 空心三角 | 类实现接口 | 可不写 |
| `extends` | 实线 + 空心三角 | 继承（类继承类，或接口继承接口） | 可不写 |
| `uses` | 虚线 + 开口箭头 | 依赖：创建、调用、作为参数使用 | 必写，如 creates、executes |
| `association` | 实线 + 开口箭头 | 关联：长期持有对方的引用 | 必写 |
| `composition` | 实线，源端实心菱形 | 组合：源端拥有目标端，同生共死 | 必写 |
| `aggregation` | 实线，源端空心菱形 | 聚合：源端包含目标端，目标可独立存在 | 必写 |

- 依赖、关联类关系必须写 `description`，说清做什么。只靠线型表达不了「为什么连」。
- 数量有意义时写 `multiplicity`，显示在目标端附近，如 `1..20`、`0..*`。
- 箭头从使用方指向被使用方；继承和实现从子类型指向父类型。

## 图面要求

模板已经自动完成以下几项，只需在数据里给齐内容：

- **标题**：图类型 + 系统名 + container 名 + component 名，如「Code View: Internet Banking System - Backend - Core Banking System Adapter」或「代码图：Agent 平台 - Agent 服务 - 事件中继」，语言和图上文字保持一致。
- 每个元素显示类型图标（C / I / A / E / F）、可见性符号、名字；接口和抽象类的名字用斜体。
- 包框左上角写包名；没写 `package` 时写「component 名 [Component]」。
- 写了 `stereotype` 的元素在名字上方显示 «构造型»；写了 `source` 的在名字下方用灰字显示「现状：…」。
- 图下方依次是「概述」（`overview`）、「对外能力」（`capabilities`）、「设计决策」（`decisions`）、「元素职责」（元素的 `responsibility`）、「ER 图」和「表结构」（`tables`），没写的部分不显示；表格里没人写的列不显示。
- 图下方有图例：用到的元素类型、外部元素、构造型和现状、可见性符号、关系线型各一行。

## 样式（模板内置）

- 按 UML 类图画，黑白为主：白底黑线，类框分标题栏、属性栏、方法栏，空栏保留一条窄栏。
- 类型图标：白底圆圈里写字母，C 类、I 接口、A 抽象类、E 枚举、F 函数。
- 可见性：类型名前用实心彩色符号，成员前用空心符号。`+` 圆（public）、`-` 方块（private）、`#` 菱形（protected）、`~` 三角（package）。
- 外部元素：灰色线条和文字，画在包框外。
- 关系：线型见「关系」一节，说明文字放在线的中点，白底盖住线。
- 标题在左下角，图例在图的下方。

## 数据格式

模板里只改 `<script type="application/json" id="c4-model">` 这一段：

```jsonc
{
  "title": "Code View: Internet Banking System - Backend - Core Banking System Adapter",   // 必填
  "description": "一行补充说明",                    // 可选，显示在标题下
  "component": "Core Banking System Adapter",       // 必填：被展开的 component，和 Component 图里的名字一致
  "package": "com.bigbank.ib.component.corebankingsystem",   // 可选：包框标签页上的包名 / 模块名
  "overview": "一段话说明",         // 可选：图下方的「概述」，空行分段
  "elements": [
    {
      "id": "conn",                // 必填，唯一，关系里用它引用
      "kind": "class",             // class | interface | abstract | enum | function
      "name": "CoreBankingSystemConnection",
      "visibility": "~",           // 可选：类型本身的可见性，+ - # ~
      "stereotype": "仓储",        // 可选：DDD 角色，显示为 «仓储»，不用自己写书名号
      "source": "SandboxRoutes",   // 可选：目标模型里这个元素现在由哪段代码承担，显示为「现状：…」
      "responsibility": "归属键 → 端点的缓存",   // 可选：职责，显示在图下方的「元素职责」表
      "attributes": [["-", "Customer customer"]],          // 可选：[可见性, 文本]，可见性取 + - # ~ 或 ""
      "methods": [["~", "Response execute(Request)"]],     // 可选：同上
      "at": [0.6, 2],              // 网格坐标 [列, 行]，可以用小数
      "external": true             // 可选：不在本 component 里（实现的外部接口、继承的外部基类），画在包框外
    }
  ],
  "relationships": [
    { "from": "impl", "to": "conn", "type": "uses",   // 不写 type 按 uses
      "description": "initialises and pools",         // uses、association、composition、aggregation 必填
      "multiplicity": "1..20",                        // 可选：目标端的数量
      "labelAt": 0.5 }                                // 可选：说明文字在线上的位置，0 = 起点，1 = 终点
  ],
  "capabilities": [                // 可选：对外能力，按领域画时必写；图下方显示成表格
    { "name": "确保沙箱",          // 必填：能力
      "entry": "ensure(owner, user, req, *, fresh)",   // 必填：入口方法或接口
      "callers": "原生沙箱、会话 Pod",                  // 可选：调用方
      "description": "锁内复用或新建，等就绪后登记路由", // 可选：语义和约束
      "source": "Provisioner._provision" }             // 可选：现状代码
  ],
  "decisions": [                   // 可选：设计决策，图下方显示成表格
    { "decision": "查了再建，放在归属键锁里",           // 必填
      "reason": "创建时不能指定 id，并发会建出两个" }   // 必填
  ],
  "schemaNote": "主键约定、是否建外键等，表结构一节开头显示一次",   // 可选
  "tables": [                      // 可选：本领域拥有的表，图下方显示 ER 图和表结构
    {
      "name": "sandbox_instance",                     // 必填，唯一
      "description": "对应聚合根 SandboxInstance",    // 可选
      "at": [1, 0],                                   // 可选：ER 图里的位置，单位同元素的 at；不写按顺序横排
      "columns": [                                    // 必填，非空
        { "name": "id", "type": "BigInteger",         // 必填
          "key": "PK",                                // 可选：PK | UK | FK
          "nullable": false,                          // 可选，默认 false
          "default": "自增",                          // 可选
          "references": "sandbox_template.template_uuid",   // 可选：「表.列」，ER 图按它连线
          "referenceOptional": false,                 // 可选：被引用的行可能已不在（日志、审计这类表），ER 图被引用端画「零或一个」
          "description": "库内主键" }                // 可选
      ],
      "indexes": [                                    // 可选
        { "name": "uk_sandbox_instance_uuid", "columns": ["sandbox_uuid"], "unique": true, "description": "…" }
      ]
    }
  ]
}
```

模板自带的示例是 c4model.com 的网银「Core Banking System Adapter」代码图，数据和原图一致，只调整了坐标。

## 布局

- `at` 一个单位 = 横向 440px、纵向 230px，指元素中心。元素宽高按内容自动算：宽度看名字和最长的成员，高度看成员数量。
- 接口和抽象类型放在上面，实现放在下面；依赖关系大致从上往下指。
- 实现同一个接口的类放在同一行，接口放在它们上方居中。
- 外部元素放在包框外、靠近使用它的元素，不要挡住包框左上角的标签页。
- 名字或签名很长的元素会很宽，左右间距不够时加大列坐标的差值。
- 连线穿过其他元素时，挪动元素位置，不要靠移动说明文字掩盖。说明文字压到其他东西时，先挪元素，再用 `labelAt` 微调。
- 元素超过 15 个时，只画和要讲的事相关的，或者拆成几张图。

## 校验结果（`<html data-c4-status>`）

| 状态 | 含义 |
|---|---|
| `error` | 数据不合规：缺字段、`kind` 或 `type` 取值不对、成员没写成 `[可见性, 文本]`、`stereotype` / `source` 不是字符串、对外能力缺 `name` 或 `entry`、设计决策缺 `decision` 或 `reason`、`implements` 的目标不是接口、`extends` 跨了类和接口、依赖和关联类关系缺 `description`、元素重叠、外部元素落进包框；表缺 `name` 或 `columns`、列缺 `name` 或 `type`、`key` 取值不对、`references` 指向不存在的表或列、表在 ER 图里重叠等 |
| `warn` | 版面问题：连线穿过元素、说明文字压到元素、说明文字互相重叠；元素超过 15 个；某个元素没有任何关系；有元素写了 `responsibility`，而本 component 里另有元素没写；ER 图的连线穿过别的表；`references` 引用的列不是 PK 或 UK |
| `ok` | 全部通过；页面顶部不显示提示框 |

每条问题会以 `<li data-level="error|warn">` 的形式显示在页面顶部的提示框里。
