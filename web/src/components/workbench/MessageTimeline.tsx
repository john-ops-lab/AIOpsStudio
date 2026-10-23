import { useEffect, useId, useRef, useState } from "react";
import { ArrowUpRight, CheckCircle2, FileText, MessageSquare, Paperclip } from "lucide-react";
import type { Entity, Run } from "../../types";
import { dateTime } from "../../api";
import Markdown from "../Markdown";
import { Button, Status } from "../ui";
import { activeRun, textValue } from "./context";
import { reportSummary } from "../reportPresentation";
function MessageBody({ text, summarize, onEvidence }: { text: string; summarize: boolean; onEvidence: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false);
  const bodyId = useId(), preview = summarize ? reportSummary(text) : text;
  return <><div id={bodyId}><Markdown text={expanded ? text : preview} onEvidence={onEvidence} /></div>{preview !== text.trim() ? <button className="text-link" onClick={() => setExpanded(!expanded)} aria-expanded={expanded} aria-controls={bodyId}>{expanded ? "收起完整回复" : "展开完整回复"}</button> : null}</>;
}
function ReportCard({ run, round, onOpen }: { run: Run; round: number; onOpen: () => void }) {
  return <button className="report-artifact" onClick={onOpen}>
    <span className="report-file-icon"><FileText size={23} aria-hidden="true" /></span>
    <span className="report-card-title"><strong>{run.title || "分析报告"}</strong><small>第 {round} 轮 · 结论、依据与待核对项</small></span>
    <span className="report-card-action"><Status value={run.status}>{run.status === "partial" ? "部分覆盖" : undefined}</Status><span>查看报告<ArrowUpRight size={14} aria-hidden="true" /></span></span>
  </button>;
}
export default function MessageTimeline({ messages, runs, attachments, liveRun, onOpenReport, onEvidence, onEvents, onFollowup }: {
  messages: Entity[]; runs: Run[]; attachments: Entity[]; liveRun: Run | null;
  onOpenReport: (id: string) => void; onEvidence: (id: string, runId?: string) => void; onEvents: (id: string) => void; onFollowup: (v: string) => void;
}) {
  const scroll = useRef<HTMLDivElement>(null), nearBottom = useRef(true);
  const runMap = new Map(runs.map((r, i) => [r.id, { run: r, round: i + 1 }]));
  const withReport = new Set(messages.filter((m) => m.role === "assistant" && m.run_id).map((m) => m.run_id));
  useEffect(() => {
    if (nearBottom.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight;
  }, [messages.length, runs.filter((r) => r.report).length, liveRun?.events?.length]);
  return <div className="message-timeline" ref={scroll} onScroll={() => { const el = scroll.current; if (el) nearBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 100; }}>
    {!messages.length ? <div className="chat-welcome"><span className="aiopsstudio-mark">A</span><h2>今天想分析什么？</h2><p>选择 Skill 并提供目标或告警。报告、原始证据和后续追问会保存在同一会话中。</p></div> : null}
    <ol className="conversation-messages" aria-label="会话消息">
      {messages.map((m, index) => {
        const user = m.role === "user", linked = runMap.get(m.run_id), report = !user && linked?.run.report;
        let content = textValue(m.content);
        if (report && typeof report === "object") {
          const analysis = textValue((report as Entity).analysis);
          content = analysis || content || textValue((report as Entity).summary);
        }
        return <li key={m.id || `${m.role}-${index}`} className={`conversation-message ${user ? "from-user" : "from-assistant"}`}>
          <span className={`message-avatar ${user ? "" : "aiopsstudio-mark"}`} aria-label={user ? "你" : "AIOpsStudio"}>{user ? "U" : "A"}</span>
          <div className="message-content">
            <div className="message-text"><MessageBody text={content} summarize={!user} onEvidence={(id) => onEvidence(id, m.run_id)} /></div>
            <time className="message-time">{dateTime(m.created_at)}</time>
            {m.status === "not_executed" ? <div className="message-needs-input" role="status"><strong>消息已保存，尚未执行</strong><p>{textValue(m.hint || m.error) || "请补充分析上下文后，显式再次发送问题。"}</p></div> : null}
            {m.status === "unprocessed" ? <div className="message-needs-input"><strong>本轮未处理，可继续发送到新一轮</strong><p>{textValue(m.reason) || "此消息未在本轮最后边界前被模型消费。"}</p><button className="text-link" onClick={() => onFollowup(textValue(m.content))}>将原问题填入输入框</button></div> : null}
            {m.error && m.status !== "not_executed" ? <div className="message-needs-input">保存或执行提示：{textValue(m.error)}</div> : null}
            {m.attachment_ids?.length ? <div className="message-attachments">{m.attachment_ids.map((id: string) => <a key={id} href={`/api/attachments/${encodeURIComponent(id)}/download`}><Paperclip size={12} aria-hidden="true" />{attachments.find((a) => a.id === id)?.name || "用户附件"}</a>)}</div> : null}
            {report && linked ? <ReportCard run={linked.run} round={linked.round} onOpen={() => onOpenReport(linked.run.id)} /> : null}
          </div>
        </li>;
      })}
    </ol>
    {liveRun ? <div className="assistant-progress">
      <span className="aiopsstudio-mark">A</span><div><button className="progress-disclosure" onClick={() => onEvents(liveRun.id)}><CheckCircle2 size={17} aria-hidden="true" /><strong>{activeRun(liveRun.status) ? (liveRun.status === "queued" ? "等待分析执行槽" : "正在分析并保存执行记录") : "本轮执行记录已保存"}</strong><Status value={liveRun.status} /></button><small>{liveRun.evidence?.length || 0} 份证据 · <button className="text-link" onClick={() => onEvents(liveRun.id)}>查看执行过程</button></small></div>
    </div> : null}
    {runs.map((run, i) => run.report && !withReport.has(run.id) ? <ReportCard key={run.id} run={run} round={i + 1} onOpen={() => onOpenReport(run.id)} /> : null)}
    {runs.some((r) => r.report) ? <div className="followup-suggestions"><span>你可以继续问：</span><Button onClick={() => onFollowup("请继续核对上一轮报告中的未覆盖范围，并说明需要补充的证据。 ")}><MessageSquare size={14} aria-hidden="true" />核对未覆盖范围</Button></div> : null}
  </div>;
}
