/**
 * 流程结构的读和改（和后端 atlas_teamflow/structure/flow.py 同一套规则）。
 *
 * ★ 依赖不存：一个步骤的每个节点接上一步的全部末端，并行组的轨道内顺序执行。
 * ★ 改结构的函数都返回新对象（不改入参），编排器直接拿去 setState。
 */
import type { NodeSpec, ReviewSpec, Step, Structure } from "./types";

/** 按步骤顺序的全部节点 id */
export function nodeIds(s: Structure): string[] {
  return s.flow.flatMap((st) => (st.type === "node" ? [st.id] : st.tracks.flatMap((t) => t.nodes)));
}

/** 一个步骤的末端（下一步的上游）：节点自己，或每条轨道的最后一个 */
function tails(step: Step): string[] {
  return step.type === "node" ? [step.id] : step.tracks.map((t) => t.nodes[t.nodes.length - 1]!).filter(Boolean);
}

/** 一个步骤的起点（接上一步）：节点自己，或每条轨道的第一个 */
function heads(step: Step): string[] {
  return step.type === "node" ? [step.id] : step.tracks.map((t) => t.nodes[0]!).filter(Boolean);
}

/** 节点 → 直接上游 */
export function upstreams(s: Structure): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  s.flow.forEach((step, i) => {
    const prev = i > 0 ? tails(s.flow[i - 1]!) : [];
    if (step.type === "node") out[step.id] = prev;
    else
      for (const t of step.tracks)
        t.nodes.forEach((n, j) => {
          out[n] = j === 0 ? prev : [t.nodes[j - 1]!];
        });
  });
  return out;
}

/** 节点 → 直接下游 */
export function downstreams(s: Structure): Record<string, string[]> {
  const out: Record<string, string[]> = Object.fromEntries(nodeIds(s).map((n) => [n, [] as string[]]));
  for (const [n, ups] of Object.entries(upstreams(s))) for (const u of ups) out[u]?.push(n);
  return out;
}

export const roleNames = (s: Structure, keys: string[] | undefined) =>
  (keys ?? []).map((k) => s.roles[k] ?? k).join("、");

/** 角色负责哪些节点：执行、准入、准出 */
export function roleDuties(s: Structure, role: string) {
  const ids = nodeIds(s);
  const name = (n: string) => s.nodes[n]?.name ?? n;
  return {
    exec: ids.filter((n) => s.nodes[n]?.exec_role === role).map(name),
    admit: ids.filter((n) => s.nodes[n]?.admit?.roles.includes(role)).map(name),
    exit: ids.filter((n) => s.nodes[n]?.exit?.roles.includes(role)).map(name),
  };
}

/** 一行文字的步骤概览：A → B → (C ∥ D) */
export function summary(s: Structure): string {
  const name = (n: string) => s.nodes[n]?.name ?? n;
  return s.flow
    .map((st) => (st.type === "node" ? name(st.id) : st.tracks.map((t) => t.nodes.map(name).join(" → ")).join(" ∥ ")))
    .join(" → ");
}

// ───────────────────────────── 编辑

function newId(s: Structure): string {
  const used = new Set(Object.keys(s.nodes));
  for (let i = used.size + 1; ; i++) if (!used.has(`n${i}`)) return `n${i}`;
}

function freeName(s: Structure): string {
  const names = new Set(Object.values(s.nodes).map((n) => n.name));
  for (let i = Object.keys(s.nodes).length + 1; ; i++) if (!names.has(`步骤 ${i}`)) return `步骤 ${i}`;
}

function blankNode(s: Structure): NodeSpec {
  const role = Object.keys(s.roles)[0] ?? "";
  return { name: freeName(s), instructions: "", exec_role: role, admit: null, exit: { roles: role ? [role] : [], rule: "any" } };
}

const clone = (s: Structure): Structure => JSON.parse(JSON.stringify(s)) as Structure;

/** 在第 at 个步骤之后（at = -1 在最前）加一个节点 → [新结构, 新节点 id] */
export function addNode(s: Structure, at: number): [Structure, string] {
  const out = clone(s);
  const id = newId(out);
  out.nodes[id] = blankNode(out);
  out.flow.splice(at + 1, 0, { type: "node", id });
  return [out, id];
}

/** 把一个单节点步骤变成并行组（原节点是第一条轨道，再加一条） */
export function makeParallel(s: Structure, stepIndex: number): [Structure, string] {
  const out = clone(s);
  const step = out.flow[stepIndex];
  if (!step || step.type !== "node") return [s, ""];
  const id = newId(out);
  out.nodes[id] = blankNode(out);
  out.flow[stepIndex] = { type: "parallel", tracks: [{ nodes: [step.id] }, { nodes: [id] }] };
  return [out, id];
}

/** 并行组加一条轨道 */
export function addTrack(s: Structure, stepIndex: number): [Structure, string] {
  const out = clone(s);
  const step = out.flow[stepIndex];
  if (!step || step.type !== "parallel") return [s, ""];
  const id = newId(out);
  out.nodes[id] = blankNode(out);
  step.tracks.push({ nodes: [id] });
  return [out, id];
}

/** 轨道末尾接一个节点 */
export function appendToTrack(s: Structure, stepIndex: number, track: number): [Structure, string] {
  const out = clone(s);
  const step = out.flow[stepIndex];
  if (!step || step.type !== "parallel" || !step.tracks[track]) return [s, ""];
  const id = newId(out);
  out.nodes[id] = blankNode(out);
  step.tracks[track]!.nodes.push(id);
  return [out, id];
}

/** 删一个节点；并行组只剩一条轨道就退回单节点步骤，空轨道去掉 */
export function removeNode(s: Structure, id: string): Structure {
  const out = clone(s);
  delete out.nodes[id];
  out.flow = out.flow
    .map((st): Step | null => {
      if (st.type === "node") return st.id === id ? null : st;
      const tracks = st.tracks.map((t) => ({ nodes: t.nodes.filter((n) => n !== id) })).filter((t) => t.nodes.length);
      if (!tracks.length) return null;
      if (tracks.length === 1 && tracks[0]!.nodes.length === 1) return { type: "node", id: tracks[0]!.nodes[0]! };
      return { type: "parallel", tracks };
    })
    .filter((x): x is Step => x !== null);
  // 第一个步骤的节点没有上游：不能有准入
  for (const n of heads(out.flow[0] ?? { type: "parallel", tracks: [] })) if (out.nodes[n]) out.nodes[n]!.admit = null;
  return out;
}

/** 步骤左右移动 */
export function moveStep(s: Structure, stepIndex: number, delta: -1 | 1): Structure {
  const to = stepIndex + delta;
  if (to < 0 || to >= s.flow.length) return s;
  const out = clone(s);
  const [step] = out.flow.splice(stepIndex, 1);
  out.flow.splice(to, 0, step!);
  for (const n of heads(out.flow[0]!)) if (out.nodes[n]) out.nodes[n]!.admit = null;
  return out;
}

export function updateNode(s: Structure, id: string, patch: Partial<NodeSpec>): Structure {
  const out = clone(s);
  out.nodes[id] = { ...out.nodes[id]!, ...patch };
  return out;
}

/** 删角色：节点上引用它的也一起去掉 */
export function removeRole(s: Structure, key: string): Structure {
  const out = clone(s);
  delete out.roles[key];
  const strip = (r: ReviewSpec | null) => (r ? { ...r, roles: r.roles.filter((x) => x !== key) } : r);
  for (const n of Object.values(out.nodes)) {
    if (n.exec_role === key) n.exec_role = "";
    n.admit = strip(n.admit);
    n.exit = strip(n.exit)!;
  }
  return out;
}

export function newRoleKey(s: Structure): string {
  for (let i = Object.keys(s.roles).length + 1; ; i++) if (!s.roles[`r${i}`]) return `r${i}`;
}

/** 节点在第几步（编排器判断「第一步的节点不能配准入」） */
export function stepOf(s: Structure, id: string): number {
  return s.flow.findIndex((st) => (st.type === "node" ? st.id === id : st.tracks.some((t) => t.nodes.includes(id))));
}

/** 节点 → 第几步（从 1 起；同一个并行组里的同号） */
export function stepNos(s: Structure): Record<string, number> {
  const out: Record<string, number> = {};
  s.flow.forEach((st, i) => {
    for (const n of st.type === "node" ? [st.id] : st.tracks.flatMap((t) => t.nodes)) out[n] = i + 1;
  });
  return out;
}

export function isRoot(s: Structure, id: string): boolean {
  return (upstreams(s)[id] ?? []).length === 0;
}
