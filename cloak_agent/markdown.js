// Visible page → markdown, run in the isolated world. Headings start new chunks (split in Python),
// so a search result (heading link + snippet) or an article section stays together.
(() => {
  if (!document.body) return '';
  const SKIP='script,style,noscript,template,svg,canvas,iframe,nav,[role="navigation"],[aria-hidden="true"]';
  const BLOCK=/^(P|DIV|SECTION|ARTICLE|MAIN|ASIDE|HEADER|FOOTER|UL|OL|TABLE|TR|BLOCKQUOTE|PRE|FORM|FIGURE|DL|DT|DD)$/;
  const out=[]; let size=0;
  const push=s=>{ if (size<60000) { out.push(s); size+=s.length; } };
  const inline=e=>e.innerText.replace(/\s+/g,' ').trim();
  const walk=n=>{
    if (size>=60000) return;
    if (n.nodeType===3) { const t=n.textContent.replace(/\s+/g,' '); if (t.trim()) push(t); return; }
    if (n.nodeType!==1 || n.matches(SKIP)) return;
    if (!n.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return;
    const tag=n.tagName;
    if (/^H[1-6]$/.test(tag)) {
      const t=inline(n); if (!t) return;
      const a=n.closest('a[href]')||n.querySelector('a[href]');
      push('\n\n'+'#'.repeat(+tag[1])+' '+(a && /^https?:/.test(a.href) ? '['+t+']('+a.href+')' : t)+'\n\n');
      return;
    }
    if (tag==='A' && /^https?:/.test(n.href)) {
      const h=n.querySelector('h1,h2,h3,h4,h5,h6');
      if (h) { walk(h); return; }
      const t=inline(n); if (!t) return;
      // Links back to this same page (e.g. "#" anchors on a search URL) are noise, keep only the text.
      push(n.href.split('#')[0]===location.href.split('#')[0] ? t : '['+t+']('+n.href+')'); return;
    }
    if (tag==='BR') { push('\n'); return; }
    if (tag==='LI') push('\n- ');
    else if (BLOCK.test(tag)) push('\n');
    if (n.shadowRoot) for (const c of n.shadowRoot.childNodes) walk(c);
    for (const c of n.childNodes) walk(c);
    if (BLOCK.test(tag)) push('\n');
  };
  walk(document.body);
  return out.join('').replace(/[ \t]+\n/g,'\n').replace(/\n{3,}/g,'\n\n').trim();
})()
