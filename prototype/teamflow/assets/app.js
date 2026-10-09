/* =========================================================
   AI TeamFlow — 应用逻辑 / Mock 数据 / 渲染 / 交互
   ========================================================= */
(function () {
  'use strict';

  /* ---------------------------------------------------------
     0. 工具函数
     --------------------------------------------------------- */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const pad = (n) => String(n).padStart(2, '0');
  const nowStr = () => {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  const hhmm = () => { const d = new Date(); return `${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  let _uid = 1000;
  const uid = (p) => `${p}${++_uid}`;

  /* ---------------------------------------------------------
     1. Mock 数据（全部数据集中在此，结构清晰）
     --------------------------------------------------------- */

  // 1.1 团队
  //   团队里有人和 AI Agent；项目从团队里挑人和 Agent 担任模板中的角色。
  const TEAMS = [
    { id: 'trade', name: '交易中台', short: '交易', desc: '交易链路、账号与营销能力',
      members: ['zhang', 'li', 'liu', 'wang'],
      agents: ['clarifier', 'pmAgent', 'archAgent', 'qaAgent', 'coderAgent', 'testAgent', 'releaseAgent'] },
    { id: 'tech', name: '技术中心', short: '技术', desc: '稳定性、基础设施与故障应急',
      members: ['chen', 'li'],
      agents: ['opsAgent', 'releaseAgent'] },
    { id: 'content', name: '内容中心', short: '内容', desc: '内容运营与数据分析',
      members: ['zhang', 'wang'],
      agents: ['dataAgent', 'pmAgent'] },
  ];

  // 1.2 人 / Agent 名册
  const PEOPLE = {
    zhang: { id: 'zhang', name: '张明', role: '产品经理', kind: 'human', avatar: '张', title: '高级产品经理' },
    li: { id: 'li', name: '李工', role: '技术负责人', kind: 'human', avatar: '李', title: '技术负责人' },
    wang: { id: 'wang', name: '王测', role: '测试负责人', kind: 'human', avatar: '王', title: '测试负责人' },
    chen: { id: 'chen', name: '陈运', role: '运维负责人', kind: 'human', avatar: '陈', title: 'SRE 负责人' },
    liu: { id: 'liu', name: '刘洋', role: '技术负责人', kind: 'human', avatar: '刘', title: '架构师' },
  };
  const AGENTS = {
    clarifier: { id: 'clarifier', name: '需求澄清 Agent', kind: 'ai', avatar: '🧠', desc: '与人对话澄清需求，整理澄清纪要', model: 'claude-sonnet-5' },
    pmAgent: { id: 'pmAgent', name: 'PRD Agent', kind: 'ai', avatar: '📝', desc: '按 PRD 文件模板撰写需求文档', model: 'claude-sonnet-5' },
    archAgent: { id: 'archAgent', name: '架构 Agent', kind: 'ai', avatar: '🏗️', desc: '基于 PRD 产出技术方案、接口与数据模型', model: 'claude-opus-5-5' },
    qaAgent: { id: 'qaAgent', name: '测试设计 Agent', kind: 'ai', avatar: '🧪', desc: '按验收标准设计测试用例', model: 'claude-sonnet-5' },
    coderAgent: { id: 'coderAgent', name: '编码 Agent', kind: 'ai', avatar: '💻', desc: '在代码仓库中编码并提交 MR', model: 'claude-code（CLI）' },
    testAgent: { id: 'testAgent', name: '测试执行 Agent', kind: 'ai', avatar: '⚙️', desc: '执行用例并产出测试报告', model: 'claude-sonnet-5' },
    releaseAgent: { id: 'releaseAgent', name: '发布 Agent', kind: 'ai', avatar: '🚀', desc: '编排发布步骤、灰度与回滚预案', model: 'claude-sonnet-5' },
    opsAgent: { id: 'opsAgent', name: '故障诊断 Agent', kind: 'ai', avatar: '🛠️', desc: '接入告警、定位根因、整理复盘', model: 'claude-opus-5-5' },
    dataAgent: { id: 'dataAgent', name: '数据分析 Agent', kind: 'ai', avatar: '📊', desc: '取数清洗、指标建模与分析报告', model: 'claude-sonnet-5' },
  };
  const ACTORS = Object.assign({ system: { id: 'system', name: '系统', kind: 'ai', avatar: '⚙' } }, PEOPLE, AGENTS);
  const actor = (id) => ACTORS[id] || { name: id, avatar: '?', kind: 'human' };

  // 1.3 状态 & 执行方式元数据
  const STATUS = {
    pending: { label: '待开始', cls: 'gray' },
    running: { label: '进行中', cls: 'blue' },
    review: { label: '待准出', cls: 'orange' },
    admit: { label: '待准入', cls: 'purple' },
    passed: { label: '已通过', cls: 'green' },
    rejected: { label: '已打回', cls: 'red' },
  };
  const MODE = {
    human: { label: '人', cls: 'human', ico: '👤' },
    ai: { label: 'AI Agent', cls: 'ai', ico: '🤖' },
    collab: { label: '人机协同', cls: 'collab', ico: '🤝' },
  };

  // 1.4 流程模板
  const TEMPLATES = [
    {
      id: 'tpl-dev', name: '研发流程模板', version: 'v1.3', icon: '🧩', refs: 12,
      scope: '交易中台 / 技术中心', desc: '需求到发布端到端研发协同，支持并行设计与评审准出。',
      nodes: [
        { id: 'clarify', name: '需求澄清', mode: 'collab', deps: [], output: '需求澄清纪要', tmpl: '纪要模板', outRev: '张明', inRev: '张明', returnTo: '' },
        { id: 'prd', name: 'PRD', mode: 'ai', deps: ['clarify'], output: 'PRD', tmpl: 'PRD 标准模板', outRev: '张明', inRev: '李工', returnTo: 'clarify' },
        { id: 'tech', name: '技术方案', mode: 'collab', deps: ['prd'], output: '技术方案', tmpl: '技术方案模板', outRev: '李工', inRev: '王测', returnTo: 'prd' },
        { id: 'testcase', name: '测试用例设计', mode: 'ai', deps: ['prd'], output: '测试用例', tmpl: '用例模板', outRev: '王测', inRev: '李工', returnTo: 'prd' },
        { id: 'code', name: '编码', mode: 'collab', deps: ['tech', 'testcase'], output: '代码', tmpl: '分支规范', outRev: '李工', inRev: '王测', returnTo: 'tech' },
        { id: 'testexec', name: '测试执行', mode: 'ai', deps: ['code'], output: '测试报告', tmpl: '测试报告模板', outRev: '王测', inRev: '陈运', returnTo: 'code' },
        { id: 'release', name: '发布上线', mode: 'human', deps: ['testexec'], output: '发布方案', tmpl: '发布方案模板', outRev: '陈运', inRev: '张明', returnTo: 'testexec' },
      ],
    },
    {
      id: 'tpl-content', name: '内容生产模板', version: 'v2.1', icon: '✍️', refs: 8,
      scope: '内容中心', desc: '选题到多渠道分发的 AI 内容流水线，含合规检查。',
      nodes: [
        { id: 'topic', name: '选题策划', mode: 'collab', deps: [], output: '选题单', tmpl: '选题模板', outRev: '张明', inRev: '张明', returnTo: '' },
        { id: 'draft', name: '初稿生成', mode: 'ai', deps: ['topic'], output: '内容初稿', tmpl: '稿件模板', outRev: '张明', inRev: '张明', returnTo: 'topic' },
        { id: 'compliance', name: '合规审核', mode: 'ai', deps: ['draft'], output: '合规结论', tmpl: '合规清单', outRev: '王测', inRev: '张明', returnTo: 'draft' },
        { id: 'polish', name: '人工润色', mode: 'human', deps: ['compliance'], output: '终稿', tmpl: '稿件模板', outRev: '张明', inRev: '张明', returnTo: 'draft' },
        { id: 'distribute', name: '多渠道分发', mode: 'ai', deps: ['polish'], output: '分发记录', tmpl: '分发模板', outRev: '张明', inRev: '张明', returnTo: 'polish' },
      ],
    },
    {
      id: 'tpl-contract', name: '合同合规审查模板', version: 'v1.0', icon: '📜', refs: 3,
      scope: '法务 / 采购', desc: '合同条款抽取、风险识别与法务复核的合规流程。',
      nodes: [
        { id: 'intake', name: '合同录入', mode: 'ai', deps: [], output: '合同结构化数据', tmpl: '抽取模板', outRev: '张明', inRev: '张明', returnTo: '' },
        { id: 'risk', name: '风险条款识别', mode: 'ai', deps: ['intake'], output: '风险清单', tmpl: '风险清单模板', outRev: '张明', inRev: '张明', returnTo: 'intake' },
        { id: 'legal', name: '法务复核', mode: 'human', deps: ['risk'], output: '法务意见', tmpl: '意见模板', outRev: '张明', inRev: '张明', returnTo: 'risk' },
        { id: 'archive', name: '结论归档', mode: 'ai', deps: ['legal'], output: '审查报告', tmpl: '报告模板', outRev: '张明', inRev: '张明', returnTo: 'legal' },
      ],
    },
    {
      id: 'tpl-data', name: '数据分析报告模板', version: 'v1.5', icon: '📈', refs: 5,
      scope: '经营分析 / 增长', desc: '取数、建模、洞察生成与复盘的标准化分析流程。',
      nodes: [
        { id: 'ask', name: '分析需求确认', mode: 'collab', deps: [], output: '分析需求单', tmpl: '需求单模板', outRev: '张明', inRev: '张明', returnTo: '' },
        { id: 'fetch', name: '数据取数与清洗', mode: 'ai', deps: ['ask'], output: '数据集', tmpl: '数据集规范', outRev: '张明', inRev: '张明', returnTo: 'ask' },
        { id: 'model', name: '指标建模', mode: 'ai', deps: ['fetch'], output: '指标口径', tmpl: '指标模板', outRev: '李工', inRev: '张明', returnTo: 'fetch' },
        { id: 'report', name: '报告撰写', mode: 'collab', deps: ['model'], output: '分析报告', tmpl: '报告模板', outRev: '张明', inRev: '张明', returnTo: 'model' },
      ],
    },
    {
      id: 'tpl-incident', name: '故障处理模板', version: 'v1.2', icon: '🚨', refs: 6,
      scope: '技术中心 / SRE', desc: '告警到复盘的事故响应流程，强调时效与根因闭环。',
      nodes: [
        { id: 'alert', name: '告警接入', mode: 'ai', deps: [], output: '告警事件', tmpl: '事件模板', outRev: '陈运', inRev: '陈运', returnTo: '' },
        { id: 'diagnose', name: '根因诊断', mode: 'collab', deps: ['alert'], output: '诊断结论', tmpl: '诊断模板', outRev: '李工', inRev: '陈运', returnTo: 'alert' },
        { id: 'mitigate', name: '止损修复', mode: 'human', deps: ['diagnose'], output: '修复记录', tmpl: '修复模板', outRev: '李工', inRev: '陈运', returnTo: 'diagnose' },
        { id: 'postmortem', name: '复盘归档', mode: 'ai', deps: ['mitigate'], output: '复盘报告', tmpl: '复盘模板', outRev: '陈运', inRev: '陈运', returnTo: 'mitigate' },
      ],
    },
  ];
  const templateById = (id) => TEMPLATES.find((t) => t.id === id);

  // 1.4.1 评审角色
  // ★ 模板只写「角色」，不写人：同一个模板给不同团队用，人各不相同。
  //   角色由谁担任是团队的事（流程管理里配置），原型先给一份默认映射。
  const ROLES = ['产品经理', '技术负责人', '研发工程师', '测试负责人', '运维负责人', '法务', '内容编辑', '数据分析师'];
  //   角色由谁担任在「项目」里分配（人和 AI Agent 都可以），见 PROJECTS.assign；
  //   一个角色可以有多个成员 —— 评审规则「所有人同意」就是对这些成员说的。
  //   流程详情里的调用默认取当前打开的流程所属的项目。
  const reviewersOf = (role, p = currentProcess()) => {
    const pj = p && projectById(p.projectId);
    return (pj && pj.assign[role]) || [];
  };
  // 评审规则：任一人同意即通过 / 角色下所有人都同意才通过
  const RULES = { any: '任一人同意', all: '所有人同意' };
  const PERSON_ROLE = { 张明: '产品经理', 李工: '技术负责人', 王测: '测试负责人', 陈运: '运维负责人' };
  const roleHolderName = (role, p) => reviewersOf(role, p).map((id) => actor(id).name).join('、') || '未分配';
  const roleLabel = (role, p) => `${role}（${roleHolderName(role, p)}）`;

  // 节点的执行角色（人机协同：该角色下的人与 Agent 一起完成节点）
  const EXEC_ROLE = {
    clarify: '需求分析', prd: '产品设计', tech: '架构设计', testcase: '测试设计', code: '开发', testexec: '测试执行', release: '发布',
    topic: '选题策划', draft: '内容创作', compliance: '合规审核', polish: '编辑润色', distribute: '运营分发',
    intake: '合同录入', risk: '风险识别', legal: '法务复核', archive: '审查归档',
    ask: '需求分析', fetch: '数据开发', model: '指标建模', report: '报告撰写',
    alert: '告警处理', diagnose: '故障诊断', mitigate: '故障修复', postmortem: '复盘',
  };
  // 模板涉及的角色：执行角色 + 评审角色
  function templateRoles(t) {
    const exec = [], review = [];
    t.nodes.forEach((n) => {
      if (n.execRole && !exec.includes(n.execRole)) exec.push(n.execRole);
      [n.outRev, n.inRev].forEach((r) => { if (r && !review.includes(r) && !exec.includes(r)) review.push(r); });
    });
    return { exec, review: review.filter((r) => !exec.includes(r)) };
  }
  // 可选角色库：基础评审角色 + 各模板里出现过的角色
  function allRoles() {
    const set = new Set(ROLES);
    TEMPLATES.forEach((t) => t.nodes.forEach((n) => [n.execRole, n.outRev, n.inRev].forEach((r) => r && set.add(r))));
    return Array.from(set);
  }

  // 1.4.2 流程结构
  // ★ 模板的结构是显式的 flow：一串步骤，步骤是单个节点或并行组；
  //   并行组里有多条轨道，每条轨道是一串节点。节点的 deps 不再手填，
  //   由 flow 推导（syncDeps）—— 流程实例的汇合逻辑仍按 deps 判断。
  //   旧数据（只有 deps）在启动时转换（flowFromDeps）。
  function flowFromDeps(nodes) {
    const flow = [];
    const used = new Set();
    nodes.forEach((n) => {
      if (used.has(n.id)) return;
      // 同一个上游、且彼此不依赖的几个节点 = 一个并行组，每个节点一条轨道
      const peers = n.deps.length === 1
        ? nodes.filter((x) => !used.has(x.id) && x.deps.length === 1 && x.deps[0] === n.deps[0])
        : [n];
      if (peers.length > 1) {
        flow.push({ type: 'parallel', tracks: peers.map((x) => ({ nodes: [x.id] })) });
        peers.forEach((x) => used.add(x.id));
      } else {
        flow.push({ type: 'node', id: n.id });
        used.add(n.id);
      }
    });
    return flow;
  }
  function syncDeps(t) {
    const byId = Object.fromEntries(t.nodes.map((n) => [n.id, n]));
    const order = [];
    let prev = [];
    t.flow.forEach((step) => {
      if (step.type === 'node') {
        byId[step.id].deps = prev.slice();
        order.push(byId[step.id]);
        prev = [step.id];
        return;
      }
      const ends = [];
      step.tracks.forEach((tr) => {
        let p = prev.slice();
        tr.nodes.forEach((id) => { byId[id].deps = p; order.push(byId[id]); p = [id]; });
        if (tr.nodes.length) ends.push(tr.nodes[tr.nodes.length - 1]);
      });
      prev = ends;
    });
    t.nodes = order;
  }
  // 启动时把旧数据整理成新口径：执行方式一律人机协同、评审写角色、产物引用文件模板
  function normalizeTemplates() {
    TEMPLATES.forEach((t) => {
      t.nodes.forEach((n) => {
        n.mode = 'collab';
        n.outRev = PERSON_ROLE[n.outRev] || n.outRev;
        n.inRev = PERSON_ROLE[n.inRev] || n.inRev;
        const def = ARTIFACT_DEFS.find((d) => d.output === n.output);
        n.artifact = def ? def.id : null;
        n.execRole = n.execRole || EXEC_ROLE[n.id] || n.name;
        n.outRule = n.outRule || (t.id === 'tpl-dev' && n.id === 'tech' ? 'all' : 'any');
        n.inRule = n.inRule || 'any';
        delete n.returnTo;
        delete n.tmpl;
      });
      if (!t.flow) t.flow = flowFromDeps(t.nodes);
      syncDeps(t);
    });
    // 在途流程固定在创建时的模板版本：模板再怎么编辑发布都不影响它
    PROCESSES.forEach((p) => { p.tpl = JSON.parse(JSON.stringify(templateById(p.templateId))); });
  }
  const procTpl = (p) => p.tpl || templateById(p.templateId);

  // 1.5 文件模板
  // ★ 文件模板是一份带版本的 Markdown 规则文件（doc：docs/file-template-design.html）。
  //   流程模板节点的产物选用它（产物格式）；未来自动准出也用它写判定规则。
  //   只支持 .md、≤ 100 KB；不解析内容，原文交给 Agent。版本不可变：新开工的节点用
  //   生效版本，进行中的节点用开工时锁定的版本。
  //   usage 只用于筛选；output 仅用于把旧数据里的节点产出名映射到模板（启动时一次）。
  const FT_USAGE = { artifact: '产物格式', review_rule: '评审规则', other: '其他' };
  // 由「章节 + 写作要求」拼出一份 Markdown 规范（原型数据用）
  function mdSpec(title, intro, sections) {
    return [`# ${title}`, '', `> ${intro}`, '', ...sections.flatMap(([h, r], k) => [`## ${k + 1}. ${h}`, '', r, ''])].join('\n');
  }
  const PRD_SECTIONS = [
    ['需求背景', '说明业务现状与痛点，引用数据需注明来源。'],
    ['目标与范围', '列出可量化目标；**明确本期不做什么**。'],
    ['用户故事', '使用「作为…我希望…以便…」格式，每条对应一个角色。'],
    ['功能详述', '按功能点逐条描述，包含正常流程与异常流程。'],
    ['验收标准', '使用 Given / When / Then 格式，每条可测试：\n\n| 编号 | Given | When | Then |\n| --- | --- | --- | --- |\n| AC-1 | 已登录用户 | 点击兑换 | 积分扣减且券到账 |'],
    ['非功能需求', '性能、安全、兼容性指标，给出具体数值。'],
    ['里程碑计划', '关键时间点与交付物。'],
    ['风险与依赖', '外部依赖方与应对措施。'],
  ];
  const TECH_SECTIONS = [
    ['方案概述', '一段话说明改动范围与核心思路。'],
    ['架构设计', '给出调用链路图，标注新增 / 修改的服务。'],
    ['数据模型', '新增或变更的表结构、索引。'],
    ['接口定义', '每个接口给出入参、出参、错误码，示例：\n\n```http\nPOST /auth/sms/send\n{ "phone": "13800000000" }\n```'],
    ['容量与性能', '预估 QPS 与资源，给出压测目标。'],
    ['安全与合规', '鉴权、限流、敏感数据处理。'],
    ['灰度与回滚', '**必填**：灰度比例与观察指标；回滚步骤。'],
    ['排期评估', '按人日拆分。'],
  ];
  const ftVer = (ver, file, by, time, note, content) => ({ ver, file, by, time, note, content, size: new Blob([content]).size });
  const ARTIFACT_DEFS = [
    {
      id: 'prd', name: 'PRD 编写规范', icon: '📄', usage: 'artifact', output: 'PRD',
      desc: '规定 PRD 的章节结构与写作要求：要做什么、为什么做、做到什么程度算完成。',
      attachment: ftVer('v3', 'PRD编写规范.md', 'zhang', '2026-09-20 15:30', '验收标准改为 Given/When/Then 格式',
        mdSpec('PRD 编写规范', 'PRD 是技术方案与测试用例的共同输入，请按以下章节撰写。', PRD_SECTIONS)),
      history: [
        ftVer('v2', 'PRD编写规范.md', 'zhang', '2026-08-11 10:02', '新增「非功能需求」章节',
          mdSpec('PRD 编写规范', 'PRD 是技术方案与测试用例的共同输入，请按以下章节撰写。',
            PRD_SECTIONS.map(([h, r]) => [h, h === '验收标准' ? '逐条列出验收标准，每条可测试。' : r]))),
        ftVer('v1', 'PRD模板.md', 'zhang', '2026-06-02 09:00', '初版',
          mdSpec('PRD 模板', '请按以下章节撰写。', PRD_SECTIONS.filter(([h]) => h !== '非功能需求').map(([h, r]) => [h, h === '验收标准' ? '逐条列出验收标准。' : r]))),
      ],
    },
    {
      id: 'tech', name: '技术方案编写规范', icon: '🏗️', usage: 'artifact', output: '技术方案',
      desc: '规定技术方案的结构：架构、接口、数据模型、灰度与回滚。',
      attachment: ftVer('v2', '技术方案编写规范.md', 'li', '2026-09-02 11:20', '「灰度与回滚」改为必填',
        mdSpec('技术方案编写规范', '基于 PRD 给出实现方式，供编码与评审使用。', TECH_SECTIONS)),
      history: [
        ftVer('v1', '技术方案模板.md', 'li', '2026-06-15 14:00', '初版',
          mdSpec('技术方案模板', '基于 PRD 给出实现方式。', TECH_SECTIONS.map(([h, r]) => [h, h === '灰度与回滚' ? '如涉及线上变更，说明灰度方式（选填）。' : r]))),
      ],
    },
    {
      id: 'testcase', name: '测试用例编写规范', icon: '🧪', usage: 'artifact', output: '测试用例',
      desc: '按 PRD 验收标准设计用例，覆盖正常、异常与边界场景。',
      attachment: ftVer('v1', '测试用例编写规范.md', 'wang', '2026-07-08 16:45', '初版',
        mdSpec('测试用例编写规范', '用例以表格形式给出，每条关联一个验收标准。', [
          ['用例表格式', '| 编号 | 前置条件 | 操作步骤 | 预期结果 | 优先级 | 覆盖需求 | 可自动化 |\n| --- | --- | --- | --- | --- | --- | --- |\n| TC-01 | … | … | … | P0 | AC-1 | 是 |'],
          ['覆盖要求', '- 每条验收标准至少一条用例\n- 必须包含边界与异常用例'],
          ['预期结果', '必须可判定，不写「正常」这类描述。'],
        ])),
      history: [],
    },
    {
      id: 'testreport', name: '测试报告规范', icon: '📊', usage: 'artifact', output: '测试报告',
      desc: '测试执行结果与发布建议，是发布上线的准入依据。',
      attachment: ftVer('v1', '测试报告规范.md', 'wang', '2026-07-08 16:50', '初版',
        mdSpec('测试报告规范', '结论必须明确，供发布决策。', [
          ['测试范围', '覆盖的功能与版本。'], ['用例执行统计', '通过 / 失败 / 阻塞数量。'],
          ['缺陷明细', '按严重级别列出，附缺陷链接。'], ['遗留风险', '未修复缺陷的影响评估。'],
          ['结论与建议', '明确写「可发布」或「不可发布」。'],
        ])),
      history: [],
    },
    {
      // 故意没有文件：演示「未上传模板文件」与首次上传
      id: 'release', name: '发布方案规范', icon: '🚀', usage: 'artifact', output: '发布方案',
      desc: '上线步骤、灰度策略、回滚预案与验证清单。',
      attachment: null, history: [],
    },
    {
      id: 'minutes', name: '需求澄清纪要规范', icon: '🗒️', usage: 'artifact', output: '需求澄清纪要',
      desc: '人与 Agent 澄清需求的问题与结论，是 PRD 的输入。',
      attachment: ftVer('v1', '需求澄清纪要规范.md', 'zhang', '2026-06-02 09:10', '初版',
        mdSpec('需求澄清纪要规范', '每个问题都要有结论与确认人。', [
          ['参会人与背景', '谁提出、为什么现在做。'], ['澄清问题清单', '问题 + 提问方。'],
          ['结论与决策', '每个问题的结论与确认人。'], ['待办与责任人', '遗留事项。'],
        ])),
      history: [],
    },
    {
      // 评审规则：为未来的自动准出准备，暂无节点引用
      id: 'tech-exit', name: '技术方案准出检查规则', icon: '✅', usage: 'review_rule', output: null,
      desc: '技术方案准出时的检查项与判定标准，供未来的 AI 自动准出使用。',
      attachment: ftVer('v1', '技术方案准出检查规则.md', 'li', '2026-10-01 10:00', '初版',
        mdSpec('技术方案准出检查规则', '以下任一项不满足即判定为「不通过」，并说明原因。', [
          ['接口完整', '每个接口都有入参、出参与错误码。'],
          ['幂等与并发', '涉及资金 / 积分的写操作给出幂等键与并发控制。'],
          ['可回滚', '给出灰度比例、观察指标与回滚步骤。'],
        ])),
      history: [],
    },
  ];
  const artifactDef = (id) => ARTIFACT_DEFS.find((d) => d.id === id);

  // 1.7 每个节点的产出物预览文档（工作台右侧）
  const NODE_DOC = {
    clarify: { title: '需求澄清纪要', version: 'v1.0', owner: 'clarifier', sections: [
      { h: '参会人与背景', p: '张明（产品）、需求澄清 Agent。目标：明确登录方式改造范围。' },
      { h: '澄清问题清单', p: '1) 是否保留密码登录？2) 验证码频控阈值？3) 国际区号是否支持？' },
      { h: '结论与决策', p: '保留密码登录并新增手机号验证码；频控 5 次/10 分钟；一期仅支持 +86。' },
    ] },
    prd: { title: 'PRD 需求文档', version: 'v1.3', owner: 'pmAgent', sections: [
      { h: '需求背景', p: '当前仅支持账号密码登录，用户抱怨找回密码链路长，登录转化率偏低。' },
      { h: '目标与范围', p: '一期支持手机号 + 验证码登录；不包含第三方登录与生物识别。' },
      { h: '用户故事', p: '作为新用户，我希望输入手机号与验证码即可登录，无需记忆密码。' },
      { h: '验收标准', p: '验证码 60s 有效；错误 5 次锁定 10 分钟；有效期可配置。' },
    ] },
    tech: { title: '技术方案', version: 'v1.0', owner: 'archAgent', sections: [
      { h: '方案概述', p: '新增验证码服务，登录网关按 loginType 路由到密码/验证码两种鉴权链路。' },
      { h: '接口定义', p: 'POST /auth/sms/send、POST /auth/login/sms，均返回 traceId。' },
      { h: '安全与合规', p: '验证码 5 分钟内单号限发 5 次；风控黑名单拦截。' },
      { h: '灰度与回滚', p: '按 5% → 20% → 100% 灰度，支持一键回滚。' },
    ] },
    testcase: { title: '测试用例', version: 'v0.9', owner: 'qaAgent', sections: [
      { h: '用例概览', p: '共 42 条：正常 18、异常 20、边界 4，自动化覆盖 35 条。' },
      { h: '重点用例', p: 'TC-10 验证码连续错误 5 次锁定；TC-11 过期后重新获取。' },
      { h: '覆盖需求', p: '覆盖 PRD 验收标准全部 4 项。' },
    ] },
    code: { title: '代码变更', version: 'PR #1287', owner: 'coderAgent', sections: [
      { h: '变更范围', p: '新增 SmsAuthController、SmsCodeService；调整 LoginGateway 路由。' },
      { h: '自动化检查', p: '单测覆盖 86%，静态扫描 0 高危。' },
    ] },
    testexec: { title: '测试报告', version: 'v1.0', owner: 'testAgent', sections: [
      { h: '执行统计', p: '用例 42，通过 41，失败 1，通过率 97.6%。' },
      { h: '缺陷明细', p: 'BUG-221 验证码过期未提示（已修复待回归）。' },
      { h: '结论', p: '满足发布准出条件，遗留 1 个低风险项。' },
    ] },
    release: { title: '发布方案', version: 'v1.0', owner: 'chen', sections: [
      { h: '发布范围', p: '交易中台登录网关、验证码服务。' },
      { h: '灰度策略', p: '5% → 20% → 100%，每档观察 30 分钟。' },
      { h: '回滚预案', p: '开关一键关闭验证码登录，5 分钟内生效。' },
    ] },
  };

  // 1.8 对话数据（按流程 → 节点）
  const CHAT = {
    p1: {
      clarify: [
        { from: 'ai', who: 'clarifier', text: '你好，我是需求澄清 Agent。已读取原始需求：「支持用户用手机号+验证码登录」。请确认是否需要保留原有密码登录？', t: '09:31' },
        { from: 'human', who: 'zhang', text: '保留密码登录，本次是新增一种登录方式。', t: '09:33' },
        { from: 'ai', who: 'clarifier', text: '明白。再确认两点：①验证码频控阈值；②是否需要支持国际区号？', t: '09:33' },
        { from: 'human', who: 'zhang', text: '频控 5 次/10 分钟；一期仅 +86。', t: '09:35' },
        { from: 'sys', who: '', text: '需求澄清纪要 v1.0 已生成并归档', t: '09:36' },
      ],
      prd: [
        { from: 'ai', who: 'pmAgent', text: '基于澄清纪要，我已产出 PRD v1.2，包含需求背景、用户故事与验收标准。', t: '10:10' },
        { from: 'human', who: 'zhang', text: '验收标准第 3 条请补充锁定策略。', t: '10:20' },
        { from: 'ai', who: 'pmAgent', text: '已更新为「验证码错误 5 次锁定 10 分钟」，提交为 v1.3。', t: '10:24' },
        { from: 'sys', who: '', text: 'PRD v1.3 通过准出评审（张明）', t: '10:30' },
      ],
      tech: [
        { from: 'ai', who: 'archAgent', text: '已根据 PRD v1.3 产出技术方案，新增验证码服务与登录网关路由。', t: '14:20' },
        { from: 'human', who: 'li', text: '灰度与回滚策略需要在方案里写清楚。', t: '17:50' },
        { from: 'ai', who: 'archAgent', text: '已补充「5%→20%→100% 灰度 + 一键回滚」，提交 v1.0 待审核。', t: '18:02' },
      ],
      testcase: [
        { from: 'ai', who: 'qaAgent', text: '正在依据 PRD 并行设计测试用例，已生成 42 条（含异常分支）。', t: '15:10' },
        { from: 'human', who: 'wang', text: '重点覆盖验证码锁定与过期场景。', t: '15:20' },
        { from: 'ai', who: 'qaAgent', text: '已补充 TC-10 / TC-11，等待技术方案确认后提交评审。', t: '15:24' },
      ],
    },
  };
  const chatFor = (pid, nodeId) => (CHAT[pid] && CHAT[pid][nodeId]) ? CHAT[pid][nodeId].slice() : [
    { from: 'sys', who: '', text: '该节点暂无对话记录，上游产出物就绪后可下发指令启动。', t: '--:--' },
  ];

  // 1.8.5 项目
  // ★ 项目属于团队，绑定一个流程模板（版本固定），并为模板涉及的每个角色
  //   从团队里挑人和 AI Agent（assign：角色 → 成员 id 列表）。流程属于项目。
  const PROJECTS = [
    {
      id: 'pj-account', teamId: 'trade', name: '账号体系升级', desc: '登录方式扩展、账号安全与风控',
      templateId: 'tpl-dev', created: '2026-09-20',
      assign: {
        需求分析: ['zhang', 'clarifier'], 产品设计: ['zhang', 'pmAgent'], 架构设计: ['li', 'archAgent'],
        测试设计: ['wang', 'qaAgent'], 开发: ['li', 'coderAgent'], 测试执行: ['wang', 'testAgent'], 发布: ['li', 'releaseAgent'],
        产品经理: ['zhang'], 技术负责人: ['li', 'liu'], 测试负责人: ['wang'], 运维负责人: ['li'],
      },
    },
    {
      // 新建不久、角色还没分配完：演示「未分配」的提示
      id: 'pj-coupon', teamId: 'trade', name: '优惠券中心重构', desc: '券模板、发放与核销链路重构',
      templateId: 'tpl-dev', created: '2026-10-06',
      assign: { 需求分析: ['zhang', 'clarifier'], 产品设计: ['zhang', 'pmAgent'], 产品经理: ['zhang'] },
    },
    {
      id: 'pj-stability', teamId: 'tech', name: '支付稳定性保障', desc: '支付链路故障应急与复盘',
      templateId: 'tpl-incident', created: '2026-08-15',
      assign: {
        告警处理: ['chen', 'opsAgent'], 故障诊断: ['li', 'opsAgent'], 故障修复: ['li'], 复盘: ['chen', 'opsAgent'],
        运维负责人: ['chen'], 技术负责人: ['li'],
      },
    },
    {
      id: 'pj-weekly', teamId: 'content', name: '推荐位运营分析', desc: '首页推荐位效果周报',
      templateId: 'tpl-data', created: '2026-09-01',
      assign: {
        需求分析: ['zhang', 'dataAgent'], 数据开发: ['dataAgent'], 指标建模: ['wang', 'dataAgent'], 报告撰写: ['zhang', 'dataAgent'],
        产品经理: ['zhang'], 技术负责人: ['wang'],
      },
    },
  ];
  const projectById = (id) => PROJECTS.find((x) => x.id === id);
  const teamById = (id) => TEAMS.find((x) => x.id === id);
  // 流程详情里的辅助函数默认作用于当前打开的流程
  const currentProcess = () => {
    const m = /^#\/process\/([^/]+)/.exec(state.route || '');
    return m ? processById(m[1]) : null;
  };

  // 1.9 流程实例
  //    states: 节点运行时状态；current: 当前聚焦节点；returns: 打回标注
  const PROCESSES = [
    {
      id: 'p1', name: '支持用户用手机号+验证码登录', team: 'trade', projectId: 'pj-account', templateId: 'tpl-dev',
      status: 'running', created: '2026-09-28 09:30', creator: 'zhang',
      current: 'tech',
      states: { clarify: 'passed', prd: 'passed', tech: 'review', testcase: 'running', code: 'pending', testexec: 'pending', release: 'pending' },
      // 审批记录：节点 → 阶段（out 准出 / in 准入）→ 评审人 → ok / no
      votes: { tech: { out: { li: 'ok' } } },
      returns: [],
    },
    {
      // 停在 PRD 准出，等产品经理（张明 = 当前用户）审批 → 出现在「待我处理」
      id: 'p5', name: '支持微信一键登录', team: 'trade', projectId: 'pj-account', templateId: 'tpl-dev',
      status: 'running', created: '2026-10-05 14:20', creator: 'li',
      current: 'prd',
      states: { clarify: 'passed', prd: 'review', tech: 'pending', testcase: 'pending', code: 'pending', testexec: 'pending', release: 'pending' },
      votes: {},
      returns: [],
    },
    {
      id: 'p4', name: '登录风控策略升级', team: 'trade', projectId: 'pj-account', templateId: 'tpl-dev',
      status: 'passed', created: '2026-09-02 10:00', creator: 'zhang',
      current: 'release',
      states: { clarify: 'passed', prd: 'passed', tech: 'passed', testcase: 'passed', code: 'passed', testexec: 'passed', release: 'passed' },
      votes: {},
      returns: [],
    },
    {
      id: 'p2', name: '支付超时故障应急处理', team: 'tech', projectId: 'pj-stability', templateId: 'tpl-incident',
      status: 'running', created: '2026-10-02 21:10', creator: 'chen',
      current: 'mitigate',
      states: { alert: 'passed', diagnose: 'passed', mitigate: 'running', postmortem: 'pending' },
      returns: [{ from: 'diagnose', to: 'alert', comment: '告警信息缺少影响面，需补充后重新诊断' }],
    },
    {
      id: 'p3', name: '首页推荐位数据周报', team: 'content', projectId: 'pj-weekly', templateId: 'tpl-data',
      status: 'passed', created: '2026-09-24 10:00', creator: 'zhang',
      current: 'report',
      states: { ask: 'passed', fetch: 'passed', model: 'passed', report: 'passed' },
      returns: [],
    },
  ];
  const processById = (id) => PROCESSES.find((p) => p.id === id);

  // 1.10 审计日志
  const AUDIT = [
    { time: '2026-09-28 09:30', actor: 'zhang', action: '发起流程', object: 'p1 · 手机号验证码登录', version: 'tpl-dev v1.2' },
    { time: '2026-09-28 09:31', actor: 'clarifier', action: '下发指令', object: 'p1 · 需求澄清', version: '—' },
    { time: '2026-09-28 09:36', actor: 'clarifier', action: '生成产出', object: 'p1 · 需求澄清纪要', version: 'v1.0' },
    { time: '2026-09-29 10:12', actor: 'pmAgent', action: '生成产出', object: 'p1 · PRD', version: 'v1.2' },
    { time: '2026-09-29 10:20', actor: 'zhang', action: '打回', object: 'p1 · PRD', version: 'v1.2 → 需求澄清' },
    { time: '2026-09-29 16:40', actor: 'pmAgent', action: '生成产出', object: 'p1 · PRD', version: 'v1.3' },
    { time: '2026-09-30 10:00', actor: 'zhang', action: '评审通过', object: 'p1 · PRD', version: 'v1.3' },
    { time: '2026-09-30 10:02', actor: 'zhang', action: '流转', object: 'p1 · PRD → 技术方案/测试用例设计', version: '—' },
    { time: '2026-09-30 14:20', actor: 'archAgent', action: '生成产出', object: 'p1 · 技术方案', version: 'v0.9' },
    { time: '2026-09-30 18:02', actor: 'archAgent', action: '生成产出', object: 'p1 · 技术方案', version: 'v1.0' },
    { time: '2026-09-30 18:05', actor: 'archAgent', action: '提交评审', object: 'p1 · 技术方案', version: 'v1.0' },
    { time: '2026-10-01 09:30', actor: 'li', action: '提交评审', object: 'p1 · 技术方案', version: 'v1.0' },
    { time: '2026-10-02 21:10', actor: 'chen', action: '发起流程', object: 'p2 · 故障应急', version: 'tpl-incident v1.2' },
    { time: '2026-10-02 21:40', actor: 'opsAgent', action: '生成产出', object: 'p2 · 诊断结论', version: 'v1.0' },
    { time: '2026-10-02 22:05', actor: 'chen', action: '打回', object: 'p2 · 根因诊断', version: 'v1.0 → 告警接入' },
  ];

  /* ---------------------------------------------------------
     2. 全局状态
     --------------------------------------------------------- */
  const state = {
    team: 'trade',
    route: '#/teams',
    // 临时视图内状态
    tplSelNode: null,
    tplEdit: null,     // { id, draft }：编辑中的模板草稿
    insMenu: null,     // 流程图上打开的插入菜单位置
    procSelNode: null,
    procArtSel: null,  // 流程详情里正在预览的产物（节点 id）
    projFilter: 'all', // 项目首页的流程筛选：all / mine / running / done
    ftUsage: 'all',    // 文件模板列表的用途筛选
    ftVer: null,       // 文件模板详情里正在查看的版本（null = 生效版本）
    ftView: 'render',  // 模板内容：render 渲染 / source 源码
    projQuery: '',
  };

  /* ---------------------------------------------------------
     3. Toast & Modal
     --------------------------------------------------------- */
  const TOAST_ICON = { ok: '✓', info: 'i', warn: '!', err: '✕' };
  function toast(msg, type = 'ok', ms = 2600) {
    const root = $('#toast-root');
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.innerHTML = `<span class="ti">${TOAST_ICON[type] || '✓'}</span><span>${esc(msg)}</span>`;
    root.appendChild(el);
    setTimeout(() => {
      el.classList.add('out');
      setTimeout(() => el.remove(), 220);
    }, ms);
  }

  function openModal({ title, body, footer, wide, onMount }) {
    const root = $('#modal-root');
    root.innerHTML = `
      <div class="modal-mask" data-modal-mask>
        <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
          <div class="modal-h">
            <h3>${esc(title)}</h3>
            <button class="btn btn-ghost btn-sm x" data-close-modal aria-label="关闭">✕</button>
          </div>
          <div class="modal-b">${body || ''}</div>
          ${footer ? `<div class="modal-f">${footer}</div>` : ''}
        </div>
      </div>`;
    const mask = $('[data-modal-mask]', root);
    mask.addEventListener('click', (e) => { if (e.target === mask) closeModal(); });
    $$('[data-close-modal]', root).forEach((b) => b.addEventListener('click', closeModal));
    if (typeof onMount === 'function') onMount(root);
  }
  function closeModal() { $('#modal-root').innerHTML = ''; }

  /* ---------------------------------------------------------
     4. 展示组件（返回 HTML 片段）
     --------------------------------------------------------- */
  const statusBadge = (s) => {
    const m = STATUS[s] || STATUS.pending;
    return `<span class="badge badge-${m.cls}"><span class="bdot"></span>${m.label}</span>`;
  };
  const modeBadge = (m) => {
    const x = MODE[m] || MODE.human;
    return `<span class="exec exec-${x.cls}">${x.ico} ${x.label}</span>`;
  };
  const avatarEl = (id, size = '') => {
    const a = actor(id);
    const cls = a.kind === 'ai' ? 'avatar-agent' : 'avatar-human';
    return `<span class="avatar ${cls} ${size}" title="${esc(a.name)}">${esc(a.avatar)}</span>`;
  };
  const avatarsLine = (ids) => `<span class="avatar-stack">${ids.map((i) => avatarEl(i, 'sm')).join('')}</span>`;

  /* ---------------------------------------------------------
     5. 顶栏 / 侧边栏
     --------------------------------------------------------- */
  function renderTopbar() {
    const team = TEAMS.find((t) => t.id === state.team);
    const crumbs = breadcrumbsFor(state.route);
    $('#topbar').innerHTML = `
      <div class="crumbs">
        <span>AI TeamFlow</span><span class="sep">/</span>
        ${crumbs.map((c, i) => i === crumbs.length - 1
          ? `<span class="cur">${esc(c)}</span>`
          : `<span>${esc(c)}</span><span class="sep">/</span>`).join('')}
      </div>
      <div class="topbar-spacer"></div>
      <div class="team-switcher">
        <button data-team-toggle>
          <span>🏢</span><span>${esc(team.name)}</span><span class="caret">▼</span>
        </button>
        <div class="team-menu" data-team-menu hidden>
          ${TEAMS.map((t) => `
            <button data-team="${t.id}" class="${t.id === state.team ? 'active' : ''}">
              <span>🏢</span><span>${esc(t.name)}</span>
              ${t.id === state.team ? '<span class="check">✓</span>' : ''}
            </button>`).join('')}
        </div>
      </div>`;
  }

  function breadcrumbsFor(route) {
    const seg = route.replace('#/', '').split('/').filter(Boolean);
    const map = { templates: '流程模板', 'file-templates': '文件模板管理', teams: '团队管理', projects: '团队管理', process: '团队管理' };
    const out = [map[seg[0]] || '团队管理'];
    if (seg[0] === 'teams' && seg[1]) { const t = teamById(seg[1]); if (t) out.push(t.name); }
    if (seg[0] === 'projects' && seg[1]) { const pj = projectById(seg[1]); if (pj) out.push(teamName(pj.teamId), pj.name); if (seg[2] === 'settings') out.push('项目设置'); }
    if (seg[0] === 'templates' && seg[1]) { const t = templateById(seg[1]); if (t) out.push(t.name); }
    if (seg[0] === 'process' && seg[1]) { const p = processById(seg[1]); if (p) { const pj = projectById(p.projectId); out.push(teamName(pj.teamId), pj.name, p.name); } }
    if (seg[0] === 'file-templates' && seg[1]) { const d = artifactDef(seg[1]); if (d) out.push(d.name); }
    return out;
  }

  function renderNav() {
    let seg = state.route.replace('#/', '').split('/')[0] || 'teams';
    if (seg === 'projects' || seg === 'process') seg = 'teams';
    $$('#nav .nav-item').forEach((a) => {
      a.classList.toggle('active', a.dataset.nav === `#/${seg}`);
    });
  }

  /* ---------------------------------------------------------
     7. 视图：流程模板管理
     --------------------------------------------------------- */
  function viewTemplates() {
    return `
      <div class="page-head">
        <div>
          <h1>流程模板管理</h1>
          <div class="sub">沉淀组织级协作规范：谁执行、依赖什么产出物、由谁评审、退回哪里。</div>
        </div>
        <div class="grow"></div>
        <button class="btn" data-act="noop">＋ 新建模板</button>
      </div>
      <div class="tpl-grid">
        ${TEMPLATES.map((t) => `
          <div class="tpl-card" data-nav="#/templates/${t.id}">
            <div class="top">
              <div class="tpl-ico">${t.icon}</div>
              <div class="grow">
                <h3>${esc(t.name)}</h3>
                <div class="row mt8" style="gap:7px">
                  <span class="tag">${t.version}</span>
                  <span class="tag">${t.nodes.length} 个节点</span>
                </div>
              </div>
            </div>
            <div class="desc">${esc(t.desc)}</div>
            <div class="tpl-stats">
              <div>被引用<b>${t.refs}</b>次</div>
              <div>适用范围<b style="font-size:12px;line-height:2.2">${esc(t.scope)}</b></div>
            </div>
          </div>`).join('')}
      </div>`;
  }

  // 编辑中的草稿：编辑只改草稿，发布才替换模板（在途流程有自己的快照，不受影响）
  const editingTpl = (id) => state.tplEdit && state.tplEdit.id === id;
  const viewTpl = (id) => (editingTpl(id) ? state.tplEdit.draft : templateById(id));
  const nextVersion = (v) => { const [a, b] = v.slice(1).split('.').map(Number); return `v${a}.${(b || 0) + 1}`; };

  function viewTemplateDetail(id) {
    const base = templateById(id);
    if (!base) return `<div class="empty"><span class="e-ico">🧩</span>模板不存在</div>`;
    const t = viewTpl(id);
    const edit = editingTpl(id);
    const sel = t.nodes.find((n) => n.id === state.tplSelNode) || t.nodes[0];

    const head = edit ? `
      <div class="callout callout-info mb16 edit-bar"><span class="ico">✏️</span>
        <div class="grow">正在编辑 <b>${nextVersion(base.version)}</b> 草稿：可新增节点、新增并行轨道、在轨道中新增节点，并修改节点配置。发布后仅对<b>新建流程</b>生效，进行中的流程仍按原版本执行。</div>
        <button class="btn btn-sm" data-act="tpl-discard">放弃修改</button>
        <button class="btn btn-sm btn-primary" data-act="tpl-publish">发布 ${nextVersion(base.version)}</button>
      </div>` : `
      <div class="callout callout-warn mb16">
        <span class="ico">⚠️</span>
        <div>当前发布 <b>${base.version}</b>。修改流程会生成新版本，新版本仅对新建流程生效，进行中的流程仍使用创建时的版本。</div>
      </div>`;

    return `
      <div class="page-head">
        <div>
          <h1>${t.icon} ${esc(t.name)}</h1>
          <div class="sub">${esc(t.desc)}</div>
        </div>
        <div class="grow"></div>
        ${edit ? '' : '<button class="btn btn-primary" data-act="tpl-edit">✏️ 编辑流程</button>'}
        <button class="btn" data-nav="#/templates">← 返回模板列表</button>
      </div>

      <div class="card pad mb16">
        <div class="row wrap" style="gap:18px">
          <span class="small muted">版本 <b style="color:var(--ink)">${edit ? `${nextVersion(base.version)}（草稿）` : `${base.version}（当前发布）`}</b></span>
          <span class="small muted">节点数 <b style="color:var(--ink)">${t.nodes.length}</b></span>
          <span class="small muted">被引用 <b style="color:var(--ink)">${t.refs}</b> 次</span>
          <span class="small muted">适用范围 <b style="color:var(--ink)">${esc(t.scope)}</b></span>
          <div class="grow"></div>
          <span class="small muted">所有节点均为 ${modeBadge('collab')}：人下达指令、评审成果，Agent 执行并产出</span>
        </div>
      </div>

      ${head}

      <div class="flow-wrap ${edit ? 'editing' : ''}">
        <div class="flow-row">${flowHtml(t, sel.id, edit)}</div>
      </div>

      <div class="section-title"><h2>节点配置</h2>${edit ? '' : '<span class="count">点击「编辑流程」后可修改</span>'}</div>
      ${nodeConfigPanel(sel, t, edit)}
    `;
  }

  // 流程图：步骤之间是箭头；编辑态下箭头处可以插入节点 / 并行轨道
  function flowHtml(t, selId, edit) {
    const byId = Object.fromEntries(t.nodes.map((n) => [n.id, n]));
    const card = (id) => flowNode(byId[id], selId, edit);
    const arrow = (at) => `
      <div class="f-arrow">
        ${edit ? `<div class="f-ins">
          <button class="f-plus" data-act="ins-menu" data-at="${at}" title="在此处插入">＋</button>
          ${state.insMenu === at ? `<div class="f-menu">
            <button data-act="ins-node" data-at="${at}">▭ 插入节点</button>
            <button data-act="ins-par" data-at="${at}">☰ 插入并行轨道</button>
          </div>` : ''}
        </div>` : ''}
      </div>`;
    const parts = [];
    if (edit) parts.push(arrow(0));
    t.flow.forEach((step, k) => {
      if (k > 0) parts.push(arrow(k));
      if (step.type === 'node') { parts.push(card(step.id)); return; }
      parts.push(`
        <div class="f-par">
          <div class="f-par-h">
            <span>⇉ 并行轨道 · 上游产出物就绪即同时开工，全部完成后汇合</span>
            ${edit ? `<button class="btn btn-sm" data-act="add-track" data-step="${k}">＋ 新增轨道</button>` : ''}
          </div>
          ${step.tracks.map((tr, ti) => `
            <div class="f-track">
              <span class="f-track-l">轨道 ${'ABCDEFGH'[ti]}</span>
              ${tr.nodes.map((id, ni) => `${ni ? '<div class="f-arrow f-arrow-s"></div>' : ''}${card(id)}`).join('')}
              ${edit ? `
                <button class="f-add" data-act="track-add-node" data-step="${k}" data-track="${ti}">＋ 节点</button>
                <button class="f-del-track" data-act="del-track" data-step="${k}" data-track="${ti}" title="删除轨道">删除轨道</button>` : ''}
            </div>`).join('')}
        </div>`);
    });
    if (edit) parts.push(arrow(t.flow.length));
    return parts.join('');
  }

  function flowNode(n, selId, edit) {
    const def = n.artifact ? artifactDef(n.artifact) : null;
    return `
      <div class="f-node ${n.id === selId ? 'sel' : ''}" data-tpl-node="${n.id}">
        ${edit ? `<button class="f-x" data-act="del-node" data-node="${n.id}" title="删除节点">×</button>` : ''}
        <div class="f-name">${esc(n.name)}</div>
        <div class="f-out"><span class="f-outname">产物：${esc(n.output)}</span>${def ? `<span class="chip">📝 ${esc(def.name)}</span>` : '<span class="chip chip-warn">未选文件模板</span>'}</div>
        <div class="f-roles"><span>执行 <b>${esc(n.execRole)}</b></span><span>准出 <b>${esc(n.outRev)}</b> · ${n.outRule === 'all' ? '所有人' : '任一人'}</span><span>准入 <b>${esc(n.inRev)}</b> · ${n.inRule === 'all' ? '所有人' : '任一人'}</span></div>
      </div>`;
  }



  function nodeConfigPanel(n, t, edit) {
    if (!n) return '';
    const def = n.artifact ? artifactDef(n.artifact) : null;
    const inputs = n.deps.map((d) => t.nodes.find((x) => x.id === d)).filter(Boolean);
    // 评审 = 角色 + 规则（任一人同意 / 所有人同意）
    const review = (roleField, ruleField) => {
      if (!edit) {
        return `<span class="tag">${esc(n[roleField])}</span> <span class="badge ${n[ruleField] === 'all' ? 'badge-orange' : 'badge-blue'}">${RULES[n[ruleField]]}</span>`;
      }
      return `<select class="cfg-input cfg-sm" data-cfg="${roleField}" data-node="${n.id}">${allRoles().map((r) => `<option ${n[roleField] === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select>
        <select class="cfg-input cfg-sm" data-cfg="${ruleField}" data-node="${n.id}">${Object.entries(RULES).map(([k, v]) => `<option value="${k}" ${n[ruleField] === k ? 'selected' : ''}>${v}</option>`).join('')}</select>
`;
    };
    return `
      <div class="card">
        <div class="card-h"><h3>${esc(n.name)}</h3>${modeBadge('collab')}<div class="grow"></div>
          <span class="small muted">节点 ID：<span class="tag mono">${n.id}</span></span></div>
        <div class="card-b">
          <div class="cfg-row"><span class="k">节点名称</span><span class="v">${edit
            ? `<input class="cfg-input" data-cfg="name" data-node="${n.id}" value="${esc(n.name)}">`
            : esc(n.name)}</span></div>
          <div class="cfg-row"><span class="k">执行方式</span><span class="v">${modeBadge('collab')} <span class="small muted">人下达指令、评审成果，Agent 执行并产出</span></span></div>
          <div class="cfg-row"><span class="k">执行角色<div class="small muted" style="font-weight:400">人机协同成员</div></span><span class="v">${edit
            ? `<select class="cfg-input cfg-sm" data-cfg="execRole" data-node="${n.id}">${allRoles().map((r) => `<option ${n.execRole === r ? 'selected' : ''}>${esc(r)}</option>`).join('')}</select>
               <input class="cfg-input cfg-sm" data-cfg="execRoleNew" data-node="${n.id}" placeholder="或输入新角色名" style="width:140px">`
            : `<span class="tag">${esc(n.execRole)}</span>`} <span class="small muted">项目中为该角色分配人和 AI Agent</span></span></div>
          <div class="cfg-row"><span class="k">产物名称</span><span class="v">${edit
            ? `<input class="cfg-input cfg-sm" data-cfg="output" data-node="${n.id}" value="${esc(n.output)}">`
            : `<span class="tag">${esc(n.output)}</span>`}</span></div>
          <div class="cfg-row"><span class="k">文件模板<div class="small muted" style="font-weight:400">产物格式</div></span><span class="v">${edit
            ? `<select class="cfg-input" data-cfg="artifact" data-node="${n.id}">
                 <option value="" ${def ? '' : 'selected'}>（不使用）</option>
                 ${ARTIFACT_DEFS.filter((d) => d.usage === 'artifact' || d.id === n.artifact).map((d) => `<option value="${d.id}" ${n.artifact === d.id ? 'selected' : ''}>📝 ${esc(d.name)}${d.attachment ? ` · ${d.attachment.ver}` : ' · 未上传文件'}</option>`).join('')}
               </select>`
            : def
              ? `<a href="#/file-templates/${def.id}">📝 ${esc(def.name)}${def.attachment ? ` · ${def.attachment.ver}` : ' · 未上传文件'}</a>`
              : '<span class="muted">未选择，Agent 按通用格式产出</span>'}</span></div>
          <div class="cfg-row"><span class="k">输入产物</span><span class="v">${inputs.length
            ? inputs.map((x) => `<span class="tag">${esc(x.output)}</span>`).join(' ') + ' <span class="small muted">由流程图上游节点决定</span>'
            : '<span class="small muted">流程起点，由人的指令开始</span>'}</span></div>
          <div class="cfg-row"><span class="k">准出评审<div class="small muted" style="font-weight:400">产出方把关</div></span><span class="v">${review('outRev', 'outRule')}</span></div>
          <div class="cfg-row"><span class="k">准入评审<div class="small muted" style="font-weight:400">下游把关</div></span><span class="v">${review('inRev', 'inRule')}</span></div>
          <div class="callout callout-info mt16"><span class="ico">ℹ️</span><div>${edit
            ? '修改保存在草稿中，发布后对新建流程生效。执行与评审写的都是<b>角色</b>，具体由哪些人和 AI Agent 担任在<b>项目</b>中分配；<b>任一人同意</b>：角色中有一人同意即通过，<b>所有人同意</b>：角色中每个人都同意才通过，任一人驳回即退回。'
            : '执行与评审写的都是<b>角色</b>，具体由哪些人和 AI Agent 担任在<b>项目</b>中分配；<b>任一人同意</b>：角色中有一人同意即通过，<b>所有人同意</b>：角色中每个人都同意才通过，任一人驳回即退回。'}</div></div>
        </div>
      </div>`;
  }

  function templateNodeName(id) {
    for (const t of TEMPLATES) { const n = t.nodes.find((x) => x.id === id); if (n) return n.name; }
    return id;
  }
  function revId(name) {
    const holders = reviewersOf(name);
    if (holders.length) return holders[0];
    const p = Object.values(PEOPLE).find((x) => x.name === name);
    return p ? p.id : 'zhang';
  }

  /* ---------------------------------------------------------
     8. 视图：文件模板管理
     --------------------------------------------------------- */
  const FT_MAX = 100 * 1024;
  const fmtSize = (b) => (b >= 1024 ? `${(b / 1024).toFixed(1)} KB` : `${b} B`);

  // 安全的 Markdown 渲染：先整体转义 HTML，再识别有限语法（标题、列表、表格、代码块、引用、加粗、行内代码）。
  // 不支持原始 HTML、图片、外链资源 —— 模板内容只做展示。
  function mdRender(src) {
    const inline = (t) => esc(t)
      .replace(/`([^`]+)`/g, '<code>$1</code>')
      .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
      .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    const lines = src.replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const l = lines[i];
      if (/^```/.test(l)) {
        const buf = [];
        i += 1;
        while (i < lines.length && !/^```/.test(lines[i])) { buf.push(lines[i]); i += 1; }
        out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`);
        i += 1; continue;
      }
      const h = /^(#{1,4})\s+(.*)$/.exec(l);
      if (h) { out.push(`<h${h[1].length}>${inline(h[2])}</h${h[1].length}>`); i += 1; continue; }
      if (/^\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const cells = (row) => row.trim().replace(/^\||\|$/g, '').split('|').map((c) => inline(c.trim()));
        const head = cells(l);
        i += 2;
        const rows = [];
        while (i < lines.length && /^\|.*\|\s*$/.test(lines[i])) { rows.push(cells(lines[i])); i += 1; }
        out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join('')}</tr></thead><tbody>${rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join('')}</tr>`).join('')}</tbody></table>`);
        continue;
      }
      if (/^>\s?/.test(l)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, '')); i += 1; }
        out.push(`<blockquote>${inline(buf.join(' '))}</blockquote>`); continue;
      }
      if (/^\s*([-*]|\d+\.)\s+/.test(l)) {
        const ordered = /^\s*\d+\./.test(l);
        const buf = [];
        while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) { buf.push(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, '')); i += 1; }
        out.push(`<${ordered ? 'ol' : 'ul'}>${buf.map((b) => `<li>${inline(b)}</li>`).join('')}</${ordered ? 'ol' : 'ul'}>`); continue;
      }
      if (!l.trim()) { i += 1; continue; }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,4}\s|```|>|\||\s*([-*]|\d+\.)\s)/.test(lines[i])) { buf.push(lines[i]); i += 1; }
      out.push(`<p>${inline(buf.join(' '))}</p>`);
    }
    return `<div class="md-body">${out.join('')}</div>`;
  }

  // 引用了这个文件模板的流程模板节点（按模板 id）
  function artifactUsage(def) {
    const out = [];
    TEMPLATES.forEach((t) => t.nodes.forEach((n) => { if (n.artifact === def.id) out.push({ t, n, how: '产物格式' }); }));
    return out;
  }
  const artifactDefByOutput = (output) => ARTIFACT_DEFS.find((d) => d.output === output);
  const ftVersions = (d) => (d.attachment ? [d.attachment].concat(d.history) : []);

  function viewFileTemplates() {
    const f = state.ftUsage || 'all';
    const list = ARTIFACT_DEFS
      .filter((d) => f === 'all' || d.usage === f)
      .sort((a, b) => (!!a.attachment - !!b.attachment) || ((b.attachment || {}).time || '').localeCompare((a.attachment || {}).time || ''));
    const missing = ARTIFACT_DEFS.filter((d) => !d.attachment).length;
    const count = (k) => ARTIFACT_DEFS.filter((d) => k === 'all' || d.usage === k).length;
    const tab = (k, label) => `<button class="${f === k ? 'on' : ''}" data-act="ft-usage" data-u="${k}">${label}<span class="n">${count(k)}</span></button>`;
    return `
      <div class="page-head">
        <div>
          <h1>文件模板管理</h1>
          <div class="sub">每个文件模板是一份 Markdown 规则文件：节点产物按它的格式产出，未来自动准出也按它判定。平台统一定义，所有团队共用。</div>
        </div>
        <div class="grow"></div>
        <button class="btn" data-act="noop">＋ 新建文件模板</button>
      </div>
      ${missing ? `<div class="callout callout-warn mb16"><span class="ico">⚠️</span><div>有 ${missing} 个文件模板还没有上传文件，引用它的节点只能按通用格式产出。</div></div>` : ''}
      <div class="proj-bar">
        <div class="proj-tabs">${tab('all', '全部')}${tab('artifact', '产物格式')}${tab('review_rule', '评审规则')}${tab('other', '其他')}</div>
      </div>
      <div class="tpl-grid">
        ${list.map((d) => `
          <div class="tpl-card" data-nav="#/file-templates/${d.id}">
            <div class="top">
              <div class="tpl-ico">${d.icon}</div>
              <div class="grow">
                <h3>${esc(d.name)}</h3>
                <div class="row mt8" style="gap:7px">
                  <span class="badge ${d.usage === 'review_rule' ? 'badge-purple' : 'badge-blue'}">${FT_USAGE[d.usage]}</span>
                  ${d.attachment ? `<span class="tag">${d.attachment.ver}</span>` : '<span class="badge badge-orange">未上传模板文件</span>'}
                </div>
              </div>
            </div>
            <div class="desc">${esc(d.desc)}</div>
            ${d.attachment
              ? `<div class="att-chip mt12" title="${esc(d.attachment.file)}"><span>📝</span><span class="att-chip-name">${esc(d.attachment.file)}</span><span class="muted">${fmtSize(d.attachment.size)}</span></div>`
              : '<div class="att-chip att-chip-empty mt12"><span>📎</span><span>点击进入上传 .md 文件</span></div>'}
            <div class="tpl-stats">
              <div>被引用<b>${artifactUsage(d).length}</b></div>
              <div>更新于<b style="font-size:12px;line-height:2.2">${d.attachment ? d.attachment.time.slice(0, 10) : '—'}</b></div>
            </div>
          </div>`).join('') || '<div class="empty">没有该用途的文件模板</div>'}
      </div>`;
  }

  function viewFileTemplateDetail(id) {
    const def = artifactDef(id);
    if (!def) return `<div class="empty"><span class="e-ico">📑</span>文件模板不存在</div>`;
    const att = def.attachment;
    const usage = artifactUsage(def);
    const versions = ftVersions(def);
    const shown = versions.find((v) => v.ver === state.ftVer) || att;
    const mode = state.ftView || 'render';

    const fileCard = att ? `
      <div class="att-file">
        <div class="att-file-ico">📝</div>
        <div class="grow">
          <div class="row" style="gap:8px"><strong>${esc(att.file)}</strong><span class="badge badge-green">当前生效 ${att.ver}</span></div>
          <div class="small muted mt4">${fmtSize(att.size)} · ${esc(actor(att.by).name)} 上传于 ${att.time} · ${esc(att.note)}</div>
        </div>
        <button class="btn btn-sm" data-act="ft-download" data-id="${def.id}" data-ver="${att.ver}">⬇ 下载</button>
        <button class="btn btn-sm btn-primary" data-act="ft-upload" data-id="${def.id}">⬆ 上传新版本</button>
      </div>
      <div class="callout callout-info mt12"><span class="ico">🤖</span><div>节点选用该模板后，Agent 会拿到这份 Markdown 原文，按它的结构与要求产出，并在提交前对照自查。上传新版本后，<b>新开工</b>的节点使用新版本，进行中的节点继续使用开工时的版本。</div></div>`
    : `
      <div class="att-drop" data-act="ft-upload" data-id="${def.id}">
        <div class="att-drop-ico">📎</div>
        <div><b>上传模板文件</b></div>
        <div class="small muted mt4">只支持 Markdown（.md），UTF-8 编码，不超过 100 KB</div>
        <button class="btn btn-primary btn-sm mt12">选择文件</button>
      </div>
      <div class="callout callout-warn mt12"><span class="ico">⚠️</span><div>未上传文件时，引用它的节点（${usage.map((u) => esc(u.n.name)).join('、') || '暂无'}）只能按通用格式产出，评审时也没有统一标准。</div></div>`;

    const contentCard = shown ? `
      <div class="card">
        <div class="card-h">
          <h3>模板内容</h3><span class="tag">${esc(shown.file)} · ${shown.ver}${shown === att ? '（生效）' : '（历史）'}</span>
          <div class="grow"></div>
          ${shown !== att ? `<button class="btn btn-sm" data-act="ft-ver" data-ver="${att.ver}">回到生效版本</button>` : ''}
          <div class="proj-tabs sm"><button class="${mode === 'render' ? 'on' : ''}" data-act="ft-view" data-v="render">渲染</button><button class="${mode === 'source' ? 'on' : ''}" data-act="ft-view" data-v="source">源码</button></div>
        </div>
        <div class="card-b">${mode === 'render' ? mdRender(shown.content) : `<pre class="md-src">${esc(shown.content)}</pre>`}</div>
      </div>` : '';

    return `
      <div class="page-head">
        <div>
          <h1>${def.icon} ${esc(def.name)}</h1>
          <div class="sub"><span class="badge ${def.usage === 'review_rule' ? 'badge-purple' : 'badge-blue'}">${FT_USAGE[def.usage]}</span> ${esc(def.desc)}</div>
        </div>
        <div class="grow"></div>
        <button class="btn" data-nav="#/file-templates">← 返回文件模板列表</button>
      </div>

      <div class="att-layout">
        <div class="stack" style="gap:16px">
          <div class="card">
            <div class="card-h"><h3>模板文件</h3><span class="small muted">Markdown · 规则 / 格式</span></div>
            <div class="card-b">${fileCard}</div>
          </div>
          ${contentCard}
        </div>

        <div class="stack" style="gap:16px">
          <div class="card">
            <div class="card-h"><h3>版本历史</h3><span class="small muted">点击查看</span></div>
            <div class="card-b">
              ${versions.length ? versions.map((v) => `
                <div class="list-row clickable ${shown === v ? 'ver-on' : ''}" data-act="ft-ver" data-ver="${v.ver}" style="align-items:flex-start">
                  <span class="badge ${v === att ? 'badge-green' : 'badge-gray'}">${v.ver}</span>
                  <div class="grow">
                    <div class="small" style="color:var(--ink-2)">${esc(v.note)}</div>
                    <div class="small muted">${esc(actor(v.by).name)} · ${v.time} · ${fmtSize(v.size)}</div>
                  </div>
                </div>`).join('') : '<div class="small muted">暂无</div>'}
            </div>
          </div>
          <div class="card">
            <div class="card-h"><h3>引用该模板的节点</h3><span class="count small muted">${usage.length}</span></div>
            <div class="card-b">
              ${usage.length ? usage.map((u) => `
                <div class="list-row clickable" data-nav="#/templates/${u.t.id}">
                  <span>${u.t.icon}</span>
                  <div class="grow"><div class="small"><strong>${esc(u.n.name)}</strong> · 产物「${esc(u.n.output)}」</div><div class="small muted">${esc(u.t.name)} ${u.t.version}</div></div>
                  <span class="tag">${u.how}</span>
                </div>`).join('') : `<div class="small muted">${def.usage === 'review_rule' ? '评审规则类模板将用于自动准出（规划中），暂无节点引用' : '暂无流程模板节点引用'}</div>`}
            </div>
          </div>
        </div>
      </div>
    `;
  }

  // 上传新版本：选 .md → 校验 → 预览 → 填更新说明 → 提交
  let ftPending = null; // { name, size, content }
  function openFtUploadModal(defId) {
    const d = artifactDef(defId);
    ftPending = null;
    const running = PROCESSES.filter((p) => p.status === 'running').reduce((n, p) => n + procTpl(p).nodes.filter((x) => x.artifact === d.id && ['running', 'review', 'admit', 'rejected'].includes(p.states[x.id])).length, 0);
    openModal({
      title: `上传新版本 · ${d.name}`,
      wide: true,
      body: `
        <div class="field">
          <label>模板文件</label>
          <input type="file" accept=".md,.markdown" data-ft-file>
          <div class="hint">只支持 Markdown（.md），UTF-8 编码，不超过 100 KB。</div>
          <div data-ft-check class="mt8"></div>
        </div>
        <div class="field" data-ft-preview-wrap hidden><label>内容预览</label><div class="ft-preview" data-ft-preview></div></div>
        <div class="field"><label>更新说明（必填）</label><textarea data-ft-note placeholder="例如：验收标准改为 Given/When/Then 格式"></textarea></div>
        <div class="callout callout-info"><span class="ico">ℹ️</span><div>上传后成为 <b>${d.attachment ? `v${parseInt(d.attachment.ver.slice(1), 10) + 1}` : 'v1'}</b> 并立即生效：新开工的节点使用新版本；进行中的 ${running} 个节点继续使用${d.attachment ? ` ${d.attachment.ver}` : '开工时的版本'}。</div></div>`,
      footer: `<button class="btn" data-close-modal>取消</button>
               <button class="btn btn-primary" data-act="ft-confirm" data-id="${d.id}">上传</button>`,
    });
  }
  // 与设计 §7.1 一致的校验：扩展名、大小、UTF-8、非空、内容未变化
  function checkFtFile(defId, file) {
    const box = $('[data-ft-check]');
    const fail = (msg) => { ftPending = null; box.innerHTML = `<span class="badge badge-red">✕ ${esc(msg)}</span>`; $('[data-ft-preview-wrap]').hidden = true; };
    if (!/\.(md|markdown)$/i.test(file.name)) return fail('只支持 .md 文件');
    if (file.size > FT_MAX) return fail(`文件 ${fmtSize(file.size)}，超过 100 KB`);
    file.arrayBuffer().then((buf) => {
      let text;
      try { text = new TextDecoder('utf-8', { fatal: true }).decode(buf); } catch { return fail('不是合法的 UTF-8 文本'); }
      if (text.includes('\u0000')) return fail('看起来是二进制文件');
      text = text.replace(/^\uFEFF/, ''); // 去掉 BOM
      if (!text.trim()) return fail('文件为空');
      const d = artifactDef(defId);
      if (d.attachment && d.attachment.content === text) return fail('与生效版本内容相同');
      ftPending = { name: file.name.replace(/\.markdown$/i, '.md'), size: new Blob([text]).size, content: text };
      box.innerHTML = `<span class="badge badge-green">✓ ${esc(file.name)} · ${fmtSize(ftPending.size)}</span>`;
      $('[data-ft-preview-wrap]').hidden = false;
      $('[data-ft-preview]').innerHTML = mdRender(text);
    });
  }
  function confirmFtUpload(defId) {
    const d = artifactDef(defId);
    const note = (($('[data-ft-note]') || {}).value || '').trim();
    if (!ftPending) { toast('请先选择一个通过校验的 .md 文件', 'warn'); return; }
    if (!note) { toast('请填写更新说明', 'warn'); return; }
    const prev = d.attachment;
    const ver = prev ? `v${parseInt(prev.ver.slice(1), 10) + 1}` : 'v1';
    if (prev) d.history.unshift(prev);
    d.attachment = ftVer(ver, ftPending.name, 'zhang', nowStr(), note, ftPending.content);
    addAudit('zhang', '上传文件模板', d.name, ver);
    ftPending = null;
    state.ftVer = null;
    closeModal();
    toast(`已上传「${d.attachment.file}」，成为「${d.name}」${ver}；新开工的节点按此版本产出`, 'ok', 3600);
    render();
  }
  function downloadFt(defId, ver) {
    const d = artifactDef(defId);
    const v = ftVersions(d).find((x) => x.ver === ver);
    if (!v) return;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([v.content], { type: 'text/markdown;charset=utf-8' }));
    a.download = v.file;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  /* ---------------------------------------------------------
     9. 视图：团队管理 → 项目 → 流程
     --------------------------------------------------------- */
  const memberChip = (id) => `<span class="mchip ${actor(id).kind === 'ai' ? 'ai' : ''}">${avatarEl(id, 'sm')}${esc(actor(id).name)}</span>`;
  // 某个成员在团队各项目里担任的角色
  function rolesOfMember(teamId, id) {
    const out = [];
    PROJECTS.filter((pj) => pj.teamId === teamId).forEach((pj) => {
      Object.entries(pj.assign).forEach(([role, ids]) => { if (ids.includes(id)) out.push(`${pj.name} · ${role}`); });
    });
    return out;
  }
  // 项目的角色分配完成度
  function assignProgress(pj) {
    const t = templateById(pj.templateId);
    const r = templateRoles(t);
    const all = r.exec.concat(r.review);
    const done = all.filter((x) => (pj.assign[x] || []).length).length;
    return { all, done, missing: all.filter((x) => !(pj.assign[x] || []).length) };
  }

  function viewTeams() {
    return `
      <div class="page-head">
        <div>
          <h1>团队管理</h1>
          <div class="sub">团队由人和 AI Agent 组成；团队里创建项目，项目绑定流程模板，并从团队中为模板角色分配人和 Agent。</div>
        </div>
        <div class="grow"></div>
        <button class="btn" data-act="noop">＋ 新建团队</button>
      </div>
      <div class="tpl-grid">
        ${TEAMS.map((t) => {
          const pjs = PROJECTS.filter((x) => x.teamId === t.id);
          const running = PROCESSES.filter((p) => pjs.some((x) => x.id === p.projectId) && p.status === 'running').length;
          return `
          <div class="tpl-card" data-nav="#/teams/${t.id}">
            <div class="top">
              <div class="tpl-ico">🏢</div>
              <div class="grow"><h3>${esc(t.name)}</h3><div class="small muted mt4">${esc(t.desc)}</div></div>
            </div>
            <div class="row mt12" style="gap:6px;flex-wrap:wrap">
              <span class="avatar-stack">${t.members.map((id) => avatarEl(id, 'sm')).join('')}</span>
              <span class="avatar-stack" style="margin-left:6px">${t.agents.map((id) => avatarEl(id, 'sm')).join('')}</span>
            </div>
            <div class="tpl-stats">
              <div>成员<b>${t.members.length}</b></div>
              <div>AI Agent<b>${t.agents.length}</b></div>
              <div>项目<b>${pjs.length}</b></div>
              <div>进行中流程<b>${running}</b></div>
            </div>
          </div>`;
        }).join('')}
      </div>`;
  }

  function viewTeamDetail(id) {
    const t = teamById(id);
    if (!t) return `<div class="empty"><span class="e-ico">🏢</span>团队不存在</div>`;
    const pjs = PROJECTS.filter((x) => x.teamId === t.id);
    return `
      <div class="page-head">
        <div><h1>🏢 ${esc(t.name)}</h1><div class="sub">${esc(t.desc)}</div></div>
        <div class="grow"></div>
        <button class="btn" data-nav="#/teams">← 返回团队列表</button>
      </div>

      <div class="section-title"><h2>项目</h2><span class="count">${pjs.length} 个</span><div class="grow" style="flex:1"></div>
        <button class="btn btn-primary btn-sm" data-act="new-project" data-team-id="${t.id}">＋ 新建项目</button></div>
      <div class="tpl-grid">
        ${pjs.map((pj) => {
          const tpl = templateById(pj.templateId);
          const ap = assignProgress(pj);
          const procs = PROCESSES.filter((p) => p.projectId === pj.id);
          return `
          <div class="tpl-card" data-nav="#/projects/${pj.id}">
            <div class="top">
              <div class="tpl-ico">📁</div>
              <div class="grow"><h3>${esc(pj.name)}</h3><div class="small muted mt4">${esc(pj.desc)}</div></div>
            </div>
            <div class="row mt12" style="gap:7px;flex-wrap:wrap">
              <span class="tag">${tpl.icon} ${esc(tpl.name)} ${tpl.version}</span>
              ${ap.missing.length ? `<span class="badge badge-orange">${ap.missing.length} 个角色未分配</span>` : '<span class="badge badge-green">角色已分配</span>'}
            </div>
            <div class="tpl-stats">
              <div>流程<b>${procs.length}</b></div>
              <div>进行中<b>${procs.filter((p) => p.status === 'running').length}</b></div>
              <div>角色<b>${ap.done}/${ap.all.length}</b></div>
            </div>
          </div>`;
        }).join('') || '<div class="empty">还没有项目</div>'}
      </div>

      <div class="team-cols">
        <div class="card">
          <div class="card-h"><h3>成员</h3><span class="count small muted">${t.members.length} 人</span><div class="grow"></div>
            <button class="btn btn-sm" data-act="noop">＋ 添加成员</button></div>
          <div class="card-b">
            <table class="table">
              <thead><tr><th>成员</th><th>岗位</th><th>在项目中担任的角色</th></tr></thead>
              <tbody>${t.members.map((mid) => {
                const roles = rolesOfMember(t.id, mid);
                return `<tr><td style="white-space:nowrap"><span class="row" style="gap:8px">${avatarEl(mid, 'sm')}<strong>${esc(actor(mid).name)}</strong></span></td>
                  <td style="white-space:nowrap">${esc(actor(mid).title || '')}</td>
                  <td>${roles.map((r) => `<span class="tag">${esc(r)}</span>`).join(' ') || '<span class="muted">—</span>'}</td></tr>`;
              }).join('')}</tbody>
            </table>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h3>AI Agent</h3><span class="count small muted">${t.agents.length} 个</span><div class="grow"></div>
            <button class="btn btn-sm" data-act="noop">＋ 添加 Agent</button></div>
          <div class="card-b agent-list">
            ${t.agents.map((aid) => {
              const a = actor(aid);
              const roles = rolesOfMember(t.id, aid);
              return `
                <div class="agent-row">
                  ${avatarEl(aid)}
                  <div class="grow">
                    <div class="row" style="gap:8px"><strong>${esc(a.name)}</strong><span class="tag mono">${esc(a.model || '')}</span></div>
                    <div class="small muted">${esc(a.desc || '')}</div>
                    <div class="mt4">${roles.map((r) => `<span class="tag">${esc(r)}</span>`).join(' ') || '<span class="small muted">未在项目中担任角色</span>'}</div>
                  </div>
                </div>`;
            }).join('')}
          </div>
        </div>
      </div>`;
  }

  const ME = 'zhang'; // 原型的当前登录用户（张明）

  // 流程现在卡在哪、等谁：返回进行中的节点与各自在等的人
  function processNow(p) {
    const tpl = procTpl(p);
    return tpl.nodes
      .filter((n) => ['running', 'review', 'admit', 'rejected'].includes(p.states[n.id]))
      .map((n) => {
        const st = p.states[n.id];
        let waiting = [], verb = '协同';
        if (st === 'review' || st === 'admit') {
          const v = voteState(p, n, st === 'review' ? 'out' : 'in');
          waiting = v.people.filter((id) => v.votes[id] !== 'ok');
          verb = '审批';
        } else {
          waiting = execMembers(n.id, p).filter((id) => actor(id).kind !== 'ai');
        }
        return { n, st, waiting, verb, mine: waiting.includes(ME) };
      });
  }

  function processRow(p) {
    const tpl = procTpl(p);
    const now = processNow(p);
    const done = tpl.nodes.filter((n) => p.states[n.id] === 'passed').length;
    const mine = now.filter((x) => x.mine);
    return `
      <div class="proc-row ${mine.length ? 'mine' : ''}" data-nav="#/process/${p.id}">
        <div class="proc-main">
          <div class="row" style="gap:8px;flex-wrap:wrap">
            <strong class="proc-name">${esc(p.name)}</strong>
            ${statusBadge(p.status)}
            ${mine.map((x) => `<span class="badge badge-red">待你${x.verb} · ${esc(x.n.name)}</span>`).join('')}
          </div>
          <div class="proc-now">
            ${p.status === 'passed' ? '<span class="muted">全部节点已通过</span>' : now.map((x) => `
              <span class="now-item">${statusBadge(x.st)} <b>${esc(x.n.name)}</b>
                <span class="muted">· 等 ${x.waiting.length ? x.waiting.map((id) => esc(actor(id).name)).join('、') : (x.verb === '协同' ? 'Agent 产出' : '分配评审人')}${x.verb === '审批' ? ' 审批' : ''}</span>
              </span>`).join('')}
          </div>
          <div class="pf-mini"><div class="pf-row">${processFlowHtml(p, tpl, null, false)}</div></div>
        </div>
        <div class="proc-side">
          <div class="proc-progress"><i style="width:${Math.round((done / tpl.nodes.length) * 100)}%"></i></div>
          <div class="small muted">${done}/${tpl.nodes.length} 节点</div>
          <div class="small muted mt8">${esc(actor(p.creator).name)} 发起</div>
          <div class="small muted">${p.created}</div>
        </div>
      </div>`;
  }

  // 项目首页的流程列表（筛选 + 搜索）；待我处理的排前面
  function projectListHtml(pj) {
    const all = PROCESSES.filter((p) => p.projectId === pj.id);
    const isMine = (p) => processNow(p).some((x) => x.mine);
    const f = state.projFilter || 'all';
    const q = (state.projQuery || '').trim();
    const list = all
      .filter((p) => f === 'all' || (f === 'running' && p.status === 'running') || (f === 'mine' && isMine(p)) || (f === 'done' && p.status === 'passed'))
      .filter((p) => !q || p.name.includes(q))
      .sort((a, b) => (isMine(b) - isMine(a)) || (a.status === 'passed') - (b.status === 'passed') || b.created.localeCompare(a.created));
    if (!all.length) {
      return `<div class="empty"><span class="e-ico">🔀</span>项目里还没有流程<br><button class="btn btn-primary mt12" data-act="start-process" data-project="${pj.id}">＋ 发起第一个流程</button></div>`;
    }
    return list.map(processRow).join('') || '<div class="empty"><span class="e-ico">🔍</span>没有符合条件的流程</div>';
  }

  function viewProject(id) {
    const pj = projectById(id);
    if (!pj) return `<div class="empty"><span class="e-ico">📁</span>项目不存在</div>`;
    const t = teamById(pj.teamId);
    const tpl = templateById(pj.templateId);
    const ap = assignProgress(pj);
    const all = PROCESSES.filter((p) => p.projectId === pj.id);
    const count = {
      all: all.length,
      running: all.filter((p) => p.status === 'running').length,
      mine: all.filter((p) => processNow(p).some((x) => x.mine)).length,
      done: all.filter((p) => p.status === 'passed').length,
    };
    const f = state.projFilter || 'all';
    const tab = (k, label) => `<button class="${f === k ? 'on' : ''}" data-act="proj-filter" data-f="${k}">${label}<span class="n">${count[k]}</span></button>`;
    return `
      <div class="page-head">
        <div>
          <h1>📁 ${esc(pj.name)}</h1>
          <div class="sub">${esc(t.name)} · ${tpl.icon} ${esc(tpl.name)} ${tpl.version} · ${esc(pj.desc)}</div>
        </div>
        <div class="grow"></div>
        <button class="btn" data-nav="#/projects/${pj.id}/settings">⚙ 项目设置</button>
        <button class="btn btn-primary" data-act="start-process" data-project="${pj.id}">＋ 发起流程</button>
      </div>

      ${ap.missing.length ? `
        <div class="callout callout-warn mb16"><span class="ico">⚠️</span>
          <div class="grow">${ap.missing.length} 个角色未分配，流转到相应节点时将无人协同或审批。</div>
          <a href="#/projects/${pj.id}/settings">去配置 →</a>
        </div>` : ''}

      <div class="proj-bar">
        <div class="proj-tabs">${tab('all', '全部')}${tab('mine', '待我处理')}${tab('running', '进行中')}${tab('done', '已完成')}</div>
        <div class="grow"></div>
        <input class="proj-search" data-proj-search placeholder="搜索流程" value="${esc(state.projQuery || '')}">
      </div>
      <div class="proc-list" id="proj-list" data-project="${pj.id}">${projectListHtml(pj)}</div>`;
  }

  // 项目设置：低频操作集中在这里
  function viewProjectSettings(id) {
    const pj = projectById(id);
    if (!pj) return `<div class="empty"><span class="e-ico">📁</span>项目不存在</div>`;
    const t = teamById(pj.teamId);
    const tpl = templateById(pj.templateId);
    const roles = templateRoles(tpl);
    const ap = assignProgress(pj);
    const row = (role, kind) => {
      const nodes = kind === 'exec'
        ? tpl.nodes.filter((n) => n.execRole === role).map((n) => n.name)
        : [].concat(
          tpl.nodes.filter((n) => n.outRev === role).map((n) => `${n.name}·准出`),
          tpl.nodes.filter((n) => n.inRev === role).map((n) => `${n.name}·准入`));
      const ids = pj.assign[role] || [];
      return `
        <tr class="${ids.length ? '' : 'unassigned'}">
          <td><strong>${esc(role)}</strong></td>
          <td class="small">${nodes.map((x) => `<span class="tag">${esc(x)}</span>`).join(' ')}</td>
          <td>${ids.length ? ids.map(memberChip).join(' ') : '<span class="badge badge-orange">未分配</span>'}</td>
          <td style="text-align:right"><button class="btn btn-sm" data-act="assign-role" data-project="${pj.id}" data-role="${esc(role)}">分配</button></td>
        </tr>`;
    };
    return `
      <div class="page-head">
        <div><h1>⚙ 项目设置</h1><div class="sub">${esc(pj.name)} · ${esc(t.name)}</div></div>
        <div class="grow"></div>
        <button class="btn" data-nav="#/projects/${pj.id}">← 返回项目</button>
      </div>

      <div class="settings">
        <nav class="settings-nav">
          <a href="#ps-basic" data-act="ps-jump" data-to="ps-basic">基本信息</a>
          <a href="#ps-tpl" data-act="ps-jump" data-to="ps-tpl">流程模板</a>
          <a href="#ps-roles" data-act="ps-jump" data-to="ps-roles">角色分配 <span class="${ap.missing.length ? 'warn-dot' : ''}">${ap.done}/${ap.all.length}</span></a>
        </nav>
        <div class="stack" style="gap:16px">
          <div class="card" id="ps-basic">
            <div class="card-h"><h3>基本信息</h3></div>
            <div class="card-b">
              <div class="field"><label>项目名称</label><input type="text" data-ps-name value="${esc(pj.name)}"></div>
              <div class="field"><label>项目说明</label><input type="text" data-ps-desc value="${esc(pj.desc)}"></div>
              <button class="btn btn-primary btn-sm" data-act="save-project" data-project="${pj.id}">保存</button>
            </div>
          </div>

          <div class="card" id="ps-tpl">
            <div class="card-h"><h3>流程模板</h3></div>
            <div class="card-b">
              <div class="field">
                <label>绑定的流程模板</label>
                <select data-ps-tpl data-project="${pj.id}">${TEMPLATES.map((x) => `<option value="${x.id}" ${x.id === pj.templateId ? 'selected' : ''}>${x.icon} ${x.name} ${x.version}</option>`).join('')}</select>
                <div class="hint">新发起的流程使用模板当前版本；进行中的流程按发起时的版本继续，不受影响。更换模板后，需为新模板涉及的角色补充分配。</div>
              </div>
              <a href="#/templates/${tpl.id}">查看「${esc(tpl.name)}」→</a>
            </div>
          </div>

          <div class="card" id="ps-roles">
            <div class="card-h"><h3>角色分配</h3><span class="count small muted">${ap.done}/${ap.all.length}</span><div class="grow"></div>
              <span class="small muted">从团队「${esc(t.name)}」中选择人和 AI Agent</span></div>
            <div class="card-b">
              <table class="table role-table">
                <thead><tr><th style="width:140px">角色</th><th>涉及节点</th><th>成员（人 / AI Agent）</th><th style="width:80px"></th></tr></thead>
                <tbody>
                  <tr class="grp"><td colspan="4">执行角色 · 人机协同完成节点</td></tr>
                  ${roles.exec.map((r) => row(r, 'exec')).join('')}
                  <tr class="grp"><td colspan="4">评审角色 · 准出 / 准入审批</td></tr>
                  ${roles.review.map((r) => row(r, 'review')).join('')}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>`;
  }

  // 节点的人机协同成员：项目里分配给该节点执行角色的人和 Agent
  function execMembers(nodeId, p = currentProcess()) {
    const tpl = p && procTpl(p);
    const node = tpl && tpl.nodes.find((n) => n.id === nodeId);
    return node ? reviewersOf(node.execRole, p) : [];
  }
  // 执行该节点的 Agent：取执行角色下的第一个 Agent；没分配时退回默认 Agent（原型兜底）
  function agentForNode(nodeId, p = currentProcess()) {
    const ai = execMembers(nodeId, p).find((id) => actor(id).kind === 'ai');
    if (ai) return ai;
    const map = { clarify: 'clarifier', prd: 'pmAgent', tech: 'archAgent', testcase: 'qaAgent', code: 'coderAgent', testexec: 'testAgent', release: 'releaseAgent', alert: 'opsAgent', diagnose: 'opsAgent', postmortem: 'opsAgent', fetch: 'dataAgent', model: 'dataAgent', report: 'dataAgent' };
    return map[nodeId] || 'pmAgent';
  }
  function teamName(id) { const t = TEAMS.find((x) => x.id === id); return t ? t.name : id; }

  /* ---------------------------------------------------------
     10. 视图：流程实例 / 节点工作台
     --------------------------------------------------------- */
  // 某节点某阶段（out 准出 / in 准入）的审批情况
  function voteState(p, node, stage) {
    const role = stage === 'out' ? node.outRev : node.inRev;
    const rule = (stage === 'out' ? node.outRule : node.inRule) || 'any';
    const people = reviewersOf(role, p);
    const votes = ((p.votes || {})[node.id] || {})[stage] || {};
    const ok = people.filter((x) => votes[x] === 'ok').length;
    const need = rule === 'all' ? people.length : Math.min(1, people.length);
    return { role, rule, people, votes, ok, need, done: people.length > 0 && ok >= need };
  }
  // 节点在流程图上的一行状态说明
  function nodeSub(p, n) {
    const st = p.states[n.id] || 'pending';
    if (st === 'review') { const v = voteState(p, n, 'out'); return `准出 ${v.ok}/${v.need}`; }
    if (st === 'admit') { const v = voteState(p, n, 'in'); return `准入 ${v.ok}/${v.need}`; }
    return { pending: '待开始', running: '人机协同中', passed: '已通过', rejected: '已打回' }[st] || '';
  }

  function viewProcess(id) {
    const p = processById(id);
    if (!p) return `<div class="empty"><span class="e-ico">🔀</span>流程不存在</div>`;
    const tpl = procTpl(p);
    const curId = state.procSelNode && p.states[state.procSelNode] ? state.procSelNode : p.current;
    const curNode = tpl.nodes.find((n) => n.id === curId) || tpl.nodes[0];
    const st = p.states[curNode.id] || 'pending';
    const done = tpl.nodes.filter((n) => p.states[n.id] === 'passed').length;

    return `
      <div class="page-head">
        <div>
          <h1>${esc(p.name)}</h1>
          <div class="sub">${esc(teamName(projectById(p.projectId).teamId))} · 项目 <a href="#/projects/${p.projectId}">${esc(projectById(p.projectId).name)}</a> · 基于 ${esc(tpl.name)} ${tpl.version} · 创建者 ${esc(actor(p.creator).name)} · ${p.created}</div>
        </div>
        <div class="grow"></div>
        <span class="small muted">进度 <b style="color:var(--ink)">${done}/${tpl.nodes.length}</b></span>
        ${statusBadge(p.status)}
        <button class="btn btn-sm" data-nav="#/projects/${p.projectId}">← 返回项目</button>
      </div>

      <div class="card pf-card mb16">
        <div class="pf-row">${processFlowHtml(p, tpl, curNode.id)}</div>
        ${p.returns.map((r) => `
          <div class="row mt8" style="gap:8px">
            <span class="return-note">↩ 退回：${esc(templateNodeName(r.from))} → ${esc(templateNodeName(r.to))}</span>
            <span class="small muted">批注：${esc(r.comment)}</span>
          </div>`).join('')}
      </div>

      <div class="wb2">
        ${collabPanel(p, curNode, st)}
        <div class="stack wb2-side" style="gap:16px">
          ${approvalPanel(p, curNode, st)}
          ${artifactPanel(p, tpl, curNode)}
        </div>
      </div>
    `;
  }

  // 流程图：按模板的 flow 画，并行组里每条轨道一行；节点带状态与审批进度
  function processFlowHtml(p, tpl, curId, interactive = true) {
    const byId = Object.fromEntries(tpl.nodes.map((n) => [n.id, n]));
    const ICON = { passed: '✓', rejected: '↩', review: '!', admit: '!', running: '●', pending: '○' };
    const node = (id) => {
      const n = byId[id];
      const st = p.states[id] || 'pending';
      return `
        <div class="pf-node pf-${STATUS[st].cls} ${id === curId ? 'sel' : ''}" ${interactive ? `data-act="switch-node" data-p="${p.id}" data-node="${id}"` : ''}>
          <span class="pf-dot ${st === 'running' || st === 'review' || st === 'admit' ? 'pulse' : ''}">${ICON[st]}</span>
          <span class="pf-txt"><span class="pf-name">${esc(n.name)}</span><span class="pf-sub">${esc(nodeSub(p, n))}</span></span>
        </div>`;
    };
    return tpl.flow.map((step, k) => {
      const arrow = k ? '<div class="pf-arrow"></div>' : '';
      if (step.type === 'node') return arrow + node(step.id);
      return arrow + `
        <div class="pf-par">
          ${step.tracks.map((tr) => `<div class="pf-track">${tr.nodes.map((id, i) => `${i ? '<div class="pf-arrow pf-arrow-s"></div>' : ''}${node(id)}`).join('')}</div>`).join('')}
        </div>`;
    }).join('');
  }

  // 主操作区：人机协同
  function collabPanel(p, n, st) {
    const agent = agentForNode(n.id);
    const canTalk = st === 'running' || st === 'rejected' || st === 'review' || st === 'admit';
    const hint = {
      pending: '上游产物还没有全部通过，节点尚未开始',
      passed: '节点已通过，对话只读',
    }[st];
    return `
      <div class="card chat collab">
        <div class="card-h">
          ${avatarEl(agent, 'sm')}
          <div>
            <h3>人机协同 · ${esc(n.name)}</h3>
            <div class="small muted">执行角色 <b>${esc(n.execRole)}</b>：${execMembers(n.id, p).map((id) => esc(actor(id).name)).join(' + ') || '未分配'} · 人下达指令、回答澄清，Agent 执行并产出</div>
          </div>
          <div class="grow"></div>
          ${statusBadge(st)}
        </div>
        <div class="chat-body" id="chat-body">${renderChat(p.id, n.id)}</div>
        ${canTalk ? `
          <form class="chat-input collab-input" id="chat-form" data-process="${p.id}" data-node="${n.id}">
            <textarea placeholder="向 Agent 下达指令、回答它的澄清问题…（Enter 发送，Shift+Enter 换行）" data-chat-input></textarea>
            <div class="collab-bar">
              <span class="small muted">产物：${esc(n.output)}</span>
              <div class="grow"></div>
              ${st === 'running' || st === 'rejected'
                ? `<button class="btn btn-sm" type="button" data-act="node-action" data-kind="submit" data-p="${p.id}" data-node="${n.id}">📨 提交准出评审</button>`
                : ''}
              <button class="btn btn-primary btn-sm" type="submit">发送</button>
            </div>
          </form>` : `<div class="collab-off small muted">🔒 ${hint}</div>`}
      </div>`;
  }

  // 当前节点的准出 / 准入审批
  function approvalPanel(p, n, st) {
    const block = (stage) => {
      const v = voteState(p, n, stage);
      const active = (stage === 'out' && st === 'review') || (stage === 'in' && st === 'admit');
      const finished = stage === 'out' ? ['admit', 'passed'].includes(st) : st === 'passed';
      const tag = finished ? '<span class="badge badge-green">已通过</span>'
        : active ? `<span class="badge badge-orange">进行中 ${v.ok}/${v.need}</span>`
        : '<span class="badge badge-gray">未开始</span>';
      return `
        <div class="ap-block ${active ? 'on' : ''}">
          <div class="ap-h">
            <strong>${stage === 'out' ? '准出评审' : '准入评审'}</strong>
            <span class="small muted">${stage === 'out' ? '产出方把关' : '下游把关'}</span>
            <div class="grow"></div>${tag}
          </div>
          <div class="small muted ap-rule">${esc(v.role)} · <b>${RULES[v.rule]}</b>（共 ${v.people.length} 人${v.rule === 'all' ? '，需全部同意' : '，一人同意即可'}）</div>
          ${v.people.map((id) => {
            const d = v.votes[id];
            return `
              <div class="ap-person">
                ${avatarEl(id, 'sm')}<span>${esc(actor(id).name)}</span>
                <div class="grow"></div>
                ${d === 'ok' ? '<span class="badge badge-green">已同意</span>'
                  : d === 'no' ? '<span class="badge badge-red">已驳回</span>'
                  : active ? `
                    <button class="btn btn-sm btn-success" data-act="vote" data-v="ok" data-stage="${stage}" data-person="${id}" data-p="${p.id}" data-node="${n.id}">同意</button>
                    <button class="btn btn-sm btn-danger" data-act="vote" data-v="no" data-stage="${stage}" data-person="${id}" data-p="${p.id}" data-node="${n.id}">驳回</button>`
                  : '<span class="small muted">待审</span>'}
              </div>`;
          }).join('')}
        </div>`;
    };
    const tip = st === 'running' || st === 'rejected'
      ? '<div class="callout callout-info mt12"><span class="ico">ℹ️</span><div>Agent 产出后，在人机协同区点击「提交准出评审」。</div></div>'
      : st === 'pending' ? '<div class="callout callout-info mt12"><span class="ico">ℹ️</span><div>节点尚未开始。</div></div>' : '';
    return `
      <div class="card">
        <div class="card-h"><h3>审批 · ${esc(n.name)}</h3><div class="grow"></div><span class="small muted" title="原型只有一个登录用户，为了演示多人审批">原型：可代任一评审人操作</span></div>
        <div class="card-b">${block('out')}${block('in')}${tip}</div>
      </div>`;
  }

  // 本流程的产物：列表 + 预览
  function artifactPanel(p, tpl, curNode) {
    const items = tpl.nodes.filter((n) => (p.states[n.id] || 'pending') !== 'pending');
    const selId = items.some((n) => n.id === state.procArtSel) ? state.procArtSel : curNode.id;
    const sel = tpl.nodes.find((n) => n.id === selId) || curNode;
    return `
      <div class="card">
        <div class="card-h"><h3>产物</h3><span class="count small muted">${items.filter((n) => NODE_DOC[n.id]).length} 份</span></div>
        <div class="card-b">
          <div class="art-list">
            ${items.map((n) => {
              const doc = NODE_DOC[n.id];
              return `
                <button class="art-item ${n.id === sel.id ? 'on' : ''}" data-act="art-sel" data-node="${n.id}">
                  <span class="art-name">📄 ${esc(n.output)}</span>
                  <span class="small muted">${doc ? doc.version : '生成中'}</span>
                  ${statusBadge(p.states[n.id])}
                </button>`;
            }).join('') || '<div class="small muted">还没有产物</div>'}
          </div>
          ${(() => {
            const d = sel.artifact && artifactDef(sel.artifact);
            return d && d.attachment
              ? `<div class="small muted mb8">依据文件模板：<a href="#/file-templates/${d.id}">📝 ${esc(d.name)} ${d.attachment.ver}</a></div>`
              : '<div class="small muted mb8">依据文件模板：无（按通用格式产出）</div>';
          })()}
          <div class="art-preview">${nodeDoc(sel)}</div>
        </div>
      </div>`;
  }

  function renderChat(pid, nodeId) {
    const msgs = chatFor(pid, nodeId);
    return msgs.map((m) => {
      if (m.from === 'sys') return `<div class="msg sys"><div class="bubble">⚙️ ${esc(m.text)} · ${m.t}</div></div>`;
      const isMe = m.from === 'human';
      return `
        <div class="msg ${isMe ? 'me' : ''}">
          ${avatarEl(m.who, 'sm')}
          <div>
            <div class="who">${esc(actor(m.who).name)}<span>· ${m.t}</span></div>
            <div class="bubble">${esc(m.text)}</div>
          </div>
        </div>`;
    }).join('');
  }

  function nodeDoc(n) {
    const doc = NODE_DOC[n.id];
    if (!doc) {
      return `<div class="empty"><span class="e-ico">📄</span>节点「${esc(n.name)}」尚未生成产出物<br><span class="small">下发指令后由 Agent 生成</span></div>`;
    }
    return `
      <div class="doc">
        <div class="doc-h">
          <span style="font-size:18px">📄</span>
          <div class="grow">
            <strong style="font-size:13.5px">${esc(doc.title)}</strong>
            <div class="small muted">owner ${esc(actor(doc.owner).name)} · ${doc.version}</div>
          </div>
          <span class="badge badge-purple">${doc.version}</span>
        </div>
        <div class="doc-b">
          ${doc.sections.map((s) => `
            <div class="doc-sec">
              <h4>${esc(s.h)} <span class="ok">✓ 已填</span></h4>
              <p>${esc(s.p)}</p>
            </div>`).join('')}
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------
     12. 路由 & 渲染
     --------------------------------------------------------- */
  function render() {
    const route = state.route || '#/teams';
    const seg = route.replace('#/', '').split('/').filter(Boolean);
    const content = $('#content');
    let html = '';
    const first = seg[0] || 'teams';

    if (first === 'templates') html = seg[1] ? viewTemplateDetail(seg[1]) : viewTemplates();
    else if (first === 'file-templates') html = seg[1] ? viewFileTemplateDetail(seg[1]) : viewFileTemplates();
    else if (first === 'teams') html = seg[1] ? viewTeamDetail(seg[1]) : viewTeams();
    else if (first === 'projects') html = seg[2] === 'settings' ? viewProjectSettings(seg[1]) : viewProject(seg[1]);
    else if (first === 'process') html = viewProcess(seg[1] || 'p1');
    else html = viewTeams();

    content.innerHTML = html;
    renderTopbar();
    renderNav();

    // 对话区滚到底
    const cb = $('#chat-body');
    if (cb) cb.scrollTop = cb.scrollHeight;
    window.scrollTo({ top: 0 });
  }

  function navigate(hash) {
    if (location.hash === hash) { render(); return; }
    location.hash = hash;
  }

  function onRoute() {
    state.route = location.hash || '#/teams';
    state.tplSelNode = null;
    state.procSelNode = null;
    state.insMenu = null;
    if (!/^#\/projects\//.test(state.route)) { state.projFilter = 'all'; state.projQuery = ''; }
    state.ftVer = null;
    if (state.tplEdit && !state.route.startsWith(`#/templates/${state.tplEdit.id}`)) state.tplEdit = null;
    render();
  }

  /* ---------------------------------------------------------
     13. 交互动作
     --------------------------------------------------------- */
  // 节点动作：提交准出评审
  function nodeAction(kind, pid, nodeId) {
    const p = processById(pid);
    if (!p) return;
    const node = procTpl(p).nodes.find((n) => n.id === nodeId);
    if (kind === 'submit') {
      p.states[nodeId] = 'review';
      p.votes = p.votes || {};
      p.votes[nodeId] = { out: {}, in: {} };
      const v = voteState(p, node, 'out');
      pushChat(pid, nodeId, { from: 'sys', who: '', text: `产物已提交准出评审：${roleLabel(node.outRev)} · ${RULES[v.rule]}`, t: hhmm() });
      addAudit(agentForNode(nodeId), '提交准出', `${pid} · ${node.name}`, currentVer(nodeId));
      toast(`已提交准出评审，等待 ${roleLabel(node.outRev)}（${RULES[v.rule]}）`, 'info');
      render();
    }
  }

  // 评审人投票。驳回走弹窗（要写批注、可选退回到哪）
  function vote(pid, nodeId, stage, person, v) {
    const p = processById(pid);
    const node = procTpl(p).nodes.find((n) => n.id === nodeId);
    if (v === 'no') { openRejectModal(pid, nodeId, stage, person); return; }
    p.votes = p.votes || {};
    p.votes[nodeId] = p.votes[nodeId] || { out: {}, in: {} };
    p.votes[nodeId][stage] = p.votes[nodeId][stage] || {};
    p.votes[nodeId][stage][person] = 'ok';
    const name = stage === 'out' ? '准出' : '准入';
    addAudit(person, `${name}同意`, `${pid} · ${node.name}`, currentVer(nodeId));
    const vs = voteState(p, node, stage);
    if (!vs.done) {
      pushChat(pid, nodeId, { from: 'sys', who: '', text: `${actor(person).name} ${name}同意（${vs.ok}/${vs.need}，${RULES[vs.rule]}）`, t: hhmm() });
      toast(`${actor(person).name} 已同意，${name}进度 ${vs.ok}/${vs.need}`, 'info');
      render();
      return;
    }
    if (stage === 'out') {
      p.states[nodeId] = 'admit';
      const vi = voteState(p, node, 'in');
      pushChat(pid, nodeId, { from: 'sys', who: '', text: `准出通过（${vs.ok}/${vs.need}），进入准入评审：${roleLabel(node.inRev)} · ${RULES[vi.rule]}`, t: hhmm() });
      toast(`准出通过 → 等待准入评审：${roleLabel(node.inRev)}`, 'ok');
      render();
      return;
    }
    pushChat(pid, nodeId, { from: 'sys', who: '', text: `准入通过（${vs.ok}/${vs.need}），产物归档`, t: hhmm() });
    completeNode(p, node);
  }

  // 准入通过：节点完成，上游全部完成的下游节点自动开工
  function completeNode(p, node) {
    const tpl = procTpl(p);
    p.states[node.id] = 'passed';
    const started = [];
    tpl.nodes.forEach((n) => {
      if ((p.states[n.id] || 'pending') !== 'pending') return;
      if (n.deps.length && n.deps.every((d) => p.states[d] === 'passed')) {
        p.states[n.id] = 'running';
        started.push(n);
      }
    });
    const allPassed = tpl.nodes.every((n) => p.states[n.id] === 'passed');
    if (allPassed) p.status = 'passed';
    const next = tpl.nodes.find((n) => ['running', 'review', 'admit', 'rejected'].includes(p.states[n.id]));
    if (next) p.current = next.id;

    if (started.length) {
      addAudit('system', '流转', `${p.id} · ${node.name} → ${started.map((n) => n.name).join('/')}`, '—');
      toast(`✅ ${node.name} 已通过，流转 → ${started.map((n) => n.name).join('、')} 开始人机协同`, 'ok', 3200);
    } else if (allPassed) {
      toast(`🎉 全流程已完成：${p.name}`, 'ok', 3600);
    } else {
      const waiting = tpl.nodes.filter((n) => (p.states[n.id] || 'pending') === 'pending' && n.deps.some((d) => p.states[d] !== 'passed'));
      const names = Array.from(new Set(waiting.flatMap((w) => w.deps.filter((d) => p.states[d] !== 'passed')))).map(templateNodeName);
      toast(names.length ? `✅ ${node.name} 已通过，等待并行节点完成：${names.join('、')}` : `✅ ${node.name} 已通过`, 'info', 3200);
    }
    state.procSelNode = null;
    state.procArtSel = null;
    render();
  }

  function currentVer(nodeId) {
    const d = NODE_DOC[nodeId];
    return d ? d.version : '—';
  }

  function openRejectModal(pid, nodeId, stage, person) {
    const p = processById(pid);
    const tpl = procTpl(p);
    const node = tpl.nodes.find((n) => n.id === nodeId);
    const earlier = tpl.nodes.filter((n) => tpl.nodes.indexOf(n) < tpl.nodes.indexOf(node));
    const options = [{ id: nodeId, t: `本节点「${node.name}」修改`, s: `${actor(agentForNode(nodeId)).name} 按批注修改后重新提交准出` }]
      .concat(earlier.slice().reverse().map((c) => ({ id: c.id, t: `打回到「${c.name}」`, s: `上游产物「${c.output}」需要补充，本节点等它重新通过` })));
    openModal({
      title: `${stage === 'out' ? '准出' : '准入'}驳回 · ${node.name}`,
      body: `
        <div class="callout callout-warn mb16"><span class="ico">↩️</span><div><b>${esc(actor(person).name)}</b> 驳回。任一评审人驳回即退回，本阶段的同意记录清空。</div></div>
        <div class="field">
          <label>退回到</label>
          ${options.map((o, k) => `
            <label class="radio-card" data-target-opt="${o.id}">
              <input type="radio" name="reject-target" value="${o.id}" ${k === 0 ? 'checked' : ''} style="margin-top:3px">
              <div><div class="rc-t">${esc(o.t)}</div><div class="rc-s">${esc(o.s)}</div></div>
            </label>`).join('')}
        </div>
        <div class="field">
          <label>驳回批注</label>
          <textarea data-reject-comment placeholder="说明驳回原因与需补充的内容…">${esc('产物缺少必要内容，请补充后重新提交。')}</textarea>
        </div>`,
      footer: `
        <button class="btn" data-close-modal>取消</button>
        <button class="btn btn-danger" data-act="confirm-reject" data-p="${pid}" data-node="${nodeId}" data-stage="${stage}" data-person="${person}">确认驳回</button>`,
      onMount(root) {
        $$('[data-target-opt]', root).forEach((card) => {
          card.addEventListener('click', () => {
            $$('[data-target-opt]', root).forEach((c) => c.classList.remove('on'));
            card.classList.add('on');
          });
          const inp = $('input', card);
          if (inp && inp.checked) card.classList.add('on');
        });
      },
    });
  }

  function confirmReject(pid, nodeId, stage, person) {
    const p = processById(pid);
    const node = procTpl(p).nodes.find((n) => n.id === nodeId);
    const target = ($('input[name="reject-target"]:checked') || {}).value;
    const comment = ($('[data-reject-comment]') || {}).value || '';
    const name = stage === 'out' ? '准出' : '准入';
    p.votes = p.votes || {};
    p.votes[nodeId] = { out: {}, in: {} };
    if (target === nodeId) {
      p.states[nodeId] = 'running';
      pushChat(pid, nodeId, { from: 'human', who: person, text: `【${name}驳回】${comment}`, t: hhmm() });
      pushChat(pid, nodeId, { from: 'ai', who: agentForNode(nodeId), text: '收到驳回批注，正在修改，完成后重新提交准出评审。', t: hhmm() });
      toast(`↩ ${actor(person).name} ${name}驳回，${node.name} 回到人机协同修改`, 'warn', 3200);
    } else {
      p.states[nodeId] = 'rejected';
      p.states[target] = 'running';
      p.current = target;
      p.returns.push({ from: nodeId, to: target, comment });
      pushChat(pid, nodeId, { from: 'sys', who: '', text: `${actor(person).name} ${name}驳回，打回至「${templateNodeName(target)}」：${comment}`, t: hhmm() });
      pushChat(pid, target, { from: 'human', who: person, text: `【来自「${node.name}」的打回】${comment}`, t: hhmm() });
      toast(`↩ 已打回至「${templateNodeName(target)}」，补充后重新走准出 / 准入`, 'warn', 3200);
    }
    addAudit(person, `${name}驳回`, `${pid} · ${node.name}`, `${currentVer(nodeId)} → ${templateNodeName(target)}`);
    closeModal();
    state.procSelNode = target;
    render();
  }

  function pushChat(pid, nodeId, msg) {
    CHAT[pid] = CHAT[pid] || {};
    CHAT[pid][nodeId] = CHAT[pid][nodeId] || [];
    CHAT[pid][nodeId].push(msg);
    // 同步 NODE_DOC 版本微调（仅演示）
  }

  function addAudit(actorId, action, object, version) {
    AUDIT.push({ time: nowStr(), actor: actorId, action, object, version });
  }

  function sendChat(pid, nodeId, text) {
    if (!text || !text.trim()) return;
    pushChat(pid, nodeId, { from: 'human', who: 'zhang', text: text.trim(), t: hhmm() });
    // Agent 自动回应
    const replies = [
      '收到，我会据此更新产出物并同步到评审记录。',
      '明白，我正在按组织规范补充相关章节。',
      '已记录该决策，稍后生成新版本供评审。',
    ];
    const reply = replies[Math.floor(Math.random() * replies.length)];
    setTimeout(() => {
      pushChat(pid, nodeId, { from: 'ai', who: agentForNode(nodeId), text: reply, t: hhmm() });
      const cb = $('#chat-body');
      if (cb && state.route.indexOf(`process/${pid}`) > -1) {
        cb.innerHTML = renderChat(pid, nodeId);
        cb.scrollTop = cb.scrollHeight;
      }
    }, 480);
    addAudit('zhang', '下发指令', `${pid} · ${templateNodeName(nodeId)}（对话）`, '—');
    const cb = $('#chat-body');
    if (cb) { cb.innerHTML = renderChat(pid, nodeId); cb.scrollTop = cb.scrollHeight; }
  }

  function openStartProcessModal(projectId) {
    const pj = projectById(projectId);
    const tpl = templateById(pj.templateId);
    const ap = assignProgress(pj);
    openModal({
      title: `发起流程 · ${pj.name}`,
      wide: true,
      body: `
        <div class="field">
          <label>一句话需求</label>
          <textarea data-sp-text placeholder="例如：支持用户用手机号+验证码登录">支持用户用手机号+验证码登录</textarea>
          <div class="hint">首个节点「${esc(tpl.nodes[0].name)}」的执行角色（${esc(roleHolderName(tpl.nodes[0].execRole, { projectId }))}）会基于这句话开始人机协同。</div>
        </div>
        <div class="field">
          <label>流程模板</label>
          <div class="tag">${tpl.icon} ${esc(tpl.name)} ${tpl.version}（项目绑定）</div>
        </div>
        ${ap.missing.length ? `<div class="callout callout-warn"><span class="ico">⚠️</span><div>还有 ${ap.missing.length} 个角色未分配（${ap.missing.map(esc).join('、')}），流转到相应节点时将无人协同或无人审批。</div></div>` : ''}`,
      footer: `
        <button class="btn" data-close-modal>取消</button>
        <button class="btn btn-primary" data-act="confirm-start" data-project="${projectId}">提交并进入流程</button>`,
    });
  }

  function confirmStartProcess(projectId) {
    const text = (($('[data-sp-text]') || {}).value || '').trim();
    if (!text) { toast('请填写一句话需求', 'warn'); return; }
    const pj = projectById(projectId);
    const tpl = templateById(pj.templateId);
    const id = uid('p');
    const states = {};
    tpl.nodes.forEach((n) => { states[n.id] = n.deps.length ? 'pending' : 'running'; });
    const p = {
      id, name: text, team: pj.teamId, projectId, templateId: tpl.id, status: 'running', created: nowStr(), creator: 'zhang',
      current: tpl.nodes[0].id, states, returns: [], votes: {},
      tpl: JSON.parse(JSON.stringify(tpl)),
    };
    PROCESSES.push(p);
    CHAT[id] = {};
    addAudit('zhang', '发起流程', `${pj.name} · ${text}`, `${tpl.name} ${tpl.version}`);
    toast(`流程已创建：${text}，首个节点「${tpl.nodes[0].name}」已开始人机协同`, 'ok', 3200);
    closeModal();
    navigate(`#/process/${id}`);
  }

  // 新建项目：选团队里的模板；角色在项目页分配
  function openNewProjectModal(teamId) {
    openModal({
      title: `新建项目 · ${teamName(teamId)}`,
      body: `
        <div class="field"><label>项目名称</label><input type="text" data-np-name value="会员积分体系"></div>
        <div class="field"><label>项目说明</label><input type="text" data-np-desc value="积分获取、兑换与过期规则"></div>
        <div class="field"><label>绑定流程模板</label>
          <select data-np-tpl>${TEMPLATES.map((t) => `<option value="${t.id}">${t.icon} ${t.name} ${t.version}</option>`).join('')}</select>
          <div class="hint">创建后在项目页为模板涉及的角色分配团队里的人和 AI Agent。</div>
        </div>`,
      footer: `<button class="btn" data-close-modal>取消</button>
               <button class="btn btn-primary" data-act="confirm-new-project" data-team-id="${teamId}">创建项目</button>`,
    });
  }
  function confirmNewProject(teamId) {
    const name = (($('[data-np-name]') || {}).value || '').trim();
    if (!name) { toast('请填写项目名称', 'warn'); return; }
    const pj = {
      id: uid('pj'), teamId, name, desc: (($('[data-np-desc]') || {}).value || '').trim(),
      templateId: ($('[data-np-tpl]') || {}).value, created: nowStr().slice(0, 10), assign: {},
    };
    PROJECTS.push(pj);
    const n = assignProgress(pj).all.length;
    addAudit('zhang', '新建项目', `${teamName(teamId)} · ${name}`, templateById(pj.templateId).version);
    closeModal();
    toast(`项目已创建，请为模板涉及的 ${n} 个角色分配人和 Agent`, 'ok', 3200);
    navigate(`#/projects/${pj.id}`);
  }

  // 角色分配：从团队里勾选人和 AI Agent
  function openAssignModal(projectId, role) {
    const pj = projectById(projectId);
    const t = teamById(pj.teamId);
    const cur = pj.assign[role] || [];
    const opt = (id) => `
      <label class="pick ${cur.includes(id) ? 'on' : ''}">
        <input type="checkbox" value="${id}" ${cur.includes(id) ? 'checked' : ''} data-pick>
        ${avatarEl(id, 'sm')}<span><b>${esc(actor(id).name)}</b><span class="small muted"> · ${esc(actor(id).title || actor(id).desc || '')}</span></span>
      </label>`;
    openModal({
      title: `分配角色 · ${role}`,
      body: `
        <div class="small muted mb12">项目「${esc(pj.name)}」· 可以同时选择人和 AI Agent。评审角色有多名成员时，按节点的评审规则（任一人 / 所有人同意）审批。</div>
        <div class="field"><label>人</label><div class="pick-grid">${t.members.map(opt).join('')}</div></div>
        <div class="field"><label>AI Agent</label><div class="pick-grid">${t.agents.map(opt).join('')}</div></div>`,
      footer: `<button class="btn" data-close-modal>取消</button>
               <button class="btn btn-primary" data-act="confirm-assign" data-project="${projectId}" data-role="${esc(role)}">保存</button>`,
      onMount(root) {
        $$('[data-pick]', root).forEach((cb) => cb.addEventListener('change', () => cb.closest('.pick').classList.toggle('on', cb.checked)));
      },
    });
  }
  function confirmAssign(projectId, role) {
    const pj = projectById(projectId);
    const ids = $$('[data-pick]').filter((x) => x.checked).map((x) => x.value);
    pj.assign[role] = ids;
    addAudit('zhang', '分配角色', `${pj.name} · ${role}`, ids.map((id) => actor(id).name).join('、') || '（清空）');
    closeModal();
    toast(ids.length ? `「${role}」已分配：${ids.map((id) => actor(id).name).join('、')}` : `已清空「${role}」`, 'ok');
    render();
  }

  /* ---------------------------------------------------------
     13.5 流程模板编辑
     --------------------------------------------------------- */
  function newTplNode() {
    return { id: uid('n'), name: '新节点', mode: 'collab', deps: [], artifact: null, output: '未命名产物', execRole: '新角色', outRev: '产品经理', outRule: 'any', inRev: '产品经理', inRule: 'any' };
  }
  // 改草稿后统一收尾：重算 deps、选中指定节点、重绘
  function afterEdit(t, selId) {
    syncDeps(t);
    state.insMenu = null;
    if (selId) state.tplSelNode = selId;
    render();
  }
  function tplEditAction(a, ds) {
    const tplId = state.route.split('/')[2];
    if (a === 'tpl-edit') {
      state.tplEdit = { id: tplId, draft: JSON.parse(JSON.stringify(templateById(tplId))) };
      render();
      return;
    }
    if (!editingTpl(tplId)) return;
    const t = state.tplEdit.draft;
    if (a === 'tpl-discard') { state.tplEdit = null; state.insMenu = null; toast('已放弃本次修改', 'info'); render(); return; }
    if (a === 'tpl-publish') {
      const base = templateById(tplId);
      const ver = nextVersion(base.version);
      Object.assign(base, { flow: t.flow, nodes: t.nodes, version: ver });
      state.tplEdit = null;
      addAudit('zhang', '发布模板', base.name, ver);
      toast(`已发布 ${base.name} ${ver}：新建流程使用新版本，进行中的流程仍按原版本执行`, 'ok', 3600);
      render();
      return;
    }
    if (a === 'ins-menu') { const at = Number(ds.at); state.insMenu = state.insMenu === at ? null : at; render(); return; }
    if (a === 'ins-node') {
      const n = newTplNode(); t.nodes.push(n);
      t.flow.splice(Number(ds.at), 0, { type: 'node', id: n.id });
      afterEdit(t, n.id); toast('已插入节点，请在下方配置名称、产物、文件模板与评审角色', 'ok');
      return;
    }
    if (a === 'ins-par') {
      const n1 = newTplNode(), n2 = newTplNode(); t.nodes.push(n1, n2);
      t.flow.splice(Number(ds.at), 0, { type: 'parallel', tracks: [{ nodes: [n1.id] }, { nodes: [n2.id] }] });
      afterEdit(t, n1.id); toast('已插入并行轨道（2 条），轨道内可继续新增节点', 'ok');
      return;
    }
    const step = t.flow[Number(ds.step)];
    if (a === 'add-track') {
      const n = newTplNode(); t.nodes.push(n);
      step.tracks.push({ nodes: [n.id] });
      afterEdit(t, n.id); toast(`已新增轨道 ${'ABCDEFGH'[step.tracks.length - 1]}`, 'ok');
      return;
    }
    if (a === 'track-add-node') {
      const n = newTplNode(); t.nodes.push(n);
      step.tracks[Number(ds.track)].nodes.push(n.id);
      afterEdit(t, n.id);
      return;
    }
    if (a === 'del-track') {
      step.tracks[Number(ds.track)].nodes.forEach((id) => { t.nodes = t.nodes.filter((x) => x.id !== id); });
      step.tracks.splice(Number(ds.track), 1);
      normalizeFlow(t);
      afterEdit(t, t.nodes[0] && t.nodes[0].id);
      return;
    }
    if (a === 'del-node') {
      if (t.nodes.length <= 1) { toast('模板至少保留一个节点', 'warn'); return; }
      const id = ds.node;
      t.nodes = t.nodes.filter((x) => x.id !== id);
      t.flow = t.flow.filter((st) => !(st.type === 'node' && st.id === id));
      t.flow.forEach((st) => { if (st.type === 'parallel') st.tracks.forEach((tr) => { tr.nodes = tr.nodes.filter((x) => x !== id); }); });
      normalizeFlow(t);
      afterEdit(t, state.tplSelNode === id ? t.nodes[0].id : state.tplSelNode);
    }
  }
  // 空轨道删掉；只剩一条轨道的并行组退化为顺序节点；空并行组删掉
  function normalizeFlow(t) {
    const out = [];
    t.flow.forEach((st) => {
      if (st.type === 'node') { out.push(st); return; }
      st.tracks = st.tracks.filter((tr) => tr.nodes.length);
      if (st.tracks.length >= 2) out.push(st);
      else if (st.tracks.length === 1) st.tracks[0].nodes.forEach((id) => out.push({ type: 'node', id }));
    });
    t.flow = out;
  }
  function tplConfigChange(el) {
    const tplId = state.route.split('/')[2];
    if (!editingTpl(tplId)) return;
    const t = state.tplEdit.draft;
    const n = t.nodes.find((x) => x.id === el.dataset.node);
    if (!n) return;
    const f = el.dataset.cfg;
    if (f === 'name') n.name = el.value.trim() || n.name;
    if (f === 'outRev' || f === 'inRev' || f === 'outRule' || f === 'inRule' || f === 'execRole') n[f] = el.value;
    if (f === 'execRoleNew' && el.value.trim()) n.execRole = el.value.trim();
    // 产物名称归节点自己；选文件模板不改产物名称
    if (f === 'artifact') n.artifact = artifactDef(el.value) ? el.value : null;
    if (f === 'output') n.output = el.value.trim() || n.output;
    render();
  }

  /* ---------------------------------------------------------
     14. 全局事件委托
     --------------------------------------------------------- */
  function bindGlobal() {
    // 团队切换下拉
    document.addEventListener('click', (e) => {
      const toggle = e.target.closest('[data-team-toggle]');
      if (toggle) { const m = $('[data-team-menu]'); if (m) m.hidden = !m.hidden; return; }
      const tm = e.target.closest('[data-team]');
      if (tm) {
        state.team = tm.dataset.team;
        const menu = $('[data-team-menu]'); if (menu) menu.hidden = true;
        toast(`已切换到「${teamName(state.team)}」`, 'info');
        navigate(`#/teams/${state.team}`);
        return;
      }
      // 流程图：点节点看配置；点别处收起插入菜单
      const fnode = e.target.closest('[data-tpl-node]');
      if (fnode && !e.target.closest('[data-act]')) { state.tplSelNode = fnode.dataset.tplNode; state.insMenu = null; render(); return; }
      if (state.insMenu != null && !e.target.closest('.f-ins')) { state.insMenu = null; render(); }

      // 点击空白关闭团队菜单
      if (!e.target.closest('.team-switcher')) { const m = $('[data-team-menu]'); if (m) m.hidden = true; }

      // 导航（data-nav）
      const nav = e.target.closest('[data-nav]');
      if (nav) { e.preventDefault(); navigate(nav.dataset.nav); return; }

      // 动作
      const act = e.target.closest('[data-act]');
      if (act) {
        const a = act.dataset.act;
        if (a === 'noop') { toast('原型演示：该功能暂未开放', 'info'); return; }
        if (a === 'start-process') { openStartProcessModal(act.dataset.project); return; }
        if (a === 'confirm-start') { confirmStartProcess(act.dataset.project); return; }
        if (a === 'new-project') { openNewProjectModal(act.dataset.teamId); return; }
        if (a === 'confirm-new-project') { confirmNewProject(act.dataset.teamId); return; }
        if (a === 'proj-filter') { state.projFilter = act.dataset.f; render(); return; }
        if (a === 'ps-jump') { e.preventDefault(); const el = $('#' + act.dataset.to); if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' }); return; }
        if (a === 'save-project') {
          const pj = projectById(act.dataset.project);
          pj.name = ($('[data-ps-name]').value || '').trim() || pj.name;
          pj.desc = ($('[data-ps-desc]').value || '').trim();
          toast('项目信息已保存', 'ok'); render(); return;
        }
        if (a === 'assign-role') { openAssignModal(act.dataset.project, act.dataset.role); return; }
        if (a === 'confirm-assign') { confirmAssign(act.dataset.project, act.dataset.role); return; }
        if (a === 'confirm-reject') { confirmReject(act.dataset.p, act.dataset.node, act.dataset.stage, act.dataset.person); return; }
        if (a === 'vote') { vote(act.dataset.p, act.dataset.node, act.dataset.stage, act.dataset.person, act.dataset.v); return; }
        if (a === 'art-sel') { state.procArtSel = act.dataset.node; render(); return; }
        if (a === 'node-action') { nodeAction(act.dataset.kind, act.dataset.p, act.dataset.node); return; }
        if (a === 'switch-node') {
          state.procSelNode = act.dataset.node;
          state.procArtSel = null;
          render();
          return;
        }
        if (['tpl-edit', 'tpl-discard', 'tpl-publish', 'ins-menu', 'ins-node', 'ins-par', 'add-track', 'track-add-node', 'del-track', 'del-node'].includes(a)) {
          tplEditAction(a, act.dataset); return;
        }
        if (a === 'ft-usage') { state.ftUsage = act.dataset.u; render(); return; }
        if (a === 'ft-ver') { state.ftVer = act.dataset.ver; render(); return; }
        if (a === 'ft-view') { state.ftView = act.dataset.v; render(); return; }
        if (a === 'ft-upload') { openFtUploadModal(act.dataset.id); return; }
        if (a === 'ft-confirm') { confirmFtUpload(act.dataset.id); return; }
        if (a === 'ft-download') { downloadFt(act.dataset.id, act.dataset.ver); return; }
      }


    });

    // 审计筛选
    document.addEventListener('change', (e) => {
      if (e.target.id === 'tpl-version') { toast(`已切换查看：${e.target.value}`, 'info'); }
    });

    document.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && e.target.matches('textarea[data-chat-input]')) {
        e.preventDefault();
        e.target.form.requestSubmit();
      }
    });

    // 对话发送
    document.addEventListener('submit', (e) => {
      if (e.target.id === 'chat-form') {
        e.preventDefault();
        const input = $('[data-chat-input]', e.target);
        const text = input ? input.value : '';
        input.value = '';
        sendChat(e.target.dataset.process, e.target.dataset.node, text);
      }
    });

    document.addEventListener('input', (e) => {
      const inp = e.target.closest('[data-proj-search]');
      if (!inp) return;
      state.projQuery = inp.value;
      const box = $('#proj-list');
      if (box) box.innerHTML = projectListHtml(projectById(box.dataset.project));
    });
    // 项目设置：更换绑定的流程模板
    document.addEventListener('change', (e) => {
      const sel = e.target.closest('[data-ps-tpl]');
      if (!sel) return;
      const pj = projectById(sel.dataset.project);
      pj.templateId = sel.value;
      const n = assignProgress(pj).missing.length;
      toast(`已绑定「${templateById(pj.templateId).name}」${n ? `，还有 ${n} 个角色待分配` : ''}；进行中的流程不受影响`, 'ok', 3200);
      render();
    });

    // 流程模板节点配置（编辑态）：名称在失焦 / 回车时生效，下拉选完即生效
    document.addEventListener('change', (e) => {
      const el = e.target.closest('[data-cfg]');
      if (el) tplConfigChange(el);
    });

    // 文件模板：选择文件后立即校验并预览
    document.addEventListener('change', (e) => {
      const inp = e.target.closest('[data-ft-file]');
      const btn = $('[data-act="ft-confirm"]');
      if (inp && inp.files && inp.files[0] && btn) checkFtFile(btn.dataset.id, inp.files[0]);
    });

    // ESC 关闭模态
    document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });

    window.addEventListener('hashchange', onRoute);
  }

  /* ---------------------------------------------------------
     15. 启动
     --------------------------------------------------------- */
  function init() {
    normalizeTemplates();
    bindGlobal();
    state.route = location.hash || '#/teams';
    render();
    // 首屏欢迎提示
    setTimeout(() => toast('欢迎使用 AI TeamFlow · 从「发起流程」开始体验', 'info', 3600), 400);
  }

  document.addEventListener('DOMContentLoaded', init);
})();
