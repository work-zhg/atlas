/**
 * 导航：一级放顶部，二级放左侧（可收缩）—— 与 Atlas 的 components/shell/nav.ts 同一种结构。
 *
 * ★ 显不显示只是体验，权限在服务端。可见条件沿用后端给的两样东西：
 *   - 菜单编码（/auth/me 的 menus，由内置应用的菜单 + 登录人的角色算出）：org、users、apps；
 *   - 操作码（/auth/me 的 permissions）：「权限」下的角色、岗位、数据、授权各看各的查看权限。
 * ★ 个人中心人人可见（个人资料、账号安全、我的应用、我的数据）。
 */
import type { IconName } from "@/components/Icon";
import type { Me, MenuNode } from "@/lib/types";

export type PlatformId = "org" | "perm" | "apps" | "me";

export interface Access {
  menus: Set<string>;
  permissions: Set<string>;
}

export interface NavItem {
  href: string;
  icon: IconName;
  label: string;
  /** 高亮：前缀匹配（/users/xxx 也高亮「人员」） */
  match: (path: string) => boolean;
  visible: (a: Access) => boolean;
}

export interface Platform {
  id: PlatformId;
  label: string;
  icon: IconName;
  items: NavItem[];
}

const under = (prefix: string) => (p: string) => p === prefix || p.startsWith(`${prefix}/`);
const menu = (code: string) => (a: Access) => a.menus.has(code);
const perm = (op: string) => (a: Access) => a.permissions.has(op);
const always = () => true;

export const PLATFORMS: Platform[] = [
  {
    id: "org",
    label: "组织",
    icon: "sitemap",
    items: [
      { href: "/org", icon: "sitemap", label: "组织架构", match: under("/org"), visible: menu("org") },
      { href: "/users", icon: "users", label: "人员", match: under("/users"), visible: menu("users") },
      // 数字员工与人员同一份操作码（user:*），不进组织树（数字员工设计 §3.4）
      { href: "/digital", icon: "bot", label: "数字员工", match: under("/digital"), visible: menu("users") },
    ],
  },
  {
    id: "perm",
    label: "权限",
    icon: "shield",
    items: [
      { href: "/perm/roles", icon: "badge", label: "角色", match: under("/perm/roles"), visible: perm("role:view") },
      { href: "/perm/positions", icon: "briefcase", label: "岗位", match: under("/perm/positions"), visible: perm("position:view") },
      { href: "/perm/data", icon: "database", label: "数据", match: under("/perm/data"), visible: perm("data:view") },
      { href: "/perm/grants", icon: "shield", label: "授权", match: under("/perm/grants"), visible: perm("grant:view") },
    ],
  },
  {
    id: "apps",
    label: "应用",
    icon: "box",
    items: [{ href: "/apps", icon: "grid", label: "已开通的应用", match: under("/apps"), visible: menu("apps") }],
  },
  {
    id: "me",
    label: "个人中心",
    icon: "user",
    items: [
      { href: "/me", icon: "user", label: "个人资料", match: (p) => p === "/me", visible: always },
      { href: "/me/security", icon: "lock", label: "账号安全", match: under("/me/security"), visible: always },
      { href: "/me/apps", icon: "grid", label: "我的应用", match: under("/me/apps"), visible: always },
      { href: "/me/data", icon: "database", label: "我的数据", match: under("/me/data"), visible: always },
    ],
  },
];

function menuCodes(nodes: MenuNode[]): string[] {
  return nodes.flatMap((n) => (n.children ? menuCodes(n.children) : n.code ? [n.code] : []));
}

export function accessOf(me: Me): Access {
  return { menus: new Set(menuCodes(me.menus)), permissions: new Set(me.permissions) };
}

export function visibleItems(p: Platform, a: Access): NavItem[] {
  return p.items.filter((it) => it.visible(a));
}

export function visiblePlatforms(a: Access): Platform[] {
  return PLATFORMS.filter((p) => visibleItems(p, a).length > 0);
}

/** 路径属于哪个导航项（详情页按前缀归到列表项） */
export function itemOf(path: string): { platform: Platform; item: NavItem } | undefined {
  for (const platform of PLATFORMS) {
    const item = platform.items.find((it) => it.match(path));
    if (item) return { platform, item };
  }
  return undefined;
}

/** 顶部点平台时去哪：第一个能看到的二级菜单 */
export function platformHome(p: Platform, a: Access): string {
  return visibleItems(p, a)[0]?.href ?? "/me";
}

/** 登录后的落地页：第一个能看到的平台（管理员是组织，普通员工是个人中心） */
export function landingPath(me: Me): string {
  const a = accessOf(me);
  const first = visiblePlatforms(a)[0];
  return first ? platformHome(first, a) : "/me";
}

/** 当前路径能不能看：不在导航里的路径不拦（由页面自己处理） */
export function allowed(path: string, a: Access): boolean {
  const hit = itemOf(path);
  return !hit || hit.item.visible(a);
}
