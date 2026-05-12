#!/usr/bin/env python3
"""
RishiPraveen Editor – Web Edition
Upload once → Cut → Burn Logo → Merge → Download
Works on mobile, Mac, Windows. Deploy to Render.com free tier.
"""
import os, sys, json, threading, uuid, time, shutil, subprocess, tempfile, re
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

PORT     = int(os.environ.get('PORT', 7892))
HOST     = '0.0.0.0'
MAX_UP   = 800 * 1024 * 1024   # 800 MB per file

WORK_DIR = os.path.join(tempfile.gettempdir(), 'rp_sessions')
os.makedirs(WORK_DIR, exist_ok=True)

_sessions: dict = {}
_jobs: dict     = {}
_L = threading.Lock()

# ── session management ────────────────────────────────────────────────────────
def new_session():
    sid = uuid.uuid4().hex
    d   = os.path.join(WORK_DIR, sid)
    os.makedirs(d, exist_ok=True)
    with _L:
        _sessions[sid] = {'dir': d, 'files': {}, 'ts': time.time()}
    return sid

def get_session(sid):
    with _L:
        s = _sessions.get(sid)
        if s:
            s['ts'] = time.time()
        return s

def add_file(sid, orig_name: str, data: bytes):
    s = get_session(sid)
    if not s:
        return None
    safe = re.sub(r'[^\w.\-]', '_', os.path.basename(orig_name)) or 'file'
    path = os.path.join(s['dir'], safe)
    if os.path.exists(path):
        base, ext = os.path.splitext(safe)
        safe = f"{base}_{uuid.uuid4().hex[:4]}{ext}"
        path = os.path.join(s['dir'], safe)
    with open(path, 'wb') as f:
        f.write(data)
    with _L:
        _sessions[sid]['files'][safe] = path
    return safe

def _gc():
    while True:
        time.sleep(600)
        cutoff = time.time() - 7200
        with _L:
            dead = [k for k, v in list(_sessions.items()) if v['ts'] < cutoff]
        for sid in dead:
            s = _sessions.pop(sid, {})
            shutil.rmtree(s.get('dir', ''), ignore_errors=True)

threading.Thread(target=_gc, daemon=True).start()

# ── ffmpeg ────────────────────────────────────────────────────────────────────
def _find_ff():
    if shutil.which('ffmpeg'):
        return shutil.which('ffmpeg'), shutil.which('ffprobe')
    sd = os.path.dirname(os.path.abspath(__file__))
    if sys.platform == 'win32':
        ff = os.path.join(sd, 'ffmpeg_bin', 'ffmpeg.exe')
        fp = os.path.join(sd, 'ffmpeg_bin', 'ffprobe.exe')
        if os.path.exists(ff):
            return ff, fp
    return None, None

FFMPEG, FFPROBE = _find_ff()

def probe(path):
    if not FFPROBE:
        return {}
    r = subprocess.run(
        [FFPROBE, '-v', 'quiet', '-print_format', 'json',
         '-show_streams', '-show_format', path],
        capture_output=True, text=True
    )
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}

def ff_run(args, jid=None):
    proc = subprocess.Popen(
        [FFMPEG] + args,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True
    )
    log = []
    for line in proc.stdout:
        line = line.rstrip()
        log.append(line)
        if jid:
            with _L:
                if jid in _jobs:
                    _jobs[jid].setdefault('log', []).append(line)
    proc.wait()
    return proc.returncode, log

# ── pipeline operations ───────────────────────────────────────────────────────
def op_cut(s, step, jid):
    inp   = step.get('input', '')
    src   = s['files'].get(inp)
    if not src:
        return None, f"File not found in session: {inp}"
    start = step.get('start', '0') or '0'
    end   = step.get('end', '').strip()
    mode  = step.get('mode', 'fast')
    base, ext = os.path.splitext(inp)
    out_name  = f"{base}_cut{ext}"
    out_path  = os.path.join(s['dir'], out_name)
    i = 1
    while os.path.exists(out_path):
        out_name = f"{base}_cut{i}{ext}"
        out_path = os.path.join(s['dir'], out_name)
        i += 1
    if mode == 'accurate':
        args = ['-y', '-i', src, '-ss', start]
        if end:
            args += ['-to', end]
        args += ['-c:v', 'libx264', '-c:a', 'aac', out_path]
    else:
        args = ['-y', '-i', src, '-ss', start]
        if end:
            args += ['-to', end]
        args += ['-c', 'copy', out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"FFmpeg cut failed (exit {rc})"
    with _L:
        s['files'][out_name] = out_path
    return out_name, None

def op_logo(s, step, jid):
    inp      = step.get('input', '')
    logo_inp = step.get('logo', '')
    src      = s['files'].get(inp)
    logo_src = s['files'].get(logo_inp)
    if not src:
        return None, f"Video not found: {inp}"
    if not logo_src:
        return None, f"Logo not found: {logo_inp}"
    corner   = step.get('corner', 'tr')
    size_pct = max(1, min(100, int(step.get('size_pct', 12))))
    pad = 20
    corners  = {
        'tl':     f"overlay={pad}:{pad}",
        'tr':     f"overlay=W-w-{pad}:{pad}",
        'bl':     f"overlay={pad}:H-h-{pad}",
        'br':     f"overlay=W-w-{pad}:H-h-{pad}",
        'center': "overlay=(W-w)/2:(H-h)/2",
    }
    overlay = corners.get(corner, corners['tr'])
    vf      = f"[1:v]scale=iw*{size_pct}/100:-1[lg];[0:v][lg]{overlay}"
    base, _ = os.path.splitext(inp)
    out_name = f"{base}_logo.mp4"
    out_path = os.path.join(s['dir'], out_name)
    i = 1
    while os.path.exists(out_path):
        out_name = f"{base}_logo{i}.mp4"
        out_path = os.path.join(s['dir'], out_name)
        i += 1
    args = ['-y', '-i', src, '-i', logo_src,
            '-filter_complex', vf, '-c:v', 'libx264', '-c:a', 'copy', out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"FFmpeg logo failed (exit {rc})"
    with _L:
        s['files'][out_name] = out_path
    return out_name, None

def op_merge(s, step, jid):
    inputs = [x for x in step.get('inputs', []) if x]
    if len(inputs) < 2:
        return None, "Need at least 2 files to merge"
    paths = [s['files'].get(n) for n in inputs]
    missing = [n for n, p in zip(inputs, paths) if not p]
    if missing:
        return None, f"Files not found: {missing}"
    base, _ = os.path.splitext(inputs[0])
    out_name = f"{base}_merged.mp4"
    out_path = os.path.join(s['dir'], out_name)
    i = 1
    while os.path.exists(out_path):
        out_name = f"{base}_merged{i}.mp4"
        out_path = os.path.join(s['dir'], out_name)
        i += 1
    list_path = os.path.join(s['dir'], f"_cl_{uuid.uuid4().hex[:6]}.txt")
    with open(list_path, 'w') as f:
        for p in paths:
            f.write(f"file '{p}'\n")
    args = ['-y', '-f', 'concat', '-safe', '0', '-i', list_path,
            '-c', 'copy', out_path]
    rc, _ = ff_run(args, jid)
    try:
        os.remove(list_path)
    except Exception:
        pass
    if rc != 0 or not os.path.exists(out_path):
        return None, f"FFmpeg merge failed (exit {rc})"
    with _L:
        s['files'][out_name] = out_path
    return out_name, None

def op_audio_cut(s, step, jid):
    inp   = step.get('input', '')
    src   = s['files'].get(inp)
    if not src:
        return None, f"File not found in session: {inp}"
    start = step.get('start', '0') or '0'
    end   = step.get('end', '').strip()
    mode  = step.get('mode', 'accurate')
    base, ext = os.path.splitext(inp)
    out_ext = ext.lower() if ext.lower() in ('.mp3', '.aac', '.wav', '.flac', '.ogg', '.m4a') else '.mp3'
    out_name = f"{base}_cut{out_ext}"
    out_path = os.path.join(s['dir'], out_name)
    i = 1
    while os.path.exists(out_path):
        out_name = f"{base}_cut{i}{out_ext}"
        out_path = os.path.join(s['dir'], out_name)
        i += 1
    if mode == 'accurate':
        info = probe(src)
        src_a = next((st for st in info.get('streams', []) if st.get('codec_type') == 'audio'), None)
        aud_bps = int((src_a or {}).get('bit_rate', 0) or 0)
        if not aud_bps:
            aud_bps = int(info.get('format', {}).get('bit_rate', 0) or 0)
        bitrate = str(aud_bps) if aud_bps > 32_000 else '192k'
        args = ['-y', '-ss', start, '-i', src]
        if end:
            args += ['-to', end]
        args += ['-vn', '-c:a', 'libmp3lame', '-b:a', bitrate, out_path]
    else:
        args = ['-y', '-ss', start, '-i', src]
        if end:
            args += ['-to', end]
        args += ['-vn', '-c:a', 'copy', out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"FFmpeg audio cut failed (exit {rc})"
    with _L:
        s['files'][out_name] = out_path
    return out_name, None

def op_convert(s, step, jid):
    inp     = step.get('input', '')
    src     = s['files'].get(inp)
    quality = step.get('quality', '192k')
    if quality not in ('128k', '192k', '320k'):
        quality = '192k'
    if not src:
        return None, f"File not found in session: {inp}"
    base, _ = os.path.splitext(inp)
    out_name = f"{base}.mp3"
    out_path = os.path.join(s['dir'], out_name)
    i = 1
    while os.path.exists(out_path):
        out_name = f"{base}_mp3_{i}.mp3"
        out_path = os.path.join(s['dir'], out_name)
        i += 1
    args = ['-y', '-i', src, '-vn', '-c:a', 'libmp3lame', '-b:a', quality, out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"FFmpeg convert failed (exit {rc})"
    with _L:
        s['files'][out_name] = out_path
    return out_name, None

def run_pipeline(jid, sid, steps):
    s = get_session(sid)
    if not s:
        with _L:
            _jobs[jid].update({'done': True, 'error': 'Session expired'})
        return
    outputs = []
    ops_map = {'cut': op_cut, 'logo': op_logo, 'merge': op_merge,
               'audio_cut': op_audio_cut, 'convert': op_convert}
    n = len(steps)
    for i, step in enumerate(steps):
        msg = f"▶ Step {i+1}/{n}: {step['op'].upper()}"
        with _L:
            _jobs[jid].setdefault('log', []).append(msg)
            _jobs[jid]['current_step'] = i
        fn = ops_map.get(step['op'])
        if not fn:
            with _L:
                _jobs[jid].update({'done': True, 'error': f"Unknown op: {step['op']}"})
            return
        out_name, err = fn(s, step, jid)
        if err:
            with _L:
                _jobs[jid].update({'done': True, 'error': err})
            return
        outputs.append(out_name)
        with _L:
            _jobs[jid].setdefault('log', []).append(f"✓ Done: {out_name}")
    with _L:
        _jobs[jid].update({'done': True, 'outputs': outputs, 'error': None})

# ── multipart parser ──────────────────────────────────────────────────────────
def parse_multipart(content_type: str, body: bytes):
    boundary = None
    for part in content_type.split(';'):
        p = part.strip()
        if p.startswith('boundary='):
            boundary = p[9:].strip('"')
    if not boundary:
        return {}, {}
    fields, files = {}, {}
    sep = ('--' + boundary).encode()
    parts = body.split(sep)
    for raw in parts[1:]:
        if raw.strip() in (b'--', b'--\r\n'):
            break
        if raw.startswith(b'\r\n'):
            raw = raw[2:]
        if raw.endswith(b'\r\n'):
            raw = raw[:-2]
        h_end = raw.find(b'\r\n\r\n')
        if h_end == -1:
            continue
        hdr = raw[:h_end].decode('utf-8', errors='replace')
        dat = raw[h_end + 4:]
        name = fname = None
        for line in hdr.split('\r\n'):
            if 'Content-Disposition' in line:
                for tok in line.split(';'):
                    tok = tok.strip()
                    if tok.startswith('name='):
                        name = tok[5:].strip('"')
                    elif tok.startswith('filename='):
                        fname = tok[9:].strip('"')
        if name:
            if fname:
                files[name] = (fname, dat)
            else:
                fields[name] = dat.decode('utf-8', errors='replace')
    return fields, files

# ── HTTP handler ──────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        p = urlparse(self.path).path

        if p == '/':
            body = HTML.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        elif p.startswith('/files/'):
            sid = p[7:]
            s = get_session(sid)
            if not s:
                self.send_json({'error': 'not found'}, 404)
                return
            self.send_json({'files': list(s['files'].keys())})

        elif p.startswith('/job/'):
            jid = p[5:]
            with _L:
                j = dict(_jobs.get(jid, {}))
            if not j:
                self.send_json({'error': 'not found'}, 404)
                return
            self.send_json(j)

        elif p.startswith('/download/'):
            parts = p[10:].split('/', 1)
            if len(parts) != 2:
                self.send_json({'error': 'bad path'}, 400)
                return
            sid, fname = parts
            s = get_session(sid)
            if not s:
                self.send_json({'error': 'session not found'}, 404)
                return
            fpath = s['files'].get(fname)
            if not fpath or not os.path.exists(fpath):
                self.send_json({'error': 'file not found'}, 404)
                return
            size = os.path.getsize(fpath)
            ext  = os.path.splitext(fname)[1].lower()
            ctype = 'video/mp4' if ext == '.mp4' else \
                    'video/quicktime' if ext == '.mov' else \
                    'video/x-matroska' if ext == '.mkv' else \
                    'audio/mpeg' if ext == '.mp3' else \
                    'audio/aac' if ext == '.aac' else \
                    'audio/wav' if ext == '.wav' else \
                    'audio/flac' if ext == '.flac' else \
                    'audio/mp4' if ext == '.m4a' else \
                    'application/octet-stream'
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Disposition', f'attachment; filename="{fname}"')
            self.send_header('Content-Length', str(size))
            self.end_headers()
            with open(fpath, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)

        else:
            self.send_json({'error': 'not found'}, 404)

    def do_POST(self):
        p      = urlparse(self.path).path
        length = int(self.headers.get('Content-Length', 0))

        if p == '/session':
            sid = new_session()
            self.send_json({'session_id': sid})

        elif p == '/upload':
            if length > MAX_UP:
                self.send_json({'error': f'File too large (max {MAX_UP//1024//1024} MB)'}, 413)
                return
            body   = self.rfile.read(length)
            ct     = self.headers.get('Content-Type', '')
            fields, files = parse_multipart(ct, body)
            sid    = fields.get('session_id', '')
            if not sid or not get_session(sid):
                self.send_json({'error': 'Invalid or expired session'}, 400)
                return
            saved = {}
            for field_key, (orig_name, data) in files.items():
                name = add_file(sid, orig_name, data)
                if name:
                    saved[field_key] = name
            s = get_session(sid)
            self.send_json({'saved': saved, 'files': list(s['files'].keys())})

        elif p == '/probe':
            body  = json.loads(self.rfile.read(length))
            sid   = body.get('session_id', '')
            fname = body.get('file', '')
            s     = get_session(sid)
            if not s:
                self.send_json({'error': 'session'}, 400)
                return
            fpath = s['files'].get(fname)
            if not fpath:
                self.send_json({'error': 'file not found'}, 404)
                return
            info = probe(fpath)
            dur  = float(info.get('format', {}).get('duration', 0)) if info else 0
            self.send_json({'duration': dur})

        elif p == '/run':
            body  = json.loads(self.rfile.read(length))
            sid   = body.get('session_id', '')
            steps = body.get('steps', [])
            if not sid or not get_session(sid):
                self.send_json({'error': 'Invalid or expired session'}, 400)
                return
            if not steps:
                self.send_json({'error': 'No steps provided'}, 400)
                return
            jid = uuid.uuid4().hex
            with _L:
                _jobs[jid] = {
                    'done': False,
                    'current_step': 0,
                    'total_steps': len(steps),
                    'log': [],
                    'outputs': [],
                    'error': None,
                }
            threading.Thread(
                target=run_pipeline, args=(jid, sid, steps), daemon=True
            ).start()
            self.send_json({'job_id': jid})

        else:
            self.send_json({'error': 'not found'}, 404)


# ── HTML ──────────────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>RishiPraveen Editor</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--surface:#111118;--card:#16161f;--border:#2a2a3a;--accent:#e8ff47;--blue:#47b3ff;--text:#e8e8f0;--muted:#6b6b80;--danger:#ff4747;--success:#47ffb0;--radius:12px;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;}
.header{border-bottom:1px solid var(--border);padding:12px 20px;display:flex;align-items:center;gap:10px;background:var(--surface);position:sticky;top:0;z-index:100;}
.logo{font-size:18px;font-weight:800;letter-spacing:-.5px;}.logo span{color:var(--accent);}
.badge{font-size:9px;font-weight:700;padding:3px 8px;border-radius:4px;letter-spacing:1px;font-family:'JetBrains Mono',monospace;background:var(--blue);color:#000;}
.sess-info{margin-left:auto;font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.tabs{display:flex;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:49px;z-index:99;}
.tab{flex:1;padding:12px 6px;background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:12px;transition:.15s;letter-spacing:.3px;}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);}
.panel{display:none;padding:20px;max-width:760px;margin:0 auto;}
.panel.active{display:block;}
.upload-zone{border:2px dashed var(--border);border-radius:var(--radius);padding:36px 20px;text-align:center;cursor:pointer;transition:.15s;background:var(--card);margin-bottom:16px;}
.upload-zone:hover,.upload-zone.drag{border-color:var(--accent);background:rgba(232,255,71,.04);}
.upload-icon{font-size:40px;margin-bottom:10px;}
.upload-title{font-size:15px;font-weight:700;margin-bottom:6px;}
.upload-sub{font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.file-list{display:flex;flex-direction:column;gap:8px;}
.file-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;align-items:center;gap:10px;}
.file-icon{font-size:20px;flex-shrink:0;}
.file-name{font-size:13px;font-family:'JetBrains Mono',monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.file-badge{font-size:10px;padding:2px 7px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-weight:700;background:var(--border);color:var(--muted);flex-shrink:0;}
.file-badge.video{background:rgba(71,179,255,.15);color:var(--blue);}
.file-badge.image{background:rgba(232,255,71,.15);color:var(--accent);}
.file-badge.audio{background:rgba(255,159,71,.15);color:#ff9f47;}
.empty-state{text-align:center;padding:40px 20px;color:var(--muted);font-size:13px;font-family:'JetBrains Mono',monospace;}
.add-ops{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:20px;}
.btn-add{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 16px;color:var(--text);cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:13px;transition:.15s;display:flex;align-items:center;gap:6px;}
.btn-add:hover{border-color:var(--accent);color:var(--accent);}
.btn-add.cut:hover{border-color:var(--danger);color:var(--danger);}
.btn-add.logo:hover{border-color:var(--accent);color:var(--accent);}
.btn-add.merge:hover{border-color:var(--blue);color:var(--blue);}
.btn-add.audio:hover{border-color:#ff9f47;color:#ff9f47;}
.btn-add.conv:hover{border-color:var(--success);color:var(--success);}
.step-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;overflow:hidden;}
.step-head{padding:12px 16px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.02);}
.step-num{width:26px;height:26px;border-radius:50%;background:var(--border);display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;font-family:'JetBrains Mono',monospace;flex-shrink:0;}
.step-label{font-size:13px;font-weight:700;flex:1;}
.step-acts{display:flex;gap:4px;}
.icon-btn{background:none;border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--muted);cursor:pointer;font-size:12px;transition:.15s;}
.icon-btn:hover{border-color:var(--text);color:var(--text);}
.icon-btn.del:hover{border-color:var(--danger);color:var(--danger);}
.step-body{padding:14px 16px;display:flex;flex-direction:column;gap:10px;}
.field{display:flex;flex-direction:column;gap:5px;}
.field label{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.5px;text-transform:uppercase;}
.field select,.field input[type=text],.field input[type=number]{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;transition:.15s;width:100%;}
.field select:focus,.field input:focus{border-color:var(--accent);}
.corner-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;}
.corner-btn{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:7px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--muted);transition:.15s;text-align:center;}
.corner-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(232,255,71,.08);}
.corner-btn:hover{border-color:var(--text);}
.merge-inputs{display:flex;flex-direction:column;gap:6px;margin-bottom:8px;}
.merge-row{display:flex;gap:6px;align-items:center;}
.merge-row select{flex:1;}
.btn-small{background:none;border:1px solid var(--border);border-radius:6px;padding:6px 12px;color:var(--muted);cursor:pointer;font-size:12px;font-family:'Syne',sans-serif;font-weight:700;transition:.15s;}
.btn-small:hover{border-color:var(--accent);color:var(--accent);}
.run-summary{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;margin-bottom:16px;}
.run-summary h3{font-size:13px;font-weight:700;margin-bottom:10px;color:var(--muted);}
.ps-item{display:flex;align-items:center;gap:10px;padding:7px 0;border-bottom:1px solid var(--border);font-size:13px;}
.ps-item:last-child{border:none;padding-bottom:0;}
.ps-num{width:22px;height:22px;border-radius:50%;background:var(--border);display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;font-family:'JetBrains Mono',monospace;flex-shrink:0;}
.ps-item em{font-style:normal;color:var(--accent);font-family:'JetBrains Mono',monospace;font-size:12px;}
.btn-run{width:100%;padding:14px;background:var(--accent);color:#000;border:none;border-radius:var(--radius);font-family:'Syne',sans-serif;font-weight:800;font-size:15px;cursor:pointer;transition:.15s;margin-bottom:16px;}
.btn-run:hover{background:#d4e83e;}
.btn-run:disabled{opacity:.4;cursor:not-allowed;}
.progress-box{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:14px;margin-bottom:16px;}
.log-box{font-family:'JetBrains Mono',monospace;font-size:11px;max-height:200px;overflow-y:auto;display:flex;flex-direction:column;gap:2px;}
.log-step{color:var(--accent);font-weight:700;padding:3px 0;}
.log-ok{color:var(--success);}
.log-line{color:var(--muted);}
.log-err{color:var(--danger);}
.downloads{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);padding:16px;}
.downloads h3{font-size:13px;font-weight:700;margin-bottom:12px;color:var(--success);}
.dl-btn{display:flex;align-items:center;gap:10px;background:var(--surface);border:1px solid var(--success);border-radius:8px;padding:12px 16px;color:var(--success);text-decoration:none;font-family:'Syne',sans-serif;font-weight:700;font-size:13px;margin-bottom:8px;transition:.15s;}
.dl-btn:hover{background:rgba(71,255,176,.08);}
.dl-btn:last-child{margin-bottom:0;}
.dl-icon{font-size:18px;}
.error-banner{background:rgba(255,71,71,.1);border:1px solid var(--danger);border-radius:8px;padding:12px 16px;color:var(--danger);font-size:13px;font-family:'JetBrains Mono',monospace;margin-top:12px;}
.up-item{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:10px 14px;display:flex;align-items:center;justify-content:space-between;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted);}
.up-spinner{width:14px;height:14px;border:2px solid var(--border);border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;}
@keyframes spin{to{transform:rotate(360deg);}}
@media(max-width:480px){
  .header{padding:10px 14px;}
  .logo{font-size:16px;}
  .panel{padding:14px;}
  .add-ops{gap:6px;}
  .btn-add{padding:9px 12px;font-size:12px;}
}
</style>
</head>
<body>
<div class="header">
  <div class="logo">RishiPraveen <span>Editor</span></div>
  <span class="badge">CLOUD</span>
  <div class="sess-info" id="sess-info">Starting…</div>
</div>

<div class="tabs">
  <button class="tab active" data-tab="files" onclick="showTab('files')">📁 Files</button>
  <button class="tab" data-tab="pipeline" onclick="showTab('pipeline')">⚙️ Pipeline</button>
  <button class="tab" data-tab="run" onclick="showTab('run')">▶ Run</button>
</div>

<div id="panel-files" class="panel active">
  <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-inp').click()">
    <div class="upload-icon">📤</div>
    <div class="upload-title">Tap to upload videos &amp; images</div>
    <div class="upload-sub">MP4, MOV, MKV, AVI · MP3, WAV, FLAC, M4A · PNG, JPG — up to 800 MB each</div>
  </div>
  <input type="file" id="file-inp" multiple accept="video/*,audio/*,image/*,.mp4,.mov,.mkv,.avi,.mp3,.aac,.wav,.flac,.m4a,.ogg,.png,.jpg,.jpeg" style="display:none">
  <div id="upload-items"></div>
  <div id="file-list" class="file-list"></div>
</div>

<div id="panel-pipeline" class="panel">
  <div class="add-ops">
    <button class="btn-add cut" onclick="addStep('cut')">✂️ Cut Video</button>
    <button class="btn-add logo" onclick="addStep('logo')">🏷️ Burn Logo</button>
    <button class="btn-add merge" onclick="addStep('merge')">🔗 Merge</button>
    <button class="btn-add audio" onclick="addStep('audio_cut')">✂️ Cut Audio</button>
    <button class="btn-add conv" onclick="addStep('convert')">🎵 To MP3</button>
  </div>
  <div id="steps-list"></div>
</div>

<div id="panel-run" class="panel">
  <div id="run-summary"></div>
  <button class="btn-run" id="run-btn" onclick="runPipeline()">▶ Run Pipeline</button>
  <div id="run-progress" style="display:none"></div>
  <div id="run-outputs" style="display:none"></div>
</div>

<script>
var SID = localStorage.getItem('rp_sid') || '';
var FILES = [];
var PIPE  = [];
try { PIPE = JSON.parse(localStorage.getItem('rp_pipe') || '[]'); } catch(e){}

// ── tabs ──────────────────────────────────────────────────────────────────────
function showTab(name) {
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.toggle('active', t.dataset.tab===name); });
  document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
  document.getElementById('panel-'+name).classList.add('active');
  if (name==='pipeline') renderPipeline();
  if (name==='run') renderRunSummary();
}

// ── session ───────────────────────────────────────────────────────────────────
async function initSession() {
  if (SID) {
    try {
      var r = await fetch('/files/'+SID);
      if (r.ok) {
        var d = await r.json();
        FILES = d.files || [];
        renderFileList();
        setInfo(FILES.length+' file(s) in session');
        return;
      }
    } catch(e){}
  }
  var r = await fetch('/session', {method:'POST'});
  var d = await r.json();
  SID = d.session_id;
  localStorage.setItem('rp_sid', SID);
  FILES = [];
  setInfo('New session ready');
}

function setInfo(t) { document.getElementById('sess-info').textContent = t; }

// ── upload ────────────────────────────────────────────────────────────────────
var dz = document.getElementById('drop-zone');
dz.addEventListener('dragover', function(e){ e.preventDefault(); dz.classList.add('drag'); });
dz.addEventListener('dragleave', function(){ dz.classList.remove('drag'); });
dz.addEventListener('drop', function(e){
  e.preventDefault(); dz.classList.remove('drag');
  uploadFiles(Array.from(e.dataTransfer.files));
});
document.getElementById('file-inp').addEventListener('change', function(e){
  uploadFiles(Array.from(e.target.files));
  e.target.value = '';
});

async function uploadFiles(files) {
  for (var i=0; i<files.length; i++) {
    await uploadOne(files[i]);
  }
}

async function uploadOne(file) {
  var itemId = 'up-'+Date.now();
  var wrap = document.getElementById('upload-items');
  var el = document.createElement('div');
  el.id = itemId;
  el.className = 'up-item';
  el.innerHTML = '<span>'+escHtml(file.name)+'</span><div class="up-spinner"></div>';
  wrap.prepend(el);
  try {
    var fd = new FormData();
    fd.append('session_id', SID);
    fd.append('file', file, file.name);
    var r = await fetch('/upload', {method:'POST', body:fd});
    var d = await r.json();
    if (d.error) throw new Error(d.error);
    FILES = d.files || FILES;
    renderFileList();
    setInfo(FILES.length+' file(s) in session');
    el.remove();
  } catch(err) {
    el.querySelector('.up-spinner').outerHTML = '<span style="color:var(--danger)">Error: '+escHtml(err.message)+'</span>';
    setTimeout(function(){ el.remove(); }, 4000);
  }
}

function renderFileList() {
  var el = document.getElementById('file-list');
  if (!FILES.length) {
    el.innerHTML = '<div class="empty-state">No files yet — tap above to upload</div>';
    return;
  }
  el.innerHTML = FILES.map(function(f){
    var isV = /\.(mp4|mov|mkv|avi|webm)$/i.test(f);
    var isA = /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f);
    var isI = /\.(png|jpg|jpeg|gif|webp)$/i.test(f);
    var icon = isV ? '🎬' : isA ? '🎵' : isI ? '🖼️' : '📄';
    var badge = isV ? '<span class="file-badge video">VIDEO</span>' : isA ? '<span class="file-badge audio">AUDIO</span>' : isI ? '<span class="file-badge image">IMAGE</span>' : '';
    return '<div class="file-item"><span class="file-icon">'+icon+'</span><span class="file-name">'+escHtml(f)+'</span>'+badge+'</div>';
  }).join('');
}

// ── pipeline ──────────────────────────────────────────────────────────────────
function savePipe() { localStorage.setItem('rp_pipe', JSON.stringify(PIPE)); }

function addStep(op) {
  var vids = FILES.filter(function(f){ return /\.(mp4|mov|mkv|avi|webm)$/i.test(f); });
  var auds = FILES.filter(function(f){ return /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f); });
  var imgs = FILES.filter(function(f){ return /\.(png|jpg|jpeg)$/i.test(f); });
  var step = {id: Date.now(), op: op};
  if (op==='cut') {
    step.input = vids[0]||''; step.start='00:00:00'; step.end=''; step.mode='fast';
  } else if (op==='logo') {
    step.input = vids[0]||''; step.logo = imgs[0]||''; step.corner='tr'; step.size_pct=12;
  } else if (op==='merge') {
    step.inputs = vids.length>=2 ? vids.slice(0,2) : ['',''];
  } else if (op==='audio_cut') {
    step.input = auds[0]||''; step.start='00:00:00'; step.end=''; step.mode='accurate';
  } else if (op==='convert') {
    step.input = vids[0]||''; step.quality='192k';
  }
  PIPE.push(step);
  savePipe();
  renderPipeline();
  showTab('pipeline');
}

function removeStep(id) { PIPE=PIPE.filter(function(s){return s.id!==id;}); savePipe(); renderPipeline(); }

function moveStep(id, dir) {
  var idx = PIPE.findIndex(function(s){return s.id===id;});
  var ni = idx+dir;
  if (ni<0||ni>=PIPE.length) return;
  var tmp=PIPE[idx]; PIPE[idx]=PIPE[ni]; PIPE[ni]=tmp;
  savePipe(); renderPipeline();
}

function stepSet(id, key, val) {
  var s=PIPE.find(function(x){return x.id===id;});
  if(s){s[key]=val;savePipe();}
}

function mergeSet(id, i, val) {
  var s=PIPE.find(function(x){return x.id===id;});
  if(s){s.inputs[i]=val;savePipe();}
}

function mergeAdd(id) {
  var s=PIPE.find(function(x){return x.id===id;});
  if(s){s.inputs.push('');savePipe();renderPipeline();}
}

function mergeDel(id,i) {
  var s=PIPE.find(function(x){return x.id===id;});
  if(s){s.inputs.splice(i,1);savePipe();renderPipeline();}
}

function fileOpts(sel) {
  return FILES.map(function(f){
    return '<option value="'+escHtml(f)+'"'+(f===sel?' selected':'')+'>'+escHtml(f)+'</option>';
  }).join('');
}

function renderPipeline() {
  var el = document.getElementById('steps-list');
  if (!PIPE.length) {
    el.innerHTML='<div class="empty-state">No steps yet — add operations above</div>'; return;
  }
  var labels={cut:'✂️ Cut Video',logo:'🏷️ Burn Logo',merge:'🔗 Merge',audio_cut:'✂️ Cut Audio',convert:'🎵 To MP3'};
  el.innerHTML = PIPE.map(function(s,idx){
    var n=PIPE.length;
    var acts=(idx>0?'<button class="icon-btn" onclick="moveStep('+s.id+',-1)" title="Move up">↑</button>':'')+
             (idx<n-1?'<button class="icon-btn" onclick="moveStep('+s.id+',1)" title="Move down">↓</button>':'')+
             '<button class="icon-btn del" onclick="removeStep('+s.id+')">🗑</button>';
    var body='';
    if (s.op==='cut') {
      body='<div class="field"><label>Video</label><select onchange="stepSet('+s.id+',\'input\',this.value)">'+fileOpts(s.input)+'</select></div>'+
           '<div class="field"><label>Start time</label><input type="text" value="'+escHtml(s.start||'00:00:00')+'" placeholder="00:00:00" oninput="stepSet('+s.id+',\'start\',this.value)"></div>'+
           '<div class="field"><label>End time</label><input type="text" value="'+escHtml(s.end||'')+'" placeholder="leave empty = till end" oninput="stepSet('+s.id+',\'end\',this.value)"></div>'+
           '<div class="field"><label>Mode</label><select onchange="stepSet('+s.id+',\'mode\',this.value)">'+
           '<option value="fast"'+(s.mode==='fast'?' selected':'')+'>Fast — stream copy (cut points ±few seconds)</option>'+
           '<option value="accurate"'+(s.mode==='accurate'?' selected':'')+'>Accurate — re-encode (exact, slower)</option>'+
           '</select></div>';
    } else if (s.op==='logo') {
      var corners=[['tl','Top-Left'],['tr','Top-Right ⭐'],['bl','Bottom-Left'],['br','Bottom-Right'],['center','Center']];
      body='<div class="field"><label>Video</label><select onchange="stepSet('+s.id+',\'input\',this.value)">'+fileOpts(s.input)+'</select></div>'+
           '<div class="field"><label>Logo image</label><select onchange="stepSet('+s.id+',\'logo\',this.value)"><option value="">— select image —</option>'+fileOpts(s.logo)+'</select></div>'+
           '<div class="field"><label>Position</label><div class="corner-grid">'+
           corners.map(function(c){return '<button class="corner-btn'+(s.corner===c[0]?' active':'')+'" onclick="stepSet('+s.id+',\'corner\',\''+c[0]+'\');renderPipeline()">'+c[1]+'</button>';}).join('')+
           '</div></div>'+
           '<div class="field"><label>Size %</label><input type="number" value="'+(s.size_pct||12)+'" min="1" max="100" oninput="stepSet('+s.id+',\'size_pct\',parseInt(this.value))"></div>';
    } else if (s.op==='merge') {
      var rows=(s.inputs||[]).map(function(inp,i){
        return '<div class="merge-row"><select onchange="mergeSet('+s.id+','+i+',this.value)"><option value="">— select video —</option>'+fileOpts(inp)+'</select>'+
               (i>=2?'<button class="icon-btn del" onclick="mergeDel('+s.id+','+i+')">✕</button>':'')+
               '</div>';
      }).join('');
      body='<div class="merge-inputs">'+rows+'</div>'+
           '<button class="btn-small" onclick="mergeAdd('+s.id+')">+ Add another video</button>';
    } else if (s.op==='audio_cut') {
      var audioOpts = FILES.filter(function(f){ return /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f); });
      var audioSel = audioOpts.map(function(f){ return '<option value="'+escHtml(f)+'"'+(f===s.input?' selected':'')+'>'+escHtml(f)+'</option>'; }).join('');
      body='<div class="field"><label>Audio file</label><select onchange="stepSet('+s.id+',\'input\',this.value)"><option value="">— select audio —</option>'+audioSel+'</select></div>'+
           '<div class="field"><label>Start time</label><input type="text" value="'+escHtml(s.start||'00:00:00')+'" placeholder="00:00:00" oninput="stepSet('+s.id+',\'start\',this.value)"></div>'+
           '<div class="field"><label>End time</label><input type="text" value="'+escHtml(s.end||'')+'" placeholder="leave empty = till end" oninput="stepSet('+s.id+',\'end\',this.value)"></div>'+
           '<div class="field"><label>Mode</label><select onchange="stepSet('+s.id+',\'mode\',this.value)">'+
           '<option value="accurate"'+(s.mode==='accurate'?' selected':'')+'>Accurate — re-encode at source bitrate (best quality)</option>'+
           '<option value="fast"'+(s.mode==='fast'?' selected':'')+'>Fast — stream copy (near-exact, no re-encode)</option>'+
           '</select></div>';
    } else if (s.op==='convert') {
      var vidOpts = FILES.filter(function(f){ return /\.(mp4|mov|mkv|avi|webm)$/i.test(f); });
      var vidSel = vidOpts.map(function(f){ return '<option value="'+escHtml(f)+'"'+(f===s.input?' selected':'')+'>'+escHtml(f)+'</option>'; }).join('');
      body='<div class="field"><label>Video file</label><select onchange="stepSet('+s.id+',\'input\',this.value)"><option value="">— select video —</option>'+vidSel+'</select></div>'+
           '<div class="field"><label>MP3 quality</label><select onchange="stepSet('+s.id+',\'quality\',this.value)">'+
           '<option value="128k"'+(s.quality==='128k'?' selected':'')+'>128 kbps — smaller file, good for speech</option>'+
           '<option value="192k"'+(s.quality==='192k'?' selected':'')+'>192 kbps — balanced (recommended)</option>'+
           '<option value="320k"'+(s.quality==='320k'?' selected':'')+'>320 kbps — highest quality</option>'+
           '</select></div>';
    }
    return '<div class="step-card">'+
           '<div class="step-head"><span class="step-num">'+(idx+1)+'</span><span class="step-label">'+labels[s.op]+'</span><div class="step-acts">'+acts+'</div></div>'+
           '<div class="step-body">'+body+'</div></div>';
  }).join('');
}

// ── run ───────────────────────────────────────────────────────────────────────
function renderRunSummary() {
  var el = document.getElementById('run-summary');
  var btn = document.getElementById('run-btn');
  if (!PIPE.length) {
    el.innerHTML='<div class="empty-state">Add operations in ⚙️ Pipeline first</div>';
    btn.disabled=true; return;
  }
  btn.disabled=false;
  var items=PIPE.map(function(s,i){
    var desc=s.op==='cut'?'Cut <em>'+escHtml(s.input)+'</em>':
             s.op==='logo'?'Logo on <em>'+escHtml(s.input)+'</em>':
             s.op==='audio_cut'?'Cut audio <em>'+escHtml(s.input)+'</em>':
             s.op==='convert'?'Convert <em>'+escHtml(s.input)+'</em> → MP3 @ '+(s.quality||'192k'):
             'Merge '+((s.inputs||[]).filter(Boolean).length)+' videos';
    var icons={'cut':'✂️ ','logo':'🏷️ ','merge':'🔗 ','audio_cut':'✂️ ','convert':'🎵 '};
    return '<div class="ps-item"><span class="ps-num">'+(i+1)+'</span><span>'+(icons[s.op]||'')+desc+'</span></div>';
  }).join('');
  el.innerHTML='<div class="run-summary"><h3>'+FILES.length+' file(s) uploaded · '+PIPE.length+' step(s) queued</h3>'+items+'</div>';
}

async function runPipeline() {
  var btn=document.getElementById('run-btn');
  var progEl=document.getElementById('run-progress');
  var outEl=document.getElementById('run-outputs');
  btn.disabled=true; btn.textContent='Running…';
  progEl.style.display='block';
  outEl.style.display='none';

  var steps=PIPE.map(function(s){
    var step={op:s.op};
    if(s.op==='cut') Object.assign(step,{input:s.input,start:s.start,end:s.end,mode:s.mode});
    else if(s.op==='logo') Object.assign(step,{input:s.input,logo:s.logo,corner:s.corner,size_pct:s.size_pct});
    else if(s.op==='merge') step.inputs=(s.inputs||[]).filter(Boolean);
    else if(s.op==='audio_cut') Object.assign(step,{input:s.input,start:s.start,end:s.end,mode:s.mode});
    else if(s.op==='convert') Object.assign(step,{input:s.input,quality:s.quality});
    return step;
  });

  try {
    var r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:SID,steps:steps})});
    var d=await r.json();
    if(d.error) throw new Error(d.error);
    pollJob(d.job_id, btn, progEl, outEl);
  } catch(err) {
    progEl.innerHTML='<div class="error-banner">'+escHtml(err.message)+'</div>';
    btn.disabled=false; btn.textContent='▶ Run Pipeline';
  }
}

function pollJob(jid, btn, progEl, outEl) {
  progEl.innerHTML='<div class="progress-box"><div class="log-box" id="log-box"></div></div>';
  var logBox=document.getElementById('log-box');
  var lastLen=0;

  function poll() {
    fetch('/job/'+jid).then(function(r){return r.json();}).then(function(j){
      if(j.log && j.log.length>lastLen){
        j.log.slice(lastLen).forEach(function(line){
          var div=document.createElement('div');
          div.className=line.startsWith('▶')?'log-step':line.startsWith('✓')?'log-ok':'log-line';
          div.textContent=line;
          logBox.appendChild(div);
          logBox.scrollTop=logBox.scrollHeight;
        });
        lastLen=j.log.length;
      }
      if(!j.done){setTimeout(poll,900);return;}
      btn.disabled=false; btn.textContent='▶ Run Pipeline';
      if(j.error){
        logBox.insertAdjacentHTML('beforeend','<div class="log-err">✗ '+escHtml(j.error)+'</div>');
        return;
      }
      outEl.style.display='block';
      outEl.innerHTML='<div class="downloads"><h3>✅ Pipeline complete — your files are ready</h3>'+
        j.outputs.map(function(f){
          return '<a href="/download/'+encodeURIComponent(SID)+'/'+encodeURIComponent(f)+'" download="'+escHtml(f)+'" class="dl-btn">'+
                 '<span class="dl-icon">⬇️</span><span>'+escHtml(f)+'</span></a>';
        }).join('')+'</div>';
      // refresh file list
      fetch('/files/'+SID).then(function(r){return r.json();}).then(function(d){
        FILES=d.files||FILES; renderFileList(); setInfo(FILES.length+' file(s) in session');
      });
    }).catch(function(e){setTimeout(poll,2000);});
  }
  poll();
}

function escHtml(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

initSession();
</script>
</body>
</html>"""

# ── entry point ───────────────────────────────────────────────────────────────
if __name__ == '__main__':
    if not FFMPEG:
        print("\n  WARNING: FFmpeg not found in PATH.")
        print("  On Mac: brew install ffmpeg")
        print("  On Linux: sudo apt install ffmpeg")
        print("  On Windows: place ffmpeg.exe in ffmpeg_bin/ folder (already done if you used the original editor)\n")
    else:
        print(f"\n  FFmpeg: {FFMPEG}")

    import socket
    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = '127.0.0.1'

    print(f"\n  +--------------------------------------------------+")
    print(f"  |  RishiPraveen Editor  -  Web / Cloud Edition    |")
    print(f"  +--------------------------------------------------+")
    print(f"  |  Local:   http://localhost:{PORT}                  |")
    print(f"  |  Network: http://{local_ip}:{PORT}            |")
    print(f"  +--------------------------------------------------+")
    print(f"\n  Press Ctrl+C to stop.\n")

    server = HTTPServer((HOST, PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
