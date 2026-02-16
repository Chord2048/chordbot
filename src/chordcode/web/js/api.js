// API client for Chord Code backend

const BASE_URL = '';

/**
 * Fetch wrapper with error handling
 */
async function request(url, options = {}) {
  const response = await fetch(BASE_URL + url, {
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
    ...options,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(error.detail || 'Request failed');
  }

  return response.json();
}

export const api = {
  // Config
  async getConfig() {
    return request('/config');
  },

  // Sessions
  async listSessions(limit = 50, offset = 0) {
    return request(`/sessions?limit=${limit}&offset=${offset}`);
  },

  async getSession(sessionId) {
    return request(`/sessions/${sessionId}`);
  },

  async createSession({ worktree, title, cwd, permissionRules }) {
    return request('/sessions', {
      method: 'POST',
      body: JSON.stringify({
        worktree,
        title,
        cwd,
        permission_rules: permissionRules,
      }),
    });
  },

  // Messages
  async listMessages(sessionId) {
    return request(`/sessions/${sessionId}/messages`);
  },

  async addMessage(sessionId, text) {
    return request(`/sessions/${sessionId}/messages`, {
      method: 'POST',
      body: JSON.stringify({ text }),
    });
  },

  // Run / Interrupt
  async run(sessionId) {
    return request(`/sessions/${sessionId}/run`, {
      method: 'POST',
    });
  },

  async interrupt(sessionId) {
    return request(`/sessions/${sessionId}/interrupt`, {
      method: 'POST',
    });
  },

  // Todos
  async getTodos(sessionId) {
    return request(`/sessions/${sessionId}/todos`);
  },

  // Permissions
  async getPendingPermissions(sessionId) {
    return request(`/permissions/pending?session_id=${sessionId}`);
  },

  async replyPermission(requestId, reply, message) {
    return request(`/permissions/${requestId}/reply`, {
      method: 'POST',
      body: JSON.stringify({ reply, message }),
    });
  },
};
