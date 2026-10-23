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
  invalidatesResource,
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
  Field,
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
const coverageNames: Record<string, string> = { complete: "声明范围已覆盖", empty: "查询成功，结果为空", forbidden: "无读取权限", failed: "查询失败", stale: "数据陈旧", partial: "仅部分覆盖", historical_unsupported: "源不支持该历史范围", unknown: "覆盖未知" };
const coverageReasons: Record<string, string> = { out_of_window: "结果超出固定窗口", pagination_incomplete: "分页未读完", truncated: "保存或阅读存在截断", stale: "数据陈旧", historical_unsupported: "源明确不支持历史", historical_unknown: "历史能力未知", scope_unverified: "对象范围未核验", source_clock_unknown: "源时钟偏差未知" };
export function Coverage({ value }: { value?: Entity }) {
  const status = value?.status || "unknown";
  return <div className="coverage-summary"><Status value={status}>{coverageNames[status] || "覆盖未知"}</Status>{value?.reasons?.length ? <span>{value.reasons.map((reason: string) => coverageReasons[reason] || reason).join("；")}</span> : null}{value?.declared_window ? <small>声明窗口：{dateTime(value.declared_window.start)} — {dateTime(value.declared_window.end)}</small> : null}</div>;
}
export function AnalysisContextPanel({ value }: { value?: Entity }) {
  const verified = value?.version && value.status === "ready";
  const identity = value?.identity?.canonical;
  return <details className="analysis-context-panel" open={value?.status === "needs_input"}>
    <summary>对象与时间范围 <Status value={verified ? "checked" : value?.status === "needs_input" ? "needs_input" : "unknown"}>{verified ? "已固定" : value?.status === "needs_input" ? "需要补充" : "历史上下文未核验"}</Status></summary>
    {identity ? <Metadata items={[["环境", identity.environment], ["集群", identity.cluster || "不适用"], ["Namespace", identity.namespace || "不适用"], ["对象类型", identity.type], ["对象 ID", identity.id]]} /> : <p className="scope-note">{value?.version ? "身份尚未唯一确定，补齐范围和来源映射后新建尝试。" : "此记录没有版本化身份、窗口与覆盖契约，不能追认为完整分析。"}</p>}
    {value?.issues?.length ? <ul className="context-issues">{value.issues.map((item: Entity, i: number) => <li key={item.code || i}>{item.detail || item.message || item.code}</li>)}</ul> : null}
    {value?.windows ? <dl className="context-windows">{Object.entries(value.windows).map(([name, data]) => { const window = data as Entity; return <div key={name}><dt>{{ fault: "故障窗口", baseline: "对照窗口", change: "变更窗口", validation: "验证窗口" }[name] || name}</dt><dd>{dateTime(window.start)} — {dateTime(window.end)}{name === "baseline" ? <small>流量与健康可比性：{window.comparability === "verified" ? "已核验" : "未知"}</small> : null}</dd></div>; })}</dl> : null}
    {value?.version ? <JsonView title="身份映射、查询目录与时钟偏差版本" value={{ identity_mapping_version: value.identity_mapping_version, query_catalog_version: value.query_catalog_version, source_offsets: value.source_offsets, mappings: value.identity?.mappings }} /> : null}
  </details>;
}
function GroundedReport({ value, onEvidence }: { value: Entity; onEvidence?: (id: string) => void }) {
  const groups: [string, string][] = [["facts", "保存材料的确定性计算"], ["claims", "结构化结论核对"], ["potential_impacts", "登记关系上的潜在风险"], ["observed_anomalies", "遥测实测异常"], ["counterevidence", "反证与冲突"], ["unknowns", "未查询与无法判断"], ["candidates", "待核验候选"]];
  return <section className="grounded-report" aria-label="结构化证据核验报告">{groups.map(([key, label]) => {
    const rows = Array.isArray(value[key]) ? value[key] : value[key] ? [value[key]] : [];
    return <section key={key}><h3>{label}</h3>{rows.length ? <ol>{rows.map((row: Entity | string, i: number) => {
      const item: Entity = row && typeof row === "object" ? row : { text: readable(row) };
      const tuple = item.rule && item.pointer !== undefined && "value" in item ? `${item.rule}(${item.pointer}) = ${readable(item.value)}${item.unit ? ` ${item.unit}` : ""}` : "结构化字段不足，参见核验明细。";
      const mainText = key === "claims" ? item.verification_scope === "structured_fields_only" ? item.text || tuple : tuple : (item.text || item.statement || item.description || item.detail || item.claim) ?? item.value ?? item;
      const modelText = item.model_text || (key === "claims" && item.verification_scope !== "structured_fields_only" ? item.text : "");
      const verificationName = ({ verified: "仅结构字段核对通过", unverified: "未通过核对", potential_only: "仅潜在关系", unresolved: "尚待核验", observed_in_declared_scope: "仅声明范围内观察", normal_under_frozen_rule_only: "仅固定规则下的对照", observed_pattern_only: "仅记录观察模式" } as Record<string, string>)[item.status] || "范围或语义尚未核验";
      const refs: Entity[] = [...(Array.isArray(item.anchors) ? item.anchors : []), ...(Array.isArray(item.evidence_refs) ? item.evidence_refs : []), ...(Array.isArray(item.evidence_ids) ? item.evidence_ids.map((id: string) => ({ evidence_id: id })) : []), ...(item.evidence_id ? [{ evidence_id: item.evidence_id, anchor_id: item.anchor_id }] : [])];
      return <li key={item.id || i}><Markdown text={readable(mainText)} onEvidence={onEvidence} />{key === "facts" ? <p className="muted tiny">返回对象身份：{item.verification?.object === "verified" ? "已按冻结映射核验" : "未知，不能仅按查询目标归属"}</p> : null}{item.status ? <p><Status value={item.status}>{verificationName}</Status></p> : null}{modelText ? <details className="model-draft"><summary>模型表述（未核验，不随数值核对自动成立）</summary><Markdown text={readable(modelText)} onEvidence={onEvidence} /></details> : null}{item.limitations?.length || item.reasons?.length ? <p className="scope-note">本项仍有核验限制，见下方来源与范围明细。</p> : null}{refs.length ? <div className="fragment-references">{refs.map((ref: Entity | string, index: number) => { const evidenceId = typeof ref === "string" ? ref.split("#")[0] : ref?.evidence_id; const anchor = typeof ref === "string" ? ref.split("#")[1] : ref?.anchor_id || ref?.fragment_id; return evidenceId ? <button type="button" className="text-link" key={`${evidenceId}-${anchor || index}`} onClick={() => onEvidence?.(`${evidenceId}${anchor ? `#${anchor}` : ""}`)}>查看{anchor ? "精确片段" : "证据"} {index + 1}</button> : null; })}</div> : null}{typeof row === "object" ? <JsonView value={item} title="计算规则、单位与来源明细" /> : null}</li>;
    })}</ol> : <p className="muted">本轮未记录此项，不能据此断言不存在。</p>}</section>;
  })}</section>;
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
    return <><p className="scope-note">历史报告原文，未通过当前结构化事实核验。</p><Markdown text={report} onEvidence={onEvidence} /></>;
  const r = report as Entity;
  const analysis = readable(r.analysis);
  const summary = analysis || r.grounding ? "" : reportSummary(readable(r.summary));
  const known = new Set([
    "summary",
    "analysis",
    "window",
    "target",
    "facts",
    "evidence_ids",
    "limitations",
    "recommendations",
    "grounding", "report_version", "model_analysis", "model_draft",
  ]);
  return (
    <div className="report">
      <h3>{title}</h3>
      {r.report_version ? <p className="muted tiny">报告版本 {r.report_version}</p> : null}
      {r.grounding ? <GroundedReport value={r.grounding} onEvidence={onEvidence} /> : null}
      {summary ? (
        <div className="summary-callout">
          <ShieldCheck size={21} />
          <Markdown text={readable(summary)} onEvidence={onEvidence} />
        </div>
      ) : null}
      {analysis ? r.grounding ? <details className="model-draft"><summary>模型原文（未经逐项核验，不作为已确认事实）</summary><Markdown text={analysis} onEvidence={onEvidence} /></details> : <><p className="scope-note">以下为历史报告或模型原文，未通过当前结构化事实核验。</p><Markdown text={analysis} onEvidence={onEvidence} /></> : null}
      {r.model_analysis || r.model_draft ? <details className="model-draft"><summary>模型草稿（未经核验）</summary><Markdown text={readable(r.model_analysis || r.model_draft)} onEvidence={onEvidence} /></details> : null}
      {r.facts ? (
        <JsonView value={r.facts} title={r.grounding ? "事实核验元数据" : "历史报告记录（未按当前契约核验）"} />
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
  const [evidenceId, anchorId] = id.split("#", 2);
  const resource = useResource(`/evidence/${encodeURIComponent(evidenceId)}`, 4000);
  const anchor = useResource(anchorId ? `/fragments/${encodeURIComponent(anchorId)}` : null);
  const action = useAction();
  const [pointer, setPointer] = useState(""), [offset, setOffset] = useState(0), [limit, setLimit] = useState(100), [series, setSeries] = useState(""), [fields, setFields] = useState(""), [start, setStart] = useState(""), [end, setEnd] = useState(""), [timeField, setTimeField] = useState("");
  const [fragment, setFragment] = useState<Entity | null>(null);
  const [fragmentDenied, setFragmentDenied] = useState(false);
  const currentId = useRef(id); currentId.current = id;
  useEffect(() => { setFragment(null); setFragmentDenied(false); setPointer(""); setOffset(0); setLimit(100); setSeries(""); setFields(""); setStart(""); setEnd(""); setTimeField(""); action.setError(""); }, [id]);
  const denied = resource.accessDenied || anchor.accessDenied || fragmentDenied;
  useEffect(() => { if (denied) { setFragment(null); anchor.setData(null); } }, [denied]);
  const selectedFragment = denied ? null : fragment || anchor.data;
  async function read(next = 0, savedSelector?: Entity) {
    await action.act("read", async () => {
      if (!resource.data?.run_id) throw new Error("此证据没有关联分析轮次，暂不能定向读取。");
      if (Boolean(start) !== Boolean(end)) throw new Error("片段时间筛选须同时填写开始与结束。");
      const selector = savedSelector ? { ...savedSelector, offset: next } : { pointer, offset: next, limit, ...(series !== "" ? { series: Number(series) } : {}), ...(fields.trim() ? { fields: fields.split(",").map((item) => item.trim()).filter(Boolean) } : {}), ...(start && end ? { time_start: new Date(start).toISOString(), time_end: new Date(end).toISOString(), ...(timeField ? { time_field: timeField } : {}) } : {}) };
      try {
        const result = await post(`/runs/${encodeURIComponent(resource.data.run_id)}/material-reads`, { resource_type: "evidence", evidence_id: evidenceId, selector });
        if (currentId.current === id) { setFragment(result); setFragmentDenied(false); setOffset(next); }
      } catch (error) {
        if (currentId.current === id && invalidatesResource(error)) { setFragmentDenied(true); resource.setData(null); }
        throw error;
      }
    });
  }
  return (
    <Modal title="证据详情" onClose={onClose} wide>
      <Notice error={resource.error || action.error || (!fragment ? anchor.error : "")} />
      {resource.loading ? <Loading /> : null}
      {resource.data && !denied ? (
        <>
          <Metadata
            items={Object.entries(resource.data)
              .filter(([k]) => !["response", "raw", "content"].includes(k))
              .slice(0, 12)}
          />
          <Coverage value={resource.data.coverage} />
          <details className="material-reader" open={Boolean(anchorId)}><summary>定向读取已保存证据</summary><p className="muted">仅重读本轮已保存材料，不重新查询源；保存时已截断的部分不能在此恢复。</p><form className="stack" onSubmit={(event) => { event.preventDefault(); void read(offset); }}><div className="form-grid">
            <Field label="JSON Pointer" hint="相对解包后的逻辑 JSON 根；例如 /data/result"><input value={pointer} onChange={(event) => setPointer(event.target.value)} placeholder="/data/result" /></Field>
            <Field label="起始位置"><input type="number" min={0} step={1} value={offset} onChange={(event) => setOffset(Number(event.target.value))} /></Field>
            <Field label="每页数量"><input type="number" min={1} max={200} step={1} value={limit} onChange={(event) => setLimit(Number(event.target.value))} /></Field>
            <Field label="序列下标（可选）"><input type="number" min={0} step={1} value={series} onChange={(event) => setSeries(event.target.value)} /></Field>
            <Field label="字段（可选，英文逗号分隔）"><input value={fields} onChange={(event) => setFields(event.target.value)} /></Field>
            <Field label="片段开始时间（可选）"><input type="datetime-local" step={1} value={start} onChange={(event) => setStart(event.target.value)} /></Field>
            <Field label="片段结束时间（可选）"><input type="datetime-local" step={1} value={end} onChange={(event) => setEnd(event.target.value)} /></Field>
            <Field label="行时间字段（JSON Pointer，可选）" hint="填写完整时间筛选时使用；默认 /0，表示行数组第一个值。"><input value={timeField} onChange={(event) => setTimeField(event.target.value)} placeholder="/timestamp" /></Field>
          </div><div className="form-actions"><Button type="submit" busy={!!action.busy} disabled={!resource.data.run_id}>读取片段</Button></div></form></details>
          {anchorId && anchor.loading && !fragment ? <Loading /> : null}
          {selectedFragment ? <section className="fragment-result"><h3>已保存片段</h3><p className="fragment-anchor">精确引用：<code>{evidenceId}#{selectedFragment.anchor_id || anchorId}</code></p><JsonView value={selectedFragment.position} title="片段位置" open /><JsonView value={selectedFragment.completeness} title="来源、保存与阅读完整性" open /><JsonView value={selectedFragment.data} title="本页片段" open /><JsonView value={selectedFragment.usage} title="读取用量" />{selectedFragment.has_more_saved ? <Button onClick={() => void read(Number(selectedFragment.next_offset), selectedFragment.selector)} busy={!!action.busy}>读取下一页</Button> : <p className="muted">该选择范围内的已保存材料已读完；不代表源端没有更多数据。</p>}</section> : null}
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
  useEffect(() => { if (resource.accessDenied) setEvidence(null); }, [resource.accessDenied]);
  const isInvestigation =
    !run || ["investigation", "skill_test"].includes(run.purpose);
  useEffect(() => {
    streamCursor.current = 0;
    setStream("连接中");
    if (run && (run.legacy_archive || !["queued", "running"].includes(run.status))) {
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
  }, [id, run?.status, run?.legacy_archive, resource.reload]);
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
  const active = !run.legacy_archive && ["queued", "running"].includes(run.status);
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
      {run.legacy_archive ? <Notice message={readable(run.legacy_archive.notice) || "升级前历史只读档案，未按当前规则重新核验。请新建会话继续分析。"} /> : null}
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
          {!run.legacy_archive && (active ? (
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
          ))}
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
      {isInvestigation ? <AnalysisContextPanel value={run.analysis_context} /> : null}
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
                          {` · ${coverageNames[e.coverage?.status || "unknown"] || "覆盖未知"}`}
                        </small>
                      </span>
                      <Status value={e.coverage?.status || e.status}>{e.coverage ? coverageNames[e.coverage.status] || "覆盖未知" : undefined}</Status>
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
