/**
 * 导航：一级按平台放顶部，二级放左侧（可收缩）。
 *
 * ★ 显不显示只是体验，权限在服务端。各平台的可见条件沿用原来的规则：
 *   技能 / MCP / 模型是用户中心里的独立应用，连得上（在它的可访问范围内）才显示。
 */
import type { IconName } from "@/components/Icon";
import type { GwMe } from "@/api/mcp-gateway";
import type { MgMe } from "@/api/model-gateway";
import type { SkMe } from "@/api/skill-types";
import { can, type Me } from "@/lib/session";

export type PlatformId = "ai" | "kb" | "skills" | "mcp" | "models" | "sys";

export interface Access {
  me?: Me;
  sk?: SkMe;
  gw?: GwMe;
  mg?: MgMe;
}

export interface NavItem {
  href: string;
  icon: IconName;
  label: string;
  /** 高亮：前缀匹配（/agents/xxx 也高亮「智能体」） */
  match: (path: string) => boolean;
  visible?: (a: Access) => boolean;
}

export interface Platform {
  id: PlatformId;
  label: string;
  icon: IconName;
  visible: (a: Access) => boolean;
  items: NavItem[];
}

const under = (prefix: string) => (p: string) => p === prefix || p.startsWith(`${prefix}/`);

const jobsVisible = (a: Access) => can(a.me, "job:manage") || !!a.gw?.can_manage || !!a.mg?.can_manage;

export const PLATFORMS: Platform[] = [
  {
    id: "ai",
    label: "智能平台",
    icon: "zap",
    visible: (a) => can(a.me, "chat:use"),
    items: [
      { href: "/agents", icon: "bot", label: "智能体", match: under("/agents") },
      { href: "/workspace", icon: "folder", label: "工作空间", match: under("/workspace") },
      { href: "/me", icon: "user", label: "个性化", match: under("/me") },
    ],
  },
  {
    id: "kb",
    label: "知识库",
    icon: "book",
    visible: (a) => can(a.me, "kb:create", "kb:review", "agent:create"),
    items: [
      {
        href: "/knowledge",
        icon: "book",
        label: "知识库",
        match: (p) => under("/knowledge")(p) && !under("/knowledge/review")(p),
      },
      {
        href: "/knowledge/review",
        icon: "check",
        label: "待审核",
        match: under("/knowledge/review"),
        visible: (a) => can(a.me, "kb:review"),
      },
    ],
  },
  {
    id: "skills",
    label: "技能",
    icon: "spark",
    visible: (a) => !!a.sk,
    items: [
      { href: "/skills", icon: "grid", label: "技能广场", match: (p) => p === "/skills" },
      { href: "/skills/mine", icon: "upload", label: "我上传的", match: under("/skills/mine") },
      {
        href: "/skills/review",
        icon: "shield",
        label: "待审查",
        match: under("/skills/review"),
        visible: (a) => !!a.sk?.can_review,
      },
    ],
  },
  {
    id: "mcp",
    label: "MCP",
    icon: "plug",
    visible: (a) => !!a.gw,
    items: [
      {
        href: "/mcp",
        icon: "plug",
        label: "服务",
        match: (p) => p === "/mcp" || under("/mcp/services")(p),
      },
      { href: "/mcp/keys", icon: "key", label: "我的 Key", match: under("/mcp/keys") },
      {
        href: "/mcp/agent-keys",
        icon: "bot",
        label: "智能体 Key",
        match: under("/mcp/agent-keys"),
        visible: (a) => !!a.gw?.can_manage,
      },
    ],
  },
  {
    id: "models",
    label: "模型",
    icon: "layers",
    visible: (a) => !!a.mg,
    items: [
      { href: "/models", icon: "layers", label: "模型", match: (p) => p === "/models" },
      { href: "/models/keys", icon: "key", label: "我的 Key", match: under("/models/keys") },
      {
        href: "/models/client-keys",
        icon: "bot",
        label: "客户端 Key",
        match: under("/models/client-keys"),
        visible: (a) => !!a.mg?.can_manage,
      },
    ],
  },
  {
    id: "sys",
    label: "系统",
    icon: "sliders",
    visible: (a) => can(a.me, "config:manage") || jobsVisible(a),
    items: [
      {
        href: "/system",
        icon: "star",
        label: "平台设置",
        match: (p) => p === "/system",
        visible: (a) => can(a.me, "config:manage"),
      },
      {
        href: "/system/configs",
        icon: "sliders",
        label: "配置项",
        match: under("/system/configs"),
        visible: (a) => can(a.me, "config:manage"),
      },
      { href: "/system/jobs", icon: "clock", label: "定时任务", match: under("/system/jobs"), visible: jobsVisible },
    ],
  },
];

const PREFIX: [string, PlatformId][] = [
  ["/knowledge", "kb"],
  ["/skills", "skills"],
  ["/mcp", "mcp"],
  ["/models", "models"],
  ["/system", "sys"],
];

/** 路径属于哪个平台：没有专属前缀的（首页、对话、智能体、个性化）都在智能平台 */
export function platformOf(path: string): PlatformId {
  for (const [prefix, id] of PREFIX) if (under(prefix)(path)) return id;
  return "ai";
}

export function visibleItems(p: Platform, a: Access): NavItem[] {
  return p.items.filter((it) => !it.visible || it.visible(a));
}

/** 顶部点平台时去哪：智能平台是首页（输入框），其余是第一个能看到的二级菜单 */
export function platformHome(p: Platform, a: Access): string {
  if (p.id === "ai") return "/";
  return visibleItems(p, a)[0]?.href ?? "/";
}

/** 编辑智能体时左侧菜单默认收起，把宽度让给「配置 + 调试预览」 */
export function isEditorPath(path: string): boolean {
  return /^\/agents\/[^/]+$/.test(path);
}
