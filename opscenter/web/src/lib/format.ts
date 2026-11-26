export function dateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("zh-CN", { hour12: false });
}

export function bytes(n: number | null | undefined): string {
  if (n === null || n === undefined) return "不限";
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 10 ? 0 : 1)} ${units[i]}`;
}

export function limit(n: number | null | undefined): string {
  return n === null || n === undefined ? "不限" : String(n);
}

export const GB = 1024 * 1024 * 1024;

export const KIND_LABEL: Record<string, string> = { OPEN: "开通", SUSPEND: "停用", RESUME: "恢复", CLOSE: "关闭", BACKFILL: "补建" };

export const ACTION_LABEL: Record<string, string> = {
  CREATE_TENANT: "建租户和首个管理员",
  CREATE: "创建",
  DELETE: "删除数据",
  // 升级前排的步骤（业务系统改成只有创建、删除之前）
  SET_QUOTA: "设份额",
  INSTALL_SKILLS: "装默认技能",
  BLOCK: "挡住入口",
  UNBLOCK: "放开入口",
  SUSPEND: "挂起",
  RESUME: "恢复",
  PURGE: "删除数据",
  DELETE_TENANT: "删除租户",
};
