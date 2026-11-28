"use client";

/**
 * 提交评审：从流程空间勾选这一轮要评审的产出物（至少一个）。只记路径，产物不做版本管理。
 *
 * ★ 默认打开本节点的目录（约定产出写在「节点名/」下），也能进别的目录选。
 */
import { useQueryClient } from "@tanstack/react-query";
import { App, Checkbox, Modal, Spin } from "antd";
import { useState } from "react";

import { api, errorText } from "@/lib/api";
import { RULE } from "@/lib/format";
import { useSpace } from "@/lib/queries";
import type { ProcessDetail, ProcessNode } from "@/lib/types";

import { Icon } from "./ui";

export function SubmitModal({ process, node, onClose }: { process: ProcessDetail; node: ProcessNode; onClose: () => void }) {
  const { message } = App.useApp();
  const qc = useQueryClient();
  const [dir, setDir] = useState(node.name);
  const space = useSpace(process.id, dir);
  const [sel, setSel] = useState<string[]>(node.latest_submission?.paths ?? []);
  const [busy, setBusy] = useState(false);
  const spec = process.structure.nodes[node.id];
  const parts = dir ? dir.split("/") : [];

  const toggle = (p: string) => setSel((s) => (s.includes(p) ? s.filter((x) => x !== p) : [...s, p]));
  const go = async () => {
    setBusy(true);
    try {
      await api.post(`/processes/${process.id}/nodes/${node.id}/submissions`, { paths: sel });
      await qc.invalidateQueries({ queryKey: ["process", process.id] });
      await qc.invalidateQueries({ queryKey: ["node", process.id, node.id] });
      onClose();
    } catch (e) {
      message.error(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      width={640}
      title={`提交评审 · ${node.name} 第 ${node.round} 轮`}
      onCancel={onClose}
      onOk={() => void go()}
      okText="提交评审"
      confirmLoading={busy}
      okButtonProps={{ disabled: !sel.length }}
    >
      <div className="sm muted">从流程空间勾选这一轮要评审的产出物。评审人看的是这些文件的当前内容，下游开工时拿到的也是它们</div>
      <div className="row sm" style={{ margin: "12px 0 6px" }}>
        <a onClick={() => setDir("")}>流程空间</a>
        {parts.map((seg, i) => (
          <span key={i} className="row" style={{ gap: 4 }}>
            <span className="faint">/</span>
            <a onClick={() => setDir(parts.slice(0, i + 1).join("/"))}>{seg}</a>
          </span>
        ))}
        <span className="sp" />
        <span className="xs muted">已选 {sel.length} 个</span>
      </div>
      <div className="col" style={{ gap: 6, maxHeight: 300, overflowY: "auto" }}>
        {space.isPending ? (
          <Spin />
        ) : space.error ? (
          <div className="sm muted">{errorText(space.error)}</div>
        ) : !space.data?.entries.length ? (
          <div className="sm muted">这个目录是空的{dir === node.name ? "：数字员工还没在「" + node.name + "/」下写文件" : ""}</div>
        ) : (
          space.data.entries.map((e) =>
            e.is_dir ? (
              <div key={e.path} className="pickrow" onClick={() => setDir(e.path)}>
                <Icon name="folder" size={15} />
                <span className="mono grow">{e.name}/</span>
                <Icon name="right" size={14} />
              </div>
            ) : (
              <div key={e.path} className={`pickrow ${sel.includes(e.path) ? "on" : ""}`} onClick={() => toggle(e.path)}>
                <Checkbox checked={sel.includes(e.path)} />
                <span className="mono grow">{e.path}</span>
              </div>
            ),
          )
        )}
      </div>
      {sel.length > 0 && (
        <div className="xs muted" style={{ marginTop: 8 }}>
          已选：{sel.join("、")}
        </div>
      )}
      <div className="banner info" style={{ marginTop: 14 }}>
        <Icon name="shield" size={15} />
        <div>
          提交后进入准出评审（{RULE[spec?.exit.rule ?? "any"]}），评审期间本节点会话不再写入。产物不做版本管理，只记下这一轮提交了哪些文件
        </div>
      </div>
    </Modal>
  );
}
