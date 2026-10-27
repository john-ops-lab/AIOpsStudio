import { useEffect, useState } from "react";
import { MessageSquare, Search } from "lucide-react";
import { api } from "../../api";
import type { Entity } from "../../types";
import { Button, Notice } from "../ui";
export default function HistorySidebar({ selected, onSelect, revision }: { selected: string | null; onSelect: (id: string) => void; revision: number }) {
  const [text, setText] = useState(""), [search, setSearch] = useState(""), [archived, setArchived] = useState(false);
  const [cursor, setCursor] = useState<string | null>(null), [next, setNext] = useState<string | null>(null);
  const [items, setItems] = useState<Entity[]>([]), [loading, setLoading] = useState(false), [error, setError] = useState("");
  useEffect(() => { setCursor(null); setItems([]); }, [search, archived, revision]);
  useEffect(() => {
    let live = true; const controller = new AbortController();
    const params = new URLSearchParams({ search, archived: String(archived), limit: "25" });
    if (cursor) params.set("cursor", cursor);
    setLoading(true); setError("");
    void api(`/conversations?${params}`, { signal: controller.signal }).then((data) => {
      if (!live) return;
      setItems((previous) => { const values = cursor ? [...previous, ...(data.items || [])] : data.items || []; return Array.from(new Map(values.map((item: Entity) => [item.id, item])).values()) as Entity[]; });
      setNext(data.next_cursor || null);
    }).catch((e) => { if (live) { setError(e instanceof Error ? e.message : String(e)); setItems([]); } }).finally(() => { if (live) setLoading(false); });
    return () => { live = false; controller.abort(); };
  }, [search, archived, cursor, revision]);
  return <>
    <form className="history-search" onSubmit={(e) => { e.preventDefault(); setCursor(null); setSearch(text.trim()); }}><input aria-label="搜索已保存会话" placeholder="搜索会话…" value={text} onChange={(e) => setText(e.target.value)} /><button aria-label="搜索会话" type="submit"><Search size={15} /></button></form>
    <div className="history-filter"><button className={!archived ? "selected" : ""} onClick={() => { setCursor(null); setArchived(false); }}>最近会话</button><button className={archived ? "selected" : ""} onClick={() => { setCursor(null); setArchived(true); }}>已归档</button></div>
    <Notice error={error} />
    <div className="conversation-history">{items.length ? <ul>{items.map((c) => <li key={c.id}><button className={selected === c.id ? "selected" : ""} onClick={() => onSelect(c.id)} aria-current={selected === c.id ? "page" : undefined}><MessageSquare size={16} aria-hidden="true" /><span>{c.title || "未命名会话"}</span></button></li>)}</ul> : <span className="history-empty">{loading ? "正在加载…" : archived ? "暂无归档会话" : "暂无匹配会话"}</span>}{next ? <Button className="history-more" busy={loading} onClick={() => setCursor(next)}>加载更多</Button> : null}</div>
  </>;
}
