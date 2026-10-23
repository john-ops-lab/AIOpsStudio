import { useState } from "react";
import { Save } from "lucide-react";
import type { Entity } from "../../types";
import { Button, Field, Modal, Notice } from "../ui";
import { analysisContext, localDate } from "./context";
import { json, parseJson } from "../../api";
const namedWindows = [["baseline", "对照"], ["change", "变更"], ["validation", "验证"]] as const;
export default function ContextEditor({ context, title, skills, models, onClose, onSave }: {
  context: Entity; title: string; skills: Entity[]; models: Entity[];
  onClose: () => void; onSave: (value: Entity, title: string) => Promise<void>;
}) {
  const [form, setForm] = useState<Entity>(() => ({ ...analysisContext(context), analysis_start: localDate(context.analysis_start), analysis_end: localDate(context.analysis_end), target_identity_json: context.target_identity ? json(context.target_identity) : "", ...Object.fromEntries(namedWindows.flatMap(([key]) => { const window = context.windows?.[key] || context[`${key}_window`] || {}; return [[`${key}_start`, localDate(window.start)], [`${key}_end`, localDate(window.end)], [`${key}_reason`, window.reason || ""]]; })) }));
  const [name, setName] = useState(title), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const set = (key: string, value: string) => setForm((f) => ({ ...f, [key]: value }));
  return <Modal title="分析上下文" onClose={onClose} wide>
    <form className="stack" onSubmit={async (event) => {
      event.preventDefault(); setError("");
      if (Boolean(form.analysis_start) !== Boolean(form.analysis_end)) { setError("固定时间窗须同时填写开始与结束时间。"); return; }
      if (form.analysis_start && new Date(form.analysis_start) >= new Date(form.analysis_end)) { setError("开始时间必须早于结束时间。"); return; }
      setBusy(true);
      try {
        const windows: Entity = {};
        for (const [key, label] of namedWindows) {
          const start = form[`${key}_start`], end = form[`${key}_end`];
          if (Boolean(start) !== Boolean(end)) throw new Error(`${label}窗口须同时填写开始与结束。`);
          if (start && new Date(start) >= new Date(end)) throw new Error(`${label}窗口开始须早于结束。`);
          if (start && end) windows[key] = { start: new Date(start).toISOString(), end: new Date(end).toISOString(), reason: form[`${key}_reason`] || "explicit_input" };
        }
        const next = analysisContext(form);
        delete next.windows; delete next.target_identity;
        for (const [key] of namedWindows) delete next[`${key}_window`];
        if (Object.keys(windows).length) next.windows = windows;
        if (form.target_identity_json.trim()) next.target_identity = parseJson(form.target_identity_json, "完整对象主键", "object");
        await onSave({ ...next, analysis_start: form.analysis_start ? new Date(form.analysis_start).toISOString() : "", analysis_end: form.analysis_end ? new Date(form.analysis_end).toISOString() : "" }, name);
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
        <Field label="集群范围"><input value={form.cluster || ""} onChange={(e) => set("cluster", e.target.value)} placeholder="用于区分同名对象" /></Field>
        <Field label="Namespace"><input value={form.namespace || ""} onChange={(e) => set("namespace", e.target.value)} placeholder="用于区分同名对象" /></Field>
        <Field label="对象类型"><input value={form.target_type || ""} onChange={(e) => set("target_type", e.target.value)} placeholder="例如 service、pod" /></Field>
        <Field label="Skill 版本"><input readOnly value={form.skill_version || "使用提交时已发布版本"} /></Field>
      </div>
      <details className="context-advanced"><summary>完整对象主键与补充窗口</summary><div className="stack"><Field label="完整对象主键（JSON，可选）" hint="须含 environment、cluster、namespace、type、id，服务端仍会核对已登记映射；不能填写自报原生源 ID。"><textarea aria-label="完整对象主键JSON" className="code-input" value={form.target_identity_json} onChange={(e) => set("target_identity_json", e.target.value)} spellCheck={false} /></Field>{namedWindows.map(([key, label]) => <div className="form-grid" key={key}><Field label={`${label}开始时间`}><input type="datetime-local" step={1} value={form[`${key}_start`]} onChange={(e) => set(`${key}_start`, e.target.value)} /></Field><Field label={`${label}结束时间`}><input type="datetime-local" step={1} value={form[`${key}_end`]} onChange={(e) => set(`${key}_end`, e.target.value)} /></Field><Field label={`${label}窗口选取原因`} hint={key === "baseline" ? "较早时段不自动等于健康基线，流量与健康可比性另行核验。" : undefined}><input value={form[`${key}_reason`]} onChange={(e) => set(`${key}_reason`, e.target.value)} /></Field></div>)}</div></details>
      <p className="form-note">修改 Skill、模型、目标、告警或时间窗后，下次发送将开启独立分析轮次；既有报告与证据继续保留。</p>
      <div className="form-actions"><Button type="button" onClick={onClose}>取消</Button><Button variant="primary" type="submit" busy={busy}><Save size={15} />保存上下文</Button></div>
    </form>
  </Modal>;
}
