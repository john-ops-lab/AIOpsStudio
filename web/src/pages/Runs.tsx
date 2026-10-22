import { useEffect, useRef, useState } from "react";
import {
  ArrowRight,
  BookOpen,
  Check,
  Clock3,
  FileText,
  Layers3,
  Send,
  ShieldCheck,
  StopCircle,
} from "lucide-react";
import {
  dateTime,
  json,
  list,
  post,
  requestId,
  useAction,
  useResource,
} from "../api";
import type { Entity, Run } from "../types";
import Markdown from "../components/Markdown";
import { reportMetadataTitles, reportSummary } from "../components/reportPresentation";
import {
  Button,
  Empty,
  JsonView,
  Loading,
  Metadata,
  Modal,
  Notice,
  Status,
} from "../components/ui";
function readable(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.map(readable).join("\n");
  if (typeof value === "object") {
    const v = value as Entity;
    return v.text || v.content || v.message || json(value);
  }
  return String(value);
}
export function Report({
  report,
  title = "分析结论",
  onEvidence,
}: {
  report: unknown;
  title?: string;
  onEvidence?: (id: string) => void;
}) {
  if (!report)
    return (
      <Empty
        title="报告尚未生成"
        detail="分析完成后，在此查看结论、事实依据与未覆盖范围。"
        icon={<FileText size={30} />}
      />
    );
  if (typeof report === "string")
    return <Markdown text={report} onEvidence={onEvidence} />;
  const r = report as Entity;
  const analysis = readable(r.analysis);
  const summary = analysis ? "" : reportSummary(readable(r.summary));
  const known = new Set([
    "summary",
    "analysis",
    "window",
    "target",
    "facts",
    "evidence_ids",
    "limitations",
    "recommendations",
  ]);
  return (
    <div className="report">
      <h3>{title}</h3>
      {summary ? (
        <div className="summary-callout">
          <ShieldCheck size={21} />
          <Markdown text={readable(summary)} onEvidence={onEvidence} />
        </div>
      ) : null}
      {analysis ? <Markdown text={analysis} onEvidence={onEvidence} /> : null}
      {r.facts ? (
        <JsonView value={r.facts} title="报告事实与证据元数据" />
      ) : null}
      {r.recommendations ? (
        <>
          <h3>建议与下一步</h3>
          <Markdown
            text={readable(r.recommendations)}
            onEvidence={onEvidence}
          />
        </>
      ) : null}
      {(
        Array.isArray(r.limitations)
          ? r.limitations.length > 0
          : !!r.limitations
      ) ? (
        <>
          <h3>范围与限制</h3>
          <div className="scope-note prose">{readable(r.limitations)}</div>
        </>
      ) : null}
      {r.evidence_ids?.length ? (
        <p className="muted">证据引用：{r.evidence_ids.join(" · ")}</p>
      ) : null}
      {r.window ? <JsonView value={r.window} title="报告时间窗" /> : null}
      {Object.entries(r)
        .filter(([k]) => !known.has(k))
        .map(([k, v]) => (
          <JsonView key={k} value={v} title={reportMetadataTitles[k] || `原始字段：${k}`} />
        ))}
      {!Object.keys(r).length ? (
        <Empty title="报告为空" detail="服务端尚未保存报告内容。" />
      ) : null}
    </div>
  );
}
export function EvidenceModal({
  id,
  onClose,
}: {
  id: string;
  onClose: () => void;
}) {
  const resource = useResource(`/evidence/${encodeURIComponent(id)}`);
  return (
    <Modal title="证据详情" onClose={onClose} wide>
      <Notice error={resource.error} />
      {resource.loading ? <Loading /> : null}
      {resource.data ? (
        <>
          <Metadata
            items={Object.entries(resource.data)
              .filter(([k]) => !["response", "raw", "content"].includes(k))
              .slice(0, 12)}
          />
          <JsonView
            value={
              resource.data.response ??
              resource.data.raw ??
              resource.data.content ??
              resource.data
            }
          />
        </>
      ) : null}
    </Modal>
  );
}
function RunDetail({
  id,
  onOpenRun,
}: {
  id: string;
  onOpenRun: (id: string) => void;
}) {
  const resource = useResource<Run>(`/runs/${encodeURIComponent(id)}`, 5000),
    state = useAction();
  const [tab, setTab] = useState("report"),
    [message, setMessage] = useState(""),
    [evidence, setEvidence] = useState<string | null>(null),
    [stream, setStream] = useState("连接中");
  const streamCursor = useRef(0);
  const pendingMessage = useRef<{ content: string; id: string } | null>(null);
  const chatEnd = useRef<HTMLDivElement>(null);
  const run = resource.data;
  const isInvestigation =
    !run || ["investigation", "skill_test"].includes(run.purpose);
  useEffect(() => {
    streamCursor.current = 0;
    setStream("连接中");
    if (run && !["queued", "running", "needs_input"].includes(run.status)) {
      setStream("记录已持久化");
      return;
    }
    let timer: number | undefined;
    const events = new EventSource(
      `/api/runs/${encodeURIComponent(id)}/events?after=0`,
      { withCredentials: true },
    );
    events.onopen = () => setStream("实时连接");
    events.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        streamCursor.current = Math.max(
          streamCursor.current,
          Number(data.seq || data.id) || 0,
        );
      } catch {
        /* A status heartbeat needs no parsing. */
      }
      window.clearTimeout(timer);
      timer = window.setTimeout(() => void resource.reload(), 120);
    };
    events.onerror = () => setStream("自动重连 · 5秒刷新");
    return () => {
      events.close();
      window.clearTimeout(timer);
    };
  }, [id, run?.status, resource.reload]);
  useEffect(() => {
    chatEnd.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [run?.messages?.length]);
  async function send(event: React.FormEvent) {
    event.preventDefault();
    const content = message.trim();
    if (!content) return;
    if (!pendingMessage.current || pendingMessage.current.content !== content)
      pendingMessage.current = { content, id: requestId() };
    await state.act(
      "message",
      async () => {
        await post(`/runs/${id}/messages`, {
          content,
          client_message_id: pendingMessage.current!.id,
          context: { run_id: id },
        });
        pendingMessage.current = null;
        setMessage("");
        await resource.reload();
      },
      "消息已保存",
    );
  }
  async function command(action: string) {
    await state.act(
      action,
      async () => {
        const result = await post<Run>(`/runs/${id}/${action}`);
        if (action === "retry" && result.id) onOpenRun(result.id);
        else await resource.reload();
      },
      action === "cancel" ? "已提交取消请求" : undefined,
    );
  }
  if (resource.loading) return <Loading />;
  if (!run)
    return (
      <div className="detail-pad">
        <Notice error={resource.error} />
        <Empty title="无法读取分析" detail="请刷新或从列表中重新选择记录。" />
      </div>
    );
  const active = ["queued", "running", "needs_input"].includes(run.status);
  const artifactFailure = run.artifact_error || run.report_attachment_error;
  const events = run.events || [];
  const loaded = events.some((event) => event.type === "skill_loaded");
  const evidenceCount = run.evidence?.length || 0;
  const lastToolEvent = events
    .filter((event) => ["tool_started", "tool_completed"].includes(event.type))
    .at(-1);
  const toolInFlight = active && lastToolEvent?.type === "tool_started";
  const stages = [
    {
      title: "提交",
      state: run.created_at ? "succeeded" : "unknown",
      detail: run.created_at ? "已持久化" : "未取得提交记录",
    },
    {
      title: "方法加载",
      state: loaded ? "succeeded" : "unknown",
      detail: loaded
        ? "所选版本已加载"
        : run.status === "queued"
          ? "等待执行槽"
          : "未取得加载事件",
    },
    {
      title: "证据留存",
      state: toolInFlight ? "running" : evidenceCount ? "succeeded" : "unknown",
      detail: evidenceCount
        ? `${evidenceCount} 条已保存${toolInFlight ? " · 取证中" : ""}`
        : toolInFlight
          ? "工具执行中"
          : "尚无已存证据",
    },
    {
      title: "报告留存",
      state: run.report ? "succeeded" : "unknown",
      detail: run.report ? "已保存，结论见报告" : "尚无已存报告",
    },
  ];
  return (
    <div className="run-detail">
      {artifactFailure ? <Notice error={`回复或报告制品保存失败：${readable(artifactFailure)}`} /> : null}
      <header className="run-heading">
        <div>
          <h1>{run.title || run.id}</h1>
          <div className="run-identifier">
            {run.incident_id
              ? `#${run.incident_id}`
              : run.purpose || "investigation"}
            <span> · </span>
            {run.id}
          </div>
        </div>
        <div className="actions">
          <Status value={run.status} />
          {active ? (
            <Button
              variant="ghost"
              onClick={() => void command("cancel")}
              busy={state.busy === "cancel"}
            >
              <StopCircle size={15} />
              取消
            </Button>
          ) : (
            <Button
              onClick={() => void command("retry")}
              busy={state.busy === "retry"}
            >
              重新运行
            </Button>
          )}
        </div>
      </header>
      <div className="run-meta">
        <span>
          <Layers3 size={15} />
          目标对象 <strong>{readable(run.target) || "未指定"}</strong>
        </span>
        <span>
          <Clock3 size={15} />
          分析时间{" "}
          {run.analysis_start
            ? `${dateTime(run.analysis_start)} — ${dateTime(run.analysis_end)}`
            : "提交时刻快照"}
        </span>
        <span className="stream">
          <i />
          {stream}
        </span>
      </div>
      {isInvestigation ? (
        <div className="stage-records">
          <div className="stages" aria-label="运行记录留存">
            {stages.map((s, i) => (
              <div
                key={s.title}
                className={`stage ${s.state === "succeeded" ? "done" : s.state === "running" ? "current" : ""}`}
              >
                <span className="stage-circle">
                  {s.state === "succeeded" ? <Check size={16} /> : i + 1}
                </span>
                <div>
                  <strong>{s.title}</strong>
                  <small>{s.detail}</small>
                </div>
                {i < 3 ? <span className="stage-line" /> : null}
              </div>
            ))}
          </div>
          <p className="stages-note">
            步骤表示运行记录的留存状态，业务判断见报告中的证据与限制。
          </p>
        </div>
      ) : (
        <div className="trial-note">
          {run.purpose === "tool_test"
            ? "只读工具试调 · 不调用模型"
            : "模型协议样本 · 独立验证中文响应与工具调用"}
        </div>
      )}
      <Notice
        error={
          resource.error ||
          state.error ||
          (run.error ? readable(run.error) : "")
        }
        message={state.notice}
      />
      <div className="investigation-body">
        <section className="report-panel">
          <div className="tabs" role="tablist" aria-label="分析详情">
            {[
              ["report", isInvestigation ? "分析报告" : "试调结果"],
              ["evidence", `证据 (${run.evidence?.length || 0})`],
              ["events", "执行记录"],
              ["config", "配置快照"],
            ].map(([key, label]) => (
              <button
                key={key}
                role="tab"
                aria-selected={tab === key}
                className={tab === key ? "active" : ""}
                onClick={() => setTab(key)}
              >
                {label}
              </button>
            ))}
          </div>
          <div className="report-scroll">
            {tab === "report" ? (
              <Report
                report={run.report}
                onEvidence={setEvidence}
                title={isInvestigation ? "分析结论" : "执行结果"}
              />
            ) : null}
            {tab === "evidence" ? (
              run.evidence?.length ? (
                <div className="evidence-list">
                  {run.evidence.map((e: Entity) => (
                    <button
                      key={e.id}
                      className="evidence-row"
                      onClick={() => setEvidence(e.id)}
                    >
                      <BookOpen size={17} />
                      <span>
                        <strong>{e.tool_name || e.source || e.id}</strong>
                        <small>
                          {dateTime(e.created_at)}
                          {e.truncated ? " · 返回已截断" : ""}
                        </small>
                      </span>
                      <Status value={e.status} />
                      <ArrowRight size={15} />
                    </button>
                  ))}
                </div>
              ) : (
                <Empty
                  title="尚无证据"
                  detail="真实工具调用返回后，证据与来源会保存在这里。"
                />
              )
            ) : null}
            {tab === "events" ? (
              events.length ? (
                <ol className="timeline">
                  {events.map((e: Entity, i: number) => (
                    <li key={e.id || e.seq || i}>
                      <span className="timeline-dot" />
                      <div>
                        <div className="timeline-label">
                          <strong>{e.type || "执行事件"}</strong>
                          <small>{dateTime(e.created_at || e.timestamp)}</small>
                        </div>
                        <JsonView value={e.payload ?? e.data ?? e} />
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <Empty
                  title="尚无执行事件"
                  detail="排队与执行事件将持续追加。"
                />
              )
            ) : null}
            {tab === "config" ? (
              <>
                <Metadata
                  items={[
                    ["Skill", run.skill_id],
                    ["内容版本", run.skill_version],
                    ["配置修订", run.config_revision],
                    ["Attempt", run.attempt_id],
                    ["线程", run.thread_id],
                    ["创建时间", dateTime(run.created_at)],
                  ]}
                />
                <JsonView
                  value={
                    run.config_snapshot ||
                    run.snapshot || { budget: run.budget, usage: run.usage }
                  }
                  title="预算、用量与配置"
                  open
                />
              </>
            ) : null}
          </div>
        </section>
        <aside className="chat-panel">
          <div className="chat-heading">
            <strong>{isInvestigation ? "分析助手" : "执行说明"}</strong>
            <span className="muted tiny">消息持久保存</span>
          </div>
          <div className="chat-messages" aria-live="polite">
            {run.messages?.length ? (
              run.messages.map((m: Entity, i: number) => (
                <div
                  key={m.id || i}
                  className={`message ${m.role === "user" ? "user" : "assistant"}`}
                >
                  <div className="avatar">{m.role === "user" ? "U" : "A"}</div>
                  <div>
                    <small>{m.role === "user" ? "你" : "分析助手"}</small>
                    <div className="message-content">
                      <Markdown
                        text={readable(m.content ?? m.text)}
                        onEvidence={setEvidence}
                      />
                    </div>
                    {m.created_at ? (
                      <time>{dateTime(m.created_at)}</time>
                    ) : null}
                  </div>
                </div>
              ))
            ) : (
              <div className="chat-welcome">
                <ShieldCheck size={28} />
                <strong>
                  {isInvestigation ? "围绕证据，继续分析" : "试调结果独立保存"}
                </strong>
                <p>
                  {isInvestigation
                    ? "补充目标或问题。你的消息会与本次分析一起保存。"
                    : "本次只执行指定工具或协议样本；完整请求、返回与错误请查看证据和执行记录。"}
                </p>
              </div>
            )}
            <div ref={chatEnd} />
          </div>
          <form className="chat-composer" onSubmit={send}>
            <label className="sr-only" htmlFor="chat-message">
              补充分析问题
            </label>
            <textarea
              id="chat-message"
              disabled={!active || !isInvestigation}
              placeholder={
                !isInvestigation
                  ? "试调不接受分析消息"
                  : !active
                    ? "本次尝试已结束，重新运行后继续分析"
                    : "补充问题或提供更多上下文…"
              }
              rows={3}
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                  e.preventDefault();
                  void send(e as unknown as React.FormEvent);
                }
              }}
            />
            <div>
              <small>⌘ / Ctrl + Enter 发送</small>
              <Button
                variant="primary"
                type="submit"
                disabled={!message.trim() || !active || !isInvestigation}
                busy={state.busy === "message"}
                aria-label="发送分析消息"
              >
                <Send size={17} />
              </Button>
            </div>
          </form>
        </aside>
      </div>
      {evidence ? (
        <EvidenceModal id={evidence} onClose={() => setEvidence(null)} />
      ) : null}
    </div>
  );
}
export default function Runs({
  selected,
  onSelect,
}: {
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  const resource = useResource("/runs", 6000),
    [search, setSearch] = useState(""),
    [status, setStatus] = useState("all");
  const runs = list(resource.data).filter(
    (r) =>
      `${r.title || ""} ${r.id} ${r.target || ""} ${r.incident_id || ""}`
        .toLowerCase()
        .includes(search.toLowerCase()) &&
      (status === "all" || r.status === status),
  );
  return (
    <div className="workspace">
      <aside className="master-list">
        <div className="master-head">
          <h2>分析列表</h2>
          <span className="muted tiny">{list(resource.data).length} 项</span>
        </div>
        <div className="list-filters">
          <input
            aria-label="搜索分析"
            placeholder="搜索名称、告警或资源 ID…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
          <select
            aria-label="筛选分析状态"
            value={status}
            onChange={(e) => setStatus(e.target.value)}
          >
            <option value="all">全部状态</option>
            {[
              "running",
              "queued",
              "needs_input",
              "succeeded",
              "partial",
              "failed",
              "cancelled",
            ].map((s) => (
              <option key={s} value={s}>
                {
                  (
                    {
                      running: "分析中",
                      queued: "排队中",
                      needs_input: "待补充",
                      succeeded: "已完成",
                      partial: "部分完成",
                      failed: "失败",
                      cancelled: "已取消",
                    } as Entity
                  )[s]
                }
              </option>
            ))}
          </select>
        </div>
        <Notice error={resource.error} />
        <div className="master-scroll">
          {resource.loading ? (
            <Loading />
          ) : runs.length ? (
            runs.map((r) => (
              <button
                key={r.id}
                className={`master-row ${selected === r.id ? "selected" : ""}`}
                onClick={() => onSelect(r.id)}
              >
                <div className="row-top">
                  <strong>{r.title || r.id}</strong>
                  <Status value={r.status} />
                </div>
                <span className="row-subtitle">
                  {r.incident_id
                    ? `#${r.incident_id}`
                    : r.skill_id || r.purpose}
                </span>
                <time>{dateTime(r.created_at)}</time>
              </button>
            ))
          ) : (
            <Empty
              title="暂无分析"
              detail={
                search ? "没有匹配的记录。" : "从告警事件或新建分析开始。"
              }
            />
          )}
        </div>
      </aside>
      <main className="workspace-main">
        {selected ? (
          <RunDetail
            key={selected}
            id={selected}
            onOpenRun={(id) => {
              onSelect(id);
              void resource.reload();
            }}
          />
        ) : (
          <Empty
            title="选择一项分析"
            detail="查看分析进展、原始证据与报告，或发起新的分析。"
            icon={<ShieldCheck size={35} />}
          />
        )}
      </main>
    </div>
  );
}
