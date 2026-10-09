/** 草稿结构编辑的纯函数（与后端 structure.normalize 同一套规则：删空轨道、单轨道并行组退化为顺序节点）。 */

import type { Definition, NodeConfig, Step } from "./types";

export function blankNode(name: string): NodeConfig {
  return {
    name,
    exec_role: "",
    output_name: "",
    artifact_file_template_id: null,
    exit_review: { role: "", rule: "any" },
    admit_review: null,
  };
}

export function nodeIds(flow: Step[]): string[] {
  return flow.flatMap((s) => (s.type === "node" ? [s.id] : s.tracks.flatMap((t) => t.nodes)));
}

function nextId(def: Definition): string {
  const max = Math.max(0, ...Object.keys(def.nodes).map((k) => Number(k.replace(/^n/, "")) || 0));
  return `n${max + 1}`;
}

export function normalize(def: Definition): Definition {
  const flow: Step[] = [];
  for (const s of def.flow) {
    if (s.type === "node") flow.push(s);
    else {
      const tracks = s.tracks.filter((t) => t.nodes.length);
      if (tracks.length === 1) flow.push(...tracks[0]!.nodes.map((id) => ({ type: "node" as const, id })));
      else if (tracks.length >= 2) flow.push({ type: "parallel", tracks });
    }
  }
  const ids = new Set(nodeIds(flow));
  return { flow, nodes: Object.fromEntries(Object.entries(def.nodes).filter(([k]) => ids.has(k))) };
}

function clone(def: Definition): Definition {
  return JSON.parse(JSON.stringify(def)) as Definition;
}

/** 在第 index 步之后插入一个节点，返回 [新定义, 新节点 id]。 */
export function addNode(def: Definition, index: number): [Definition, string] {
  const d = clone(def);
  const id = nextId(d);
  d.nodes[id] = blankNode(`节点 ${Object.keys(d.nodes).length + 1}`);
  d.flow.splice(index + 1, 0, { type: "node", id });
  return [d, id];
}

export function addParallel(def: Definition, index: number): [Definition, string] {
  const d = clone(def);
  const a = nextId(d);
  d.nodes[a] = blankNode("并行节点 A");
  const b = nextId(d);
  d.nodes[b] = blankNode("并行节点 B");
  d.flow.splice(index + 1, 0, { type: "parallel", tracks: [{ nodes: [a] }, { nodes: [b] }] });
  return [d, a];
}

export function addTrack(def: Definition, index: number): [Definition, string] {
  const d = clone(def);
  const s = d.flow[index];
  const id = nextId(d);
  d.nodes[id] = blankNode(`轨道节点 ${id}`);
  if (s?.type === "parallel") s.tracks.push({ nodes: [id] });
  return [d, id];
}

export function addToTrack(def: Definition, index: number, track: number): [Definition, string] {
  const d = clone(def);
  const s = d.flow[index];
  const id = nextId(d);
  d.nodes[id] = blankNode(`轨道节点 ${id}`);
  if (s?.type === "parallel") s.tracks[track]?.nodes.push(id);
  return [d, id];
}

export function moveStep(def: Definition, index: number, delta: -1 | 1): Definition {
  const d = clone(def);
  const to = index + delta;
  if (to < 0 || to >= d.flow.length) return def;
  const [s] = d.flow.splice(index, 1);
  d.flow.splice(to, 0, s!);
  return d;
}

export function removeNode(def: Definition, id: string): Definition {
  const d = clone(def);
  d.flow = d.flow
    .filter((s) => !(s.type === "node" && s.id === id))
    .map((s) => (s.type === "parallel" ? { ...s, tracks: s.tracks.map((t) => ({ nodes: t.nodes.filter((n) => n !== id) })) } : s));
  return normalize(d);
}
