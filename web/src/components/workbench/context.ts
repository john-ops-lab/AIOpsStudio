import type { Entity } from "../../types";
export const contextKeys = ["skill_id", "skill_version", "model_id", "target", "incident_id", "analysis_start", "analysis_end", "environment"] as const;
export function analysisContext(value: Entity = {}): Entity {
  return Object.fromEntries(contextKeys.map((key) => [key, value[key] || ""]));
}
export function localDate(value: unknown) {
  if (!value) return "";
  const d = new Date(String(value));
  return Number.isNaN(d.getTime()) ? "" : new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
}
export const activeRun = (status?: string) => ["queued", "running", "needs_input"].includes(status || "");
export function textValue(value: unknown): string {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(textValue).join("\n");
  if (typeof value === "object") {
    const v = value as Entity;
    return String(v.text || v.content || v.detail || v.message || JSON.stringify(v));
  }
  return String(value);
}
export const sizeLabel = (size: number) => size >= 1048576 ? `${(size / 1048576).toFixed(1)} MiB` : `${Math.ceil(size / 1024)} KiB`;
export const isReportAttachment = (value: Entity) => ["generated_report", "report"].includes(value.kind);
export function fileBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("无法读取文件，请重新选择。"));
    reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
    reader.readAsDataURL(file);
  });
}
