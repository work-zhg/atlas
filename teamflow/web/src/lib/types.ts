export type Level = "NONE" | "READ" | "WRITE" | "OWNER";

export interface MenuNode {
  code?: string;
  name: string;
  icon?: string | null;
  path?: string | null;
  children?: MenuNode[];
}

export interface Me {
  user: { id: string; account: string; name: string; email: string | null };
  roles: string[];
  permissions: string[];
  menus: MenuNode[];
}

export interface Issue {
  message: string;
  node: string | null;
  field?: string | null;
}

// ───────────── 文件模板

export type Usage = "artifact" | "review_rule" | "other";

export interface FileVersion {
  id: string;
  version_no: number;
  file_name: string;
  size_bytes: number;
  sha256: string;
  change_note: string;
  uploaded_by: string | null;
  uploaded_by_name?: string | null;
  uploaded_at: string;
  content?: string;
}

export interface FileTemplate {
  id: string;
  name: string;
  icon: string | null;
  description: string | null;
  usage: Usage;
  status: "active" | "disabled";
  version: number;
  current_version: FileVersion | null;
  updated_at: string;
  reference_count?: number;
  versions?: FileVersion[];
  references?: { flow_template_id: string; flow_template: string; node_id: string; node: string; in: string }[];
  content?: string | null;
}

// ───────────── 流程模板

export interface Review {
  role: string;
  rule: "any" | "all";
}

export interface NodeConfig {
  name: string;
  exec_role: string;
  output_name: string;
  artifact_file_template_id: string | null;
  exit_review: Review;
  admit_review: Review | null;
}

export type Step = { type: "node"; id: string } | { type: "parallel"; tracks: { nodes: string[] }[] };

export interface Definition {
  flow: Step[];
  nodes: Record<string, NodeConfig>;
}

export interface FlowVersion {
  id: string;
  label: string;
  roles: { exec: string[]; review: string[] };
  change_note: string;
  published_by: string | null;
  published_by_name?: string | null;
  published_at: string;
  definition?: Definition;
  deps?: Record<string, string[]>;
}

export interface Draft {
  definition: Definition;
  deps: Record<string, string[]>;
  base_version_id: string | null;
  editor_id: string | null;
  editor_name: string | null;
  editing_by_me: boolean;
  updated_at: string;
  version: number;
  errors: Issue[];
  warnings: Issue[];
}

export interface FlowTemplate {
  id: string;
  name: string;
  icon: string | null;
  description: string | null;
  scope: string | null;
  builtin: boolean;
  status: "active" | "disabled";
  version: number;
  current_version: FlowVersion | null;
  updated_at: string;
  node_count: number;
  project_count: number;
  has_draft: boolean;
  draft_editor_name?: string | null;
  definition?: Definition | null;
  deps?: Record<string, string[]>;
  versions?: FlowVersion[];
  projects?: Project[];
  draft?: Draft | null;
}

// ───────────── 团队与项目

export interface Subject {
  type: "USER" | "DEPT";
  id: string;
  name: string;
  account?: string;
}

export interface Grant {
  id: string;
  permission: Level;
  subject: Subject;
  include_sub: boolean;
}

export interface Holder {
  user: { id: string; account: string; name: string };
  permission: Level;
  roles?: string[];
}

export interface TeamAgent {
  id: string;
  atlas_agent_id: string;
  name: string;
  avatar_key: string | null;
  description: string | null;
  model: string | null;
  available: boolean;
  synced_at: string;
  roles?: string[];
}

export interface Team {
  id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  version: number;
  my_level?: Level;
  member_count?: number;
  agent_count?: number;
  project_count?: number;
  agents?: { name: string; avatar_key: string | null; available: boolean }[];
  warnings?: string[];
}

export interface TeamDetail extends Omit<Team, "agents"> {
  can: { edit: boolean; member: boolean; agent: boolean; create: boolean };
  members: { grants: Grant[]; holders: Holder[]; can_manage: boolean };
  agents: TeamAgent[];
  projects: Project[];
}

export interface Project {
  id: string;
  team_id: string;
  name: string;
  description: string | null;
  flow_template_id: string | null;
  status: "active" | "archived";
  version: number;
  updated_at: string;
  template_name?: string | null;
  template_version?: string | null;
  completion?: { total: number; done: number };
}

export interface RoleRow {
  name: string;
  kind: "exec" | "review";
  users: { id: string; name: string; account: string | null; is_member: boolean }[];
  agents: { id: string; name: string; available: boolean }[];
  problems: string[];
  nodes: { node: string; as: string; rule?: "any" | "all" }[];
}

export interface ProjectRoles {
  template: { id: string; name: string; status: string; version: string } | null;
  roles: RoleRow[];
  incomplete: number;
}

export interface ProjectDetail extends Project {
  team: Team;
  can: { configure: boolean; archive: boolean };
  roles: ProjectRoles;
  hints: { kind: string; message: string }[];
}

export interface DirUser {
  id: string;
  account: string;
  name: string;
  dept_id: string;
}

export interface DirDept {
  id: string;
  parent_id: string | null;
  name: string;
}

// ───────────── 流程运行

export type NodeStatus = "pending" | "working" | "exit_review" | "admit_review" | "passed" | "returned";

export interface ProcessSummary {
  id: string;
  no: number;
  title: string;
  status: "running" | "completed" | "terminated";
  started_by: string | null;
  started_at: string;
  finished_at: string | null;
  progress: { passed: number; total: number };
  waiting: { node: string; stage: string; who: string }[];
  notices: string[];
}

export interface ProcessNodeState {
  status: NodeStatus;
  stage: string;
  round: number;
  notice: string | null;
  agent: string | null;
  agent_running: boolean;
  artifact: { version: number; round: number; commit: string } | null;
  executor: boolean;
  reviewer: boolean;
}

export interface ProcessDetail extends ProcessSummary {
  requirement: string;
  team_id: string;
  team_name: string;
  project_id: string;
  project_name: string;
  template_version: string;
  definition: Definition;
  deps: Record<string, string[]>;
  nodes: Record<string, ProcessNodeState>;
  event_seq: number;
  can: { terminate: boolean };
}

export interface NodeDetail {
  node_id: string;
  name: string;
  config: NodeConfig;
  status: NodeStatus;
  stage: string;
  round: number;
  notice: string | null;
  agent_running: boolean;
  file_template: { id: string; name: string; version: number } | null;
  inputs: { node_id: string; node: string; output: string; version: number | null }[];
  upstream: string[];
  messages: {
    id: string;
    role: "user" | "agent" | "system";
    author: string | null;
    content: string;
    status: "done" | "queued" | "streaming" | "failed";
    round: number;
    created_at: string;
  }[];
  artifacts: {
    version: number;
    round: number;
    commit: string;
    path: string;
    source: "agent" | "user";
    by: string | null;
    note: string | null;
    current: boolean;
    url: string | null;
    created_at: string;
  }[];
  current_content: string | null;
  reviews: {
    id: string;
    stage: "exit" | "admit";
    round: number;
    role: string;
    rule: "any" | "all";
    status: "open" | "passed" | "rejected" | "cancelled";
    artifact_version: number | null;
    reviewers: { id: string; name: string }[];
    votes: { user: string | null; user_id: string; decision: "approve" | "reject"; comment: string | null; return_to: string | null; created_at: string }[];
    opened_at: string;
    closed_at: string | null;
  }[];
  can: { handle: boolean; vote: boolean; resubmit: boolean };
}

export interface TodoItem {
  kind: "work" | "exit_review" | "admit_review";
  team_id: string;
  project_id: string;
  process_id: string;
  process_no: number;
  process_title: string;
  node_id: string;
  node_name: string;
  since: string | null;
}
