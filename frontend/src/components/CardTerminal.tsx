import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowDown, Terminal } from 'lucide-react';
import type { CardLogChannel, CardLogEntry, Task } from '../types';

type FilterTab = 'all' | 'dialogue' | 'terminal' | 'system';

const PROVIDER_COLORS: Record<string, string> = {
  claude: '#d97757',
  codex: '#10a37f',
  gemini: '#4285f4',
  copilot: '#8b5cf6',
  agy: '#ec4899',
  groq: '#f59e0b',
  api: '#38bdf8',
  local: '#14b8a6',
  system: '#8291a3',
};

function getProviderColor(provider?: string | null, nodeId?: string | null): string {
  const candidates = [provider, nodeId].filter(Boolean).map(v => String(v).toLowerCase());
  for (const val of candidates) {
    if (val.includes('claude') || val.includes('anthropic')) return PROVIDER_COLORS.claude;
    if (val.includes('codex') || val.includes('openai')) return PROVIDER_COLORS.codex;
    if (val.includes('gemini') || val.includes('google')) return PROVIDER_COLORS.gemini;
    if (val.includes('copilot') || val.includes('github')) return PROVIDER_COLORS.copilot;
    if (val.includes('agy') || val.includes('antigravity')) return PROVIDER_COLORS.agy;
    if (val.includes('groq')) return PROVIDER_COLORS.groq;
    if (val.includes('local') || val.includes('ollama') || val.includes('lmstudio')) return PROVIDER_COLORS.local;
  }
  for (const val of candidates) {
    if (val === 'api' || val.startsWith('api_') || val.includes('custom_api') || val.includes('openrouter')) {
      return PROVIDER_COLORS.api;
    }
  }
  return PROVIDER_COLORS.system;
}

function formatTime(ts: string): string {
  const date = new Date(ts);
  if (Number.isNaN(date.getTime())) return '--:--:--';
  return date.toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

const CHANNEL_LABEL: Record<CardLogChannel, string> = {
  dialogue: 'DIALOGUE',
  stdout: 'STDOUT',
  stderr: 'STDERR',
  command: 'CMD',
  system: 'SYSTEM',
};

export default function CardTerminal({
  task,
  logs,
  connected,
}: {
  task: Task | null;
  logs: CardLogEntry[];
  connected: boolean;
}) {
  const [filter, setFilter] = useState<FilterTab>('all');
  const [autoScroll, setAutoScroll] = useState(true);
  const bodyRef = useRef<HTMLDivElement | null>(null);

  const filteredLogs = useMemo(() => {
    if (filter === 'all') return logs;
    if (filter === 'dialogue') return logs.filter(entry => entry.channel === 'dialogue');
    if (filter === 'system') return logs.filter(entry => entry.channel === 'system');
    return logs.filter(
      entry => entry.channel === 'stdout' || entry.channel === 'stderr' || entry.channel === 'command',
    );
  }, [logs, filter]);

  useEffect(() => {
    setAutoScroll(true);
  }, [task?.task_id]);

  useEffect(() => {
    if (!autoScroll || !bodyRef.current) return;
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [filteredLogs, autoScroll]);

  const handleScroll = () => {
    const el = bodyRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (distanceFromBottom > 32) {
      if (autoScroll) setAutoScroll(false);
    } else if (!autoScroll) {
      setAutoScroll(true);
    }
  };

  const jumpToLatest = () => {
    setAutoScroll(true);
    if (bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  };

  return (
    <div className="card-terminal">
      <div className="terminal-toolbar">
        <div className="terminal-title-group">
          <Terminal size={14} className="terminal-icon" />
          <strong className="terminal-task-title">
            {task ? task.title : 'No card selected'}
          </strong>
          {task && <span className="task-status">{task.status}</span>}
          <span className="pill">
            <span className={`status-dot ${connected ? 'green' : 'muted'}`} />
            {connected ? 'Live' : 'Offline'}
          </span>
          <span className="terminal-count">{filteredLogs.length} lines</span>
        </div>

        <div className="terminal-filters" role="tablist" aria-label="Log channel filter">
          {(
            [
              { id: 'all', label: 'All' },
              { id: 'dialogue', label: 'Dialogue' },
              { id: 'terminal', label: 'Terminal' },
              { id: 'system', label: 'System' },
            ] as { id: FilterTab; label: string }[]
          ).map(tab => (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={filter === tab.id}
              className={`skills-filter-pill ${filter === tab.id ? 'active' : ''}`}
              onClick={() => setFilter(tab.id)}
            >
              {tab.label}
            </button>
          ))}
        </div>
      </div>

      <div className="terminal-body-wrap">
        <div className="terminal-body" ref={bodyRef} onScroll={handleScroll}>
          {!task ? (
            <div className="terminal-empty">Select a Kanban card or run to inspect live activity and terminal logs.</div>
          ) : filteredLogs.length === 0 ? (
            <div className="terminal-empty">No {filter === 'all' ? '' : `${filter} `}logs recorded for this card yet.</div>
          ) : (
            filteredLogs.map(entry => {
              const color = getProviderColor(entry.provider, entry.node_id);
              const tagLabel = entry.provider && entry.provider !== 'system'
                ? `${entry.provider}:${entry.node_id}`
                : entry.node_id || entry.provider || 'system';
              return (
                <div key={entry.log_id} className={`terminal-row terminal-row-${entry.channel}`}>
                  <span className="terminal-ts">{formatTime(entry.ts)}</span>
                  <span
                    className="provider-tag"
                    style={{
                      color,
                      borderColor: `${color}55`,
                      backgroundColor: `${color}18`,
                    }}
                    title={`${entry.provider} · ${entry.node_id}`}
                  >
                    {tagLabel}
                  </span>
                  <span className={`channel-badge channel-badge-${entry.channel}`}>
                    {CHANNEL_LABEL[entry.channel] ?? entry.channel.toUpperCase()}
                  </span>
                  <span className="terminal-text">{entry.text}</span>
                </div>
              );
            })
          )}
        </div>

        {!autoScroll && filteredLogs.length > 0 && (
          <button type="button" className="scroll-resume-btn" onClick={jumpToLatest}>
            <ArrowDown size={13} /> Auto-scroll paused · Jump to latest
          </button>
        )}
      </div>
    </div>
  );
}
