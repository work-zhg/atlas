/** 运营服务和各系统运营接口的返回形状（只有元数据和数字，没有任何租户的业务内容）。 */

export type Role = "ADMIN" | "OBSERVER";

export interface Me {
  id: string;
  account: string;
  name: string;
  role: Role;
  must_change_password: boolean;
}

export interface Operator {
  id: string;
  account: string;
  name: string;
  role: Role;
  status: "active" | "disabled";
  locked: boolean;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string;
}

export interface SystemInfo {
  code: string;
  name: string;
  capabilities: string[];
  key_configured: boolean;
}

export interface OperationBrief {
  id: string;
  kind: OperationKind;
  stuck: boolean;
}

export interface Tenant {
  id: string;
  code: string;
  name: string;
  status: "active" | "suspended" | "closed";
  usable: boolean;
  max_users: number | null;
  expires_at: string | null;
  created_at: string;
  version: number;
  users?: number;
  operation?: OperationBrief | null;
}

export type OperationKind = "OPEN" | "SUSPEND" | "RESUME" | "CLOSE";

export interface OperationStep {
  id: string;
  seq: number;
  system: string;
  action: string;
  status: "PENDING" | "DONE" | "FAILED";
  attempts: number;
  next_attempt_at: string;
  last_error: string | null;
  rejected: boolean;
  result: Record<string, unknown> | null;
  finished_at: string | null;
}

export interface Operation {
  id: string;
  tenant_id: string;
  tenant_code: string;
  kind: OperationKind;
  status: "RUNNING" | "DONE";
  reason: string | null;
  requested_by: string | null;
  created_at: string;
  finished_at: string | null;
  stuck: boolean;
  steps: OperationStep[];
}

export interface OpenResult {
  operation: Operation;
  tenant: Tenant | null;
  admin: { id: string; account: string; name: string } | null;
  temp_password: string | null;
  message: string | null;
}

export interface AppInfo {
  id: string;
  name: string;
  code: string | null;
  icon: string | null;
  description: string | null;
  app_key: string | null;
  secret_tail: string | null;
  status: "active" | "disabled";
  is_builtin: boolean;
  home_url: string | null;
  created_at: string;
  version: number;
  operations?: number;
  menus?: number;
  roles?: number;
  data_types?: number;
}

export interface OpInfo {
  id: string;
  module: string;
  code: string;
  name: string;
  source: string;
  roles?: string[];
}

export interface MenuInfo {
  id: string;
  parent_id: string | null;
  type: "dir" | "menu";
  code: string;
  name: string;
  icon: string | null;
  path: string | null;
  is_public: boolean;
  sort: number;
  source: string;
  role_count?: number;
}

export interface DataTypeInfo {
  id: string;
  app_id: string;
  code: string;
  name: string;
  description: string | null;
  data_count?: number;
}

export interface RoleInfo {
  id: string;
  app_id: string;
  code: string;
  name: string;
  description: string | null;
  is_builtin: boolean;
  shared: boolean;
  version: number;
  operation_count?: number;
  operation_ids?: string[];
  menu_ids?: string[];
}

export interface TenantApp {
  app: { id: string; name: string; code: string | null; icon: string | null; status: string };
  enabled: boolean;
  enabled_at: string | null;
}

/** 份额：null = 不限。Atlas 有三项，知识库只有存储 */
export interface Quota {
  running_runs?: number | null;
  sandboxes?: number | null;
  storage_bytes?: number | null;
}

export interface TenantUsage {
  tenant_uuid: string;
  quota: Quota;
  is_default: boolean;
  usage: { running_runs?: number; sandboxes?: number; storage_bytes?: number };
}

export interface Quotas {
  default: Quota;
  tenants: (Quota & { tenant_uuid: string; updated_by: string; updated_at: string })[];
}

export interface CapacityTotal {
  runs_per_instance?: number;
  usage: { running_runs?: number; sandboxes?: number; storage_bytes?: number };
}

export interface AuditEntry {
  id: string;
  at: string;
  operator_id: string | null;
  operator: string | null;
  action: string;
  target: string | null;
  tenant_id: string | null;
  detail: Record<string, unknown> | null;
  result: string | null;
  finished_at: string | null;
}

export interface SandboxTemplate {
  template_uuid: string;
  code: string;
  description: string;
  server: string;
  version: number;
  published_version: number | null;
  published_at: string | null;
  updated_by: string;
  yaml?: string;
  published_sha?: string | null;
  drifted?: boolean | null;
}

export interface MarketVersion {
  slug: string;
  version: number;
  status: "published" | "revoked";
  description: string;
  size_bytes: number;
  file_count: number;
  has_scripts: boolean;
  status_reason: string | null;
  published_by: string;
  published_at: string;
}

export interface MarketSkill {
  slug: string;
  install_by_default: boolean;
  latest: number | null;
  description: string | null;
  versions: MarketVersion[];
}

export interface MemoryScope {
  code: string;
  description: string;
  key_fields: string[];
  check_permission: boolean;
  data_code: string | null;
  created_at: string;
  updated_at: string;
}
