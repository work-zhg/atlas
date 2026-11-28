"use client";

/**
 * 编排器：团队流程模板和流程市场模板共用（保存、发布由外层传入）。
 *
 * ★ 模板只存结构：顺序步骤 + 并行组（界面叫「同时进行的步骤」）；依赖由结构推导，不用连线。
 * ★ 角色只是占位（模板角色）：在项目配置里给每个角色分配成员和数字员工，和用户中心的系统角色无关。
 * ★ 第一步没有上一步，不设准入；其余步骤没配准入只给警告（直接开始）。
 * ★ 草稿自动保存（停手 1 秒后存）；「检查」列出问题，点问题跳到对应步骤。
 */
import { App, Button, Input, Modal, Popconfirm, Segmented, Tag, Tooltip } from "antd";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";

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
  stepOf,
  updateNode,
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
  const [sel, setSel] = useState<string | undefined>(Object.keys(p.structure.nodes)[0]);
  const [issues, setIssues] = useState<{ errors: Issue[]; warnings: Issue[] } | null>(null);
  const [showIssues, setShowIssues] = useState(false);
  const [saveState, setSaveState] = useState<"saved" | "dirty" | "saving" | "error">("saved");
  const [saveError, setSaveError] = useState("");
  const [roles, setRoles] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [note, setNote] = useState("");
  const latest = useRef(s);
  const inflight = useRef(false);
  const saveFn = useRef(p.save);
  saveFn.current = p.save;

  const change = (next: Structure) => {
    setS(next);
    latest.current = next;
    setSaveState("dirty");
  };
  const save = useCallback(async (): Promise<{ errors: Issue[]; warnings: Issue[] } | null> => {
    if (inflight.current) return null;
    inflight.current = true;
    const cur = latest.current;
    setSaveState("saving");
    try {
      const r = await saveFn.current(cur);
      setIssues(r);
      // 存的时候又改了：再存一次（状态回到 dirty，下面的定时器接着存）
      setSaveState(latest.current === cur ? "saved" : "dirty");
      return r;
    } catch (e) {
      setSaveError(errorText(e));
      setSaveState("error");
      return null;
    } finally {
      inflight.current = false;
    }
  }, []);
  // 自动保存：停手 1 秒后存
  useEffect(() => {
    if (saveState !== "dirty") return;
    const t = setTimeout(() => void save(), 1000);
    return () => clearTimeout(t);
  }, [s, saveState, save]);
  // 离开前提醒还没存下
  useEffect(() => {
    if (saveState === "saved") return;
    const h = (e: BeforeUnloadEvent) => e.preventDefault();
    window.addEventListener("beforeunload", h);
    return () => window.removeEventListener("beforeunload", h);
  }, [saveState]);

  const check = async () => {
    const r = await save();
    if (r) setShowIssues(true);
    return r;
  };
  const startPublish = async () => {
    const r = await check();
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

  const bad = new Set(showIssues ? (issues?.errors ?? []).map((i) => i.node).filter((x): x is string => !!x) : []);
  const add = (r: [Structure, string]) => {
    change(r[0]);
    if (r[1]) setSel(r[1]);
  };
  // 加步骤：接在选中的那一步后面（没选就加在最后）；同时进行：和选中的那一步并排
  const at = sel ? stepOf(s, sel) : s.flow.length - 1;
  const addStep = () => add(addNode(s, at < 0 ? s.flow.length - 1 : at));
  const addParallel = () => {
    const i = at < 0 ? s.flow.length - 1 : at;
    add(s.flow[i]?.type === "parallel" ? addTrack(s, i) : makeParallel(s, i));
  };
  const status = {
    saved: <span className="xs faint">已自动保存</span>,
    dirty: <span className="xs faint">有修改，马上保存…</span>,
    saving: <span className="xs faint">保存中…</span>,
    error: (
      <Tooltip title={saveError}>
        <Tag color="error" style={{ cursor: "pointer" }} onClick={() => void save()}>
          保存失败，点这里重试
        </Tag>
      </Tooltip>
    ),
  }[saveState];
  const problems = (issues?.errors.length ?? 0) + (issues?.warnings.length ?? 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minWidth: 0, minHeight: 0 }}>
      <div className="edtop">
        <Link className="row" href={p.backHref} title="返回">
          <Icon name="left" size={16} />
        </Link>
        <b>{p.title}</b>
        {p.tags}
        {status}
        <span className="sp" />
        <Button size="small" icon={<Icon name="users" size={13} />} onClick={() => setRoles(true)}>
          角色（{Object.keys(s.roles).length}）
        </Button>
        <Button size="small" onClick={() => void check()}>
          检查{issues && problems ? `（${problems}）` : ""}
        </Button>
        <Button size="small" type="primary" onClick={() => void startPublish()}>
          发布 v{p.nextVersion}
        </Button>
      </div>
      <div className="editor">
        <div className="canvas">
          <FlowGraph structure={s} selected={sel} onSelect={setSel} bad={bad} />
          <div className="row" style={{ position: "absolute", left: 18, bottom: 14 }}>
            <Button icon={<Icon name="plus" size={13} />} onClick={addStep}>
              添加步骤
            </Button>
            <Button icon={<Icon name="layers" size={13} />} onClick={addParallel}>
              添加同时进行的步骤
            </Button>
            {sel && <span className="xs faint">都加在「{s.nodes[sel]?.name}」旁边</span>}
          </div>
        </div>
        <div className="inspector">
          {showIssues && issues && (
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
              {!problems && (
                <div className="banner info">
                  <Icon name="check" size={14} />
                  <div className="sm">没有问题，可以发布</div>
                </div>
              )}
            </div>
          )}
          {sel && s.nodes[sel] ? (
            <Inspector
              key={sel}
              s={s}
              id={sel}
              onChange={change}
              onAppend={(r) => add(r)}
              onRemove={() => {
                change(removeNode(s, sel));
                setSel(undefined);
              }}
            />
          ) : (
            <div className="sm muted">点画布上的步骤来编辑；用左下角的按钮加步骤</div>
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
            <div className="sm muted">{p.publishHint}</div>
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

function Inspector({
  s,
  id,
  onChange,
  onAppend,
  onRemove,
}: {
  s: Structure;
  id: string;
  onChange: (s: Structure) => void;
  onAppend: (r: [Structure, string]) => void;
  onRemove: () => void;
}) {
  const n = s.nodes[id]!;
  const root = isRoot(s, id);
  const set = (patch: Partial<typeof n>) => onChange(updateNode(s, id, patch));
  const total = Object.keys(s.nodes).length;
  const i = stepOf(s, id);
  const step = s.flow[i];
  const track = step?.type === "parallel" ? step.tracks.findIndex((t) => t.nodes.includes(id)) : -1;
  return (
    <>
      <div className="row" style={{ marginBottom: 12 }}>
        <h2 className="grow ellipsis">{n.name || "（未命名）"}</h2>
        <Button size="small" type="text" disabled={i === 0} onClick={() => onChange(moveStep(s, i, -1))}>
          前移
        </Button>
        <Button size="small" type="text" disabled={i === s.flow.length - 1} onClick={() => onChange(moveStep(s, i, 1))}>
          后移
        </Button>
        {track >= 0 && (
          <Button size="small" type="text" onClick={() => onAppend(appendToTrack(s, i, track))} title="同一条线上，接在它后面再做一步">
            接一步
          </Button>
        )}
        <Popconfirm title="删除这一步？" onConfirm={onRemove} disabled={total <= 1}>
          <Button size="small" type="text" danger disabled={total <= 1} title={total <= 1 ? "至少留一步" : undefined}>
            删除
          </Button>
        </Popconfirm>
      </div>
      <div className="fld">
        <label className="label">名称</label>
        <Input value={n.name} onChange={(e) => set({ name: e.target.value })} maxLength={32} />
        <div className="help">不能包含 /</div>
      </div>
      <div className="fld">
        <label className="label">工作说明</label>
        <Input.TextArea value={n.instructions} onChange={(e) => set({ instructions: e.target.value })} autoSize={{ minRows: 4, maxRows: 12 }} maxLength={4000} />
        <div className="help">数字员工开始时收到的第一条说明，会自动带上上一步交付的文件和成员上传的资料</div>
      </div>
      <div className="fld">
        <label className="label">谁来做</label>
        <RoleChips s={s} value={n.exec_role ? [n.exec_role] : []} onChange={(v) => set({ exec_role: v[0] ?? "" })} multi={false} />
      </div>
      {!root && (
        <div className="fld">
          <label className="label">准入评审（开始前审上一步交付的文件）</label>
          <RoleChips
            s={s}
            value={n.admit?.roles ?? []}
            onChange={(v) => set({ admit: v.length ? { roles: v, rule: n.admit?.rule ?? "any" } : null })}
            multi
          />
          {n.admit ? <RuleSeg value={n.admit.rule} onChange={(r) => set({ admit: { ...n.admit!, rule: r } })} /> : <div className="help">不选就直接开始</div>}
        </div>
      )}
      <div className="fld">
        <label className="label">准出评审（交付前审这一步的文件）</label>
        <RoleChips s={s} value={n.exit.roles} onChange={(v) => set({ exit: { ...n.exit, roles: v } })} multi />
        <RuleSeg value={n.exit.rule} onChange={(r) => set({ exit: { ...n.exit, rule: r } })} />
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
        {hint}。一个角色可以负责或评审好几步
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
              description={used(k) ? "步骤上用到它的地方会一起清掉" : undefined}
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
