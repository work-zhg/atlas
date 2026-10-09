/* =========================================================
   用户中心原型 —— 用户、组织与权限的主数据；接入应用通过开放接口获取（统一登录暂不提供，后续用 Keycloak）
   纯静态，内存态 Mock，刷新即重置。
   ========================================================= */
(function () {
  'use strict';

  /* ---------------------------------------------------------
     0. 工具
     --------------------------------------------------------- */
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const esc = (v) => String(v == null ? '' : v).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let _uid = 100;
  const uid = (p) => `${p}${++_uid}`;
  const pad = (n) => String(n).padStart(2, '0');
  const nowStr = () => { const d = new Date(); return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
  const COLORS = ['#6366f1', '#0ea5e9', '#16a34a', '#f59e0b', '#ec4899', '#8b5cf6', '#14b8a6', '#ef4444'];
  const colorOf = (s) => COLORS[[...s].reduce((a, c) => a + c.charCodeAt(0), 0) % COLORS.length];

  function toast(msg, type = 'ok', ms = 2600) {
    const el = document.createElement('div');
    el.className = `toast ${type}`;
    el.textContent = msg;
    $('#toast-root').appendChild(el);
    setTimeout(() => el.remove(), ms);
  }
  function openModal({ title, body, footer, wide, onMount }) {
    $('#modal-root').innerHTML = `
      <div class="modal-mask" data-mask>
        <div class="modal ${wide ? 'wide' : ''}" role="dialog" aria-modal="true">
          <div class="modal-h"><h3>${esc(title)}</h3><button class="x" data-close-modal aria-label="关闭">×</button></div>
          <div class="modal-b">${body}</div>
          ${footer ? `<div class="modal-f">${footer}</div>` : ''}
        </div>
      </div>`;
    if (onMount) onMount($('#modal-root'));
  }
  const closeModal = () => { $('#modal-root').innerHTML = ''; };
  function download(name, text, type = 'text/csv;charset=utf-8') {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob(['\uFEFF' + text], { type }));
    a.download = name;
    a.click();
    URL.revokeObjectURL(a.href);
  }

  /* ---------------------------------------------------------
     1. Mock 数据
     --------------------------------------------------------- */
  // 1.1 组织（部门树）
  const DEPTS = [
    { id: 'root', name: '星海科技', parent: null, leader: 'ceo' },
    { id: 'rd', name: '研发中心', parent: 'root', leader: 'li' },
    { id: 'trade', name: '交易中台', parent: 'rd', leader: 'li' },
    { id: 'infra', name: '技术中心', parent: 'rd', leader: 'chen' },
    { id: 'qa', name: '质量部', parent: 'rd', leader: 'wang' },
    { id: 'product', name: '产品部', parent: 'root', leader: 'zhang' },
    { id: 'content', name: '内容中心', parent: 'root', leader: 'zhao' },
    { id: 'hr', name: '人力资源部', parent: 'root', leader: 'sun' },
  ];

  // 1.2 用户
  //   status：active 正常 / pending 未激活（已创建、未首次登录改密）/ disabled 已停用 / locked 已锁定
  //   岗位与角色见 1.2.x：数据里先写岗位名称，加载后转成岗位 id（pos）与直接绑定的角色（roles）
  const U = (id, name, account, dept, title, extra = {}) => Object.assign({
    id, name, account, dept, title, email: `${account}@xinghai.com`, phone: '',
    status: 'active', adminRole: null, created: '2026-03-01 10:00', lastLogin: '2026-10-08 09:12',
  }, extra);
  const userByIdRaw = (id) => USERS.find((u) => u.id === id);
  const USERS = [
    U('ceo', '林远', 'lin.yuan', 'root', 'CEO', { phone: '13800000001' }),
    U('sun', '孙丽', 'sun.li', 'hr', 'HR 负责人', { adminRole: 'super', phone: '13800000002', lastLogin: '2026-10-09 08:55' }),
    U('zhang', '张明', 'zhang.ming', 'product', '高级产品经理', { phone: '13800000003' }),
    U('li', '李工', 'li.gong', 'trade', '技术负责人', { adminRole: 'user_admin' }),
    U('liu', '刘洋', 'liu.yang', 'trade', '架构师'),
    U('wang', '王测', 'wang.ce', 'qa', '测试负责人'),
    U('chen', '陈运', 'chen.yun', 'infra', 'SRE 负责人'),
    U('zhao', '赵敏', 'zhao.min', 'content', '内容负责人'),
    U('he', '何超', 'he.chao', 'trade', '后端工程师', { lastLogin: '2026-10-07 18:20' }),
    U('xu', '许晴', 'xu.qing', 'trade', '前端工程师'),
    U('gao', '高峰', 'gao.feng', 'infra', '运维工程师', { status: 'locked', lastLogin: '2026-10-08 22:41' }),
    U('ma', '马骁', 'ma.xiao', 'qa', '测试工程师'),
    U('zhou', '周宁', 'zhou.ning', 'product', '产品经理', { status: 'pending', lastLogin: null, created: '2026-10-08 15:00' }),
    U('wu', '吴迪', 'wu.di', 'content', '内容编辑'),
    U('qian', '钱多', 'qian.duo', 'trade', '后端工程师', { status: 'disabled', lastLogin: '2026-08-30 17:02' }),
    U('fang', '方圆', 'fang.yuan', 'hr', 'HRBP'),
  ];

  // 1.2.1 岗位：统一维护；可关联默认角色（担任该岗位的人自动获得）
  const POSITIONS = [
    { id: 'p-ceo', name: 'CEO', code: 'CEO', roles: ['uc-auditor'] },
    { id: 'p-hrlead', name: 'HR 负责人', code: 'HR_LEAD', roles: ['uc-useradmin'] },
    { id: 'p-hrbp', name: 'HRBP', code: 'HRBP', roles: ['uc-useradmin'] },
    { id: 'p-pm-sr', name: '高级产品经理', code: 'PM_SR', roles: ['tf-member'] },
    { id: 'p-pm', name: '产品经理', code: 'PM', roles: ['tf-member'] },
    { id: 'p-techlead', name: '技术负责人', code: 'TECH_LEAD', roles: ['tf-template', 'tf-member'] },
    { id: 'p-arch', name: '架构师', code: 'ARCH', roles: ['tf-member'] },
    { id: 'p-be', name: '后端工程师', code: 'BE', roles: ['tf-member'] },
    { id: 'p-fe', name: '前端工程师', code: 'FE', roles: ['tf-member'] },
    { id: 'p-qalead', name: '测试负责人', code: 'QA_LEAD', roles: ['tf-member'] },
    { id: 'p-qa', name: '测试工程师', code: 'QA', roles: ['tf-member'] },
    { id: 'p-sre', name: 'SRE 负责人', code: 'SRE_LEAD', roles: ['tf-member', 'at-admin'] },
    { id: 'p-ops', name: '运维工程师', code: 'OPS', roles: ['tf-member'] },
    { id: 'p-contentlead', name: '内容负责人', code: 'CONTENT_LEAD', roles: ['tf-member'] },
    { id: 'p-editor', name: '内容编辑', code: 'EDITOR', roles: [] },
  ];

  // 1.2.2 操作目录：每个接入应用登记自己的操作（按模块分组）；前端用操作码控制菜单与按钮
  const OPS = [
    // 用户中心
    { app: 'uc', module: '组织架构', code: 'org:view', name: '查看组织架构' },
    { app: 'uc', module: '组织架构', code: 'org:manage', name: '管理部门（新建 / 编辑 / 删除 / 调整成员）' },
    { app: 'uc', module: '用户', code: 'user:view', name: '查看用户' },
    { app: 'uc', module: '用户', code: 'user:create', name: '新建用户' },
    { app: 'uc', module: '用户', code: 'user:edit', name: '编辑用户' },
    { app: 'uc', module: '用户', code: 'user:disable', name: '停用 / 启用 / 解锁' },
    { app: 'uc', module: '用户', code: 'user:reset_pwd', name: '重置密码' },
    { app: 'uc', module: '权限', code: 'role:view', name: '查看角色' },
    { app: 'uc', module: '权限', code: 'role:manage', name: '管理角色（新建 / 编辑操作 / 删除）' },
    { app: 'uc', module: '权限', code: 'position:view', name: '查看岗位' },
    { app: 'uc', module: '权限', code: 'position:manage', name: '管理岗位' },
    { app: 'uc', module: '权限', code: 'grant:view', name: '查看授权' },
    { app: 'uc', module: '权限', code: 'grant:manage', name: '授权 / 撤销（岗位、角色 → 用户、组织）' },
    { app: 'uc', module: '数据权限', code: 'data:view', name: '查看全部数据的权限（只读，不能修改）' },
    { app: 'uc', module: '系统', code: 'app:manage', name: '应用接入、操作目录与数据编码' },
    { app: 'uc', module: '系统', code: 'security:manage', name: '安全策略' },
    { app: 'uc', module: '系统', code: 'audit:view', name: '查看审计日志' },
    // AI TeamFlow
    { app: 'teamflow', module: '团队', code: 'team:create', name: '新建团队' },
    { app: 'teamflow', module: '流程模板', code: 'flow_template:edit', name: '编辑流程模板' },
    { app: 'teamflow', module: '流程模板', code: 'flow_template:publish', name: '发布流程模板' },
    { app: 'teamflow', module: '文件模板', code: 'file_template:upload', name: '上传文件模板' },
    { app: 'teamflow', module: '流程', code: 'process:view', name: '查看流程' },
    { app: 'teamflow', module: '流程', code: 'process:start', name: '发起流程' },
    // Atlas
    { app: 'atlas', module: '智能体', code: 'agent:view', name: '查看智能体' },
    { app: 'atlas', module: '智能体', code: 'agent:edit', name: '新建 / 编辑智能体' },
    { app: 'atlas', module: '智能体', code: 'agent:publish', name: '启用 / 停用智能体' },
    { app: 'atlas', module: '技能', code: 'skill:review', name: '审核技能' },
  ];
  const UC_APP = { id: 'uc', name: '用户中心', icon: '👥' };

  // 1.2.3 角色：属于某个应用，包含该应用的若干操作；内置角色不可删除、不可改操作
  const ucOps = (...pre) => OPS.filter((o) => o.app === 'uc' && pre.some((p) => o.code.startsWith(p))).map((o) => o.code);
  const ROLES = [
    { id: 'uc-super', app: 'uc', name: '超级管理员', code: 'UC_SUPER', builtin: true, desc: '用户中心全部操作', ops: OPS.filter((o) => o.app === 'uc').map((o) => o.code) },
    { id: 'uc-useradmin', app: 'uc', name: '用户管理员', code: 'UC_USER_ADMIN', desc: '维护组织、用户与岗位，可为用户和组织授权', ops: [...ucOps('org:', 'user:', 'position:', 'grant:'), 'role:view', 'audit:view'] },
    { id: 'uc-auditor', app: 'uc', name: '审计员', code: 'UC_AUDITOR', desc: '只读：组织、用户、授权与审计日志', ops: ['org:view', 'user:view', 'grant:view', 'data:view', 'audit:view'] },
    { id: 'tf-platform', app: 'teamflow', name: '平台管理员', code: 'TF_PLATFORM_ADMIN', desc: '建团队、维护与发布模板', ops: OPS.filter((o) => o.app === 'teamflow').map((o) => o.code) },
    { id: 'tf-template', app: 'teamflow', name: '模板维护者', code: 'TF_TEMPLATE_EDITOR', desc: '编辑流程模板、上传文件模板，不能发布', ops: ['flow_template:edit', 'file_template:upload', 'process:view'] },
    { id: 'tf-member', app: 'teamflow', name: '流程成员', code: 'TF_MEMBER', desc: '查看与发起流程', ops: ['process:view', 'process:start'] },
    { id: 'at-admin', app: 'atlas', name: 'Agent 管理员', code: 'AT_AGENT_ADMIN', desc: '管理智能体与技能审核', ops: OPS.filter((o) => o.app === 'atlas').map((o) => o.code) },
  ];

  // 1.2.3.1 菜单：每个应用一棵菜单树（目录 / 菜单）；角色绑定菜单，用户可见菜单 = 所有角色绑定的菜单 ∪ 公共菜单
  //   目录只在有可见的子菜单时显示；菜单控制「看得到哪些页面」，操作码控制「页面里能做什么」
  const MENUS = [];
  const M = (app, code, name, icon, path, parent = null, extra = {}) => MENUS.push(Object.assign({ id: `m-${app}-${code}`, app, code, name, icon, path, parent: parent && `m-${app}-${parent}`, type: path ? 'menu' : 'dir', sort: MENUS.filter((m) => m.app === app).length + 1, public: false }, extra));
  // 用户中心（内置，随版本发布）：侧边栏就是按这棵树和登录人的角色生成的
  M('uc', 'org', '组织架构', '🏢', '/org', null, { builtin: true });
  M('uc', 'users', '用户管理', '👤', '/users', null, { builtin: true });
  M('uc', 'grants', '权限管理', '🛂', '/grants', null, { builtin: true, public: true });
  M('uc', 'apps', '应用接入', '🔗', '/apps', null, { builtin: true });
  M('uc', 'security', '安全策略', '🛡️', '/security', null, { builtin: true });
  M('uc', 'audit', '审计日志', '📜', '/audit', null, { builtin: true });
  // AI TeamFlow
  M('teamflow', 'work', '工作', '🗂️', '');
  M('teamflow', 'process', '我的流程', '🔁', '/processes', 'work', { public: true });
  M('teamflow', 'team', '团队', '👥', '/teams', 'work');
  M('teamflow', 'tpl', '模板', '🧩', '');
  M('teamflow', 'flow_tpl', '流程模板', '🧭', '/flow-templates', 'tpl');
  M('teamflow', 'file_tpl', '文件模板', '📄', '/file-templates', 'tpl');
  M('teamflow', 'settings', '系统设置', '⚙️', '/settings');
  // Atlas
  M('atlas', 'chat', '对话', '💬', '/chat', null, { public: true });
  M('atlas', 'agents', '智能体', '🤖', '/agents');
  M('atlas', 'skills', '技能', '🧰', '/skills');
  M('atlas', 'admin', '系统管理', '⚙️', '/admin');
  const ROLE_MENUS = {
    'uc-super': ['org', 'users', 'apps', 'security', 'audit'],
    'uc-useradmin': ['org', 'users', 'apps', 'audit'],
    'uc-auditor': ['org', 'users', 'audit'],
    'tf-platform': ['team', 'flow_tpl', 'file_tpl', 'settings'],
    'tf-template': ['flow_tpl', 'file_tpl'],
    'tf-member': ['team'],
    'at-admin': ['agents', 'skills', 'admin'],
  };
  ROLES.forEach((r) => { r.menus = (ROLE_MENUS[r.id] || []).map((c) => `m-${r.app}-${c}`); });

  // 1.3 接入的应用（服务端用 App Key / Secret 调用开放接口）
  const APPS = [
    { id: 'teamflow', name: 'AI TeamFlow', icon: '🧩', desc: 'AI 协同工作流平台', clientId: 'tf_7c1e9a2b', secretTail: 'k9Q2', scope: { all: true, depts: [] }, status: 'active', created: '2026-09-01 10:00' },
    { id: 'atlas', name: 'Atlas', icon: '🤖', desc: 'AI Agent 平台', clientId: 'at_3f8d0c41', secretTail: 'Zx81', scope: { all: false, depts: ['rd'] }, status: 'active', created: '2026-09-05 14:30' },
    { id: 'wiki', name: '知识库', icon: '📚', desc: '内部文档与知识库', clientId: 'wk_a12b77e0', secretTail: 'Pq03', scope: { all: true, depts: [] }, status: 'disabled', created: '2026-06-12 09:00' },
  ];

  // 1.4 安全策略
  const POLICY = { minLen: 8, needUpper: true, needDigit: true, needSymbol: false, expireDays: 90, lockAfter: 5, lockMinutes: 15, sessionHours: 12 };

  // 1.5 日志
  const LOGIN_LOGS = [
    { time: '2026-10-09 08:55', user: 'sun', app: '用户中心', ip: '10.2.3.18', ok: true },
    { time: '2026-10-08 22:41', user: 'gao', app: 'AI TeamFlow', ip: '10.2.8.66', ok: false, reason: '密码错误，已锁定' },
    { time: '2026-10-08 09:12', user: 'zhang', app: 'AI TeamFlow', ip: '10.2.3.40', ok: true },
    { time: '2026-10-08 09:05', user: 'li', app: 'Atlas', ip: '10.2.5.12', ok: true },
    { time: '2026-10-07 18:20', user: 'he', app: 'AI TeamFlow', ip: '10.2.5.31', ok: true },
  ];
  const AUDIT = [
    { time: '2026-10-08 15:00', actor: 'sun', action: '新建用户', target: '周宁（zhou.ning）', detail: '产品部 · 产品经理' },
    { time: '2026-10-08 22:41', actor: 'system', action: '锁定账号', target: '高峰（gao.feng）', detail: '连续 5 次密码错误' },
    { time: '2026-09-30 11:20', actor: 'sun', action: '停用用户', target: '钱多（qian.duo）', detail: '离职' },
    { time: '2026-09-05 14:30', actor: 'sun', action: '接入应用', target: 'Atlas', detail: '可访问范围：研发中心' },
  ];

  // 1.2.4 授权：岗位和角色都可以授予用户或组织（部门）；授予部门时可选「包含下级部门」
  //   { id, kind: 'pos' | 'role', ref: 岗位/角色 id, subj: 'user' | 'dept', to: 用户/部门 id, sub: 是否含下级, by, at }
  //   用户的有效授权 = 授予本人 + 授予所在部门 + 授予上级部门且包含下级
  const GRANTS = [];
  const G = (kind, ref, subj, to, sub = false, at = '2026-09-01 10:00') => GRANTS.push({ id: uid('g'), kind, ref, subj, to, sub: subj === 'dept' && sub, by: 'sun', at });
  USERS.forEach((u) => {
    const pos = POSITIONS.find((x) => x.name === u.title);
    if (pos) G('pos', pos.id, 'user', u.id);
    ({ super: ['uc-super'], user_admin: ['uc-useradmin'] }[u.adminRole] || []).forEach((r) => G('role', r, 'user', u.id));
    delete u.title;
    delete u.adminRole;
  });
  G('role', 'tf-platform', 'user', 'zhang', false, '2026-09-02 09:30');
  G('role', 'tf-member', 'dept', 'rd', true, '2026-09-10 15:00');
  G('role', 'at-admin', 'dept', 'infra', false, '2026-09-12 11:00');
  G('pos', 'p-editor', 'dept', 'content', true, '2026-09-15 16:20');

  // 1.2.5 数据权限：按「数据编码 + 数据 Id」控制到每一条数据
  //   数据类型由应用登记（如 Atlas 的 Agent）；应用创建数据时调接口写入一条 Owner 授权
  //   权限级别：只读 < 读写 < Owner；可授给用户或组织（部门可含下级）；多来源取最高
  //   只有对该条数据有 Owner 权限的人，才能修改这条数据的授权
  const LEVELS = {
    read: { label: '只读', rank: 1, cls: 'gray', api: 'READ' },
    write: { label: '读写', rank: 2, cls: 'blue', api: 'WRITE' },
    owner: { label: 'Owner', rank: 3, cls: 'purple', api: 'OWNER' },
  };
  const DATA_TYPES = [
    { code: 'Agent', app: 'atlas', name: '智能体', desc: 'Atlas 中的 AI Agent' },
    { code: 'Skill', app: 'atlas', name: '技能', desc: 'Atlas 中的技能包' },
    { code: 'FlowTemplate', app: 'teamflow', name: '流程模板', desc: 'AI TeamFlow 的流程模板' },
  ];
  const DATA = [
    { code: 'Agent', id: 'agt_7f3a21', name: '代码评审助手', created: '2026-09-20 10:12' },
    { code: 'Agent', id: 'agt_21bc90', name: '需求分析师', created: '2026-09-18 14:30' },
    { code: 'Agent', id: 'agt_90de44', name: '测试用例生成器', created: '2026-09-22 09:05' },
    { code: 'Agent', id: 'agt_c05e17', name: '运维巡检', created: '2026-09-25 16:40' },
    { code: 'Skill', id: 'skl_3a9c02', name: 'Git 提交规范', created: '2026-09-12 11:00' },
    { code: 'FlowTemplate', id: 'ft_001', name: '标准研发流程', created: '2026-09-03 10:00' },
  ];
  const DACL = [];
  const DA = (code, dataId, level, subj, to, by, at, sub = false) => DACL.push({ id: uid('da'), code, dataId, level, subj, to, sub: subj === 'dept' && sub, by, at, via: by === 'api' ? 'api' : 'manual' });
  DA('Agent', 'agt_7f3a21', 'owner', 'user', 'zhang', 'api', '2026-09-20 10:12');
  DA('Agent', 'agt_7f3a21', 'read', 'dept', 'rd', 'zhang', '2026-09-20 10:30', true);
  DA('Agent', 'agt_7f3a21', 'write', 'user', 'chen', 'zhang', '2026-09-21 09:00');
  DA('Agent', 'agt_21bc90', 'owner', 'user', 'li', 'api', '2026-09-18 14:30');
  DA('Agent', 'agt_21bc90', 'write', 'user', 'zhang', 'li', '2026-09-18 15:00');
  DA('Agent', 'agt_21bc90', 'read', 'dept', 'product', 'li', '2026-09-19 10:00');
  DA('Agent', 'agt_90de44', 'owner', 'user', 'wang', 'api', '2026-09-22 09:05');
  DA('Agent', 'agt_90de44', 'write', 'dept', 'qa', 'wang', '2026-09-22 09:20');
  DA('Agent', 'agt_c05e17', 'owner', 'user', 'chen', 'api', '2026-09-25 16:40');
  DA('Agent', 'agt_c05e17', 'owner', 'dept', 'infra', 'chen', '2026-09-25 17:00');
  DA('Skill', 'skl_3a9c02', 'owner', 'user', 'chen', 'api', '2026-09-12 11:00');
  DA('Skill', 'skl_3a9c02', 'read', 'dept', 'root', 'chen', '2026-09-12 11:10', true);
  DA('FlowTemplate', 'ft_001', 'owner', 'user', 'sun', 'api', '2026-09-03 10:00');
  DA('FlowTemplate', 'ft_001', 'write', 'dept', 'rd', 'sun', '2026-09-03 10:20', true);

  const deptById = (id) => DEPTS.find((d) => d.id === id);
  const userById = (id) => USERS.find((u) => u.id === id);
  const appById = (id) => APPS.find((a) => a.id === id);
  const childrenOf = (id) => DEPTS.filter((d) => d.parent === id);
  const descendants = (id) => childrenOf(id).flatMap((c) => [c.id, ...descendants(c.id)]);
  const deptPath = (id) => { const out = []; let d = deptById(id); while (d) { out.unshift(d.name); d = deptById(d.parent); } return out; };
  const usersIn = (id, includeSub) => { const ids = includeSub ? [id, ...descendants(id)] : [id]; return USERS.filter((u) => ids.includes(u.dept)); };
  // 直属上级不存储，由组织计算：从所在部门向上找第一个「不是本人、未停用」的部门负责人
  //   部门换负责人后，相关人员的直属上级自动跟着变，不需要改用户数据
  function managerOf(u) {
    let d = deptById(u.dept);
    while (d) {
      const l = userById(d.leader);
      if (l && l.id !== u.id && l.status !== 'disabled') return l;
      d = deptById(d.parent);
    }
    return null;
  }
  const posById = (id) => POSITIONS.find((p) => p.id === id);
  const deptChain = (id) => { const out = []; let d = deptById(id); while (d) { out.push(d.id); d = deptById(d.parent); } return out; };
  // 用户身上生效的授权
  const grantsOf = (u) => GRANTS.filter((g) => (g.subj === 'user' ? g.to === u.id : g.to === u.dept || (g.sub && deptChain(u.dept).includes(g.to))));
  // 部门身上生效的授权：自己的 + 上级部门「含下级」的
  const deptGrants = (id) => GRANTS.filter((g) => g.subj === 'dept' && (g.to === id || (g.sub && deptChain(id).slice(1).includes(g.to))));
  const grantSrc = (g) => (g.subj === 'user' ? '直接授权' : `部门：${deptById(g.to).name}${g.sub ? '（含下级）' : ''}`);
  const posOf = (u) => [...new Set(grantsOf(u).filter((g) => g.kind === 'pos').map((g) => g.ref))].filter(posById);
  const posName = (u) => posOf(u).map((x) => posById(x).name).join('、');
  const roleById = (id) => ROLES.find((r) => r.id === id);
  const appName = (id) => (id === 'uc' ? UC_APP.name : (APPS.find((a) => a.id === id) || { name: id }).name);
  const opByCode = (code) => OPS.find((o) => o.code === code);
  // 用户的角色及来源：授予的角色 + 所授岗位的默认角色（去重，记录每个来源）
  function rolesOf(u) {
    const src = new Map();
    const add = (r, label) => { if (!roleById(r)) return; if (!src.has(r)) src.set(r, []); if (!src.get(r).includes(label)) src.get(r).push(label); };
    grantsOf(u).forEach((g) => {
      if (g.kind === 'role') add(g.ref, grantSrc(g));
      else if (posById(g.ref)) posById(g.ref).roles.forEach((r) => add(r, `岗位：${posById(g.ref).name}`));
    });
    return { all: [...src.keys()], src };
  }
  const opsOf = (u) => new Set(rolesOf(u).all.flatMap((r) => roleById(r).ops));
  // ★ 前端按操作码控制权限：没有该操作的菜单与按钮不渲染
  const can = (op) => { const u = userById(state.me); return !!u && opsOf(u).has(op); };
  const roleBadges = (u, app = 'uc') => rolesOf(u).all.map(roleById).filter((r) => r.app === app).map((r) => ` <span class="badge badge-purple">${esc(r.name)}</span>`).join('');
  const superCount = (exceptId) => USERS.filter((x) => x.id !== exceptId && x.status !== 'disabled' && rolesOf(x).all.includes('uc-super')).length;
  const usersWithRole = (roleId) => USERS.filter((u) => rolesOf(u).all.includes(roleId));
  const holdersOf = (g) => (g.subj === 'user' ? [userById(g.to)].filter(Boolean) : usersIn(g.to, g.sub));
  const refName = (g) => (g.kind === 'pos' ? posById(g.ref).name : roleById(g.ref).name);
  const subjName = (g) => (g.subj === 'user' ? userById(g.to).name : deptById(g.to).name);
  // 授权变更后至少保留一名可用的超级管理员，否则回滚
  function guardSuper(fn) {
    const snap = { g: GRANTS.map((g) => ({ ...g })), p: POSITIONS.map((x) => [x.id, [...x.roles]]) };
    fn();
    if (superCount()) return true;
    GRANTS.splice(0, GRANTS.length, ...snap.g);
    snap.p.forEach(([id, roles]) => { const x = posById(id); if (x) x.roles = roles; });
    toast('该操作会导致没有可用的超级管理员，已取消', 'warn', 3200);
    return false;
  }
  const actorName = (id) => (id === 'system' ? '系统' : (userById(id) || { name: id }).name);

  const STATUS = {
    active: { label: '正常', cls: 'green' },
    pending: { label: '未激活', cls: 'blue' },
    disabled: { label: '已停用', cls: 'gray' },
    locked: { label: '已锁定', cls: 'red' },
  };
  const statusBadge = (s) => `<span class="badge badge-${STATUS[s].cls}"><span class="bdot"></span>${STATUS[s].label}</span>`;
  const avatar = (u, size = '') => `<span class="avatar ${size}" style="background:${colorOf(u.name)}">${esc(u.name.slice(-1))}</span>`;
  const audit = (action, target, detail = '') => AUDIT.unshift({ time: nowStr(), actor: state.me || 'system', action, target, detail });

  /* ---------------------------------------------------------
     2. 状态
     --------------------------------------------------------- */
  const state = {
    me: null,               // 登录用户 id；null = 未登录
    route: '#/org',
    loginStep: 'login',     // login / change（首次登录改密）
    pendingUser: null,
    fails: {},              // 账号 → 连续失败次数
    orgSel: 'rd',
    orgOpen: new Set(['root', 'rd']),
    orgSub: true,           // 成员是否包含子部门（默认包含：上级部门往往没有直属成员）
    uq: '', udept: '', ustatus: 'all',
    utab: 'info',
    auditTab: 'op', auditQ: '',
    roleSel: null, roleApp: 'uc', roleTab: 'ops',
    appTab: 'conf', grantSec: 'role', menuPreview: null,
    dataTab: 'mine', dataCode: 'all',
    grantTab: 'subject', grantMode: 'dept', grantSubj: { t: 'dept', id: 'rd' }, grantKind: 'all', grantTo: 'all',
  };

  /* ---------------------------------------------------------
     3. 密码策略
     --------------------------------------------------------- */
  function checkPassword(pwd) {
    const errs = [];
    if (pwd.length < POLICY.minLen) errs.push(`至少 ${POLICY.minLen} 位`);
    if (POLICY.needUpper && !/[A-Z]/.test(pwd)) errs.push('含大写字母');
    if (POLICY.needDigit && !/\d/.test(pwd)) errs.push('含数字');
    if (POLICY.needSymbol && !/[^A-Za-z0-9]/.test(pwd)) errs.push('含特殊字符');
    return errs;
  }
  const policyText = () => [`至少 ${POLICY.minLen} 位`, POLICY.needUpper && '含大写字母', POLICY.needDigit && '含数字', POLICY.needSymbol && '含特殊字符'].filter(Boolean).join('、');
  function tempPassword() {
    const pick = (s) => s[Math.floor(Math.random() * s.length)];
    let p = pick('ABCDEFGHJKLMNPQRSTUVWXYZ') + pick('23456789') + pick('!@#$%');
    while (p.length < Math.max(10, POLICY.minLen)) p += pick('abcdefghijkmnpqrstuvwxyz23456789');
    return p.split('').sort(() => Math.random() - 0.5).join('');
  }
  // 原型里所有已激活账号的密码都是 Passw0rd!（演示用）
  const DEMO_PWD = 'Passw0rd!';
  const PWD = {};

  /* ---------------------------------------------------------
     4. 登录
     --------------------------------------------------------- */
  function viewLogin() {
    const change = state.loginStep === 'change';
    const u = change && userById(state.pendingUser);
    return `
      <div class="login">
        <div class="login-hero">
          <div class="row" style="gap:10px"><strong style="color:#fff;font-size:16px">星海科技 · 用户中心</strong></div>
          <div>
            <h2>一个账号，登录所有内部系统</h2>
            <p>用户、组织与权限在这里统一维护；AI TeamFlow、Atlas、知识库等系统通过开放接口获取，按部门控制可访问范围。</p>
          </div>
          <div class="apps">${APPS.filter((a) => a.status === 'active').map((a) => `<span>${a.icon} ${esc(a.name)}</span>`).join('')}</div>
        </div>
        <div class="login-box">
          <div class="login-card">
            ${change ? `
              <h3>首次登录，请设置新密码</h3>
              <div class="small muted mt4 mb16">${esc(u.name)}（${esc(u.account)}），密码要求：${policyText()}</div>
              <form id="change-form">
                <div class="field"><label>新密码</label><input type="password" data-new autocomplete="new-password"></div>
                <div class="field"><label>确认新密码</label><input type="password" data-confirm autocomplete="new-password"><div class="err" data-err></div></div>
                <button class="btn btn-primary" type="submit">设置并登录</button>
              </form>` : `
              <h3>登录</h3>
              <div class="small muted mt4 mb16">使用公司账号登录</div>
              <form id="login-form">
                <div class="field"><label>账号</label><input type="text" data-account value="sun.li" autocomplete="username"></div>
                <div class="field"><label>密码</label><input type="password" data-pwd value="${DEMO_PWD}" autocomplete="current-password"><div class="err" data-err></div></div>
                <button class="btn btn-primary" type="submit">登录</button>
              </form>
              <div class="login-demo">原型演示：已激活账号的密码均为 <b>${DEMO_PWD}</b>
                <ul style="margin:6px 0 0;padding-left:18px">
                  <li><b>sun.li</b> 超级管理员</li>
                  <li><b>li.gong</b> 用户管理员（无应用接入、安全策略）</li>
                  <li><b>zhang.ming</b> 普通用户（只有个人中心）</li>
                  <li><b>zhou.ning</b> 未激活，首次登录须改密</li>
                  <li><b>gao.feng</b> 已锁定 · <b>qian.duo</b> 已停用</li>
                </ul></div>`}
          </div>
        </div>
      </div>`;
  }
  function doLogin() {
    const account = $('[data-account]').value.trim();
    const pwd = $('[data-pwd]').value;
    const errEl = $('[data-err]');
    const u = USERS.find((x) => x.account === account);
    const fail = (msg) => { errEl.textContent = msg; };
    if (!u) return fail('账号或密码错误');
    if (u.status === 'disabled') return fail('账号已停用，请联系管理员');
    if (u.status === 'locked') return fail(`账号已锁定，请 ${POLICY.lockMinutes} 分钟后重试或联系管理员解锁`);
    const right = PWD[u.id] || DEMO_PWD;
    if (pwd !== right) {
      state.fails[u.id] = (state.fails[u.id] || 0) + 1;
      LOGIN_LOGS.unshift({ time: nowStr(), user: u.id, app: '用户中心', ip: '10.2.3.18', ok: false, reason: '密码错误' });
      const left = POLICY.lockAfter - state.fails[u.id];
      if (left <= 0) {
        u.status = 'locked';
        AUDIT.unshift({ time: nowStr(), actor: 'system', action: '锁定账号', target: `${u.name}（${u.account}）`, detail: `连续 ${POLICY.lockAfter} 次密码错误` });
        return fail(`连续 ${POLICY.lockAfter} 次密码错误，账号已锁定`);
      }
      return fail(`账号或密码错误（再错 ${left} 次将锁定）`);
    }
    state.fails[u.id] = 0;
    if (u.status === 'pending') { state.loginStep = 'change'; state.pendingUser = u.id; render(); return; }
    finishLogin(u);
  }
  function doChangeFirst() {
    const u = userById(state.pendingUser);
    const np = $('[data-new]').value;
    const cp = $('[data-confirm]').value;
    const errs = checkPassword(np);
    if (errs.length) { $('[data-err]').textContent = `密码不符合要求：${errs.join('、')}`; return; }
    if (np !== cp) { $('[data-err]').textContent = '两次输入不一致'; return; }
    PWD[u.id] = np;
    u.status = 'active';
    finishLogin(u);
    toast('密码已设置，账号已激活');
  }
  function finishLogin(u) {
    u.lastLogin = nowStr();
    LOGIN_LOGS.unshift({ time: u.lastLogin, user: u.id, app: '用户中心', ip: '10.2.3.18', ok: true });
    state.me = u.id;
    state.loginStep = 'login';
    location.hash = landing();
    render();
  }
  // 登录后落到第一个有权限的页面；一个管理操作都没有的人只有个人中心
  // 菜单可见性：权限管理对所有人可见（至少有「数据授权」），应用接入有 app:manage 或 role:view 即可进入
  const menuById = (id) => MENUS.find((m) => m.id === id);
  const menusOfApp = (app) => MENUS.filter((m) => m.app === app).sort((a, b) => a.sort - b.sort);
  const menuKids = (app, parent) => menusOfApp(app).filter((m) => (m.parent || null) === parent);
  const menuDesc = (id) => MENUS.filter((m) => m.parent === id).flatMap((m) => [m.id, ...menuDesc(m.id)]);
  // 用户在某个应用里可见的菜单：角色绑定的菜单 ∪ 公共菜单，再补上它们的上级目录
  function visibleMenus(u, app) {
    const bound = new Set(rolesOf(u).all.map(roleById).filter((r) => r.app === app).flatMap((r) => r.menus));
    const out = new Set();
    menusOfApp(app).forEach((m) => {
      if (m.type !== 'menu' || !(m.public || bound.has(m.id))) return;
      out.add(m.id);
      let p = menuById(m.parent);
      while (p) { out.add(p.id); p = menuById(p.parent); }
    });
    return out;
  }
  const menuSrc = (u, m) => (m.public ? ['公共菜单'] : rolesOf(u).all.map(roleById).filter((r) => r.menus.includes(m.id)).map((r) => r.name));
  // 可见菜单树（给前端渲染导航用的结构）
  const menuTree = (u, app, parent = null) => { const vis = visibleMenus(u, app); return menuKids(app, parent).filter((m) => vis.has(m.id)).map((m) => (m.type === 'dir' ? { name: m.name, children: menuTree(u, app, m.id) } : { code: m.code, name: m.name, path: m.path })); };
  // ★ 用户中心自己的侧边栏也由「菜单 + 角色」决定
  const ucMenus = () => { const me = userById(state.me); if (!me) return []; const vis = visibleMenus(me, 'uc'); return menusOfApp('uc').filter((m) => m.type === 'menu' && vis.has(m.id)); };
  const navOk = (k) => ucMenus().some((m) => m.code === k);
  const landing = () => { const m = ucMenus().find((x) => !x.public); return m ? `#/${m.code}` : '#/me'; };

  /* ---------------------------------------------------------
     5. 组织架构
     --------------------------------------------------------- */
  function treeHtml(id, depth) {
    const d = deptById(id);
    const kids = childrenOf(id);
    const open = state.orgOpen.has(id);
    const count = usersIn(id, true).length;
    return `
      <div class="tree-node ${state.orgSel === id ? 'on' : ''}" style="padding-left:${8 + depth * 16}px" data-act="org-sel" data-id="${id}">
        <span class="caret" ${kids.length ? `data-act="org-toggle" data-id="${id}"` : ''}>${kids.length ? (open ? '▾' : '▸') : ''}</span>
        <span>${depth === 0 ? '🏢' : '📁'} ${esc(d.name)}</span><span class="cnt">${count}</span>
      </div>
      ${open ? kids.map((k) => treeHtml(k.id, depth + 1)).join('') : ''}`;
  }
  function viewOrg() {
    const d = deptById(state.orgSel) || DEPTS[0];
    const members = usersIn(d.id, state.orgSub);
    const leader = userById(d.leader);
    const subs = childrenOf(d.id);
    return `
      <div class="page-head">
        <div><h1>组织架构</h1><div class="sub">维护部门树与部门成员；应用的可访问范围按部门控制。</div></div>
      </div>
      <div class="org">
        <div class="card">
          <div class="card-h"><h3>部门</h3><div class="grow"></div><span class="small muted">${DEPTS.length} 个</span></div>
          <div class="tree">${treeHtml('root', 0)}</div>
        </div>
        <div class="stack">
          <div class="card pad">
            <div class="row wrap-row" style="gap:12px">
              <div>
                <div class="dept-path">${deptPath(d.id).map(esc).join(' / ')}</div>
                <h2 style="font-size:19px;margin-top:2px">${esc(d.name)}</h2>
              </div>
              <div class="grow"></div>
              ${can('grant:view') ? `<button class="btn" data-act="grant-goto" data-t="dept" data-id="${d.id}">授权</button>` : ''}
              ${can('org:manage') ? `<button class="btn" data-act="dept-new" data-id="${d.id}">＋ 新建子部门</button>` : ''}
              ${can('org:manage') ? `<button class="btn" data-act="dept-edit" data-id="${d.id}">编辑</button>` : ''}
              ${d.parent && can('org:manage') ? `<button class="btn btn-danger" data-act="dept-del" data-id="${d.id}">删除</button>` : ''}
            </div>
            <div class="kv mt16">
              <span class="k">负责人</span><span>${leader ? `<span class="row">${avatar(leader, 'sm')}${esc(leader.name)} <span class="muted small">${esc(posName(leader))}</span>${leader.status === 'disabled' ? ' <span class="badge badge-orange">负责人已停用，请更换</span>' : ''}</span>` : `<span class="muted">未设置${d.parent ? '（成员的直属上级取上级部门负责人）' : ''}</span>`}</span>
              <span class="k">直属成员 / 含子部门</span><span>${usersIn(d.id, false).length} 人 / ${usersIn(d.id, true).length} 人</span>
              <span class="k">子部门</span><span>${subs.length ? subs.map((s) => `<a class="tag" data-act="org-sel" data-id="${s.id}" style="cursor:pointer">${esc(s.name)}</a>`).join(' ') : '<span class="muted">无</span>'}</span>
            </div>
          </div>
          <div class="card">
            <div class="card-h">
              <h3>成员</h3><span class="small muted">${members.length} 人</span>
              <label class="check" style="margin:0 0 0 8px"><input type="checkbox" data-act="org-sub" ${state.orgSub ? 'checked' : ''}> 包含子部门</label>
              <div class="grow"></div>
              ${can('org:manage') ? `<button class="btn btn-sm" data-act="dept-move-in" data-id="${d.id}">调入成员</button>` : ''}
              ${can('user:create') ? `<button class="btn btn-sm btn-primary" data-act="user-new" data-dept="${d.id}">＋ 新建用户</button>` : ''}
            </div>
            ${members.length ? `
              <table class="table">
                <thead><tr><th>成员</th><th>部门</th><th>岗位</th><th>状态</th><th></th></tr></thead>
                <tbody>${members.map((u) => `
                  <tr class="click" data-nav="#/users/${u.id}">
                    <td><span class="row">${avatar(u, 'sm')}<b>${esc(u.name)}</b> <span class="muted small">${esc(u.account)}</span>${d.leader === u.id ? ' <span class="badge badge-purple">负责人</span>' : ''}</span></td>
                    <td class="small">${esc(deptById(u.dept).name)}</td><td>${esc(posName(u))}</td><td>${statusBadge(u.status)}</td>
                    <td class="nowrap" style="text-align:right">${d.leader !== u.id && u.status !== 'disabled' && can('org:manage') ? `<button class="btn-link" data-act="dept-leader" data-id="${d.id}" data-user="${u.id}">设为负责人</button>` : ''}</td>
                  </tr>`).join('')}</tbody>
              </table>` : '<div class="empty">该部门还没有成员</div>'}
          </div>
        </div>
      </div>`;
  }
  // 部门下拉：按树的顺序缩进展示
  function deptOptions(sel, exclude = []) {
    const out = [];
    const walk = (id, depth) => {
      if (exclude.includes(id)) return;
      const d = deptById(id);
      out.push(`<option value="${id}" ${sel === id ? 'selected' : ''}>${'　'.repeat(depth)}${esc(d.name)}</option>`);
      childrenOf(id).forEach((c) => walk(c.id, depth + 1));
    };
    walk('root', 0);
    return out.join('');
  }
  function openDeptForm(mode, id) {
    const d = deptById(id);
    const editing = mode === 'edit';
    openModal({
      title: editing ? `编辑部门 · ${d.name}` : `新建子部门 · ${d.name}`,
      body: `
        <div class="field"><label>部门名称<span class="req">*</span></label><input type="text" data-dname value="${editing ? esc(d.name) : ''}" placeholder="例如：支付中台"></div>
        ${editing && d.parent ? `<div class="field"><label>上级部门</label><select data-dparent>${deptOptions(d.parent, [d.id, ...descendants(d.id)])}</select><div class="hint">移动部门会连同其子部门与成员一起移动。</div></div>` : ''}
        ${editing ? `<div class="field"><label>负责人</label><select data-dleader><option value="">（不设置）</option>${usersIn(d.id, true).filter((u) => u.status !== 'disabled').map((u) => `<option value="${u.id}" ${d.leader === u.id ? 'selected' : ''}>${esc(u.name)} · ${esc(deptById(u.dept).name)}</option>`).join('')}</select><div class="hint">只能从本部门及下级部门的成员中选择；直属上级按负责人计算</div></div>`
          : '<div class="hint small muted">新部门还没有成员，调入成员后再设置负责人。</div>'}`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="dept-save" data-mode="${mode}" data-id="${id}">保存</button>`,
    });
  }
  function saveDept(mode, id) {
    const name = $('[data-dname]').value.trim();
    if (!name) { toast('请填写部门名称', 'warn'); return; }
    const parent = mode === 'edit' ? ($('[data-dparent]') ? $('[data-dparent]').value : deptById(id).parent) : id;
    if (DEPTS.some((x) => x.parent === parent && x.name === name && x.id !== id)) { toast('同一上级下已有同名部门', 'warn'); return; }
    const leader = $('[data-dleader]') ? $('[data-dleader]').value || null : null;
    if (mode === 'edit') {
      const d = deptById(id);
      const moved = d.parent !== parent;
      Object.assign(d, { name, parent, leader });
      audit('编辑部门', name, moved ? `移动到 ${deptPath(parent).join('/')}` : '');
      const cleared = moved ? fixLeaders() : '';
      toast(`部门已保存${cleared}`, cleared ? 'warn' : 'ok', cleared ? 4000 : 2600);
    } else {
      const d = { id: uid('d'), name, parent, leader: null };
      DEPTS.push(d);
      state.orgOpen.add(parent);
      state.orgSel = d.id;
      audit('新建部门', name, `上级：${deptById(parent).name}`);
      toast(`已新建部门「${name}」`);
    }
    closeModal();
    render();
  }
  function deleteDept(id) {
    const d = deptById(id);
    const subs = childrenOf(id).length;
    const n = usersIn(id, false).length;
    if (subs || n) {
      openModal({
        title: `无法删除「${d.name}」`,
        body: `<div class="callout callout-warn"><span>⚠️</span><div>部门下还有 ${subs ? `${subs} 个子部门` : ''}${subs && n ? '、' : ''}${n ? `${n} 名成员` : ''}。请先移走或删除后再删除该部门。</div></div>`,
        footer: '<button class="btn btn-primary" data-close-modal>知道了</button>',
      });
      return;
    }
    const usedBy = APPS.filter((a) => a.scope.depts.includes(id));
    const gs = GRANTS.filter((g) => g.subj === 'dept' && g.to === id);
    openModal({
      title: `删除部门 · ${d.name}`,
      body: `<div>确定删除「${esc(deptPath(id).join(' / '))}」？</div>${usedBy.length ? `<div class="callout callout-warn mt12"><span>⚠️</span><div>该部门在 ${usedBy.map((a) => esc(a.name)).join('、')} 的可访问范围中，删除后会从范围中移除。</div></div>` : ''}${gs.length ? `<div class="callout callout-warn mt12"><span>⚠️</span><div>该部门有 ${gs.length} 条授权（${gs.map((g) => esc(refName(g))).join('、')}），删除后一并撤销。</div></div>` : ''}`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="dept-del-confirm" data-id="${id}">删除</button>`,
    });
  }
  function openMoveIn(deptId) {
    const d = deptById(deptId);
    const cands = USERS.filter((u) => u.dept !== deptId && u.status !== 'disabled');
    openModal({
      title: `调入成员 · ${d.name}`,
      body: `
        <input class="input" style="width:100%" placeholder="搜索姓名或账号" data-pick-q>
        <div class="pick-list mt8">${cands.map((u) => `
          <label class="pick-item" data-pick-row="${esc(u.name + u.account)}"><input type="checkbox" value="${u.id}" data-pick>${avatar(u, 'sm')}<b>${esc(u.name)}</b><span class="muted small">${esc(deptPath(u.dept).slice(-1)[0])} · ${esc(posName(u))}</span></label>`).join('')}</div>
        <div class="hint small muted mt8">调入后，成员的主部门变更为「${esc(d.name)}」。</div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="move-in-confirm" data-id="${deptId}">调入</button>`,
    });
  }
  function moveUsers(ids, deptId) {
    ids.forEach((id) => { const u = userById(id); audit('调整部门', `${u.name}（${u.account}）`, `${deptById(u.dept).name} → ${deptById(deptId).name}`); u.dept = deptId; });
    const cleared = fixLeaders();
    toast(`已将 ${ids.length} 人调入「${deptById(deptId).name}」${cleared}`, cleared ? 'warn' : 'ok', cleared ? 4000 : 2600);
  }
  // 部门负责人必须在本部门或其下级部门中；人员调走 / 部门移动后不再满足的，自动清空负责人
  const leaderOk = (d, uid_) => usersIn(d.id, true).some((u) => u.id === uid_);
  function fixLeaders() {
    const cleared = DEPTS.filter((d) => d.leader && !leaderOk(d, d.leader));
    cleared.forEach((d) => { audit('清空部门负责人', d.name, `${userById(d.leader).name} 已不在该部门及其下级部门`); AUDIT[0].actor = 'system'; d.leader = null; });
    return cleared.length ? `；已清空 ${cleared.map((d) => `「${d.name}」`).join('')}的负责人` : '';
  }

  /* ---------------------------------------------------------
     6. 用户管理
     --------------------------------------------------------- */
  function filteredUsers() {
    const q = state.uq.trim().toLowerCase();
    const deptIds = state.udept ? [state.udept, ...descendants(state.udept)] : null;
    return USERS.filter((u) => (state.ustatus === 'all' || u.status === state.ustatus)
      && (!deptIds || deptIds.includes(u.dept))
      && (!q || u.name.toLowerCase().includes(q) || u.account.includes(q) || u.email.includes(q) || u.phone.includes(q)));
  }
  function userRowsHtml() {
    const list = filteredUsers();
    if (!list.length) return `<tr><td colspan="6"><div class="empty">没有符合条件的用户</div></td></tr>`;
    return list.map((u) => `
      <tr class="click" data-nav="#/users/${u.id}">
        <td><span class="row">${avatar(u, 'sm')}<span><b>${esc(u.name)}</b>${roleBadges(u)}<div class="muted small">${esc(u.account)}</div></span></span></td>
        <td class="small">${esc(deptPath(u.dept).slice(1).join(' / ') || deptPath(u.dept)[0])}</td>
        <td>${esc(posName(u))}</td>
        <td>${statusBadge(u.status)}</td>
        <td class="small muted nowrap">${u.lastLogin || '从未登录'}</td>
        <td class="nowrap" data-stop style="text-align:right">
          ${can('user:edit') ? `<button class="btn-link" data-act="user-edit" data-id="${u.id}">编辑</button>` : ''}
          ${can('user:reset_pwd') ? `<button class="btn-link" data-act="user-reset" data-id="${u.id}" ${u.status === 'disabled' ? 'disabled' : ''}>重置密码</button>` : ''}
          ${can('user:disable') ? (u.status === 'locked' ? `<button class="btn-link" data-act="user-unlock" data-id="${u.id}">解锁</button>` : '') : ''}
          ${can('user:disable') ? (u.status === 'disabled' ? `<button class="btn-link" data-act="user-enable" data-id="${u.id}">启用</button>` : `<button class="btn-link danger" data-act="user-disable" data-id="${u.id}" ${u.id === state.me ? 'disabled' : ''}>停用</button>`) : ''}
        </td>
      </tr>`).join('');
  }
  function viewUsers() {
    const count = (s) => USERS.filter((u) => s === 'all' || u.status === s).length;
    const tab = (s, label) => `<button class="${state.ustatus === s ? 'on' : ''}" data-act="ustatus" data-s="${s}">${label}<span class="n">${count(s)}</span></button>`;
    return `
      <div class="page-head">
        <div><h1>用户管理</h1><div class="sub">本系统自建账号：新建、编辑、停用、重置密码；所有接入应用共用这些账号。</div></div>
        <div class="grow"></div>
        ${can('user:create') ? '<button class="btn btn-primary" data-act="user-new">＋ 新建用户</button>' : ''}
      </div>
      <div class="toolbar">
        <div class="tabs">${tab('all', '全部')}${tab('active', '正常')}${tab('pending', '未激活')}${tab('locked', '已锁定')}${tab('disabled', '已停用')}</div>
        <div class="grow"></div>
        <select class="select" data-udept><option value="">全部部门</option>${deptOptions(state.udept)}</select>
        <input class="input" style="width:240px" placeholder="搜索姓名 / 账号 / 邮箱 / 手机" data-uq value="${esc(state.uq)}">
      </div>
      <div class="card">
        <table class="table">
          <thead><tr><th>用户</th><th>部门</th><th>岗位</th><th>状态</th><th>最近登录</th><th></th></tr></thead>
          <tbody id="user-rows">${userRowsHtml()}</tbody>
        </table>
      </div>`;
  }
  function refreshUserRows() {
    const tb = $('#user-rows');
    if (tb) tb.innerHTML = userRowsHtml();
  }

  function openUserForm(id, presetDept) {
    const u = id ? userById(id) : null;
    const v = (k) => esc(u ? u[k] || '' : '');
    openModal({
      title: u ? `编辑用户 · ${u.name}` : '新建用户',
      wide: true,
      body: `
        <div class="grid2">
          <div class="field"><label>姓名<span class="req">*</span></label><input type="text" data-f="name" value="${v('name')}"></div>
          <div class="field"><label>账号<span class="req">*</span></label><input type="text" data-f="account" value="${v('account')}" ${u ? 'disabled' : ''} placeholder="小写字母、数字、点、下划线，3–32 位"><div class="hint">${u ? '账号创建后不可修改（各系统以它识别用户）' : '用于登录，创建后不可修改'}</div></div>
          <div class="field"><label>邮箱<span class="req">*</span></label><input type="email" data-f="email" value="${v('email')}"></div>
          <div class="field"><label>手机</label><input type="text" data-f="phone" value="${v('phone')}"></div>
          <div class="field"><label>部门<span class="req">*</span></label><select data-f="dept">${deptOptions(u ? u.dept : presetDept || 'root')}</select></div>
        </div>
        <div data-ferr class="small" style="color:var(--danger)"></div>
        ${u ? '' : '<div class="callout callout-info mt8"><span>ℹ️</span><div>创建后生成一次性临时密码，账号为「未激活」；用户首次登录时必须修改密码。</div></div>'}`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="user-save" data-id="${id || ''}">保存</button>`,
    });
  }
  const ACCOUNT_RE = /^[a-z0-9._]{3,32}$/;
  const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  function saveUser(id) {
    const f = (k) => { const el = $(`[data-f="${k}"]`); return el ? el.value.trim() : ''; };
    const data = { name: f('name'), account: f('account'), email: f('email'), phone: f('phone'), dept: f('dept') };
    const err = (m) => { $('[data-ferr]').textContent = m; };
    if (!data.name) return err('请填写姓名');
    if (!id) {
      if (!ACCOUNT_RE.test(data.account)) return err('账号格式不正确：小写字母、数字、点、下划线，3–32 位');
      if (USERS.some((u) => u.account === data.account)) return err('账号已存在');
    }
    if (!EMAIL_RE.test(data.email)) return err('邮箱格式不正确');
    if (USERS.some((u) => u.email === data.email && u.id !== id)) return err('邮箱已被其他用户使用');
    if (id) {
      const u = userById(id);
      delete data.account;
      const deptChanged = u.dept !== data.dept;
      Object.assign(u, data);
      audit('编辑用户', `${u.name}（${u.account}）`);
      const cleared = deptChanged ? fixLeaders() : '';
      closeModal();
      toast(`已保存${cleared}`, cleared ? 'warn' : 'ok', cleared ? 4000 : 2600);
      render();
      return;
    }
    const u = Object.assign({ id: uid('u'), status: 'pending', created: nowStr(), lastLogin: null }, data);
    USERS.push(u);
    const tmp = tempPassword();
    PWD[u.id] = tmp;
    audit('新建用户', `${u.name}（${u.account}）`, deptById(u.dept).name);
    render();
    showTempPassword(u, tmp, '用户已创建');
  }
  function showTempPassword(u, tmp, title) {
    openModal({
      title,
      body: `
        <div class="kv"><span class="k">用户</span><span>${esc(u.name)}（${esc(u.account)}）</span><span class="k">临时密码</span><span><span class="secret"><code data-tmp>${esc(tmp)}</code><button class="btn btn-sm" data-act="copy" data-text="${esc(tmp)}">复制</button></span></span></div>
        <div class="callout callout-warn mt12"><span>⚠️</span><div>临时密码<b>只显示这一次</b>，请通过安全渠道告知用户。用户首次登录时必须修改密码。</div></div>`,
      footer: '<button class="btn btn-primary" data-close-modal>我已记下</button>',
    });
  }
  function resetPassword(id) {
    const u = userById(id);
    openModal({
      title: `重置密码 · ${u.name}`,
      body: '<div>将生成新的临时密码，原密码立即失效；用户下次登录时必须修改密码。该用户在各应用中的登录会话将被注销。</div>',
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="user-reset-confirm" data-id="${id}">重置</button>`,
    });
  }
  function setStatus(id, status, action) {
    const u = userById(id);
    if (status === 'disabled' && rolesOf(u).all.includes('uc-super') && !superCount(u.id)) { toast('不能停用唯一的超级管理员', 'warn'); return; }
    u.status = status;
    if (status === 'active') state.fails[id] = 0;
    audit(action, `${u.name}（${u.account}）`);
    toast(`${action}：${u.name}`);
    render();
  }
  function confirmDisable(ids) {
    const names = ids.map((id) => userById(id).name);
    openModal({
      title: ids.length > 1 ? `停用 ${ids.length} 名用户` : `停用用户 · ${names[0]}`,
      body: `<div>停用后无法登录用户中心及所有接入应用，已登录的会话立即失效。数据保留，可随时启用。</div><div class="small muted mt8">${names.map(esc).join('、')}</div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="disable-confirm" data-ids="${ids.join(',')}">停用</button>`,
    });
  }

  /* ---------------------------------------------------------
     7. 用户详情
     --------------------------------------------------------- */
  function appsOf(u) {
    const chain = (id) => { const out = []; let d = deptById(id); while (d) { out.push(d.id); d = deptById(d.parent); } return out; };
    const mine = chain(u.dept);
    return APPS.filter((a) => a.scope.all || a.scope.depts.some((d) => mine.includes(d)));
  }
  function viewUser(id) {
    const u = userById(id);
    if (!u) return '<div class="empty">用户不存在</div>';
    const mgr = managerOf(u);
    const tab = state.utab;
    const logs = LOGIN_LOGS.filter((l) => l.user === u.id);
    const ptab = (k, l) => `<button class="${tab === k ? 'on' : ''}" data-act="utab" data-t="${k}">${l}</button>`;
    return `
      <div class="page-head"><div class="grow"></div><button class="btn" data-nav="#/users">← 返回用户列表</button></div>
      <div class="card pad">
        <div class="profile">
          ${avatar(u, 'lg')}
          <div>
            <div class="row"><h2 style="font-size:20px">${esc(u.name)}</h2>${statusBadge(u.status)}${roleBadges(u)}</div>
            <div class="muted small mt4">${esc(u.account)} · ${esc(deptPath(u.dept).join(' / '))} · ${esc(posName(u))}</div>
          </div>
          <div class="grow"></div>
          ${can('user:edit') ? `<button class="btn" data-act="user-edit" data-id="${u.id}">编辑</button>` : ''}
          ${can('user:reset_pwd') ? `<button class="btn" data-act="user-reset" data-id="${u.id}" ${u.status === 'disabled' ? 'disabled' : ''}>重置密码</button>` : ''}
          ${can('user:disable') && u.status === 'locked' ? `<button class="btn" data-act="user-unlock" data-id="${u.id}">解锁</button>` : ''}
          ${can('user:disable') ? (u.status === 'disabled' ? `<button class="btn" data-act="user-enable" data-id="${u.id}">启用</button>` : `<button class="btn btn-danger" data-act="user-disable" data-id="${u.id}" ${u.id === state.me ? 'disabled' : ''}>停用</button>`) : ''}
        </div>
        <div class="ptabs">${ptab('info', '基本信息')}${can('grant:view') ? ptab('perm', '权限') : ''}${ptab('login', `登录记录（${logs.length}）`)}${ptab('apps', '可访问的应用')}</div>
        ${tab === 'info' ? `
          <div class="kv">
            <span class="k">邮箱</span><span>${esc(u.email)}</span>
            <span class="k">手机</span><span>${esc(u.phone) || '<span class="muted">—</span>'}</span>
            <span class="k">部门</span><span>${esc(deptPath(u.dept).join(' / '))}</span>
            <span class="k">岗位</span><span>${esc(posName(u)) || '<span class="muted">—</span>'} <span class="muted small">（在权限管理中授权）</span></span>
            <span class="k">直属上级</span><span>${mgr ? `<a class="btn-link" data-nav="#/users/${mgr.id}">${esc(mgr.name)}</a> <span class="muted small">（${esc(deptById(u.dept).leader === mgr.id ? '所在部门负责人' : '上级部门负责人')}，由组织架构计算）</span>` : '<span class="muted">—</span>'}</span>
            <span class="k">创建时间</span><span>${u.created}</span>
            <span class="k">最近登录</span><span>${u.lastLogin || '从未登录'}</span>
          </div>` : tab === 'perm' ? userPermHtml(u) : tab === 'login' ? (logs.length ? `
          <table class="table"><thead><tr><th>时间</th><th>应用</th><th>IP</th><th>结果</th></tr></thead>
          <tbody>${logs.map((l) => `<tr><td class="nowrap">${l.time}</td><td>${esc(l.app)}</td><td class="mono">${l.ip}</td><td>${l.ok ? '<span class="badge badge-green">成功</span>' : `<span class="badge badge-red">失败 · ${esc(l.reason || '')}</span>`}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">没有登录记录</div>') : `
          <div class="small muted mb12">按应用的「可访问范围」与该用户所在部门计算。</div>
          ${appsOf(u).map((a) => `<div class="row mb12"><span class="app-ico" style="width:32px;height:32px;font-size:16px">${a.icon}</span><b>${esc(a.name)}</b><span class="muted small">${esc(a.desc)}</span>${a.status === 'disabled' ? '<span class="badge badge-gray">应用已停用</span>' : ''}</div>`).join('') || '<div class="empty">没有可访问的应用</div>'}`}
      </div>`;
  }

  /* ---------------------------------------------------------
     7.5 角色管理 / 岗位管理 / 用户权限
     --------------------------------------------------------- */
  const ROLE_APPS = () => [UC_APP, ...APPS.map((a) => ({ id: a.id, name: a.name, icon: a.icon }))].filter((a) => OPS.some((o) => o.app === a.id) || ROLES.some((r) => r.app === a.id));
  const roleTag = (r) => `<span class="badge badge-purple" title="${esc(appName(r.app))}">${esc(r.name)}<span style="opacity:.6;font-weight:500"> · ${esc(appName(r.app))}</span></span>`;

  // 应用详情 ·「角色」：角色属于应用，只能包含本应用的操作
  function appRolesHtml(appId) {
    state.roleApp = appId;
    const list = ROLES.filter((r) => r.app === appId);
    if (!list.some((r) => r.id === state.roleSel)) state.roleSel = (list[0] || {}).id;
    const r = roleById(state.roleSel);
    return `
      <div class="org">
        <div class="card">
          <div class="card-h"><h3>角色</h3><span class="small muted">${list.length} 个</span><div class="grow"></div>${can('role:manage') ? '<button class="btn btn-sm btn-primary" data-act="role-new">＋ 新建</button>' : ''}</div>
          <div class="tree">
          ${list.map((x) => { const n = usersWithRole(x.id).length; return `
            <div class="tree-node ${x.id === state.roleSel ? 'on' : ''}" data-act="role-sel" data-id="${x.id}" style="align-items:flex-start;flex-direction:column;gap:2px">
              <span class="row" style="gap:6px">${esc(x.name)}${x.builtin ? ' <span class="badge badge-gray">内置</span>' : ''}</span>
              <span class="small muted">${x.ops.length} 项操作 · ${n} 人</span>
            </div>`; }).join('') || '<div class="empty">该应用还没有角色</div>'}
          </div>
        </div>
        ${r ? roleDetail(r) : '<div class="card pad"><div class="empty">请先在「操作目录」登记操作，再新建角色</div></div>'}
      </div>`;
  }
  function roleDetail(r) {
    const tab = state.roleTab || 'ops';
    const gs = GRANTS.filter((g) => g.kind === 'role' && g.ref === r.id);
    const poss = POSITIONS.filter((x) => x.roles.includes(r.id));
    const holders = usersWithRole(r.id);
    const editable = can('role:manage') && !r.builtin;
    const mods = [...new Set(OPS.filter((o) => o.app === r.app).map((o) => o.module))];
    return `
      <div class="stack">
        <div class="card pad">
          <div class="row wrap-row" style="gap:10px">
            <div>
              <div class="row"><h2 style="font-size:19px">${esc(r.name)}</h2>${r.builtin ? '<span class="badge badge-gray">内置</span>' : ''}<span class="tag">${esc(appName(r.app))}</span></div>
              <div class="small muted mt4"><span class="mono">${esc(r.code)}</span> · ${esc(r.desc || '')}</div>
            </div>
            <div class="grow"></div>
            ${can('role:manage') ? `<button class="btn" data-act="role-copy" data-id="${r.id}">复制</button>` : ''}
            ${editable ? `<button class="btn" data-act="role-edit" data-id="${r.id}">编辑</button><button class="btn btn-danger" data-act="role-del" data-id="${r.id}">删除</button>` : ''}
          </div>
          <div class="ptabs" style="margin-bottom:0">
            <button class="${tab === 'ops' ? 'on' : ''}" data-act="role-tab" data-t="ops">操作权限（${r.ops.length}）</button>
            <button class="${tab === 'menus' ? 'on' : ''}" data-act="role-tab" data-t="menus">菜单权限（${r.menus.filter((id) => (menuById(id) || {}).type === 'menu').length}）</button>
            <button class="${tab === 'grants' ? 'on' : ''}" data-act="role-tab" data-t="grants">授权（${gs.length}）</button>
            <button class="${tab === 'holders' ? 'on' : ''}" data-act="role-tab" data-t="holders">拥有者（${holders.length}）</button>
          </div>
        </div>
        ${tab === 'ops' ? `
          <div class="card">
            <div class="card-h"><h3>操作权限</h3><span class="small muted">${r.builtin ? '内置角色的操作不可修改' : editable ? '勾选该角色可以执行的操作' : '只读'}</span><div class="grow"></div>
              ${editable ? `<button class="btn btn-sm btn-primary" data-act="role-ops-save" data-id="${r.id}">保存</button>` : ''}</div>
            <div class="card-b">
              ${mods.map((mod) => { const ops = OPS.filter((o) => o.app === r.app && o.module === mod); return `
                <div class="mb16">
                  <label class="check" style="font-weight:650"><input type="checkbox" data-mod-all="${esc(mod)}" ${ops.every((o) => r.ops.includes(o.code)) ? 'checked' : ''} ${editable ? '' : 'disabled'}> ${esc(mod)}</label>
                  <div class="ops-grid">${ops.map((o) => `<label class="check"><input type="checkbox" data-op="${o.code}" data-mod="${esc(mod)}" ${r.ops.includes(o.code) ? 'checked' : ''} ${editable ? '' : 'disabled'}> ${esc(o.name)} <span class="mono muted" style="font-size:11px">${o.code}</span></label>`).join('')}</div>
                </div>`; }).join('') || '<div class="empty">该应用还没有登记操作，请先在「应用接入 → 操作目录」中添加</div>'}
            </div>
          </div>` : tab === 'menus' ? roleMenusHtml(r, editable) : tab === 'grants' ? `
          <div class="card">
            <div class="card-h"><h3>授予的用户和组织</h3><span class="small muted">${gs.length} 条</span><div class="grow"></div>
              ${can('grant:manage') ? `<button class="btn btn-sm btn-primary" data-act="grant-to" data-kind="role" data-ref="${r.id}">＋ 授权</button>` : ''}</div>
            ${grantTable(gs, { hideRef: true })}
          </div>
          <div class="card">
            <div class="card-h"><h3>通过岗位授予</h3><span class="small muted">以下岗位的默认角色包含「${esc(r.name)}」，岗位授给谁，谁就获得该角色</span></div>
            ${poss.length ? `<table class="table"><thead><tr><th>岗位</th><th>岗位的授权对象</th></tr></thead><tbody>${poss.map((x) => { const pg = GRANTS.filter((g) => g.kind === 'pos' && g.ref === x.id); return `
              <tr><td><b>${esc(x.name)}</b></td><td>${pg.map(subjTag).join(' ') || '<span class="muted small">尚未授权</span>'}</td></tr>`; }).join('')}</tbody></table>` : '<div class="empty">没有岗位关联该角色</div>'}
          </div>` : `
          <div class="card">
            <div class="card-h"><h3>实际拥有该角色的人</h3><span class="small muted">${holders.length} 人 · 由授权自动计算，只读</span></div>
            ${holders.length ? `<table class="table"><thead><tr><th>用户</th><th>部门</th><th>来源</th></tr></thead><tbody>${holders.map((u) => `
              <tr><td><span class="row">${avatar(u, 'sm')}<b>${esc(u.name)}</b><span class="muted small">${esc(u.account)}</span>${u.status === 'disabled' ? statusBadge(u.status) : ''}</span></td><td class="small">${esc(deptById(u.dept).name)}</td>
              <td class="small">${rolesOf(u).src.get(r.id).map(esc).join('、')}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">还没有人拥有该角色</div>'}
          </div>`}
      </div>`;
  }
  function roleMenusHtml(r, editable) {
    const rows = (parent, depth) => menuKids(r.app, parent).map((m) => `
      <label class="check" style="padding-left:${depth * 22}px">
        <input type="checkbox" data-rmenu="${m.id}" ${m.public || r.menus.includes(m.id) || (m.type === 'dir' && menuDesc(m.id).some((x) => menuById(x).type === 'menu' && (menuById(x).public || r.menus.includes(x)))) ? 'checked' : ''} ${editable && !m.public ? '' : 'disabled'}>
        ${m.icon} ${m.type === 'dir' ? `<b>${esc(m.name)}</b> <span class="badge badge-gray">目录</span>` : `${esc(m.name)} <span class="mono muted" style="font-size:11px">${esc(m.path)}</span>`}${m.public ? ' <span class="badge badge-green">公共</span>' : ''}
      </label>${rows(m.id, depth + 1)}`).join('');
    return `
      <div class="card">
        <div class="card-h"><h3>菜单权限</h3><span class="small muted">${r.builtin ? '内置角色的菜单不可修改' : editable ? '勾选该角色可以看到的菜单；勾选目录即勾选其下全部菜单' : '只读'}</span><div class="grow"></div>
          ${editable ? `<button class="btn btn-sm btn-primary" data-act="role-menus-save" data-id="${r.id}">保存</button>` : ''}</div>
        <div class="card-b">${rows(null, 0) || '<div class="empty">该应用还没有菜单，请先在「菜单」中添加</div>'}
          <div class="small muted mt12">公共菜单所有人可见，无需绑定。目录只在至少有一个可见子菜单时显示。</div></div>
      </div>`;
  }
  // 应用详情 ·「菜单」
  function appMenusHtml(a) {
    const editable = a.id !== 'uc' && can('app:manage');
    const roleCnt = (m) => ROLES.filter((r) => r.menus.includes(m.id)).length;
    const rows = (parent, depth) => menuKids(a.id, parent).map((m) => `
      <tr>
        <td class="nowrap"><span style="padding-left:${depth * 22}px">${m.icon} ${m.type === 'dir' ? `<b>${esc(m.name)}</b>` : esc(m.name)}</span></td>
        <td class="nowrap">${m.type === 'dir' ? '<span class="badge badge-gray">目录</span>' : '<span class="badge badge-blue">菜单</span>'}${m.public ? ' <span class="badge badge-green">公共</span>' : ''}</td>
        <td class="mono">${esc(m.code)}</td>
        <td class="mono nowrap">${esc(m.path || '—')}</td>
        <td class="small nowrap">${m.type === 'dir' ? '<span class="muted">随子菜单</span>' : m.public ? '<span class="muted">所有人</span>' : `${roleCnt(m)} 个角色`}</td>
        <td class="nowrap" style="text-align:right">${editable ? `${m.type === 'dir' ? `<button class="btn-link" data-act="menu-new" data-app="${a.id}" data-parent="${m.id}">＋ 子菜单</button>` : ''}<button class="btn-link" data-act="menu-up" data-id="${m.id}" title="上移">↑</button><button class="btn-link" data-act="menu-edit" data-id="${m.id}">编辑</button><button class="btn-link danger" data-act="menu-del" data-id="${m.id}">删除</button>` : ''}</td>
      </tr>${rows(m.id, depth + 1)}`).join('');
    const pv = userById(state.menuPreview) || userById(state.me);
    const tree = (parent, depth) => { const vis = visibleMenus(pv, a.id); return menuKids(a.id, parent).filter((m) => vis.has(m.id)).map((m) => m.type === 'dir'
      ? `<div class="pv-dir" style="padding-left:${10 + depth * 14}px">${m.icon} ${esc(m.name)}</div>${tree(m.id, depth + 1)}`
      : `<div class="pv-item" style="padding-left:${10 + depth * 14}px" title="来自：${esc(menuSrc(pv, m).join('、'))}">${m.icon} ${esc(m.name)}</div>`).join(''); };
    return `
      <div style="display:grid;grid-template-columns:minmax(0,1fr) 320px;gap:16px;align-items:start">
        <div class="card">
          <div class="card-h"><h3>菜单</h3><span class="small muted">${a.id === 'uc' ? '内置应用的菜单随版本发布；用户中心的侧边栏就是按它生成的' : '应用的导航结构；角色在「角色 → 菜单权限」中绑定'}</span><div class="grow"></div>
            ${editable ? `<button class="btn btn-sm btn-primary" data-act="menu-new" data-app="${a.id}" data-parent="">＋ 新增</button>` : ''}</div>
          ${menusOfApp(a.id).length ? `<table class="table"><thead><tr><th>名称</th><th>类型</th><th>编码</th><th>路由</th><th>可见</th><th></th></tr></thead><tbody>${rows(null, 0)}</tbody></table>` : '<div class="empty">还没有菜单</div>'}
        </div>
        <div class="card">
          <div class="card-h"><h3>菜单预览</h3><div class="grow"></div>
            <select class="input" style="width:auto;padding:4px 8px" data-menu-preview>${USERS.filter((u) => u.status !== 'disabled').map((u) => `<option value="${u.id}" ${u.id === pv.id ? 'selected' : ''}>${esc(u.name)}</option>`).join('')}</select></div>
          <div class="card-b">
            <div class="pv-side"><div class="pv-brand">${a.icon} ${esc(a.name)}</div>${tree(null, 0) || '<div class="pv-empty">没有可见菜单</div>'}</div>
            <div class="small muted mt8">${esc(pv.name)} 的角色：${rolesOf(pv).all.map(roleById).filter((r) => r.app === a.id).map((r) => esc(r.name)).join('、') || '无'}（鼠标悬停菜单可看来源）</div>
            <div class="small muted mt12 mb12">应用调用开放接口 <span class="mono">GET /open/v1/users/{id}/authz</span> 取得可见菜单树渲染导航；路由守卫拦截不在树里的路径：</div>
            <pre class="code">${esc(JSON.stringify({ user: pv.account, can_access: appsOf(pv).some((x) => x.id === a.id) || a.id === 'uc', menus: menuTree(pv, a.id) }, null, 2))}</pre>
          </div>
        </div>
      </div>`;
  }
  function openMenuForm(app, id, parent) {
    const m = id ? menuById(id) : null;
    const dirs = menusOfApp(app).filter((x) => x.type === 'dir' && (!m || x.id !== m.id));
    const type = m ? m.type : 'menu';
    openModal({
      title: m ? `编辑菜单 · ${m.name}` : `新增菜单 · ${appName(app)}`,
      body: `
        <div class="field"><label>类型</label><div class="row" style="gap:18px">
          <label class="check" style="margin:0"><input type="radio" name="mtype" value="menu" data-mf-type ${type === 'menu' ? 'checked' : ''} ${m ? 'disabled' : ''}> 菜单（对应一个页面）</label>
          <label class="check" style="margin:0"><input type="radio" name="mtype" value="dir" data-mf-type ${type === 'dir' ? 'checked' : ''} ${m ? 'disabled' : ''}> 目录（只用来分组）</label></div></div>
        <div class="grid2">
          <div class="field"><label>名称<span class="req">*</span></label><input type="text" data-mf="name" value="${m ? esc(m.name) : ''}"></div>
          <div class="field"><label>编码<span class="req">*</span></label><input type="text" data-mf="code" value="${m ? esc(m.code) : ''}" ${m ? 'disabled' : ''} placeholder="小写字母、数字、下划线"><div class="hint">应用内唯一，前端以它识别菜单</div></div>
          <div class="field"><label>上级目录</label><select data-mf="parent"><option value="">（顶级）</option>${dirs.map((x) => `<option value="${x.id}" ${(m ? m.parent : parent) === x.id ? 'selected' : ''}>${esc(x.name)}</option>`).join('')}</select></div>
          <div class="field"><label>图标</label><input type="text" data-mf="icon" value="${m ? esc(m.icon) : '📄'}"></div>
        </div>
        <div class="field" data-mf-pathbox ${type === 'dir' ? 'hidden' : ''}><label>路由<span class="req">*</span></label><input type="text" data-mf="path" value="${m ? esc(m.path) : ''}" placeholder="/reports"></div>
        <label class="check" data-mf-pubbox ${type === 'dir' ? 'hidden' : ''}><input type="checkbox" data-mf="public" ${m && m.public ? 'checked' : ''}> 公共菜单（所有能登录该应用的人都可见，无需角色绑定）</label>
        <div data-mferr class="small" style="color:var(--danger)"></div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="menu-save" data-app="${app}" data-id="${id || ''}">保存</button>`,
    });
  }
  function saveMenu(app, id) {
    const f = (k) => $(`[data-mf="${k}"]`).value.trim();
    const err = (msg) => { $('[data-mferr]').textContent = msg; };
    const type = id ? menuById(id).type : $$('[data-mf-type]').find((x) => x.checked).value;
    const name = f('name'), code = f('code'), path = f('path'), parent = f('parent') || null;
    if (!name) return err('请填写名称');
    if (!id) {
      if (!/^[a-z][a-z0-9_]{1,31}$/.test(code)) return err('编码格式：小写字母开头，小写字母、数字、下划线');
      if (MENUS.some((x) => x.app === app && x.code === code)) return err('编码已存在');
    }
    if (type === 'menu' && !/^\/[\w\-/]*$/.test(path)) return err('路由需以 / 开头');
    if (type === 'menu' && MENUS.some((x) => x.app === app && x.path === path && x.id !== id)) return err('路由已被其他菜单使用');
    const data = { name, icon: f('icon') || '📄', parent, path: type === 'menu' ? path : '', public: type === 'menu' && $('[data-mf="public"]').checked };
    if (id) { Object.assign(menuById(id), data); audit('编辑菜单', `${appName(app)} · ${name}`); }
    else { MENUS.push({ id: `m-${app}-${code}`, app, code, type, sort: Math.max(0, ...menusOfApp(app).map((x) => x.sort)) + 1, ...data }); audit('新增菜单', `${appName(app)} · ${name}`); }
    closeModal(); toast(type === 'menu' && !data.public && !id ? '已保存；在角色的「菜单权限」中绑定后才可见' : '已保存', 'ok', 3000); render();
  }
  function openRoleForm(mode, id) {
    const r = id ? roleById(id) : null;
    const editing = mode === 'edit';
    openModal({
      title: editing ? `编辑角色 · ${r.name}` : mode === 'copy' ? `复制角色 · ${r.name}` : '新建角色',
      body: `
        <div class="field"><label>所属应用</label><input type="text" value="${esc(appName(r ? r.app : state.roleApp))}" disabled><input type="hidden" data-rf="app" value="${r ? r.app : state.roleApp}"><div class="hint">角色属于应用，只能包含该应用的操作</div></div>
        <div class="grid2">
          <div class="field"><label>角色名称<span class="req">*</span></label><input type="text" data-rf="name" value="${editing ? esc(r.name) : mode === 'copy' ? esc(`${r.name}（副本）`) : ''}"></div>
          <div class="field"><label>角色编码<span class="req">*</span></label><input type="text" data-rf="code" value="${editing ? esc(r.code) : ''}" ${editing ? 'disabled' : ''} placeholder="大写字母、数字、下划线"><div class="hint">各应用以编码识别角色，创建后不可修改</div></div>
        </div>
        <div class="field"><label>说明</label><input type="text" data-rf="desc" value="${r ? esc(r.desc || '') : ''}"></div>
        ${mode === 'copy' ? `<div class="callout callout-info"><span>ℹ️</span><div>将复制「${esc(r.name)}」的 ${r.ops.length} 项操作，不复制成员。</div></div>` : ''}
        <div data-rerr class="small" style="color:var(--danger)"></div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="role-save" data-mode="${mode}" data-id="${id || ''}">保存</button>`,
    });
  }
  function saveRole(mode, id) {
    const f = (k) => $(`[data-rf="${k}"]`).value.trim();
    const err = (m) => { $('[data-rerr]').textContent = m; };
    const name = f('name');
    if (!name) return err('请填写角色名称');
    if (mode === 'edit') {
      const r = roleById(id);
      if (ROLES.some((x) => x.app === r.app && x.name === name && x.id !== id)) return err('同一应用下已有同名角色');
      Object.assign(r, { name, desc: f('desc') });
      audit('编辑角色', `${appName(r.app)} · ${name}`);
      closeModal(); toast('已保存'); render(); return;
    }
    const src = id ? roleById(id) : null;
    const app = src ? src.app : f('app');
    const code = f('code');
    if (!/^[A-Z][A-Z0-9_]{1,31}$/.test(code)) return err('编码格式：大写字母开头，大写字母、数字、下划线，2–32 位');
    if (ROLES.some((x) => x.code === code)) return err('编码已存在');
    if (ROLES.some((x) => x.app === app && x.name === name)) return err('同一应用下已有同名角色');
    const r = { id: uid('r'), app, name, code, desc: f('desc'), ops: src ? [...src.ops] : [], menus: src ? [...src.menus] : [] };
    ROLES.push(r);
    state.roleSel = r.id; state.roleTab = 'ops';
    audit(src ? '复制角色' : '新建角色', `${appName(app)} · ${name}`, src ? `来自「${src.name}」` : '');
    closeModal(); toast(src ? '已复制，可继续调整操作' : '角色已创建，请勾选操作权限'); render();
  }
  function saveRoleOps(id) {
    const r = roleById(id);
    const before = new Set(r.ops);
    r.ops = $$('[data-op]').filter((x) => x.checked).map((x) => x.dataset.op);
    const added = r.ops.filter((o) => !before.has(o)).length;
    const removed = [...before].filter((o) => !r.ops.includes(o)).length;
    audit('修改角色操作', `${appName(r.app)} · ${r.name}`, `新增 ${added} 项，移除 ${removed} 项`);
    toast(`已保存；${usersWithRole(id).length} 名拥有者的权限随之更新（下次刷新令牌时生效）`, 'ok', 3600);
    render();
  }
  function posSection() {
    return `
      <div class="card">
        <table class="table">
          <thead><tr><th>岗位</th><th>编码</th><th>默认角色</th><th>授权对象</th><th>人数</th><th></th></tr></thead>
          <tbody>${POSITIONS.map((x) => { const pg = GRANTS.filter((g) => g.kind === 'pos' && g.ref === x.id); const n = USERS.filter((u) => posOf(u).includes(x.id)).length; return `
            <tr>
              <td><b>${esc(x.name)}</b></td><td class="mono">${esc(x.code)}</td>
              <td>${x.roles.map(roleById).filter(Boolean).map(roleTag).join(' ') || '<span class="muted small">无</span>'}</td>
              <td>${pg.length ? `${pg.slice(0, 3).map(subjTag).join(' ')}${pg.length > 3 ? ` <span class="muted small">等 ${pg.length} 个</span>` : ''}` : '<span class="muted small">尚未授权</span>'}</td>
              <td>${n}</td>
              <td class="nowrap" style="text-align:right">${can('grant:manage') ? `<button class="btn-link" data-act="grant-to" data-kind="pos" data-ref="${x.id}">授权</button>` : ''}${can('position:manage') ? `<button class="btn-link" data-act="pos-edit" data-id="${x.id}">编辑</button><button class="btn-link danger" data-act="pos-del" data-id="${x.id}">删除</button>` : ''}</td>
            </tr>`; }).join('')}</tbody>
        </table>
      </div>`;
  }
  function roleChecks(selected, attr) {
    return ROLE_APPS().map((a) => {
      const rs = ROLES.filter((r) => r.app === a.id);
      if (!rs.length) return '';
      return `<div class="mb12"><div class="small muted" style="font-weight:650">${a.icon} ${esc(a.name)}</div>${rs.map((r) => `<label class="check"><input type="checkbox" value="${r.id}" ${attr} ${selected.includes(r.id) ? 'checked' : ''}> ${esc(r.name)} <span class="muted small">${esc(r.desc || '')}</span></label>`).join('')}</div>`;
    }).join('');
  }
  function openPosForm(id) {
    const p = id ? posById(id) : null;
    openModal({
      title: p ? `编辑岗位 · ${p.name}` : '新建岗位',
      body: `
        <div class="grid2">
          <div class="field"><label>岗位名称<span class="req">*</span></label><input type="text" data-pf="name" value="${p ? esc(p.name) : ''}"></div>
          <div class="field"><label>岗位编码<span class="req">*</span></label><input type="text" data-pf="code" value="${p ? esc(p.code) : ''}" ${p ? 'disabled' : ''} placeholder="大写字母、数字、下划线"></div>
        </div>
        <div class="field"><label>默认角色</label><div class="hint mb12">获得该岗位的人（含通过部门获得）自动拥有以下角色；调整后立即生效</div>${roleChecks(p ? p.roles : [], 'data-pos-role')}</div>
        <div data-perr class="small" style="color:var(--danger)"></div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="pos-save" data-id="${id || ''}">保存</button>`,
    });
  }
  function savePos(id) {
    const name = $('[data-pf="name"]').value.trim();
    const code = $('[data-pf="code"]').value.trim();
    const roles = $$('[data-pos-role]').filter((x) => x.checked).map((x) => x.value);
    const err = (m) => { $('[data-perr]').textContent = m; };
    if (!name) return err('请填写岗位名称');
    if (POSITIONS.some((x) => x.name === name && x.id !== id)) return err('岗位名称已存在');
    if (id) {
      const p = posById(id);
      if (!guardSuper(() => Object.assign(p, { name, roles }))) return;
      audit('编辑岗位', name, `默认角色：${roles.map((r) => roleById(r).name).join('、') || '无'}`);
    } else {
      if (!/^[A-Z][A-Z0-9_]{1,31}$/.test(code)) return err('编码格式：大写字母开头，大写字母、数字、下划线');
      if (POSITIONS.some((x) => x.code === code)) return err('岗位编码已存在');
      POSITIONS.push({ id: uid('p'), name, code, roles });
      audit('新建岗位', name);
    }
    closeModal(); toast('岗位已保存'); render();
  }

  // 权限管理：岗位 / 角色 → 用户 / 组织
  const subjTag = (g) => `<span class="tag">${g.subj === 'user' ? '👤' : '🏢'} ${esc(subjName(g))}${g.sub ? '<span class="muted">（含下级）</span>' : ''}</span>`;
  const kindBadge = (k) => (k === 'pos' ? '<span class="badge badge-blue">岗位</span>' : '<span class="badge badge-purple">角色</span>');
  const grantScope = (g) => (g.subj === 'user' ? '本人' : g.sub ? '本部门及下级部门' : '仅本部门');
  function grantTable(gs, opt = {}) {
    if (!gs.length) return '<div class="empty">暂无授权</div>';
    return `<table class="table"><thead><tr>${opt.hideRef ? '' : '<th>授权内容</th>'}<th>授权对象</th><th>范围</th><th>覆盖人数</th><th>授权人 · 时间</th><th></th></tr></thead><tbody>${gs.map((g) => `
      <tr data-pick-row="${esc(refName(g) + subjName(g))}">
        ${opt.hideRef ? '' : `<td>${kindBadge(g.kind)} <b>${esc(refName(g))}</b>${g.kind === 'role' ? ` <span class="muted small">${esc(appName(roleById(g.ref).app))}</span>` : ''}</td>`}
        <td><a class="btn-link" style="padding:0" data-act="grant-goto" data-t="${g.subj}" data-id="${g.to}">${g.subj === 'user' ? '👤' : '🏢'} ${esc(subjName(g))}</a>${g.subj === 'dept' ? ` <span class="muted small">${esc(deptPath(g.to).slice(1, -1).join(' / '))}</span>` : ''}</td>
        <td class="small">${grantScope(g)}</td>
        <td>${holdersOf(g).length}</td>
        <td class="small muted nowrap">${esc(actorName(g.by))} · ${g.at}</td>
        <td class="nowrap" style="text-align:right">${can('grant:manage') ? `${g.subj === 'dept' ? `<button class="btn-link" data-act="grant-sub" data-id="${g.id}">${g.sub ? '改为仅本部门' : '改为含下级'}</button>` : ''}<button class="btn-link danger" data-act="grant-revoke" data-id="${g.id}">撤销</button>` : ''}</td>
      </tr>`).join('')}</tbody></table>`;
  }
  function viewGrants() {
    const secs = [can('grant:view') && ['role', '角色授权'], ['data', '数据授权'], can('position:view') && ['pos', '岗位管理']].filter(Boolean);
    const sec = secs.some(([k]) => k === state.grantSec) ? state.grantSec : secs[0][0];
    state.grantSec = sec;
    const sub = {
      role: '把岗位和角色授予用户或组织，决定能用应用的哪些功能。授予部门时可包含下级部门；用户的有效授权 = 本人 + 所在部门 + 上级部门（含下级）。',
      data: '按「数据编码 + 数据 Id」把只读 / 读写 / Owner 授给用户或组织，决定能动哪一条数据。应用创建数据时写入创建人的 Owner，之后只有 Owner 能修改这条数据的授权。',
      pos: '岗位统一维护，可关联默认角色；在「角色授权」中把岗位授给用户或组织，获得岗位的人自动拥有其默认角色。',
    }[sec];
    return `
      <div class="page-head">
        <div><h1>权限管理</h1><div class="sub">${sub}</div></div>
        <div class="grow"></div>
        ${sec === 'data' ? '<button class="btn" data-act="data-sim">🧪 模拟：在 Atlas 中创建 Agent</button>' : ''}
        ${sec === 'pos' && can('position:manage') ? '<button class="btn btn-primary" data-act="pos-new">＋ 新建岗位</button>' : ''}
      </div>
      <div class="ptabs" style="margin-top:0">${secs.map(([k, l]) => `<button class="${sec === k ? 'on' : ''}" data-act="grant-sec" data-s="${k}">${l}</button>`).join('')}</div>
      ${sec === 'role' ? roleGrantSection() : sec === 'data' ? dataSection() : posSection()}`;
  }
  function roleGrantSection() {
    const tab = state.grantTab;
    return `
      <div class="toolbar"><div class="tabs">
        <button class="${tab === 'subject' ? 'on' : ''}" data-act="grant-tab" data-t="subject">按对象授权</button>
        <button class="${tab === 'list' ? 'on' : ''}" data-act="grant-tab" data-t="list">全部授权<span class="n">${GRANTS.length}</span></button>
      </div></div>
      ${tab === 'subject' ? grantSubjectView() : grantListView()}`;
  }
  function grantSubjectView() {
    const mode = state.grantMode;
    const sel = state.grantSubj;
    const own = (t, id) => GRANTS.filter((g) => g.subj === t && g.to === id).length;
    const deptRows = (id, depth) => { const d = deptById(id); const n = own('dept', id); return `
      <div class="tree-node ${sel.t === 'dept' && sel.id === id ? 'on' : ''}" data-act="grant-subj" data-t="dept" data-id="${id}" style="padding-left:${10 + depth * 18}px">
        <span>🏢 ${esc(d.name)}</span><span class="grow"></span>${n ? `<span class="badge badge-gray">${n}</span>` : ''}
      </div>${childrenOf(id).map((c) => deptRows(c.id, depth + 1)).join('')}`; };
    return `
      <div class="org">
        <div class="card">
          <div class="card-h"><div class="tabs">
            <button class="${mode === 'dept' ? 'on' : ''}" data-act="grant-mode" data-m="dept">组织</button>
            <button class="${mode === 'user' ? 'on' : ''}" data-act="grant-mode" data-m="user">用户</button>
          </div></div>
          ${mode === 'dept' ? `<div class="tree">${deptRows('root', 0)}</div>` : `
            <div style="padding:10px 12px 0"><input class="input" style="width:100%" placeholder="搜索姓名或账号" data-pick-q></div>
            <div class="tree" style="max-height:620px;overflow:auto">${USERS.map((u) => { const n = own('user', u.id); return `
              <div class="tree-node ${sel.t === 'user' && sel.id === u.id ? 'on' : ''}" data-act="grant-subj" data-t="user" data-id="${u.id}" data-pick-row="${esc(u.name + u.account)}">
                ${avatar(u, 'sm')}<span>${esc(u.name)} <span class="muted small">${esc(deptById(u.dept).name)}</span></span><span class="grow"></span>${n ? `<span class="badge badge-gray">${n}</span>` : ''}
              </div>`; }).join('')}</div>`}
        </div>
        ${grantPanel(sel)}
      </div>`;
  }
  function grantPanel(sel) {
    const isDept = sel.t === 'dept';
    const obj = isDept ? deptById(sel.id) : userById(sel.id);
    if (!obj) return '<div class="card pad"><div class="empty">请选择组织或用户</div></div>';
    const own = GRANTS.filter((g) => g.subj === sel.t && g.to === sel.id);
    const inherited = (isDept ? deptGrants(sel.id) : grantsOf(obj)).filter((g) => !own.includes(g));
    const rows = (kind) => {
      const list = [...own, ...inherited].filter((g) => g.kind === kind);
      if (!list.length) return `<div class="empty">${kind === 'pos' ? '没有岗位' : '没有角色'}</div>`;
      return `<table class="table"><thead><tr><th>${kind === 'pos' ? '岗位' : '角色'}</th><th>${kind === 'pos' ? '默认角色' : '所属应用'}</th><th>来源</th><th>授权人 · 时间</th><th></th></tr></thead><tbody>${list.map((g) => { const mine = own.includes(g); return `
        <tr>
          <td><b>${esc(refName(g))}</b></td>
          <td>${kind === 'pos' ? (posById(g.ref).roles.map((r) => `<span class="tag">${esc(roleById(r).name)}</span>`).join(' ') || '<span class="muted small">无</span>') : `<span class="small">${esc(appName(roleById(g.ref).app))}</span>`}</td>
          <td class="small">${mine ? (isDept ? `本部门授权 · ${g.sub ? '含下级部门' : '仅本部门'}` : '直接授权') : `继承自 <a class="btn-link" style="padding:0" data-act="grant-subj" data-t="dept" data-id="${g.to}">🏢 ${esc(deptById(g.to).name)}</a>${g.sub ? '（含下级）' : ''}`}</td>
          <td class="small muted nowrap">${esc(actorName(g.by))} · ${g.at}</td>
          <td class="nowrap" style="text-align:right">${mine && can('grant:manage') ? `${isDept ? `<button class="btn-link" data-act="grant-sub" data-id="${g.id}">${g.sub ? '改为仅本部门' : '改为含下级'}</button>` : ''}<button class="btn-link danger" data-act="grant-revoke" data-id="${g.id}">撤销</button>` : mine ? '' : '<span class="muted small">在来源处管理</span>'}</td>
        </tr>`; }).join('')}</tbody></table>`;
    };
    const covered = isDept ? usersIn(sel.id, true).length : 0;
    const roles = isDept ? null : rolesOf(obj);
    return `
      <div class="stack">
        <div class="card pad">
          <div class="row wrap-row" style="gap:12px">
            ${isDept ? `<div><div class="dept-path">${deptPath(sel.id).map(esc).join(' / ')}</div><h2 style="font-size:19px;margin-top:2px">🏢 ${esc(obj.name)}</h2>
              <div class="small muted mt4">本部门 ${usersIn(sel.id, false).length} 人 · 含下级部门 ${covered} 人。授予本部门的岗位、角色，部门成员自动获得；选择「含下级」时下级部门成员也获得。</div></div>`
              : `<div class="row">${avatar(obj)}<div><div class="row"><h2 style="font-size:19px">${esc(obj.name)}</h2>${statusBadge(obj.status)}</div><div class="small muted mt4">${esc(obj.account)} · ${esc(deptPath(obj.dept).join(' / '))}</div></div></div>`}
            <div class="grow"></div>
            ${can('grant:manage') ? `<button class="btn" data-act="grant-add" data-kind="pos" data-t="${sel.t}" data-id="${sel.id}">＋ 授予岗位</button><button class="btn btn-primary" data-act="grant-add" data-kind="role" data-t="${sel.t}" data-id="${sel.id}">＋ 授予角色</button>` : ''}
          </div>
        </div>
        <div class="card"><div class="card-h"><h3>岗位</h3><span class="small muted">岗位的默认角色随岗位一起生效</span></div>${rows('pos')}</div>
        <div class="card"><div class="card-h"><h3>角色</h3></div>${rows('role')}</div>
        ${isDept ? '' : `
        <div class="card">
          <div class="card-h"><h3>有效角色</h3><span class="small muted">${roles.all.length} 个角色 · ${opsOf(obj).size} 项操作</span><div class="grow"></div>${can('user:view') ? `<button class="btn btn-sm" data-act="grant-user-detail" data-id="${obj.id}">查看操作明细 →</button>` : ''}</div>
          <div class="card-b">${roles.all.map((r) => `<div class="row small mt4" style="justify-content:space-between">${roleTag(roleById(r))}<span class="muted">来自：${roles.src.get(r).map(esc).join('、')}</span></div>`).join('') || '<span class="muted small">没有任何角色</span>'}</div>
        </div>`}
      </div>`;
  }
  function grantListView() {
    const k = state.grantKind, t = state.grantTo;
    const list = GRANTS.filter((g) => (k === 'all' || g.kind === k) && (t === 'all' || g.subj === t)).sort((a, b) => b.at.localeCompare(a.at));
    const btns = (cur, act, items) => `<div class="tabs">${items.map(([v, l]) => `<button class="${cur === v ? 'on' : ''}" data-act="${act}" data-v="${v}">${l}</button>`).join('')}</div>`;
    return `
      <div class="card">
        <div class="card-h" style="flex-wrap:wrap">
          ${btns(k, 'grant-kind', [['all', '全部'], ['pos', '岗位'], ['role', '角色']])}
          ${btns(t, 'grant-to-f', [['all', '全部对象'], ['user', '用户'], ['dept', '组织']])}
          <div class="grow"></div>
          <input class="input" style="width:220px" placeholder="搜索岗位、角色、用户、部门" data-pick-q>
        </div>
        ${grantTable(list)}
      </div>`;
  }
  function deptCheckRows(id, depth) {
    return `<label class="pick-item" style="padding-left:${8 + depth * 18}px"><input type="checkbox" value="${id}" data-grant-dept>🏢 ${esc(deptById(id).name)}</label>${childrenOf(id).map((c) => deptCheckRows(c.id, depth + 1)).join('')}`;
  }
  // 从对象出发：给某个用户 / 部门授予岗位或角色
  function openGrantItems(kind, t, id) {
    const has = new Set(GRANTS.filter((g) => g.kind === kind && g.subj === t && g.to === id).map((g) => g.ref));
    const name = t === 'dept' ? deptById(id).name : userById(id).name;
    const items = kind === 'pos'
      ? POSITIONS.map((x) => `<label class="check"><input type="checkbox" value="${x.id}" data-grant-ref ${has.has(x.id) ? 'checked disabled' : ''}> ${esc(x.name)} <span class="muted small">${x.roles.map((r) => esc(roleById(r).name)).join('、') || '无默认角色'}</span>${has.has(x.id) ? ' <span class="badge badge-gray">已授予</span>' : ''}</label>`).join('')
      : ROLE_APPS().map((a) => { const rs = ROLES.filter((r) => r.app === a.id); return rs.length ? `<div class="mb12"><div class="small muted" style="font-weight:650">${a.icon} ${esc(a.name)}</div>${rs.map((r) => `<label class="check"><input type="checkbox" value="${r.id}" data-grant-ref ${has.has(r.id) ? 'checked disabled' : ''}> ${esc(r.name)} <span class="muted small">${esc(r.desc || '')}</span>${has.has(r.id) ? ' <span class="badge badge-gray">已授予</span>' : ''}</label>`).join('')}</div>` : ''; }).join('');
    openModal({
      title: `授予${kind === 'pos' ? '岗位' : '角色'} · ${t === 'dept' ? '🏢' : '👤'} ${name}`,
      body: `<div style="max-height:380px;overflow:auto">${items}</div>
        ${t === 'dept' ? '<div class="callout callout-info mt12"><span>ℹ️</span><div><label class="check" style="margin:0"><input type="checkbox" data-grant-sub checked> 包含下级部门（下级部门的成员也获得）</label></div></div>' : ''}`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="grant-items-save" data-kind="${kind}" data-t="${t}" data-id="${id}">授权</button>`,
    });
  }
  // 从岗位 / 角色出发：授予多个用户和部门
  function openGrantTo(kind, ref) {
    const name = kind === 'pos' ? posById(ref).name : roleById(ref).name;
    openModal({
      title: `授权 · ${kind === 'pos' ? '岗位' : '角色'}「${name}」`,
      wide: true,
      body: `
        <div class="grid2" style="gap:16px;align-items:start">
          <div><div class="small" style="font-weight:650;margin-bottom:6px">组织</div><div class="pick-list">${deptCheckRows('root', 0)}</div>
            <label class="check mt8"><input type="checkbox" data-grant-sub checked> 包含下级部门</label></div>
          <div><div class="small" style="font-weight:650;margin-bottom:6px">用户</div>
            <input class="input" style="width:100%" placeholder="搜索姓名或账号" data-pick-q>
            <div class="pick-list mt8">${USERS.filter((u) => u.status !== 'disabled').map((u) => `<label class="pick-item" data-pick-row="${esc(u.name + u.account)}"><input type="checkbox" value="${u.id}" data-grant-user>${avatar(u, 'sm')}<b>${esc(u.name)}</b><span class="muted small">${esc(deptById(u.dept).name)}</span></label>`).join('')}</div></div>
        </div>
        <div class="small muted mt12">已授权的对象会跳过；部门已授权时按本次的「包含下级部门」更新范围。</div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="grant-to-save" data-kind="${kind}" data-ref="${ref}">授权</button>`,
    });
  }
  function addGrants(kind, refs, subjects, sub) {
    let added = 0, updated = 0;
    refs.forEach((ref) => subjects.forEach(({ t, id }) => {
      const ex = GRANTS.find((g) => g.kind === kind && g.ref === ref && g.subj === t && g.to === id);
      if (ex) { if (t === 'dept' && ex.sub !== sub) { ex.sub = sub; updated++; } return; }
      GRANTS.push({ id: uid('g'), kind, ref, subj: t, to: id, sub: t === 'dept' && sub, by: state.me, at: nowStr() });
      added++;
    }));
    const refText = refs.map((r) => (kind === 'pos' ? posById(r).name : roleById(r).name)).join('、');
    const subjText = subjects.map(({ t, id }) => (t === 'dept' ? deptById(id).name : userById(id).name)).join('、');
    if (added || updated) audit(kind === 'pos' ? '授予岗位' : '授予角色', refText, `→ ${subjText}${subjects.some((x) => x.t === 'dept') ? (sub ? '（含下级）' : '（仅本部门）') : ''}`);
    return { added, updated };
  }

  // 数据权限
  const dataBy = (code, id) => DATA.find((x) => x.code === code && x.id === id);
  const dtype = (code) => DATA_TYPES.find((t) => t.code === code);
  const aclOf = (code, id) => DACL.filter((a) => a.code === code && a.dataId === id);
  const aclHits = (u, a) => (a.subj === 'user' ? a.to === u.id : a.to === u.dept || (a.sub && deptChain(u.dept).includes(a.to)));
  // 用户对某条数据的有效权限：命中的授权里取最高级别
  function levelOf(u, code, id) {
    const hits = aclOf(code, id).filter((a) => aclHits(u, a));
    if (!hits.length) return { level: null, hits };
    const level = hits.reduce((m, a) => (LEVELS[a.level].rank > LEVELS[m].rank ? a.level : m), hits[0].level);
    return { level, hits };
  }
  const levelBadge = (l) => (l ? `<span class="badge badge-${LEVELS[l].cls}">${LEVELS[l].label}</span>` : '<span class="badge badge-gray">无权限</span>');
  const aclSubjName = (a) => (a.subj === 'user' ? userById(a.to).name : deptById(a.to).name);
  const aclSrc = (a) => (a.subj === 'user' ? '直接授权' : `部门：${deptById(a.to).name}${a.sub ? '（含下级）' : ''}`);
  const ownersOf = (code, id) => USERS.filter((u) => u.status !== 'disabled' && levelOf(u, code, id).level === 'owner');
  const isOwner = (code, id) => { const me = userById(state.me); return !!me && levelOf(me, code, id).level === 'owner'; };
  const dataHref = (x) => `#/grants/data/${encodeURIComponent(x.code)}/${encodeURIComponent(x.id)}`;
  // 修改后至少保留一名可用的 Owner，否则回滚
  function guardOwner(code, id, fn) {
    const snap = DACL.map((a) => ({ ...a }));
    fn();
    if (ownersOf(code, id).length) return true;
    DACL.splice(0, DACL.length, ...snap);
    toast('至少保留一名可用的 Owner，已取消', 'warn', 3200);
    return false;
  }

  function dataSection() {
    const me = userById(state.me);
    const tab = state.dataTab;
    const tabs = [['mine', '我管理的'], ['access', '我可访问的']];
    if (can('data:view')) tabs.push(['all', '全部数据']);
    const withLv = DATA.map((x) => ({ x, lv: levelOf(me, x.code, x.id).level }));
    const count = { mine: withLv.filter((r) => r.lv === 'owner').length, access: withLv.filter((r) => r.lv && r.lv !== 'owner').length, all: DATA.length };
    const cur = tabs.some(([k]) => k === tab) ? tab : 'mine';
    return `
      <div class="toolbar"><div class="tabs">${tabs.map(([k, l]) => `<button class="${cur === k ? 'on' : ''}" data-act="data-tab" data-t="${k}">${l}${count[k] != null ? `<span class="n">${count[k]}</span>` : ''}</button>`).join('')}</div></div>
      ${dataListView(withLv.filter((r) => (cur === 'mine' ? r.lv === 'owner' : cur === 'access' ? r.lv && r.lv !== 'owner' : true)), cur)}`;
  }
  function dataListView(rows, tab) {
    const codeF = state.dataCode;
    rows = rows.filter((r) => codeF === 'all' || r.x.code === codeF);
    const empty = { mine: '你还不是任何数据的 Owner。在 Atlas 中创建 Agent 后，你会自动成为它的 Owner。', access: '还没有人把数据授权给你（或你所在的部门）。', all: '暂无数据' }[tab];
    return `
      <div class="card">
        <div class="card-h">
          <select class="input" data-data-code style="width:auto"><option value="all">全部数据编码</option>${DATA_TYPES.map((t) => `<option value="${t.code}" ${codeF === t.code ? 'selected' : ''}>${t.code} · ${esc(t.name)}</option>`).join('')}</select>
          <div class="grow"></div>
          <input class="input" style="width:240px" placeholder="搜索数据名称或数据 Id" data-pick-q>
        </div>
        ${rows.length ? `<table class="table"><thead><tr><th>数据</th><th>数据编码</th><th>我的权限</th><th>Owner</th><th>授权</th><th>创建时间</th></tr></thead><tbody>${rows.map(({ x, lv }) => { const acl = aclOf(x.code, x.id); return `
          <tr class="click" data-nav="${dataHref(x)}" data-pick-row="${esc(x.name + x.id)}">
            <td><b>${esc(x.name)}</b><div class="mono muted" style="font-size:11.5px">${esc(x.id)}</div></td>
            <td><span class="tag mono">${esc(x.code)}</span> <span class="muted small">${esc(appName(dtype(x.code).app))}</span></td>
            <td>${levelBadge(lv)}</td>
            <td class="small">${ownersOf(x.code, x.id).map((u) => esc(u.name)).join('、') || '<span style="color:var(--danger)">无可用 Owner</span>'}</td>
            <td class="small muted">${acl.filter((a) => a.subj === 'user').length} 个用户 · ${acl.filter((a) => a.subj === 'dept').length} 个组织</td>
            <td class="small muted nowrap">${x.created}</td>
          </tr>`; }).join('')}</tbody></table>` : `<div class="empty">${empty}</div>`}
      </div>`;
  }
  // 应用详情 ·「数据编码」：数据编码属于应用，应用只能读写自己的数据编码
  function appDataTypesHtml(appId) {
    const list = DATA_TYPES.filter((t) => t.app === appId);
    return `
      <div class="card mb16">
        <div class="card-h"><h3>数据编码</h3><span class="small muted">${list.length} 个 · 数据编码全局唯一，应用调接口时用它标识数据类型</span><div class="grow"></div>
          ${can('app:manage') ? `<button class="btn btn-sm btn-primary" data-act="dtype-new" data-app="${appId}">＋ 登记数据编码</button>` : ''}</div>
        ${list.length ? `<table class="table"><thead><tr><th>数据编码</th><th>名称</th><th>数据条数</th><th>说明</th><th></th></tr></thead><tbody>${list.map((t) => { const n = DATA.filter((x) => x.code === t.code).length; return `
          <tr><td class="mono"><b>${esc(t.code)}</b></td><td>${esc(t.name)}</td><td>${n}</td><td class="small muted">${esc(t.desc || '')}</td>
          <td style="text-align:right">${can('app:manage') ? `<button class="btn-link danger" data-act="dtype-del" data-code="${esc(t.code)}">删除</button>` : ''}</td></tr>`; }).join('')}</tbody></table>` : '<div class="empty">还没有登记数据编码</div>'}
        <div class="card-b" style="border-top:1px solid var(--line-2)">
          <div class="small muted">权限级别（高级别包含低级别）：</div>
          <div class="row mt8" style="gap:16px;flex-wrap:wrap">
            <span>${levelBadge('read')} <span class="small">查看、使用</span></span>
            <span>${levelBadge('write')} <span class="small">只读 + 编辑</span></span>
            <span>${levelBadge('owner')} <span class="small">读写 + 删除 + 管理这条数据的授权</span></span>
          </div>
          <div class="small muted mt8">「只读 / 读写」在数据上能做什么由应用自己解释，用户中心只负责记录与计算级别。</div>
        </div>
      </div>
      ${list.length ? `<h3 style="font-size:14.5px;margin:4px 0 12px">接口（应用调用）</h3>${dataApiView(list[0].code)}` : ''}`;
  }
  function dataApiView(code) {
    const blk = (title, req, res, note) => `
      <div class="card mb16">
        <div class="card-h"><h3>${title}</h3>${note ? `<span class="small muted">${note}</span>` : ''}</div>
        <div class="card-b"><pre class="code">${esc(req)}</pre>${res ? `<div class="small muted mt8 mb12">响应</div><pre class="code">${esc(res)}</pre>` : ''}</div>
      </div>`;
    return `
      <div class="callout callout-info mb16"><span>ℹ️</span><div>应用用 App Key / Secret 换取接口令牌后调用（服务端到服务端）。应用只能读写自己登记的数据编码。用户在用户中心或应用里修改授权时，服务端会校验操作人对该数据是否为 Owner。</div></div>
      ${blk('① 创建数据时写入 Owner', `POST /open/v1/data-permissions
{
  "dataCode": "${code}",
  "dataId": "xxx_7f3a21",
  "dataName": "示例数据",          // 可选，用于在用户中心展示
  "permission": "OWNER",              // READ | WRITE | OWNER
  "subjectType": "USER",              // USER | DEPT
  "subjectId": "zhang.ming",
  "includeSub": false                 // 仅 DEPT 有效：是否包含下级部门
}`, `{ "id": "da_10293", "created": true }`, '应用创建数据后调用')}
      ${blk('② 校验某个用户的权限', `GET /open/v1/data-permissions/check?dataCode=${code}&dataId=xxx_7f3a21&user=chen.yun`, `{ "permission": "WRITE", "sources": ["USER:chen.yun", "DEPT:研发中心(含下级)"] }`, '打开 / 编辑 / 删除数据前调用；无权限返回 "NONE"')}
      ${blk('③ 列出用户可访问的数据', `GET /open/v1/data-permissions/accessible?dataCode=${code}&user=zhang.ming&min=READ`, `{ "items": [ { "dataId": "xxx_7f3a21", "permission": "OWNER" }, { "dataId": "xxx_21bc90", "permission": "WRITE" } ] }`, '应用的列表页按此过滤')}
      ${blk('④ 数据删除后清理授权', `DELETE /open/v1/data-permissions?dataCode=${code}&dataId=xxx_7f3a21`, '', '删除这条数据的全部授权')}`;
  }
  function viewDataItem(code, id) {
    const x = dataBy(code, id);
    if (!x) return '<div class="empty">数据不存在</div>';
    const me = userById(state.me);
    const mine = levelOf(me, code, id);
    const owner = mine.level === 'owner';
    const acl = aclOf(code, id).sort((a, b) => LEVELS[b.level].rank - LEVELS[a.level].rank);
    const holders = USERS.map((u) => ({ u, ...levelOf(u, code, id) })).filter((r) => r.level).sort((a, b) => LEVELS[b.level].rank - LEVELS[a.level].rank);
    return `
      <div class="page-head"><div class="grow"></div><button class="btn" data-act="grant-sec" data-s="data" data-back="1">← 返回数据授权</button></div>
      <div class="stack">
        <div class="card pad">
          <div class="row wrap-row" style="gap:12px">
            <div>
              <div class="row"><span class="tag mono">${esc(code)}</span><span class="muted small">${esc(dtype(code).name)} · ${esc(appName(dtype(code).app))}</span></div>
              <h2 style="font-size:20px;margin-top:6px">${esc(x.name)}</h2>
              <div class="small muted mt4">数据 Id <span class="mono">${esc(x.id)}</span> · 创建于 ${x.created}</div>
            </div>
            <div class="grow"></div>
            <div style="text-align:right">
              <div class="small muted">我的权限</div>
              <div class="mt4">${levelBadge(mine.level)}</div>
              ${mine.hits.length ? `<div class="small muted mt4">来自：${mine.hits.map((a) => `${esc(aclSrc(a))}·${LEVELS[a.level].label}`).join('、')}</div>` : ''}
            </div>
          </div>
          ${owner ? '' : `<div class="callout callout-info mt12"><span>🔒</span><div>只有 Owner 可以修改这条数据的授权。${mine.level ? `你当前是「${LEVELS[mine.level].label}」。` : can('data:view') ? '你是以「查看全部数据权限」身份浏览，不能修改。' : ''}</div></div>`}
        </div>
        <div class="card">
          <div class="card-h"><h3>授权</h3><span class="small muted">${acl.length} 条 · 同一对象只保留一条，级别可直接修改</span><div class="grow"></div>
            ${owner ? `<button class="btn btn-sm btn-primary" data-act="dacl-add" data-code="${esc(code)}" data-id="${esc(id)}">＋ 添加授权</button>` : ''}</div>
          ${acl.length ? `<table class="table"><thead><tr><th>授权对象</th><th>权限</th><th>范围</th><th>来源</th><th>时间</th><th></th></tr></thead><tbody>${acl.map((a) => `
            <tr>
              <td>${a.subj === 'user' ? `<span class="row">${avatar(userById(a.to), 'sm')}<b>${esc(userById(a.to).name)}</b><span class="muted small">${esc(userById(a.to).account)}</span>${userById(a.to).status === 'disabled' ? statusBadge('disabled') : ''}</span>` : `<b>🏢 ${esc(deptById(a.to).name)}</b> <span class="muted small">${esc(deptPath(a.to).slice(1, -1).join(' / '))}</span>`}</td>
              <td>${owner ? `<select class="input" style="width:auto;padding:3px 8px" data-dacl-level="${a.id}">${Object.entries(LEVELS).map(([k, v]) => `<option value="${k}" ${a.level === k ? 'selected' : ''}>${v.label}</option>`).join('')}</select>` : levelBadge(a.level)}</td>
              <td class="small">${a.subj === 'user' ? '本人' : a.sub ? '本部门及下级部门' : '仅本部门'}</td>
              <td class="small muted">${a.via === 'api' ? `接口写入 · ${esc(appName(dtype(code).app))}` : `${esc(actorName(a.by))} 授权`}</td>
              <td class="small muted nowrap">${a.at}</td>
              <td class="nowrap" style="text-align:right">${owner ? `${a.subj === 'dept' ? `<button class="btn-link" data-act="dacl-sub" data-id="${a.id}">${a.sub ? '改为仅本部门' : '改为含下级'}</button>` : ''}<button class="btn-link danger" data-act="dacl-del" data-id="${a.id}">移除</button>` : ''}</td>
            </tr>`).join('')}</tbody></table>` : '<div class="empty">暂无授权</div>'}
        </div>
        <div class="card">
          <div class="card-h"><h3>实际可访问的人</h3><span class="small muted">${holders.length} 人 · 按授权自动计算，多个来源取最高级别</span></div>
          <table class="table"><thead><tr><th>用户</th><th>部门</th><th>有效权限</th><th>来源</th></tr></thead><tbody>${holders.map((r) => `
            <tr><td><span class="row">${avatar(r.u, 'sm')}<b>${esc(r.u.name)}</b>${r.u.status === 'disabled' ? statusBadge('disabled') : ''}</span></td><td class="small">${esc(deptById(r.u.dept).name)}</td><td>${levelBadge(r.level)}</td>
            <td class="small muted">${r.hits.map((a) => `${esc(aclSrc(a))}·${LEVELS[a.level].label}`).join('、')}</td></tr>`).join('')}</tbody></table>
        </div>
      </div>`;
  }
  function openDaclAdd(code, id) {
    const x = dataBy(code, id);
    openModal({
      title: `添加授权 · ${x.name}`,
      wide: true,
      body: `
        <div class="field"><label>权限</label><div class="row" style="gap:18px">${Object.entries(LEVELS).map(([k, v]) => `<label class="check" style="margin:0"><input type="radio" name="dlv" value="${k}" data-dacl-lv ${k === 'read' ? 'checked' : ''}> ${v.label}</label>`).join('')}</div>
          <div class="hint">授予 Owner 后，对方也能管理这条数据的授权</div></div>
        <div class="grid2" style="gap:16px;align-items:start">
          <div><div class="small" style="font-weight:650;margin-bottom:6px">组织</div><div class="pick-list">${deptCheckRows('root', 0)}</div>
            <label class="check mt8"><input type="checkbox" data-grant-sub checked> 包含下级部门</label></div>
          <div><div class="small" style="font-weight:650;margin-bottom:6px">用户</div>
            <input class="input" style="width:100%" placeholder="搜索姓名或账号" data-pick-q>
            <div class="pick-list mt8">${USERS.filter((u) => u.status !== 'disabled').map((u) => `<label class="pick-item" data-pick-row="${esc(u.name + u.account)}"><input type="checkbox" value="${u.id}" data-grant-user>${avatar(u, 'sm')}<b>${esc(u.name)}</b><span class="muted small">${esc(deptById(u.dept).name)}</span></label>`).join('')}</div></div>
        </div>
        <div class="small muted mt12">对象已有授权时，按本次选择更新级别（和部门范围）。</div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="dacl-save" data-code="${esc(code)}" data-id="${esc(id)}">授权</button>`,
    });
  }
  function openDataSim() {
    const me = userById(state.me);
    openModal({
      title: '模拟：在 Atlas 中创建 Agent',
      body: `
        <div class="field"><label>Agent 名称</label><input type="text" data-sim-name value="我的新 Agent"></div>
        <div class="small muted mb12">Atlas 保存 Agent 后，用自己的客户端凭证调用用户中心接口：</div>
        <pre class="code" data-sim-preview></pre>
        <div class="small muted mt8">创建人 ${esc(me.name)} 将成为这个 Agent 的 Owner，之后可以把只读、读写或 Owner 授给组织和其他人。</div>`,
      footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="data-sim-go">创建并写入 Owner</button>`,
    });
    state._simId = `agt_${Math.random().toString(16).slice(2, 8)}`;
    simPreview();
  }
  function simPreview() {
    const el = $('[data-sim-preview]'); if (!el) return;
    const me = userById(state.me);
    el.textContent = `POST /open/v1/data-permissions\n${JSON.stringify({ dataCode: 'Agent', dataId: state._simId, dataName: ($('[data-sim-name]') || {}).value || '', permission: 'OWNER', subjectType: 'USER', subjectId: me.account }, null, 2)}`;
  }

  // 用户详情 ·「权限」标签（只读，授权在权限管理中进行）
  function userPermHtml(u) {
    const gs = grantsOf(u);
    const roles = rolesOf(u);
    const ops = opsOf(u);
    const byApp = ROLE_APPS().map((a) => ({ a, ops: OPS.filter((o) => o.app === a.id && ops.has(o.code)) })).filter((x) => x.ops.length);
    const srcOf = (code) => roles.all.map(roleById).filter((r) => r.ops.includes(code)).map((r) => r.name);
    return `
      <div class="grid2" style="gap:16px;align-items:start">
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3>生效的授权</h3><span class="small muted">${gs.length} 条</span><div class="grow"></div><button class="btn btn-sm" data-act="grant-goto" data-t="user" data-id="${u.id}">在权限管理中授权 →</button></div>
            ${gs.length ? `<table class="table"><tbody>${gs.map((g) => `<tr><td>${kindBadge(g.kind)} <b>${esc(refName(g))}</b></td><td class="small muted">${esc(grantSrc(g))}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">没有任何授权</div>'}
          </div>
          <div class="card">
            <div class="card-h"><h3>有效角色</h3><span class="small muted">授予的角色 ∪ 岗位的默认角色</span></div>
            <div class="card-b">${roles.all.map((r) => `<div class="row small mt4" style="justify-content:space-between">${roleTag(roleById(r))}<span class="muted">${roles.src.get(r).map(esc).join('、')}</span></div>`).join('') || '<span class="muted small">无</span>'}</div>
          </div>
          <div class="card">
            <div class="card-h"><h3>可见菜单</h3><span class="small muted">角色绑定的菜单 ∪ 公共菜单</span></div>
            <div class="card-b">${[UC_APP, ...APPS.filter((x) => x.status === 'active')].map((x) => { const vis = visibleMenus(u, x.id); const ms = menusOfApp(x.id).filter((m) => m.type === 'menu' && vis.has(m.id)); return `<div class="row small mt4" style="align-items:flex-start"><span style="width:110px;flex:none">${x.icon} ${esc(x.name)}</span><span>${ms.map((m) => `<span class="tag" title="来自：${esc(menuSrc(u, m).join('、'))}">${esc(m.name)}</span>`).join(' ') || '<span class="muted">无</span>'}</span></div>`; }).join('')}</div>
          </div>
          <div class="card">
            <div class="card-h"><h3>数据权限</h3><span class="small muted">按数据逐条授权</span></div>
            ${(() => { const rows = DATA.map((x) => ({ x, lv: levelOf(u, x.code, x.id).level })).filter((r) => r.lv); return rows.length ? `<table class="table"><tbody>${rows.map((r) => `<tr><td><span class="tag mono">${esc(r.x.code)}</span> <b>${esc(r.x.name)}</b></td><td>${levelBadge(r.lv)}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">没有数据权限</div>'; })()}
          </div>
          <div class="card">
            <div class="card-h"><h3>接入应用拿到的权限</h3></div>
            <div class="card-b">
              <div class="small muted mb12">接入应用调用开放接口 <span class="mono">GET /open/v1/users/{id}/authz</span>，拿到属于自己的操作码和可见菜单树：菜单决定导航，操作码决定按钮。</div>
              <pre class="code">${esc(JSON.stringify({ app: 'teamflow', user: u.account, can_access: appsOf(u).some((x) => x.id === 'teamflow'), permissions: OPS.filter((o) => o.app === 'teamflow' && ops.has(o.code)).map((o) => o.code), menus: menuTree(u, 'teamflow') }, null, 2))}</pre>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h3>有效操作</h3><span class="small muted">${ops.size} 项</span></div>
          <div class="card-b">${byApp.map(({ a, ops: list }) => `
            <div class="mb16"><div style="font-weight:650">${a.icon} ${esc(a.name)} <span class="muted small">${list.length} 项</span></div>
              ${list.map((o) => `<div class="row small mt4" style="justify-content:space-between"><span>${esc(o.name)} <span class="mono muted" style="font-size:11px">${o.code}</span></span><span class="muted" style="font-size:11.5px">来自：${srcOf(o.code).map(esc).join('、')}</span></div>`).join('')}
            </div>`).join('') || '<div class="empty">没有任何操作权限</div>'}</div>
        </div>
      </div>`;
  }

  // 应用详情 ·「操作目录」
  const opsEditable = (appId) => appId !== 'uc' && can('app:manage');
  function appOpsHtml(a) {
    const ops = OPS.filter((o) => o.app === a.id);
    return `
      <div class="card">
        <div class="card-h"><h3>操作目录</h3><span class="small muted">${ops.length} 项 · 该应用可被授权的操作，角色从这里勾选${a.id === 'uc' ? ' · 内置应用的操作随版本发布，不能在这里修改' : ''}</span><div class="grow"></div>
          ${opsEditable(a.id) ? `<button class="btn btn-sm btn-primary" data-act="op-new" data-app="${a.id}">＋ 新增操作</button>` : ''}</div>
        ${ops.length ? `<table class="table"><thead><tr><th>模块</th><th>操作</th><th>操作码</th><th>使用该操作的角色</th><th></th></tr></thead><tbody>${ops.map((o) => { const rs = ROLES.filter((r) => r.ops.includes(o.code)); return `
          <tr><td>${esc(o.module)}</td><td>${esc(o.name)}</td><td class="mono">${o.code}</td><td>${rs.map((r) => `<span class="tag">${esc(r.name)}</span>`).join(' ') || '<span class="muted small">无</span>'}</td>
          <td style="text-align:right">${opsEditable(a.id) ? `<button class="btn-link danger" data-act="op-del" data-code="${o.code}">删除</button>` : ''}</td></tr>`; }).join('')}</tbody></table>` : '<div class="empty">还没有登记操作</div>'}
        <div class="card-b" style="border-top:1px solid var(--line-2)">
          <div class="small muted mb12">前端按操作码控制权限（示例）：</div>
          <pre class="code">// 用户登录应用后，应用后端调用开放接口 /open/v1/users/{id}/authz 取得本应用的操作码
const can = (op) =&gt; permissions.includes(op);
{can('${(ops[0] || { code: 'xxx:view' }).code}') &amp;&amp; &lt;Menu.Item&gt;…&lt;/Menu.Item&gt;}   // 没有权限：菜单不显示
{can('${(ops[1] || ops[0] || { code: 'xxx:edit' }).code}') &amp;&amp; &lt;Button&gt;…&lt;/Button&gt;}        // 没有权限：按钮不显示
// 后端接口同样按操作码校验，前端控制只负责「看不到」</pre>
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------
     8. 应用接入
     --------------------------------------------------------- */
  const scopeText = (a) => (a.scope.all ? '全员' : a.scope.depts.map((d) => deptById(d) ? deptById(d).name : '').filter(Boolean).join('、') || '未设置（无人可访问）');
  function viewApps() {
    return `
      <div class="page-head">
        <div><h1>应用接入</h1><div class="sub">接入的系统在服务端用 App Key / Secret 调用开放接口，获取用户、组织与本应用的权限；按部门控制哪些人可以访问。</div></div>
        <div class="grow"></div>
        ${can('app:manage') ? '<button class="btn btn-primary" data-act="app-new">＋ 接入应用</button>' : ''}
      </div>
      <div class="app-grid">
        <div class="app-card" data-nav="#/apps/uc">
          <div class="row"><span class="app-ico">${UC_APP.icon}</span><div><b style="font-size:15px">${UC_APP.name}</b><div class="small muted">本系统 · 内置应用</div></div><div class="grow"></div><span class="badge badge-purple">内置</span></div>
          <div class="kv mt12 small"><span class="k">操作</span><span>${OPS.filter((o) => o.app === 'uc').length} 项</span><span class="k">角色</span><span>${ROLES.filter((r) => r.app === 'uc').length} 个</span><span class="k">菜单</span><span>${MENUS.filter((m) => m.app === 'uc' && m.type === 'menu').length} 个</span></div>
        </div>
        ${APPS.map((a) => `
          <div class="app-card" data-nav="#/apps/${a.id}">
            <div class="row"><span class="app-ico">${a.icon}</span><div><b style="font-size:15px">${esc(a.name)}</b><div class="small muted">${esc(a.desc)}</div></div><div class="grow"></div>${a.status === 'active' ? '<span class="badge badge-green">已启用</span>' : '<span class="badge badge-gray">已停用</span>'}</div>
            <div class="kv mt12 small"><span class="k">App Key</span><span class="mono">${a.clientId}</span><span class="k">可访问范围</span><span>${esc(scopeText(a))}</span><span class="k">角色 / 数据编码</span><span>${ROLES.filter((r) => r.app === a.id).length} 个角色 · ${DATA_TYPES.filter((t) => t.app === a.id).length} 个数据编码</span></div>
          </div>`).join('')}
      </div>`;
  }
  function scopeTreeHtml(a) {
    const rows = [];
    const walk = (id, depth) => {
      const d = deptById(id);
      rows.push(`<label class="check" style="padding-left:${depth * 18}px"><input type="checkbox" value="${id}" data-scope-dept ${a.scope.depts.includes(id) ? 'checked' : ''} ${a.scope.all ? 'disabled' : ''}>${esc(d.name)}</label>`);
      childrenOf(id).forEach((c) => walk(c.id, depth + 1));
    };
    walk('root', 0);
    return rows.join('');
  }
  // 应用详情：接入配置 / 操作目录 / 角色 / 数据编码（角色与数据编码都属于应用）
  function viewApp(id) {
    const isUc = id === 'uc';
    const a = isUc ? { ...UC_APP, desc: '本系统 · 内置应用', status: 'active' } : appById(id);
    if (!a) return '<div class="empty">应用不存在</div>';
    const tabs = [
      !isUc && can('app:manage') && ['conf', '接入配置'],
      ['ops', `操作目录（${OPS.filter((o) => o.app === id).length}）`],
      ['menus', `菜单（${MENUS.filter((m) => m.app === id && m.type === 'menu').length}）`],
      can('role:view') && ['roles', `角色（${ROLES.filter((r) => r.app === id).length}）`],
      !isUc && can('app:manage') && ['dtypes', `数据编码（${DATA_TYPES.filter((t) => t.app === id).length}）`],
    ].filter(Boolean);
    const tab = tabs.some(([k]) => k === state.appTab) ? state.appTab : tabs[0][0];
    return `
      <div class="page-head">
        <div class="row"><span class="app-ico">${a.icon}</span><div><h1>${esc(a.name)}</h1><div class="sub">${esc(a.desc)}${a.created ? ` · 接入于 ${a.created}` : ''}</div></div></div>
        <div class="grow"></div>
        ${!isUc && can('app:manage') ? (a.status === 'active' ? `<button class="btn btn-danger" data-act="app-toggle" data-id="${a.id}">停用</button>` : `<button class="btn" data-act="app-toggle" data-id="${a.id}">启用</button>`) : ''}
        <button class="btn" data-nav="#/apps">← 返回应用列表</button>
      </div>
      ${a.status === 'disabled' ? '<div class="callout callout-warn mb16"><span>⚠️</span><div>应用已停用：开放接口拒绝该应用的调用，该应用无法再获取用户与权限。</div></div>' : ''}
      <div class="ptabs" style="margin-top:0">${tabs.map(([k, l]) => `<button class="${tab === k ? 'on' : ''}" data-act="app-tab" data-t="${k}">${l}</button>`).join('')}</div>
      ${tab === 'conf' ? appConfHtml(a) : tab === 'ops' ? appOpsHtml(a) : tab === 'menus' ? appMenusHtml(a) : tab === 'roles' ? appRolesHtml(id) : appDataTypesHtml(id)}`;
  }
  function appConfHtml(a) {
    const users = USERS.filter((u) => u.status !== 'disabled' && appsOf(u).includes(a)).length;
    return `
      <div class="grid2" style="gap:16px;align-items:start">
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3>应用凭据</h3><span class="badge badge-blue">服务端调用开放接口</span></div>
            <div class="card-b">
              <div class="field"><label>App Key</label><div class="secret"><code>${a.clientId}</code><button class="btn btn-sm" data-act="copy" data-text="${a.clientId}">复制</button></div></div>
              <div class="field"><label>App Secret</label><div class="secret"><code>••••••••••••${a.secretTail}</code><button class="btn btn-sm" data-act="app-secret" data-id="${a.id}">重新生成</button></div><div class="hint">Secret 只在生成时显示一次；只能保存在应用后端，不能下发到浏览器。重新生成后旧 Secret 立即失效。</div></div>
              <div class="field"><label>开放接口</label>
                <div class="kv small"><span class="k">Base URL</span><span class="mono">https://uc.xinghai.com/open/v1</span><span class="k">获取令牌</span><span class="mono">POST /auth/token</span><span class="k">主要接口</span><span>用户与部门查询、本应用授权快照（can_access / 角色 / 操作码 / 菜单）、实时校验、数据权限</span></div>
              </div>
            </div>
          </div>
          <div class="callout callout-info"><span>ℹ️</span><div>统一登录（SSO）暂不提供：应用自行处理登录，按<b>账号</b>与用户中心对齐身份。后续引入 Keycloak 作为统一登录，届时回调地址等在 Keycloak 中配置。</div></div>
        </div>
        <div class="card">
          <div class="card-h"><h3>可访问范围</h3><span class="small muted">当前 ${users} 人可访问</span></div>
          <div class="card-b">
            <label class="check"><input type="radio" name="scope" value="all" data-scope-all ${a.scope.all ? 'checked' : ''}> 全员</label>
            <label class="check"><input type="radio" name="scope" value="depts" data-scope-all ${a.scope.all ? '' : 'checked'}> 指定部门（含其子部门）</label>
            <div class="scope-tree mt8">${scopeTreeHtml(a)}</div>
            <div class="row mt12"><div class="grow"></div><button class="btn btn-sm btn-primary" data-act="scope-save" data-id="${a.id}">保存范围</button></div>
          </div>
        </div>
      </div>`;
  }
  function openNewApp() {
    openModal({
      title: '接入应用',
      body: `
        <div class="field"><label>应用名称<span class="req">*</span></label><input type="text" data-an placeholder="例如：报销系统"></div>
        <div class="field"><label>说明</label><input type="text" data-ad></div>
        <div data-aerr class="small" style="color:var(--danger)"></div>`,
      footer: '<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="app-create">创建</button>',
    });
  }
  function randHex(n) { return Array.from({ length: n }, () => '0123456789abcdef'[Math.floor(Math.random() * 16)]).join(''); }
  function showSecret(a, title) {
    const secret = `${randHex(28)}${randHex(4)}`;
    a.secretTail = secret.slice(-4);
    openModal({
      title,
      body: `
        <div class="kv"><span class="k">App Key</span><span class="mono">${a.clientId}</span><span class="k">App Secret</span><span><span class="secret"><code>${secret}</code><button class="btn btn-sm" data-act="copy" data-text="${secret}">复制</button></span></span></div>
        <div class="callout callout-warn mt12"><span>⚠️</span><div>Secret <b>只显示这一次</b>，关闭后无法再查看，请立即配置到「${esc(a.name)}」。</div></div>`,
      footer: '<button class="btn btn-primary" data-close-modal>我已保存</button>',
    });
  }

  /* ---------------------------------------------------------
     9. 安全策略
     --------------------------------------------------------- */
  function viewSecurity() {
    const P = POLICY;
    return `
      <div class="page-head"><div><h1>安全策略</h1><div class="sub">对所有账号与所有接入应用生效。</div></div></div>
      <div class="grid2" style="gap:16px;align-items:start">
        <div class="card">
          <div class="card-h"><h3>密码策略</h3></div>
          <div class="card-b">
            <div class="field"><label>最小长度</label><input type="number" min="6" max="32" data-p="minLen" value="${P.minLen}"><div class="hint">6–32</div></div>
            <label class="check"><input type="checkbox" data-p="needUpper" ${P.needUpper ? 'checked' : ''}> 必须包含大写字母</label>
            <label class="check"><input type="checkbox" data-p="needDigit" ${P.needDigit ? 'checked' : ''}> 必须包含数字</label>
            <label class="check"><input type="checkbox" data-p="needSymbol" ${P.needSymbol ? 'checked' : ''}> 必须包含特殊字符</label>
            <div class="field mt12"><label>密码有效期（天）</label><input type="number" min="0" max="365" data-p="expireDays" value="${P.expireDays}"><div class="hint">0 = 永不过期；到期后登录时要求修改</div></div>
          </div>
        </div>
        <div class="card">
          <div class="card-h"><h3>登录保护</h3></div>
          <div class="card-b">
            <div class="field"><label>连续失败几次后锁定</label><input type="number" min="3" max="20" data-p="lockAfter" value="${P.lockAfter}"></div>
            <div class="field"><label>锁定时长（分钟）</label><input type="number" min="5" max="1440" data-p="lockMinutes" value="${P.lockMinutes}"><div class="hint">到期自动解锁；管理员也可手动解锁</div></div>
            <div class="field"><label>登录会话有效期（小时）</label><input type="number" min="1" max="720" data-p="sessionHours" value="${P.sessionHours}"><div class="hint">超时后需要重新登录（各接入应用同步失效）</div></div>
          </div>
        </div>
      </div>
      <div class="row mt16"><div class="grow"></div><button class="btn btn-primary" data-act="policy-save">保存策略</button></div>`;
  }
  function savePolicy() {
    const num = (k, lo, hi) => { const v = Number($(`[data-p="${k}"]`).value); return Number.isInteger(v) && v >= lo && v <= hi ? v : null; };
    const next = { minLen: num('minLen', 6, 32), expireDays: num('expireDays', 0, 365), lockAfter: num('lockAfter', 3, 20), lockMinutes: num('lockMinutes', 5, 1440), sessionHours: num('sessionHours', 1, 720) };
    const bad = Object.entries(next).filter(([, v]) => v === null).map(([k]) => k);
    if (bad.length) { toast('有字段超出允许范围，请检查', 'warn'); return; }
    ['needUpper', 'needDigit', 'needSymbol'].forEach((k) => { next[k] = $(`[data-p="${k}"]`).checked; });
    Object.assign(POLICY, next);
    audit('修改安全策略', '密码与登录保护', `最小长度 ${POLICY.minLen}，锁定阈值 ${POLICY.lockAfter} 次`);
    toast('安全策略已保存，新密码与下次登录按新策略执行');
  }

  /* ---------------------------------------------------------
     10. 审计日志
     --------------------------------------------------------- */
  function viewAudit() {
    const q = state.auditQ.trim();
    const op = state.auditTab === 'op';
    const rows = op
      ? AUDIT.filter((a) => !q || [actorName(a.actor), a.action, a.target, a.detail].join(' ').includes(q))
      : LOGIN_LOGS.filter((l) => !q || [actorName(l.user), l.app, l.ip].join(' ').includes(q));
    return `
      <div class="page-head"><div><h1>审计日志</h1><div class="sub">所有管理操作与登录行为都会记录，只追加、不可修改。</div></div></div>
      <div class="toolbar">
        <div class="tabs"><button class="${op ? 'on' : ''}" data-act="audit-tab" data-t="op">操作日志</button><button class="${op ? '' : 'on'}" data-act="audit-tab" data-t="login">登录日志</button></div>
        <div class="grow"></div>
        <input class="input" style="width:260px" placeholder="搜索人员 / 动作 / 对象" data-audit-q value="${esc(state.auditQ)}">
      </div>
      <div class="card">
        <table class="table">
          ${op ? `<thead><tr><th>时间</th><th>操作人</th><th>动作</th><th>对象</th><th>说明</th></tr></thead>
            <tbody>${rows.map((a) => `<tr><td class="nowrap">${a.time}</td><td>${esc(actorName(a.actor))}</td><td><span class="tag">${esc(a.action)}</span></td><td>${esc(a.target)}</td><td class="small muted">${esc(a.detail)}</td></tr>`).join('') || '<tr><td colspan="5"><div class="empty">无记录</div></td></tr>'}</tbody>`
          : `<thead><tr><th>时间</th><th>用户</th><th>应用</th><th>IP</th><th>结果</th></tr></thead>
            <tbody>${rows.map((l) => `<tr><td class="nowrap">${l.time}</td><td>${esc(actorName(l.user))}</td><td>${esc(l.app)}</td><td class="mono">${l.ip}</td><td>${l.ok ? '<span class="badge badge-green">成功</span>' : `<span class="badge badge-red">失败 · ${esc(l.reason || '')}</span>`}</td></tr>`).join('') || '<tr><td colspan="5"><div class="empty">无记录</div></td></tr>'}</tbody>`}
        </table>
      </div>`;
  }

  /* ---------------------------------------------------------
     11. 个人中心
     --------------------------------------------------------- */
  function viewMe() {
    const u = userById(state.me);
    return `
      <div class="page-head"><div><h1>个人中心</h1><div class="sub">查看个人信息、修改密码；可访问的应用列表。</div></div></div>
      <div class="grid2" style="gap:16px;align-items:start">
        <div class="card">
          <div class="card-h"><h3>个人信息</h3></div>
          <div class="card-b">
            <div class="profile mb16">${avatar(u, 'lg')}<div><b style="font-size:17px">${esc(u.name)}</b><div class="small muted">${esc(u.account)} · ${esc(deptPath(u.dept).join(' / '))}</div></div></div>
            <div class="field"><label>邮箱</label><input type="email" data-me="email" value="${esc(u.email)}"></div>
            <div class="field"><label>手机</label><input type="text" data-me="phone" value="${esc(u.phone)}"></div>
            <div class="hint small muted mb12">姓名、部门、岗位由管理员维护。</div>
            <button class="btn btn-primary btn-sm" data-act="me-save">保存</button>
          </div>
        </div>
        <div class="stack">
          <div class="card">
            <div class="card-h"><h3>修改密码</h3></div>
            <div class="card-b">
              <div class="field"><label>当前密码</label><input type="password" data-pw="old"></div>
              <div class="field"><label>新密码</label><input type="password" data-pw="new"><div class="hint">要求：${policyText()}</div></div>
              <div class="field"><label>确认新密码</label><input type="password" data-pw="confirm"><div class="err" data-pw-err></div></div>
              <button class="btn btn-primary btn-sm" data-act="me-pwd">修改密码</button>
            </div>
          </div>
          <div class="card">
            <div class="card-h"><h3>我可以访问的应用</h3></div>
            <div class="card-b">${appsOf(u).filter((a) => a.status === 'active').map((a) => `<div class="row mb12"><span class="app-ico" style="width:32px;height:32px;font-size:16px">${a.icon}</span><b>${esc(a.name)}</b><span class="muted small">${esc(a.desc)}</span></div>`).join('') || '<div class="empty">没有可访问的应用</div>'}</div>
          </div>
        </div>
      </div>`;
  }

  /* ---------------------------------------------------------
     12. 路由与渲染
     --------------------------------------------------------- */
  function crumbs(seg) {
    const map = { org: '组织架构', users: '用户管理', grants: '权限管理', apps: '应用接入', security: '安全策略', audit: '审计日志', me: '个人中心' };
    const out = [map[seg[0]] || '组织架构'];
    if (seg[0] === 'users' && seg[1] && userById(seg[1])) out.push(userById(seg[1]).name);
    if (seg[0] === 'apps' && seg[1]) out.push(appName(seg[1]));
    if (seg[0] === 'grants') {
      out.push({ role: '角色授权', data: '数据授权', pos: '岗位管理' }[seg[1] === 'data' ? 'data' : state.grantSec] || '角色授权');
      const x = seg[1] === 'data' && dataBy(decodeURIComponent(seg[2] || ''), decodeURIComponent(seg[3] || ''));
      if (x) out.push(x.name);
    }
    return out;
  }
  function render() {
    const logged = !!state.me;
    $('#login-root').hidden = logged;
    $('#app').hidden = !logged;
    if (!logged) { $('#login-root').innerHTML = viewLogin(); return; }
    let seg = state.route.replace('#/', '').split('/').filter(Boolean);
    // ★ 菜单不可见的页面不可进入；页面内的按钮再按操作码控制
    if (!seg.length || (seg[0] !== 'me' && !navOk(seg[0]))) seg = landing().slice(2).split('/');
    const me = userById(state.me);
    $('#nav').innerHTML = ucMenus().map((m) => `<a class="nav-item ${m.code === seg[0] ? 'active' : ''}" data-nav="#${m.path}" href="#${m.path}"><span class="nav-ico">${m.icon}</span><span>${esc(m.name)}</span></a>`).join('');
    $('#sidebar-foot').innerHTML = `
      <div class="me" data-nav="#/me">${avatar(me)}<div><strong>${esc(me.name)}</strong><small>${esc(rolesOf(me).all.map(roleById).filter((r) => r.app === 'uc').map((r) => r.name).join('、') || '普通用户')}</small></div></div>
      <button class="logout" data-act="logout">退出登录</button>`;
    const c = crumbs(seg);
    $('#topbar').innerHTML = `<div class="crumbs"><span>用户中心</span>${c.map((x, i) => `<span class="sep">/</span><span class="${i === c.length - 1 ? 'cur' : ''}">${esc(x)}</span>`).join('')}</div>`;
    const v = {
      org: viewOrg, users: () => (seg[1] ? viewUser(seg[1]) : viewUsers()), apps: () => (seg[1] ? viewApp(seg[1]) : viewApps()),
      grants: () => (seg[1] === 'data' && seg[3] ? viewDataItem(decodeURIComponent(seg[2]), decodeURIComponent(seg[3])) : viewGrants()),
      security: viewSecurity, audit: viewAudit, me: viewMe,
    }[seg[0]] || viewOrg;
    $('#content').innerHTML = v();
  }
  function navigate(h) { if (location.hash === h) render(); else location.hash = h; }

  /* ---------------------------------------------------------
     13. 事件
     --------------------------------------------------------- */
  document.addEventListener('click', (e) => {
    if (e.target.matches('[data-mask]') || e.target.closest('[data-close-modal]')) { closeModal(); return; }
    const act = e.target.closest('[data-act]');
    // 表格行内的按钮 / 复选框不触发整行跳转
    if (!act && e.target.closest('[data-stop]')) return;
    const nav = e.target.closest('[data-nav]');
    if (nav && !act) { e.preventDefault(); navigate(nav.dataset.nav); return; }
    if (!act) return;
    const a = act.dataset.act, d = act.dataset;
    if (a === 'logout') { state.me = null; state.dataTab = 'mine'; state.dataCode = 'all'; render(); toast('已退出登录', 'ok'); return; }
    if (a === 'copy') { (navigator.clipboard ? navigator.clipboard.writeText(d.text) : Promise.resolve()).catch(() => {}); toast('已复制'); return; }
    // 组织
    if (a === 'org-toggle') { e.stopPropagation(); state.orgOpen.has(d.id) ? state.orgOpen.delete(d.id) : state.orgOpen.add(d.id); render(); return; }
    if (a === 'org-sel') { state.orgSel = d.id; let p = deptById(d.id).parent; while (p) { state.orgOpen.add(p); p = deptById(p).parent; } render(); return; }
    if (a === 'dept-new') { openDeptForm('new', d.id); return; }
    if (a === 'dept-edit') { openDeptForm('edit', d.id); return; }
    if (a === 'dept-save') { saveDept(d.mode, d.id); return; }
    if (a === 'dept-del') { deleteDept(d.id); return; }
    if (a === 'dept-del-confirm') {
      const dept = deptById(d.id);
      APPS.forEach((x) => { x.scope.depts = x.scope.depts.filter((id) => id !== d.id); });
      for (let i = GRANTS.length - 1; i >= 0; i--) if (GRANTS[i].subj === 'dept' && GRANTS[i].to === d.id) GRANTS.splice(i, 1);
      for (let i = DACL.length - 1; i >= 0; i--) if (DACL[i].subj === 'dept' && DACL[i].to === d.id) DACL.splice(i, 1);
      DEPTS.splice(DEPTS.indexOf(dept), 1);
      state.orgSel = dept.parent;
      audit('删除部门', dept.name);
      closeModal(); toast(`已删除部门「${dept.name}」`); render(); return;
    }
    if (a === 'dept-leader') { const dept = deptById(d.id); dept.leader = d.user; audit('设置部门负责人', dept.name, userById(d.user).name); toast(`已将 ${userById(d.user).name} 设为「${dept.name}」负责人`); render(); return; }
    if (a === 'dept-move-in') { openMoveIn(d.id); return; }
    if (a === 'move-in-confirm') {
      const ids = $$('[data-pick]').filter((x) => x.checked).map((x) => x.value);
      if (!ids.length) { toast('请选择成员', 'warn'); return; }
      moveUsers(ids, d.id); closeModal(); render(); return;
    }
    // 用户
    if (a === 'ustatus') { state.ustatus = d.s; render(); return; }
    if (a === 'user-new') { openUserForm(null, d.dept); return; }
    if (a === 'user-edit') { openUserForm(d.id); return; }
    if (a === 'user-save') { saveUser(d.id || null); return; }
    if (a === 'user-reset') { resetPassword(d.id); return; }
    if (a === 'user-reset-confirm') {
      const u = userById(d.id); const tmp = tempPassword();
      PWD[u.id] = tmp; if (u.status !== 'disabled') u.status = 'pending'; state.fails[u.id] = 0;
      audit('重置密码', `${u.name}（${u.account}）`); render(); showTempPassword(u, tmp, '密码已重置'); return;
    }
    if (a === 'user-unlock') { setStatus(d.id, 'active', '解锁账号'); return; }
    if (a === 'user-enable') { setStatus(d.id, 'active', '启用用户'); return; }
    if (a === 'user-disable') { confirmDisable([d.id]); return; }
    if (a === 'disable-confirm') {
      const ids = d.ids.split(',');
      if (ids.includes(state.me)) { toast('不能停用自己', 'warn'); return; }
      closeModal(); ids.forEach((id) => setStatus(id, 'disabled', '停用用户')); render(); return;
    }
    if (a === 'utab') { state.utab = d.t; render(); return; }
    // 应用
    if (a === 'app-new') { openNewApp(); return; }
    if (a === 'app-create') {
      const name = $('[data-an]').value.trim();
      if (!name) { $('[data-aerr]').textContent = '请填写应用名称'; return; }
      const app = { id: uid('app'), name, icon: '🧱', desc: $('[data-ad]').value.trim(), clientId: `ap_${randHex(8)}`, secretTail: '', scope: { all: false, depts: [] }, status: 'active', created: nowStr() };
      APPS.push(app); audit('接入应用', name, '可访问范围：未设置');
      location.hash = `#/apps/${app.id}`; render(); showSecret(app, '应用已接入，请保存 App Secret'); return;
    }
    if (a === 'app-secret') {
      const app = appById(d.id);
      openModal({
        title: `重新生成 Secret · ${app.name}`,
        body: `<div>旧 Secret 将<b>立即失效</b>，「${esc(app.name)}」在更新配置前无法调用开放接口。确定重新生成？</div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="app-secret-confirm" data-id="${d.id}">重新生成</button>`,
      });
      return;
    }
    if (a === 'app-secret-confirm') { const app = appById(d.id); audit('重新生成 Secret', app.name); showSecret(app, '新的 App Secret'); render(); return; }
    if (a === 'app-toggle') { const app = appById(d.id); app.status = app.status === 'active' ? 'disabled' : 'active'; audit(app.status === 'active' ? '启用应用' : '停用应用', app.name); toast(app.status === 'active' ? '应用已启用' : '应用已停用，用户无法再登录该应用'); render(); return; }
    if (a === 'scope-save') {
      const app = appById(d.id);
      const all = $('[data-scope-all]:checked').value === 'all';
      const depts = $$('[data-scope-dept]').filter((x) => x.checked).map((x) => x.value);
      if (!all && !depts.length) { toast('请至少选择一个部门，或选择「全员」', 'warn'); return; }
      app.scope = { all, depts: all ? [] : depts };
      audit('修改可访问范围', app.name, scopeText(app)); toast(`可访问范围：${scopeText(app)}`); render(); return;
    }
    // 角色
    if (a === 'menu-new') { openMenuForm(d.app, null, d.parent || null); return; }
    if (a === 'menu-edit') { const m = menuById(d.id); openMenuForm(m.app, m.id, m.parent); return; }
    if (a === 'menu-save') { saveMenu(d.app, d.id || null); return; }
    if (a === 'menu-up') {
      const m = menuById(d.id); const sib = menuKids(m.app, m.parent || null); const i = sib.indexOf(m);
      if (i > 0) { const t = sib[i - 1].sort; sib[i - 1].sort = m.sort; m.sort = t; render(); }
      return;
    }
    if (a === 'menu-del') {
      const m = menuById(d.id);
      if (MENUS.some((x) => x.parent === m.id)) { toast('目录下还有菜单，请先删除或移走', 'warn'); return; }
      const rs = ROLES.filter((r) => r.menus.includes(m.id));
      openModal({
        title: `删除菜单 · ${m.name}`,
        body: `<div>${rs.length ? `该菜单绑定在 ${rs.map((r) => esc(r.name)).join('、')} 上，删除后一并解除。` : '确定删除？'}</div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="menu-del-confirm" data-id="${m.id}">删除</button>`,
      });
      return;
    }
    if (a === 'menu-del-confirm') {
      const m = menuById(d.id); ROLES.forEach((r) => { r.menus = r.menus.filter((x) => x !== m.id); });
      MENUS.splice(MENUS.indexOf(m), 1); audit('删除菜单', `${appName(m.app)} · ${m.name}`); closeModal(); toast('已删除'); render(); return;
    }
    if (a === 'role-menus-save') {
      const r = roleById(d.id);
      r.menus = $$('[data-rmenu]').filter((x) => x.checked && !x.disabled && menuById(x.dataset.rmenu).type === 'menu').map((x) => x.dataset.rmenu);
      audit('修改角色菜单', `${appName(r.app)} · ${r.name}`, `${r.menus.length} 个菜单`);
      toast(`已保存；${usersWithRole(r.id).length} 名拥有者的菜单随之更新`, 'ok', 3000); render(); return;
    }
    if (a === 'app-tab') { state.appTab = d.t; render(); return; }
    if (a === 'grant-sec') { state.grantSec = d.s; if (d.back || location.hash !== '#/grants') navigate('#/grants'); else render(); return; }
    if (a === 'role-sel') { state.roleSel = d.id; render(); return; }
    if (a === 'role-tab') { state.roleTab = d.t; render(); return; }
    if (a === 'role-new') { openRoleForm('new'); return; }
    if (a === 'role-edit') { openRoleForm('edit', d.id); return; }
    if (a === 'role-copy') { openRoleForm('copy', d.id); return; }
    if (a === 'role-save') { saveRole(d.mode, d.id || null); return; }
    if (a === 'role-ops-save') { saveRoleOps(d.id); return; }
    if (a === 'role-del') {
      const r = roleById(d.id); const gs = GRANTS.filter((g) => g.kind === 'role' && g.ref === d.id); const poss = POSITIONS.filter((p) => p.roles.includes(d.id));
      openModal({
        title: `删除角色 · ${r.name}`,
        body: `<div>删除后将撤销 ${gs.length} 条授权${poss.length ? `、${poss.length} 个岗位（${poss.map((p) => esc(p.name)).join('、')}）` : ''}中也会移除该角色；目前 ${usersWithRole(d.id).length} 人将失去其 ${r.ops.length} 项操作权限。</div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="role-del-confirm" data-id="${d.id}">删除</button>`,
      });
      return;
    }
    if (a === 'role-del-confirm') {
      const r = roleById(d.id);
      for (let i = GRANTS.length - 1; i >= 0; i--) if (GRANTS[i].kind === 'role' && GRANTS[i].ref === d.id) GRANTS.splice(i, 1);
      POSITIONS.forEach((p) => { p.roles = p.roles.filter((x) => x !== d.id); });
      ROLES.splice(ROLES.indexOf(r), 1); state.roleSel = null;
      audit('删除角色', `${appName(r.app)} · ${r.name}`); closeModal(); toast(`已删除角色「${r.name}」`); render(); return;
    }
    // 数据权限
    if (a === 'data-tab') { state.dataTab = d.t; render(); return; }
    if (a === 'data-sim') { openDataSim(); return; }
    if (a === 'data-sim-go') {
      const name = $('[data-sim-name]').value.trim();
      if (!name) { toast('请填写 Agent 名称', 'warn'); return; }
      const me = userById(state.me);
      DATA.unshift({ code: 'Agent', id: state._simId, name, created: nowStr() });
      DACL.push({ id: uid('da'), code: 'Agent', dataId: state._simId, level: 'owner', subj: 'user', to: me.id, sub: false, by: 'api', at: nowStr(), via: 'api' });
      AUDIT.unshift({ time: nowStr(), actor: 'system', action: '接口写入数据权限', target: `Agent · ${name}`, detail: `Atlas → OWNER · ${me.name}` });
      closeModal(); toast('Atlas 已写入 Owner 授权'); navigate(`#/grants/data/Agent/${state._simId}`); return;
    }
    if (a === 'dtype-new') {
      openModal({
        title: `登记数据编码 · ${appName(d.app)}`,
        body: `<div class="grid2"><div class="field"><label>数据编码<span class="req">*</span></label><input type="text" data-dt="code" placeholder="如 KnowledgeBase"><div class="hint">大写字母开头，字母和数字，全局唯一</div></div>
          <div class="field"><label>名称<span class="req">*</span></label><input type="text" data-dt="name" placeholder="如 知识库"></div></div>
          <div class="field"><label>所属应用</label><input type="text" value="${esc(appName(d.app))}" disabled></div>
          <div class="field"><label>说明</label><input type="text" data-dt="desc"></div><div data-dterr class="small" style="color:var(--danger)"></div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="dtype-save" data-app="${d.app}">保存</button>`,
      });
      return;
    }
    if (a === 'dtype-save') {
      const f = (k) => $(`[data-dt="${k}"]`).value.trim();
      const err = (m) => { $('[data-dterr]').textContent = m; };
      if (!/^[A-Z][A-Za-z0-9]{1,31}$/.test(f('code'))) return err('数据编码格式：大写字母开头，字母和数字，2–32 位');
      if (DATA_TYPES.some((t) => t.code === f('code'))) return err('数据编码已存在');
      if (!f('name')) return err('请填写名称');
      DATA_TYPES.push({ code: f('code'), name: f('name'), app: d.app, desc: f('desc') });
      audit('登记数据编码', `${appName(d.app)} · ${f('code')}`); closeModal(); toast('已登记'); render(); return;
    }
    if (a === 'dtype-del') {
      const n = DATA.filter((x) => x.code === d.code).length;
      if (n) { openModal({ title: `无法删除「${d.code}」`, body: `<div class="callout callout-warn"><span>⚠️</span><div>还有 ${n} 条数据使用该编码，应用删除数据并调用清理接口后才能删除。</div></div>`, footer: '<button class="btn btn-primary" data-close-modal>知道了</button>' }); return; }
      DATA_TYPES.splice(DATA_TYPES.findIndex((t) => t.code === d.code), 1); audit('删除数据编码', d.code); toast('已删除'); render(); return;
    }
    if (a === 'dacl-add') { if (!isOwner(d.code, d.id)) { toast('只有 Owner 可以修改授权', 'warn'); return; } openDaclAdd(d.code, d.id); return; }
    if (a === 'dacl-save') {
      if (!isOwner(d.code, d.id)) { toast('只有 Owner 可以修改授权', 'warn'); return; }
      const level = ($$('[data-dacl-lv]').find((x) => x.checked) || {}).value;
      const subs = [...$$('[data-grant-dept]').filter((x) => x.checked).map((x) => ({ t: 'dept', id: x.value })), ...$$('[data-grant-user]').filter((x) => x.checked).map((x) => ({ t: 'user', id: x.value }))];
      if (!subs.length) { toast('请选择组织或用户', 'warn'); return; }
      const sub = $('[data-grant-sub]').checked;
      let added = 0, updated = 0;
      const ok = guardOwner(d.code, d.id, () => subs.forEach(({ t, id }) => {
        const ex = DACL.find((x) => x.code === d.code && x.dataId === d.id && x.subj === t && x.to === id);
        if (ex) { ex.level = level; if (t === 'dept') ex.sub = sub; ex.by = state.me; ex.at = nowStr(); ex.via = 'manual'; updated++; return; }
        DACL.push({ id: uid('da'), code: d.code, dataId: d.id, level, subj: t, to: id, sub: t === 'dept' && sub, by: state.me, at: nowStr(), via: 'manual' });
        added++;
      }));
      if (ok) { audit('数据授权', `${d.code} · ${dataBy(d.code, d.id).name}`, `${LEVELS[level].label} → ${subs.map(({ t, id }) => (t === 'dept' ? deptById(id).name : userById(id).name)).join('、')}`); toast(`新增 ${added} 条${updated ? `，更新 ${updated} 条` : ''}`); }
      closeModal(); render(); return;
    }
    if (a === 'dacl-sub' || a === 'dacl-del') {
      const x = DACL.find((y) => y.id === d.id);
      if (!isOwner(x.code, x.dataId)) { toast('只有 Owner 可以修改授权', 'warn'); return; }
      const ok = guardOwner(x.code, x.dataId, () => { if (a === 'dacl-sub') x.sub = !x.sub; else DACL.splice(DACL.indexOf(x), 1); });
      if (ok) { audit(a === 'dacl-sub' ? '调整数据授权范围' : '移除数据授权', `${x.code} · ${dataBy(x.code, x.dataId).name}`, aclSubjName(x)); toast(a === 'dacl-sub' ? '范围已调整' : '已移除'); }
      render(); return;
    }
    // 权限管理
    if (a === 'grant-tab') { state.grantTab = d.t; render(); return; }
    if (a === 'grant-mode') { state.grantMode = d.m; render(); return; }
    if (a === 'grant-subj') { state.grantSubj = { t: d.t, id: d.id }; state.grantMode = d.t; render(); return; }
    if (a === 'grant-goto') { state.grantSec = 'role'; state.grantTab = 'subject'; state.grantMode = d.t; state.grantSubj = { t: d.t, id: d.id }; closeModal(); navigate('#/grants'); return; }
    if (a === 'grant-kind') { state.grantKind = d.v; render(); return; }
    if (a === 'grant-to-f') { state.grantTo = d.v; render(); return; }
    if (a === 'grant-user-detail') { state.utab = 'perm'; navigate(`#/users/${d.id}`); return; }
    if (a === 'grant-add') { openGrantItems(d.kind, d.t, d.id); return; }
    if (a === 'grant-items-save') {
      const refs = $$('[data-grant-ref]').filter((x) => x.checked && !x.disabled).map((x) => x.value);
      if (!refs.length) { toast('请选择要授予的' + (d.kind === 'pos' ? '岗位' : '角色'), 'warn'); return; }
      const sub = !!($('[data-grant-sub]') || {}).checked;
      const r = addGrants(d.kind, refs, [{ t: d.t, id: d.id }], sub);
      closeModal(); toast(`已授予 ${r.added} 项`); render(); return;
    }
    if (a === 'grant-to') { openGrantTo(d.kind, d.ref); return; }
    if (a === 'grant-to-save') {
      const subs = [...$$('[data-grant-dept]').filter((x) => x.checked).map((x) => ({ t: 'dept', id: x.value })), ...$$('[data-grant-user]').filter((x) => x.checked).map((x) => ({ t: 'user', id: x.value }))];
      if (!subs.length) { toast('请选择组织或用户', 'warn'); return; }
      const r = addGrants(d.kind, [d.ref], subs, $('[data-grant-sub]').checked);
      closeModal(); toast(`新增 ${r.added} 条授权${r.updated ? `，更新 ${r.updated} 条范围` : ''}`); render(); return;
    }
    if (a === 'grant-sub') {
      const g = GRANTS.find((x) => x.id === d.id);
      if (guardSuper(() => { g.sub = !g.sub; })) { audit('调整授权范围', `${refName(g)} → ${subjName(g)}`, g.sub ? '含下级部门' : '仅本部门'); toast(g.sub ? '已改为包含下级部门' : '已改为仅本部门'); }
      render(); return;
    }
    if (a === 'grant-revoke') {
      const g = GRANTS.find((x) => x.id === d.id);
      openModal({
        title: '撤销授权',
        body: `<div>撤销 ${kindBadge(g.kind)} <b>${esc(refName(g))}</b> 对 ${subjTag(g)} 的授权？</div><div class="small muted mt8">覆盖 ${holdersOf(g).length} 人；没有其他来源的人将失去对应权限（下次刷新令牌时生效）。</div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="grant-revoke-confirm" data-id="${g.id}">撤销</button>`,
      });
      return;
    }
    if (a === 'grant-revoke-confirm') {
      const g = GRANTS.find((x) => x.id === d.id);
      closeModal();
      if (guardSuper(() => GRANTS.splice(GRANTS.indexOf(g), 1))) { audit('撤销授权', `${refName(g)} → ${subjName(g)}`); toast('已撤销'); }
      render(); return;
    }
    // 岗位
    if (a === 'pos-new') { openPosForm(null); return; }
    if (a === 'pos-edit') { openPosForm(d.id); return; }
    if (a === 'pos-save') { savePos(d.id || null); return; }
    if (a === 'pos-del') {
      const p = posById(d.id); const n = GRANTS.filter((g) => g.kind === 'pos' && g.ref === d.id).length;
      if (n) { openModal({ title: `无法删除「${p.name}」`, body: `<div class="callout callout-warn"><span>⚠️</span><div>该岗位还有 ${n} 条授权，请先在权限管理中撤销。</div></div>`, footer: '<button class="btn btn-primary" data-close-modal>知道了</button>' }); return; }
      POSITIONS.splice(POSITIONS.indexOf(p), 1); audit('删除岗位', p.name); toast(`已删除岗位「${p.name}」`); render(); return;
    }
    // 操作目录
    if (a === 'op-new') {
      openModal({
        title: `新增操作 · ${appName(d.app)}`,
        body: `<div class="grid2"><div class="field"><label>模块<span class="req">*</span></label><input type="text" data-of="module" placeholder="例如：流程"></div><div class="field"><label>操作名称<span class="req">*</span></label><input type="text" data-of="name" placeholder="例如：撤回流程"></div></div>
          <div class="field"><label>操作码<span class="req">*</span></label><input type="text" data-of="code" placeholder="资源:动作，例如 process:withdraw"><div class="hint">小写字母、数字、下划线，格式「资源:动作」；在应用内唯一，前端与后端都用它判断权限</div></div><div data-oerr class="small" style="color:var(--danger)"></div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-primary" data-act="op-save" data-app="${d.app}">保存</button>`,
      });
      return;
    }
    if (a === 'op-save') {
      const f = (k) => $(`[data-of="${k}"]`).value.trim();
      const code = f('code'), err = (m) => { $('[data-oerr]').textContent = m; };
      if (!f('module') || !f('name')) return err('请填写模块与操作名称');
      if (!/^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$/.test(code)) return err('操作码格式：资源:动作（小写字母、数字、下划线）');
      if (OPS.some((o) => o.app === d.app && o.code === code)) return err('该应用已有相同操作码');
      OPS.push({ app: d.app, module: f('module'), code, name: f('name') }); audit('新增操作', `${appName(d.app)} · ${code}`); closeModal(); toast('操作已登记，可在角色中勾选'); render(); return;
    }
    if (a === 'op-del') {
      const rs = ROLES.filter((r) => r.ops.includes(d.code));
      openModal({
        title: `删除操作 · ${d.code}`,
        body: `<div>${rs.length ? `该操作被 ${rs.map((r) => esc(r.name)).join('、')} 使用，删除后将从这些角色中移除。` : '确定删除？'}应用前端中对应的菜单 / 按钮将对所有人隐藏。</div>`,
        footer: `<button class="btn" data-close-modal>取消</button><button class="btn btn-danger" data-act="op-del-confirm" data-code="${d.code}">删除</button>`,
      });
      return;
    }
    if (a === 'op-del-confirm') {
      const o = opByCode(d.code); ROLES.forEach((r) => { r.ops = r.ops.filter((x) => x !== d.code); }); OPS.splice(OPS.indexOf(o), 1);
      audit('删除操作', `${appName(o.app)} · ${d.code}`); closeModal(); toast('操作已删除'); render(); return;
    }
    // 安全策略 / 审计 / 个人中心
    if (a === 'policy-save') { savePolicy(); return; }
    if (a === 'audit-tab') { state.auditTab = d.t; render(); return; }
    if (a === 'me-save') {
      const u = userById(state.me); const email = $('[data-me="email"]').value.trim();
      if (!EMAIL_RE.test(email)) { toast('邮箱格式不正确', 'warn'); return; }
      if (USERS.some((x) => x.email === email && x.id !== u.id)) { toast('邮箱已被其他用户使用', 'warn'); return; }
      u.email = email; u.phone = $('[data-me="phone"]').value.trim(); audit('修改个人信息', u.name); toast('已保存'); return;
    }
    if (a === 'me-pwd') {
      const u = userById(state.me); const err = $('[data-pw-err]');
      const old = $('[data-pw="old"]').value, np = $('[data-pw="new"]').value, cp = $('[data-pw="confirm"]').value;
      if (old !== (PWD[u.id] || DEMO_PWD)) { err.textContent = '当前密码不正确'; return; }
      const errs = checkPassword(np);
      if (errs.length) { err.textContent = `新密码不符合要求：${errs.join('、')}`; return; }
      if (np === old) { err.textContent = '新密码不能与当前密码相同'; return; }
      if (np !== cp) { err.textContent = '两次输入不一致'; return; }
      PWD[u.id] = np; audit('修改密码', u.name); err.textContent = ''; $$('[data-pw]').forEach((x) => { x.value = ''; }); toast('密码已修改，其他设备上的登录会话已注销');
    }
  });

  document.addEventListener('submit', (e) => {
    if (e.target.id === 'login-form') { e.preventDefault(); doLogin(); }
    if (e.target.id === 'change-form') { e.preventDefault(); doChangeFirst(); }
  });

  document.addEventListener('input', (e) => {
    if (e.target.matches('[data-uq]')) { state.uq = e.target.value; refreshUserRows(); }
    if (e.target.matches('[data-audit-q]')) { state.auditQ = e.target.value; const pos = e.target.selectionStart; render(); const el = $('[data-audit-q]'); el.focus(); el.setSelectionRange(pos, pos); }
    if (e.target.matches('[data-sim-name]')) simPreview();
    if (e.target.matches('[data-pick-q]')) { const q = e.target.value.trim(); $$('[data-pick-row]').forEach((r) => { r.hidden = !!q && !r.dataset.pickRow.includes(q); }); }
  });

  document.addEventListener('change', (e) => {
    const t = e.target;
    if (t.matches('[data-udept]')) { state.udept = t.value; refreshUserRows(); }
    if (t.matches('[data-act="org-sub"]')) { state.orgSub = t.checked; render(); }
    if (t.matches('[data-menu-preview]')) { state.menuPreview = t.value; render(); return; }
    if (t.matches('[data-mf-type]')) { const dir = t.value === 'dir'; $('[data-mf-pathbox]').hidden = dir; $('[data-mf-pubbox]').hidden = dir; }
    if (t.matches('[data-rmenu]')) {
      menuDesc(t.dataset.rmenu).forEach((id) => { const el = $(`[data-rmenu="${id}"]`); if (el && !el.disabled) el.checked = t.checked; });
      // 目录的勾选状态跟随子菜单
      let p = menuById(menuById(t.dataset.rmenu).parent);
      while (p) { const el = $(`[data-rmenu="${p.id}"]`); if (el) el.checked = menuDesc(p.id).some((id) => { const c = $(`[data-rmenu="${id}"]`); return c && c.checked && menuById(id).type === 'menu'; }); p = menuById(p.parent); }
    }
    if (t.matches('[data-data-code]')) { state.dataCode = t.value; render(); return; }
    if (t.matches('[data-dacl-level]')) {
      const x = DACL.find((y) => y.id === t.dataset.daclLevel);
      if (!isOwner(x.code, x.dataId)) { toast('只有 Owner 可以修改授权', 'warn'); render(); return; }
      const before = x.level;
      if (guardOwner(x.code, x.dataId, () => { x.level = t.value; })) { audit('修改数据权限', `${x.code} · ${dataBy(x.code, x.dataId).name}`, `${aclSubjName(x)}：${LEVELS[before].label} → ${LEVELS[x.level].label}`); toast(`已改为「${LEVELS[x.level].label}」`); }
      render(); return;
    }
    if (t.matches('[data-mod-all]')) { $$(`[data-mod="${t.dataset.modAll}"]`).forEach((x) => { x.checked = t.checked; }); }
    if (t.matches('[data-op]')) { const all = $$(`[data-mod="${t.dataset.mod}"]`); const box = $(`[data-mod-all="${t.dataset.mod}"]`); if (box) box.checked = all.every((x) => x.checked); }
    if (t.matches('[data-scope-all]')) { $$('[data-scope-dept]').forEach((x) => { x.disabled = t.value === 'all'; }); }
  });

  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') closeModal(); });
  window.addEventListener('hashchange', () => { state.route = location.hash || '#/org'; state.utab = 'info'; render(); window.scrollTo(0, 0); });

  state.route = location.hash || '#/org';
  render();
})();
