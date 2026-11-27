import { useEffect, useMemo, useState, type CSSProperties } from 'react';
import { Bot, Network, RotateCcw, X } from 'lucide-react';
import {
  Background, BackgroundVariant, Controls, Handle, MarkerType, Position,
  ReactFlow, type Edge, type Node, type NodeProps, type NodeTypes,
} from '@xyflow/react';
import type { GraphEvent } from '../types';

const STORAGE_KEY = 'dagger.providerColors.v1';

const SYSTEM_CONTROL_NODES = new Set([
  'initialize',
  'initialize_node',
  'human_review_gate',
  'finalize',
  'schedule_node',
  'start',
]);

const PROVIDER_ORDER = ['claude', 'codex', 'gemini', 'copilot', 'agy', 'groq', 'api', 'local', 'system'] as const;
type ProviderKey = (typeof PROVIDER_ORDER)[number];

const DEFAULT_PROVIDER_COLORS: Record<ProviderKey, string> = {
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

const PROVIDER_LABELS: Record<ProviderKey, string> = {
  claude: 'Claude',
  codex: 'Codex',
  gemini: 'Gemini',
  copilot: 'Copilot',
  agy: 'AGY',
  groq: 'Groq',
  api: 'Custom API',
  local: 'Local',
  system: 'System',
};

function normalizeProvider(raw?: string | null, containerId?: string | null, nodeId?: string): ProviderKey {
  const candidates = [raw, containerId, nodeId].filter(Boolean).map(v => String(v).toLowerCase());
  for (const val of candidates) {
    if (val.includes('claude') || val.includes('anthropic')) return 'claude';
    if (val.includes('codex') || val.includes('openai')) return 'codex';
    if (val.includes('gemini') || val.includes('google')) return 'gemini';
    if (val.includes('copilot') || val.includes('github')) return 'copilot';
    if (val.includes('agy') || val.includes('antigravity')) return 'agy';
    if (val.includes('groq')) return 'groq';
    if (val.includes('local') || val.includes('ollama') || val.includes('lmstudio')) return 'local';
  }
  for (const val of candidates) {
    if (val === 'api' || val.startsWith('api_') || val.includes('custom_api') || val.includes('openrouter')) {
      return 'api';
    }
  }
  return 'system';
}

function formatProviderLabel(provider: ProviderKey, rawProviderLabel?: string): string {
  if (provider === 'system' && rawProviderLabel && rawProviderLabel.toLowerCase() !== 'system') {
    return rawProviderLabel;
  }
  return PROVIDER_LABELS[provider];
}

function loadSavedColors(): Record<ProviderKey, string> {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return { ...DEFAULT_PROVIDER_COLORS };
    const parsed = JSON.parse(raw) as Partial<Record<ProviderKey, string>>;
    return { ...DEFAULT_PROVIDER_COLORS, ...parsed };
  } catch {
    return { ...DEFAULT_PROVIDER_COLORS };
  }
}

type FlowData = {
  label: string;
  detail: string;
  provider: ProviderKey;
  rawProviderLabel?: string;
  model?: string;
  color: string;
  skills?: string[];
  allowed_mcp_tools?: string[];
  isWorker?: boolean;
  [key: string]: unknown;
};
type FlowNode = Node<FlowData, 'agent'>;

function extractStringArray(value: unknown): string[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const cleaned = Array.from(
    new Set(
      value
        .filter((item): item is string => typeof item === 'string' && item.trim().length > 0)
        .map(item => item.trim()),
    ),
  );
  return cleaned.length > 0 ? cleaned : undefined;
}

function isWorkerNodeId(id: string): boolean {
  return /(?:^|:)(worker|agent):/.test(id);
}

function AgentNode({ data }: NodeProps<FlowNode>) {
  const color = data.color || DEFAULT_PROVIDER_COLORS[data.provider] || DEFAULT_PROVIDER_COLORS.system;
  const nodeStyle: CSSProperties = {
    borderColor: color,
    background: `linear-gradient(135deg, ${color}1f, #22242b 65%)`,
  };
  const iconStyle: CSSProperties = {
    color,
    background: `${color}22`,
  };
  const pillStyle: CSSProperties = {
    color,
    borderColor: `${color}66`,
    background: `${color}1a`,
  };

  return (
    <div className="flow-node" style={nodeStyle}>
      <Handle type="target" position={Position.Left} />
      <div className="flow-icon" style={iconStyle}>
        <Bot size={17} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flow-top-row">
          <div className="flow-label">{data.label}</div>
          <span className="flow-provider-pill" style={pillStyle}>
            {formatProviderLabel(data.provider, data.rawProviderLabel)}
            {(data.provider !== 'system' || data.rawProviderLabel) && data.model ? ` · ${data.model}` : ''}
          </span>
        </div>
        <div className="flow-detail">{data.detail}</div>
        {data.isWorker && data.skills && data.skills.length > 0 && (
          <div className="flow-skill-pills" title={data.skills.join(', ')}>
            {data.skills.slice(0, 2).map(skill => (
              <span className="flow-skill-pill" key={skill}>
                {skill}
              </span>
            ))}
            {data.skills.length > 2 && (
              <span className="flow-skill-more">+{data.skills.length - 2}</span>
            )}
          </div>
        )}
      </div>
      <span className="status-dot" style={{ background: color }} />
      <Handle type="source" position={Position.Right} />
    </div>
  );
}

type SwarmPostView = {
  id: string;
  seq: string | null;
  author: string;
  tag: 'done' | 'stall' | 'blocked' | null;
  message: string;
  timestamp: string;
};

function parseSwarmPost(event: GraphEvent): SwarmPostView {
  const meta = event.metadata ?? {};
  const rawMsg = String(
    meta.message ?? meta.line ?? meta.content ?? meta.summary ?? event.summary ?? event.label ?? '',
  ).trim();
  const rawSeq = meta.seq;
  let seq: string | null =
    rawSeq !== undefined && rawSeq !== null && String(rawSeq).trim() !== ''
      ? String(rawSeq).startsWith('#')
        ? String(rawSeq).trim()
        : `#${String(rawSeq).trim()}`
      : null;
  if (!seq) {
    const seqMatch = rawMsg.match(/(?:^|\s)#(\d+)\b/);
    if (seqMatch) seq = `#${seqMatch[1]}`;
  }

  const rawTag = typeof meta.tag === 'string' ? meta.tag.toLowerCase() : '';
  let tag: SwarmPostView['tag'] = null;
  if (rawTag.includes('done')) tag = 'done';
  else if (rawTag.includes('stall')) tag = 'stall';
  else if (rawTag.includes('blocked')) tag = 'blocked';
  else {
    const tagMatch = rawMsg.match(/\[(done|stall|blocked)\]/i);
    if (tagMatch) tag = tagMatch[1].toLowerCase() as SwarmPostView['tag'];
  }

  const author =
    typeof meta.author === 'string' && meta.author.trim() ? meta.author.trim() : 'swarm';

  return {
    id: event.event_id,
    seq,
    author,
    tag,
    message: rawMsg || 'Swarm update',
    timestamp: event.timestamp,
  };
}

const nodeTypes: NodeTypes = { agent: AgentNode };

export default function LiveGraph({
  events,
  selected,
  onSelect,
}: {
  events: GraphEvent[];
  selected: string | null;
  onSelect: (id: string | null) => void;
}) {
  const [providerColors, setProviderColors] = useState<Record<ProviderKey, string>>(loadSavedColors);

  useEffect(() => {
    try {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(providerColors));
    } catch {
      /* Ignore storage quota errors */
    }
  }, [providerColors]);

  const { nodes, edges } = useMemo(() => {
    const nodeMap = new Map<string, FlowNode>();
    const edgeMap = new Map<string, Edge>();
    const addNode = (id: string, event: GraphEvent, isTarget: boolean) => {
      if (!id) return;
      const current = nodeMap.get(id);
      const index = nodeMap.size;
      const metaProvider =
        isTarget && typeof event.metadata?.provider === 'string' ? event.metadata.provider : undefined;
      const metaModel =
        isTarget && typeof event.metadata?.model === 'string' && event.metadata.model.trim()
          ? event.metadata.model.trim()
          : undefined;
      const isControlGate = SYSTEM_CONTROL_NODES.has(id.toLowerCase());
      const rawProvider =
        isTarget && event.provider && event.provider !== 'system' ? event.provider : metaProvider;
      const inferred = isControlGate
        ? 'system'
        : normalizeProvider(
            isTarget ? rawProvider : undefined,
            isTarget ? event.container_id : undefined,
            id,
          );
      const provider: ProviderKey = isControlGate
        ? 'system'
        : inferred !== 'system'
          ? inferred
          : current?.data.provider ?? 'system';
      const rawProviderLabel = isControlGate
        ? undefined
        : (rawProvider && rawProvider.toLowerCase() !== 'system' ? rawProvider : current?.data.rawProviderLabel);
      const model =
        provider !== 'system' || rawProviderLabel ? metaModel || current?.data.model : undefined;
      const color = providerColors[provider] || DEFAULT_PROVIDER_COLORS[provider];
      const isWorker =
        isWorkerNodeId(id) ||
        (isTarget && event.metadata?.node === 'container_worker_node') ||
        Boolean(current?.data.isWorker);
      const metaSkills = isTarget
        ? extractStringArray(event.metadata?.skills) ?? extractStringArray(event.metadata?.skill_names)
        : undefined;
      const metaTools = isTarget ? extractStringArray(event.metadata?.allowed_mcp_tools) : undefined;
      const skills = metaSkills ?? current?.data.skills;
      const allowed_mcp_tools = metaTools ?? current?.data.allowed_mcp_tools;

      nodeMap.set(id, {
        id,
        type: 'agent',
        position: current?.position ?? { x: (index % 3) * 260, y: Math.floor(index / 3) * 150 },
        data: {
          label:
            event.event_type === 'NODE_SPAWNED' && isTarget
              ? event.label && event.label !== event.event_type
                ? event.label
                : id
              : current?.data.label || id,
          detail:
            (isTarget ? metaModel || event.container_id || (provider !== 'system' ? event.provider : rawProviderLabel) : undefined) ||
            current?.data.detail ||
            (provider === 'system' && !rawProviderLabel ? 'Control gate' : 'Agent'),
          provider,
          rawProviderLabel,
          model,
          color,
          skills,
          allowed_mcp_tools,
          isWorker,
        },
      });
    };

    for (const event of events) {
      if (event.source_node) addNode(event.source_node, event, false);
      if (event.target_node) addNode(event.target_node, event, true);
      if (event.source_node && event.target_node && ['EDGE_TRAVERSED', 'FALLBACK'].includes(event.event_type)) {
        const fallback = event.event_type === 'FALLBACK';
        const id = `${event.source_node}:${event.target_node}:${fallback}`;
        edgeMap.set(id, {
          id,
          source: event.source_node,
          target: event.target_node,
          label: event.label || (fallback ? 'Fallback' : undefined),
          animated: !fallback,
          markerEnd: { type: MarkerType.ArrowClosed, color: fallback ? '#e6ae70' : '#8291a3' },
          style: {
            stroke: fallback ? '#e6ae70' : '#8291a3',
            strokeWidth: 1.5,
            ...(fallback ? { strokeDasharray: '6 5' } : {}),
          },
          labelStyle: { fill: '#b5bcc9', fontSize: 11 },
          labelBgStyle: { fill: '#191c23' },
        });
      }
    }
    return { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] };
  }, [events, providerColors]);

  const relevant = events.filter(
    event =>
      event.source_node === selected ||
      event.target_node === selected ||
      event.node_id === selected ||
      event.metadata?.node_id === selected ||
      (typeof event.metadata?.milestone_id === 'string' &&
        selected === `worker:${event.metadata.milestone_id}`),
  );
  const swarmPosts = relevant
    .filter(event => event.event_type === 'SWARM_POST')
    .map(parseSwarmPost);
  const selectedNode = selected ? nodes.find(node => node.id === selected) : undefined;
  const selectedProvider: ProviderKey = selectedNode?.data.provider ?? 'system';
  const selectedRawProviderLabel = selectedNode?.data.rawProviderLabel;
  const selectedColor =
    selectedNode?.data.color ?? providerColors[selectedProvider] ?? DEFAULT_PROVIDER_COLORS.system;
  const selectedModel =
    selectedProvider !== 'system' || selectedRawProviderLabel ? selectedNode?.data.model : undefined;

  return (
    <div className="graph-wrapper">
      <div className="graph-color-bar" role="region" aria-label="Provider Color Legend and Customizer">
        <span className="graph-color-title">Provider colors</span>
        <div className="graph-color-swatches">
          {PROVIDER_ORDER.map(provider => (
            <label
              key={provider}
              className="provider-swatch-chip"
              title={`Customize ${PROVIDER_LABELS[provider]} color`}
            >
              <span
                className="provider-swatch-dot"
                style={{ backgroundColor: providerColors[provider] }}
              />
              <span>{PROVIDER_LABELS[provider]}</span>
              <input
                type="color"
                className="provider-color-input"
                value={providerColors[provider]}
                aria-label={`${PROVIDER_LABELS[provider]} color`}
                onChange={e =>
                  setProviderColors(prev => ({ ...prev, [provider]: e.target.value }))
                }
              />
            </label>
          ))}
        </div>
        <button
          type="button"
          className="reset-colors-button"
          onClick={() => setProviderColors({ ...DEFAULT_PROVIDER_COLORS })}
        >
          <RotateCcw size={12} /> Reset Defaults
        </button>
      </div>

      <div className="graph-layout">
        <div className="graph-canvas">
          {nodes.length ? (
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              fitView
              minZoom={0.3}
              maxZoom={1.5}
              onNodeClick={(_, node) => onSelect(node.id)}
              onPaneClick={() => onSelect(null)}
            >
              <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#30343d" />
              <Controls showInteractive={false} />
            </ReactFlow>
          ) : (
            <div className="graph-empty">
              <div className="empty-icon">
                <Network size={23} />
              </div>
              <strong>No graph events yet</strong>
              <span>Select a run to see its agents and connections.</span>
            </div>
          )}
          <div className="graph-legend">
            <span className="status-dot green" /> Agent <span className="legend-line" /> Connection{' '}
            <span className="legend-line dashed" /> Fallback
          </div>
        </div>
        {selected && (
          <aside className="inspector">
            <div className="inspector-head">
              <strong>Node inspector</strong>
              <button className="icon-button" aria-label="Close inspector" onClick={() => onSelect(null)}>
                <X size={15} />
              </button>
            </div>
            <div
              className="agent-icon"
              style={{
                color: selectedColor,
                background: `${selectedColor}22`,
                borderColor: `${selectedColor}66`,
              }}
            >
              <Bot size={20} />
            </div>
            <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' }}>
              <h3 style={{ margin: 0 }}>{selected}</h3>
              <span
                className="flow-provider-pill"
                style={{
                  color: selectedColor,
                  borderColor: `${selectedColor}66`,
                  background: `${selectedColor}1a`,
                }}
              >
                {formatProviderLabel(selectedProvider, selectedRawProviderLabel)}
                {selectedModel ? ` · ${selectedModel}` : ''}
              </span>
            </div>
            <span className="subtle">{relevant.length} related events</span>
            {(() => {
              const selectedSkills = selectedNode?.data.skills ?? [];
              const selectedTools = selectedNode?.data.allowed_mcp_tools ?? [];
              const isSelectedWorker = Boolean(
                selectedNode?.data.isWorker || isWorkerNodeId(selected),
              );
              if (!isSelectedWorker && selectedSkills.length === 0 && selectedTools.length === 0) {
                return null;
              }
              return (
                <div className="inspector-telemetry">
                  <div className="inspector-section">
                    <div className="inspector-section-label">Matched skills</div>
                    {selectedSkills.length > 0 ? (
                      <div className="inspector-skill-list">
                        {selectedSkills.map(skill => (
                          <span className="inspector-skill-pill" key={skill}>
                            {skill}
                          </span>
                        ))}
                      </div>
                    ) : (
                      <span className="inspector-empty">None</span>
                    )}
                  </div>
                  <div className="inspector-section">
                    <div className="inspector-section-label">Allowed MCP tools</div>
                    {selectedTools.length > 0 ? (
                      <div className="inspector-tool-list">
                        {selectedTools.map(tool => (
                          <code className="inspector-tool-chip" key={tool}>
                            {tool}
                          </code>
                        ))}
                      </div>
                    ) : (
                      <span className="inspector-empty">None</span>
                    )}
                  </div>
                </div>
              );
            })()}
            {swarmPosts.length > 0 && (
              <div className="blackboard-feed" role="region" aria-label="Live Blackboard Feed">
                <div className="blackboard-head">
                  <span>Blackboard feed</span>
                  <span className="blackboard-count">{swarmPosts.length}</span>
                </div>
                <div className="blackboard-list">
                  {swarmPosts.map(post => (
                    <div className="blackboard-post" key={post.id}>
                      <div className="blackboard-meta">
                        {post.seq && <span className="blackboard-seq">{post.seq}</span>}
                        <span className="blackboard-author">{post.author}</span>
                        {post.tag && (
                          <span className={`blackboard-tag ${post.tag}`}>[{post.tag}]</span>
                        )}
                      </div>
                      <div className="blackboard-msg">{post.message}</div>
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="event-list">
              {relevant
                .slice()
                .reverse()
                .map(event => (
                  <div className="event" key={event.event_id}>
                    <strong>{event.event_type.replace(/_/g, ' ')}</strong>
                    <span>
                      {event.label || 'Graph event'} ·{' '}
                      {event.provider && event.provider !== 'system'
                        ? event.provider
                        : typeof event.metadata?.provider === 'string' && event.metadata.provider
                          ? event.metadata.provider
                          : event.provider || 'system'}
                      {typeof event.metadata?.model === 'string' && event.metadata.model
                        ? ` (${event.metadata.model})`
                        : ''}
                    </span>
                    {event.container_id && <span>Container: {event.container_id}</span>}
                    {event.metadata && Object.keys(event.metadata).length > 0 && (
                      <code>{JSON.stringify(event.metadata, null, 2)}</code>
                    )}
                    <small>{event.timestamp}</small>
                  </div>
                ))}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
