/**
 * 导航：一级放顶部，二级放左侧（可收缩）—— 与 Atlas、用户中心的 nav.ts 同一种结构。
 *
 * ★ 运营人员（管理员、观察员）看到的菜单一样：观察员只是不能改，不藏菜单。
 * ★ 二级菜单可以分组（系统 › 运行资源 / 配置）：group 是分组名，不是链接。
 */
import type { IconName } from "@/components/Icon";

export type PlatformId = "tenants" | "apps" | "system" | "ops";

export interface NavLink {
  href: string;
  icon: IconName;
  label: string;
  /** 高亮：前缀匹配（/tenants/xxx 也高亮「租户」） */
  match: (path: string) => boolean;
}

export type NavEntry = NavLink | { group: string };

export interface Platform {
  id: PlatformId;
  label: string;
  icon: IconName;
  items: NavEntry[];
}

const under = (prefix: string) => (p: string) => p === prefix || p.startsWith(`${prefix}/`);

export const PLATFORMS: Platform[] = [
  {
    id: "tenants",
    label: "租户",
    icon: "building",
    items: [
      { href: "/tenants", icon: "building", label: "租户", match: under("/tenants") },
      // 份额按企业定，和租户放一起（原来是一级菜单「容量」）
      { href: "/capacity", icon: "gauge", label: "份额与容量", match: under("/capacity") },
    ],
  },
  {
    id: "apps",
    label: "应用",
    icon: "grid",
    items: [{ href: "/apps", icon: "grid", label: "应用目录", match: under("/apps") }],
  },
  {
    id: "system",
    label: "系统",
    icon: "server",
    items: [
      { href: "/systems", icon: "server", label: "接入的系统", match: under("/systems") },
      { group: "运行资源" },
      { href: "/settings/templates", icon: "pkg", label: "沙箱模板", match: under("/settings/templates") },
      { href: "/settings/skills", icon: "store", label: "技能市场", match: under("/settings/skills") },
      { href: "/settings/scopes", icon: "brain", label: "记忆作用域", match: under("/settings/scopes") },
      { group: "配置" },
      { href: "/settings/params", icon: "sliders", label: "系统参数", match: under("/settings/params") },
      { href: "/settings/jobs", icon: "timer", label: "系统任务", match: under("/settings/jobs") },
    ],
  },
  {
    id: "ops",
    label: "运营",
    icon: "shield",
    items: [
      { href: "/operators", icon: "idcard", label: "运营账号", match: under("/operators") },
      { href: "/audit", icon: "scroll", label: "运营审计", match: under("/audit") },
    ],
  },
];

export const isLink = (e: NavEntry): e is NavLink => "href" in e;

/** 路径属于哪个平台（详情页按前缀归到列表项）；不在导航里的归第一个 */
export function platformOf(path: string): Platform {
  return PLATFORMS.find((p) => p.items.some((e) => isLink(e) && e.match(path))) ?? PLATFORMS[0]!;
}

/** 顶部点平台时去哪：第一个二级菜单 */
export function platformHome(p: Platform): string {
  return p.items.find(isLink)!.href;
}
