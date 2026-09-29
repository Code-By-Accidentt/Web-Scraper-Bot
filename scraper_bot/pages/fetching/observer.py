"""The script that reports address-like text while a page is still loading.

It runs in every tab before the page's own scripts and watches the document as it is parsed
and hydrated. Each new text or attribute that may hold an address goes to Python at once,
so a token alert need not wait for the network to go quiet.
"""

from __future__ import annotations

EARLY_BINDING = "__textSeen"  # the page calls this to hand a piece of text to Python
EARLY_ITEMS = 400  # pieces one document may report (the rest waits for the full read)
EARLY_CHARS = 4000  # characters of one text or attribute value handed over
BEFORE_CHARS = 100  # characters of the text in front of a text node

# Only the top document reports. Frames count as page source, and only deep scan reads
# them. We skip the text of scripts and styles, since the full read finds addresses there.
OBSERVER_JS = f"""
(() => {{
  if (window.top !== window) return;
  const CANDIDATE = /0x[0-9a-fA-F]{{40}}|[1-9A-HJ-NP-Za-km-z]{{32,44}}/;
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE"]);
  const seen = new Set();
  let budget = {EARLY_ITEMS};
  const offer = (kind, text, before) => {{
    if (budget <= 0 || !text || text.length < 32 || !CANDIDATE.test(text)) return;
    const key = kind + "|" + before + "|" + text;
    if (seen.has(key)) return;
    seen.add(key);
    budget -= 1;
    try {{
      window.{EARLY_BINDING}(kind, text.slice(0, {EARLY_CHARS}), before.slice(-{BEFORE_CHARS}));
    }} catch (e) {{}}
  }};
  const textBefore = (node) => {{
    const walker = document.createTreeWalker(document, NodeFilter.SHOW_TEXT);
    walker.currentNode = node;
    for (let prev = walker.previousNode(); prev; prev = walker.previousNode()) {{
      const parent = prev.parentNode;
      const text = prev.data.trim();
      if (text && !(parent && SKIP.has(parent.nodeName))) return text;
    }}
    return "";
  }};
  const text = (node) => {{
    const parent = node.parentNode;
    if (!parent || SKIP.has(parent.nodeName) || !CANDIDATE.test(node.data)) return;
    offer("text", node.data, textBefore(node));
  }};
  const element = (el) => {{
    for (const attr of el.attributes) offer("attr", attr.value, attr.name);
  }};
  const scan = (root) => {{
    if (root.nodeType === 3) return text(root);
    if (root.nodeType !== 1 || SKIP.has(root.nodeName)) return;
    element(root);
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {{
      if (node.nodeType === 3) text(node);
      else if (!SKIP.has(node.nodeName)) element(node);
    }}
  }};
  new MutationObserver((records) => {{
    for (const record of records) {{
      if (record.type === "childList") record.addedNodes.forEach(scan);
      else if (record.type === "characterData") text(record.target);
      else if (record.target.nodeType === 1) {{
        const value = record.target.getAttribute(record.attributeName);
        if (value) offer("attr", value, record.attributeName);
      }}
    }}
  }}).observe(document, {{
    childList: true, subtree: true, characterData: true, attributes: true
  }});
}})();
"""
