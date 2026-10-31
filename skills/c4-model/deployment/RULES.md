# Deployment

## 这张图回答什么

静态模型里的软件系统和 container，在**某一个部署环境**里各自跑在哪些基础设施上：运行在什么节点上、跑几个实例、经过哪些 DNS、负载均衡和专线、彼此怎么连。

过去基础设施团队画网络图，开发团队画架构图，两边各说各的。Deployment 图把两者合到一张图上：既有基础设施，也有部署在上面的应用和数据存储。即使部署已经全自动，一张汇总部署映射的图依然有用。

读者是团队内外的技术人员：架构师、开发者、基础设施架构师、运维和支持人员。**推荐画**，每个重要环境一张。

## 范围与元素

| 项 | 内容 |
|---|---|
| Scope | 一个部署环境（Live、Staging、Development 等）里的一个或多个软件系统 |
| Primary elements | 部署节点、容器实例、软件系统实例 |
| Supporting elements | 部署要用到的基础设施节点：DNS、负载均衡、防火墙、网络专线等 |

### 部署节点（`kind: node`）

部署节点是软件系统或 container 实例运行的地方，可以层层嵌套。常见的几类：

- 物理设施：物理服务器、设备、用户的电脑；
- 虚拟化设施：IaaS、PaaS、虚拟机、云区域（如「eu-west-1」）；
- 容器化设施：Docker 容器、K8s Pod、命名空间；
- 执行环境：数据库服务器、应用服务器、JVM、浏览器。

每个节点记录名字、技术（如「Ubuntu 24.04 LTS」「Docker Container」「MySQL 8.4 LTS」），有多个实例时写实例数（如 `1..10`、`3`）。

### 分组（`kind: group`）

用来圈出归属方或供应商，如「Amazon Web Services」「Cloudflare」「Big Bank」。分组不是运行环境，不写技术。

### 容器实例、软件系统实例

- 容器实例必须是 Container 图里已有的 container：名字、`shape`、`technology`、描述都一致，并写明部署在哪个节点上（`in`）。
- 别人的系统画成软件系统实例；颜色和静态图一致（按相同的先后顺序列出，或者直接写 `color`）。
- 同一个 container 在一个环境里跑多份时，把实例数写在承载它的节点上（`instances`），不要把 container 画多次。主备、复制这类拓扑例外：分别画在各自的节点上，并用关系说明复制方向。

### 基础设施节点（`kind: infrastructure`）

DNS 记录、负载均衡、防火墙、网关、网络专线等。需要时可以把主机名、IP、端口写进名字或描述，只要有助于讲清这张图要讲的事。

### 只画基础设施是不够的

云架构图常见的问题是只画用了哪些云服务（Route 53、ELB、EC2、RDS），这只讲了一半。要同时画出**自己的应用和数据存储部署在这些服务的哪里**。Docker、K8s 环境同理：不要只画集群、节点和 Pod，要画出哪个 container 跑在哪里。

## 关系

- 实例之间的关系沿用 Container 图的关系，但落到具体环境：写这个环境里实际用的协议，包括 TLS 等细节，如「MySQL protocol/TLS」「JSON/HTTPS」。
- 基础设施之间的关系也要画：DNS「Is an alias for」负载均衡、负载均衡「Forwards API requests to」后端等。关系的端点可以是部署节点，比如 DNS 记录指向一个 S3 桶节点。
- 容器实例发起的关系必须写 `technology`；DNS 别名这类基础设施关系可以不写。
- 走专线或内网直连的关系写 `private: true`，画成实线，和经过公网的虚线区分开。
- 每条线都要标注 `description`，按「源 + 描述 + 目标」读要通顺。

## 图必须反映现实

- **按实际部署画**：节点名、技术、版本、实例数和部署清单（K8s YAML、Terraform、docker-compose、部署脚本）一致，不要画成「理想中的部署」。
- **共享库不是独立节点**：被打包进多个应用的共享组件，随应用一起部署，不要画成一个单独的节点或服务。
- **一个环境一张图**：开发、测试、生产的拓扑不同，不要混在一张图里；Container 图有意不画的集群、副本、failover、复制，都在这里按环境画清楚。

## 图面要求

模板已经自动完成以下几项，只需在数据里给齐内容：

- **标题**：图类型 + 系统名 + 环境，如「Deployment View: Internet Banking System - Live」或「部署图：Agent 平台 - 生产环境」，语言和图上文字保持一致。
- 部署节点左下角写「名字」和 `[Deployment Node: 技术]`，有实例数时右下角写 `x实例数`；分组左下角只写名字。
- 元素显示类型：`[Container: 技术]`、`[Software System]`、`[Infrastructure Node: 技术]`。
- 图下方有图例：每种实例和基础设施节点、部署节点、分组、两种关系线型。
- 用了云厂商图标的话，图标必须出现在图例里。本模板不画图标，用技术名称说明。

## 样式（模板内置）

- 白底。容器实例沿用 Container 图的形状和蓝色 `#1168bd`；软件系统实例是彩色圆角矩形（按出现顺序依次 `#ed8609`、`#bf101d`、`#8411bd`……）；基础设施节点是灰色椭圆。
- 部署节点是灰色细线圆角框；分组是灰色点线框。节点名画在连线上层、带白底，连线从字底下穿过。
- 关系：`#444` 虚线 + 实心三角箭头；`private: true` 时是实线。标签在线的中点，有 `technology` 时下面多一行小字 `[技术]`。
- 标题在左下角，图例在图的下方。

## 数据格式

模板里只改 `<script type="application/json" id="c4-model">` 这一段：

```jsonc
{
  "title": "Deployment View: Internet Banking System - Live",   // 必填
  "description": "一行补充说明",                                 // 可选，显示在标题下
  "environment": "Live",                  // 必填：部署环境
  "nodes": [                              // 部署节点和分组，用 in 表示嵌套；框根据里面的内容自动计算
    { "id": "aws", "kind": "group", "name": "Amazon Web Services" },
    { "id": "region", "kind": "node", "name": "eu-west-1", "technology": "Amazon Web Services Region", "in": "aws" },
    { "id": "backendContainer", "kind": "node", "name": "Backend Container", "technology": "Docker Container",
      "instances": "1..10",               // 可选：实例数
      "in": "region" }
  ],
  "elements": [                           // 容器实例、软件系统实例、基础设施节点
    {
      "id": "backend",                    // 必填，唯一（和节点 id 也不能重复）
      "kind": "container",                // container | system | infrastructure
      "shape": "server",                  // 仅 container，取值同 Container 图：box / server / browser / database / bucket / folder / queue
      "technology": "Java and Spring Boot", // container 必填
      "name": "Backend",
      "description": "Provides Internet banking functionality via a JSON/HTTP API.",
      "in": "backendContainer",           // 部署在哪个节点上；container 必填
      "at": [1, 3.55],                    // 网格坐标 [列, 行]，可以用小数
      "scope": true,                      // 可选：仅 system，in-scope 系统画成蓝色
      "tag": "同级服务",                   // 可选：图例文字，tag 相同颜色相同
      "color": "#8411bd"                  // 可选：覆盖自动配色
    }
  ],
  "relationships": [
    { "from": "backend", "to": "db", "description": "Reads from and writes to",
      "technology": "MySQL protocol/TLS", // 容器实例发起的关系必填
      "private": true,                    // 可选：专线 / 内网直连，画成实线
      "labelAt": 0.5 }                    // 可选：标签在线上的位置，0 = 起点，1 = 终点
  ]
}
```

关系的 `from`、`to` 可以写元素 id，也可以写部署节点 id。

模板自带的示例是 c4model.com 的网银 Live 部署图，数据和原图一致。只调整了坐标，并给「Static Content → UI」补了 `HTTPS`（容器实例发起的关系必须写技术）。

## 布局

- `at` 一个单位 = 横向 560px、纵向 420px，指元素中心。元素宽 300px，高约 200px，文字多时自动加高。
- 节点框 = 里面所有元素和子节点的外接矩形，四周外扩 24px，底部再留 44px 写名字。**每多一层嵌套，元素之间就要多留一圈空间**：嵌套越深，元素之间的列、行坐标差要越大。
- 按访问路径从上到下摆：用户设备在上，DNS、负载均衡在中间，应用居中，数据存储在下；别的组织的系统（如自建机房里的核心系统）放在侧边的分组里。
- 同一个节点里的元素在网格上排成连续的一块，不属于它的元素放在它的框外。
- 连线尽量水平、竖直或短斜线。连线穿过其他元素时，挪动元素位置；标签压到其他东西（包括节点名）时，先挪元素，再用 `labelAt` 微调。

## 校验结果（`<html data-c4-status>`）

| 状态 | 含义 |
|---|---|
| `error` | 数据不合规：缺 `title` 或 `environment`、节点 `kind` 不对、节点嵌套成环、节点里没有任何内容、容器实例缺 `technology` 或 `in`、元素缺 `description`、元素落进不属于它的节点框、两个互不包含的节点框重叠、关系端点不存在、关系缺 `description`、容器实例发起的关系缺 `technology`、元素重叠 |
| `warn` | 版面问题：连线穿过元素、标签压到元素或节点名、标签互相重叠；某个元素没有任何关系 |
| `ok` | 全部通过；页面顶部不显示提示框 |

每条问题会以 `<li data-level="error|warn">` 的形式显示在页面顶部的提示框里。
