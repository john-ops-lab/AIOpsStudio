import { useEffect, useState } from "react";
import { Play } from "lucide-react";
import { list, post, requestId, useAction, useResource } from "../api";
import type { Run, RunPreset } from "../types";
import { Button, Field, Loading, Modal, Notice } from "./ui";
const localDate = (value?: string) => {
  if (!value) return "";
  const d = new Date(value);
  return Number.isNaN(d.getTime())
    ? ""
    : new Date(d.getTime() - d.getTimezoneOffset() * 60000)
        .toISOString()
        .slice(0, 19);
};
export default function NewRun({
  preset = {},
  onClose,
  onCreated,
}: {
  preset?: RunPreset;
  onClose: () => void;
  onCreated: (run: Run) => void;
}) {
  const skills = useResource("/skills"),
    models = useResource("/model-configs");
  const [form, setForm] = useState({
    skill_id: preset.skill_id || "",
    skill_version: preset.skill_version || "",
    purpose: preset.purpose || "investigation",
    title: preset.title || "",
    target: preset.target || "",
    incident_id: preset.incident_id || "",
    analysis_start: localDate(preset.analysis_start),
    analysis_end: localDate(preset.analysis_end),
    message: preset.message || "",
    environment: "lab",
    model_id: "",
  });
  const versions = useResource(
    form.skill_id
      ? `/skills/${encodeURIComponent(form.skill_id)}/versions`
      : null,
  );
  const [submissionId] = useState(requestId);
  const state = useAction();
  const set = (key: string, value: string) =>
    setForm((f) => ({ ...f, [key]: value }));
  useEffect(() => {
    if (!form.skill_id && list(skills.data).length)
      set("skill_id", list(skills.data)[0].id);
  }, [skills.data, form.skill_id]);
  async function submit(event: React.FormEvent) {
    event.preventDefault();
    await state.act("submit", async () => {
      if (!form.skill_id) throw new Error("请选择 Skill");
      if (Boolean(form.analysis_start) !== Boolean(form.analysis_end))
        throw new Error("分析窗口必须同时填写开始和结束时间");
      if (
        form.analysis_start &&
        new Date(form.analysis_start) >= new Date(form.analysis_end)
      )
        throw new Error("开始时间必须早于结束时间");
      const payload: Record<string, unknown> = {
        ...form,
        client_request_id: submissionId,
      };
      for (const [key, value] of Object.entries(payload))
        if (value === "") delete payload[key];
      if (form.analysis_start)
        payload.analysis_start = new Date(form.analysis_start).toISOString();
      if (form.analysis_end)
        payload.analysis_end = new Date(form.analysis_end).toISOString();
      if (form.skill_version === "draft") payload.purpose = "skill_test";
      const run = await post<Run>("/runs", payload);
      onCreated(run);
    });
  }
  return (
    <Modal
      title={preset.purpose === "skill_test" ? "Skill 试运行" : "新建分析"}
      onClose={onClose}
      wide
    >
      <form onSubmit={submit} className="stack">
        <Notice error={skills.error || models.error || state.error} />
        {skills.loading ? <Loading /> : null}
        <div className="form-grid">
          <Field label="分析方法" required>
            <select
              aria-label="分析方法"
              value={form.skill_id}
              onChange={(e) => {
                set("skill_id", e.target.value);
                set("skill_version", "");
              }}
              required
            >
              <option value="">请选择 Skill</option>
              {list(skills.data).map((s) => (
                <option key={s.id} value={s.id}>
                  {s.display_name || s.name}
                  {s.enabled === false ? " · 已停用" : ""}
                </option>
              ))}
            </select>
          </Field>
          <Field label="内容版本">
            <select
              aria-label="内容版本"
              value={form.skill_version}
              onChange={(e) => set("skill_version", e.target.value)}
            >
              <option value="">当前已发布版本</option>
              <option value="draft">当前草稿 · 独立试运行</option>
              {list(versions.data).map((v) => (
                <option key={v.id || v.version_id} value={v.id || v.version_id}>
                  {v.label || v.version || v.id || v.version_id}
                </option>
              ))}
            </select>
          </Field>
          <Field label="分析标题">
            <input
              aria-label="分析标题"
              value={form.title}
              onChange={(e) => set("title", e.target.value)}
              placeholder="简要描述要分析的问题"
            />
          </Field>
          <Field label="模型">
            <select
              aria-label="分析模型"
              value={form.model_id}
              onChange={(e) => set("model_id", e.target.value)}
            >
              <option value="">使用默认模型</option>
              {list(models.data).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} · {m.model}
                </option>
              ))}
            </select>
          </Field>
          <Field label="目标对象">
            <input
              aria-label="目标对象"
              value={form.target}
              onChange={(e) => set("target", e.target.value)}
              placeholder="资源、服务或业务标识"
            />
          </Field>
          <Field label="告警 / 事件 ID">
            <input
              aria-label="告警或事件ID"
              value={form.incident_id}
              onChange={(e) => set("incident_id", e.target.value)}
            />
          </Field>
          <Field label="分析开始时间">
            <input
              aria-label="分析开始时间"
              type="datetime-local"
              step={1}
              value={form.analysis_start}
              onChange={(e) => set("analysis_start", e.target.value)}
            />
          </Field>
          <Field
            label="分析结束时间"
            hint="固定时间窗以当前时区输入，提交时转换为 UTC。"
          >
            <input
              aria-label="分析结束时间"
              type="datetime-local"
              step={1}
              value={form.analysis_end}
              onChange={(e) => set("analysis_end", e.target.value)}
            />
          </Field>
          <Field label="环境">
            <input
              aria-label="环境"
              value={form.environment}
              onChange={(e) => set("environment", e.target.value)}
            />
          </Field>
        </div>
        <Field label="分析问题与补充信息">
          <textarea
            aria-label="分析问题与补充信息"
            rows={4}
            value={form.message}
            onChange={(e) => set("message", e.target.value)}
            placeholder="说明分析目的、已知现象和需要确认的内容。"
          />
        </Field>
        <p className="form-note">
          提交后固定
          Skill、模型和工具配置快照；分析与试调共享单执行槽，读取真实只读数据。
        </p>
        <div className="form-actions">
          <Button onClick={onClose} type="button">
            取消
          </Button>
          <Button
            variant="primary"
            busy={!!state.busy}
            disabled={!form.skill_id}
            type="submit"
          >
            <Play size={15} />
            提交分析
          </Button>
        </div>
      </form>
    </Modal>
  );
}
