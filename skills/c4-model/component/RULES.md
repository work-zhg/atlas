# Level 3：Component

## 这张图回答什么

把 Container 图上的一个 container 打开，看里面由哪些 component 组成：

1. 这个 container 由哪些 component 组成？
2. 每个 component 都有归属吗（都落在某个 container 里）？
3. 软件在高层上是怎么工作的，看得清楚吗？

它展示 container 被拆成哪些各有职责的 component、component 之间的依赖，以及实现层面的概要（用了哪些框架和库）。如果图上的 component 能和代码一一对应，它就是理解代码结构的好入口。

读者是开发团队内部的技术人员：架构师和开发者。

**这一层是可选的。** 只在确实有帮助时画：数据存储类 container（数据库、文件系统、对象存储）和很简单的 container（如单一职责的微服务）通常不画；单体应用、前期设计时值得画。component 多了图会很快变乱，长期维护的文档要考虑维护成本。

## 范围与元素

| 项 | 内容 |
|---|---|
| Scope | 一个 container，画成包住全部 component 的 container 边界 |
| Primary elements | 这个 container 里的 components |
| Supporting elements | 同系统里的其他 containers，以及和这些 component **直接相连**的 people、software systems |

### Component 怎么判定

- component 是**封装在清晰接口后面的一组相关功能**。在 Java、C# 这类语言里，可以理解为一个接口加一组实现类。
- component **不能单独部署**，部署单元是 container；同一个 container 里的所有 component 运行在同一个进程里。
- 按语言理解：
  - 面向对象语言：一组类和接口；
  - 过程式语言：比如某个目录下的几个 C 文件；
  - JavaScript：一个模块；
  - 函数式语言：一个 module。
- JAR、DLL、模块、包、命名空间、目录**通常不是** component，它们是组织代码的方式。有时会一一对应（比如六边形架构里每个 component 一个 JAR），但不要默认如此。
- 从代码里找 component 的做法：
  1. 先去掉纯数据结构（domain / model 类）和工具类；
  2. 剩下有架构意义的类和接口，按功能分组。比如每个 web controller 各算一个 component，一个 service 接口连同它的实现类合成一个 component。
- 图要反映实际在用的架构风格（分层、六边形或别的）。

### 每个 component 记录

- **name**：如「Sign In API」「Security Component」。
- **technology**：实现技术，如「Spring MVC Controller」「Spring Bean」「FastAPI Router」「Python 模块」。前期设计时写已知的部分即可。
- **description**：一句话说清职责。

### 基础设施组件和横切关注点

日志这类被大多数 component 使用的组件，全画出来会让图很乱。二选一：

1. 不画，它对讲清楚这张图没有帮助时；
2. 不画，但在 `description`（标题下那行）里写一句，如「所有组件都通过 Logging Component 写日志」。

不要画成一个没有连线的孤立组件，模板会对孤立组件给出告警。

### 共享组件和库

画出来有助于讲清楚就画。要标明它来自共享库时，给它写 `tag`（如「共享库」）和 `color`（如 `#5b6770`），图例里会单独列一项。

### 一个接口多种实现

比如存储组件同时有 SQL Server 和 MySQL 两种实现、部署时按配置选。三种画法：

1. 只画一个 component，描述职责，忽略有多种实现；
2. 画一个 component，在描述里写明实现可替换、由什么决定；
3. 每种实现组合各画一张图。只适合组合很少的情况；如果某种实现还会引入别的组件，适合用这种画法。

### 人员、软件系统和其他 container

- 和同系统的 Container 图保持一致：
  - 人员和外部系统同名、同描述、同 `tag`，按相同的先后顺序列出；
  - 其他 container 同名、同 `shape`、同 `technology`。
- Container 图里连到这个 container 的关系，在这里要改为连到具体的 component。
- 只画和某个 component 直接相连的元素。

### 边界

模板自动画两层边界：

- 内层 container 边界：包住全部 component，名字取 `container` 字段，必须和 Container 图里的名字相同；
- 外层系统边界：包住内层边界和其他 container，名字取 `system` 字段。

### 不画什么

- component 内部的类和接口，这属于 Level 4。
- 部署细节。
- 和这个 container 无关的其他 container。

## 关系

- **每条线都要标注**，`description` 写交互目的，如「Validates credentials using」「持久化数据到」「委派给」。
- component 之间是进程内调用，`technology` 可以不写；异步（事件、队列、后台任务）或有特别机制时写上，如「asyncio 任务」「事件总线」。
- component 和其他 container、外部系统之间是跨进程调用，`technology` 必须写，如「JSON/HTTPS」「SQL/TCP」。
- 通信方式（同步、异步）有意义时写进 `technology` 或 `description`。
- 详细程度按用途来：前期设计时只写已经知道的；给现有系统写文档时，可以写到所用的框架。
- 箭头从调用方指向被调用方。

## 图面要求

模板已经自动完成以下几项，只需在数据里给齐内容：

- **标题**：图类型 + 系统名 + container 名，如「Component View: Internet Banking System - Backend」或「组件图：网上银行系统 - 后端」，语言和图上文字保持一致。
- 每个元素显示类型和描述：component 是 `[Component: 技术]`，container 是 `[Container: 技术]`。
- 两层边界的左下角分别写 `[Container]`、`[Software System]` 和名字。
- 图下方有图例（Diagram key）。
- 缩写尽量不用；要用的话，在描述里解释。

## 样式（模板内置）

- 白底，彩色描边 + 同色文字。component 画成左侧带两个小矩形的方框；container 按 `shape` 画；软件系统画成圆角矩形；人员画成圆头 + 圆角身体。
- 颜色：component、container 和两层边界都用蓝色 `#1168bd`，其他元素不用蓝色。人员依次用 `#297e06`、`#1a849c`……，外部系统依次用 `#ed8609`、`#bf101d`、`#8411bd`……，按在 `elements` 里出现的顺序分配。`tag` 相同的元素颜色相同。
- 边界：两层都是蓝色实线圆角框。
- 关系：`#444` 虚线 + 实心三角箭头，标签放在线的中点，白底盖住线；有 `technology` 时，下面多一行小字 `[技术]`。
- 标题在左下角，图例在图的下方。

## 数据格式

模板里只改 `<script type="application/json" id="c4-model">` 这一段：

```jsonc
{
  "title": "Component View: Internet Banking System - Backend",   // 必填
  "description": "一行补充说明",                                   // 可选，显示在标题下
  "system": "Internet Banking System",    // 必填：范围系统的名字，画成外层边界
  "container": "Backend",                 // 必填：被展开的 container 的名字，画成内层边界
  "elements": [
    {
      "id": "security",            // 必填，唯一，关系里用它引用
      "kind": "component",         // person | system | container | component
      "technology": "Spring Bean", // component、container 必填
      "shape": "database",         // 仅 container，不写按 box，取值见下表
      "name": "Security Component",
      "description": "Provides functionality related to signing in, changing passwords, etc.",
      "at": [1.5, 3.1],            // 网格坐标 [列, 行]，可以用小数
      "tag": "共享库",             // 可选：图例上的文字，tag 相同则颜色相同
      "color": "#5b6770"           // 可选：覆盖自动配色，一般不用
    }
  ],
  "relationships": [
    { "from": "security", "to": "db", "description": "Reads from and writes to",
      "technology": "MySQL protocol",  // 跨进程的关系必填
      "labelAt": 0.5 }                 // 可选：标签在线上的位置，0 = 起点，1 = 终点
  ]
}
```

不写 `scope`，也不写 `boundary`：两层边界由 `system`、`container` 两个字段自动生成。

| container 的 `shape` | 画成 | 用于 |
|---|---|---|
| `box` | 圆角框 | 默认；归不进下面几类时用 |
| `server` | 左上角带 `>_` 的方框 | 服务端应用、后台服务、批处理、serverless 函数 |
| `browser` | 浏览器窗口 | 在浏览器里运行的前端（单页应用） |
| `database` | 圆柱 | 数据库 schema |
| `bucket` | 桶 | 对象存储桶、CDN |
| `folder` | 文件夹 | 文件系统、目录 |
| `queue` | 横放的管道 | 消息队列、topic、事件流 |

模板自带的示例是 c4model.com 的网银 Backend 组件图，数据和原图一致，只调整了坐标。

## 布局

- `at` 一个单位 = 横向 560px、纵向 420px，指元素主体（人员是身体）的中心。元素宽 300px，高约 200px，文字多时自动加高。
- component 放在中间，排成连续的一块；调用其他组件的入口（API、controller）在上，被依赖的服务在中间，访问外部的适配器靠近对应的外部元素。
- 其他 container 放在 component 块的外面、系统边界里面：调用方（如前端）在上，存储在下。
- 外部系统放在最外层边界的右侧；人员放在最上方。
- 元素少时参照模板自带的示例：入口 component 一行 `[0,1.3]`～`[3,1.3]`，中间层 `[0,2.4]`、`[3,2.4]`，公共组件 `[1.5,3.1]`，前端 container `[1,0]`，存储 `[0,4.4]`、`[1.5,4.4]`，外部系统 `[4.4,1.3]`、`[4.4,2.4]`。
- 连线尽量水平、竖直或短斜线。连线穿过其他元素时，挪动元素位置，不要靠移动标签掩盖。
- 标签压到其他东西（包括边界左下角的名字）时，先挪元素，再用 `labelAt` 微调。
- 两层边界都是外接矩形外扩 40px（底部再多留 30px 写名字）：component 要排成连续的一块，其他 container 放在这块外面；人员和外部系统放在所有 component 和 container 的外面。
- component 多到十几个时，图会很乱：只画和要讲的事相关的那部分 component，或者按功能拆成几张图，在 `description` 里说明画的是哪一部分。

## 校验结果（`<html data-c4-status>`）

| 状态 | 含义 |
|---|---|
| `error` | 数据不合规：缺字段、component 或 container 缺 technology、跨进程的关系缺 technology、写了 `scope` 或 `boundary`、支撑元素没和任何 component 直接相连、元素重叠、非 component 落进 container 边界、人员或外部系统落进系统边界等 |
| `warn` | 版面问题：连线穿过元素、标签压到元素或边界名、标签互相重叠；某个 component 没有任何关系 |
| `ok` | 全部通过；页面顶部不显示提示框 |

每条问题会以 `<li data-level="error|warn">` 的形式显示在页面顶部的提示框里。
