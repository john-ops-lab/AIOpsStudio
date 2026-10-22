import { useState } from "react";
import { Save } from "lucide-react";
import type { Entity } from "../../types";
import { Button, Field, Modal, Notice } from "../ui";
import { analysisContext, localDate } from "./context";
export default function ContextEditor({ context, title, skills, models, onClose, onSave }: {
  context: Entity; title: string; skills: Entity[]; models: Entity[];
  onClose: () => void; onSave: (value: Entity, title: string) => Promise<void>;
}) {
  const [form, setForm] = useState<Entity>(() => ({ ...analysisContext(context), analysis_start: localDate(context.analysis_start), analysis_end: localDate(context.analysis_end) }));
  const [name, setName] = useState(title), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const set = (key: string, value: string) => setForm((f) => ({ ...f, [key]: value }));
  return <Modal title="分析上下文" onClose={onClose} wide>
    <form className="stack" onSubmit={async (event) => {
      event.preventDefault(); setError("");
      if (Boolean(form.analysis_start) !== Boolean(form.analysis_end)) { setError("固定时间窗须同时填写开始与结束时间。"); return; }
      if (form.analysis_start && new Date(form.analysis_start) >= new Date(form.analysis_end)) { setError("开始时间必须早于结束时间。"); return; }
      setBusy(true);
      try {
        await onSave({ ...form, analysis_start: form.analysis_start ? new Date(form.analysis_start).toISOString() : "", analysis_end: form.analysis_end ? new Date(form.analysis_end).toISOString() : "" }, name);
        onClose();
      } catch (err) { setError(err instanceof Error ? err.message : String(err)); }
      finally { setBusy(false); }
    }}>
      <Notice error={error} />
      <Field label="会话名称"><input value={name} onChange={(e) => setName(e.target.value)} maxLength={160} /></Field>
      <div className="form-grid">
        <Field label="分析 Skill" hint="正式分析只使用已发布且启用的 Skill。">
          <select value={form.skill_id} onChange={(e) => { set("skill_id", e.target.value); set("skill_version", ""); }}>
            <option value="">尚未选择</option>{skills.map((s) => <option key={s.id} value={s.id}>{s.display_name || s.name}</option>)}
          </select>
        </Field>
        <Field label="分析模型"><select value={form.model_id} onChange={(e) => set("model_id", e.target.value)}><option value="">默认模型</option>{models.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.model}</option>)}</select></Field>
        <Field label="目标对象"><input value={form.target} onChange={(e) => set("target", e.target.value)} placeholder="资源、服务或源对象标识" /></Field>
        <Field label="告警 / 事件 ID"><input value={form.incident_id} onChange={(e) => set("incident_id", e.target.value)} /></Field>
        <Field label="分析开始时间"><input type="datetime-local" step={1} value={form.analysis_start} onChange={(e) => set("analysis_start", e.target.value)} /></Field>
        <Field label="分析结束时间" hint="使用当前时区输入，保存为固定 UTC 时间窗。"><input type="datetime-local" step={1} value={form.analysis_end} onChange={(e) => set("analysis_end", e.target.value)} /></Field>
        <Field label="数据环境"><input value={form.environment} onChange={(e) => set("environment", e.target.value)} placeholder="可选" /></Field>
        <Field label="Skill 版本"><input readOnly value={form.skill_version || "使用提交时已发布版本"} /></Field>
      </div>
      <p className="form-note">修改 Skill、模型、目标、告警或时间窗后，下次发送将开启独立分析轮次；既有报告与证据继续保留。</p>
      <div className="form-actions"><Button type="button" onClick={onClose}>取消</Button><Button variant="primary" type="submit" busy={busy}><Save size={15} />保存上下文</Button></div>
    </form>
  </Modal>;
}
