function renderJson(value) {
  const escape = text => String(text).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function node(item, label = null, comma = '', expanded = false) {
    const prefix = label !== null ? `<span class="json-key">${escape(JSON.stringify(label))}</span>: ` : '';
    if (item !== null && typeof item === 'object') {
      const array = Array.isArray(item), entries = Object.entries(item);
      const [open, close] = array ? ['[', ']'] : ['{', '}'];
      if (!entries.length) return `<div class="json-line">${prefix}${open}${close}${comma}</div>`;
      const children = entries.map(([key, child], i) => node(child, array ? null : key, i < entries.length - 1 ? ',' : '')).join('');
      const body = `<div class="json-children">${children}</div><div>${close}${comma}</div>`;
      if (expanded) return `<div>${open}</div>${body}`;
      return `<details class="json-node"><summary>${prefix}${open}<span class="json-preview"> ${entries.length} ${array ? 'items' : 'keys'} ${close}${comma}</span></summary>${body}</details>`;
    }
    const text = JSON.stringify(item);
    const content = `<span class="json-${typeof item === 'string' ? 'string' : 'literal'}">${escape(text)}</span>`;
    if (typeof item === 'string' && item.length > 120) {
      return `<details class="json-node json-text"><summary>${prefix}<span class="json-preview">${escape(JSON.stringify(item.slice(0, 80)))}…${comma}</span></summary><div class="json-children">${content}${comma}</div></details>`;
    }
    return `<div class="json-line">${prefix}${content}${comma}</div>`;
  }
  return node(value, null, '', true);
}
