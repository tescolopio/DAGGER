import { Check, Terminal } from 'lucide-react';
import type { Task, TaskStatus } from '../types';

const columns: { status: TaskStatus; dot: string }[] = [
  { status: 'Backlog', dot: 'muted' }, { status: 'Planning', dot: 'purple' },
  { status: 'Running', dot: 'blue' }, { status: 'DoD Audit', dot: 'yellow' },
  { status: 'Review', dot: 'yellow' }, { status: 'Done', dot: 'green' },
  { status: 'Failed', dot: 'red' },
];

export default function KanbanBoard({
  tasks,
  onMove,
  onApprove,
  selectedTaskId,
  onSelectTask,
}: {
  tasks: Task[];
  onMove: (id: string, status: TaskStatus) => void;
  onApprove: (id: string) => void;
  selectedTaskId?: string | null;
  onSelectTask?: (task: Task) => void;
}) {
  return <div className="board">{columns.map(column => {
    const items = tasks.filter(task => task.status === column.status);
    return <section className="board-column" key={column.status} onDragOver={event => event.preventDefault()} onDrop={event => { event.preventDefault(); const id = event.dataTransfer.getData('text/plain'); if (id && column.status !== 'Done') onMove(id, column.status); }}><div className="column-heading"><span className={`status-dot ${column.dot}`} />{column.status}<span className="count">{items.length}</span></div><div className="column-content">{items.map(task => <article className={`task-card ${selectedTaskId === task.task_id ? 'selected' : ''}`} key={task.task_id} draggable onDragStart={event => event.dataTransfer.setData('text/plain', task.task_id)}><div className="task-id-row"><div className="task-id">TASK-{task.task_id.slice(0, 6).toUpperCase()}</div>{onSelectTask && <button type="button" className="task-activity-btn" onClick={() => onSelectTask(task)} title="Open Card Terminal & Live Graph"><Terminal size={12} /> Activity</button>}</div><h3>{task.title}</h3><p>{task.description}</p><div className="task-footer"><span>{task.milestones?.length || 0} milestones</span><label className="sr-only" htmlFor={`status-${task.task_id}`}>Move task</label><select id={`status-${task.task_id}`} value={task.status} onChange={event => onMove(task.task_id, event.target.value as TaskStatus)}>{columns.map(option => <option value={option.status} key={option.status} disabled={option.status === 'Done' && task.status !== 'Done'}>{option.status}</option>)}</select></div>{task.status === 'Review' && <button className="approve-button" onClick={() => onApprove(task.task_id)}><Check size={14} /> Approve</button>}</article>)}{!items.length && <div className="column-empty">No tasks</div>}</div></section>;
  })}</div>;
}
