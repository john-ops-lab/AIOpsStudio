import { useEffect, useRef, useState } from "react";
import { ChevronRight, Download, FileText, ListTree, PanelRightClose } from "lucide-react";
import type { Entity, Run } from "../../types";
import { dateTime } from "../../api";
import { Report } from "../../pages/Runs";
import { Button, Empty, JsonView, Loading, Modal, Notice, Status } from "../ui";
import { isReportAttachment, textValue } from "./context";
export type CanvasTab = "report" | "evidence" | "events";
export default function AnalysisCanvas({ run, runs, attachments, selected, onSelect, tab, onTab, onClose, onEvidence, loading, error }: {
  run: Run | null; runs: Run[]; attachments: Entity[]; selected: string | null; onSelect: (id: string) => void;
  tab: CanvasTab; onTab: (tab: CanvasTab) => void; onClose: () => void; onEvidence: (id: string) => void; loading: boolean; error: string;
}) {
  const [narrow, setNarrow] = useState(() => window.matchMedia("(max-width: 1050px)").matches);
  const tabs = useRef<HTMLDivElement>(null);
  useEffect(() => { const media = window.matchMedia("(max-width: 1050px)"); const change = () => setNarrow(media.matches); media.addEventListener("change", change); return () => media.removeEventListener("change", change); }, []);
  const allTabs: [CanvasTab, string][] = [["report", "报告"], ["evidence", `证据 ${run?.evidence?.length || 0}`], ["events", "执行记录"]];
  const reportFile = attachments.find((a) => a.run_id === run?.id && isReportAttachment(a));
  const content = <>
    <div ref={tabs} className="canvas-tabs" role="tablist" aria-label="分析画布内容" onKeyDown={(e) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
      e.preventDefault(); const current = allTabs.findIndex(([id]) => id === tab);
      const next = e.key === "Home" ? 0 : e.key === "End" ? 2 : (current + (e.key === "ArrowRight" ? 1 : -1) + 3) % 3;
      onTab(allTabs[next][0]); tabs.current?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
    }}>{allTabs.map(([id, label]) => <button key={id} id={`canvas-tab-${id}`} role="tab" aria-selected={tab === id} aria-controls="canvas-panel" tabIndex={tab === id ? 0 : -1} className={tab === id ? "active" : ""} onClick={() => onTab(id)}>{label}</button>)}</div>
    <div className="canvas-scroll" id="canvas-panel" role="tabpanel" aria-labelledby={`canvas-tab-${tab}`} tabIndex={0}>
      <div className="canvas-round"><label className="sr-only" htmlFor="canvas-round">选择分析轮次</label><select id="canvas-round" value={selected || ""} onChange={(e) => onSelect(e.target.value)}><option value="" disabled>选择分析轮次</option>{runs.map((r, i) => <option key={r.id} value={r.id}>第 {i + 1} 轮 · {r.title || r.id}</option>)}</select>{run ? <Status value={run.status}>{run.status === "partial" ? "部分覆盖" : undefined}</Status> : null}</div>
      <Notice error={error} />
      {loading ? <Loading /> : !run ? <Empty title="尚无分析轮次" detail="发送问题并补齐上下文后，报告与证据会保存在这里。" /> : <>
        <div className="canvas-run-meta"><h2>{run.title || "分析记录"}</h2><p>{run.analysis_start ? `${dateTime(run.analysis_start)} — ${dateTime(run.analysis_end)}` : "此轮未设置时间窗"}</p></div>
        {run.error ? <Notice error={textValue(run.error)} /> : null}
        {run.artifact_error || run.report_attachment_error ? <Notice error={`本轮执行已结束，但回复或报告制品保存失败：${textValue(run.artifact_error || run.report_attachment_error)}`} /> : null}
        {tab === "report" ? <><Report report={run.report} title="分析报告" onEvidence={onEvidence} />{reportFile ? <a className="btn report-download" href={`/api/attachments/${encodeURIComponent(reportFile.id)}/download`}><Download size={15} aria-hidden="true" />下载本轮 Markdown 报告</a> : null}</> : null}
        {tab === "evidence" ? run.evidence?.length ? <ol className="canvas-evidence-list">{run.evidence.map((e, i) => <li key={e.id}>
          <div className="evidence-card-heading"><FileText size={17} aria-hidden="true" /><strong>证据 {i + 1} · {e.tool_name || e.source || "源响应"}</strong><Status value={e.status} /></div>
          <dl><div><dt>来源</dt><dd>{e.source || e.tool_name || "未提供"}</dd></div><div><dt>保存时间</dt><dd>{dateTime(e.created_at)}</dd></div><div><dt>证据 ID</dt><dd><code>{e.id}</code></dd></div></dl>
          {e.truncated ? <p className="scope-note">响应存在截断，完整性以证据元数据为准。</p> : null}
          <div className="actions"><Button onClick={() => onEvidence(e.id)}>查看查询<ChevronRight size={14} aria-hidden="true" /></Button><Button onClick={() => onEvidence(e.id)}>原始响应</Button></div>
        </li>)}</ol> : <Empty title="尚无已保存证据" detail="这里仅展示本轮实际留存的源系统响应。" /> : null}
        {tab === "events" ? run.events?.length ? <ol className="canvas-events">{run.events.map((event, i) => <li key={event.id || event.seq || i}><ListTree size={15} aria-hidden="true" /><div><strong>{event.type}</strong><time>{dateTime(event.created_at || event.timestamp)}</time><JsonView title="事件详情" value={event.payload ?? event} /></div></li>)}</ol> : <Empty title="尚无持久执行事件" /> : null}
      </>}
    </div>
  </>;
  return narrow ? <Modal title="分析画布" onClose={onClose} wide><div className="analysis-canvas mobile-canvas">{content}</div></Modal> : <aside className="analysis-canvas" aria-label="分析画布"><header><h2>分析画布</h2><Button variant="ghost" onClick={onClose} aria-label="收起分析画布"><PanelRightClose size={18} /></Button></header>{content}</aside>;
}
