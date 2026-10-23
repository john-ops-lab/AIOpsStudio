import { useEffect, useRef, useState } from "react";
import { Bell, Clock3, Download, Layers3, MoreHorizontal, PanelRightOpen, Paperclip, Plus, RefreshCw, StopCircle } from "lucide-react";
import { api, dateTime, list, post, put, requestId, useAction, useResource } from "../api";
import type { Conversation, Entity, Run } from "../types";
import { Button, JsonView, Loading, Modal, Notice, Status } from "../components/ui";
import { EvidenceModal } from "./Runs";
import ContextEditor from "../components/workbench/ContextEditor";
import Composer from "../components/workbench/Composer";
import MessageTimeline from "../components/workbench/MessageTimeline";
import AnalysisCanvas from "../components/workbench/AnalysisCanvas";
import type { CanvasTab } from "../components/workbench/AnalysisCanvas";
import { activeRun, analysisContext, fileBase64, isReportAttachment, sizeLabel, textValue } from "../components/workbench/context";
export default function Workbench({ selected, initialMessage = "", onSelect, onChanged }: {
  selected: string | null; initialMessage?: string; onSelect: (id: string) => void; onChanged: () => void;
}) {
  const conversation = useResource<Conversation>(selected ? `/conversations/${encodeURIComponent(selected)}` : null, 3500);
  const skillsResource = useResource("/skills"), modelsResource = useResource("/model-configs");
  const skills = list(skillsResource.data).filter((s) => s.current_version && s.enabled !== false && !s.blocked);
  const models = list(modelsResource.data).filter((m) => m.current_version && m.enabled !== false && !m.blocked);
  const [context, setContext] = useState<Entity>({}), [title, setTitle] = useState("新会话"), [message, setMessage] = useState(initialMessage);
  const [contextOpen, setContextOpen] = useState(false), [toolsOpen, setToolsOpen] = useState(false), [attachmentsOpen, setAttachmentsOpen] = useState(false);
  const [canvasOpen, setCanvasOpen] = useState(false), [tab, setTab] = useState<CanvasTab>("report"), [selectedRun, setSelectedRun] = useState<string | null>(null);
  const [evidence, setEvidence] = useState<string | null>(null), [staged, setStaged] = useState<string[]>([]), [uploading, setUploading] = useState(""), [uploadError, setUploadError] = useState("");
  const state = useAction(), activeId = useRef(selected), hydratedId = useRef<string | null>(null), createId = useRef(requestId()), creating = useRef<Promise<Conversation> | null>(null);
  const pending = useRef<{ fingerprint: string; id: string } | null>(null), lastAutoCanvas = useRef<string | null>(null);
  const runs = (conversation.data?.runs || []) as Run[];
  const currentRun = runs.filter((r) => activeRun(r.status)).at(-1) || runs.at(-1) || null;
  const runResource = useResource<Run>(selectedRun ? `/runs/${encodeURIComponent(selectedRun)}` : null, selectedRun ? 4000 : 0);
  const activeResource = useResource<Run>(currentRun && activeRun(currentRun.status) && currentRun.id !== selectedRun ? `/runs/${encodeURIComponent(currentRun.id)}` : null, 4000);
  const liveRun = currentRun?.id === selectedRun ? runResource.data : activeResource.data;
  const attachments = conversation.data?.attachments || [];
  const archived = Boolean(conversation.data?.legacy_archive);
  const artifactError = liveRun?.artifact_error || liveRun?.report_attachment_error || currentRun?.artifact_error || currentRun?.report_attachment_error || conversation.data?.report_attachment_error;
  useEffect(() => {
    if (conversation.accessDenied) {
      hydratedId.current = null;
      setContext({}); setTitle("会话不可访问"); setStaged([]); setSelectedRun(null);
      setEvidence(null); setAttachmentsOpen(false); setContextOpen(false); setCanvasOpen(false);
    }
  }, [conversation.accessDenied]);
  useEffect(() => { if (runResource.accessDenied) setEvidence(null); }, [runResource.accessDenied]);

  useEffect(() => {
    if (selected === activeId.current) return;
    activeId.current = selected; hydratedId.current = null; creating.current = null; createId.current = requestId(); pending.current = null;
    setContext({}); setTitle("新会话"); setMessage(initialMessage); setStaged([]); setSelectedRun(null); setCanvasOpen(false); setEvidence(null); setUploadError(""); state.setError(""); state.setNotice("");
  }, [selected, initialMessage]);
  useEffect(() => {
    const data = conversation.data;
    if (!data || data.id !== selected) return;
    if (hydratedId.current !== data.id) { hydratedId.current = data.id; setContext(analysisContext(data.context)); setTitle(data.title || "新会话"); if (initialMessage) setMessage(initialMessage); }
    if (!selectedRun && data.runs?.length) setSelectedRun(data.runs.at(-1)!.id);
  }, [conversation.data, selected, selectedRun, initialMessage]);
  useEffect(() => {
    const run = runResource.data;
    if (run?.report && lastAutoCanvas.current !== run.id) {
      lastAutoCanvas.current = run.id;
      if (window.matchMedia("(min-width: 1051px)").matches) setCanvasOpen(true);
    }
  }, [runResource.data]);
  const currentActiveId = currentRun && activeRun(currentRun.status) ? currentRun.id : null;
  useEffect(() => {
    if (!currentActiveId) return;
    let timer: number | undefined;
    const stream = new EventSource(`/api/runs/${encodeURIComponent(currentActiveId)}/events?after=0`, { withCredentials: true });
    stream.onmessage = () => {
      window.clearTimeout(timer); timer = window.setTimeout(() => { void conversation.reload(); void runResource.reload(); void activeResource.reload(); }, 150);
    };
    return () => { stream.close(); window.clearTimeout(timer); };
  }, [currentActiveId, conversation.reload, runResource.reload, activeResource.reload]);
  async function refresh(id: string) {
    const data = await api<Conversation>(`/conversations/${encodeURIComponent(id)}`);
    if (activeId.current === id) conversation.setData(data);
    onChanged(); return data;
  }
  async function ensureConversation() {
    if (activeId.current) return activeId.current;
    if (!creating.current) creating.current = post<Conversation>("/conversations", { title: title === "新会话" ? undefined : title, context: analysisContext(context), client_request_id: createId.current });
    try {
      const data = await creating.current;
      activeId.current = data.id; hydratedId.current = data.id; conversation.setData(data); onSelect(data.id); onChanged(); return data.id;
    } catch (err) { creating.current = null; throw err; }
  }
  async function send() {
    const content = message.trim();
    if (!content || uploading) return;
    const runInput = analysisContext(context), attachmentIds = [...staged];
    const fingerprint = JSON.stringify([content, attachmentIds, runInput]);
    if (pending.current?.fingerprint !== fingerprint) pending.current = { fingerprint, id: requestId() };
    const clientMessageId = pending.current.id;
    await state.act("send", async () => {
      const id = await ensureConversation();
      const receipt = await post<{ message: Entity; run: Run | null; conversation: Conversation }>(`/conversations/${encodeURIComponent(id)}/messages`, { content, attachment_ids: attachmentIds, client_message_id: clientMessageId, run_input: runInput });
      pending.current = null;
      if (activeId.current === id) {
        setStaged([]);
        if (receipt.run) { setMessage(""); setSelectedRun(receipt.run.id); state.setNotice("消息与执行收据已保存。"); }
        else { state.setNotice(textValue(receipt.message.hint || receipt.message.error) || "消息已保存，尚未执行。请补齐上下文后再次发送。"); setContextOpen(true); }
      }
      await refresh(id).catch(() => { if (activeId.current === id) state.setNotice("消息已受理，正在重新加载持久记录；请勿重复提交。"); });
    });
  }
  async function upload(files: File[]) {
    if (!files.length || uploading) return;
    setUploadError("");
    let destination: string | null = null;
    for (const file of files) {
      if (destination && activeId.current !== destination) break;
      if (file.size > 5 * 1024 * 1024) { setUploadError(`${file.name} 超过单件 5 MiB 限额，未上传。`); continue; }
      setUploading(file.name);
      try {
        const id: string = destination || await ensureConversation();
        destination = id;
        const saved = await post(`/conversations/${encodeURIComponent(id)}/attachments`, { name: file.name, media_type: file.type || "application/octet-stream", content_base64: await fileBase64(file) });
        if (activeId.current === id) setStaged((items) => [...new Set([...items, saved.id])]);
        await refresh(id).catch(() => { if (activeId.current === id) state.setNotice("附件已上传，正在重新加载附件列表。 "); });
      } catch (err) { setUploadError(err instanceof Error ? err.message : String(err)); }
      finally { setUploading(""); }
    }
  }
  function openRun(id: string, selectedTab: CanvasTab = "report") { setSelectedRun(id); setTab(selectedTab); setCanvasOpen(true); }
  function openEvidence(id: string, runId?: string) { if (runId) setSelectedRun(runId); setTab("evidence"); setCanvasOpen(true); setEvidence(id); }
  const selectedSkill = skills.find((s) => s.id === context.skill_id);
  return <div className={`chat-workbench ${canvasOpen ? "canvas-open" : ""}`}>
    <main className="conversation-main" aria-label="运维会话">
      <header className="conversation-header"><div><h1>{title}</h1>{currentRun ? <Status value={currentRun.status}>{currentRun.status === "partial" ? "本轮完成 · 部分覆盖" : undefined}</Status> : null}</div><div className="actions"><Button variant="ghost" onClick={() => setAttachmentsOpen(true)} aria-label="查看会话附件"><Paperclip size={17} /><span className="hide-small">{attachments.length || ""}</span></Button><Button variant="ghost" onClick={() => { if (!selectedRun && currentRun) setSelectedRun(currentRun.id); setCanvasOpen(!canvasOpen); }} aria-label={canvasOpen ? "收起分析画布" : "打开分析画布"} aria-expanded={canvasOpen} aria-controls="workbench-canvas"><PanelRightOpen size={18} /></Button><Button variant="ghost" onClick={() => setContextOpen(true)} aria-label="编辑会话与上下文"><MoreHorizontal size={19} /></Button></div></header>
      <div className="context-bar" aria-label="当前分析上下文">
        {context.incident_id ? <button onClick={() => setContextOpen(true)}><Bell size={14} />告警 #{context.incident_id}</button> : null}
        {context.target ? <button onClick={() => setContextOpen(true)} title={context.target}><Layers3 size={14} />{context.target}</button> : null}
        {context.analysis_start ? <button onClick={() => setContextOpen(true)}><Clock3 size={14} />{dateTime(context.analysis_start)} — {dateTime(context.analysis_end)}</button> : null}
        {selectedSkill ? <button onClick={() => setContextOpen(true)}><Layers3 size={14} />Skill：{selectedSkill.display_name || selectedSkill.name}</button> : null}
        <button onClick={() => setContextOpen(true)}><Plus size={14} />添加上下文</button>
      </div>
      <div className="conversation-notices"><Notice error={conversation.error || skillsResource.error || modelsResource.error || uploadError} message={state.notice} />{artifactError ? <Notice error={`回复或报告制品保存失败：${textValue(artifactError)}`} /> : null}{currentActiveId ? <Button variant="ghost" busy={state.busy === "cancel"} onClick={() => void state.act("cancel", async () => { await post(`/runs/${encodeURIComponent(currentActiveId)}/cancel`); await refresh(activeId.current!); }, "已提交本轮取消请求。 ")}><StopCircle size={14} />取消当前轮</Button> : null}{conversation.error ? <Button onClick={() => void conversation.reload()}><RefreshCw size={14} />重新加载</Button> : null}</div>
      {conversation.loading ? <Loading /> : <MessageTimeline messages={conversation.data?.messages || []} runs={runs} attachments={attachments} liveRun={liveRun || currentRun} onOpenReport={openRun} onEvidence={openEvidence} onEvents={(id) => openRun(id, "events")} onFollowup={(value) => { setMessage(value); document.getElementById("analysis-message")?.focus(); }} />}
      {archived ? <p className="scope-note" role="status">升级前历史只读档案：原内容与附件保留，来源未按新契约核验。继续分析请新建会话，旧材料不会自动送入模型。</p> : <Composer value={message} onChange={setMessage} onSend={() => void send()} busy={state.busy === "send"} uploading={uploading} attachments={attachments} selectedAttachments={staged} onRemoveAttachment={(id) => setStaged((items) => items.filter((a) => a !== id))} onUpload={(files) => void upload(files)} onContext={() => setContextOpen(true)} onTools={() => setToolsOpen(true)} models={models} modelId={context.model_id || ""} onModel={(id) => setContext((c) => ({ ...c, model_id: id }))} skillName={selectedSkill?.display_name || selectedSkill?.name} active={Boolean(currentActiveId)} error={state.error} />}
    </main>
    {canvasOpen ? <div id="workbench-canvas" className="canvas-host"><AnalysisCanvas run={runResource.data} runs={runs} attachments={attachments} selected={selectedRun} onSelect={setSelectedRun} tab={tab} onTab={setTab} onClose={() => setCanvasOpen(false)} onEvidence={(id) => openEvidence(id, selectedRun || undefined)} loading={runResource.loading} error={runResource.error} /></div> : null}
    {contextOpen && !archived ? <ContextEditor context={context} title={title} skills={skills} models={models} onClose={() => setContextOpen(false)} onSave={async (value, name) => {
      if (selected) { const result = await put<Conversation>(`/conversations/${encodeURIComponent(selected)}`, { context: value, title: name, expected_revision: conversation.data?.revision }); conversation.setData(result); onChanged(); }
      setContext(value); setTitle(name.trim() || "新会话");
    }} /> : null}
    {toolsOpen ? <Modal title="本轮工具范围" onClose={() => setToolsOpen(false)}><p>实际工具由所选 Skill 的依赖绑定和 MCP 只读白名单共同限定。会话声明不能扩大工具权限。</p>{selectedSkill ? <JsonView title="所选 Skill 依赖" open value={selectedSkill.dependencies || []} /> : <p className="scope-note">尚未选择可用 Skill，请先补充上下文。</p>}</Modal> : null}
    {attachmentsOpen ? <Modal title="会话附件" onClose={() => setAttachmentsOpen(false)} wide><p className="muted">用户材料与源系统证据分别保存。单件上限 5 MiB，用户附件总量上限 100 MiB；未提取的文件不会声称已被分析读取。</p>{attachments.length ? <ul className="saved-attachments">{attachments.map((a) => <li key={a.id}><FileAttachment value={a} /><div className="actions">{!archived && !isReportAttachment(a) ? <Button onClick={() => { setStaged((items) => [...new Set([...items, a.id])]); setAttachmentsOpen(false); }}>添加到消息</Button> : null}<a className="btn" href={`/api/attachments/${encodeURIComponent(a.id)}/download`}><Download size={14} />下载</a></div></li>)}</ul> : <p>尚无已保存附件。</p>}</Modal> : null}
    {evidence ? <EvidenceModal id={evidence} onClose={() => setEvidence(null)} /> : null}
  </div>;
}
function FileAttachment({ value: a }: { value: Entity }) { return <div className="attachment-details"><strong>{a.name}</strong><small>{sizeLabel(Number(a.size || 0))} · {isReportAttachment(a) ? "分析报告制品" : "用户材料"} · {a.extraction_status === "text" ? "可读取文本" : "未提取内容"}</small>{a.extraction_status === "text" && Number(a.text_char_count || 0) > 12000 ? <small>文本超过单件读取上限，模型材料按上限截断；原文件可下载。</small> : null}{a.sha256 ? <code title={a.sha256}>SHA-256 {a.sha256}</code> : null}</div>; }
