#!/usr/bin/env python3
"""
INESC TEC Letter Writer
Standalone web UI (localhost:8767) — paste a job, get a motivation letter.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from letter_generator import (
    DEFAULT_MODEL, generate_letter_stream, list_models,
    load_profile, ollama_available,
)

PROFILE_FILE = os.path.join(os.path.dirname(__file__), "profile.json")
PORT = 8767

_sse_clients: list = []
_sse_lock = threading.Lock()


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

def _render_html() -> str:
    profile = load_profile()
    profile_json = json.dumps(profile, indent=2, ensure_ascii=False)
    models = list_models()
    model_options = "".join(
        f'<option value="{m}" {"selected" if m == DEFAULT_MODEL else ""}>{m}</option>'
        for m in (models or [DEFAULT_MODEL])
    )
    ollama_ok = ollama_available()
    ollama_badge = (
        '<span class="badge badge--green">● Ollama running</span>'
        if ollama_ok else
        '<span class="badge badge--red">● Ollama offline</span>'
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Letter Writer — INESC TEC</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
  *, *::before, *::after {{box-sizing:border-box;margin:0;padding:0}}

  :root {{
    --indigo-900: #1e1b4b;
    --indigo-700: #3730a3;
    --indigo-600: #4f46e5;
    --indigo-500: #6366f1;
    --indigo-100: #e0e7ff;
    --indigo-50:  #eef2ff;
    --green-600:  #16a34a;
    --green-100:  #dcfce7;
    --gray-900:   #111827;
    --gray-700:   #374151;
    --gray-600:   #4b5563;
    --gray-500:   #6b7280;
    --gray-400:   #9ca3af;
    --gray-200:   #e5e7eb;
    --gray-100:   #f3f4f6;
    --gray-50:    #f9fafb;
    --white:      #ffffff;
    --shadow-sm:  0 1px 2px rgba(0,0,0,.05);
    --shadow:     0 1px 3px rgba(0,0,0,.08), 0 1px 2px rgba(0,0,0,.05);
    --radius:     10px;
    --radius-sm:  6px;
  }}

  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
    background: var(--gray-50);
    color: var(--gray-900);
    min-height: 100vh;
    font-size: 14px;
  }}

  /* ── TOPBAR ── */
  .topbar {{
    background: linear-gradient(135deg, var(--indigo-900) 0%, var(--indigo-700) 100%);
    padding: 0 32px;
    height: 58px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    box-shadow: 0 2px 12px rgba(0,0,0,.2);
    position: sticky; top: 0; z-index: 100;
  }}
  .topbar-brand {{
    display: flex; align-items: center; gap: 10px;
    font-size: 15px; font-weight: 700; color: #fff;
  }}
  .topbar-brand svg {{ opacity: .85 }}
  .topbar-right {{ display: flex; align-items: center; gap: 10px }}
  .badge {{
    display: inline-flex; align-items: center; gap: 5px;
    padding: 4px 10px; border-radius: 20px;
    font-size: 12px; font-weight: 500;
  }}
  .badge--green {{ background: rgba(22,163,74,.2); color: #4ade80 }}
  .badge--red   {{ background: rgba(220,38,38,.2);  color: #f87171 }}

  /* ── LAYOUT ── */
  .workspace {{
    max-width: 1240px; margin: 0 auto;
    padding: 24px;
    display: grid;
    grid-template-columns: 420px 1fr;
    gap: 20px;
    align-items: start;
  }}
  @media(max-width:900px) {{ .workspace {{ grid-template-columns: 1fr }} }}

  /* ── CARDS ── */
  .card {{
    background: var(--white);
    border: 1px solid var(--gray-200);
    border-radius: var(--radius);
    box-shadow: var(--shadow);
    overflow: hidden;
    margin-bottom: 16px;
  }}
  .card:last-child {{ margin-bottom: 0 }}
  .card-header {{
    padding: 14px 18px;
    border-bottom: 1px solid var(--gray-100);
    display: flex; align-items: center; justify-content: space-between;
  }}
  .card-title {{
    font-size: 11px; font-weight: 700; color: var(--gray-500);
    text-transform: uppercase; letter-spacing: .6px;
  }}
  .card-body {{ padding: 18px }}

  /* ── FORM FIELDS ── */
  .field {{ margin-bottom: 12px }}
  .field:last-child {{ margin-bottom: 0 }}
  .field label {{
    display: block; font-size: 12px; font-weight: 600;
    color: var(--gray-600); margin-bottom: 5px;
    text-transform: uppercase; letter-spacing: .4px;
  }}
  .field input, .field select, .field textarea {{
    width: 100%; padding: 8px 11px;
    border: 1.5px solid var(--gray-200);
    border-radius: var(--radius-sm);
    font-size: 13.5px; font-family: inherit;
    color: var(--gray-900); background: var(--white);
    outline: none; transition: border-color .15s, box-shadow .15s;
  }}
  .field textarea {{ resize: vertical; line-height: 1.55 }}
  .field input:focus, .field select:focus, .field textarea:focus {{
    border-color: var(--indigo-500);
    box-shadow: 0 0 0 3px rgba(99,102,241,.12);
  }}
  .field-hint {{ font-size: 11.5px; color: var(--gray-400); margin-top: 4px }}
  .field-row {{ display: grid; grid-template-columns: 1fr 1fr; gap: 12px }}

  /* ── SEPARATOR ── */
  .sep {{
    border: none; border-top: 1px solid var(--gray-100);
    margin: 16px 0;
  }}

  /* ── TONE SELECTOR ── */
  .tone-group {{ display: flex; gap: 6px; flex-wrap: wrap }}
  .tone-btn {{
    padding: 6px 14px;
    border: 1.5px solid var(--gray-200);
    border-radius: 20px; font-size: 12.5px; font-weight: 500;
    cursor: pointer; background: var(--white);
    color: var(--gray-600); font-family: inherit;
    transition: all .15s;
  }}
  .tone-btn:hover {{ border-color: var(--indigo-500); color: var(--indigo-600) }}
  .tone-btn.active {{
    background: var(--indigo-600); color: #fff;
    border-color: var(--indigo-600); font-weight: 600;
  }}

  /* ── BUTTONS ── */
  .btn {{
    display: inline-flex; align-items: center; gap: 6px;
    padding: 8px 16px; border: 1px solid transparent;
    border-radius: var(--radius-sm); font-size: 13.5px; font-weight: 600;
    cursor: pointer; font-family: inherit; transition: all .15s;
    text-decoration: none;
  }}
  .btn:active {{ transform: scale(.97) }}
  .btn--primary {{ background: var(--indigo-600); color: #fff; border-color: var(--indigo-700) }}
  .btn--primary:hover {{ background: var(--indigo-700) }}
  .btn--outline {{
    background: var(--white); color: var(--gray-700);
    border-color: var(--gray-200); box-shadow: var(--shadow-sm);
  }}
  .btn--outline:hover {{ background: var(--gray-50); border-color: var(--gray-300) }}
  .btn--ghost {{ background: transparent; color: var(--gray-500) }}
  .btn--ghost:hover {{ background: var(--gray-100); color: var(--gray-700) }}
  .btn-row {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 16px }}

  /* ── OUTPUT PANE ── */
  .output-card {{ position: sticky; top: 82px }}
  .output-header {{
    padding: 14px 18px;
    border-bottom: 1px solid var(--gray-100);
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px;
  }}
  .output-title-row {{ display: flex; align-items: center; gap: 10px }}
  #gen_status {{
    font-size: 11px; font-weight: 600; color: var(--gray-400);
    text-transform: uppercase; letter-spacing: .4px;
  }}
  #gen_status.active {{ color: var(--indigo-500) }}
  #output {{
    padding: 20px 22px;
    min-height: 460px; max-height: calc(100vh - 200px);
    font-size: 13.5px; line-height: 1.85;
    white-space: pre-wrap; overflow-y: auto;
    color: var(--gray-700);
    font-family: 'Georgia', serif;
  }}
  #output.placeholder {{ color: var(--gray-400); font-family: inherit; font-style: italic }}
  .output-footer {{
    padding: 12px 18px; border-top: 1px solid var(--gray-100);
    display: flex; align-items: center; gap: 8px;
    background: var(--gray-50);
  }}

  /* ── PROFILE EDITOR ── */
  .profile-toggle {{
    display: flex; align-items: center; justify-content: space-between;
    cursor: pointer; user-select: none;
  }}
  .profile-toggle-icon {{
    font-size: 12px; color: var(--gray-400);
    transition: transform .2s;
  }}
  .profile-toggle-icon.open {{ transform: rotate(180deg) }}
  #profile-body {{ display: none }}
  #profile-body.open {{ display: block }}
  #profile_json {{
    font-family: 'SF Mono','Fira Code',monospace;
    font-size: 12px; min-height: 200px;
    background: var(--gray-50); color: var(--gray-700);
  }}
</style>
</head>
<body>

<div class="topbar">
  <div class="topbar-brand">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="rgba(255,255,255,.9)" stroke-width="2">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
      <polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/>
      <line x1="16" y1="17" x2="8" y2="17"/><polyline points="10 9 9 9 8 9"/>
    </svg>
    Letter Writer
  </div>
  <div class="topbar-right">
    {ollama_badge}
  </div>
</div>

<div class="workspace">

  <!-- ── LEFT PANEL ── -->
  <div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">Opportunity</span>
      </div>
      <div class="card-body">
        <div class="field">
          <label>Work Area / Role Title</label>
          <input type="text" id="work_area" placeholder="Computer Science and Artificial Intelligence">
        </div>
        <div class="field-row">
          <div class="field">
            <label>Reference</label>
            <input type="text" id="ref" placeholder="AE2026-0110">
          </div>
          <div class="field">
            <label>Deadline</label>
            <input type="text" id="deadline" placeholder="2026-05-30">
          </div>
        </div>
        <div class="field">
          <label>Research Centre</label>
          <input type="text" id="centre" placeholder="Biomedical Engineering Research Centre">
        </div>
        <div class="field">
          <label>Scientific Advisor</label>
          <input type="text" id="advisor" placeholder="João Manuel Pedrosa">
        </div>
        <div class="field">
          <label>Position Type</label>
          <input type="text" id="position" value="Investigação (BI)">
        </div>
        <div class="field">
          <label>Opportunity URL</label>
          <input type="text" id="url" placeholder="https://www.inesctec.pt/en/opportunities/...">
        </div>
        <div class="field">
          <label>Description / Research Summary</label>
          <textarea id="summary" rows="5" placeholder="Paste the opportunity description for a more tailored letter…"></textarea>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <span class="card-title">Generation</span>
      </div>
      <div class="card-body">
        <div class="field">
          <label>Model</label>
          <select id="model">{model_options}</select>
          {'<p class="field-hint">No models found. Run: <code>ollama pull llama3.1:8b</code></p>' if not models else ''}
        </div>
        <div class="field">
          <label>Tone</label>
          <div class="tone-group">
            <button class="tone-btn active" data-tone="formal" onclick="setTone(this)">Formal</button>
            <button class="tone-btn" data-tone="enthusiastic" onclick="setTone(this)">Enthusiastic</button>
            <button class="tone-btn" data-tone="concise" onclick="setTone(this)">Concise</button>
          </div>
        </div>
        <input type="hidden" id="tone" value="formal">
        <div class="btn-row" style="margin-top:12px">
          <button class="btn btn--primary" onclick="generate()">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M12 2l3.09 6.26L22 9.27l-5 4.87 1.18 6.88L12 17.77l-6.18 3.25L7 14.14 2 9.27l6.91-1.01L12 2z"/></svg>
            Generate Letter
          </button>
          <button class="btn btn--ghost" onclick="clearAll()">Clear</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header" onclick="toggleProfile()" style="cursor:pointer">
        <span class="card-title">Your Profile</span>
        <span class="profile-toggle-icon" id="profile-icon">▼</span>
      </div>
      <div id="profile-body">
        <div class="card-body">
          <div class="field">
            <textarea id="profile_json" rows="14">{profile_json}</textarea>
            <p class="field-hint">Edit your CV details — changes apply to the next generation.</p>
          </div>
          <button class="btn btn--outline" onclick="saveProfile()">Save Profile</button>
          <span id="profile_save_msg" style="font-size:12px;color:var(--green-600);margin-left:10px;display:none">Saved ✓</span>
        </div>
      </div>
    </div>
  </div>

  <!-- ── RIGHT PANEL ── -->
  <div>
    <div class="card output-card">
      <div class="output-header">
        <div class="output-title-row">
          <span class="card-title">Generated Letter</span>
          <span id="gen_status"></span>
        </div>
        <button class="btn btn--outline" id="regen_btn" onclick="generate()" style="display:none;font-size:12px;padding:5px 12px">
          ↺ Regenerate
        </button>
      </div>
      <div id="output" class="placeholder">Your letter will appear here…</div>
      <div class="output-footer">
        <button class="btn btn--outline" onclick="copyLetter()" id="copy_btn">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
          </svg>
          Copy to Clipboard
        </button>
      </div>
    </div>
  </div>

</div>

<script>
  var currentTone = 'formal';

  function setTone(btn) {{
    document.querySelectorAll('.tone-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    document.getElementById('tone').value = btn.dataset.tone;
    currentTone = btn.dataset.tone;
  }}

  function getProfileJson() {{
    try {{ return JSON.parse(document.getElementById('profile_json').value); }}
    catch(e) {{ alert('Profile JSON is invalid: ' + e.message); return null; }}
  }}

  function toggleProfile() {{
    var body = document.getElementById('profile-body');
    var icon = document.getElementById('profile-icon');
    var open = body.classList.toggle('open');
    icon.classList.toggle('open', open);
  }}

  function generate() {{
    var out    = document.getElementById('output');
    var status = document.getElementById('gen_status');
    var profile = getProfileJson();
    if (!profile) return;

    var opp = {{
      ref:       document.getElementById('ref').value,
      work_area: document.getElementById('work_area').value,
      position:  document.getElementById('position').value,
      centre:    document.getElementById('centre').value,
      advisor:   document.getElementById('advisor').value,
      deadline:  document.getElementById('deadline').value,
      url:       document.getElementById('url').value,
      summary:   document.getElementById('summary').value,
    }};

    if (!opp.work_area) {{ alert('Please enter a Work Area / Role Title.'); return; }}

    out.textContent = '';
    out.classList.remove('placeholder');
    status.textContent = 'Generating…';
    status.classList.add('active');
    document.getElementById('regen_btn').style.display = 'none';

    var fullText = '';

    fetch('/generate', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{ opportunity: opp, tone: document.getElementById('tone').value,
                              model: document.getElementById('model').value, profile: profile }}),
    }}).then(function(resp) {{
      if (!resp.ok) {{ return resp.text().then(function(t) {{ throw new Error(t); }}); }}
      var reader = resp.body.getReader();
      var decoder = new TextDecoder();
      function read() {{
        reader.read().then(function({{done, value}}) {{
          if (done) {{
            status.textContent = 'Done ✓'; status.classList.remove('active');
            document.getElementById('regen_btn').style.display = 'inline-flex';
            return;
          }}
          decoder.decode(value, {{stream:true}}).split('\\n').forEach(function(line) {{
            if (line.startsWith('data: ')) {{
              var token = line.slice(6);
              if (token === '[DONE]') return;
              fullText += token;
              out.textContent = fullText;
            }}
          }});
          read();
        }});
      }}
      read();
    }}).catch(function(err) {{
      out.textContent = 'Error: ' + err.message;
      status.textContent = 'Error'; status.classList.remove('active');
    }});
  }}

  function copyLetter() {{
    var text = document.getElementById('output').textContent;
    var btn  = document.getElementById('copy_btn');
    navigator.clipboard.writeText(text).then(function() {{
      btn.innerHTML = '✓ Copied!';
      setTimeout(function() {{
        btn.innerHTML = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg> Copy to Clipboard';
      }}, 2000);
    }});
  }}

  function clearAll() {{
    ['ref','work_area','centre','advisor','deadline','url','summary'].forEach(function(id) {{
      document.getElementById(id).value = '';
    }});
    var out = document.getElementById('output');
    out.textContent = 'Your letter will appear here…';
    out.classList.add('placeholder');
    document.getElementById('gen_status').textContent = '';
    document.getElementById('regen_btn').style.display = 'none';
  }}

  function saveProfile() {{
    var profile = getProfileJson();
    if (!profile) return;
    fetch('/save_profile', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify(profile),
    }}).then(function(r) {{
      if (r.ok) {{
        var msg = document.getElementById('profile_save_msg');
        msg.style.display = 'inline';
        setTimeout(function() {{ msg.style.display = 'none'; }}, 2500);
      }}
    }});
  }}

  // Fill in from query params (used by the monitor to pre-populate)
  (function() {{
    var params = new URLSearchParams(window.location.search);
    var filled = false;
    ['ref','work_area','centre','advisor','deadline','url','summary','position'].forEach(function(k) {{
      var el = document.getElementById(k);
      var v  = params.get(k);
      if (el && v) {{ el.value = v; filled = true; }}
    }});
  }})();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            body = _render_html().encode()
            self._respond(200, "text/html; charset=utf-8", body)
        else:
            self._respond(404, "text/plain", b"Not found")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw    = self.rfile.read(length)
        path   = urlparse(self.path).path

        if path == "/generate":
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                self._respond(400, "text/plain", b"Invalid JSON")
                return
            self._stream_letter(data)

        elif path == "/save_profile":
            try:
                profile = json.loads(raw)
                with open(PROFILE_FILE, "w", encoding="utf-8") as f:
                    json.dump(profile, f, ensure_ascii=False, indent=2)
                self._respond(200, "text/plain", b"OK")
            except Exception as e:
                self._respond(500, "text/plain", str(e).encode())
        else:
            self._respond(404, "text/plain", b"Not found")

    def _stream_letter(self, data: dict):
        opp     = data.get("opportunity", {})
        tone    = data.get("tone", "formal")
        model   = data.get("model", DEFAULT_MODEL)
        profile = data.get("profile")

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        try:
            from letter_generator import generate_letter_stream
            for token in generate_letter_stream(opp, tone=tone, model=model, profile=profile):
                escaped = token.replace("\n", "\\n")
                self.wfile.write(f"data: {escaped}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except Exception as exc:
            self.wfile.write(f"data: [ERROR] {exc}\n\n".encode())
            self.wfile.flush()

    def _respond(self, code: int, ctype: str, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    server = ThreadedHTTPServer(("127.0.0.1", PORT), Handler)
    url    = f"http://localhost:{PORT}"
    print(f"Letter Writer running at {url}")
    print("Press Ctrl+C to quit.")
    threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        server.shutdown()
