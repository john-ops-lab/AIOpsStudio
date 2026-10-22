import { useEffect, useState } from "react";
import { Play } from "lucide-react";
import { list, useResource } from "../api";
import type { Entity, RunPreset } from "../types";
import { Button, Empty, JsonView, Loading, Notice, PageTitle, Status } from "../components/ui";
import Markdown from "../components/Markdown";
export default function ConfigCatalog({ kind, initialId, onAnalyze }: { kind: "skills" | "mcp" | "models"; initialId?: string | null; onAnalyze: (preset: RunPreset) => void }) {
  const path = kind === "skills" ? "/skills" : kind === "mcp" ? "/mcp-servers" : "/model-configs";
  const resource = useResource(path), [selected, setSelected] = useState(initialId || ""), [query, setQuery] = useState("");
  const items = list(resource.data).filter((item) => `${item.name} ${item.display_name || ""} ${item.description || ""}`.toLowerCase().includes(query.toLowerCase()));
  useEffect(() => { if (initialId) setSelected(initialId); }, [initialId]);
  const detail = useResource(selected ? `${path}/${encodeURIComponent(selected)}` : null);
  const value = detail.data;
  return <div className="page-pad config-catalog"><PageTitle title={kind === "skills" ? "Skill 管理" : kind === "mcp" ? "MCP 管理" : "模型设置"} description="查看共享配置目录；配置维护与试调由管理员操作。" /><Notice error={resource.error || detail.error} />
    <div className="catalog-layout"><section className="surface"><div className="surface-toolbar"><input aria-label="搜索配置目录" placeholder="搜索名称或用途…" value={query} onChange={(e) => setQuery(e.target.value)} /></div>{resource.loading ? <Loading /> : items.length ? <ul className="catalog-list">{items.map((item) => <li key={item.id}><button className={selected === item.id ? "selected" : ""} onClick={() => setSelected(item.id)}><strong>{item.display_name || item.name}</strong><span>{item.description || item.model || item.url || item.id}</span><Status value={item.enabled === false ? "disabled" : item.current_version ? "published" : "draft"} /></button></li>)}</ul> : <Empty title="暂无可用配置" />}</section>
      <section className="surface detail-pad">{detail.loading ? <Loading /> : value ? <><h2>{value.display_name || value.name}</h2><p>{value.description || value.model}</p>{kind === "skills" && value.enabled !== false && value.current_version && !value.blocked ? <Button variant="primary" onClick={() => onAnalyze({ skill_id: value.id, skill_version: value.current_version })}><Play size={14} />使用此 Skill 分析</Button> : null}{kind === "skills" ? <><h3>已提供的正文</h3><Markdown text={value.content || "尚无正文。"} /><JsonView title="输入定义" value={value.input_schema} /><JsonView title="依赖绑定" value={value.dependencies} /></> : <JsonView value={Object.fromEntries(Object.entries(value as Entity).filter(([key]) => !/secret|password|token|auth_header/i.test(key)))} />}</> : <Empty title="选择一项配置" detail="查看可用方法、工具定义或模型信息。" />}</section>
    </div>
  </div>;
}
