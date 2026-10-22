import { useRef } from "react";
import { ArrowUp, FileUp, LockKeyhole, Paperclip, Plus, Settings2, X } from "lucide-react";
import type { Entity } from "../../types";
import { Button } from "../ui";
import { sizeLabel } from "./context";
export default function Composer({ value, onChange, onSend, busy, uploading, attachments, selectedAttachments, onRemoveAttachment, onUpload, onContext, onTools, models, modelId, onModel, skillName, active, error }: {
  value: string; onChange: (v: string) => void; onSend: () => void; busy: boolean; uploading: string;
  attachments: Entity[]; selectedAttachments: string[]; onRemoveAttachment: (id: string) => void;
  onUpload: (files: File[]) => void; onContext: () => void; onTools: () => void;
  models: Entity[]; modelId: string; onModel: (id: string) => void; skillName?: string; active: boolean; error?: string;
}) {
  const input = useRef<HTMLInputElement>(null);
  return <div className="composer-wrap">
    <form className="workbench-composer" onSubmit={(e) => { e.preventDefault(); onSend(); }} aria-busy={busy || Boolean(uploading)}>
      <div className="composer-tools">
        <Button type="button" onClick={onContext}><Plus size={15} aria-hidden="true" />添加上下文</Button>
        <Button type="button" onClick={onContext}><span aria-hidden="true">/</span>{skillName || "选择 Skill"}</Button>
        <Button type="button" onClick={onTools}><Settings2 size={15} aria-hidden="true" />工具范围</Button>
        <Button type="button" onClick={() => input.current?.click()} disabled={Boolean(uploading)} aria-label="上传用户材料附件"><Paperclip size={15} aria-hidden="true" /><span className="hide-small">附件</span></Button>
        <input ref={input} type="file" multiple className="sr-only" tabIndex={-1} aria-label="选择附件文件" onChange={(e) => { if (e.target.files) onUpload(Array.from(e.target.files)); e.target.value = ""; }} />
      </div>
      {selectedAttachments.length || uploading ? <ul className="pending-attachments" aria-label="本条消息附件">
        {attachments.filter((a) => selectedAttachments.includes(a.id)).map((a) => <li key={a.id}>
          <FileUp size={14} aria-hidden="true" /><span>{a.name}<small>{sizeLabel(Number(a.size || 0))} · {a.extraction_status === "text" ? "可读取文本" : "未提取内容"}</small></span>
          <button type="button" aria-label={`从待发送消息移除 ${a.name}`} onClick={() => onRemoveAttachment(a.id)}><X size={14} /></button>
        </li>)}
        {uploading ? <li role="status">正在上传 {uploading}…</li> : null}
      </ul> : null}
      <label className="sr-only" htmlFor="analysis-message">分析问题或补充信息</label>
      <textarea id="analysis-message" rows={3} placeholder="描述你想分析的问题，或继续追问…" value={value} onChange={(e) => onChange(e.target.value)} aria-describedby="composer-hint composer-error" aria-invalid={Boolean(error)} onKeyDown={(e) => {
        if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing) { e.preventDefault(); onSend(); }
      }} />
      <div className="composer-bottom">
        <div className="composer-options">
          <select aria-label="选择分析模型" value={modelId} onChange={(e) => onModel(e.target.value)}><option value="">默认模型</option>{models.map((m) => <option key={m.id} value={m.id}>{m.name || m.model}</option>)}</select>
          <span className="readonly-label"><LockKeyhole size={13} aria-hidden="true" />只读分析</span>
        </div>
        <Button variant="primary" type="submit" busy={busy} disabled={!value.trim() || Boolean(uploading)} aria-label="发送分析消息">发送<ArrowUp size={16} aria-hidden="true" /></Button>
      </div>
      <p id="composer-error" className="composer-error" role={error ? "alert" : undefined}>{error || ""}</p>
    </form>
    <p className="composer-hint" id="composer-hint">{active ? "同上下文的补充进入当前轮；上下文改变时开启新轮。" : "继续追问将开启新一轮分析，保留现有报告与证据。"}<span>⌘ / Ctrl + Enter 发送</span></p>
  </div>;
}
