"use client";

/**
 * 编排器：团队流程模板和流程市场模板共用（保存、发布由外层传入）。
 *
 * ★ 模板只存结构：顺序步骤 + 并行组；节点依赖由结构推导，不用连线。
 * ★ 角色只是占位（模板角色）：在项目配置里给每个角色分配成员和数字员工，和用户中心的系统角色无关。
 * ★ 第一步的节点没有上游，不设准入；其余节点没配准入只给警告（直接开工）。
 */
import { App, Button, Input, Modal, Popconfirm, Segmented, Tag } from "antd";
import Link from "next/link";
import { useEffect, useState, type ReactNode } from "react";

import { errorText } from "@/lib/api";
import {
  addNode,
  addTrack,
  appendToTrack,
  isRoot,
  makeParallel,
  moveStep,
  newRoleKey,
  removeNode,
  removeRole,
  updateNode,
  upstreams,
} from "@/lib/structure";
import type { Issue, Rule, Structure } from "@/lib/types";

import { FlowGraph } from "./FlowGraph";
import { Icon } from "./ui";

export interface EditorProps {
  title: string;
  backHref: string;
  tags?: ReactNode;
  structure: Structure;
  /** 下一个版本号（按钮上写「发布 vN」） */
  nextVersion: number;
  /** 存草稿 → 校验结果 */
  save: (s: Structure) => Promise<{ errors: Issue[]; warnings: Issue[] }>;
  publish: (note: string) => Promise<void>;
  /** 发布前提示：发布后对谁有影响 */
  publishHint: ReactNode;
  roleHint: string;
}

export function TemplateEditor(p: EditorProps) {
  const { message } = App.useApp();
  const [s, setS] = useState<Structure>(p.structure);
  const [dirty, setDirty] = useState(false);
  const [sel, setSel] = useState<string | undefined>(Object.keys(p.structure.nodes)[0]);
  const [issues, setIssues] = useState<{ errors: Issue[]; warnings: Issue[] } | null>(null);
  const [saving, setSaving] = useState(false);
  const [roles, setRoles] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [note, setNote] = useState("");

  const change = (next: Structure) => {
    setS(next);
    setDirty(true);
  };
  const save = async (): Promise<{ errors: Issue[]; warnings: Issue[] } | null> => {
    setSaving(true);
    try {
      const r = await p.save(s);
      setIssues(r);
      setDirty(false);
      return r;
    } catch (e) {
      message.error(errorText(e));
      return null;
    } finally {
      setSaving(false);
    }
  };
  // 离开前提醒还没保存
  useEffect(() => {
    if (!dirty) return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [dirty]);

  const startPublish = async () => {
    const r = await save();
    if (r) setPublishing(true);
  };
  const doPublish = async () => {
    try {
      await p.publish(note.trim());
      message.success(`已发布 v${p.nextVersion}`);
      setPublishing(false);
      setNote("");
    } catch (e) {
      message.error(errorText(e));
    }
  };

  const bad = new Set((issues?.errors ?? []).map((i) => i.node).filter((x): x is string => !!x));
  const add = (r: [Structure, string]) => {
    change(r[0]);
    if (r[1]) setSel(r[1]);
  };
  const plus = (title: string, onClick: () => void, icon: "plus" | "layers" = "plus") => (
    <button type="button" className="addbtn" title={title} onClick={onClick}>
      <Icon name={icon} size={13} />
    </button>
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minWidth: 0, minHeight: 0 }}>
      <div className="edtop">
        <Link className="row" href={p.backHref} title="返回">
          <Icon name="left" size={16} />
        </Link>
        <b>{p.title}</b>
        {p.tags}
        {dirty && <Tag color="warning">有未保存的修改</Tag>}
        <span className="sp" />
        <Button size="small" icon={<Icon name="users" size={13} />} onClick={() => setRoles(true)}>
          角色（{Object.keys(s.roles).length}）
        </Button>
        <Button size="small" loading={saving} onClick={() => void save()}>
          保存并校验
        </Button>
        <Button size="small" type="primary" onClick={() => void startPublish()}>
          发布 v{p.nextVersion}
        </Button>
      </div>
      <div className="editor">
        <div className="canvas">
          <FlowGraph
            structure={s}
            selected={sel}
            onSelect={setSel}
            bad={bad}
            head={
              <>
                {plus("在最前面加一个节点", () => add(addNode(s, -1)))}
                <span className="arrow" style={{ width: 14 }} />
              </>
            }
            renderAfterStep={(i) => (
              // 后面那段箭头由流程图在下一步前面画
              <>
                <span className="arrow" style={{ width: 14 }} />
                {plus("在这一步后面加一个节点", () => add(addNode(s, i)))}
              </>
            )}
            renderInPar={(i) => <div className="row" style={{ justifyContent: "center" }}>{plus("加一条并行轨道", () => add(addTrack(s, i)))}</div>}
            renderAfterTrack={(i, t) => plus("在这条轨道末尾接一个节点", () => add(appendToTrack(s, i, t)))}
            renderUnderStep={(i) => (
              <div className="stepbar">
                <Button size="small" type="text" disabled={i === 0} onClick={() => change(moveStep(s, i, -1))} title="往前挪">
                  <Icon name="left" size={13} />
                </Button>
                {s.flow[i]!.type === "node" && (
                  <Button size="small" type="text" onClick={() => add(makeParallel(s, i))} title="改成并行组：再加一条轨道">
                    ∥
                  </Button>
                )}
                <Button size="small" type="text" disabled={i === s.flow.length - 1} onClick={() => change(moveStep(s, i, 1))} title="往后挪">
                  <Icon name="right" size={13} />
                </Button>
              </div>
            )}
          />
          <div className="xs faint" style={{ position: "absolute", left: 18, bottom: 14 }}>
            模板只存结构：顺序步骤 + 并行组。节点依赖由结构推导，不用连线
          </div>
        </div>
        <div className="inspector">
          {issues && (issues.errors.length > 0 || issues.warnings.length > 0) && (
            <div className="col" style={{ gap: 6, marginBottom: 14 }}>
              {issues.errors.map((e, i) => (
                <div key={`e${i}`} className="banner danger" style={{ cursor: e.node ? "pointer" : undefined }} onClick={() => e.node && setSel(e.node)}>
                  <Icon name="alert" size={14} />
                  <div className="sm">{e.message}</div>
                </div>
              ))}
              {issues.warnings.map((w, i) => (
                <div key={`w${i}`} className="banner warn" style={{ cursor: w.node ? "pointer" : undefined }} onClick={() => w.node && setSel(w.node)}>
                  <Icon name="warn" size={14} />
                  <div className="sm">{w.message}</div>
                </div>
              ))}
            </div>
          )}
          {issues && !issues.errors.length && !issues.warnings.length && (
            <div className="banner info" style={{ marginBottom: 14 }}>
              <Icon name="check" size={14} />
              <div className="sm">校验通过，可以发布</div>
            </div>
          )}
          {sel && s.nodes[sel] ? (
            <Inspector key={sel} s={s} id={sel} onChange={change} onRemove={() => {
              change(removeNode(s, sel));
              setSel(undefined);
            }} />
          ) : (
            <div className="sm muted">点画布上的节点编辑；用加号加节点、加并行轨道</div>
          )}
        </div>
      </div>
      {roles && <RolesModal s={s} hint={p.roleHint} onChange={change} onClose={() => setRoles(false)} />}
      <Modal open={publishing} title={`发布 v${p.nextVersion}`} onCancel={() => setPublishing(false)} onOk={() => void doPublish()} okText="发布" okButtonProps={{ disabled: !!issues?.errors.length }}>
        {issues?.errors.length ? (
          <div className="banner danger">
            <Icon name="alert" size={15} />
            <div>还有 {issues.errors.length} 个问题没解决，解决后才能发布（看右侧列表）</div>
          </div>
        ) : (
          <div className="col" style={{ gap: 10 }}>
            {!!issues?.warnings.length && (
              <div className="banner warn">
                <Icon name="warn" size={15} />
                <div>有 {issues.warnings.length} 条提醒，不挡发布</div>
              </div>
            )}
            <div className="banner info">
              <Icon name="lock" size={15} />
              <div>{p.publishHint}</div>
            </div>
            <Input value={note} onChange={(e) => setNote(e.target.value)} maxLength={200} placeholder="这一版改了什么（可选）" />
          </div>
        )}
      </Modal>
    </div>
  );
}

function RoleChips({ s, value, onChange, multi }: { s: Structure; value: string[]; onChange: (v: string[]) => void; multi: boolean }) {
  return (
    <div className="row" style={{ flexWrap: "wrap", gap: 6 }}>
      {Object.entries(s.roles).map(([k, v]) => {
        const on = value.includes(k);
        return (
          <Tag.CheckableTag
            key={k}
            checked={on}
            onChange={() => onChange(multi ? (on ? value.filter((x) => x !== k) : [...value, k]) : [k])}
            style={{ border: "1px solid var(--line)", padding: "2px 10px" }}
          >
            {v}
          </Tag.CheckableTag>
        );
      })}
      {!Object.keys(s.roles).length && <span className="xs warn-text">先在顶栏「角色」里加角色</span>}
    </div>
  );
}

function Inspector({ s, id, onChange, onRemove }: { s: Structure; id: string; onChange: (s: Structure) => void; onRemove: () => void }) {
  const n = s.nodes[id]!;
  const root = isRoot(s, id);
  const ups = (upstreams(s)[id] ?? []).map((u) => s.nodes[u]?.name ?? u);
  const set = (patch: Partial<typeof n>) => onChange(updateNode(s, id, patch));
  const total = Object.keys(s.nodes).length;
  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 className="grow">{n.name || "（未命名）"}</h2>
        <Popconfirm title="删除这个节点？" onConfirm={onRemove} disabled={total <= 1}>
          <Button size="small" type="text" icon={<Icon name="trash" size={14} />} disabled={total <= 1} title={total <= 1 ? "至少留一个节点" : "删除节点"} />
        </Popconfirm>
      </div>
      <div className="fld">
        <label className="label">名称</label>
        <Input value={n.name} onChange={(e) => set({ name: e.target.value })} maxLength={32} />
        <div className="help">也是流程空间里放它产出的目录名：不能带 / 、不能以 . 开头</div>
      </div>
      <div className="fld">
        <label className="label">工作说明</label>
        <Input.TextArea value={n.instructions} onChange={(e) => set({ instructions: e.target.value })} autoSize={{ minRows: 4, maxRows: 12 }} maxLength={4000} />
        <div className="help">开工时和上游提交的产出物一起，作为第一条指令发给执行的数字员工。产出物的格式由数字员工的技能约定，这里可以点名要用的技能</div>
      </div>
      <div className="fld">
        <label className="label">执行角色</label>
        <RoleChips s={s} value={n.exec_role ? [n.exec_role] : []} onChange={(v) => set({ exec_role: v[0] ?? "" })} multi={false} />
        <div className="help">这个角色的成员下指令、必要时接管、提交评审时选产出物；分配的数字员工干活</div>
      </div>
      <div className="fld">
        <label className="label">
          准入评审 <span className="rtag admit">开工前</span>
        </label>
        {root ? (
          <div className="help">没有上游，不设准入</div>
        ) : (
          <>
            <RoleChips
              s={s}
              value={n.admit?.roles ?? []}
              onChange={(v) => set({ admit: v.length ? { roles: v, rule: n.admit?.rule ?? "any" } : null })}
              multi
            />
            {n.admit && <RuleSeg value={n.admit.rule} onChange={(r) => set({ admit: { ...n.admit!, rule: r } })} />}
            <div className="help">审核上游「{ups.join("、")}」提交的产出物；不通过可打回上游。不配就直接开工</div>
          </>
        )}
      </div>
      <div className="fld">
        <label className="label">
          准出评审 <span className="rtag exit">交付前</span>
        </label>
        <RoleChips s={s} value={n.exit.roles} onChange={(v) => set({ exit: { ...n.exit, roles: v } })} multi />
        <RuleSeg value={n.exit.rule} onChange={(r) => set({ exit: { ...n.exit, rule: r } })} />
        <div className="help">结论：同意（准出）/ 驳回修改（本节点再做一轮）；打回上游在下游准入时做</div>
      </div>
    </>
  );
}

function RuleSeg({ value, onChange }: { value: Rule; onChange: (r: Rule) => void }) {
  return (
    <div className="row" style={{ marginTop: 8 }}>
      <span className="sm muted">通过规则</span>
      <Segmented
        size="small"
        value={value}
        onChange={(v) => onChange(v as Rule)}
        options={[
          { value: "any", label: "任一人同意" },
          { value: "all", label: "全部同意" },
        ]}
      />
    </div>
  );
}

function RolesModal({ s, hint, onChange, onClose }: { s: Structure; hint: string; onChange: (s: Structure) => void; onClose: () => void }) {
  const [name, setName] = useState("");
  const used = (k: string) =>
    Object.values(s.nodes).some((n) => n.exec_role === k || n.admit?.roles.includes(k) || n.exit.roles.includes(k));
  const add = () => {
    const v = name.trim();
    if (!v || Object.values(s.roles).includes(v)) return;
    onChange({ ...s, roles: { ...s.roles, [newRoleKey(s)]: v } });
    setName("");
  };
  return (
    <Modal open title="模板角色" onCancel={onClose} footer={<Button onClick={onClose}>完成</Button>}>
      <div className="sm muted" style={{ marginBottom: 10 }}>
        {hint}。一个角色可以执行或评审好几个节点（所以用角色、不直接写人）
      </div>
      <div className="col" style={{ gap: 6 }}>
        {Object.entries(s.roles).map(([k, v]) => (
          <div key={k} className="row">
            <Input
              value={v}
              maxLength={32}
              onChange={(e) => onChange({ ...s, roles: { ...s.roles, [k]: e.target.value } })}
              style={{ flex: 1 }}
            />
            <Popconfirm
              title={`删除角色「${v}」？`}
              description={used(k) ? "节点上用到它的地方会一起清掉" : undefined}
              onConfirm={() => onChange(removeRole(s, k))}
            >
              <Button type="text" icon={<Icon name="trash" size={14} />} />
            </Popconfirm>
          </div>
        ))}
        <div className="row" style={{ marginTop: 6 }}>
          <Input value={name} onChange={(e) => setName(e.target.value)} onPressEnter={add} maxLength={32} placeholder="新角色，如：产品、开发、测试" />
          <Button onClick={add} disabled={!name.trim()}>
            添加
          </Button>
        </div>
      </div>
    </Modal>
  );
}
