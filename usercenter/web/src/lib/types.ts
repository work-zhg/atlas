/** 与后端 api/views.py 对应的类型。所有 id 都是逻辑主键 uuid。 */

export type UserStatus = "active" | "pending" | "locked" | "disabled";

export interface UserBrief {
  id: string;
  name: string;
  account: string;
  status: string;
}

export interface MenuNode {
  code?: string;
  name: string;
  icon?: string | null;
  path?: string;
  children?: MenuNode[];
}

export interface Me {
  user: User & { dept_path: string[] };
  must_change_password: boolean;
  first_login: boolean;
  manager: (UserBrief & { source: string }) | null;
  permissions: string[];
  menus: MenuNode[];
  password_policy: string;
}

export interface User {
  id: string;
  account: string;
  name: string;
  email: string;
  phone: string | null;
  dept_id: string;
  status: UserStatus;
  must_change_password: boolean;
  last_login_at: string | null;
  created_at: string | null;
  disabled_reason: string | null;
  version: number;
  dept_path?: string[];
  positions?: string[];
  uc_roles?: string[];
  manager?: (UserBrief & { source: string }) | null;
  is_leader?: boolean;
}

export interface DeptNode {
  id: string;
  parent_id: string | null;
  name: string;
  sort: number;
  depth: number;
  leader: UserBrief | null;
  direct_count: number;
  total_count: number;
}

export interface DeptDetail {
  id: string;
  parent_id: string | null;
  name: string;
  path: string[];
  depth: number;
  leader: UserBrief | null;
  direct_count: number;
  total_count: number;
  children: { id: string; name: string }[];
  version: number;
}

export interface AppInfo {
  id: string;
  name: string;
  icon: string | null;
  description: string | null;
  app_key: string | null;
  secret_tail: string | null;
  scope_all: boolean;
  status: "active" | "disabled";
  is_builtin: boolean;
  created_at: string | null;
  version: number;
  operations?: number;
  menus?: number;
  roles?: number;
  data_types?: number;
  accessible_users?: number;
  scope_dept_ids?: string[];
}

export interface Operation {
  id: string;
  module: string;
  code: string;
  name: string;
  source: string;
  roles?: string[];
}

export interface MenuItem {
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

export interface Role {
  id: string;
  app_id: string;
  code: string;
  name: string;
  description: string | null;
  is_builtin: boolean;
  version: number;
  operation_count?: number;
  holder_count?: number;
  operation_ids?: string[];
  menu_ids?: string[];
}

export interface DataTypeInfo {
  id: string;
  app_id: string;
  code: string;
  name: string;
  description: string | null;
  admin_operation_code?: string | null;
  data_count?: number;
}

export interface GrantRow {
  id: string;
  kind: "role" | "position";
  target: { id: string; name: string; code: string; app?: string };
  subject:
    | { type: "user"; id: string; name: string; account: string; status: string }
    | { type: "dept"; id: string; name: string; path: string[] };
  include_sub: boolean;
  covered: number;
  granted_by: string | null;
  granted_at: string;
}

export interface PositionRow {
  id: string;
  code: string;
  name: string;
  version: number;
  roles: { id: string; name: string; code: string; app?: string }[];
  grants: GrantRow[];
  holders: number;
}

export type Level = "READ" | "WRITE" | "OWNER" | "NONE";

export interface DataRow {
  id: string;
  data_code: string;
  data_type_name: string;
  app: string;
  data_id: string;
  data_name: string | null;
  my_level: Level;
  owners: string[];
  user_grants: number;
  dept_grants: number;
  created_at: string;
}

export interface Page<T> {
  total: number;
  items: T[];
}
