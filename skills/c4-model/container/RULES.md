# Level 2：Container

## 这张图回答什么

把 System Context 图上那一个系统框打开，看里面由哪些 container 组成：

1. 系统整体是什么形状？
2. 主要的技术选型是什么？
3. 职责怎么分布在各个 container 上？
4. container 之间怎么通信？
5. 作为开发者，实现一个功能要去改哪里？

读者是团队内外的技术人员：架构师、开发者、运维和支持人员。它是 System Context 图和 Component 图之间的一层，只画到 container 为止，不展开 container 内部。

## 范围与元素

| 项 | 内容 |
|---|---|
| Scope | 一个 software system，画成包住全部 container 的系统边界 |
| Primary elements | 这个系统里的 containers |
| Supporting elements | 和这些 container **直接相连**的 people、software systems |

### Container 怎么判定

- container 是一个 **application 或 data store**：整个系统要正常工作，它就必须在运行。本质上它是一个运行时边界，里面是正在执行的代码或存着的数据。和 Docker 无关。
- 常见的 container：
  - 应用类：服务端 web 应用、在浏览器里运行的单页应用、桌面应用、移动 App、服务端命令行程序或批处理、单个 serverless 函数、shell 脚本。
  - 存储类：数据库 schema、对象存储桶或 CDN、文件系统。
- 服务端 web 应用主要输出静态 HTML 时，算一个 container。它还下发大量 JavaScript 时（比如 Angular 单页应用），算服务端和浏览器端两个 container，因为这是两个进程空间，要跨进程通信。
- **不是** container：JAR、DLL、模块、包、纯代码库（比如被多个服务引用的 `libs/`）。它们只是组织代码的方式，归属于使用它们的 container。
- 托管的存储服务（S3、RDS、OSS）虽然不由自己运维，但 bucket 和 schema 归本系统所有，所以画成 container。
- 业务关键数据存在文件系统里时，画一个 File System container。只用来写日志的文件系统不画。
- 别的团队拥有、本系统只是调用的东西，仍然是外部 software system，不是 container。

### 每个 container 记录

- **name**：如「Backend」「Database」。
- **technology**：实现技术，如「Java and Spring Boot」「MySQL Database Schema」。前期设计还没定下来时，写候选清单或大类（如「关系型数据库」）。标准关系型数据库、没用专有特性的，可以只写大类。
- **description**：一句话说清职责；存储类可以写主要存什么（实体、表、文件）。

### 人员和外部系统

- 和同系统的 System Context 图保持一致：人员和外部系统相同，名字、描述、`tag` 都相同，并按相同的先后顺序列出，这样自动配色才会一致。
- Context 图里连到 scope 系统的关系，在这里要改为连到具体的 container。
- 只画和某个 container 直接相连的人员和系统。

### 系统边界

模板会按全部 container 自动画出系统边界，名字取 `system` 字段，必须和 Context 图上 scope 系统的名字相同。

### 不画什么

- 部署细节：实例数量、集群、负载均衡、复制、failover、各环境之间的差异。这些属于 deployment 图。
- container 内部的组件和类。这些属于 Level 3 / Level 4。

## 关系

- container 之间通常是跨进程通信，**每条线都要标注**，不允许没有说明的线。
- `description` 写交互目的，如「Reads from and writes to」「发布运行事件到」。
- `technology` 写通信机制和协议，如「JSON/HTTPS」「SQL/TCP」「gRPC」「AMQP」「Redis Streams」。container 发起的关系必须写；人员发起的关系可以不写。
- 通信方式（同步、异步、批量）有意义时，写进 `technology` 或 `description`。
- 详细程度按用途来：前期设计时只写已经知道的；给现有系统写文档时，可以写到协议甚至端口。
- 箭头从发起方指向被调用方，和 Context 图一致。

## 图面要求

模板已经自动完成以下几项，只需在数据里给齐内容：

- **标题**：图类型 + 范围，如「Container View: Internet Banking System」或「容器图：网上银行系统」，语言和图上文字保持一致。
- 每个元素显示类型（`[Person]` / `[Software System]` / `[Container: 技术]`）和描述。
- 系统边界左下角写系统名和 `[Software System]`。
- 图下方有图例（Diagram key），每种 container 形状各占一项。
- 缩写尽量不用；要用的话，在描述里解释。

## 样式（模板内置）

- 白底，彩色描边 + 同色文字。软件系统画成圆角矩形，人员画成圆头 + 圆角身体，container 按 `shape` 画（见「数据格式」里的形状表）。
- 颜色：全部 container 和系统边界都用蓝色 `#1168bd`，其他元素不用蓝色。人员依次用 `#297e06`、`#1a849c`……，外部系统依次用 `#ed8609`、`#bf101d`、`#8411bd`……，按在 `elements` 里出现的顺序分配。`tag` 相同的元素颜色相同。
- 系统边界：蓝色实线圆角框。
- 关系：`#444` 虚线 + 实心三角箭头，标签放在线的中点，白底盖住线；有 `technology` 时，下面多一行小字 `[技术]`。
- 标题在左下角，图例在图的下方。

## 数据格式

模板里只改 `<script type="application/json" id="c4-model">` 这一段：

```jsonc
{
  "title": "Container View: Internet Banking System",   // 必填
  "description": "一行补充说明",                         // 可选，显示在标题下
  "system": "Internet Banking System",                  // 必填：范围系统的名字，画成系统边界
  "elements": [
    {
      "id": "backend",             // 必填，唯一，关系里用它引用
      "kind": "container",         // person | system | container
      "shape": "server",           // 仅 container，不写按 box，取值见下表
      "technology": "Java and Spring Boot",   // container 必填
      "name": "Backend",
      "description": "Provides Internet banking functionality via a JSON/HTTP API.",
      "at": [1, 2.4],              // 网格坐标 [列, 行]，可以用小数
      "tag": "Customer",           // 可选：图例上的文字，tag 相同则颜色相同
      "color": "#297e06"           // 可选：覆盖自动配色，一般不用
    }
  ],
  "relationships": [
    { "from": "ui", "to": "backend", "description": "Makes API requests to",
      "technology": "JSON/HTTP",   // container 发起的关系必填
      "labelAt": 0.5 }             // 可选：标签在线上的位置，0 = 起点，1 = 终点
  ]
}
```

不写 `scope`，也不写 `boundary`：系统边界由 `system` 字段和全部 container 自动生成。

| `shape` | 画成 | 用于 |
|---|---|---|
| `box` | 圆角框 | 默认；归不进下面几类时用 |
| `server` | 左上角带 `>_` 的方框 | 服务端应用、后台服务、批处理、serverless 函数 |
| `browser` | 浏览器窗口 | 在浏览器里运行的前端（单页应用） |
| `database` | 圆柱 | 数据库 schema |
| `bucket` | 桶 | 对象存储桶、CDN |
| `folder` | 文件夹 | 文件系统、目录 |
| `queue` | 横放的管道 | 消息队列、topic、事件流 |

模板自带的示例是 c4model.com 的网银 Container 图。原图里「Static Content → UI」没写技术，示例补了 `HTTPS`，因为本技能要求 container 发起的关系必须写技术。

## 布局

- `at` 一个单位 = 横向 560px、纵向 420px，指元素主体（人员是身体）的中心。元素宽 300px，高约 200px，文字多时自动加高。
- 人员放在系统边界上方；container 放在边界里；外部系统放在边界右侧或下方。
- 边界里按调用方向从上到下摆：入口和前端在上，服务在中间，存储在下。
- 人员连多个 container 时，放在这些 container 的上方居中。
- 元素少时参照模板自带的示例：前端一行 `[0,1.3]`、`[1,1.3]`，服务 `[1,2.4]`，存储一行 `[0.4,3.5]`、`[1.6,3.5]`，人员 `[0.5,0]`，外部系统在右侧 `[2.4,1.0]`、`[2.4,2.4]`。
- 连线尽量水平、竖直或短斜线。连线穿过其他元素时，挪动元素位置，不要靠移动标签掩盖。
- 标签压到其他东西时，先挪元素，再用 `labelAt` 微调。
- 系统边界是全部 container 的外接矩形外扩 40px（底部再多留 30px 写名字），所以 container 在网格上要排成连续的一块，人员和外部系统放在这块外面。
- container 多到十几个时，先回头检查是否把代码模块或部署实例当成了 container。

## 校验结果（`<html data-c4-status>`）

| 状态 | 含义 |
|---|---|
| `error` | 数据不合规：缺字段、container 缺 technology、container 发起的关系缺 technology、写了 `scope` 或 `boundary`、人员或外部系统没和任何 container 直接相连、元素重叠、非 container 落进系统边界等 |
| `warn` | 版面问题：连线穿过元素、标签压到元素、标签互相重叠；某个 container 没有任何关系 |
| `ok` | 全部通过；页面顶部不显示提示框 |

每条问题会以 `<li data-level="error|warn">` 的形式显示在页面顶部的提示框里。
