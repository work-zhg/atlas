# Level 1：System Context

## 这张图回答什么

1. 我们在建（或已建成）的是什么软件系统？
2. 谁在用它？
3. 它怎样融入现有环境？

这是一张**大局图**：把系统当成一个黑盒，只画它和周围的人、系统。读者包括技术和非技术人员、团队内外。检验标准：能直接拿给非技术人员看。它的价值在于让范围和上下文变明确、不留隐含假设，也是找出「要和哪些团队对接口」的起点。

## 范围与元素

| 项 | 内容 |
|---|---|
| Scope | 一个 software system |
| Primary element | in-scope 系统，画在中间，有且只有一个 |
| Supporting elements | 和它**直接相连**的 people、software systems |

### Software system 怎么判定

- 最高层的抽象：给用户（人或程序）交付价值的东西。组织里可能叫 application、product、service。
- 判定方法：由**一个团队**构建、拥有、负责，并且能看到内部实现；通常在一个代码仓库里、一起部署。团队边界通常就是系统边界。
- 下面这些**不是** software system：product domain、bounded context、business capability、feature team、tribe、squad。
- 本系统内部的东西（web app、API、数据库、消息队列、各个微服务）是 Level 2 container，**不画在这张图上**。常见错误是把自家数据库画成外部系统。
- 不归本团队拥有、负责的系统都是外部系统，包括公司内其他团队的系统。画它们是为了说明「这是依赖，不是我们要建的」。

### Person

- 用户、角色、actor、persona 都可以，按角色写，不写具体人名。
- 公司内部员工（客服、运营、管理员）只要直接使用本系统，也要画。

### 每个元素记录

- **name**：人员写角色名，系统写系统名。
- **description**：一两句话，说清职责或用途，不写技术。

### 企业边界（可选）

系统建在某个组织内部、又和组织外的人或系统打交道时，用虚线框标出组织边界（如「Big Bank plc」），区分内外。不需要区分内外时不画。

## 关系

- **每条线都要标注交互目的**，不允许没有说明的线。
- 不写技术、协议、数据格式（HTTP、REST、JSON、gRPC、JDBC、MQ……），这些是 Level 2 的内容。
- 单向箭头，从发起方（依赖方）指向被使用方。描述和箭头方向一致，按「源 + 描述 + 目标」读要通顺：
  - 「个人客户 —查看余额、发起付款→ 网上银行系统」
  - 英文常以 using / from / to 结尾：「Internet Banking System — *Sends e-mails to customers using* → Email Service」
- 同一对元素之间优先用一条关系概括；只有双方确实各自主动发起时才画两条（模板会自动把两条线平行错开）。
- 支撑元素之间也可以有关系（如「邮件服务 —给…发邮件→ 客户」），但每个支撑元素都必须至少有一条和 scope 系统直接相连的关系；只和 scope 间接相连的系统不画。

## 图面要求

模板已经自动完成以下几项，只需在数据里给齐内容：

- **标题**：图类型 + 范围，如「System Context View: Internet Banking System」或「系统上下文图：网上银行系统」，语言和图上文字保持一致。
- 每个元素显示类型（`[Person]` / `[Software System]`）和描述。
- 图下方有图例（Diagram key）。
- 缩写尽量不用；要用的话，在描述里解释。

## 样式（模板内置）

- 白底，彩色描边 + 同色文字。软件系统画成圆角矩形，人员画成圆头 + 圆角身体。
- 颜色：in-scope 系统固定为蓝色 `#1168bd`，其他元素不用蓝色；人员依次用 `#297e06`、`#1a849c`……；外部系统依次用 `#ed8609`、`#bf101d`、`#8411bd`……。`tag` 相同的元素颜色相同。
- 关系：`#444` 虚线 + 实心三角箭头，标签放在线的中点，白底盖住线。
- 标题在左下角，图例在图的下方。

## 数据格式

模板里只改 `<script type="application/json" id="c4-model">` 这一段：

```jsonc
{
  "title": "System Context View: Internet Banking System",   // 必填
  "description": "一行补充说明",                               // 可选，显示在标题下
  "elements": [
    {
      "id": "ibs",               // 必填，唯一，关系里用它引用
      "kind": "system",          // person | system
      "scope": true,             // 只给 in-scope 系统写，全图有且只有一个
      "name": "Internet Banking System",
      "description": "Allows customers to view information about their bank accounts and make payments via the web.",
      "at": [0, 1],              // 网格坐标 [列, 行]，可以用小数
      "tag": "Customer",         // 可选：图例上的文字，tag 相同则颜色相同
      "color": "#297e06"         // 可选：覆盖自动配色，一般不用
    }
  ],
  "relationships": [
    { "from": "customer", "to": "ibs", "description": "Views account balances and makes payments using",
      "labelAt": 0.5 }           // 可选：标签在线上的位置，0 = 起点，1 = 终点
  ],
  "boundary": { "name": "Big Bank plc", "members": ["ibs", "core", "staff"] }   // 可选：企业边界
}
```

## 布局

- `at` 一个单位 = 横向 560px、纵向 420px，指元素主体（人员是身体）的中心。元素宽 300px，高约 200px，文字多时自动加高。
- in-scope 系统放中间；人员放在上一行；外部系统放左右两侧和下方。人员不要放在系统正下方，头部会挤占标签空间。
- 元素少时参照模板自带的示例：人员 `[0,0]`，scope `[0,1.1]`，外部系统在右侧 `[1.2,0.65]`、`[1.2,1.55]` 上下错开。
- 连线尽量水平、竖直或短斜线。连线穿过其他元素时，挪动元素位置，不要靠移动标签掩盖。
- 标签压到其他东西时，先挪元素，再用 `labelAt` 微调。
- 企业边界：边界框是成员外接矩形外扩 40px，所以成员在网格上要排成连续的一块，非成员放在这块外面。
- 支撑元素多到十几个时，先回头检查是否混进了 container，或者混进了只和 scope 间接相连的系统。

## 校验结果（`<html data-c4-status>`）

| 状态 | 含义 |
|---|---|
| `error` | 数据不合规：缺字段、scope 不唯一、支撑元素没和 scope 直接相连、元素重叠、非成员落进边界框等 |
| `warn` | 版面问题：连线穿过元素、标签压到元素、标签互相重叠 |
| `ok` | 全部通过；页面顶部不显示提示框 |

每条问题会以 `<li data-level="error|warn">` 的形式显示在页面顶部的提示框里。
