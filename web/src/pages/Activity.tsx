import { useState } from "react";
import { ArrowUpRight, Clock3, Database, ListChecks } from "lucide-react";
import { dateTime, json, list, useResource } from "../api";
import type { Entity } from "../types";
import {
  Button,
  Empty,
  JsonView,
  Loading,
  Metadata,
  Modal,
  Notice,
  PageTitle,
  Refresh,
  Status,
} from "../components/ui";
import { EvidenceModal } from "./Runs";
export function Overview({ onRun }: { onRun: (id: string) => void }) {
  const resource = useResource("/overview", 10000),
    data = resource.data;
  const counts = data?.counts || {};
  const cards = [
    ["分析总数", counts.runs ?? counts.total_runs],
    ["执行中", data?.queue?.running ?? counts.running],
    ["等待队列", data?.queue?.length ?? data?.queue?.queued ?? counts.queued],
    ["证据记录", counts.evidence ?? counts.evidences],
  ];
  return (
    <div className="page-pad">
      <PageTitle
        title="运行概览"
        description="查看实际队列、服务连接与最近运行记录。"
        actions={
          <Refresh
            onClick={() => void resource.reload()}
            busy={resource.loading}
          />
        }
      />
      <Notice error={resource.error} />
      {resource.loading ? <Loading /> : null}
      <div className="metrics">
        {cards.map(([label, value]) => (
          <div key={String(label)} className="metric">
            <span>{label}</span>
            <strong>
              {value === undefined || value === null
                ? "—"
                : typeof value === "object"
                  ? json(value)
                  : String(value)}
            </strong>
            <small>
              {value === undefined || value === null
                ? "后端尚未提供"
                : "当前持久记录"}
            </small>
          </div>
        ))}
      </div>
      <div className="overview-grid">
        <section className="surface">
          <div className="section-heading">
            <h2>服务状态</h2>
            <Database size={19} />
          </div>
          {data?.services ? (
            <JsonView value={data.services} />
          ) : (
            <Empty title="尚无服务检查结果" detail="服务检查数据由后端返回。" />
          )}
        </section>
        <section className="surface">
          <div className="section-heading">
            <h2>执行队列</h2>
            <ListChecks size={20} />
          </div>
          {data?.queue ? (
            <JsonView value={data.queue} />
          ) : (
            <Empty
              title="尚无队列状态"
              detail="分析和各类试调共享一个执行槽。"
            />
          )}
        </section>
      </div>
      <section className="surface">
        <div className="section-heading">
          <h2>最近运行</h2>
          <Clock3 size={18} />
        </div>
        {data?.recent_runs?.length ? (
          <RunRows items={data.recent_runs} onRun={onRun} />
        ) : (
          <Empty title="暂无运行记录" detail="提交分析或试调后将在这里出现。" />
        )}
      </section>
      <section className="surface storage-section">
        <h2>存储与计量</h2>
        <JsonView value={data?.storage || { 说明: "后端尚未提供存储统计" }} />
        <p className="muted">
          用量缺失显示未知；费用仅在有明确单价来源时估算。
        </p>
      </section>
    </div>
  );
}
function RunRows({
  items,
  onRun,
}: {
  items: Entity[];
  onRun: (id: string) => void;
}) {
  return (
    <div className="table-wrap">
      <table>
        <thead>
          <tr>
            <th>任务</th>
            <th>用途</th>
            <th>状态</th>
            <th>创建时间</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {items.map((r) => (
            <tr key={r.id}>
              <td>
                <button
                  className="text-link strong"
                  onClick={() => onRun(r.id)}
                >
                  {r.title || r.id}
                </button>
              </td>
              <td>{r.purpose}</td>
              <td>
                <Status value={r.status} />
              </td>
              <td>{dateTime(r.created_at)}</td>
              <td>
                <Button
                  variant="ghost"
                  onClick={() => onRun(r.id)}
                  aria-label={`打开${r.title || r.id}`}
                >
                  <ArrowUpRight size={16} />
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
export function Invocations({ onRun, isAdmin = true }: { onRun: (id: string) => void; isAdmin?: boolean }) {
  const resource = useResource("/invocations", 10000),
    audit = useResource(isAdmin ? "/config-audit" : null),
    [tab, setTab] = useState("calls"),
    [search, setSearch] = useState(""),
    [selected, setSelected] = useState<Entity | null>(null),
    [evidence, setEvidence] = useState<string | null>(null);
  const items = list(tab === "calls" ? resource.data : audit.data).filter((i) =>
    json(i).toLowerCase().includes(search.toLowerCase()),
  );
  return (
    <div className="page-pad">
      <PageTitle
        title="调用记录"
        description="保留实际调用、首败、显式重试与配置变更的来源。"
        actions={
          <Refresh
            onClick={() => {
              void resource.reload();
              void audit.reload();
            }}
            busy={resource.loading || audit.loading}
          />
        }
      />
      <Notice error={resource.error || audit.error} />
      <section className="surface">
        <div className="surface-toolbar">
          <div className="tabs" role="tablist" aria-label="记录类型">
            <button
              role="tab"
              aria-selected={tab === "calls"}
              className={tab === "calls" ? "active" : ""}
              onClick={() => setTab("calls")}
            >
              工具与模型调用
            </button>
            {isAdmin ? <button
              role="tab"
              aria-selected={tab === "audit"}
              className={tab === "audit" ? "active" : ""}
              onClick={() => setTab("audit")}
            >
              配置变更
            </button> : null}
          </div>
          <input
            aria-label="搜索调用记录"
            className="record-search"
            placeholder="搜索名称、状态或来源…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        {(tab === "calls" ? resource.loading : audit.loading) ? (
          <Loading />
        ) : items.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>{tab === "calls" ? "调用 / 工具" : "配置操作"}</th>
                  <th>来源</th>
                  <th>状态</th>
                  <th>耗时</th>
                  <th>发生时间</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((item, index) => (
                  <tr key={item.id || index}>
                    <td>
                      <button
                        className="text-link strong"
                        onClick={() => setSelected(item)}
                      >
                        {item.tool_name ||
                          item.model ||
                          item.action ||
                          item.type ||
                          item.id}
                      </button>
                      <small className="block muted">{item.id}</small>
                    </td>
                    <td>
                      {item.server_id ||
                        item.provider ||
                        item.kind ||
                        item.entity_type ||
                        item.purpose ||
                        "未知"}
                    </td>
                    <td>
                      <Status
                        value={
                          item.status ||
                          (item.success === true
                            ? "succeeded"
                            : item.success === false
                              ? "failed"
                              : undefined)
                        }
                      />
                    </td>
                    <td>
                      {item.duration_ms !== undefined
                        ? `${item.duration_ms} ms`
                        : item.elapsed_ms !== undefined
                          ? `${item.elapsed_ms} ms`
                          : "未知"}
                    </td>
                    <td>{dateTime(item.created_at || item.timestamp)}</td>
                    <td>
                      {item.run_id ? (
                        <Button
                          variant="ghost"
                          onClick={() => onRun(item.run_id)}
                        >
                          分析
                          <ArrowUpRight size={14} />
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title={tab === "calls" ? "暂无调用记录" : "暂无配置变更"}
            detail={
              search
                ? "没有匹配的记录。"
                : "真实执行或配置变更后，记录会保存在这里。"
            }
          />
        )}
      </section>
      {selected ? (
        <Modal title="记录详情" onClose={() => setSelected(null)} wide>
          <Metadata
            items={[
              ["记录 ID", selected.id],
              ["来源任务", selected.run_id],
              ["时间", dateTime(selected.created_at)],
            ]}
          />
          <JsonView value={selected} />
          <div className="form-actions">
            {selected.evidence_id ? (
              <Button onClick={() => setEvidence(selected.evidence_id)}>
                打开原始证据
              </Button>
            ) : null}
            {selected.run_id ? (
              <Button variant="primary" onClick={() => onRun(selected.run_id)}>
                打开来源分析
              </Button>
            ) : null}
          </div>
        </Modal>
      ) : null}
      {evidence ? (
        <EvidenceModal id={evidence} onClose={() => setEvidence(null)} />
      ) : null}
    </div>
  );
}
