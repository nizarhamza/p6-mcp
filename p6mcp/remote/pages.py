"""The browser pages of remote access: the connection page (/authorize/wait) and the error pages.

Self-contained HTML (no outside font, image or script: the CSP allows nothing else), in the brand colors, light and
dark. The connection page offers two ways to approve on the PC:

1. Connect (only in the browser that started the request, with JavaScript): the page posts its request id, code and
   browser key to the tray app on this PC's loopback address (p6mcp.remote.pairing), which fills the code in the
   approval window; the user clicks Approve there.
2. The code, with a Copy button, to type in the approval window.

With JavaScript the page asks /authorize/wait?check=1 every 2 s (no side effect) and goes back to the AI app once
the request is decided; without it a meta refresh reloads the page every 2 s, as in 3.2.
"""

from __future__ import annotations

import html
import json
from typing import Optional

MARK = ('<svg class="mark" viewBox="0 0 120 120" aria-hidden="true"><g transform="translate(6 0)">'
        '<rect class="mk-a" x="4" y="56" width="16" height="8" rx="4"/>'
        '<path class="mk-a" d="M36 28H48Q52 28 52 32V88Q52 92 48 92H36A20 20 0 0 1 16 72V48A20 20 0 0 1 36 28Z"/>'
        '<rect class="mk-b" x="58" y="42" width="46" height="12" rx="6"/>'
        '<rect class="mk-b" x="58" y="66" width="26" height="12" rx="6"/>'
        '<path class="mk-b" d="M96 64L104 72L96 80L88 72Z"/></g></svg>')

CSS = """
:root{--bg:#f3f6fb;--card:#fff;--ink:#0a1a33;--muted:#4a5b73;--line:#dbe3ef;--brand:#1f6feb;--brand-ink:#fff;
--brand-hover:#1a5fcc;--soft:#eef3fb;--code-bg:#f5f8fd;--ok:#1a7f37;--ok-bg:#e8f5ec;--warn:#9a3412;--warn-bg:#fdf0e8;
--mk-a:#0a1a33;--mk-b:#1f6feb;--focus:#1f6feb;color-scheme:light}
@media (prefers-color-scheme:dark){:root{--bg:#0b1322;--card:#121c2e;--ink:#e6edf7;--muted:#9fb3d1;--line:#24324a;
--brand:#58a6ff;--brand-ink:#0a1a33;--brand-hover:#79b8ff;--soft:#17243b;--code-bg:#0e1828;--ok:#56d364;
--ok-bg:#12261b;--warn:#ffa657;--warn-bg:#2b1d12;--mk-a:#fff;--mk-b:#58a6ff;--focus:#79b8ff;color-scheme:dark}}
*{box-sizing:border-box}
html,body{margin:0}
body{background:var(--bg);color:var(--ink);font:16px/1.5 "IBM Plex Sans","Segoe UI",system-ui,-apple-system,
Arial,sans-serif;min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 16px}
.brand{display:flex;align-items:center;gap:10px;margin-bottom:20px;color:var(--ink);font-weight:700;
letter-spacing:-.01em}
.brand .mark{width:34px;height:34px}.mk-a{fill:var(--mk-a)}.mk-b{fill:var(--mk-b)}
.brand small{font-weight:500;color:var(--brand);font-size:.8em;margin-left:2px}
.card{width:100%;max-width:480px;background:var(--card);border:1px solid var(--line);border-radius:16px;
padding:28px;box-shadow:0 1px 2px rgba(10,26,51,.04),0 8px 24px rgba(10,26,51,.06)}
h1{font-size:1.35rem;line-height:1.3;margin:0 0 6px}
.lead{margin:0 0 22px;color:var(--muted)}
.lead b{color:var(--ink);font-weight:600}
.btn{font:inherit;font-weight:600;border-radius:10px;cursor:pointer;border:1px solid transparent;
display:inline-flex;align-items:center;justify-content:center;gap:8px;transition:background .15s,border-color .15s}
.btn:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.btn-primary{width:100%;min-height:52px;padding:12px 18px;background:var(--brand);color:var(--brand-ink);
font-size:1.05rem}
.btn-primary:hover{background:var(--brand-hover)}
.btn-primary[disabled]{opacity:.65;cursor:default}
.btn-ghost{min-height:40px;padding:6px 14px;background:transparent;color:var(--brand);border-color:var(--line)}
.btn-ghost:hover{background:var(--soft)}
.btn svg{width:18px;height:18px;flex:none}
.hint{margin:10px 0 0;color:var(--muted);font-size:.9rem}
.status{margin:14px 0 0;padding:10px 12px;border-radius:10px;font-size:.95rem;display:none}
.status.ok{display:block;background:var(--ok-bg);color:var(--ok)}
.status.warn{display:block;background:var(--warn-bg);color:var(--warn)}
.status.busy{display:block;background:var(--soft);color:var(--muted)}
.or{display:flex;align-items:center;gap:12px;margin:24px 0 16px;color:var(--muted);font-size:.85rem}
.or::before,.or::after{content:"";flex:1;height:1px;background:var(--line)}
.step{margin:0 0 12px;font-size:.95rem}
.codebox{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;
background:var(--code-bg);border:1px solid var(--line);border-radius:12px;padding:12px 14px}
.code{font:700 2rem/1.1 "Cascadia Mono",Consolas,"IBM Plex Mono",ui-monospace,monospace;letter-spacing:.3em;
margin:0;padding-left:.1em;user-select:all;white-space:nowrap}
.foot{margin:22px 0 0;display:flex;align-items:center;gap:10px;color:var(--muted);font-size:.85rem}
.timer{flex:none;width:44px;height:6px;border-radius:3px;background:var(--line);overflow:hidden}
.timer i{display:block;height:100%;background:var(--brand);width:100%;transition:width 1s linear}
.js-only{display:none}.js .js-only{display:block}.js .js-flex{display:inline-flex}
.msg{margin:0;color:var(--ink)}
@media (max-width:420px){.card{padding:22px 18px}.code{font-size:1.7rem}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

COPY_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true"><rect x="9" y="9" width="12" height="12" rx="2"/>'
             '<path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>')
LINK_ICON = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true"><path d="M13 2 4 14h7l-1 8 9-12h-7z"/></svg>')

SCRIPT = """
(function(){
var d=document.documentElement;d.classList.add('js');
var m=document.getElementById('cfg');if(!m)return;var c=JSON.parse(m.textContent);
var left=c.left,total=c.total,t=document.getElementById('left'),bar=document.getElementById('bar');
function fmt(s){s=Math.max(0,s);var mm=Math.floor(s/60),ss=s%60;return mm+':'+(ss<10?'0':'')+ss;}
var gone=false,tick,poll;
function back(){if(gone)return;gone=true;clearInterval(tick);clearInterval(poll);location.replace(c.wait);}
tick=setInterval(function(){left-=1;if(t)t.textContent=fmt(left);
if(bar)bar.style.width=Math.max(0,left/total*100)+'%';if(left<=-2)back();},1000);
poll=setInterval(function(){if(gone)return;fetch(c.check,{cache:'no-store',credentials:'same-origin'}).then(function(r){
return r.ok?r.json():{status:'done'};}).then(function(j){if(j.status!=='pending')back();}).catch(function(){});},2000);
var cp=document.getElementById('copy');
if(cp)cp.addEventListener('click',function(){var lbl=cp.querySelector('span');
function done(){lbl.textContent=c.t.copied;setTimeout(function(){lbl.textContent=c.t.copy;},2000);}
if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(c.code).then(done,function(){});}
else{var r=document.createRange();r.selectNodeContents(document.getElementById('code'));
var s=getSelection();s.removeAllRanges();s.addRange(r);}});
var b=document.getElementById('connect'),st=document.getElementById('status');
function say(k,txt){st.className='status '+k;st.textContent=txt;}
if(b)b.addEventListener('click',function(){b.disabled=true;say('busy',c.t.busy);
var ctl=window.AbortController?new AbortController():null;var to=setTimeout(function(){if(ctl)ctl.abort();},60000);
fetch(c.local,{method:'POST',mode:'cors',credentials:'omit',cache:'no-store',
headers:{'content-type':'text/plain'},body:JSON.stringify({id:c.id,code:c.code,key:c.key}),
signal:ctl?ctl.signal:undefined}).then(function(r){return r.json().catch(function(){return {};}).then(function(j){
return r.ok&&j.ok;});}).then(function(ok){clearTimeout(to);if(ok){say('ok',c.t.done);}
else{b.disabled=false;say('warn',c.t.failed);}}).catch(function(){clearTimeout(to);b.disabled=false;
say('warn',c.t.failed);});});
})();
"""


def csp(nonce: Optional[str] = None, local: Optional[str] = None) -> str:
    """The page's Content-Security-Policy: inline styles, the nonce script, fetches to itself and the loopback
    listener only; no frame, no form, no base."""
    script = f"script-src 'nonce-{nonce}'; " if nonce else ""
    connect = "connect-src 'self'" + (f" {local}" if local else "") + "; " if nonce else ""
    return (f"default-src 'none'; style-src 'unsafe-inline'; {script}{connect}frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")


def page(lang: str, title: str, body: str, refresh: Optional[str] = None, nonce: Optional[str] = None,
         script_data: Optional[dict] = None) -> str:
    """A whole page. refresh: URL reloaded every 2 s without JavaScript; script_data + nonce: the page script."""
    meta = (f'<noscript><meta http-equiv="refresh" content="2;url={html.escape(refresh)}"></noscript>'
            if refresh else "")
    script = ""
    if nonce and script_data is not None:
        data = json.dumps(script_data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
        script = (f'<script type="application/json" id="cfg">{data}</script>'
                  f'<script nonce="{html.escape(nonce)}">{SCRIPT}</script>')
    return (f'<!doctype html><html lang="{html.escape(lang)}"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta name="referrer" content="no-referrer">{meta}<title>{html.escape(title)}</title>'
            f'<style>{CSS}</style></head><body>'
            f'<div class="brand">{MARK}<span>MCP <small>for Primavera P6</small></span></div>'
            f'<main class="card">{body}</main>{script}</body></html>')


def message_body(title: str, text: str) -> str:
    return f'<h1>{html.escape(title)}</h1><p class="msg">{html.escape(text)}</p>'


def wait_body(tr, client: str, host: str, code: str, left: int, total: int, connect: bool) -> str:
    """The connection page's card. tr(key, **kw) -> text; connect: offer the Connect button (starting browser)."""
    e = html.escape
    parts = [f"<h1>{e(tr('remote.page.heading', client=client))}</h1>",
             f'<p class="lead">{e(tr("remote.page.asks", client=client, host=host))}</p>']
    if connect:
        parts += [
            '<div class="js-only">',
            f'<button type="button" id="connect" class="btn btn-primary">{LINK_ICON}'
            f'<span>{e(tr("remote.page.connect"))}</span></button>',
            f'<p class="hint">{e(tr("remote.page.connect_hint"))}</p>',
            '<div id="status" class="status" role="status" aria-live="polite"></div>',
            f'<div class="or">{e(tr("remote.page.or"))}</div>',
            '</div>',
        ]
    parts += [
        f'<p class="step">{e(tr("remote.page.approve_on_pc"))}</p>',
        '<div class="codebox">',
        f'<p class="code" id="code" aria-label="{e(" ".join(code))}">{e(code)}</p>',
        f'<button type="button" id="copy" class="btn btn-ghost js-only js-flex">{COPY_ICON}'
        f'<span>{e(tr("remote.page.copy"))}</span></button>',
        '</div>',
    ]
    mm, ss = divmod(max(0, int(left)), 60)
    waiting = e(tr("remote.page.waiting", left="\x00")).replace(
        "\x00", f'<span id="left">{mm}:{ss:02d}</span>')
    pct = round(100 * max(0, int(left)) / max(1, int(total)), 1)
    parts.append(f'<p class="foot"><span class="timer js-only js-flex"><i id="bar" style="width:{pct}%"></i></span>'
                 f'<span>{waiting}</span></p>')
    return "".join(parts)


__all__ = ["csp", "message_body", "page", "wait_body"]
