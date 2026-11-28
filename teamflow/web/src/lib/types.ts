/** 后端接口的形状（atlas_teamflow/api/views.py）。uuid 都是字符串，时间是 ISO 8601。 */

export type Level = "OWNER" | "WRITE" | "READ" | "NONE";
export type MarketLevel = "maintain" | "view";

export interface Me {
  id: string;
  name: string;
  tenant_id: string;
  operations: string[];
  menus: unknown[];
  system_tenant: boolean;
  all_teams_level: Level;
  market: MarketLevel;
}

export interface Person {
  id: string;
  name: string;
  account?: string;
  dept?: string | null;
}

// ───────────────────────────── 团队

export interface Team {
  id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  version: number;
  my_level: Level | null;
  created_at: string;
}

export interface TeamMember {
  id: string;
  name: string;
  account: string;
  level: Level;
}

/** 用户中心里团队这条数据的授权行（人或部门）：改级别、移除按授权行。 */
export interface TeamGrant {
  id: string;
  subject: { type: "USER" | "DEPT"; id: string; name?: string };
  permission: Level;
  include_sub?: boolean;
}

export interface TeamMembers {
  grants: TeamGrant[];
  members: TeamMember[];
}

export interface TeamAgent {
  id: string;
  atlas_agent_id: string;
  name: string;
  kind: string;
  model: string | null;
  description: string | null;
  available: boolean;
  added_by: string;
  synced_at: string | null;
  stats: { working: number; passed: number; avg_rounds: number | null };
}

export interface AgentCard {
  atlas_agent_id: string;
  name: string;
  kind: string;
  model: string | null;
  description: string | null;
  skills: string[];
  available: boolean;
}

export interface AgentCandidate extends AgentCard {
  added: boolean;
}

export interface TeamChannels {
  wecom: boolean;
  dingtalk: boolean;
  email_digest: boolean;
}

// ───────────────────────────── 流程结构（模板草稿、版本、市场版本共用）

export type Rule = "any" | "all";

export interface ReviewSpec {
  roles: string[];
  rule: Rule;
}

export interface NodeSpec {
  name: string;
  instructions: string;
  exec_role: string;
  admit: ReviewSpec | null;
  exit: ReviewSpec;
}

export type Step = { type: "node"; id: string } | { type: "parallel"; tracks: { nodes: string[] }[] };

export interface Structure {
  roles: Record<string, string>;
  flow: Step[];
  nodes: Record<string, NodeSpec>;
}

export interface Issue {
  level: "error" | "warn";
  message: string;
  node: string | null;
  field: string | null;
}

// ───────────────────────────── 流程模板与市场

export interface Template {
  id: string;
  team_id: string;
  name: string;
  description: string | null;
  icon: string | null;
  status: "active" | "archived";
  latest_version_no: number;
  origin: { market_template_id: string; version: number } | null;
  updated_at: string | null;
  draft?: {
    structure: Structure;
    rev: number;
    base_version_no: number;
    editor: string | null;
    updated_at: string | null;
  };
  versions?: { id: string; version_no: number; note: string | null; published_by: string; published_at: string }[];
}

export type MarketCategory = "rd" | "content" | "legal" | "data";

export interface MarketTemplate {
  id: string;
  name: string;
  category: MarketCategory;
  category_name: string;
  description: string | null;
  who: string | null;
  icon: string | null;
  color: string | null;
  status: "draft" | "listed" | "unlisted";
  latest_version: number;
  structure?: Structure;
  published_at?: string | null;
  draft?: Structure | null;
  version?: number;
  copies?: number;
  tenants?: number;
}

// ───────────────────────────── 项目

export interface Project {
  id: string;
  team_id: string;
  name: string;
  description: string | null;
  status: "active" | "archived";
  version: number;
  created_at: string;
  running?: number;
  todos?: number;
}

export interface Binding {
  template: Template;
  version_no: number;
  roles_of_template: Record<string, string>;
  exec_roles: string[];
  assignments: Record<string, { members: string[]; agent: string | null }>;
  ready: boolean;
  missing: { role: string; what: "no_member" | "no_agent" | string }[];
  running: number;
}

export interface ProjectDetail extends Project {
  bindings: Binding[];
}

// ───────────────────────────── 流程

export type NodeStatus = "pending" | "working" | "exit_review" | "exited" | "admit_review" | "passed" | "returned";
export type ProcessStatus = "running" | "completed" | "terminated";

export interface Process {
  id: string;
  no: number;
  code: string;
  title: string;
  team_id: string;
  project_id: string;
  template_id: string;
  template_version_id: string;
  space_id: string;
  status: ProcessStatus;
  started_by: string;
  started_at: string;
  finished_at: string | null;
}

export interface ProcessRow extends Process {
  nodes: { id: string; name: string; status: NodeStatus; round: number }[];
  waiting_for_me: boolean;
}

export interface Submission {
  id: string;
  round: number;
  paths: string[];
  submitted_by: string;
  submitted_at: string;
}

export interface NodeSession {
  thread_id: string | null;
  atlas_agent_id: string | null;
  state: "active" | "taken_over" | "frozen" | "stopped" | null;
  taken_by?: string | null;
  last_run_id?: string | null;
  can_send: boolean;
}

export interface NodeRow {
  id: string;
  name: string;
  exec_role: string;
  status: NodeStatus;
  round: number;
  atlas_agent_id: string | null;
  started_at: string | null;
  passed_at: string | null;
}

export interface ProcessNode extends NodeRow {
  executors: string[];
  i_am_executor: boolean;
  session: NodeSession;
  open_review: { stage: "admit" | "exit"; reviewers: string[]; i_can_vote: boolean } | null;
  latest_submission: Submission | null;
}

export interface ProcessDetail extends Process {
  structure: Structure;
  nodes: ProcessNode[];
  roles: Record<string, string[]>;
  can_run: boolean;
  can_terminate: boolean;
}

export interface Vote {
  reviewer: string;
  decision: "approve" | "reject";
  comment: string | null;
  return_to: string | null;
  at: string;
}

export interface Review {
  id: string;
  stage: "admit" | "exit";
  round: number;
  roles: string[];
  rule: Rule;
  reviewers: string[];
  submissions: string[];
  status: "open" | "approved" | "rejected" | "cancelled";
  opened_at: string;
  closed_at: string | null;
  votes: Vote[];
}

export interface Instruction {
  id: string;
  kind: "kickoff" | "revise" | "human" | "handback";
  round: number;
  author: string | null;
  status: "queued" | "sent" | "cancelled";
  run_id: string | null;
  sent_at: string | null;
  last_error: string | null;
  created_at: string;
}

export interface NodeDetail extends NodeRow {
  process: Process;
  executors: string[];
  i_am_executor: boolean;
  session: NodeSession;
  submissions: Submission[];
  reviews: Review[];
  instructions: Instruction[];
}

export interface ProcessEvent {
  seq: number;
  type: string;
  node_id: string | null;
  actor: string | null;
  data: Record<string, unknown>;
  at: string;
}

export interface SpaceEntry {
  path: string;
  name: string;
  is_dir: boolean;
  size: number | null;
  modified_at: string | null;
  node: string | null;
  uploaded_by: string | null;
}

export interface SpaceListing {
  dir: string;
  truncated: boolean;
  entries: SpaceEntry[];
}

// ───────────────────────────── 待办与通知

export interface Todo {
  kind: "admit" | "exit";
  process_id: string;
  process_code: string;
  process_title: string;
  project_id: string;
  team_id: string;
  node_id: string;
  node_name: string;
  since: string;
}

export interface Notification {
  id: string;
  text: string;
  link: string | null;
  read: boolean;
  at: string;
}
