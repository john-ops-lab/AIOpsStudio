import { useEffect, useRef, useState } from "react";
import {
  Check,
  ChevronRight,
  Cable,
  Play,
  Plus,
  Radio,
  Save,
  Search,
  Send,
} from "lucide-react";
import {
  dateTime,
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
  Metadata,
  Modal,
  Notice,
  PageTitle,
  Status,
} from "../components/ui";
function CreateMcp({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState(""),
    [url, setUrl] = useState("");
  const state = useAction();
  return (
    <Modal title="新增 MCP 连接" onClose={onClose}>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void state.act("create", async () => {
            const result = await post("/mcp-servers", {
              name,
              url,
              auth_type: "none",
              timeout_seconds: 30,
              allowed_tools: [],
              enabled: false,
            });
            onCreated(result.id);
          });
        }}
      >
        <Notice error={state.error} />
        <Field label="连接名称" required>
          <input
            aria-label="MCP连接名称"
            value={name}
            onChange={(e) => setName(e.target.value)}
            required
          />
        </Field>
        <Field label="Streamable HTTP 地址" required>
          <input
            aria-label="MCP服务地址"
            type="url"
            placeholder="https://host/mcp"
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            required
          />
        </Field>
        <p className="muted">
          连接创建后，先发现工具，再检查只读行为并配置白名单。
        </p>
        <div className="form-actions">
          <Button type="button" onClick={onClose}>
            取消
          </Button>
          <Button variant="primary" type="submit" busy={!!state.busy}>
            创建连接草稿
          </Button>
        </div>
      </form>
    </Modal>
  );
}
function ToolInspector({
  server,
  tool,
  onRun,
  onChanged,
}: {
  server: Entity;
  tool: Entity;
  onRun: (run: Run) => void;
  onChanged: () => void;
}) {
  const [tab, setTab] = useState("params"),
    [args, setArgs] = useState("{}");
  const state = useAction();
  const pendingRequest = useRef<{ args: string; id: string } | null>(null);
  const allowed = (server.allowed_tools || []).includes(tool.name);
  return (
    <div className="tool-inspector">
      <div className="section-heading">
        <div>
          <h3>{tool.name}</h3>
          <p className="muted">{tool.description || "源端未提供工具说明"}</p>
        </div>
        <Status value={allowed ? "enabled" : "disabled"}>
          {allowed ? "已开放" : "未开放"}
        </Status>
      </div>
      <Metadata
        items={[
          ["服务", server.name],
          ["工具别名", tool.alias],
          ["只读确认", tool.readonly ? "已确认" : "未确认"],
          ["配置版本", server.current_version || "尚未发布"],
        ]}
      />
      <div className="tabs" role="tablist" aria-label="工具详情">
        {[
          ["params", "调用参数"],
          ["schema", "输入 Schema"],
          ["output", "输出 Schema"],
        ].map(([k, v]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            className={tab === k ? "active" : ""}
            onClick={() => setTab(k)}
          >
            {v}
          </button>
        ))}
      </div>
      <Notice error={state.error} message={state.notice} />
      {tab === "params" ? (
        <div className="stack">
          <Field
            label="请求参数（JSON）"
            hint="按工具输入 Schema 提供参数；涉及时间窗时填写绝对时间。"
          >
            <textarea
              aria-label="MCP工具请求参数JSON"
              className="code-input large"
              value={args}
              onChange={(e) => setArgs(e.target.value)}
              spellCheck={false}
            />
          </Field>
          {!allowed ? (
            <div className="scope-note">
              此工具未在已保存白名单中开放，请先检查只读行为并发布配置。
            </div>
          ) : null}
          <div className="form-actions">
            <Button
              variant="primary"
              busy={!!state.busy}
              disabled={
                !allowed ||
                !tool.readonly ||
                server.blocked ||
                !server.enabled ||
                !server.current_version
              }
              onClick={() =>
                void state.act("test", async () => {
                  if (
                    !pendingRequest.current ||
                    pendingRequest.current.args !== args
                  ) {
                    pendingRequest.current = { args, id: requestId() };
                  }
                  const run = await post<Run>("/tool-tests", {
                    server_id: server.id,
                    tool_name: tool.name,
                    arguments: parseJson(args, "请求参数", "object"),
                    client_request_id: pendingRequest.current!.id,
                  });
                  pendingRequest.current = null;
                  onChanged();
                  onRun(run);
                })
              }
            >
              <Play size={15} />
              提交工具试调
            </Button>
          </div>
        </div>
      ) : (
        <JsonView
          value={
            tab === "schema"
              ? tool.inputSchema || tool.input_schema || {}
              : tool.outputSchema ||
                tool.output_schema || { 说明: "源端未提供输出Schema" }
          }
        />
      )}
    </div>
  );
}
function McpEditor({ id, onRun }: { id: string; onRun: (run: Run) => void }) {
  const resource = useResource(`/mcp-servers/${encodeURIComponent(id)}`),
    tools = useResource(`/mcp-servers/${encodeURIComponent(id)}/tools`),
    state = useAction();
  const [form, setForm] = useState<Entity | null>(null),
    [secret, setSecret] = useState(""),
    [advanced, setAdvanced] = useState("{}"),
    [tab, setTab] = useState("config"),
    [selectedTool, setSelectedTool] = useState(""),
    [diagnostics, setDiagnostics] = useState<unknown>(null),
    [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (resource.data) {
      const d = resource.data;
      setForm(d);
      setAdvanced(json({ ca_file: d.ca_file || "", headers: d.headers || {} }));
      setDirty(false);
    }
  }, [resource.data]);
  const change = (key: string, value: unknown) => {
    setForm((f) => ({ ...f, [key]: value }));
    setDirty(true);
  };
  async function save() {
    await state.act(
      "save",
      async () => {
        const config = parseJson(advanced, "高级连接设置", "object");
        if ("secret_value" in config)
          throw new Error("凭据请在专用密码框中填写");
        const result = await put(`/mcp-servers/${id}`, {
          ...form,
          ...config,
          expected_revision: form?.revision,
          ...(secret ? { secret_value: secret } : {}),
        });
        setSecret("");
        resource.setData(result);
        await tools.reload();
      },
      "连接草稿已保存",
    );
  }
  async function command(action: string, body: Entity = {}) {
    await state.act(
      action,
      async () => {
        const result = await post(`/mcp-servers/${id}/${action}`, {
          expected_revision: form?.revision,
          ...body,
        });
        if (["probe", "discover"].includes(action)) {
          setDiagnostics(result);
          setTab("diagnostics");
        }
        await resource.reload();
        await tools.reload();
      },
      action === "publish"
        ? "连接版本已发布"
        : action === "state"
          ? "状态已更新"
          : "检查已完成，查看各阶段结果",
    );
  }
  if (resource.loading) return <Loading />;
  if (!form) return <Notice error={resource.error} />;
  const catalog = list(tools.data).length ? list(tools.data) : form.tools || [];
  const currentTool = catalog.find((t: Entity) => t.name === selectedTool);
  return (
    <div className="editor-layout">
      <div className="editor-top">
        <div>
          <h2>{form.name}</h2>
          <div className="inline-meta">
            <Status value={form.enabled ? "enabled" : "disabled"} />
            <Status value={form.status} />
            <span>修订 {form.revision}</span>
            {form.blocked ? <Status value="failed">已立即阻止</Status> : null}
          </div>
        </div>
        <span className="tiny muted">Streamable HTTP</span>
      </div>
      <Notice
        error={resource.error || tools.error || state.error}
        message={state.notice}
      />
      <div className="tabs" role="tablist" aria-label="MCP连接详情">
        {[
          ["config", "连接配置"],
          ["tools", `工具目录 (${catalog.length})`],
          ["diagnostics", "检查结果"],
        ].map(([k, v]) => (
          <button
            key={k}
            role="tab"
            aria-selected={tab === k}
            className={tab === k ? "active" : ""}
            onClick={() => setTab(k)}
          >
            {v}
          </button>
        ))}
      </div>
      <div className="editor-main">
        {tab === "config" ? (
          <div className="stack">
            <div className="form-grid">
              <Field label="连接名称">
                <input
                  aria-label="连接名称"
                  value={form.name || ""}
                  onChange={(e) => change("name", e.target.value)}
                />
              </Field>
              <Field label="调用超时（秒）">
                <input
                  aria-label="调用超时秒数"
                  type="number"
                  min={1}
                  max={120}
                  value={form.timeout_seconds || 30}
                  onChange={(e) =>
                    change("timeout_seconds", Number(e.target.value))
                  }
                />
              </Field>
              <div className="span-2">
                <Field label="服务地址">
                  <input
                    aria-label="服务地址"
                    type="url"
                    value={form.url || ""}
                    onChange={(e) => change("url", e.target.value)}
                  />
                </Field>
              </div>
              <Field label="认证方式">
                <select
                  aria-label="认证方式"
                  value={form.auth_type || "none"}
                  onChange={(e) => change("auth_type", e.target.value)}
                >
                  <option value="none">无认证</option>
                  <option value="bearer">Bearer Token</option>
                  <option value="header">指定认证头</option>
                </select>
              </Field>
              <Field
                label="新凭据（只写）"
                hint={
                  form.secret_id
                    ? `已保存凭据引用：${form.secret_id}`
                    : "保存后不会回显。留空则不修改已有凭据。"
                }
              >
                <input
                  aria-label="MCP新凭据"
                  type="password"
                  autoComplete="new-password"
                  value={secret}
                  onChange={(e) => {
                    setSecret(e.target.value);
                    setDirty(true);
                  }}
                  placeholder="留空保留已有凭据"
                />
              </Field>
              {form.auth_type === "header" ? (
                <Field label="认证头名称">
                  <input
                    aria-label="认证头名称"
                    value={form.auth_header || "Authorization"}
                    onChange={(e) => change("auth_header", e.target.value)}
                  />
                </Field>
              ) : null}
            </div>
            <Field
              label="高级连接设置（JSON）"
              hint="ca_file 为 服务端 CA 文件路径；headers 可配置 SSH 转发所需 Host。密钥通过上方专用字段保存。"
            >
              <textarea
                aria-label="MCP高级连接设置JSON"
                className="code-input medium"
                value={advanced}
                onChange={(e) => {
                  setAdvanced(e.target.value);
                  setDirty(true);
                }}
                spellCheck={false}
              />
            </Field>
            <Metadata
              items={[
                ["当前发布版本", form.current_version || "尚未发布"],
                [
                  "上次检查",
                  dateTime(
                    form.last_probe?.checked_at ||
                      form.last_checked_at ||
                      form.checked_at,
                  ),
                ],
                ["远端信息", form.last_probe?.server_info || form.server_info],
                [
                  "协议版本",
                  form.last_probe?.protocol_version || form.protocol_version,
                ],
              ]}
            />
          </div>
        ) : null}
        {tab === "tools" ? (
          <div className="tools-layout">
            <div className="tool-directory">
              <div className="directory-heading">开放工具白名单</div>
              {catalog.length ? (
                catalog.map((t: Entity) => {
                  const editable =
                    (form.tools || catalog).find(
                      (x: Entity) => x.name === t.name,
                    ) || t;
                  return (
                    <div
                      key={t.name}
                      className={`tool-option ${selectedTool === t.name ? "selected" : ""}`}
                    >
                      <button
                        className="tool-select"
                        onClick={() => setSelectedTool(t.name)}
                      >
                        <ChevronRight size={13} />
                        <span>{t.name}</span>
                      </button>
                      <label className="tool-alias">
                        <span>模型工具别名</span>
                        <input
                          aria-label={`${t.name}模型工具别名`}
                          value={editable.alias || ""}
                          onChange={(e) =>
                            change(
                              "tools",
                              (form.tools || catalog).map((x: Entity) =>
                                x.name === t.name
                                  ? { ...x, alias: e.target.value }
                                  : x,
                              ),
                            )
                          }
                        />
                      </label>
                      <label className="check-row">
                        <input
                          type="checkbox"
                          aria-label={`确认${t.name}为只读`}
                          checked={!!editable.readonly}
                          onChange={(e) => {
                            const next = (form.tools || catalog).map(
                              (x: Entity) =>
                                x.name === t.name
                                  ? { ...x, readonly: e.target.checked }
                                  : x,
                            );
                            change("tools", next);
                            if (!e.target.checked)
                              change(
                                "allowed_tools",
                                (form.allowed_tools || []).filter(
                                  (n: string) => n !== t.name,
                                ),
                              );
                          }}
                        />
                        已检查只读行为
                      </label>
                      <label className="check-row">
                        <input
                          type="checkbox"
                          aria-label={`开放${t.name}`}
                          checked={(form.allowed_tools || []).includes(t.name)}
                          disabled={!editable.readonly}
                          onChange={(e) =>
                            change(
                              "allowed_tools",
                              e.target.checked
                                ? [...(form.allowed_tools || []), t.name]
                                : (form.allowed_tools || []).filter(
                                    (n: string) => n !== t.name,
                                  ),
                            )
                          }
                        />
                        允许 Agent / 人工试调
                      </label>
                    </div>
                  );
                })
              ) : (
                <Empty
                  title="尚未发现工具"
                  detail="保存连接后，点击发现工具。"
                />
              )}
            </div>
            <div className="tool-detail">
              {currentTool ? (
                <ToolInspector
                  key={currentTool.name}
                  server={resource.data || form}
                  tool={
                    (form.tools || catalog).find(
                      (t: Entity) => t.name === currentTool.name,
                    ) || currentTool
                  }
                  onRun={onRun}
                  onChanged={() => void resource.reload()}
                />
              ) : (
                <Empty
                  title="选择一个工具"
                  detail="查看实际 Schema、检查只读行为并提交参数试调。"
                  icon={<Cable size={29} />}
                />
              )}
            </div>
          </div>
        ) : null}
        {tab === "diagnostics" ? (
          diagnostics ? (
            <JsonView value={diagnostics} />
          ) : (
            <>
              <Empty
                title="尚无本次检查结果"
                detail="连接探测、工具发现、业务试调分别记录，不自动等同。"
              />
              <JsonView
                value={form.probe_result || form.discovery || {}}
                title="服务端最近检查"
              />
            </>
          )
        ) : null}
      </div>
      <footer className="editor-footer">
        <div className="actions">
          <Button
            onClick={() => void command("state", { enabled: !form.enabled })}
            disabled={!!state.busy || dirty}
          >
            {form.enabled ? "停用" : "启用"}
          </Button>
          <Button
            variant={form.blocked ? "secondary" : "danger"}
            onClick={() =>
              void command("state", {
                enabled: form.enabled,
                blocked: !form.blocked,
              })
            }
            disabled={!!state.busy || dirty}
          >
            {form.blocked ? "解除阻止" : "立即阻止后续调用"}
          </Button>
        </div>
        <div className="actions">
          <span className="muted tiny">{dirty ? "有未保存修改" : ""}</span>
          <Button onClick={() => void save()} busy={state.busy === "save"}>
            <Save size={14} />
            保存草稿
          </Button>
          <Button
            onClick={() => void command("probe")}
            busy={state.busy === "probe"}
            disabled={dirty}
          >
            <Radio size={14} />
            探测
          </Button>
          <Button
            onClick={() => void command("discover")}
            busy={state.busy === "discover"}
            disabled={dirty}
          >
            <Search size={14} />
            发现工具
          </Button>
          <Button
            variant="primary"
            onClick={() => void command("publish")}
            busy={state.busy === "publish"}
            disabled={dirty}
          >
            <Send size={14} />
            发布配置
          </Button>
        </div>
      </footer>
    </div>
  );
}
export default function Mcp({
  onRun,
  initialId,
}: {
  onRun: (run: Run) => void;
  initialId?: string | null;
}) {
  const resource = useResource("/mcp-servers", 8000),
    [selected, setSelected] = useState<string | null>(initialId || null),
    [create, setCreate] = useState(false);
  useEffect(() => {
    if (initialId) setSelected(initialId);
    else if (!selected && list(resource.data).length)
      setSelected(list(resource.data)[0].id);
  }, [initialId, selected, resource.data]);
  return (
    <div className="page-pad management">
      <PageTitle
        title="MCP 管理"
        description="连接已有服务，管理工具目录、只读确认与调用白名单。"
        actions={
          <Button variant="primary" onClick={() => setCreate(true)}>
            <Plus size={16} />
            新增 MCP
          </Button>
        }
      />
      <Notice error={resource.error} />
      <div className="management-grid">
        <aside className="surface entity-list">
          <div className="directory-heading">服务连接</div>
          {resource.loading ? (
            <Loading />
          ) : list(resource.data).length ? (
            list(resource.data).map((s) => (
              <button
                key={s.id}
                className={`entity-row ${selected === s.id ? "selected" : ""}`}
                onClick={() => setSelected(s.id)}
              >
                <div className="row-top">
                  <strong>
                    <Cable size={15} />
                    {s.name}
                  </strong>
                  <Status value={s.enabled ? "enabled" : "disabled"} />
                </div>
                <small>{s.url}</small>
                <div className="inline-meta">
                  <Status value={s.status} />
                  <span>{s.allowed_tools?.length || 0} 个开放工具</span>
                </div>
              </button>
            ))
          ) : (
            <Empty
              title="暂无 MCP 连接"
              detail="新增已有 Streamable HTTP 服务的连接。"
            />
          )}
          <div className="integration-note">
            <Check size={14} />
            <span>GLPI 工单：未接入</span>
          </div>
        </aside>
        <section className="surface entity-detail">
          {selected ? (
            <McpEditor key={selected} id={selected} onRun={onRun} />
          ) : (
            <Empty
              title="选择一个 MCP 服务"
              detail="查看连接配置，发现并调试真实工具。"
              icon={<Cable size={32} />}
            />
          )}
        </section>
      </div>
      {create ? (
        <CreateMcp
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
