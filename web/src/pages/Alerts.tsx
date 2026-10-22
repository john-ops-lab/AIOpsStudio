import { useRef, useState } from "react";
import { ArrowUpRight, Bell, Search } from "lucide-react";
import { dateTime, json, list, useResource } from "../api";
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
  Refresh,
  Status,
} from "../components/ui";
const hasContent = (value: unknown) =>
  typeof value === "string"
    ? !!value.trim()
    : Array.isArray(value)
      ? value.length > 0
      : !!value && typeof value === "object"
        ? Object.keys(value).length > 0
        : !!value;
const titleOf = (a: Entity) =>
  a.rule_name || a.title || a.name || a.summary || String(a.id);
function targetOf(a: Entity) {
  if (a.target_type && a.target_id !== undefined && a.target_id !== null)
    return `${a.target_type}:${a.target_id}`;
  const target = a.target || a.resource || a.object_id;
  return typeof target === "object"
    ? json(target)
    : target
      ? String(target)
      : "";
}
const limitationText = (value: unknown) =>
  Array.isArray(value)
    ? value.map((x) => (typeof x === "string" ? x : json(x))).join("\n")
    : typeof value === "string"
      ? value
      : json(value);
function AlertDetail({
  id,
  onClose,
  onInvestigate,
}: {
  id: string;
  onClose: () => void;
  onInvestigate: (preset: RunPreset) => void;
}) {
  const resource = useResource(`/alerts/${encodeURIComponent(id)}`),
    [anchorMode, setAnchorMode] = useState("latest");
  const openedAt = useRef(Date.now());
  const raw = resource.data;
  const data =
    raw?.detail && typeof raw.detail === "object"
      ? {
          ...raw.detail,
          timeline: raw.timeline ?? raw.detail.timeline,
          limitations: raw.limitations ?? raw.detail.limitations,
        }
      : raw;
  const first =
    data?.fired_at || data?.starts_at || data?.started_at || data?.created_at;
  const latest = data?.last_fired_at || first;
  const anchor = anchorMode === "latest" ? latest : first;
  const anchorMs = anchor ? new Date(anchor).getTime() : NaN;
  const start = Number.isFinite(anchorMs)
    ? new Date(anchorMs - 5 * 60000).toISOString()
    : data?.analysis_start;
  const end = Number.isFinite(anchorMs)
    ? new Date(
        Math.max(
          anchorMs + 1000,
          Math.min(anchorMs + 15 * 60000, openedAt.current),
        ),
      ).toISOString()
    : data?.analysis_end;
  return (
    <Modal title="告警事件详情" onClose={onClose} wide>
      <Notice error={resource.error} />
      {resource.loading ? <Loading /> : null}
      {data ? (
        <div className="stack">
          <div className="section-heading">
            <h3>{titleOf(data)}</h3>
            <Status
              value={String(
                data.severity || data.level || "unknown",
              ).toLowerCase()}
            />
          </div>
          <Metadata
            items={[
              ["事件 ID", data.id || id],
              ["规则标识", data.rule_key],
              ["来源", data.source || "告警 API"],
              ["源对象", targetOf(data) || "未知"],
              ["状态", data.status],
              ["初次触发", dateTime(first)],
              ["最近触发", dateTime(latest)],
              ["恢复时间", dateTime(data.resolved_at || data.ends_at)],
            ]}
          />
          {data.summary || data.description ? (
            <div className="prose">{data.summary || data.description}</div>
          ) : null}
          {hasContent(data.limitations) ? (
            <div className="scope-note">{limitationText(data.limitations)}</div>
          ) : null}
          <div className="form-grid">
            <Field
              label="分析触发时刻"
              hint="同一告警可能重复触发，请选择本次要分析的时间。"
            >
              <select
                aria-label="分析触发时刻"
                value={anchorMode}
                onChange={(e) => setAnchorMode(e.target.value)}
              >
                <option value="latest">最近触发 · {dateTime(latest)}</option>
                <option value="first">初次触发 · {dateTime(first)}</option>
              </select>
            </Field>
            <Field
              label="预设分析窗口"
              hint="默认触发前5分钟至后15分钟；未来结束时间固定到本次打开时刻。下一步可调整。"
            >
              <div className="window-preview">
                {start && end
                  ? `${dateTime(start)} — ${dateTime(end)}`
                  : "源时间不足，请在下一步手动指定窗口"}
              </div>
            </Field>
          </div>
          <div className="scope-note">
            源对象 {targetOf(data) || "未知"} 保留告警系统的类型与
            ID，尚未核对为 CMDB CI；分析时需要确认身份映射。
          </div>
          <JsonView value={data.timeline || []} title="来源事件时间线" />
          <JsonView value={data} title="源告警完整字段" />
          <div className="form-actions">
            <Button
              variant="primary"
              onClick={() =>
                onInvestigate({
                  incident_id: String(data.id || id),
                  title: `${titleOf(data)} · ${anchorMode === "latest" ? "最近触发" : "初次触发"}`,
                  target: targetOf(data) || undefined,
                  analysis_start: start,
                  analysis_end: end,
                  message: [
                    data.summary || data.description,
                    `本次选择${anchorMode === "latest" ? "最近触发" : "初次触发"}：${dateTime(anchor)}。源对象 ${targetOf(data) || "未知"} 尚未核对为 CMDB CI。`,
                  ]
                    .filter(Boolean)
                    .join("\n"),
                })
              }
            >
              基于此告警分析
              <ArrowUpRight size={15} />
            </Button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}
export default function Alerts({
  onInvestigate,
}: {
  onInvestigate: (preset: RunPreset) => void;
}) {
  const resource = useResource("/alerts", 30000),
    [search, setSearch] = useState(""),
    [selected, setSelected] = useState<string | null>(null);
  const items = list(resource.data).filter((a) =>
    `${titleOf(a)} ${a.id} ${a.rule_key || ""} ${targetOf(a)}`
      .toLowerCase()
      .includes(search.toLowerCase()),
  );
  return (
    <div className="page-pad">
      <PageTitle
        title="告警事件"
        description="从已有告警中选择事件，固定对象和时间窗发起分析。"
        actions={
          <Refresh
            onClick={() => void resource.reload()}
            busy={resource.loading}
          />
        }
      />
      <Notice error={resource.error} />
      {hasContent(resource.data?.limitations) ? (
        <div className="scope-note">
          {limitationText(resource.data?.limitations)}
        </div>
      ) : null}
      <div className="surface">
        <div className="surface-toolbar">
          <div className="search-control">
            <Search size={16} />
            <input
              aria-label="搜索告警事件"
              placeholder="搜索事件、源对象或告警 ID"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
            />
          </div>
          <span className="muted">
            {resource.data?.total ?? items.length} 条源记录
          </span>
        </div>
        {resource.loading ? (
          <Loading />
        ) : items.length ? (
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>事件</th>
                  <th>级别</th>
                  <th>源对象</th>
                  <th>状态</th>
                  <th>最近触发</th>
                  <th>来源</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {items.map((a) => (
                  <tr key={a.id}>
                    <td>
                      <button
                        className="text-link strong"
                        onClick={() => setSelected(String(a.id))}
                      >
                        {titleOf(a)}
                      </button>
                      <small className="block muted">
                        #{a.id}
                        {a.rule_key ? ` · ${a.rule_key}` : ""}
                      </small>
                    </td>
                    <td>
                      <Status
                        value={String(
                          a.severity || a.level || "unknown",
                        ).toLowerCase()}
                      />
                    </td>
                    <td>{targetOf(a) || "未知"}</td>
                    <td>
                      <Status value={a.status} />
                    </td>
                    <td className="nowrap">
                      {dateTime(
                        a.last_fired_at ||
                          a.fired_at ||
                          a.starts_at ||
                          a.started_at ||
                          a.created_at,
                      )}
                    </td>
                    <td>{a.source || "告警 API"}</td>
                    <td>
                      <Button
                        variant="ghost"
                        onClick={() => setSelected(String(a.id))}
                      >
                        详情
                        <ArrowUpRight size={14} />
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : (
          <Empty
            title="当前没有可显示的告警"
            detail={
              search
                ? "没有匹配的记录。"
                : "请在模型设置中检查告警数据源配置；仅展示源接口实际返回的事件。"
            }
            icon={<Bell size={31} />}
          />
        )}
      </div>
      {selected ? (
        <AlertDetail
          id={selected}
          onClose={() => setSelected(null)}
          onInvestigate={(preset) => {
            setSelected(null);
            onInvestigate(preset);
          }}
        />
      ) : null}
    </div>
  );
}
