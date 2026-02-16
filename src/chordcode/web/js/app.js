// Chord Code - Main Application
import { render } from 'preact';
import { html } from 'htm/preact';
import { useEffect, useState, useRef, useCallback } from 'preact/hooks';
import { signal, computed } from '@preact/signals';
import { marked } from 'marked';

// ============ State (inline for simplicity) ============
const sessions = signal([]);
const activeSessionId = signal(null);
const messages = signal(new Map());
const parts = signal(new Map());
const partsByMessage = signal(new Map());
const todos = signal([]);
const events = signal([]);
const pendingPermissions = signal([]);
const sessionStatus = signal('idle');
const sseConnected = signal(false);
const sidebarCollapsed = signal(false);
const observabilityCollapsed = signal(false);
const observabilityTab = signal('events');
const showNewSessionForm = signal(false);
const defaultWorktree = signal('');

// ============ API ============
async function api(url, options = {}) {
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', ...options.headers },
    ...options,
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(err.detail || 'Request failed');
  }
  return res.json();
}

// ============ SSE Manager ============
let sseSource = null;

function connectSSE(sessionId) {
  if (sseSource) sseSource.close();

  const url = `/events?session_id=${encodeURIComponent(sessionId)}`;
  sseSource = new EventSource(url);

  sseSource.onopen = () => {
    console.log('[SSE] Connected');
    sseConnected.value = true;
  };

  sseSource.onmessage = (evt) => {
    try {
      const event = JSON.parse(evt.data);
      handleSSEEvent(event);
    } catch (e) {
      console.error('[SSE] Parse error:', e);
    }
  };

  sseSource.onerror = () => {
    console.warn('[SSE] Connection error');
    sseConnected.value = false;
  };
}

function handleSSEEvent(event) {
  const { type, properties } = event;

  // Add to events log
  events.value = [{ type, properties, time: Date.now() }, ...events.value].slice(0, 100);

  switch (type) {
    case 'session.status':
      sessionStatus.value = properties.status;
      break;

    case 'message.updated':
      const msg = properties.info;
      const newMsgs = new Map(messages.value);
      newMsgs.set(msg.id, msg);
      messages.value = newMsgs;
      break;

    case 'message.part.updated':
      const part = properties.part;
      const newParts = new Map(parts.value);
      newParts.set(part.id, part);
      parts.value = newParts;

      // Update partsByMessage
      const newPBM = new Map(partsByMessage.value);
      const pset = newPBM.get(part.message_id) || new Set();
      pset.add(part.id);
      newPBM.set(part.message_id, pset);
      partsByMessage.value = newPBM;
      break;

    case 'permission.asked':
      pendingPermissions.value = [...pendingPermissions.value, properties];
      break;

    case 'permission.replied':
      pendingPermissions.value = pendingPermissions.value.filter(p => p.id !== properties.request_id);
      break;

    case 'todo.updated':
      todos.value = properties.todos || [];
      break;
  }
}

// ============ Utilities ============
function formatTime(ts) {
  if (!ts) return '';
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function formatRelative(ts) {
  if (!ts) return '';
  const diff = Date.now() - ts;
  const mins = Math.floor(diff / 60000);
  const hours = Math.floor(mins / 60);
  const days = Math.floor(hours / 24);
  if (days > 0) return `${days}d ago`;
  if (hours > 0) return `${hours}h ago`;
  if (mins > 0) return `${mins}m ago`;
  return 'just now';
}

function formatDuration(ms) {
  if (!ms) return '';
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
}

// Configure marked
marked.setOptions({ breaks: true, gfm: true });

function renderMd(text) {
  if (!text) return '';
  try {
    return marked.parse(text);
  } catch (e) {
    return text.replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }
}

// ============ Components ============

// App Shell
function App() {
  useEffect(() => {
    // Load initial data
    loadConfig();
    loadSessions();
  }, []);

  async function loadConfig() {
    try {
      const cfg = await api('/config');
      defaultWorktree.value = cfg.default_worktree || '';
    } catch (e) {
      console.error('Failed to load config:', e);
    }
  }

  async function loadSessions() {
    try {
      const data = await api('/sessions');
      sessions.value = data.sessions || [];

      // Auto-select first session if exists
      if (data.sessions.length > 0 && !activeSessionId.value) {
        selectSession(data.sessions[0].id);
      }
    } catch (e) {
      console.error('Failed to load sessions:', e);
    }
  }

  async function selectSession(id) {
    activeSessionId.value = id;
    messages.value = new Map();
    parts.value = new Map();
    partsByMessage.value = new Map();
    todos.value = [];
    pendingPermissions.value = [];

    // Connect SSE
    connectSSE(id);

    // Load messages
    try {
      const msgList = await api(`/sessions/${id}/messages`);
      const msgMap = new Map();
      const partMap = new Map();
      const pbmMap = new Map();

      msgList.forEach(mwp => {
        msgMap.set(mwp.info.id, mwp.info);
        const pids = new Set();
        mwp.parts.forEach(p => {
          partMap.set(p.id, p);
          pids.add(p.id);
        });
        pbmMap.set(mwp.info.id, pids);
      });

      messages.value = msgMap;
      parts.value = partMap;
      partsByMessage.value = pbmMap;
    } catch (e) {
      console.error('Failed to load messages:', e);
    }

    // Load todos
    try {
      const data = await api(`/sessions/${id}/todos`);
      todos.value = data.todos || [];
    } catch (e) {
      console.error('Failed to load todos:', e);
    }
  }

  async function createSession(worktree, title) {
    try {
      const session = await api('/sessions', {
        method: 'POST',
        body: JSON.stringify({ worktree, title: title || 'New Session' }),
      });
      sessions.value = [session, ...sessions.value];
      showNewSessionForm.value = false;
      selectSession(session.id);
    } catch (e) {
      alert('Failed to create session: ' + e.message);
    }
  }

  const layoutClass = [
    'app-layout',
    sidebarCollapsed.value ? 'sidebar-collapsed' : '',
    observabilityCollapsed.value ? 'observability-collapsed' : '',
  ].filter(Boolean).join(' ');

  return html`
    <div class=${layoutClass}>
      <${Header} />
      <${Sidebar}
        sessions=${sessions.value}
        activeId=${activeSessionId.value}
        onSelect=${selectSession}
        onCreate=${createSession}
      />
      <${MainContent}
        sessionId=${activeSessionId.value}
        messages=${messages.value}
        parts=${parts.value}
        partsByMessage=${partsByMessage.value}
        status=${sessionStatus.value}
        permissions=${pendingPermissions.value}
      />
      <${ObservabilityPanel}
        events=${events.value}
        todos=${todos.value}
        tab=${observabilityTab.value}
        onTabChange=${(t) => observabilityTab.value = t}
        onClearEvents=${() => events.value = []}
      />
    </div>
  `;
}

// Header
function Header() {
  const session = sessions.value.find(s => s.id === activeSessionId.value);

  return html`
    <header class="app-header">
      <div class="header-left">
        <div class="logo">
          <div class="logo-icon">CC</div>
          <span class="logo-text">Chord Code</span>
        </div>
      </div>
      <div class="header-center">
        ${session && html`
          <div class="session-info">
            <span class="title">${session.title}</span>
            <span class="badge">${session.worktree.split('/').pop()}</span>
          </div>
        `}
      </div>
      <div class="header-right">
        <div class="status-indicator ${sessionStatus.value}">
          <span class="dot"></span>
          ${sessionStatus.value}
        </div>
        <button class="icon-btn" onClick=${() => observabilityCollapsed.value = !observabilityCollapsed.value}>
          ${observabilityCollapsed.value ? '◀' : '▶'}
        </button>
      </div>
    </header>
  `;
}

// Sidebar
function Sidebar({ sessions, activeId, onSelect, onCreate }) {
  const [worktree, setWorktree] = useState('');
  const [title, setTitle] = useState('');

  useEffect(() => {
    if (defaultWorktree.value && !worktree) {
      setWorktree(defaultWorktree.value);
    }
  }, [defaultWorktree.value]);

  function handleCreate(e) {
    e.preventDefault();
    if (worktree.trim()) {
      onCreate(worktree.trim(), title.trim());
      setWorktree(defaultWorktree.value || '');
      setTitle('');
    }
  }

  return html`
    <aside class="sidebar">
      <div class="sidebar-header">
        <button class="new-session-btn" onClick=${() => showNewSessionForm.value = !showNewSessionForm.value}>
          ${showNewSessionForm.value ? '✕ Cancel' : '+ New Session'}
        </button>
      </div>

      ${showNewSessionForm.value && html`
        <form class="new-session-form" onSubmit=${handleCreate}>
          <div class="form-group">
            <label>Worktree Path</label>
            <input
              class="input"
              type="text"
              value=${worktree}
              onInput=${(e) => setWorktree(e.target.value)}
              placeholder="/path/to/project"
              required
            />
          </div>
          <div class="form-group">
            <label>Title (optional)</label>
            <input
              class="input"
              type="text"
              value=${title}
              onInput=${(e) => setTitle(e.target.value)}
              placeholder="My Session"
            />
          </div>
          <div class="form-actions">
            <button type="submit" class="btn" disabled=${!worktree.trim()}>Create</button>
          </div>
        </form>
      `}

      <div class="sidebar-content">
        <div class="sidebar-title">Sessions</div>
        <div class="session-list">
          ${sessions.length === 0 && html`
            <div class="session-empty">
              <div class="session-empty-icon">💬</div>
              <div class="session-empty-text">No sessions yet</div>
            </div>
          `}
          ${sessions.map(s => html`
            <div
              key=${s.id}
              class="session-item ${s.id === activeId ? 'active' : ''}"
              onClick=${() => onSelect(s.id)}
            >
              <div class="session-item-indicator"></div>
              <div class="session-item-content">
                <div class="session-item-title">${s.title}</div>
                <div class="session-item-meta">${formatRelative(s.updated_at)}</div>
              </div>
            </div>
          `)}
        </div>
      </div>
    </aside>
  `;
}

// Main Content (Chat)
function MainContent({ sessionId, messages, parts, partsByMessage, status, permissions }) {
  const [input, setInput] = useState('');
  const [autorun, setAutorun] = useState(true);
  const chatRef = useRef(null);

  // Get sorted messages
  const sortedMsgs = Array.from(messages.values())
    .filter(m => m.session_id === sessionId && m.role !== 'tool')
    .sort((a, b) => a.created_at - b.created_at);

  // Auto-scroll
  useEffect(() => {
    if (chatRef.current) {
      chatRef.current.scrollTop = chatRef.current.scrollHeight;
    }
  }, [parts, sortedMsgs.length]);

  async function handleSend(e) {
    e.preventDefault();
    if (!input.trim() || !sessionId) return;

    const text = input.trim();
    setInput('');

    try {
      await api(`/sessions/${sessionId}/messages`, {
        method: 'POST',
        body: JSON.stringify({ text }),
      });

      if (autorun) {
        await api(`/sessions/${sessionId}/run`, { method: 'POST' });
      }
    } catch (e) {
      alert('Failed to send: ' + e.message);
    }
  }

  async function handleRun() {
    if (!sessionId) return;
    try {
      await api(`/sessions/${sessionId}/run`, { method: 'POST' });
    } catch (e) {
      alert('Failed to run: ' + e.message);
    }
  }

  async function handleInterrupt() {
    if (!sessionId) return;
    try {
      await api(`/sessions/${sessionId}/interrupt`, { method: 'POST' });
    } catch (e) {
      alert('Failed to interrupt: ' + e.message);
    }
  }

  async function handlePermissionReply(reqId, reply) {
    try {
      await api(`/permissions/${reqId}/reply`, {
        method: 'POST',
        body: JSON.stringify({ reply }),
      });
    } catch (e) {
      alert('Failed to reply: ' + e.message);
    }
  }

  if (!sessionId) {
    return html`
      <main class="main-content">
        <div class="chat-empty">
          <div class="chat-empty-icon">🎵</div>
          <div class="chat-empty-title">Welcome to Chord Code</div>
          <div class="chat-empty-text">Create or select a session to start chatting</div>
        </div>
      </main>
    `;
  }

  return html`
    <main class="main-content">
      <div class="chat-container">
        <div class="chat-header">
          <div class="chat-header-left">
            <label class="toggle">
              <input
                type="checkbox"
                checked=${autorun}
                onChange=${(e) => setAutorun(e.target.checked)}
              />
              Auto-run
            </label>
          </div>
          <div class="chat-header-right">
            <button
              class="btn btn-sm"
              onClick=${handleRun}
              disabled=${status === 'busy'}
            >
              ▶ Run
            </button>
            <button
              class="btn btn-sm btn-danger"
              onClick=${handleInterrupt}
              disabled=${status !== 'busy'}
            >
              ⏹ Stop
            </button>
          </div>
        </div>

        <div class="chat-messages" ref=${chatRef}>
          ${sortedMsgs.map(msg => html`
            <${Message}
              key=${msg.id}
              message=${msg}
              parts=${parts}
              partsByMessage=${partsByMessage}
              permissions=${permissions}
              onPermissionReply=${handlePermissionReply}
            />
          `)}

          ${sortedMsgs.length === 0 && html`
            <div class="chat-empty">
              <div class="chat-empty-text">Send a message to start the conversation</div>
            </div>
          `}
        </div>

        <div class="chat-composer">
          <form class="composer-form" onSubmit=${handleSend}>
            <div class="composer-input-wrapper">
              <textarea
                class="composer-textarea"
                value=${input}
                onInput=${(e) => setInput(e.target.value)}
                onKeyDown=${(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleSend(e);
                  }
                }}
                placeholder="Type a message... (Enter to send, Shift+Enter for newline)"
                rows="1"
              />
            </div>
            <button type="submit" class="send-btn" disabled=${!input.trim()}>
              ➤
            </button>
          </form>
        </div>
      </div>
    </main>
  `;
}

// Message Component
function Message({ message, parts, partsByMessage, permissions, onPermissionReply }) {
  const partIds = partsByMessage.get(message.id) || new Set();
  const msgParts = Array.from(partIds)
    .map(id => parts.get(id))
    .filter(Boolean)
    .sort((a, b) => (a.created_at || 0) - (b.created_at || 0));

  const isUser = message.role === 'user';

  // Find permission cards for this message's tool parts
  const messagePermissions = permissions.filter(p =>
    p.tool && p.tool.message_id === message.id
  );

  return html`
    <div class="message ${message.role}">
      <div class="message-avatar">
        ${isUser ? '👤' : '🤖'}
      </div>
      <div class="message-content">
        <div class="message-header">
          <span class="message-role">${message.role}</span>
          <span class="message-time">${formatTime(message.created_at)}</span>
          ${message.finish && html`
            <span class="badge ${message.finish === 'blocked' ? 'badge-warning' : ''}">${message.finish}</span>
          `}
        </div>
        <div class="message-parts">
          ${msgParts.map(part => html`
            <${Part} key=${part.id} part=${part} />
          `)}
          ${messagePermissions.map(perm => html`
            <${PermissionCard}
              key=${perm.id}
              permission=${perm}
              onReply=${onPermissionReply}
            />
          `)}
        </div>
      </div>
    </div>
  `;
}

// Part Component
function Part({ part }) {
  if (part.type === 'text') {
    return html`
      <div class="message-bubble">
        <div
          class="message-text"
          dangerouslySetInnerHTML=${{ __html: renderMd(part.text) }}
        />
      </div>
    `;
  }

  if (part.type === 'tool') {
    return html`<${ToolCard} part=${part} />`;
  }

  if (part.type === 'reasoning') {
    return html`<${ReasoningPart} part=${part} />`;
  }

  // Fallback
  return html`
    <div class="message-bubble">
      <pre>${JSON.stringify(part, null, 2)}</pre>
    </div>
  `;
}

// Tool Card Component
function ToolCard({ part }) {
  const [expanded, setExpanded] = useState(false);
  const { state } = part;
  const status = state?.status || 'pending';

  const icon = {
    pending: '⏳',
    running: '🔄',
    completed: '✓',
    error: '✗',
  }[status] || '🔧';

  const duration = state?.time?.end && state?.time?.start
    ? formatDuration(state.time.end - state.time.start)
    : '';

  // Auto-collapse completed tools
  const shouldCollapse = status === 'completed' && !expanded;

  return html`
    <div class="tool-card ${status}">
      <div class="tool-card-header" onClick=${() => setExpanded(!expanded)}>
        <span class="tool-card-icon">${icon}</span>
        <span class="tool-card-title">${part.tool}</span>
        <div class="tool-card-meta">
          ${duration && html`<span class="tool-card-time">${duration}</span>`}
          ${status === 'running' && html`<span class="spinner"></span>`}
        </div>
        <span class="tool-card-chevron ${expanded ? 'expanded' : ''}">▶</span>
      </div>
      <div class="tool-card-body ${shouldCollapse ? 'collapsed' : ''}">
        ${state?.input && html`
          <div class="tool-section">
            <div class="tool-section-label">Input</div>
            <pre class="tool-section-content input">${typeof state.input === 'string' ? state.input : JSON.stringify(state.input, null, 2)}</pre>
          </div>
        `}
        ${status === 'completed' && state?.output && html`
          <div class="tool-section">
            <div class="tool-section-label">Output</div>
            <pre class="tool-section-content output">${state.output}</pre>
          </div>
        `}
        ${status === 'error' && state?.error && html`
          <div class="tool-section">
            <div class="tool-section-label">Error</div>
            <pre class="tool-section-content error">${state.error}</pre>
          </div>
        `}
      </div>
    </div>
  `;
}

// Reasoning Part
function ReasoningPart({ part }) {
  const [expanded, setExpanded] = useState(false);

  return html`
    <div class="reasoning-part">
      <div class="reasoning-header" onClick=${() => setExpanded(!expanded)}>
        <span class="reasoning-icon">🤔</span>
        <span class="reasoning-title">Thinking...</span>
        <span class="reasoning-chevron ${expanded ? 'expanded' : ''}">▶</span>
      </div>
      <div class="reasoning-content ${expanded ? '' : 'collapsed'}">
        ${part.text}
      </div>
    </div>
  `;
}

// Permission Card (inline in chat)
function PermissionCard({ permission, onReply }) {
  return html`
    <div class="permission-card">
      <div class="permission-card-header">
        <span class="permission-card-icon">🔐</span>
        <span class="permission-card-title">Permission Required</span>
      </div>
      <div class="permission-card-body">
        <div class="permission-card-desc">
          <strong>${permission.permission}</strong> wants to access:
        </div>
        <pre class="permission-card-content">${permission.patterns?.join('\n') || ''}</pre>
        ${permission.metadata && Object.keys(permission.metadata).length > 0 && html`
          <div class="permission-card-meta">
            ${Object.entries(permission.metadata).map(([k, v]) => `${k}: ${v}`).join(', ')}
          </div>
        `}
        <div class="permission-card-actions">
          <button class="btn btn-success btn-sm" onClick=${() => onReply(permission.id, 'once')}>
            Allow Once
          </button>
          <button class="btn btn-sm" onClick=${() => onReply(permission.id, 'always')}>
            Always Allow
          </button>
          <button class="btn btn-danger btn-sm" onClick=${() => onReply(permission.id, 'reject')}>
            Reject
          </button>
        </div>
      </div>
    </div>
  `;
}

// Observability Panel
function ObservabilityPanel({ events, todos, tab, onTabChange, onClearEvents }) {
  if (observabilityCollapsed.value) {
    return html`
      <aside class="observability-panel" style="width: 48px; min-width: 48px;">
        <div style="writing-mode: vertical-rl; text-align: center; padding: 12px; color: var(--muted); font-size: 12px;">
          Observability
        </div>
      </aside>
    `;
  }

  return html`
    <aside class="observability-panel">
      <div class="observability-header">
        <div class="tabs">
          <button
            class="tab ${tab === 'events' ? 'active' : ''}"
            onClick=${() => onTabChange('events')}
          >Events</button>
          <button
            class="tab ${tab === 'todos' ? 'active' : ''}"
            onClick=${() => onTabChange('todos')}
          >Todos</button>
        </div>
        ${tab === 'events' && html`
          <div class="panel-actions">
            <button class="icon-btn" onClick=${onClearEvents} title="Clear events">🗑</button>
          </div>
        `}
      </div>
      <div class="observability-content">
        ${tab === 'events' && html`<${EventsTab} events=${events} />`}
        ${tab === 'todos' && html`<${TodosTab} todos=${todos} />`}
      </div>
    </aside>
  `;
}

// Events Tab
function EventsTab({ events }) {
  if (events.length === 0) {
    return html`
      <div class="observability-empty">
        <div class="observability-empty-icon">📡</div>
        <div class="observability-empty-text">No events yet</div>
      </div>
    `;
  }

  function getEventClass(type) {
    if (type.startsWith('session.')) return 'session';
    if (type.startsWith('message.')) return 'message';
    if (type.startsWith('permission.')) return 'permission';
    if (type.startsWith('todo.')) return 'todo';
    if (type.includes('error')) return 'error';
    return '';
  }

  return html`
    <div class="panel-section">
      <div class="events-list">
        ${events.map((evt, i) => html`
          <div key=${i} class="event-item ${getEventClass(evt.type)}">
            <div class="event-header">
              <span class="event-type">${evt.type}</span>
              <span class="event-time">${formatTime(evt.time)}</span>
            </div>
          </div>
        `)}
      </div>
    </div>
  `;
}

// Todos Tab
function TodosTab({ todos }) {
  if (todos.length === 0) {
    return html`
      <div class="observability-empty">
        <div class="observability-empty-icon">📋</div>
        <div class="observability-empty-text">No todos</div>
      </div>
    `;
  }

  const completed = todos.filter(t => t.status === 'completed').length;
  const percent = Math.round((completed / todos.length) * 100);

  const statusIcon = {
    pending: '[ ]',
    in_progress: '[>]',
    completed: '[x]',
    cancelled: '[-]',
  };

  return html`
    <div class="todos-container">
      <div class="todos-header">
        <span class="todos-stats">${completed}/${todos.length}</span>
        <div class="todos-progress">
          <div class="todos-progress-fill" style="width: ${percent}%"></div>
        </div>
      </div>
      <div class="todos-list">
        ${todos.map(todo => html`
          <div
            key=${todo.id}
            class="todo-item ${todo.status === 'in_progress' ? 'in-progress' : ''} ${todo.status} ${todo.priority === 'high' ? 'priority-high' : ''}"
          >
            <span class="todo-icon">${statusIcon[todo.status] || '[ ]'}</span>
            <div class="todo-content">
              <div class="todo-text">${todo.content}</div>
              ${todo.status === 'in_progress' && todo.activeForm && html`
                <div class="todo-active-form">${todo.activeForm}</div>
              `}
            </div>
          </div>
        `)}
      </div>
    </div>
  `;
}

// ============ Mount App ============
render(html`<${App} />`, document.getElementById('app'));
