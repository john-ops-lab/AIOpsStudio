import { useEffect, useState } from "react";
import {
  BookOpen,
  CheckCheck,
  FileCode2,
  FilePlus2,
  Play,
  Plus,
  Save,
  Send,
  ShieldAlert,
} from "lucide-react";
import {
  dateTime,
  json,
  list,
  parseJson,
  post,
  put,
  useAction,
  useResource,
} from "../api";
import type { Entity, RunPreset } from "../types";
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
  Updated,
} from "../components/ui";
function CreateSkill({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState(""),
    [displayName, setDisplayName] = useState(""),
    [description, setDescription] = useState("");
  const state = useAction();
  return (
    <Modal title="新建 Skill 草稿" onClose={onClose}>
      <form
        className="stack"
        onSubmit={(e) => {
          e.preventDefault();
          void state.act("create", async () => {
            const content = `---\nname: ${JSON.stringify(name)}\ndescription: ${JSON.stringify(description)}\n---\n\n# ${displayName || name}\n\n## 分析方法\n\n`;
            const result = await post("/skills", {
              name,
              display_name: displayName || name,
              description,
              content,
              references: {},
              input_schema: { type: "object", properties: {} },
              dependencies: [],
            });
            onCreated(result.id);
          });
        }}
      >
        <Notice error={state.error} />
        <Field
          label="稳定名称"
          required
          hint="使用小写英文、数字和连字符，便于版本引用。"
        >
          <input
            aria-label="Skill稳定名称"
            pattern="[a-z0-9][a-z0-9_-]*"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="例如 alert-impact-analysis"
            required
          />
        </Field>
        <Field label="显示名称">
          <input
            aria-label="Skill显示名称"
            value={displayName}
            onChange={(e) => setDisplayName(e.target.value)}
          />
        </Field>
        <Field label="用途说明" required>
          <textarea
            aria-label="Skill用途说明"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            rows={3}
            required
          />
        </Field>
        <div className="form-actions">
          <Button type="button" onClick={onClose}>
            取消
          </Button>
          <Button type="submit" variant="primary" busy={!!state.busy}>
            创建草稿
          </Button>
        </div>
      </form>
    </Modal>
  );
}
function SkillEditor({
  id,
  onRun,
}: {
  id: string;
  onRun: (preset: RunPreset) => void;
}) {
  const resource = useResource(`/skills/${encodeURIComponent(id)}`),
    versions = useResource(`/skills/${encodeURIComponent(id)}/versions`),
    state = useAction();
  const [form, setForm] = useState<Entity | null>(null),
    [tab, setTab] = useState("content"),
    [file, setFile] = useState("SKILL.md"),
    [refs, setRefs] = useState("{}"),
    [schema, setSchema] = useState("{}"),
    [deps, setDeps] = useState("[]"),
    [diagnostics, setDiagnostics] = useState<unknown>(null),
    [version, setVersion] = useState<Entity | null>(null),
    [referenceName, setReferenceName] = useState(""),
    [addingReference, setAddingReference] = useState(false),
    [dirty, setDirty] = useState(false);
  useEffect(() => {
    if (resource.data) {
      const data = resource.data;
      setForm(data);
      setRefs(json(data.references || {}));
      setSchema(json(data.input_schema || {}));
      setDeps(json(data.dependencies || []));
      setDirty(false);
    }
  }, [resource.data]);
  const change = (key: string, value: unknown) => {
    setForm((f) => ({ ...f, [key]: value }));
    setDirty(true);
  };
  const refMap = form?.references || {};
  async function save() {
    return state.act(
      "save",
      async () => {
        const payload = {
          ...form,
          references: parseJson(refs, "参考文件", "object"),
          input_schema: parseJson(schema, "输入 schema", "object"),
          dependencies: parseJson(deps, "依赖绑定", "array"),
          expected_revision: form?.revision,
        };
        const result = await put(`/skills/${id}`, payload);
        resource.setData(result);
        return result;
      },
      "草稿已保存",
    );
  }
  async function command(action: string, body: Entity = {}) {
    await state.act(
      action,
      async () => {
        const result = await post(`/skills/${id}/${action}`, {
          expected_revision: form?.revision,
          ...body,
        });
        if (action === "validate") setDiagnostics(result);
        await resource.reload();
        await versions.reload();
        return result;
      },
      action === "validate"
        ? "校验结果已返回"
        : action === "publish"
          ? "新版本已发布"
          : action === "rollback"
            ? "默认版本已回退"
            : "状态已更新",
    );
  }
  if (resource.loading) return <Loading />;
  if (!form) return <Notice error={resource.error} />;
  return (
    <div className="editor-layout">
      <div className="editor-top">
        <div>
          <h2>{form.display_name || form.name}</h2>
          <div className="inline-meta">
            <code>{form.name}</code>
            <span>修订 {form.revision}</span>
            <Status value={form.enabled ? "enabled" : "disabled"} />
            {form.blocked ? <Status value="failed">已立即阻止</Status> : null}
          </div>
        </div>
        <Updated value={form.updated_at} />
      </div>
      <Notice
        error={resource.error || versions.error || state.error}
        message={state.notice}
      />
      <div className="form-grid compact">
        <Field label="显示名称">
          <input
            aria-label="Skill显示名称"
            value={form.display_name || ""}
            onChange={(e) => change("display_name", e.target.value)}
          />
        </Field>
        <Field label="当前默认版本">
          <input
            aria-label="当前默认版本"
            value={form.current_version || "尚未发布"}
            readOnly
          />
        </Field>
        <div className="span-2">
          <Field label="用途说明">
            <textarea
              aria-label="用途说明"
              rows={2}
              value={form.description || ""}
              onChange={(e) => change("description", e.target.value)}
            />
          </Field>
        </div>
      </div>
      <div className="tabs" role="tablist" aria-label="Skill编辑器">
        {[
          ["content", "正文与引用"],
          ["schema", "输入定义"],
          ["deps", "工具依赖"],
          ["versions", "版本历史"],
          ["diagnostics", "校验结果"],
        ].map(([key, label]) => (
          <button
            role="tab"
            aria-selected={tab === key}
            className={tab === key ? "active" : ""}
            key={key}
            onClick={() => setTab(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="editor-main">
        {tab === "content" ? (
          <div className="file-editor">
            <aside className="file-tree">
              <button
                className={file === "SKILL.md" ? "active" : ""}
                onClick={() => setFile("SKILL.md")}
              >
                <FileCode2 size={14} />
                SKILL.md
              </button>
              <div className="tree-heading">
                references/{" "}
                <button
                  aria-label="新增参考文件"
                  onClick={() => setAddingReference(true)}
                >
                  <Plus size={14} />
                </button>
              </div>
              {Object.keys(refMap).map((path) => (
                <button
                  key={path}
                  className={file === path ? "active" : ""}
                  onClick={() => setFile(path)}
                >
                  <BookOpen size={14} />
                  {path.replace(/^references\//, "")}
                </button>
              ))}
            </aside>
            <div className="code-editor">
              <div className="code-toolbar">
                <code>{file}</code>
                <span className="muted tiny">
                  Markdown 原文 · {dirty ? "有未保存修改" : "已保存"}
                </span>
              </div>
              <textarea
                spellCheck={false}
                className="code-input"
                aria-label={`${file} 正文`}
                value={
                  file === "SKILL.md" ? form.content || "" : refMap[file] || ""
                }
                onChange={(e) => {
                  if (file === "SKILL.md") change("content", e.target.value);
                  else {
                    const next = { ...refMap, [file]: e.target.value };
                    change("references", next);
                    setRefs(json(next));
                  }
                }}
              />
            </div>
          </div>
        ) : null}
        {tab === "schema" ? (
          <div className="stack">
            <p className="muted">
              定义本 Skill 的运行输入；复杂结构使用 JSON
              Schema，提交仍由服务端校验。
            </p>
            <textarea
              aria-label="Skill输入JSON Schema"
              className="code-input large"
              value={schema}
              onChange={(e) => {
                setSchema(e.target.value);
                setDirty(true);
              }}
              spellCheck={false}
            />
          </div>
        ) : null}
        {tab === "deps" ? (
          <div className="stack">
            <div className="scope-note">
              按 server_id 与 tool_name
              绑定已开放只读工具。必需依赖缺失会阻止启动；可选依赖缺失保留范围说明。
            </div>
            <textarea
              aria-label="Skill工具依赖JSON"
              className="code-input large"
              value={deps}
              onChange={(e) => {
                setDeps(e.target.value);
                setDirty(true);
              }}
              spellCheck={false}
            />
            <JsonView
              value={
                form.dependency_status || form.validation?.dependencies || {}
              }
              title="服务端依赖检查"
              open
            />
          </div>
        ) : null}
        {tab === "versions" ? (
          <div className="stack">
            {list(versions.data).length ? (
              <div className="table-wrap">
                <table>
                  <thead>
                    <tr>
                      <th>内容版本</th>
                      <th>类型</th>
                      <th>创建时间</th>
                      <th>操作</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list(versions.data).map((v) => (
                      <tr key={v.id || v.version_id}>
                        <td>
                          <button
                            className="text-link"
                            onClick={() => setVersion(v)}
                          >
                            {v.label || v.version || v.id || v.version_id}
                          </button>
                          {form.current_version === (v.id || v.version_id) ? (
                            <small className="block muted">当前默认</small>
                          ) : null}
                        </td>
                        <td>{v.purpose || "发布快照"}</td>
                        <td>{dateTime(v.created_at)}</td>
                        <td>
                          <div className="actions">
                            <Button
                              variant="ghost"
                              onClick={() =>
                                onRun({
                                  skill_id: id,
                                  skill_version: v.id || v.version_id,
                                  purpose: "skill_test",
                                })
                              }
                            >
                              试运行
                            </Button>
                            <Button
                              variant="ghost"
                              onClick={() =>
                                void command("rollback", {
                                  version_id: v.id || v.version_id,
                                })
                              }
                              disabled={!!state.busy || dirty}
                            >
                              回退默认
                            </Button>
                          </div>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <Empty
                title="还没有内容版本"
                detail="发布草稿后，将在此保留不可变的内容快照。"
              />
            )}
          </div>
        ) : null}
        {tab === "diagnostics" ? (
          diagnostics ? (
            <JsonView value={diagnostics} />
          ) : (
            <Empty
              title="尚未运行校验"
              detail="先保存当前草稿，再校验引用、输入定义及依赖。"
            />
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
            <ShieldAlert size={14} />
            {form.blocked ? "解除阻止" : "立即阻止后续调用"}
          </Button>
        </div>
        <div className="actions">
          <span className="tiny muted">{dirty ? "有未保存修改" : ""}</span>
          <Button onClick={() => void save()} busy={state.busy === "save"}>
            <Save size={14} />
            保存草稿
          </Button>
          <Button
            onClick={() => {
              setTab("diagnostics");
              void command("validate");
            }}
            disabled={dirty}
            busy={state.busy === "validate"}
          >
            <CheckCheck size={15} />
            校验
          </Button>
          <Button
            variant="primary"
            onClick={() => void command("publish")}
            disabled={dirty}
            busy={state.busy === "publish"}
          >
            <Send size={14} />
            发布版本
          </Button>
          <Button
            onClick={() =>
              onRun({
                skill_id: id,
                skill_version: "draft",
                purpose: "skill_test",
              })
            }
            disabled={dirty}
          >
            <Play size={14} />
            试运行
          </Button>
        </div>
      </footer>
      {version ? (
        <Modal title="不可变内容版本" onClose={() => setVersion(null)} wide>
          <Metadata
            items={[
              ["版本", version.id || version.version_id],
              ["时间", dateTime(version.created_at)],
            ]}
          />
          <JsonView value={version} />
        </Modal>
      ) : null}
      {addingReference ? (
        <Modal title="新增参考文件" onClose={() => setAddingReference(false)}>
          <form
            className="stack"
            onSubmit={(e) => {
              e.preventDefault();
              const path = referenceName.startsWith("references/")
                ? referenceName
                : `references/${referenceName}`;
              if (
                path.includes("..") ||
                path.includes("\\") ||
                !referenceName.trim()
              ) {
                state.setError("请填写包内有效的参考文件路径");
                return;
              }
              const next = { ...refMap, [path]: "" };
              change("references", next);
              setRefs(json(next));
              setFile(path);
              setReferenceName("");
              setAddingReference(false);
            }}
          >
            <Field label="文件名" required>
              <input
                aria-label="参考文件名"
                placeholder="query-guide.md"
                value={referenceName}
                onChange={(e) => setReferenceName(e.target.value)}
                required
              />
            </Field>
            <Button type="submit" variant="primary">
              添加到草稿
            </Button>
          </form>
        </Modal>
      ) : null}
    </div>
  );
}
export default function Skills({
  onRun,
  initialId,
}: {
  onRun: (preset: RunPreset) => void;
  initialId?: string | null;
}) {
  const resource = useResource("/skills", 8000),
    [selected, setSelected] = useState<string | null>(initialId || null),
    [search, setSearch] = useState(""),
    [create, setCreate] = useState(false);
  const items = list(resource.data).filter((s) =>
    `${s.name} ${s.display_name} ${s.description}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  useEffect(() => {
    if (initialId) setSelected(initialId);
    else if (!selected && list(resource.data).length)
      setSelected(list(resource.data)[0].id);
  }, [initialId, selected, resource.data]);
  return (
    <div className="page-pad management">
      <PageTitle
        title="Skill 管理"
        description="编辑分析方法，管理不可变内容版本与工具依赖。"
        actions={
          <Button variant="primary" onClick={() => setCreate(true)}>
            <FilePlus2 size={15} />
            新建 Skill
          </Button>
        }
      />
      <Notice error={resource.error} />
      <div className="management-grid">
        <aside className="surface entity-list">
          <div className="surface-toolbar">
            <input
              aria-label="搜索Skill"
              placeholder="搜索 Skill 名称或用途…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          {resource.loading ? (
            <Loading />
          ) : items.length ? (
            items.map((s) => (
              <button
                key={s.id}
                className={`entity-row ${selected === s.id ? "selected" : ""}`}
                onClick={() => setSelected(s.id)}
              >
                <div className="row-top">
                  <strong>{s.display_name || s.name}</strong>
                  <Status
                    value={
                      s.enabled
                        ? "enabled"
                        : s.current_version
                          ? "disabled"
                          : "draft"
                    }
                  />
                </div>
                <small>{s.description || s.name}</small>
                <span className="row-subtitle">
                  {s.current_version || "尚未发布"}
                </span>
              </button>
            ))
          ) : (
            <Empty title="暂无 Skill" detail="创建并发布你的第一项分析方法。" />
          )}
        </aside>
        <section className="surface entity-detail">
          {selected ? (
            <SkillEditor key={selected} id={selected} onRun={onRun} />
          ) : (
            <Empty
              title="选择一个 Skill"
              detail="查看正文、依赖与版本，或创建新的草稿。"
              icon={<BookOpen size={32} />}
            />
          )}
        </section>
      </div>
      {create ? (
        <CreateSkill
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
