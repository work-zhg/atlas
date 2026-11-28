"use client";

/**
 * 从其他文件夹选文件（步骤页「交付」里提交评审时用）：在流程文件里逐层进文件夹、勾文件。
 * 选好的交回给交付页签，一起提交。
 */
import { Checkbox, Modal, Spin } from "antd";
import { useState } from "react";

import { errorText } from "@/lib/api";
import { useSpace } from "@/lib/queries";

import { Icon } from "./ui";

export function PickFilesModal({
  processId,
  startDir,
  picked,
  onPick,
  onClose,
}: {
  processId: string;
  startDir: string;
  /** 已经选了的（打勾显示） */
  picked: string[];
  onPick: (paths: string[]) => void;
  onClose: () => void;
}) {
  const [dir, setDir] = useState(startDir);
  const space = useSpace(processId, dir);
  const [sel, setSel] = useState<string[]>([]);
  const parts = dir ? dir.split("/") : [];
  const on = (p: string) => sel.includes(p) || picked.includes(p);
  const toggle = (p: string) => {
    if (picked.includes(p)) return;
    setSel((s) => (s.includes(p) ? s.filter((x) => x !== p) : [...s, p]));
  };

  return (
    <Modal open width={600} title="从其他文件夹选" onCancel={onClose} onOk={() => onPick(sel)} okText={`加入（${sel.length}）`} okButtonProps={{ disabled: !sel.length }}>
      <div className="row sm" style={{ margin: "4px 0 8px" }}>
        <a onClick={() => setDir("")}>全部</a>
        {parts.map((seg, i) => (
          <span key={i} className="row" style={{ gap: 4 }}>
            <span className="faint">/</span>
            <a onClick={() => setDir(parts.slice(0, i + 1).join("/"))}>{seg}</a>
          </span>
        ))}
      </div>
      <div className="col" style={{ gap: 6, maxHeight: 340, overflowY: "auto" }}>
        {space.isPending ? (
          <Spin />
        ) : space.error ? (
          <div className="sm muted">{errorText(space.error)}</div>
        ) : !space.data?.entries.length ? (
          <div className="sm muted">这个文件夹是空的</div>
        ) : (
          space.data.entries.map((e) =>
            e.is_dir ? (
              <div key={e.path} className="pickrow" onClick={() => setDir(e.path)}>
                <Icon name="folder" size={15} />
                <span className="grow">{e.name}</span>
                <Icon name="right" size={14} />
              </div>
            ) : (
              <div key={e.path} className={`pickrow ${on(e.path) ? "on" : ""}`} onClick={() => toggle(e.path)}>
                <Checkbox checked={on(e.path)} disabled={picked.includes(e.path)} />
                <span className="grow ellipsis">{e.name}</span>
              </div>
            ),
          )
        )}
      </div>
    </Modal>
  );
}
