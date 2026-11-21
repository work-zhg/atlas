"use client";

/** 改份额的弹窗（默认份额、某个企业的份额共用）。存储按 GB 填，存的是字节；留空 = 不限。 */
import { InputNumber, Modal, Space } from "antd";
import { useState } from "react";

import { QUOTA_LABEL } from "@/components/common";
import { GB } from "@/lib/format";
import type { Quota } from "@/lib/types";

export function QuotaModal({
  title,
  quota,
  hint,
  onSave,
  onClose,
}: {
  title: string;
  quota: Quota;
  hint: string;
  onSave: (body: Record<string, number | null>) => Promise<unknown>;
  onClose: () => void;
}) {
  const [value, setValue] = useState<Record<string, number | null>>(() =>
    Object.fromEntries(
      Object.entries(quota).map(([k, v]) => [k, k === "storage_bytes" && v !== null && v !== undefined ? v / GB : (v ?? null)]),
    ),
  );
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      await onSave(Object.fromEntries(Object.entries(value).map(([k, v]) => [k, v === null ? null : k === "storage_bytes" ? Math.round(v * GB) : v])));
      onClose();
    } catch {
      // 出错提示由 onSave 给；弹窗留着让人改
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal open title={title} okText="保存" confirmLoading={busy} onCancel={onClose} onOk={() => void save()}>
      <Space direction="vertical" style={{ width: "100%" }}>
        {Object.entries(value).map(([k, v]) => (
          <div key={k} className="ops-row">
            <span style={{ width: 130 }}>{QUOTA_LABEL[k] ?? k}</span>
            <InputNumber
              min={0}
              placeholder="不限"
              value={v}
              addonAfter={k === "storage_bytes" ? "GB" : undefined}
              onChange={(n) => setValue({ ...value, [k]: (n as number | null) ?? null })}
              style={{ flex: 1 }}
            />
          </div>
        ))}
        <span className="ops-sub">{hint}</span>
      </Space>
    </Modal>
  );
}
