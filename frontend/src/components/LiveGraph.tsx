import { useMemo } from 'react';
import { Bot, Network, X } from 'lucide-react';
import {
  Background, BackgroundVariant, Controls, Handle, MarkerType, Position,
  ReactFlow, type Edge, type Node, type NodeProps, type NodeTypes,
} from '@xyflow/react';
import type { GraphEvent } from '../types';

type FlowData = { label: string; detail: string; [key: string]: unknown };
type FlowNode = Node<FlowData, 'agent'>;

function AgentNode({ data }: NodeProps<FlowNode>) {
  return <div className="flow-node"><Handle type="target" position={Position.Left} /><div className="flow-icon"><Bot size={17} /></div><div className="min-w-0 flex-1"><div className="flow-label">{data.label}</div><div className="flow-detail">{data.detail}</div></div><span className="status-dot green" /><Handle type="source" position={Position.Right} /></div>;
}
const nodeTypes: NodeTypes = { agent: AgentNode };

export default function LiveGraph({ events, selected, onSelect }: { events: GraphEvent[]; selected: string | null; onSelect: (id: string | null) => void }) {
  const { nodes, edges } = useMemo(() => {
    const nodeMap = new Map<string, FlowNode>();
    const edgeMap = new Map<string, Edge>();
    const addNode = (id: string, event: GraphEvent) => {
      if (!id) return;
      const current = nodeMap.get(id);
      const index = nodeMap.size;
      nodeMap.set(id, {
        id, type: 'agent', position: current?.position ?? { x: (index % 3) * 260, y: Math.floor(index / 3) * 150 },
        data: {
          label: event.event_type === 'NODE_SPAWNED' && event.target_node === id ? (event.label && event.label !== event.event_type ? event.label : id) : current?.data.label || id,
          detail: event.container_id || event.provider || current?.data.detail || 'Agent',
        },
      });
    };
    for (const event of events) {
      if (event.source_node) addNode(event.source_node, event);
      if (event.target_node) addNode(event.target_node, event);
      if (event.source_node && event.target_node && ['EDGE_TRAVERSED', 'FALLBACK'].includes(event.event_type)) {
        const fallback = event.event_type === 'FALLBACK';
        const id = `${event.source_node}:${event.target_node}:${fallback}`;
        edgeMap.set(id, { id, source: event.source_node, target: event.target_node, label: event.label || (fallback ? 'Fallback' : undefined), animated: !fallback, markerEnd: { type: MarkerType.ArrowClosed, color: fallback ? '#e6ae70' : '#8291a3' }, style: { stroke: fallback ? '#e6ae70' : '#8291a3', strokeWidth: 1.5, ...(fallback ? { strokeDasharray: '6 5' } : {}) }, labelStyle: { fill: '#b5bcc9', fontSize: 11 }, labelBgStyle: { fill: '#191c23' } });
      }
    }
    return { nodes: [...nodeMap.values()], edges: [...edgeMap.values()] };
  }, [events]);
  const relevant = events.filter(event => event.source_node === selected || event.target_node === selected);
  return <div className="graph-layout"><div className="graph-canvas">{nodes.length ? <ReactFlow nodes={nodes} edges={edges} nodeTypes={nodeTypes} fitView minZoom={0.3} maxZoom={1.5} onNodeClick={(_, node) => onSelect(node.id)} onPaneClick={() => onSelect(null)} proOptions={{ hideAttribution: true }}><Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#30343d" /><Controls showInteractive={false} /></ReactFlow> : <div className="graph-empty"><div className="empty-icon"><Network size={23} /></div><strong>No graph events yet</strong><span>Select a run to see its agents and connections.</span></div>}<div className="graph-legend"><span className="status-dot green" /> Agent <span className="legend-line" /> Connection <span className="legend-line dashed" /> Fallback</div></div>{selected && <aside className="inspector"><div className="inspector-head"><strong>Node inspector</strong><button className="icon-button" aria-label="Close inspector" onClick={() => onSelect(null)}><X size={15} /></button></div><div className="agent-icon"><Bot size={20} /></div><h3>{selected}</h3><span className="subtle">{relevant.length} related events</span><div className="event-list">{relevant.slice().reverse().map(event => <div className="event" key={event.event_id}><strong>{event.event_type.replace(/_/g, ' ')}</strong><span>{event.label || 'Graph event'} · {event.provider || 'system'}</span>{event.container_id && <span>Container: {event.container_id}</span>}{event.metadata && Object.keys(event.metadata).length > 0 && <code>{JSON.stringify(event.metadata, null, 2)}</code>}<small>{event.timestamp}</small></div>)}</div></aside>}</div>;
}
