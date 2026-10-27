import { cloneElement, isValidElement, useEffect, useId, useRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import {
  AlertCircle,
  ChevronRight,
  Inbox,
  LoaderCircle,
  RefreshCw,
  X,
} from "lucide-react";
import { dateTime, json } from "../api";
export function Button({
  children,
  variant = "secondary",
  busy,
  className = "",
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: "primary" | "secondary" | "ghost" | "danger";
  busy?: boolean;
}) {
  return (
    <button
      className={`btn ${variant} ${className}`}
      {...props}
      disabled={props.disabled || busy}
    >
      {busy ? <LoaderCircle size={15} className="spin" /> : null}
      {children}
    </button>
  );
}
export function Notice({
  error,
  message,
}: {
  error?: string;
  message?: string;
}) {
  return error || message ? (
    <div
      className={`notice ${error ? "error" : "success"}`}
      role={error ? "alert" : "status"}
    >
      <AlertCircle size={16} />
      <span>{error || message}</span>
    </div>
  ) : null;
}
export function Empty({
  title = "暂无记录",
  detail = "数据将从已连接的数据源加载。",
  icon,
  action,
}: {
  title?: string;
  detail?: string;
  icon?: ReactNode;
  action?: ReactNode;
}) {
  return (
    <div className="empty">
      {icon || <Inbox size={30} />}
      <h3>{title}</h3>
      <p>{detail}</p>
      {action}
    </div>
  );
}
export function Loading() {
  return (
    <div className="loading" role="status">
      <LoaderCircle size={19} className="spin" />
      正在加载…
    </div>
  );
}
const statusNames: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  succeeded: "已完成",
  partial: "部分完成",
  failed: "失败",
  needs_input: "待补充",
  cancelled: "已取消",
  enabled: "已启用",
  disabled: "已停用",
  draft: "草稿",
  published: "已发布",
  unknown: "未检查",
  saved: "已保存",
  checked: "检查完成",
  not_connected: "未接入",
  unchecked: "未检查",
  processing: "处理中",
  completed: "已完成",
  connected: "协议连通",
  ready: "可用",
  ok: "检查通过",
  error: "错误",
  healthy: "正常",
  unavailable: "不可用",
  high: "高",
  critical: "紧急",
  medium: "中",
  warning: "警告",
  low: "低",
  info: "提示",
  resolved: "已恢复",
  firing: "触发中",
  pending: "待确认",
};
export function Status({
  value,
  children,
}: {
  value?: string;
  children?: ReactNode;
}) {
  const state = value || "unknown";
  return (
    <span className={`status status-${state.replace(/[^a-zA-Z0-9_-]/g, "")}`}>
      <i />
      {children || statusNames[state] || state}
    </span>
  );
}
export function Field({
  label,
  children,
  hint,
  required,
}: {
  label: string;
  children: ReactNode;
  hint?: string;
  required?: boolean;
}) {
  const hintId = useId();
  const control = hint && isValidElement<{ "aria-describedby"?: string }>(children) && ["input", "select", "textarea"].includes(String(children.type))
    ? cloneElement(children, { "aria-describedby": [children.props["aria-describedby"], hintId].filter(Boolean).join(" ") })
    : children;
  return (
    <label className="field">
      <span>
        {label}
        {required ? <b className="required"> *</b> : null}
      </span>
      <div>{control}</div>
      {hint ? <small id={hintId}>{hint}</small> : null}
    </label>
  );
}
export function JsonView({
  value,
  title,
  open = false,
}: {
  value: unknown;
  title?: string;
  open?: boolean;
}) {
  return title ? (
    <details className="json-disclosure" open={open}>
      <summary>
        {title}
        <ChevronRight size={14} />
      </summary>
      <pre className="json-view">{json(value)}</pre>
    </details>
  ) : (
    <pre className="json-view">
      {typeof value === "string" ? value : json(value)}
    </pre>
  );
}
export function Modal({
  title,
  children,
  onClose,
  wide = false,
  disableClose = false,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
  wide?: boolean;
  disableClose?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const el = ref.current;
    const previousFocus =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null;
    el?.showModal();
    return () => {
      el?.close();
      if (previousFocus?.isConnected)
        previousFocus.focus({ preventScroll: true });
    };
  }, []);
  return (
    <dialog
      ref={ref}
      className={`modal ${wide ? "wide" : ""}`}
      aria-labelledby={titleId}
      onCancel={(event) => { event.preventDefault(); if (!disableClose) onClose(); }}
      onClick={(event) => {
        if (!disableClose && event.target === event.currentTarget) onClose();
      }}
    >
      <div className="modal-head">
        <h2 id={titleId}>{title}</h2>
        <Button variant="ghost" aria-label="关闭对话框" disabled={disableClose} onClick={onClose}>
          <X size={18} />
        </Button>
      </div>
      <div className="modal-body">{children}</div>
    </dialog>
  );
}
export function PageTitle({
  title,
  description,
  actions,
}: {
  title: string;
  description?: string;
  actions?: ReactNode;
}) {
  return (
    <div className="page-title">
      <div>
        <h1>{title}</h1>
        {description ? <p>{description}</p> : null}
      </div>
      <div className="actions">{actions}</div>
    </div>
  );
}
export function Refresh({
  onClick,
  busy = false,
}: {
  onClick: () => void;
  busy?: boolean;
}) {
  return (
    <Button onClick={onClick} busy={busy} aria-label="刷新数据">
      <RefreshCw size={15} />
      <span className="hide-small">刷新</span>
    </Button>
  );
}
export function Metadata({ items }: { items: [string, unknown][] }) {
  return (
    <dl className="metadata">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>
            {value === undefined || value === null || value === ""
              ? "未知"
              : typeof value === "object"
                ? json(value)
                : String(value)}
          </dd>
        </div>
      ))}
    </dl>
  );
}
export function Updated({ value }: { value: unknown }) {
  return <span className="muted tiny">更新于 {dateTime(value)}</span>;
}
