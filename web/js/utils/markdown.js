// Markdown rendering utilities
// Uses marked.js for parsing and highlight.js for syntax highlighting

// Configure marked options
export function configureMarked(marked, hljs) {
  marked.setOptions({
    highlight: function(code, lang) {
      if (lang && hljs.getLanguage(lang)) {
        try {
          return hljs.highlight(code, { language: lang }).value;
        } catch (e) {
          console.warn('Highlight error:', e);
        }
      }
      return hljs.highlightAuto(code).value;
    },
    breaks: true,
    gfm: true,
  });
}

/**
 * Render markdown to HTML
 */
export function renderMarkdown(text, marked) {
  if (!text) return '';
  try {
    return marked.parse(text);
  } catch (e) {
    console.warn('Markdown parse error:', e);
    return escapeHtml(text);
  }
}

/**
 * Escape HTML special characters
 */
export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

/**
 * Simple code block renderer (without full markdown)
 */
export function renderCodeBlock(code, language = '') {
  return `<pre class="code-block"><code class="language-${language}">${escapeHtml(code)}</code></pre>`;
}
