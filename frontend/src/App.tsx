import { useCallback, useEffect, useState } from 'react';
import {
  Activity, ArrowRight, Bot, Boxes, Check, ChevronDown, CircleDot,
  Command, GitBranch, LayoutDashboard, LoaderCircle, Network, Plus,
  RefreshCw, X,
} from 'lucide-react';
import { api } from './api';
import type { Agent, Capability, GraphEvent, Task } from './types';
import LiveGraph from './components/LiveGraph';
import KanbanBoard from './components/KanbanBoard';
import ContainerRegistry from './components/ContainerRegistry';

type View = 'overview' | 'graph' | 'tasks' | 'agents';

const navigation: { id: View; label: string; icon: typeof Network }[] = [
  { id: 'overview', label: 'Overview', icon: LayoutDashboard },
  { id: 'graph', label: 'Live graph', icon: Network },
  { id: 'tasks', label: 'Task board', icon: Boxes },
  { id: 'agents', label: 'Registry', icon: Bot },
];
function mergeEvents(existing: GraphEvent[], incoming: GraphEvent[]) {
  const byId = new Map(existing.map(event => [event.event_id, event]));
  incoming.forEach(event => byId.set(event.event_id, event));
  return [...byId.values()].sort((a, b) => a.timestamp.localeCompare(b.timestamp));
}

function App() {
  const [view, setView] = useState<View>('overview');
  const [tasks, setTasks] = useState<Task[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [capabilities, setCapabilities] = useState<Capability[]>([]);
  const [events, setEvents] = useState<GraphEvent[]>([]);
  const [runId, setRunId] = useState('');
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [connected, setConnected] = useState(false);
  const [error, setError] = useState('');
  const [modal, setModal] = useState(false);
  const [title, setTitle] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const results = await Promise.allSettled([api.tasks(), api.agents(), api.capabilities()]);
    if (results[0].status === 'fulfilled') setTasks(results[0].value);
    if (results[1].status === 'fulfilled') setAgents(results[1].value);
    if (results[2].status === 'fulfilled') setCapabilities(results[2].value);
    if (results.some(result => result.status === 'rejected')) setError('Unable to reach the DAGGER API. Check that the backend is running.');
  }, []);

  useEffect(() => {
    void refresh();
    const interval = window.setInterval(() => void refresh(), 15000);
    return () => window.clearInterval(interval);
  }, [refresh]);
  useEffect(() => {
    if (!runId) {
      const firstRun = tasks.find(task => task.run_id)?.run_id;
      if (firstRun) setRunId(firstRun);
    }
  }, [tasks, runId]);
  useEffect(() => {
    setEvents([]); setSelectedNode(null);
    if (!runId) return;
    let active = true;
    const hydrate = () => api.events(runId).then(result => { if (active) setEvents(current => mergeEvents(current, result)); }).catch(() => { if (active) setError('Could not load graph events for this run.'); });
    void hydrate();
    let retry: number | undefined;
    let socket: WebSocket;
    const connect = () => {
      socket = new WebSocket(`${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/ws/graph`);
      socket.onopen = () => { setConnected(true); void hydrate(); };
      socket.onmessage = event => {
        try {
          const value = JSON.parse(String(event.data)) as GraphEvent;
          if (value.event_id && value.run_id === runId) setEvents(current => mergeEvents(current, [value]));
          void refresh();
        } catch { /* Ignore malformed events; hydration restores the stream. */ }
      };
      socket.onclose = () => { setConnected(false); if (active) retry = window.setTimeout(connect, 3000); };
      socket.onerror = () => socket.close();
    };
    connect();
    return () => { active = false; setConnected(false); window.clearTimeout(retry); socket.close(); };
  }, [runId, refresh]);

  const act = async (operation: () => Promise<unknown>) => {
    try { setError(''); await operation(); await refresh(); }
    catch (err) { setError(err instanceof Error ? err.message : 'Request failed.'); }
  };
  const submit = async (autoStart: boolean) => {
    if (!title.trim() || !description.trim()) return;
    setBusy(true);
    try {
      setError('');
      const task = await api.createTask(title.trim(), description.trim(), autoStart);
      setModal(false); setTitle(''); setDescription('');
      if (task.run_id) setRunId(task.run_id);
      await refresh(); setView(autoStart ? 'graph' : 'tasks');
    } catch (err) { setError(err instanceof Error ? err.message : 'Could not create task.'); }
    finally { setBusy(false); }
  };
  const runs = tasks.filter(task => task.run_id);

  return <div className="app-shell">
    <aside className="sidebar"><div className="brand"><div className="brand-mark"><Command size={19} /></div><div><strong>DAGGER</strong><span>AGENT CONTROL PLANE</span></div></div><div className="workspace"><div className="workspace-icon">D</div><div><strong>Workspace</strong><span>Local environment</span></div><ChevronDown size={15} /></div><div className="nav-caption">WORKSPACE</div><nav aria-label="Main navigation">{navigation.map(item => <button key={item.id} className={`nav-item ${view === item.id ? 'selected' : ''}`} onClick={() => setView(item.id)}><item.icon size={17} />{item.label}{item.id === 'tasks' && <span className="nav-count">{tasks.length}</span>}</button>)}</nav><div className="sidebar-bottom"><div className="sidebar-status"><span className={`status-dot ${connected ? 'green' : 'muted'}`} /><div><strong>{connected ? 'Live updates connected' : 'Live updates offline'}</strong><span>{runId ? 'Graph event stream' : 'Select a run to connect'}</span></div></div><div className="sidebar-version">DAGGER <span>LOCAL FIRST · v0.1</span></div></div></aside>
    <main className="main"><header className="topbar"><div className="breadcrumb">Workspace <span>/</span> <strong>{navigation.find(item => item.id === view)?.label}</strong></div><div className="top-actions"><span className="live"><span className={`status-dot ${connected ? 'green' : 'muted'}`} /> {connected ? 'LIVE' : 'OFFLINE'}</span><button className="icon-button" title="Refresh" aria-label="Refresh" onClick={() => void refresh()}><RefreshCw size={16} /></button><div className="avatar">D</div></div></header>
      <div className="content">{error && <div className="error-banner" role="alert">{error}<button aria-label="Dismiss error" onClick={() => setError('')}><X size={15} /></button></div>}
        <div className="page-heading"><div><div className="eyebrow"><span className="status-dot green" /> YOUR CONTROL PLANE</div><h1>{view === 'overview' ? 'Mission control' : view === 'graph' ? 'Live graph' : view === 'tasks' ? 'Task board' : 'Agent registry'}</h1><p>{view === 'overview' ? 'Your agents, tasks, and workflows at a glance.' : view === 'graph' ? 'See how your agents work together in real time.' : view === 'tasks' ? 'Track and manage work across your agents.' : 'Discover available agents and endpoint capabilities.'}</p></div><button className="primary-button" onClick={() => setModal(true)}><Plus size={16} /> New task</button></div>
        {view === 'overview' && <><div className="stats-grid"><Stat icon={Bot} label="TOTAL AGENTS" value={agents.length} note="Configured agents" /><Stat icon={Activity} label="RUNNING TASKS" value={tasks.filter(task => task.status === 'Running').length} note="Currently in progress" /><Stat icon={Check} label="COMPLETED" value={tasks.filter(task => task.status === 'Done').length} note="Tasks finished" /><Stat icon={GitBranch} label="GRAPH EVENTS" value={events.length} note="In selected run" /></div><div className="panel graph-panel"><GraphHeader runs={runs} runId={runId} onRun={setRunId} connected={connected} /><LiveGraph events={events} selected={selectedNode} onSelect={setSelectedNode} /></div><div className="panel recent-panel"><div className="panel-header"><div><h2>Recent tasks</h2><p>The latest work in your workspace</p></div><button className="text-button" onClick={() => setView('tasks')}>View all <ArrowRight size={14} /></button></div>{tasks.length ? <div className="recent-list">{tasks.slice(0, 5).map(task => <div className="recent-row" key={task.task_id}><div className="recent-icon"><CircleDot size={16} /></div><div className="recent-title"><strong>{task.title}</strong><span>{task.description}</span></div><span className="task-status">{task.status}</span></div>)}</div> : <div className="empty-line">No tasks yet. Create a task to get started.</div>}</div></>}
        {view === 'graph' && <div className="panel graph-panel full-graph"><GraphHeader runs={runs} runId={runId} onRun={setRunId} connected={connected} /><LiveGraph events={events} selected={selectedNode} onSelect={setSelectedNode} /></div>}
        {view === 'tasks' && <><div className="board-intro"><span>{tasks.length} total tasks</span><span>Drag a card or use its menu to update status</span></div><KanbanBoard tasks={tasks} onMove={(id, status) => void act(() => api.updateTask(id, status))} onApprove={id => void act(() => api.approve(id))} /></>}
        {view === 'agents' && <ContainerRegistry agents={agents} capabilities={capabilities} onRefresh={() => void act(api.refreshRegistry)} />}
      </div></main>
    {modal && <div className="modal-backdrop" onMouseDown={event => { if (event.target === event.currentTarget && !busy) setModal(false); }}><div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title"><div className="modal-heading"><div><h2 id="modal-title">Create a task</h2><p>Give your agents something to work on.</p></div><button className="icon-button" aria-label="Close" onClick={() => setModal(false)} disabled={busy}><X size={17} /></button></div><form onSubmit={event => { event.preventDefault(); void submit(false); }}><label htmlFor="task-title">Title</label><input id="task-title" value={title} onChange={event => setTitle(event.target.value)} placeholder="e.g. Research authentication options" required autoFocus /><label htmlFor="task-description">Description</label><textarea id="task-description" value={description} onChange={event => setDescription(event.target.value)} placeholder="Describe what you'd like your agents to do..." rows={5} required /><div className="modal-actions"><button type="button" className="secondary-button" onClick={() => setModal(false)} disabled={busy}>Cancel</button><button type="submit" className="secondary-button" disabled={busy || !title.trim() || !description.trim()}>Add to board</button><button type="button" className="primary-button" disabled={busy || !title.trim() || !description.trim()} onClick={() => void submit(true)}>{busy && <LoaderCircle size={15} className="spin" />} Create &amp; run</button></div></form></div></div>}
  </div>;
}

function GraphHeader({ runs, runId, onRun, connected }: { runs: Task[]; runId: string; onRun: (id: string) => void; connected: boolean }) {
  return <div className="panel-header"><div><h2>Agent graph <span className="pill"><span className={`status-dot ${connected ? 'green' : 'muted'}`} /> {connected ? 'Live' : 'Offline'}</span></h2><p>Events and connections for the selected run</p></div><label className="run-select">Run <select value={runId} onChange={event => onRun(event.target.value)}><option value="">Select a run</option>{runs.map(task => <option key={task.task_id} value={task.run_id || ''}>{task.title} · {task.run_id?.slice(0, 8)}</option>)}</select></label></div>;
}

function Stat({ icon: Icon, label, value, note }: { icon: typeof Bot; label: string; value: number; note: string }) {
  return <div className="stat-card"><div className="stat-head"><span>{label}</span><Icon size={16} /></div><div className="stat-value">{value.toString().padStart(2, '0')}</div><div className="stat-note">{note}</div></div>;
}
export default App;
