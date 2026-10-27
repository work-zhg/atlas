/** 记忆里的时间都是 UTC 的 ISO 串：按浏览器本地时区显示。 */

export function localDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function localDate(iso: string | null | undefined): string {
  return localDateTime(iso).slice(0, 10);
}
