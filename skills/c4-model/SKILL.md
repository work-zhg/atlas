---
name: c4-model
description: 用 C4 model 画软件架构图，输出单文件 HTML；支持全部四层（System Context、Container、Component、Code）、sequence 风格的 Dynamic 图和 Deployment 图。Use when the user asks for a C4 diagram, a system context diagram / 系统上下文图, a container diagram / 容器图, a component diagram / 组件图, a code diagram / 代码图 / UML class diagram of one component, a dynamic or sequence diagram / 动态图 / 时序图 of one feature, a deployment diagram / 部署图 of one environment, or a big-picture, technology or code-structure view of one software system.
---

# C4 model 图

C4 按层级逐级放大：Level 1 System Context → Level 2 Container → Level 3 Component → Level 4 Code，这四层画静态结构。补充的 Dynamic 图画运行时一个场景里的交互，本技能只画它的 sequence 风格；Deployment 图画一个环境里的实例部署在哪些基础设施上。每种图有自己的规则和模板，不要拿一种图的模板画另一种。

| 层级 | 规则 | 模板 | 输出文件名 |
|---|---|---|---|
| System Context | [context/RULES.md](context/RULES.md) | [context/template.html](context/template.html) | `system-context.html` |
| Container | [container/RULES.md](container/RULES.md) | [container/template.html](container/template.html) | `container.html` |
| Component | [component/RULES.md](component/RULES.md) | [component/template.html](component/template.html) | `component-<container 短名>.html`，每个 container 一张 |
| Code | [code/RULES.md](code/RULES.md) | [code/template.html](code/template.html) | `code-<component 短名>.html`，每个 component 一张 |
| Dynamic（sequence） | [dynamic/RULES.md](dynamic/RULES.md) | [dynamic/template.html](dynamic/template.html) | `dynamic-<场景短名>.html`，每个场景一张 |
| Deployment | [deployment/RULES.md](deployment/RULES.md) | [deployment/template.html](deployment/template.html) | `deployment-<环境短名>.html`，每个环境一张 |

## 步骤

1. **定层级和范围**：确定画哪种图、唯一的 in-scope software system 是哪个。图要反映真实代码时，先查仓库证据（README、配置、外部调用的客户端、鉴权和回调、部署清单），再下结论。
   画 Component 图还要确定展开哪一个 container；它是数据存储或很简单的 container 时，先告诉用户这一层可能不值得画（理由见 component/RULES.md）。画 Code 图还要确定展开哪一个 component；它不是最复杂、最重要的那类 component，也不是用来说明一种通用模式时，先告诉用户这一层一般不推荐画（理由见 code/RULES.md）。画 Dynamic 图还要确定是哪个场景（功能、用户故事、用例），以及参与者用哪一层（系统、container 或 component）；场景交互简单、看静态图就能懂时，先告诉用户可能不值得画（理由见 dynamic/RULES.md）。画 Deployment 图还要确定是哪个部署环境，一个环境一张图。
   完成标准：能用一句话说出「画的是哪个系统的哪一层（Component 图还要说出哪个 container，Code 图还要说出哪个 component，Dynamic 图还要说出哪个场景，Deployment 图还要说出哪个环境）、系统归哪个团队」。范围不清（用户给的是一组服务或一条产品线）时，先问用户再动手。

2. **列清单**：读上表中对应层级的 RULES.md，按其中「范围与元素」「关系」两节列出元素和关系。画 Container 图时，如果同系统的 `system-context.html` 已存在，先读出它的 JSON，沿用其中的人员和外部系统；画 Component 图时，同样先读 `container.html`，沿用其中的人员、外部系统和其他 container；画 Code 图时，先读对应的 `component-*.html`，确认这个 component 的职责和对应代码，再去读源码；画 Dynamic 图时，先读同系统已有的静态图，沿用其中的参与者（名字、技术、颜色），再沿着代码的调用路径逐条核对消息；画 Deployment 图时，先读 `container.html` 沿用其中的 container（名字、形状、技术），再按这个环境的部署清单（K8s YAML、Terraform、docker-compose、部署脚本）列出节点、实例数和基础设施。
   完成标准（Code 图另看 code/RULES.md：每个元素有 kind 和 name、只留和要讲的事相关的成员、依赖和关联类关系写了说明；Dynamic 图另看 dynamic/RULES.md：每个参与者在静态图和代码里都找得到，每条消息对应一次真实的调用，跨进程的消息写了 technology；Deployment 图另看 deployment/RULES.md：每个容器实例都在 Container 图里、写明部署在哪个节点上，节点、版本、实例数和部署清单一致）：每个元素都有 name 和 description，container、component 还有 technology；每条关系都写了交互目的，System Context 图不含技术细节，Container 图里 container 发起的关系写了 technology，Component 图里跨进程的关系写了 technology；每个支撑元素都直接连到 scope 系统（System Context）、某个 container（Container）或某个 component（Component）；拿不准的地方（是否存在、归谁所有、用什么技术）记为假设，不要编造。

3. **生成 HTML**：把上表中对应层级的 template.html 复制到输出路径，只改其中 `<script id="c4-model">` 的 JSON。字段和布局按该层级 RULES.md 的「数据格式」「布局」两节。不改渲染脚本。
   输出路径：`<项目根目录>/c4-model/<系统短名>/<输出文件名>`，输出文件名见上表。项目根目录是 git 仓库根，不在仓库里时用当前目录；系统短名用英文 kebab-case，优先用系统在代码里的目录名或服务名。同一系统的各层级图放在同一个目录，例如 `agent-platform/services/agent` 的图是 `c4-model/agent/system-context.html`、`c4-model/agent/container.html`，它的「Agent 服务」container 的组件图是 `c4-model/agent/component-server.html`，其中「事件中继」component 的代码图是 `c4-model/agent/code-event-relay.html`，「发送一条消息」场景的动态图是 `c4-model/agent/dynamic-send-message.html`，生产环境的部署图是 `c4-model/agent/deployment-live.html`（container、component、场景、环境短名同样用英文 kebab-case）。目录不存在就建。用户指定了路径时按用户的。

4. **验证**：用 headless Chrome 读出校验状态并截图，再用 Read 看截图。`OUT` 必须是绝对路径：

   ```bash
   CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"   # Linux 用 google-chrome 或 chromium
   "$CHROME" --headless=new --disable-gpu --virtual-time-budget=2000 --dump-dom "file://$OUT" 2>/dev/null \
     | grep -oE 'data-c4-status="[a-z]+"|<li data-level=[^<]*'
   "$CHROME" --headless=new --disable-gpu --hide-scrollbars --window-size=1600,1600 \
     --screenshot=/tmp/c4-check.png "file://$OUT" 2>/dev/null
   ```

   完成标准：状态是 `ok`，截图里没有压字、穿线、难以辨认的标签。不是 `ok` 就按提示改 JSON 后重跑。找不到 Chrome 时，告诉用户在浏览器里打开并看页面顶部有没有提示框，不要声称已验证。

5. **交付**：回复输出路径、元素和关系清单摘要，以及第 2 步记下的假设。
