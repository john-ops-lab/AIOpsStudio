import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Activity, ArrowLeft, Bell, Blocks, Bot, ChevronDown, FileClock, LayoutDashboard, LogOut, Menu, MessageSquare, Network, Plus, Search, ShieldCheck, UsersRound, X } from "lucide-react";
import { api, list, post, requestId, useAction, useResource } from "./api";
import type { Conversation, Entity, Page, Run, RunPreset, User } from "./types";
import { Button, Empty, Field, Loading, Modal, Notice } from "./components/ui";
import { IdentityContext } from "./components/Identity";
import NewRun from "./components/NewRun";
const Workbench = lazy(() => import("./pages/Workbench")), Runs = lazy(() => import("./pages/Runs")), Alerts = lazy(() => import("./pages/Alerts")), Skills = lazy(() => import("./pages/Skills")), Mcp = lazy(() => import("./pages/Mcp")), Models = lazy(() => import("./pages/Models")), Users = lazy(() => import("./pages/Users")), Catalog = lazy(() => import("./pages/ConfigCatalog")), Overview = lazy(() => import("./pages/Activity").then((m) => ({ default: m.Overview }))), Invocations = lazy(() => import("./pages/Activity").then((m) => ({ default: m.Invocations }))), RuntimeProvider = lazy(() => import("./RuntimeProvider"));
const nav = [
  { id: "workbench", label: "运维工作台", icon: MessageSquare },
  { id: "alerts", label: "告警事件", icon: Bell },
  { id: "skills", label: "Skill 管理", icon: Blocks },
  { id: "mcp", label: "MCP 管理", icon: Network },
  { id: "models", label: "模型设置", icon: Bot },
  { id: "invocations", label: "调用记录", icon: FileClock },
  { id: "overview", label: "运行概览", icon: LayoutDashboard },
] as const;
function route() {
  const [page, id] = location.hash.slice(1).split("/");
  return { page: [...nav.map((n) => n.id), "runs", "users"].includes(page as never) ? page as Page : "workbench" as Page, id: id ? decodeURIComponent(id) : null };
}
function Login({ onLogin }: { onLogin: (session: Entity) => void }) {
  const [username, setUsername] = useState("admin"), [password, setPassword] = useState(""), state = useAction();
  return <main className="login-screen"><div className="login-brand"><span className="easyops-mark">E</span><span>EasyOps</span></div><form className="login-form" onSubmit={(e) => { e.preventDefault(); void state.act("login", async () => { await post("/login", { username, password }); setPassword(""); onLogin(await api("/session")); }); }}><div className="login-symbol"><ShieldCheck size={27} /></div><h1>登录运维工作台</h1><p>使用你的 EasyOps 账号继续。</p><Notice error={state.error} /><Field label="用户名" required><input value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" autoFocus required /></Field><Field label="密码" required><input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required /></Field><Button variant="primary" type="submit" busy={Boolean(state.busy)}>登录工作台</Button><small>只读分析 · 会话与报告持续保存</small></form></main>;
}
function GlobalSearch({ query, onClose, onNavigate }: { query: string; onClose: () => void; onNavigate: (page: Page, id?: string) => void }) {
  const conversations = useResource("/conversations"), runs = useResource("/runs"), skills = useResource("/skills"), mcps = useResource("/mcp-servers");
  const [text, setText] = useState(query);
  const results = [
    ...list(conversations.data).map((x) => ({ id: x.id, page: "workbench" as Page, label: x.title || x.id, kind: "会话" })),
    ...list(runs.data).map((x) => ({ id: x.id, page: "runs" as Page, label: x.title || x.id, kind: "分析记录" })),
    ...list(skills.data).map((x) => ({ id: x.id, page: "skills" as Page, label: x.display_name || x.name, kind: "Skill" })),
    ...list(mcps.data).map((x) => ({ id: x.id, page: "mcp" as Page, label: x.name, kind: "MCP" })),
  ].filter((x) => `${x.label} ${x.id}`.toLowerCase().includes(text.toLowerCase()));
  return <Modal title="搜索工作台" onClose={onClose}><div className="stack"><input aria-label="搜索工作台内容" value={text} onChange={(e) => setText(e.target.value)} autoFocus placeholder="输入会话、分析记录、Skill 或 MCP 名称" /><Notice error={conversations.error || runs.error || skills.error || mcps.error} />{conversations.loading || runs.loading || skills.loading || mcps.loading ? <Loading /> : results.length ? <div className="search-results">{results.map((r) => <button key={`${r.page}:${r.id}`} onClick={() => { onNavigate(r.page, r.id); onClose(); }}><span>{r.label}</span><small>{r.kind}</small></button>)}</div> : <Empty title="没有匹配的内容" detail="尝试名称或记录 ID。" />}</div></Modal>;
}
function Shell({ session, onLogout }: { session: Entity; onLogout: () => void }) {
  const navPanel = useRef<HTMLElement>(null);
  const [current, setCurrent] = useState(route), [newRun, setNewRun] = useState<RunPreset | null>(null), [menu, setMenu] = useState(false), [search, setSearch] = useState(""), [searchOpen, setSearchOpen] = useState(false), [starter, setStarter] = useState<{ id: string; message: string } | null>(null), [draftReset, setDraftReset] = useState(0);
  const conversations = useResource("/conversations", 6000), action = useAction();
  const user: User = typeof session.user === "object" ? session.user : { id: "admin", username: String(session.user || "admin"), name: String(session.user || "admin"), role: "admin" };
  const isAdmin = user.role === "admin", name = user.name || user.username;
  useEffect(() => { const onHash = () => { setCurrent(route()); setMenu(false); }; window.addEventListener("hashchange", onHash); return () => window.removeEventListener("hashchange", onHash); }, []);
  useEffect(() => { if (!menu) return; navPanel.current?.querySelector("button")?.focus(); const escape = (e: KeyboardEvent) => { if (e.key === "Escape") { setMenu(false); document.querySelector<HTMLButtonElement>(".mobile-menu")?.focus(); } }; window.addEventListener("keydown", escape); return () => window.removeEventListener("keydown", escape); }, [menu]);
  function navigate(page: Page, id?: string) { location.hash = `${page}${id ? "/" + encodeURIComponent(id) : ""}`; setCurrent({ page, id: id || null }); setMenu(false); }
  function created(run: Run) { setNewRun(null); navigate("runs", run.id); }
  function beginAnalysis(preset: RunPreset) {
    if (preset.purpose === "skill_test" && isAdmin) { setNewRun(preset); return; }
    void action.act("conversation", async () => {
      const { message, title, ...context } = preset;
      const result = await post<Conversation>("/conversations", { title, context, client_request_id: requestId() });
      setStarter({ id: result.id, message: message || "" }); navigate("workbench", result.id); await conversations.reload();
    });
  }
  const item = nav.find((n) => n.id === current.page) || { id: current.page, label: current.page === "users" ? "账号管理" : "分析记录", icon: current.page === "users" ? UsersRound : FileClock };
  const workbench = current.page === "workbench";
  return <IdentityContext value={user}><div className="app-shell chat-shell">
    <a className="skip-link" href="#main-content" onClick={(e) => { e.preventDefault(); document.getElementById("main-content")?.focus(); }}>跳转到主内容</a>
    {menu ? <button className="sidebar-scrim" onClick={() => setMenu(false)} aria-label="收起导航" /> : null}
    <aside ref={navPanel} className={`sidebar ${menu ? "open" : ""}`}><a className="brand" href="#workbench" onClick={() => navigate("workbench")}><span className="easyops-mark">E</span><span>EasyOps</span></a><nav aria-label="主导航">{nav.map((n) => <button key={n.id} className={current.page === n.id || (current.page === "runs" && n.id === "workbench") ? "active" : ""} onClick={() => navigate(n.id)} aria-current={current.page === n.id ? "page" : undefined}><n.icon size={19} strokeWidth={1.7} aria-hidden="true" /><span>{n.label}</span></button>)}</nav>
      <div className="conversation-nav"><Button className="new-conversation" onClick={() => { setStarter(null); setDraftReset((n) => n + 1); navigate("workbench"); }}><Plus size={17} />新建会话</Button><div className="history-label">最近会话</div><Notice error={conversations.error} /><div className="conversation-history">{conversations.loading ? <span className="history-empty">正在加载…</span> : list(conversations.data).length ? <ul>{list(conversations.data).map((c) => <li key={c.id}><button className={workbench && current.id === c.id ? "selected" : ""} onClick={() => navigate("workbench", c.id)} aria-current={workbench && current.id === c.id ? "page" : undefined}><MessageSquare size={16} aria-hidden="true" /><span>{c.title || "未命名会话"}</span></button></li>)}</ul> : <span className="history-empty">尚无已保存会话</span>}</div></div>
      <div className="sidebar-user"><span className="avatar">{name.slice(0, 1).toUpperCase()}</span><div><strong>{name}</strong><small>{isAdmin ? "管理员" : "运维用户"}</small></div><details><summary aria-label="账号操作"><ChevronDown size={16} /></summary><div className="account-popover">{isAdmin ? <button onClick={() => navigate("users")}><UsersRound size={15} />账号管理</button> : null}<button onClick={() => void action.act("logout", async () => { await post("/logout"); onLogout(); })}><LogOut size={15} />退出登录</button></div></details></div>
    </aside>
    <div className="app-content"><header className="topbar"><div className="breadcrumb"><Button className="mobile-menu" variant="ghost" onClick={() => setMenu(!menu)} aria-label={menu ? "收起导航" : "展开导航"} aria-expanded={menu}>{menu ? <X size={19} /> : <Menu size={19} />}</Button><item.icon size={16} aria-hidden="true" /><span>{item.label}</span></div><div className="topbar-actions"><form className="global-search" onSubmit={(e) => { e.preventDefault(); setSearchOpen(true); }}><Search size={15} aria-hidden="true" /><input aria-label="全局搜索" placeholder="搜索会话、分析、Skill…" value={search} onChange={(e) => setSearch(e.target.value)} /><kbd>↵</kbd></form><span className="topbar-identity"><span className="avatar">{name.slice(0, 1).toUpperCase()}</span><span>{name}</span></span></div></header>
      <Notice error={action.error} />
      <div id="main-content" className={`page-content ${workbench ? "workbench-page" : ""}`} tabIndex={-1}><Suspense fallback={<Loading />}>
        {workbench ? <Workbench key={draftReset} selected={current.id} initialMessage={starter?.id === current.id ? starter.message : ""} onSelect={(id) => navigate("workbench", id)} onChanged={() => void conversations.reload()} /> : null}
        {current.page === "runs" ? <Runs selected={current.id} onSelect={(id) => navigate("runs", id)} /> : null}
        {current.page === "alerts" ? <Alerts onInvestigate={beginAnalysis} /> : null}
        {current.page === "skills" ? isAdmin ? <Skills initialId={current.id} onRun={beginAnalysis} /> : <Catalog kind="skills" initialId={current.id} onAnalyze={beginAnalysis} /> : null}
        {current.page === "mcp" ? isAdmin ? <Mcp initialId={current.id} onRun={created} /> : <Catalog kind="mcp" initialId={current.id} onAnalyze={beginAnalysis} /> : null}
        {current.page === "models" ? isAdmin ? <Models onRun={created} /> : <Catalog kind="models" onAnalyze={beginAnalysis} /> : null}
        {current.page === "invocations" ? <Invocations onRun={(id) => navigate("runs", id)} isAdmin={isAdmin} /> : null}
        {current.page === "overview" ? <Overview onRun={(id) => navigate("runs", id)} /> : null}
        {current.page === "users" ? isAdmin ? <Users /> : <Empty title="需要管理员权限" detail="账号管理由管理员操作。" /> : null}
      </Suspense></div></div>
    {newRun ? <NewRun preset={newRun} onClose={() => setNewRun(null)} onCreated={created} /> : null}
    {searchOpen ? <GlobalSearch query={search} onClose={() => setSearchOpen(false)} onNavigate={navigate} /> : null}
  </div></IdentityContext>;
}
export default function App() {
  const session = useResource("/session");
  useEffect(() => { const expired = () => session.setData({ authenticated: false }); window.addEventListener("easyops:session-expired", expired); return () => window.removeEventListener("easyops:session-expired", expired); }, [session.setData]);
  if (session.loading) return <div className="full-loading"><Activity size={28} /><Loading /></div>;
  if (session.error) return <main className="connection-error"><ShieldCheck size={36} /><h1>无法连接 EasyOps</h1><Notice error={session.error} /><p>服务暂时不可用，请稍后重新连接。</p><Button onClick={() => void session.reload()}><ArrowLeft size={16} />重新连接</Button></main>;
  if (!session.data?.authenticated) return <Login onLogin={session.setData} />;
  return <Suspense fallback={<Loading />}><RuntimeProvider><Shell key={session.data.user?.id || "admin"} session={session.data} onLogout={() => session.setData({ authenticated: false })} /></RuntimeProvider></Suspense>;
}
