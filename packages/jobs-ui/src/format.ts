/** 展示格式：周期、耗时、时刻、相对时间。实例任务多是秒级周期，时刻精确到秒。 */
import { useEffect, useState } from "react";

import type { InstanceJobOut } from "./api";

/** 秒 → 「10 秒」「5 分钟」「2 小时」；不整除就用秒 */
export function seconds(s: number): string {
  if (s >= 3600 && s % 3600 === 0) return `${s / 3600} 小时`;
  if (s >= 60 && s % 60 === 0) return `${s / 60} 分钟`;
  return `${Number(s.toFixed(2))} 秒`;
}

/** 触发规则：每 10 秒 ± 3 秒 */
export function schedule(job: InstanceJobOut): string {
  return job.jitter > 0 ? `每 ${seconds(job.every)} ± ${seconds(job.jitter)}` : `每 ${seconds(job.every)}`;
}

export function duration(s: number): string {
  if (s < 1) return `${Math.round(s * 1000)} 毫秒`;
  if (s < 60) return `${s.toFixed(1)} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${Math.round(s - m * 60)} 秒`;
}

/** 时刻：今天的只显示时分秒 */
export function clock(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  const hms = `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  return d.toDateString() === new Date().toDateString() ? hms : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hms}`;
}

/** 相对现在：12 秒前 / 约 8 秒后 */
export function relative(iso: string, now: number): string {
  const diff = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(diff);
  const text =
    abs < 1 ? "" : abs < 60 ? `${Math.round(abs)} 秒` : abs < 3600 ? `${Math.round(abs / 60)} 分钟` : `${Math.round(abs / 3600)} 小时`;
  if (!text) return diff > 0 ? "马上" : "刚刚";
  return diff > 0 ? `约 ${text}后` : `${text}前`;
}

/**
 * 下一轮：约 8 秒后。★ 数据每 5 秒才刷新一次，而「现在」每秒走：计划时刻过了、新数据还没到时
 * 显示「马上」，不显示成「3 秒前」。
 */
export function upcoming(iso: string, now: number): string {
  return new Date(iso).getTime() - now < 1000 ? "马上" : relative(iso, now);
}

/** 每秒走一次的「现在」：相对时间、已跑多久不用等下一次刷新 */
export function useNow(intervalMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}
