export type Entity = Record<string, any>;
export type Run = Entity & {
  id: string;
  title?: string;
  status: string;
  messages?: Entity[];
  events?: Entity[];
  evidence?: Entity[];
  report?: Entity | string | null;
};
export type Page =
  | "workbench"
  | "users"
  | "runs"
  | "alerts"
  | "skills"
  | "mcp"
  | "models"
  | "invocations"
  | "overview";
export type RunPreset = {
  skill_id?: string;
  skill_version?: string;
  purpose?: string;
  target?: string;
  incident_id?: string;
  title?: string;
  analysis_start?: string;
  analysis_end?: string;
  message?: string;
};
export type User = Entity & { id: string; username: string; name: string; role: string };
export type Conversation = Entity & {
  id: string;
  title: string;
  context?: Entity;
  messages?: Entity[];
  runs?: Run[];
  attachments?: Entity[];
};
