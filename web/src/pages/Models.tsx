import QualityPanel from "../components/management/QualityPanel";
import { useEffect, useState } from "react";
import { Bot, Database, Plus, Save, Send, TestTube2 } from "lucide-react";
import {
  json,
  list,
  parseJson,
  post,
  put,
  requestId,
  useAction,
  useResource,
} from "../api";
import type { Entity, Run } from "../types";
import {
  Button,
  Empty,
  Field,
  JsonView,
  Loading,
  Modal,
  Notice,
  PageTitle,
  Status,
} from "../components/ui";
function CreateModel({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState(""),
    [model, setModel] = useState("");
  const state = useAction();
  return (
    <Modal title="新增模型配置" onClose={onClose}>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void state.act("create", async () => {
            const result = await post("/model-configs", {
              name,
              model,
              provider: "openai",
              base_url: "https://api.openai.com/v1",
              temperature: 0,
              max_tokens: 2048,
              enabled: false,
            });
            onCreated(result.id);
          });
        }}
      >
        <Notice error={state.error} />
        <Field label="配置名称" required>
          <input
            aria-label="模型配置名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field label="模型 ID" required>
          <input
            aria-label="新模型ID"
            value={model}
            onChange={(e) => setModel(e.target.value)}
            required
          />
        </Field>
        <div className="form-actions">
          <Button type="button" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" type="submit" busy={!!state.busy}>
            创建草稿
          </Button>
        </div>
      </form>
    </Modal>
  );
}
function ModelEditor({ id, onRun }: { id: string; onRun: (run: Run) => void }) {
  const resource = useResource(`/model-configs/${encodeURIComponent(id)}`),
    state = useAction();
  const [form, setForm] = useState<Entity | null>(null),
    [secret, setSecret] = useState(""),
    [dirty, setDirty] = useState(false), [testVersion, setTestVersion] = useState("draft");
  useEffect(() => {
    if (resource.data) {
      setForm(resource.data);
      setDirty(false);
    }
  }, [resource.data]);
  const change = (key: string, value: unknown) => {
    setForm((f) => ({ ...f, [key]: value }));
    setDirty(true);
  };
  async function command(action: string, body: Entity = {}) {
    await state.act(
      action,
      async () => {
        const result = await post(`/model-configs/${id}/${action}`, {
          expected_revision: form?.revision,
          ...body,
        });
        if (action === "test") onRun(result as Run);
        else await resource.reload();
      },
      action === "publish"
        ? "模型版本已发布"
        : action === "state"
          ? "状态已更新"
          : undefined,
    );
  }
  if (resource.loading) return <Loading />;
  if (!form) return <Notice error={resource.error} />;
  return (
    <div className="editor-layout">
      <div className="editor-top">
        <div>
          <h2>{form.name}</h2>
          <div className="inline-meta">
            <Status value={form.enabled ? "enabled" : "disabled"} />
            <Status value={form.status} />
            <span>修订 {form.revision}</span>
          </div>
        </div>
        <code>{form.current_version || "尚未发布"}</code>
      </div>
      <Notice error={resource.error || state.error} message={state.notice} />
      <div className="editor-main stack">
        <div className="form-grid">
          <Field label="配置名称">
            <input
              aria-label="配置名称"
              value={form.name || ""}
              onChange={(e) => change("name", e.target.value)}
            />
          </Field>
          <Field label="Provider" hint="后端按已支持的 Provider 进行协议适配。">
            <input
              aria-label="模型Provider"
              value={form.provider || ""}
              onChange={(e) => change("provider", e.target.value)}
              placeholder="openai"
            />
          </Field>
          <Field label="模型 ID">
            <input
              aria-label="模型ID"
              value={form.model || ""}
              onChange={(e) => change("model", e.target.value)}
            />
          </Field>
          <Field label="服务地址">
            <input
              aria-label="模型服务地址"
              type="url"
              value={form.base_url || ""}
              onChange={(e) => change("base_url", e.target.value)}
              placeholder="Provider 默认地址"
            />
          </Field>
          <Field label="Temperature" hint="仅在所选模型支持时生效。">
            <input
              aria-label="Temperature"
              type="number"
              step="0.1"
              min={0}
              max={2}
              value={form.temperature ?? ""}
              onChange={(e) =>
                change(
                  "temperature",
                  e.target.value === "" ? null : Number(e.target.value),
                )
              }
            />
          </Field>
          <Field label="最大输出 token">
            <input
              aria-label="最大输出token"
              type="number"
              min={1}
              value={form.max_tokens || 2048}
              onChange={(e) => change("max_tokens", Number(e.target.value))}
            />
          </Field>
          <div className="span-2">
            <Field
              label="新 API Key（只写）"
              hint={
                form.secret_id
                  ? `现有凭据引用：${form.secret_id}。留空保留，保存后清空输入。`
                  : "凭据在服务端保存，浏览器不持久存储。"
              }
            >
              <input
                aria-label="模型API Key"
                type="password"
                autoComplete="new-password"
                value={secret}
                onChange={(e) => {
                  setSecret(e.target.value);
                  setDirty(true);
                }}
                placeholder="留空不修改已有凭据"
              />
            </Field>
          </div>
        </div>
        <div className="scope-note">
          模型测试会创建独立持久任务，验证中文响应与工具调用协议；真实 MCP
          与业务分析另行保留验证结果。
        </div>
        <details><summary>费用单价（可选）</summary><div className="form-grid">{[["input_per_million", "每百万输入 token 单价"], ["output_per_million", "每百万输出 token 单价"], ["cached_input_per_million", "每百万缓存读取 token 单价"]].map(([key,label]) => <Field key={key} label={label}><input type="number" min="0" step="any" aria-label={label} value={form.pricing?.[key] ?? ""} onChange={(e) => (() => { const next = {...form.pricing}; if (e.target.value === "") delete next[key]; else next[key] = Number(e.target.value); change("pricing", Object.keys(next).length ? next : null); })()} /></Field>)}{[["currency", "币种"], ["as_of", "单价日期"], ["version", "单价版本"]].map(([key,label]) => <Field key={key} label={label}><input type={key === "as_of" ? "date" : "text"} aria-label={label} value={form.pricing?.[key] || ""} onChange={(e) => change("pricing", {...form.pricing, [key]: e.target.value})} /></Field>)}</div><p className="muted">未配置完整单价时费用显示未知；按本轮固定单价估算。</p><Button disabled={!form.pricing} onClick={() => change("pricing", null)}>清除单价</Button></details>
        <JsonView
          value={form.capabilities || form.test_result || form.validation || {}}
          title="已记录的能力与检查范围"
          open
        />
      </div>
      <QualityPanel key={String(id) + ":" + form.revision} kind="model" value={form} dirty={dirty} onPublished={async () => { await resource.reload(); }} />
      <footer className="editor-footer">
        <Button
          onClick={() => void command("state", { enabled: !form.enabled })}
          disabled={!!state.busy || dirty}
        >
          {form.enabled ? "停用" : "启用"}
        </Button>
        <div className="actions">
          <span className="muted tiny">{dirty ? "有未保存修改" : ""}</span>
          <Button
            busy={state.busy === "save"}
            onClick={() =>
              void state.act(
                "save",
                async () => {
                  const result = await put(`/model-configs/${id}`, {
                    ...form,
                    expected_revision: form.revision,
                    ...(secret ? { secret_value: secret } : {}),
                  });
                  setSecret("");
                  resource.setData(result);
                },
                "模型草稿已保存",
              )
            }
          >
            <Save size={14} />
            保存草稿
          </Button>
          <select aria-label="模型试运行版本" value={testVersion} onChange={(e) => setTestVersion(e.target.value)}><option value="draft">测试当前草稿</option>{form.current_version ? <option value={form.current_version}>测试当前正式版（对照）</option> : null}</select>
          <Button
            onClick={() =>
              void command("test", { client_request_id: requestId(), config_version: testVersion })
            }
            disabled={dirty}
            busy={state.busy === "test"}
          >
            <TestTube2 size={15} />
            样本测试
          </Button>
          <Button
            variant="primary"
            onClick={() => document.querySelector<HTMLElement>('[aria-label="发布质量检查"]')?.scrollIntoView({ behavior: "smooth", block: "center" })}
            disabled={dirty}
            busy={state.busy === "publish"}
          >
            <Send size={14} />
            发布检查
          </Button>
        </div>
      </footer>
    </div>
  );
}
function Settings({ models }: { models: Entity[] }) {
  const resource = useResource("/settings"),
    state = useAction();
  const [settings, setSettings] = useState<Entity | null>(null),
    [budget, setBudget] = useState("{}"),
    [source, setSource] = useState("{}"),
    [mapping, setMapping] = useState("{}"),
    [catalog, setCatalog] = useState("{}"),
    [sourceSecret, setSourceSecret] = useState(""),
    [sourcePassword, setSourcePassword] = useState("");
  useEffect(() => {
    if (resource.data) {
      setSettings(resource.data);
      setBudget(
        json(resource.data.default_budget || resource.data.budget || {}),
      );
      setSource(json(resource.data.ongrid || {}));
      setMapping(json(resource.data.identity_mapping || {}));
      setCatalog(json(resource.data.query_catalog || {}));
    }
  }, [resource.data]);
  if (resource.loading) return <Loading />;
  if (!settings) return <Notice error={resource.error} />;
  return (
    <section className="surface settings-surface">
      <div className="section-heading">
        <div>
          <h2>数据源与运行设置</h2>
          <p>运行默认值、Ongrid 源接口与身份映射。</p>
        </div>
        <Database size={23} className="muted" />
      </div>
      <Notice error={resource.error || state.error} message={state.notice} />
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void state.act(
            "save",
            async () => {
              const config = parseJson(source, "Ongrid数据源", "object");
              if (
                "secret_value" in config ||
                "password_secret_value" in config ||
                "password" in config
              )
                throw new Error("Ongrid凭据请在只写框输入");
              const result = await put("/settings", {
                ...settings,
                budget: parseJson(budget, "默认预算", "object"),
                ongrid: {
                  ...config,
                  ...(sourceSecret ? { secret_value: sourceSecret } : {}),
                  ...(sourcePassword
                    ? { password_secret_value: sourcePassword }
                    : {}),
                },
                identity_mapping: parseJson(mapping, "身份映射", "object"),
                query_catalog: parseJson(catalog, "查询目录", "object"),
                expected_revision: settings.revision,
              });
              setSourceSecret("");
              setSourcePassword("");
              resource.setData(result);
            },
            "默认设置已保存",
          );
        }}
      >
        <div className="form-grid">
          <Field label="默认模型">
            <select
              aria-label="默认模型"
              value={settings.default_model_id || ""}
              onChange={(e) =>
                setSettings({ ...settings, default_model_id: e.target.value })
              }
            >
              <option value="">尚未指定默认模型</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name} · {m.model}
                </option>
              ))}
            </select>
          </Field>
          <Field
            label="Ongrid 新凭据（只写）"
            hint="留空保留现有引用；使用 Bearer / 指定认证头时在数据源 JSON 中配置类型。"
          >
            <input
              aria-label="Ongrid新凭据"
              type="password"
              autoComplete="new-password"
              value={sourceSecret}
              onChange={(e) => setSourceSecret(e.target.value)}
            />
          </Field>
          <Field
            label="Ongrid 登录邮箱"
            hint="需要登录后取告警时填写，保存在数据源配置中。"
          >
            <input
              aria-label="Ongrid登录邮箱"
              type="email"
              value={(() => {
                try {
                  return JSON.parse(source).email || "";
                } catch {
                  return "";
                }
              })()}
              onChange={(e) => {
                try {
                  setSource(
                    json({ ...JSON.parse(source), email: e.target.value }),
                  );
                } catch {
                  state.setError("请先修正Ongrid数据源JSON");
                }
              }}
            />
          </Field>
          <Field
            label="Ongrid 新登录密码（只写）"
            hint="保存后清空；现有凭据仅显示引用。"
          >
            <input
              aria-label="Ongrid新登录密码"
              type="password"
              autoComplete="new-password"
              value={sourcePassword}
              onChange={(e) => setSourcePassword(e.target.value)}
            />
          </Field>
          <Field label="默认运行预算（JSON）">
            <textarea
              aria-label="默认运行预算JSON"
              className="code-input medium"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              spellCheck={false}
            />
          </Field>
          <Field
            label="Ongrid 数据源（JSON）"
            hint="支持 url、ca_file、auth_type、secret_id、headers 等后端已实现字段。"
          >
            <textarea
              aria-label="Ongrid数据源JSON"
              className="code-input medium"
              value={source}
              onChange={(e) => setSource(e.target.value)}
              spellCheck={false}
            />
          </Field>
          <div className="span-2">
            <Field label="环境与身份映射（JSON）" hint="版本化 records 须登记完整对象键、source_id、原生 ID、映射来源与有效期；未配置或旧格式会要求补充，不会猜测目标。">
              <textarea
                aria-label="环境与身份映射JSON"
                className="code-input medium"
                value={mapping}
                onChange={(e) => setMapping(e.target.value)}
                spellCheck={false}
              />
            </Field>
          </div>
          <div className="span-2"><Field label="查询目录（JSON）" hint="版本化 entries 须明确绑定 source_id、tool_name、工具定义摘要、身份和时间参数；source_offsets 登记源时钟偏差。未登记工具不能自动查询对象。"><textarea aria-label="查询目录JSON" className="code-input medium" value={catalog} onChange={(event) => setCatalog(event.target.value)} spellCheck={false} /></Field></div>
        </div>
        <div className="form-actions">
          <Button variant="primary" type="submit" busy={!!state.busy}>
            <Save size={15} />
            保存运行设置
          </Button>
        </div>
      </form>
    </section>
  );
}
function PolicyEditor({ kind }: { kind: "source" | "egress" }) {
  const resource = useResource(`/access-policies/${kind}`), action = useAction();
  const [draft, setDraft] = useState("{}");
  const title = kind === "source" ? "数据源读取授权" : "模型数据出站规则";
  useEffect(() => {
    if (!resource.data) return;
    const { configured, revision, id, created_at, updated_at, ...policy } = resource.data;
    void revision; void id; void created_at; void updated_at;
    setDraft(json(configured === false ? kind === "source" ? { schema: "source-access/v1", version: "draft-1", enabled: false, grants: [], denies: [] } : { schema: "model-egress/v1", version: "draft-1", enabled: false, model_bindings: [], types: {}, denied_types: [] } : resource.data.policy || policy));
  }, [resource.data, kind]);
  const enabled = resource.data?.policy?.enabled ?? resource.data?.enabled;
  return <section className="surface settings-surface policy-editor"><div className="section-heading"><div><h2>{title}</h2><p>{kind === "source" ? "按主体、来源、目标、环境、工具和用途明确授权；管理员角色本身不授予源数据读取权。" : "按材料类型和字段约束模型可见内容；标识替换不改变真实源查询参数。"}</p></div><Status value={resource.data?.configured === false ? "unknown" : enabled ? "enabled" : "disabled"}>{resource.data?.configured === false ? "尚未配置" : enabled ? "已启用" : "未启用"}</Status></div><Notice error={resource.error || action.error} message={action.notice} />{resource.loading ? <Loading /> : resource.data ? <form className="stack" onSubmit={(event) => { event.preventDefault(); void action.act("save", async () => { const policy = parseJson(draft, title, "object"); const result = await put(`/access-policies/${kind}`, { policy, expected_revision: resource.data?.revision ?? 0 }); resource.setData(result); }, `${title}已保存`); }}><Field label={`${title}（JSON）`} hint="保存使用独立修订检查。未配置、禁用或匹配不到规则时拒绝操作；下方初始草稿保持禁用。请勿在规则中填写密钥正文。"><textarea className="code-input medium" aria-label={`${title}JSON`} value={draft} onChange={(event) => setDraft(event.target.value)} spellCheck={false} /></Field><div className="form-actions"><Button type="submit" variant="primary" busy={!!action.busy}><Save size={15} />保存{title}</Button></div></form> : null}</section>;
}
export default function Models({ onRun }: { onRun: (run: Run) => void }) {
  const resource = useResource("/model-configs", 8000),
    [selected, setSelected] = useState<string | null>(null),
    [create, setCreate] = useState(false);
  useEffect(() => {
    if (!selected && list(resource.data).length)
      setSelected(list(resource.data)[0].id);
  }, [selected, resource.data]);
  return (
    <div className="page-pad management">
      <PageTitle
        title="模型设置"
        description="管理模型凭据、能力测试与任务默认配置。"
        actions={
          <Button variant="primary" onClick={() => setCreate(true)}>
            <Plus size={15} />
            新增模型
          </Button>
        }
      />
      <Notice error={resource.error} />
      <div className="management-grid model-grid">
        <aside className="surface entity-list">
          <div className="directory-heading">模型配置</div>
          {resource.loading ? (
            <Loading />
          ) : list(resource.data).length ? (
            list(resource.data).map((m) => (
              <button
                key={m.id}
                className={`entity-row ${selected === m.id ? "selected" : ""}`}
                onClick={() => setSelected(m.id)}
              >
                <div className="row-top">
                  <strong>{m.name}</strong>
                  <Status value={m.enabled ? "enabled" : "disabled"} />
                </div>
                <small>
                  {m.provider} / {m.model}
                </small>
                <span className="row-subtitle">
                  {m.current_version || "尚未发布"}
                </span>
              </button>
            ))
          ) : (
            <Empty title="暂无模型配置" detail="新增模型并完成小型样本测试。" />
          )}
        </aside>
        <section className="surface entity-detail">
          {selected ? (
            <ModelEditor key={selected} id={selected} onRun={onRun} />
          ) : (
            <Empty
              title="选择一个模型配置"
              detail="模型测试、版本发布与默认选择分别管理。"
              icon={<Bot size={32} />}
            />
          )}
        </section>
      </div>
      <Settings models={list(resource.data)} />
      <PolicyEditor kind="source" />
      <PolicyEditor kind="egress" />
      {create ? (
        <CreateModel
          onClose={() => setCreate(false)}
          onCreated={(id) => {
            setCreate(false);
            setSelected(id);
            void resource.reload();
          }}
        />
      ) : null}
    </div>
  );
}
