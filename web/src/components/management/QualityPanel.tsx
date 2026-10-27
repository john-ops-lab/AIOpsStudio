import { useState } from "react";
import { list, post, useAction, useResource } from "../../api";
import type { Entity } from "../../types";
import { Button, Field, JsonView, Loading, Notice } from "../ui";
const paths = { skill: "/skills", mcp: "/mcp-servers", model: "/model-configs" };
export default function QualityPanel({ kind, value, dirty, onPublished }: { kind: keyof typeof paths; value: Entity; dirty: boolean; onPublished: () => void | Promise<void> }) {
  const base = `${paths[kind]}/${encodeURIComponent(value.id)}`;
  const quality = useResource(`${base}/quality`), runs = useResource("/runs");
  const [candidate, setCandidate] = useState<string[]>([]), [baseline, setBaseline] = useState<string[]>([]), action = useAction();
  const purpose = kind === "model" ? "model_test" : kind === "skill" ? "skill_test" : "tool_test";
  const choices = list(runs.data).filter((r) => r.purpose === purpose && ["succeeded", "partial", "failed"].includes(r.status));
  const pipeline = quality.data?.pipeline;
  const evaluation = list(quality.data?.evaluations).find((e) => e.eligible === true && e.current === true);
  async function publish(channel: string) {
    await action.act("publish", async () => {
      await post(`${base}/publish`, { expected_revision: value.revision, channel, ...(channel === "formal" ? { evaluation_id: evaluation?.id } : {}) });
      await onPublished(); await quality.reload();
    }, channel === "formal" ? "正式版本已发布" : "实验版本已保存；默认正式版本保持原选择");
  }
  return <section className="quality-panel" aria-label="发布质量检查"><h3>发布质量检查</h3><p className="muted">先对已保存的草稿执行试运行，再选择本次候选及当前正式版的对照记录。服务器会核对实际结果、输入和版本；评估通过后才能正式发布。</p><Notice error={quality.error || runs.error || action.error} message={action.notice} />{quality.loading ? <Loading /> : <><div className="scope-note"><strong>固定案例比较：{pipeline?.eligible ? "当前实现已通过" : "待验证"}</strong><p>正式发布同时需要固定案例比较和本配置试运行评估。只使用隔离合成案例，不调用真实模型。</p><Button busy={action.busy === "pipeline"} onClick={() => void action.act("pipeline", async () => { const result = await post("/quality/pipeline"); await quality.reload(); if (!result.eligible) throw new Error(`固定案例比较未通过：${(result.reasons || []).join("、")}`); }, "固定案例比较已完成")}>运行固定案例比较</Button></div><div className="quality-selection"><Field label="候选草稿试运行"><select multiple aria-label="候选草稿试运行" value={candidate} onChange={(e) => setCandidate(Array.from(e.target.selectedOptions, (o) => o.value))}>{choices.map((r) => <option key={r.id} value={r.id}>{r.title || r.id} · {r.status}</option>)}</select></Field><Field label="当前正式版对照试运行" hint={value.current_version ? "已有正式版，需提供相同输入和源材料的对照。" : "首次发布按固定协议基准检查。"}><select multiple aria-label="当前正式版对照试运行" value={baseline} onChange={(e) => setBaseline(Array.from(e.target.selectedOptions, (o) => o.value))}>{choices.map((r) => <option key={r.id} value={r.id}>{r.title || r.id} · {r.status}</option>)}</select></Field></div><div className="actions"><Button disabled={dirty || !candidate.length} busy={action.busy === "evaluate"} onClick={() => void action.act("evaluate", async () => { await post(`${base}/quality`, { expected_revision: value.revision, candidate_run_ids: candidate, baseline_run_ids: baseline }); await quality.reload(); }, "质量检查结果已保存")}>评估所选试运行</Button><Button onClick={() => { void runs.reload(); void quality.reload(); }}>刷新试运行与评估</Button><Button variant="primary" disabled={dirty || !evaluation || !pipeline?.eligible} busy={action.busy === "publish"} onClick={() => void publish("formal")}>正式发布</Button><Button disabled={dirty} busy={action.busy === "publish"} onClick={() => void publish("experimental")}>保存实验版本</Button></div>{dirty ? <p className="scope-note">先保存草稿；修改内容或依赖后需重新试运行和评估。</p> : null}{!choices.length ? <p className="scope-note">尚无可选试运行，请先运行本项的草稿测试。</p> : null}<JsonView title="检查策略与评估结果" value={{ policy: quality.data?.policy, evaluations: quality.data?.evaluations }} /></>}</section>;
}
