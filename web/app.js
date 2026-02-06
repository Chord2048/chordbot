const $ = (id) => document.getElementById(id)

const state = {
  sessionId: null,
  sse: null,
  messages: new Map(), // Message info
  parts: new Map(), // Parts keyed by part.id
  partsByMessage: new Map(), // Map<messageId, Set<partId>>
  seq: 0,
  autorun: true,
  status: "idle", // idle | busy
  todos: [], // Todo list
}

const meta = $("meta")
const chat = $("chat")
const events = $("events")
const perms = $("perms")
const todos = $("todos")
const worktreeInput = $("worktree")
const autorunInput = $("autorun")

function setMeta() {
  meta.textContent = state.sessionId ? `session: ${state.sessionId}` : "no session"
}

function appendEvent(obj) {
  const line = JSON.stringify(obj)
  events.textContent += (events.textContent ? "\n" : "") + line
  events.scrollTop = events.scrollHeight
}

function upsertMessage(info) {
  const existing = state.messages.get(info.id)
  const msg = existing || { info, seq: ++state.seq }
  msg.info = info
  state.messages.set(info.id, msg)
  return msg
}

function upsertPart(part) {
  const existing = state.parts.get(part.id)
  const p = existing || { part, seq: existing?.seq || ++state.seq }
  p.part = part
  state.parts.set(part.id, p)

  // Track parts by message
  if (!state.partsByMessage.has(part.message_id)) {
    state.partsByMessage.set(part.message_id, new Set())
  }
  state.partsByMessage.get(part.message_id).add(part.id)

  return p
}

function renderChat() {
  // Collect all messages and their parts
  const items = []

  for (const msg of state.messages.values()) {
    // Skip tool messages (they are internal, tool results are shown in assistant's tool part)
    if (msg.info.role === "tool") {
      continue
    }

    // Add message header item
    items.push({
      kind: "message-header",
      seq: msg.seq,
      message: msg.info,
    })

    // Add all parts for this message
    const partIds = state.partsByMessage.get(msg.info.id) || new Set()
    for (const partId of partIds) {
      const partWrapper = state.parts.get(partId)
      if (partWrapper) {
        items.push({
          kind: "part",
          seq: partWrapper.seq,
          part: partWrapper.part,
          message: msg.info,
        })
      }
    }
  }

  // Sort by sequence
  items.sort((a, b) => a.seq - b.seq)

  chat.innerHTML = ""
  for (const item of items) {
    if (item.kind === "message-header") {
      const el = renderMessageHeader(item.message)
      chat.appendChild(el)
    } else if (item.kind === "part") {
      const elements = renderPart(item.part, item.message)
      if (elements) {
        // renderPart returns either single element or array of elements
        if (Array.isArray(elements)) {
          for (const el of elements) {
            chat.appendChild(el)
          }
        } else {
          chat.appendChild(elements)
        }
      }
    }
  }

  chat.scrollTop = chat.scrollHeight
}

function renderMessageHeader(message) {
  const el = document.createElement("div")
  el.className = "msg-header"

  const role = message.role
  const title = role === "tool" ? `tool (${message.tool_name || ""})` : role
  const status = message.finish ? `finish=${message.finish}` : ""
  const meta = [message.id.slice(0, 8)]
  if (status) meta.push(status)

  el.innerHTML = `
    <span class="role">${title}</span>
    <span class="tag">${meta.join(" • ")}</span>
  `

  return el
}

function renderPart(part, message) {
  if (part.type === "text") {
    const el = document.createElement("div")
    el.className = "part"

    const hdr = document.createElement("div")
    hdr.className = "part-hdr"
    hdr.innerHTML = `<span class="part-type">💬 Text</span><span class="part-tag">${part.id.slice(0, 8)}</span>`

    const content = document.createElement("div")
    content.className = "part-content"
    content.textContent = part.text.trim()

    el.appendChild(hdr)
    el.appendChild(content)
    return el
  } else if (part.type === "tool") {
    const state = part.state || {}
    const status = state.status || "unknown"

    // For completed tools, render as two separate blocks: call + result
    if (status === "completed") {
      // Tool Call block
      const callEl = document.createElement("div")
      callEl.className = "part tool-call"

      const callHdr = document.createElement("div")
      callHdr.className = "part-hdr"
      callHdr.innerHTML = `<span class="part-type">🔧 ${part.tool} (call)</span><span class="part-tag">${part.id.slice(0, 8)}</span>`

      const callContent = document.createElement("div")
      callContent.className = "part-content"
      callContent.textContent = state.input && Object.keys(state.input).length > 0 ? JSON.stringify(state.input, null, 2) : "(no input)"

      callEl.appendChild(callHdr)
      callEl.appendChild(callContent)

      // Tool Result block
      const resultEl = document.createElement("div")
      resultEl.className = "part tool-result"
      resultEl.classList.add(state.metadata?.error ? "tool-error" : "tool-completed")

      const resultHdr = document.createElement("div")
      resultHdr.className = "part-hdr"
      resultHdr.innerHTML = `<span class="part-type">📦 ${state.title || part.tool} (result)</span><span class="part-tag">${part.id.slice(0, 8)}</span>`

      const resultContent = document.createElement("div")
      resultContent.className = "part-content"
      resultContent.textContent = state.output || "(no output)"

      resultEl.appendChild(resultHdr)
      resultEl.appendChild(resultContent)

      return [callEl, resultEl]
    } else {
      // For pending/running/error, show single block
      const el = document.createElement("div")
      el.className = `part tool-${status}`

      const hdr = document.createElement("div")
      hdr.className = "part-hdr"
      hdr.innerHTML = `<span class="part-type">🔧 ${part.tool}</span><span class="part-tag">${part.id.slice(0, 8)} • ${status}</span>`

      const content = document.createElement("div")
      content.className = "part-content"

      const pieces = []
      if (state.input && Object.keys(state.input).length > 0) {
        pieces.push(`[Input]\n${JSON.stringify(state.input, null, 2)}`)
      }
      if (state.status === "error" && state.error) {
        pieces.push(`[Error]\n${state.error}`)
      }
      content.textContent = pieces.join("\n\n") || "(no content)"

      el.appendChild(hdr)
      el.appendChild(content)
      return el
    }
  } else if (part.type === "reasoning") {
    const el = document.createElement("div")
    el.className = "part"

    const hdr = document.createElement("div")
    hdr.className = "part-hdr"
    hdr.innerHTML = `<span class="part-type">🤔 Reasoning</span><span class="part-tag">${part.id.slice(0, 8)}</span>`

    const content = document.createElement("div")
    content.className = "part-content"
    content.textContent = part.text.trim()

    el.appendChild(hdr)
    el.appendChild(content)
    return el
  } else {
    const el = document.createElement("div")
    el.className = "part"

    const hdr = document.createElement("div")
    hdr.className = "part-hdr"
    hdr.innerHTML = `<span class="part-type">${part.type}</span><span class="part-tag">${part.id.slice(0, 8)}</span>`

    const content = document.createElement("div")
    content.className = "part-content"
    content.textContent = JSON.stringify(part, null, 2)

    el.appendChild(hdr)
    el.appendChild(content)
    return el
  }
}

async function api(path, init) {
  const res = await fetch(path, {
    headers: { "content-type": "application/json" },
    ...init,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(`${res.status} ${res.statusText}: ${text}`)
  }
  const ct = res.headers.get("content-type") || ""
  if (ct.includes("application/json")) return res.json()
  return res.text()
}

async function refreshPending() {
  if (!state.sessionId) return
  const items = await api(`/permissions/pending?session_id=${encodeURIComponent(state.sessionId)}`)
  renderPerms(items)
}

function renderPerms(items) {
  perms.innerHTML = ""
  for (const p of items) {
    const el = document.createElement("div")
    el.className = "perm"
    el.innerHTML = `
      <div class="title">${p.permission} (${p.id})</div>
      <div class="body">${JSON.stringify({ patterns: p.patterns, metadata: p.metadata, always: p.always }, null, 2)}</div>
      <div class="actions">
        <button class="btn" data-action="once" data-id="${p.id}">Once</button>
        <button class="btn secondary" data-action="always" data-id="${p.id}">Always</button>
        <button class="btn danger" data-action="reject" data-id="${p.id}">Reject</button>
      </div>
    `
    el.querySelectorAll("button").forEach((b) =>
      b.addEventListener("click", async () => {
        const id = b.getAttribute("data-id")
        const action = b.getAttribute("data-action")
        await api(`/permissions/${encodeURIComponent(id)}/reply`, { method: "POST", body: JSON.stringify({ reply: action }) })
        await refreshPending()
      }),
    )
    perms.appendChild(el)
  }
}

// Todo functions
function escapeHtml(text) {
  const div = document.createElement("div")
  div.textContent = text
  return div.innerHTML
}

function renderTodos() {
  if (!todos) return

  if (state.todos.length === 0) {
    todos.innerHTML = '<div class="empty">No todos</div>'
    return
  }

  const stats = {
    total: state.todos.length,
    completed: state.todos.filter((t) => t.status === "completed").length,
    inProgress: state.todos.filter((t) => t.status === "in_progress").length,
  }

  const progressPct = stats.total > 0 ? Math.round((stats.completed / stats.total) * 100) : 0

  let html = `
    <div class="todo-header">
      <div class="todo-stats">${stats.completed}/${stats.total} completed</div>
      <div class="todo-progress-bar">
        <div class="todo-progress-fill" style="width: ${progressPct}%"></div>
      </div>
    </div>
    <div class="todo-list">
  `

  for (const todo of state.todos) {
    const statusIcon = {
      pending: "[ ]",
      in_progress: "[>]",
      completed: "[x]",
      cancelled: "[-]",
    }[todo.status] || "[ ]"

    const priorityClass = `priority-${todo.priority || "medium"}`
    const statusClass = `status-${todo.status}`

    html += `
      <div class="todo-item ${statusClass} ${priorityClass}">
        <span class="todo-icon">${statusIcon}</span>
        <span class="todo-content">${escapeHtml(todo.content)}</span>
        ${todo.status === "in_progress" ? `<span class="todo-active">${escapeHtml(todo.activeForm)}</span>` : ""}
      </div>
    `
  }

  html += "</div>"
  todos.innerHTML = html
}

async function refreshTodos() {
  if (!state.sessionId) return
  try {
    const data = await api(`/sessions/${encodeURIComponent(state.sessionId)}/todos`)
    state.todos = data.todos || []
    renderTodos()
  } catch (e) {
    console.error("Failed to refresh todos:", e)
  }
}

function onEvent(envelope) {
  appendEvent(envelope)
  const type = envelope.type
  const props = envelope.properties || {}

  if (type === "message.updated") {
    const info = props.info
    if (info) {
      upsertMessage(info)
      renderChat()
    }
    return
  }

  if (type === "message.part.updated") {
    const part = props.part
    if (part && part.id) {
      upsertPart(part)
      renderChat()
    }
    return
  }

  if (type === "session.status") {
    const status = props.status
    if (status) {
      state.status = status
      $("interrupt").disabled = status !== "busy"
      if (status === "busy") {
        $("run").disabled = true
        $("send").disabled = true
      } else {
        $("run").disabled = !state.sessionId
        $("send").disabled = !state.sessionId
      }
    }
    return
  }

  if (type === "permission.asked") {
    void refreshPending()
  }

  if (type === "todo.updated") {
    const todoList = props.todos
    if (Array.isArray(todoList)) {
      state.todos = todoList
      renderTodos()
    }
    return
  }

  if (type === "session.error") {
    const error = props.error
    if (error) {
      console.error("Session error:", error)
    }
  }
}

function connectSSE() {
  if (!state.sessionId) return
  if (state.sse) state.sse.close()

  const url = `/events?session_id=${encodeURIComponent(state.sessionId)}`
  const es = new EventSource(url)
  state.sse = es

  es.onmessage = (evt) => {
    try {
      onEvent(JSON.parse(evt.data))
    } catch (e) {
      appendEvent({ type: "client.parse_error", properties: { error: String(e), data: evt.data } })
    }
  }

  es.onerror = () => {
    appendEvent({ type: "client.sse_error", properties: {} })
  }
}

$("clear").addEventListener("click", () => {
  events.textContent = ""
})

$("create").addEventListener("click", async () => {
  const worktree = worktreeInput.value.trim()
  if (!worktree) return
  const s = await api("/sessions", { method: "POST", body: JSON.stringify({ worktree, title: "UI session" }) })
  state.sessionId = s.id
  setMeta()
  localStorage.setItem("chordcode.worktree", worktree)
  $("send").disabled = false
  $("run").disabled = false
  $("connect").disabled = false
  $("refreshPerm").disabled = false
  $("refreshTodos").disabled = false
  appendEvent({ type: "client.session_created", properties: { session_id: s.id } })
  // Initialize todos for new session
  state.todos = []
  renderTodos()
})

$("connect").addEventListener("click", () => {
  connectSSE()
  refreshTodos()
})

$("send").addEventListener("click", async () => {
  if (!state.sessionId) return
  const text = $("input").value
  if (!text.trim()) return
  $("input").value = ""
  await api(`/sessions/${encodeURIComponent(state.sessionId)}/messages`, {
    method: "POST",
    body: JSON.stringify({ text }),
  })
  if (state.autorun) {
    await api(`/sessions/${encodeURIComponent(state.sessionId)}/run`, { method: "POST", body: JSON.stringify({}) })
  }
})

$("run").addEventListener("click", async () => {
  if (!state.sessionId) return
  await api(`/sessions/${encodeURIComponent(state.sessionId)}/run`, { method: "POST", body: JSON.stringify({}) })
})

$("refreshPerm").addEventListener("click", async () => {
  await refreshPending()
})

$("refreshTodos").addEventListener("click", async () => {
  await refreshTodos()
})

$("interrupt").addEventListener("click", async () => {
  if (!state.sessionId) return
  await api(`/sessions/${encodeURIComponent(state.sessionId)}/interrupt`, { method: "POST", body: JSON.stringify({}) })
  appendEvent({ type: "client.interrupt_requested", properties: { session_id: state.sessionId } })
})

setMeta()

async function initWorktree() {
  const saved = localStorage.getItem("chordcode.worktree")
  if (saved) {
    worktreeInput.value = saved
    return
  }
  try {
    const cfg = await api("/config", {})
    if (cfg && cfg.default_worktree) {
      worktreeInput.value = cfg.default_worktree
    }
  } catch {
    // ignore
  }
}

void initWorktree()

function initAutorun() {
  const saved = localStorage.getItem("chordcode.autorun")
  if (saved === "0") state.autorun = false
  autorunInput.checked = state.autorun
  autorunInput.addEventListener("change", () => {
    state.autorun = autorunInput.checked
    localStorage.setItem("chordcode.autorun", state.autorun ? "1" : "0")
  })
}

initAutorun()
