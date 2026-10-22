import { useCallback, useEffect, useRef, useState } from "react";
import type { Entity } from "./types";
export class ApiError extends Error {
  status: number;
  code?: string;
  detail: unknown;
  constructor(status: number, detail: unknown, code?: string) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}
export async function api<T = Entity>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...options,
    credentials: "same-origin",
    headers: {
      ...(options.body ? { "Content-Type": "application/json" } : {}),
      ...options.headers,
    },
  });
  const text = await response.text();
  let data: any;
  try {
    data = text ? JSON.parse(text) : {};
  } catch {
    if (response.ok)
      throw new ApiError(502, "服务返回了非 JSON 数据，请检查API 服务。");
    data = { detail: response.statusText || "服务返回格式异常" };
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/login" && path !== "/session")
      window.dispatchEvent(new Event("easyops:session-expired"));
    throw new ApiError(
      response.status,
      data.detail || data.message || response.statusText,
      data.code,
    );
  }
  return data as T;
}
export const post = <T = Entity>(path: string, body: unknown = {}) =>
  api<T>(path, { method: "POST", body: JSON.stringify(body) });
export const put = <T = Entity>(path: string, body: unknown) =>
  api<T>(path, { method: "PUT", body: JSON.stringify(body) });
export const list = (data: unknown): Entity[] =>
  Array.isArray(data)
    ? data
    : Array.isArray((data as Entity)?.items)
      ? (data as Entity).items
      : [];
export function useResource<T = Entity>(path: string | null, interval = 0) {
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(true);
  const seq = useRef(0),
    mounted = useRef(false);
  const reload = useCallback(async () => {
    if (!path) return null;
    const current = ++seq.current;
    try {
      const value = await api<T>(path);
      if (mounted.current && current === seq.current) {
        setData(value);
        setError("");
      }
      return value;
    } catch (err) {
      if (mounted.current && current === seq.current) setError(errorText(err));
      return null;
    } finally {
      if (mounted.current && current === seq.current) setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    mounted.current = true;
    setData(null);
    setError("");
    setLoading(Boolean(path));
    if (path) void reload();
    const timer =
      interval && path
        ? window.setInterval(() => {
            if (document.visibilityState === "visible") void reload();
          }, interval)
        : undefined;
    return () => {
      mounted.current = false;
      seq.current++;
      window.clearInterval(timer);
    };
  }, [reload, interval, path]);
  return { data, error, loading, reload, setData };
}
export function useAction() {
  const inFlight = useRef(false);
  const [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const act = async <T>(
    name: string,
    work: () => Promise<T>,
    success?: string,
  ): Promise<T | null> => {
    if (inFlight.current) return null;
    inFlight.current = true;
    setBusy(name);
    setError("");
    setNotice("");
    try {
      const result = await work();
      if (success) setNotice(success);
      return result;
    } catch (err) {
      setError(errorText(err));
      return null;
    } finally {
      inFlight.current = false;
      setBusy("");
    }
  };
  return { busy, error, notice, act, setError, setNotice };
}
export const errorText = (err: unknown) =>
  err instanceof Error ? err.message : String(err);
export const requestId = () => crypto.randomUUID();
export const dateTime = (value: unknown) => {
  if (!value) return "未知";
  const d = new Date(String(value));
  return Number.isNaN(d.getTime())
    ? String(value)
    : d.toLocaleString("zh-CN", { hour12: false });
};
export const json = (value: unknown) => JSON.stringify(value ?? {}, null, 2);
export function parseJson(
  value: string,
  name: string,
  kind?: "array" | "object",
) {
  let parsed;
  try {
    parsed = JSON.parse(value);
  } catch {
    throw new Error(`${name} 不是有效 JSON`);
  }
  if (kind === "array" && !Array.isArray(parsed))
    throw new Error(`${name} 必须是 JSON 数组`);
  if (
    kind === "object" &&
    (parsed === null || typeof parsed !== "object" || Array.isArray(parsed))
  )
    throw new Error(`${name} 必须是 JSON 对象`);
  return parsed;
}
