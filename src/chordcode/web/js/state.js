// Global state management using Preact signals
import { signal, computed, effect, batch } from 'https://esm.sh/@preact/signals@1.2.2';

// ============ Session State ============
export const sessions = signal([]);
export const activeSessionId = signal(null);
export const sessionsLoading = signal(false);

// Computed: active session object
export const activeSession = computed(() => {
  const id = activeSessionId.value;
  return sessions.value.find(s => s.id === id) || null;
});

// ============ Messages State ============
export const messages = signal(new Map()); // messageId -> Message
export const parts = signal(new Map());    // partId -> Part
export const partsByMessage = signal(new Map()); // messageId -> Set<partId>
export const messagesLoading = signal(false);

// Computed: sorted messages for current session
export const sortedMessages = computed(() => {
  const sessionId = activeSessionId.value;
  if (!sessionId) return [];

  const allMessages = Array.from(messages.value.values())
    .filter(m => m.session_id === sessionId && m.role !== 'tool')
    .sort((a, b) => a.created_at - b.created_at);

  return allMessages;
});

// ============ Real-time State ============
export const sessionStatus = signal('idle'); // idle | busy
export const sseConnected = signal(false);

// ============ Streaming State ============
export const streamingPartId = signal(null);
export const streamingText = signal('');

// ============ Todos State ============
export const todos = signal([]);

// Computed: todo progress
export const todoProgress = computed(() => {
  const list = todos.value;
  if (list.length === 0) return { completed: 0, total: 0, percent: 0 };
  const completed = list.filter(t => t.status === 'completed').length;
  return {
    completed,
    total: list.length,
    percent: Math.round((completed / list.length) * 100),
  };
});

// ============ Events State ============
export const events = signal([]);
export const maxEvents = 100;

// ============ Permissions State ============
export const pendingPermissions = signal([]);

// ============ UI State ============
export const sidebarCollapsed = signal(false);
export const observabilityCollapsed = signal(false);
export const observabilityTab = signal('events'); // events | todos | logs
export const showNewSessionForm = signal(false);

// ============ Actions ============

export const actions = {
  // Sessions
  setSessions(list) {
    sessions.value = list;
  },

  addSession(session) {
    sessions.value = [session, ...sessions.value];
  },

  setActiveSession(sessionId) {
    activeSessionId.value = sessionId;
    // Clear messages when switching sessions
    messages.value = new Map();
    parts.value = new Map();
    partsByMessage.value = new Map();
    todos.value = [];
    pendingPermissions.value = [];
    sessionStatus.value = 'idle';
  },

  // Messages
  setMessages(messageList, partList) {
    batch(() => {
      const msgMap = new Map();
      const partMap = new Map();
      const partByMsgMap = new Map();

      messageList.forEach(msgWithParts => {
        const { info, parts: msgParts } = msgWithParts;
        msgMap.set(info.id, info);

        const partIds = new Set();
        msgParts.forEach(p => {
          partMap.set(p.id, p);
          partIds.add(p.id);
        });
        partByMsgMap.set(info.id, partIds);
      });

      messages.value = msgMap;
      parts.value = partMap;
      partsByMessage.value = partByMsgMap;
    });
  },

  upsertMessage(msg) {
    const newMap = new Map(messages.value);
    newMap.set(msg.id, msg);
    messages.value = newMap;
  },

  upsertPart(part) {
    batch(() => {
      // Update part
      const newParts = new Map(parts.value);
      newParts.set(part.id, part);
      parts.value = newParts;

      // Update partsByMessage
      const newPartsByMsg = new Map(partsByMessage.value);
      const msgParts = newPartsByMsg.get(part.message_id) || new Set();
      msgParts.add(part.id);
      newPartsByMsg.set(part.message_id, msgParts);
      partsByMessage.value = newPartsByMsg;
    });
  },

  appendStreamingText(partId, delta) {
    if (streamingPartId.value !== partId) {
      streamingPartId.value = partId;
      streamingText.value = delta;
    } else {
      streamingText.value += delta;
    }
  },

  clearStreaming() {
    streamingPartId.value = null;
    streamingText.value = '';
  },

  // Status
  setSessionStatus(status) {
    sessionStatus.value = status;
    if (status === 'idle') {
      this.clearStreaming();
    }
  },

  setSSEConnected(connected) {
    sseConnected.value = connected;
  },

  // Todos
  setTodos(list) {
    todos.value = list;
  },

  // Events
  addEvent(event) {
    const newEvents = [event, ...events.value].slice(0, maxEvents);
    events.value = newEvents;
  },

  clearEvents() {
    events.value = [];
  },

  // Permissions
  setPendingPermissions(list) {
    pendingPermissions.value = list;
  },

  addPendingPermission(perm) {
    pendingPermissions.value = [...pendingPermissions.value, perm];
  },

  removePendingPermission(requestId) {
    pendingPermissions.value = pendingPermissions.value.filter(p => p.id !== requestId);
  },

  // UI
  toggleSidebar() {
    sidebarCollapsed.value = !sidebarCollapsed.value;
  },

  toggleObservability() {
    observabilityCollapsed.value = !observabilityCollapsed.value;
  },

  setObservabilityTab(tab) {
    observabilityTab.value = tab;
  },

  toggleNewSessionForm() {
    showNewSessionForm.value = !showNewSessionForm.value;
  },
};

// ============ Persistence ============

// Load persisted state from localStorage
export function loadPersistedState() {
  try {
    const collapsed = localStorage.getItem('chordcode.sidebarCollapsed');
    if (collapsed) sidebarCollapsed.value = collapsed === 'true';

    const obsCollapsed = localStorage.getItem('chordcode.observabilityCollapsed');
    if (obsCollapsed) observabilityCollapsed.value = obsCollapsed === 'true';

    const tab = localStorage.getItem('chordcode.observabilityTab');
    if (tab) observabilityTab.value = tab;

    const lastSession = localStorage.getItem('chordcode.lastSessionId');
    if (lastSession) activeSessionId.value = lastSession;
  } catch (e) {
    console.warn('Failed to load persisted state:', e);
  }
}

// Persist state changes
effect(() => {
  localStorage.setItem('chordcode.sidebarCollapsed', sidebarCollapsed.value);
});

effect(() => {
  localStorage.setItem('chordcode.observabilityCollapsed', observabilityCollapsed.value);
});

effect(() => {
  localStorage.setItem('chordcode.observabilityTab', observabilityTab.value);
});

effect(() => {
  if (activeSessionId.value) {
    localStorage.setItem('chordcode.lastSessionId', activeSessionId.value);
  }
});
