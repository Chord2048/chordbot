// SSE (Server-Sent Events) connection manager

/**
 * SSE Manager - handles connection, reconnection, and event dispatch
 */
export class SSEManager {
  constructor(sessionId, options = {}) {
    this.sessionId = sessionId;
    this.eventSource = null;
    this.listeners = new Map();
    this.reconnectAttempts = 0;
    this.maxReconnectAttempts = options.maxReconnectAttempts || 10;
    this.reconnectDelay = options.reconnectDelay || 1000;
    this.connected = false;
    this.onConnectionChange = options.onConnectionChange || (() => {});
  }

  /**
   * Connect to SSE endpoint
   */
  connect() {
    if (this.eventSource) {
      this.disconnect();
    }

    const url = `/events?session_id=${encodeURIComponent(this.sessionId)}`;
    this.eventSource = new EventSource(url);

    this.eventSource.onopen = () => {
      console.log('[SSE] Connected');
      this.connected = true;
      this.reconnectAttempts = 0;
      this.onConnectionChange(true);
    };

    this.eventSource.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        this.dispatch(data);
      } catch (err) {
        console.error('[SSE] Failed to parse event:', err);
      }
    };

    this.eventSource.onerror = (error) => {
      console.error('[SSE] Connection error:', error);
      this.connected = false;
      this.onConnectionChange(false);
      this.eventSource.close();

      // Attempt reconnection
      if (this.reconnectAttempts < this.maxReconnectAttempts) {
        this.reconnectAttempts++;
        const delay = this.reconnectDelay * Math.pow(1.5, this.reconnectAttempts - 1);
        console.log(`[SSE] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
        setTimeout(() => this.connect(), delay);
      } else {
        console.error('[SSE] Max reconnection attempts reached');
      }
    };
  }

  /**
   * Disconnect from SSE
   */
  disconnect() {
    if (this.eventSource) {
      this.eventSource.close();
      this.eventSource = null;
      this.connected = false;
      this.onConnectionChange(false);
    }
  }

  /**
   * Subscribe to a specific event type
   * @param {string} eventType - Event type to subscribe to (e.g., 'message.part.updated')
   * @param {Function} callback - Callback function
   * @returns {Function} Unsubscribe function
   */
  on(eventType, callback) {
    if (!this.listeners.has(eventType)) {
      this.listeners.set(eventType, new Set());
    }
    this.listeners.get(eventType).add(callback);

    // Return unsubscribe function
    return () => {
      const callbacks = this.listeners.get(eventType);
      if (callbacks) {
        callbacks.delete(callback);
      }
    };
  }

  /**
   * Subscribe to all events
   * @param {Function} callback - Callback function
   * @returns {Function} Unsubscribe function
   */
  onAll(callback) {
    return this.on('*', callback);
  }

  /**
   * Dispatch event to listeners
   */
  dispatch(event) {
    const { type, properties } = event;

    // Dispatch to specific listeners
    const callbacks = this.listeners.get(type);
    if (callbacks) {
      callbacks.forEach((cb) => {
        try {
          cb(properties, event);
        } catch (err) {
          console.error(`[SSE] Error in listener for ${type}:`, err);
        }
      });
    }

    // Dispatch to wildcard listeners
    const wildcardCallbacks = this.listeners.get('*');
    if (wildcardCallbacks) {
      wildcardCallbacks.forEach((cb) => {
        try {
          cb(event);
        } catch (err) {
          console.error('[SSE] Error in wildcard listener:', err);
        }
      });
    }
  }

  /**
   * Check if connected
   */
  isConnected() {
    return this.connected && this.eventSource?.readyState === EventSource.OPEN;
  }
}

/**
 * Create an SSE manager for a session
 */
export function createSSE(sessionId, options = {}) {
  const manager = new SSEManager(sessionId, options);
  return manager;
}
