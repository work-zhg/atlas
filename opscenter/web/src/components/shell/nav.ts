/**
 * 导航：一级放顶部，二级放左侧（可收缩）—— 与 Atlas、用户中心的 nav.ts 同一种结构。
 *
 * ★ 按模块显示（c4-model/opscenter/design-opscenter.html）：一个菜单属于哪个模块，就看这个人在那个模块
 *   有没有只读以上的级别；没有就不显示。能看但不能改的，按钮置灰、不隐藏（各页面按级别判）。
 * ★ 二级菜单可以分组（系统 › 运行资源 / 配置）：group 是分组名，不是链接；组里没有能看的菜单就不显示组名。
 */
import type { IconName } from "@/components/Icon";
import { TENANT_MODULE, withCapability, type Access } from "@/lib/session";
import type { SystemInfo } from "@/lib/types";

export type PlatformId = "tenants" | "apps" | "system" | "ops";

export interface NavCtx {
  access: Access;
  systems: SystemInfo[];
}

export interface NavLink {
  href: string;
  icon: IconName;
  label: string;
  /** 高亮：前缀匹配（/tenants/xxx 也高亮「租户」） */
  match: (path: string) => boolean;
  /** 谁能看到：不给 = 进得了运营中心的都能看 */
  visible?: (c: NavCtx) => boolean;
}

export type NavEntry = NavLink | { group: string };

export interface Platform {
  id: PlatformId;
  label: string;
  icon: IconName;
  items: NavEntry[];
}

const under = (prefix: string) => (p: string) => p === prefix || p.startsWith(`${prefix}/`);

/** 声明了某种能力的系统里，有一个能看就显示 */
const anySystemWith = (cap: string) => (c: NavCtx) =>
  withCapability(c.systems, cap).some((s) => c.access.read(s.module));

export const PLATFORMS: Platform[] = [
  {
    id: "tenants",
    label: "租户",
    icon: "building",
    items: [
      { href: "/tenants", icon: "building", label: "租户", match: under("/tenants"), visible: (c) => c.access.read(TENANT_MODULE) },
      // 份额按企业定，和租户放一起；份额存在各系统里，看的是那些系统的模块
      { href: "/capacity", icon: "gauge", label: "份额与容量", match: under("/capacity"), visible: anySystemWith("capacity") },
    ],
  },
  {
    id: "apps",
    label: "应用",
    icon: "grid",
    items: [{ href: "/apps", icon: "grid", label: "应用目录", match: under("/apps"), visible: anySystemWith("user_center") }],
  },
  {
    id: "system",
    label: "系统",
    icon: "server",
    items: [
      { href: "/systems", icon: "server", label: "接入的系统", match: under("/systems") },
      { group: "运行资源" },
      { href: "/settings/templates", icon: "pkg", label: "沙箱模板", match: under("/settings/templates"), visible: anySystemWith("runtime_env") },
      { href: "/settings/skills", icon: "store", label: "技能市场", match: under("/settings/skills"), visible: anySystemWith("skill_market") },
      { href: "/settings/scopes", icon: "brain", label: "记忆作用域", match: under("/settings/scopes"), visible: anySystemWith("memory_scopes") },
      { group: "配置" },
      { href: "/settings/params", icon: "sliders", label: "系统参数", match: under("/settings/params"), visible: anySystemWith("settings") },
      { href: "/settings/jobs", icon: "timer", label: "系统任务", match: under("/settings/jobs"), visible: anySystemWith("jobs") },
    ],
  },
  {
    id: "ops",
    label: "运营",
    icon: "shield",
    // 运营人员、角色、模块授权都在用户中心里管（系统租户的管理员）；这里只剩运营审计
    items: [{ href: "/audit", icon: "scroll", label: "运营审计", match: under("/audit"), visible: (c) => c.access.canAudit }],
  },
];

export const isLink = (e: NavEntry): e is NavLink => "href" in e;

const canSee = (e: NavLink, c: NavCtx) => (e.visible ? e.visible(c) : true);

/** 一个平台里这个人能看到的二级菜单（组里没有能看的菜单，组名也不要） */
export function visibleItems(p: Platform, c: NavCtx): NavEntry[] {
  const out: NavEntry[] = [];
  let pendingGroup: NavEntry | null = null;
  for (const e of p.items) {
    if (!isLink(e)) {
      pendingGroup = e;
      continue;
    }
    if (!canSee(e, c)) continue;
    if (pendingGroup) {
      out.push(pendingGroup);
      pendingGroup = null;
    }
    out.push(e);
  }
  return out;
}

/** 有能看的菜单的平台 */
export function visiblePlatforms(c: NavCtx): Platform[] {
  return PLATFORMS.filter((p) => visibleItems(p, c).some(isLink));
}

/** 路径属于哪个平台（详情页按前缀归到列表项）；不在导航里的归第一个 */
export function platformOf(path: string): Platform {
  return PLATFORMS.find((p) => p.items.some((e) => isLink(e) && e.match(path))) ?? PLATFORMS[0]!;
}

/** 顶部点平台时去哪：第一个能看的二级菜单 */
export function platformHome(p: Platform, c: NavCtx): string {
  return (visibleItems(p, c).find(isLink) as NavLink | undefined)?.href ?? "/systems";
}

/** 进运营中心后的首页：第一个能看的菜单 */
export function homeOf(c: NavCtx): string {
  const first = visiblePlatforms(c)[0];
  return first ? platformHome(first, c) : "/systems";
}
