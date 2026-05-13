#!/usr/bin/env python3
"""
RishiPraveen Editor  Web Edition
ArhamVijja Social Welfare Organisation
Upload  Cut / Merge / Logo / Audio Cut / MP3  Download
Accessible on any device over the internet.
"""
import os, sys, json, threading, uuid, time, shutil, subprocess, tempfile, re, base64
from http.server import HTTPServer, BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer
except ImportError:
    from socketserver import ThreadingMixIn
    class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True
from urllib.parse import urlparse

PORT     = int(os.environ.get('PORT', 7892))
HOST     = '0.0.0.0'
MAX_UP   = 2 * 1024 * 1024 * 1024  # 2 GB per file

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
WORK_DIR    = os.path.join(tempfile.gettempdir(), 'rp_sessions')
os.makedirs(WORK_DIR, exist_ok=True)

GURUDEV_VIDEO   = os.path.join(SCRIPT_DIR, 'Gurudev_Ashirwaad.mp4')
GURUDEV_PHOTO   = os.path.join(SCRIPT_DIR, 'gurudev_photo.jpg')
ARHAMVIJJA_LOGO = os.path.join(SCRIPT_DIR, 'arhamvijja_logo.png')

_sessions: dict = {}
_jobs: dict     = {}
_L = threading.Lock()

# - session management -
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
        s['files'][safe] = path
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

# - ffmpeg -
def _find_ff():
    if shutil.which('ffmpeg'):
        return shutil.which('ffmpeg'), shutil.which('ffprobe')
    if sys.platform == 'win32':
        ff = os.path.join(SCRIPT_DIR, 'ffmpeg_bin', 'ffmpeg.exe')
        fp = os.path.join(SCRIPT_DIR, 'ffmpeg_bin', 'ffprobe.exe')
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

def to_seconds(s):
    if not s:
        return 0.0
    parts = str(s).strip().split(':')
    try:
        if len(parts) == 3:
            return float(parts[0])*3600 + float(parts[1])*60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0])*60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0

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

# - pipeline operations -

def _unique_out(s, name):
    base, ext = os.path.splitext(name)
    path = os.path.join(s['dir'], name)
    i = 1
    while os.path.exists(path):
        name = f"{base}_{i}{ext}"
        path = os.path.join(s['dir'], name)
        i += 1
    return name, path

def op_cut_multi(s, step, jid):
    """Cut multiple ranges to REMOVE from a video."""
    inp  = step.get('input', '')
    src  = s['files'].get(inp)
    if not src:
        return None, f"File not found: {inp}"

    ranges = step.get('ranges', [])
    mode   = step.get('mode', 'fast')

    info = probe(src)
    dur  = float(info.get('format', {}).get('duration', 0) or 0)

    # Sort cut ranges
    cut_segs = sorted(
        [(to_seconds(r.get('start', '0')), to_seconds(r.get('end', '0'))) for r in ranges],
        key=lambda x: x[0]
    )

    # Compute kept segments (gaps between removed ranges)
    kept = []
    prev = 0.0
    for cs, ce in cut_segs:
        if cs > prev + 0.01:
            kept.append((prev, cs))
        prev = max(prev, ce)
    end_of_file = dur if dur > 0 else None
    if end_of_file is None or prev < end_of_file - 0.01:
        kept.append((prev, end_of_file))

    if not kept:
        return None, "Nothing to keep after applying all cut ranges"

    base, ext = os.path.splitext(inp)
    out_name, out_path = _unique_out(s, f"{base}_cut{ext}")

    if len(kept) == 1 and not cut_segs:
        shutil.copy2(src, out_path)
    elif len(kept) == 1:
        ks, ke = kept[0]
        args = ['-y', '-i', src, '-ss', str(ks)]
        if ke is not None:
            args += ['-to', str(ke)]
        if mode == 'accurate':
            args += ['-c:v', 'libx264', '-c:a', 'aac']
        else:
            args += ['-c', 'copy']
        args.append(out_path)
        rc, _ = ff_run(args, jid)
        if rc != 0 or not os.path.exists(out_path):
            return None, f"FFmpeg cut failed (exit {rc})"
    else:
        seg_paths = []
        for idx, (ks, ke) in enumerate(kept):
            seg_name = f"_seg_{idx}_{uuid.uuid4().hex[:4]}{ext}"
            seg_path = os.path.join(s['dir'], seg_name)
            args = ['-y', '-i', src, '-ss', str(ks)]
            if ke is not None:
                args += ['-to', str(ke)]
            if mode == 'accurate':
                args += ['-c:v', 'libx264', '-c:a', 'aac']
            else:
                args += ['-c', 'copy']
            args.append(seg_path)
            rc, _ = ff_run(args, jid)
            if rc != 0 or not os.path.exists(seg_path):
                for p in seg_paths:
                    try: os.remove(p)
                    except: pass
                return None, f"Segment {idx+1} extraction failed (exit {rc})"
            seg_paths.append(seg_path)

        list_path = os.path.join(s['dir'], f"_cl_{uuid.uuid4().hex[:6]}.txt")
        with open(list_path, 'w') as f:
            for p in seg_paths:
                f.write(f"file '{p}'\n")

        rc, _ = ff_run(['-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', out_path], jid)
        for p in seg_paths:
            try: os.remove(p)
            except: pass
        try: os.remove(list_path)
        except: pass

        if rc != 0 or not os.path.exists(out_path):
            return None, "FFmpeg concat after cut failed"

    with _L:
        s['files'][out_name] = out_path
    return out_name, None


def op_logo_multi(s, step, jid):
    """Burn logo with multiple timing windows."""
    inp      = step.get('input', '')
    logo_inp = step.get('logo', '')
    src      = s['files'].get(inp)
    logo_src = s['files'].get(logo_inp)
    if not src:
        return None, f"Video not found: {inp}"
    if not logo_src:
        return None, f"Logo image not found: {logo_inp}"

    corner   = step.get('corner', 'tr')
    size_pct = max(1, min(100, int(step.get('size_pct', 12))))
    windows  = step.get('windows', [])
    pos_mode = step.get('pos_mode', 'corner')
    px_pct   = float(step.get('px_pct', 50) or 50)
    py_pct   = float(step.get('py_pct', 50) or 50)

    if pos_mode == 'custom':
        pos = f'W*{px_pct:.4f}/100-w/2:H*{py_pct:.4f}/100-h/2'
    else:
        positions = {
            'tl': '20:20',
            'tr': 'W-w-20:20',
            'bl': '20:H-h-20',
            'br': 'W-w-20:H-h-20',
            'center': '(W-w)/2:(H-h)/2',
        }
        pos = positions.get(corner, positions['tr'])

    # Build enable expression from windows
    enable = ''
    valid = []
    for w in windows:
        ts = to_seconds(w.get('t_start', '') or '0')
        te = to_seconds(w.get('t_end', '') or '0')
        if te > ts:
            valid.append(f"between(t,{ts},{te})")
        elif ts > 0 and te == 0:
            valid.append(f"gte(t,{ts})")
    if valid:
        enable = f":enable='{'+'.join(valid)}'"

    vf = f"[1:v]scale=iw*{size_pct}/100:-1[lg];[0:v][lg]overlay={pos}{enable}"

    base, _ = os.path.splitext(inp)
    out_name, out_path = _unique_out(s, f"{base}_logo.mp4")

    args = ['-y', '-i', src, '-i', logo_src,
            '-filter_complex', vf, '-c:v', 'libx264', '-c:a', 'copy', out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"Logo burn failed (exit {rc})"

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
        return None, f"Files not found in session: {missing}"

    base, _ = os.path.splitext(inputs[0])
    out_name, out_path = _unique_out(s, f"{base}_merged.mp4")

    ref_idx = int(step.get('ref_idx', -1))

    if ref_idx >= 0 and ref_idx < len(paths):
        # Reference-based re-encode: scale all clips to reference resolution
        ref_info = probe(paths[ref_idx])
        ref_w, ref_h = 1920, 1080
        vid_bps = None
        aud_bps = None
        for st in ref_info.get('streams', []):
            if st.get('codec_type') == 'video':
                ref_w = int(st.get('width', 1920))
                ref_h = int(st.get('height', 1080))
                b = st.get('bit_rate') or ref_info.get('format', {}).get('bit_rate')
                if b:
                    vid_bps = int(b)
            elif st.get('codec_type') == 'audio':
                ab = st.get('bit_rate')
                if ab:
                    aud_bps = int(ab)
        if vid_bps is None:
            vid_bps = 4000000
        if aud_bps is None:
            aud_bps = 128000

        has_audio = []
        for p in paths:
            info = probe(p)
            has_audio.append(any(st.get('codec_type') == 'audio' for st in info.get('streams', [])))

        filter_parts = []
        v_labels = []
        a_labels = []
        for i, (p, ha) in enumerate(zip(paths, has_audio)):
            filter_parts.append(
                f"[{i}:v]scale={ref_w}:{ref_h}:force_original_aspect_ratio=decrease,"
                f"pad={ref_w}:{ref_h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[v{i}]"
            )
            v_labels.append(f"[v{i}]")
            if ha:
                filter_parts.append(
                    f"[{i}:a]aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
                )
            else:
                filter_parts.append(
                    f"aevalsrc=0:c=stereo:s=48000:d=1,aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}]"
                )
            a_labels.append(f"[a{i}]")

        n = len(paths)
        filter_parts.append(
            f"{''.join(v_labels)}{''.join(a_labels)}concat=n={n}:v=1:a=1[outv][outa]"
        )

        args = ['-y']
        for p in paths:
            args += ['-i', p]
        args += [
            '-filter_complex', ';'.join(filter_parts),
            '-map', '[outv]', '-map', '[outa]',
            '-c:v', 'libx264', '-preset', 'veryfast', '-b:v', str(vid_bps),
            '-c:a', 'aac', '-b:a', str(aud_bps),
            out_path
        ]
        rc, _ = ff_run(args, jid)
    else:
        # Lossless stream copy — files must share codec/resolution
        list_path = os.path.join(s['dir'], f"_cl_{uuid.uuid4().hex[:6]}.txt")
        with open(list_path, 'w') as f:
            for p in paths:
                f.write(f"file '{p}'\n")
        rc, _ = ff_run(['-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', out_path], jid)
        try: os.remove(list_path)
        except: pass

    if rc != 0 or not os.path.exists(out_path):
        return None, f"FFmpeg merge failed (exit {rc})"

    with _L:
        s['files'][out_name] = out_path
    return out_name, None


def op_audio_cut_multi(s, step, jid):
    """Cut multiple ranges to REMOVE from an audio file."""
    inp  = step.get('input', '')
    src  = s['files'].get(inp)
    if not src:
        return None, f"File not found: {inp}"

    ranges = step.get('ranges', [])
    mode   = step.get('mode', 'accurate')

    info = probe(src)
    dur  = float(info.get('format', {}).get('duration', 0) or 0)

    cut_segs = sorted(
        [(to_seconds(r.get('start', '0')), to_seconds(r.get('end', '0'))) for r in ranges],
        key=lambda x: x[0]
    )

    kept = []
    prev = 0.0
    for cs, ce in cut_segs:
        if cs > prev + 0.01:
            kept.append((prev, cs))
        prev = max(prev, ce)
    end_of_file = dur if dur > 0 else None
    if end_of_file is None or prev < end_of_file - 0.01:
        kept.append((prev, end_of_file))

    if not kept:
        return None, "Nothing to keep after applying all cut ranges"

    base, ext = os.path.splitext(inp)
    out_ext = ext.lower() if ext.lower() in ('.mp3', '.aac', '.wav', '.flac', '.m4a') else '.mp3'

    # Determine bitrate for accurate mode
    bitrate = '192k'
    if mode == 'accurate':
        src_a = next((st for st in info.get('streams', []) if st.get('codec_type') == 'audio'), None)
        aud_bps = int((src_a or {}).get('bit_rate', 0) or 0)
        if not aud_bps:
            aud_bps = int(info.get('format', {}).get('bit_rate', 0) or 0)
        if aud_bps > 32_000:
            bitrate = str(aud_bps)

    out_name, out_path = _unique_out(s, f"{base}_cut{out_ext}")

    if len(kept) == 1 and not cut_segs:
        shutil.copy2(src, out_path)
    elif len(kept) == 1:
        ks, ke = kept[0]
        args = ['-y', '-ss', str(ks), '-i', src]
        if ke is not None:
            args += ['-to', str(ke)]
        args += ['-vn']
        if mode == 'accurate':
            args += ['-c:a', 'libmp3lame', '-b:a', bitrate]
        else:
            args += ['-c:a', 'copy']
        args.append(out_path)
        rc, _ = ff_run(args, jid)
        if rc != 0 or not os.path.exists(out_path):
            return None, f"FFmpeg audio cut failed (exit {rc})"
    else:
        seg_paths = []
        for idx, (ks, ke) in enumerate(kept):
            seg_name = f"_aseg_{idx}_{uuid.uuid4().hex[:4]}{out_ext}"
            seg_path = os.path.join(s['dir'], seg_name)
            args = ['-y', '-ss', str(ks), '-i', src]
            if ke is not None:
                args += ['-to', str(ke)]
            args += ['-vn']
            if mode == 'accurate':
                args += ['-c:a', 'libmp3lame', '-b:a', bitrate]
            else:
                args += ['-c:a', 'copy']
            args.append(seg_path)
            rc, _ = ff_run(args, jid)
            if rc != 0 or not os.path.exists(seg_path):
                for p in seg_paths:
                    try: os.remove(p)
                    except: pass
                return None, f"Audio segment {idx+1} failed (exit {rc})"
            seg_paths.append(seg_path)

        list_path = os.path.join(s['dir'], f"_cl_{uuid.uuid4().hex[:6]}.txt")
        with open(list_path, 'w') as f:
            for p in seg_paths:
                f.write(f"file '{p}'\n")

        rc, _ = ff_run(['-y', '-f', 'concat', '-safe', '0', '-i', list_path, '-c', 'copy', out_path], jid)
        for p in seg_paths:
            try: os.remove(p)
            except: pass
        try: os.remove(list_path)
        except: pass

        if rc != 0 or not os.path.exists(out_path):
            return None, "FFmpeg audio concat failed"

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
        return None, f"File not found: {inp}"
    base, _ = os.path.splitext(inp)
    out_name, out_path = _unique_out(s, f"{base}.mp3")
    args = ['-y', '-i', src, '-vn', '-c:a', 'libmp3lame', '-b:a', quality, out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"Conversion failed (exit {rc})"
    with _L:
        s['files'][out_name] = out_path
    return out_name, None


def op_compress(s, step, jid):
    inp = step.get('input', '')
    src = s['files'].get(inp)
    if not src:
        return None, f"File not found: {inp}"
    crf = int(step.get('crf', 23))
    if crf not in (18, 23, 28):
        crf = 23
    base, _ = os.path.splitext(inp)
    out_name, out_path = _unique_out(s, f"{base}_compressed.mp4")
    args = ['-y', '-i', src,
            '-c:v', 'libx264', '-crf', str(crf), '-preset', 'slow',
            '-c:a', 'copy', '-movflags', '+faststart', out_path]
    rc, _ = ff_run(args, jid)
    if rc != 0 or not os.path.exists(out_path):
        return None, f"Compression failed (exit {rc})"
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
    ops_map = {
        'cut_multi':       op_cut_multi,
        'logo_multi':      op_logo_multi,
        'merge':           op_merge,
        'audio_cut_multi': op_audio_cut_multi,
        'convert':         op_convert,
        'compress':        op_compress,
    }
    n = len(steps)
    prev_output = None  # auto-chain: output of step N feeds into step N+1
    for i, step in enumerate(steps):
        # Resolve __prev__ placeholder to the previous step's output filename
        step = dict(step)
        if step.get('input') == '__prev__' and prev_output:
            step['input'] = prev_output
        if 'inputs' in step:
            step['inputs'] = [
                prev_output if (x == '__prev__' and prev_output) else x
                for x in step.get('inputs', [])
            ]
        msg = f"▶ Step {i+1}/{n}: {step['op'].upper()}"
        with _L:
            _jobs[jid].setdefault('log', []).append(msg)
            _jobs[jid]['current_step'] = i
        fn = ops_map.get(step['op'])
        if not fn:
            with _L:
                _jobs[jid].update({'done': True, 'error': f"Unknown operation: {step['op']}"})
            return
        out_name, err = fn(s, step, jid)
        if err:
            with _L:
                _jobs[jid].update({'done': True, 'error': err})
            return
        outputs.append(out_name)
        prev_output = out_name
        with _L:
            _jobs[jid].setdefault('log', []).append(f"[OK] Done: {out_name}")
    with _L:
        _jobs[jid].update({'done': True, 'outputs': outputs, 'error': None})

# - multipart parser -
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

# - HTTP handler -
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

    def serve_file_with_range(self, fpath, ctype):
        if not os.path.exists(fpath):
            self.send_json({'error': 'not found'}, 404)
            return
        size = os.path.getsize(fpath)
        range_hdr = self.headers.get('Range', '')
        if range_hdr and range_hdr.startswith('bytes='):
            try:
                r = range_hdr[6:]
                s_str, e_str = r.split('-')
                start = int(s_str) if s_str else 0
                end   = int(e_str) if e_str else size - 1
                end   = min(end, size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', ctype)
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                with open(fpath, 'rb') as f:
                    f.seek(start)
                    remaining = length
                    while remaining > 0:
                        chunk = f.read(min(65536, remaining))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except Exception:
                self.send_response(400)
                self.end_headers()
        else:
            self.send_response(200)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(size))
            self.send_header('Accept-Ranges', 'bytes')
            self.end_headers()
            with open(fpath, 'rb') as f:
                shutil.copyfileobj(f, self.wfile)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers',
                         'Content-Type, X-Session-Id, X-Upload-Id, X-Chunk-Index')
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

        elif p == '/static/gurudev_video.mp4':
            self.serve_file_with_range(GURUDEV_VIDEO, 'video/mp4')

        elif p == '/static/gurudev_photo.jpg':
            self.serve_file_with_range(GURUDEV_PHOTO, 'image/jpeg')

        elif p == '/static/arhamvijja_logo.png':
            self.serve_file_with_range(ARHAMVIJJA_LOGO, 'image/png')

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
            ext  = os.path.splitext(fname)[1].lower()
            ctype = {
                '.mp4': 'video/mp4', '.mov': 'video/quicktime',
                '.mkv': 'video/x-matroska', '.avi': 'video/x-msvideo',
                '.mp3': 'audio/mpeg', '.aac': 'audio/aac',
                '.wav': 'audio/wav', '.flac': 'audio/flac',
                '.m4a': 'audio/mp4',
            }.get(ext, 'application/octet-stream')
            size = os.path.getsize(fpath)
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

        elif p == '/upload_chunk':
            sid    = self.headers.get('X-Session-Id', '')
            uid    = self.headers.get('X-Upload-Id', '')
            idx    = int(self.headers.get('X-Chunk-Index', '0') or 0)
            length = int(self.headers.get('Content-Length', 0))
            s = get_session(sid)
            if not s or not uid or length == 0:
                self.send_json({'error': 'Bad request'}, 400)
                return
            data = self.rfile.read(length)
            chunk_dir = os.path.join(s['dir'], '.chunks', uid)
            os.makedirs(chunk_dir, exist_ok=True)
            with open(os.path.join(chunk_dir, f'{idx:06d}'), 'wb') as f:
                f.write(data)
            self.send_json({'ok': True})

        elif p == '/upload_done':
            body         = json.loads(self.rfile.read(length))
            sid          = body.get('session_id', '')
            uid          = body.get('upload_id', '')
            orig_name    = body.get('filename', 'file')
            total_chunks = int(body.get('total_chunks', 1))
            s = get_session(sid)
            if not s:
                self.send_json({'error': 'Invalid or expired session'}, 400)
                return
            safe = re.sub(r'[^\w.\-]', '_', os.path.basename(orig_name)) or 'file'
            final_path = os.path.join(s['dir'], safe)
            if os.path.exists(final_path):
                base, ext = os.path.splitext(safe)
                safe = f"{base}_{uuid.uuid4().hex[:4]}{ext}"
                final_path = os.path.join(s['dir'], safe)
            chunk_dir = os.path.join(s['dir'], '.chunks', uid)
            try:
                with open(final_path, 'wb') as out:
                    for ci in range(total_chunks):
                        cp = os.path.join(chunk_dir, f'{ci:06d}')
                        with open(cp, 'rb') as f:
                            shutil.copyfileobj(f, out)
                shutil.rmtree(chunk_dir, ignore_errors=True)
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
                return
            with _L:
                s['files'][safe] = final_path
            self.send_json({'saved': safe, 'files': list(s['files'].keys())})

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

        elif p == '/frame':
            body      = json.loads(self.rfile.read(length))
            sid       = body.get('session_id', '')
            vid_name  = body.get('input', '')
            logo_name = body.get('logo', '')
            seek_raw  = body.get('seek', '5')
            try:    seek_sec = str(max(0, float(seek_raw)))
            except: seek_sec = '5'
            s = get_session(sid)
            if not s:
                self.send_json({'error': 'Invalid session'}, 400)
                return
            vid_path  = s['files'].get(vid_name)
            logo_path = s['files'].get(logo_name)
            if not vid_path:
                self.send_json({'error': f'Video not found: {vid_name}'}, 404)
                return
            if not logo_path:
                self.send_json({'error': f'Logo not found: {logo_name}'}, 404)
                return
            tmp_frame = os.path.join(s['dir'], '_preview_frame.jpg')
            try:
                # Probe native resolution
                pr = subprocess.run(
                    [FFPROBE, '-v', 'quiet', '-print_format', 'json', '-show_streams', vid_path],
                    capture_output=True, text=True, timeout=30
                )
                src_info = json.loads(pr.stdout) if pr.stdout else {}
                src_v = next((st for st in src_info.get('streams', [])
                              if st.get('codec_type') == 'video'), None)
                vid_w = int(src_v.get('width',  1920)) if src_v else 1920
                vid_h = int(src_v.get('height', 1080)) if src_v else 1080
                # Extract frame at user-requested time, scale to max 960px wide
                subprocess.run(
                    [FFMPEG, '-y', '-ss', seek_sec, '-i', vid_path,
                     '-vframes', '1', '-vf', 'scale=960:-2', '-q:v', '3', tmp_frame],
                    capture_output=True, text=True, timeout=60
                )
                if not os.path.exists(tmp_frame):
                    raise Exception('Frame extraction failed')
                with open(tmp_frame, 'rb') as f:
                    frame_b64 = base64.b64encode(f.read()).decode()
                ext  = os.path.splitext(logo_path)[1].lower().lstrip('.')
                mime = {'png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg',
                        'webp':'image/webp','gif':'image/gif'}.get(ext, 'image/png')
                with open(logo_path, 'rb') as f:
                    logo_b64 = base64.b64encode(f.read()).decode()
                self.send_json({'frame': frame_b64, 'logo': logo_b64,
                                'logo_mime': mime, 'vid_w': vid_w, 'vid_h': vid_h})
            except Exception as e:
                self.send_json({'error': str(e)}, 500)
            finally:
                try: os.unlink(tmp_frame)
                except: pass

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
                    'done': False, 'current_step': 0,
                    'total_steps': len(steps), 'log': [],
                    'outputs': [], 'error': None,
                }
            threading.Thread(target=run_pipeline, args=(jid, sid, steps), daemon=True).start()
            self.send_json({'job_id': jid})

        else:
            self.send_json({'error': 'not found'}, 404)

# - HTML -
HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1">
<title>RishiPraveen Editor - ArhamVijja</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
:root{--bg:#0a0a0f;--surface:#111118;--card:#16161f;--border:#2a2a3a;
  --accent:#e8ff47;--blue:#47b3ff;--text:#e8e8f0;--muted:#6b6b80;
  --danger:#ff4747;--success:#47ffb0;--accent2:#ff9f47;--radius:12px;}
:root.light{--bg:#f4f4ef;--surface:#ffffff;--card:#ededea;--border:#d2d2c2;
  --accent:#6a7a00;--blue:#0066cc;--text:#18181f;--muted:#5a5a68;
  --danger:#cc2222;--success:#1a7744;--accent2:#b85500;}
*{margin:0;padding:0;box-sizing:border-box;}
body{background:var(--bg);color:var(--text);font-family:'Syne',sans-serif;min-height:100vh;transition:background .2s,color .2s;}
.header{border-bottom:1px solid var(--border);padding:15px 36px;display:flex;align-items:center;gap:14px;background:var(--surface);position:sticky;top:0;z-index:200;}
.logo{font-size:24px;font-weight:800;letter-spacing:-.5px;white-space:nowrap;}.logo span{color:var(--accent);}
.badge{font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;letter-spacing:1px;font-family:'JetBrains Mono',monospace;background:var(--blue);color:#000;flex-shrink:0;}
.av-wrap{position:absolute;left:50%;transform:translateX(-50%);display:flex;align-items:center;pointer-events:none;}
.av-wrap a{pointer-events:auto;}
.av-logo{height:54px;width:auto;display:block;transition:opacity .2s;}.av-logo:hover{opacity:.8;}
.sess-info{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace;white-space:nowrap;flex-shrink:0;margin-left:auto;}
#theme-toggle{width:32px;height:32px;border-radius:50%;border:1px solid var(--border);background:transparent;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;color:var(--text);transition:background .2s;margin-left:6px;}
#theme-toggle:hover{background:var(--border);}
#gurudev-panel{position:fixed;left:max(14px,calc((100vw - 800px)/4 - 108px));top:139px;z-index:400;cursor:pointer;width:216px;display:flex;flex-direction:column;align-items:center;gap:6px;}
#gurudev-panel img{width:216px;border-radius:12px;border:2px solid var(--border);transition:transform .2s,box-shadow .2s;}
#gurudev-panel img:hover{transform:scale(1.04);box-shadow:0 0 20px rgba(245,166,35,.4);}
#gurudev-panel .gd-label{font-size:12px;font-weight:600;color:var(--muted);font-family:'Syne',sans-serif;text-align:center;letter-spacing:.3px;}
@media(max-width:960px){#gurudev-panel{display:none;}}
#gd-modal{display:none;position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,.9);align-items:center;justify-content:center;flex-direction:column;gap:12px;}
#gd-modal.open{display:flex;}
#gd-modal video{width:min(85vw,900px);border-radius:10px;outline:none;}
#gd-modal-close{position:fixed;top:18px;right:24px;font-size:31px;color:#fff;cursor:pointer;opacity:.7;transition:opacity .15s;line-height:1;}
#gd-modal-close:hover{opacity:1;}
.tabs{display:flex;background:var(--surface);border-bottom:1px solid var(--border);position:sticky;top:85px;z-index:199;overflow-x:auto;scrollbar-width:none;}
.tabs::-webkit-scrollbar{display:none;}
.tab{flex:1;min-width:80px;padding:13px 8px;background:none;border:none;border-bottom:2px solid transparent;color:var(--muted);cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:14px;transition:.15s;white-space:nowrap;}
.tab.active{color:var(--accent);border-bottom-color:var(--accent);}
.tab:hover:not(.active){color:var(--text);}
.panel{display:none;padding:20px;max-width:800px;margin:0 auto;}
.panel.active{display:block;}
.card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:16px;overflow:hidden;}
.card-head{padding:13px 18px;display:flex;align-items:center;gap:10px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.015);}
.card-icon{width:28px;height:28px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0;}
.card-title{font-size:14px;font-weight:700;}.card-sub{font-size:12px;color:var(--muted);margin-top:2px;font-family:'JetBrains Mono',monospace;}
.card-body{padding:16px 18px;}
.upload-zone{border:2px dashed var(--border);border-radius:var(--radius);padding:32px 20px;text-align:center;cursor:pointer;transition:.15s;background:var(--card);margin-bottom:14px;}
.upload-zone:hover,.upload-zone.drag{border-color:var(--accent);background:rgba(232,255,71,.04);}
.upload-icon{font-size:37px;margin-bottom:8px;}.upload-title{font-size:15px;font-weight:700;margin-bottom:4px;}
.upload-sub{font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.up-item{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 14px;margin-bottom:6px;display:flex;flex-direction:column;gap:5px;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted);}
.up-item-row{display:flex;align-items:center;justify-content:space-between;}
.up-prog-track{height:3px;background:var(--border);border-radius:2px;overflow:hidden;}
.up-prog-fill{height:100%;background:var(--accent);border-radius:2px;transition:width .2s;}
.file-list{display:flex;flex-direction:column;gap:6px;}
.file-item{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 14px;display:flex;align-items:center;gap:10px;}
.file-icon{font-size:19px;flex-shrink:0;}.file-name{font-size:13px;font-family:'JetBrains Mono',monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text);}
.file-badge{font-size:10px;padding:2px 6px;border-radius:4px;font-family:'JetBrains Mono',monospace;font-weight:700;flex-shrink:0;}
.badge-video{background:rgba(71,179,255,.15);color:var(--blue);}.badge-audio{background:rgba(255,159,71,.15);color:var(--accent2);}.badge-image{background:rgba(232,255,71,.15);color:var(--accent);}
.empty-state{text-align:center;padding:30px;color:var(--muted);font-size:13px;font-family:'JetBrains Mono',monospace;}
.field{display:flex;flex-direction:column;gap:5px;margin-bottom:12px;}.field:last-child{margin-bottom:0;}
.field label{font-size:11px;font-weight:700;color:var(--muted);letter-spacing:.6px;text-transform:uppercase;}
.field select,.field input[type=text],.field input[type=number]{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;transition:.15s;width:100%;}
.field select:focus,.field input:focus{border-color:var(--accent);}
.ri{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:8px;animation:fi .2s ease;}
@keyframes fi{from{opacity:0;transform:translateY(-4px)}to{opacity:1;transform:none}}
.rt{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
.ridx{font-family:'JetBrains Mono',monospace;font-size:11px;color:var(--muted);width:18px;text-align:center;flex-shrink:0;}
.ti{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:7px 8px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;width:88px;outline:none;transition:border-color .15s;text-align:center;}
.ti:focus{border-color:var(--accent);}
.rarr{color:var(--muted);font-size:12px;flex-shrink:0;}.rlbl{font-size:11px;color:var(--muted);flex-shrink:0;font-family:'JetBrains Mono',monospace;}
.delbtn{background:transparent;border:none;color:var(--muted);cursor:pointer;font-size:14px;padding:3px 6px;transition:color .15s;flex-shrink:0;border-radius:4px;}
.delbtn:hover{color:var(--danger);}
.refbtn{background:transparent;border:none;cursor:pointer;font-size:14px;padding:3px 6px;transition:all .15s;flex-shrink:0;border-radius:4px;color:var(--muted);opacity:.45;}
.refbtn:hover{opacity:.85;}.refbtn.active{color:#ffd700;opacity:1;}
.addbtn{width:100%;display:flex;align-items:center;justify-content:center;gap:6px;padding:8px;background:transparent;border:1px dashed var(--border);border-radius:8px;color:var(--muted);cursor:pointer;font-family:'Syne',sans-serif;font-size:13px;font-weight:600;transition:all .15s;margin-top:4px;padding:9px;}
.addbtn:hover{border-color:var(--accent);color:var(--accent);}
.mode-row{display:flex;gap:8px;flex-wrap:wrap;}
.mode-opt{display:flex;align-items:center;gap:8px;cursor:pointer;padding:8px 12px;border-radius:8px;border:1px solid var(--border);flex:1;background:var(--surface);min-width:150px;transition:.15s;}
.mode-title{font-size:13px;font-weight:700;}.mode-desc{font-size:11px;color:var(--muted);font-family:'JetBrains Mono',monospace;}
.corner-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:5px;}
.corner-btn{background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:7px;cursor:pointer;font-size:11px;font-family:'JetBrains Mono',monospace;color:var(--muted);transition:.15s;text-align:center;}
.corner-btn.active{border-color:var(--accent);color:var(--accent);background:rgba(232,255,71,.08);}.corner-btn:hover{border-color:var(--text);}
.merge-row{display:flex;gap:6px;align-items:center;margin-bottom:6px;}
.merge-row select{flex:1;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:9px 12px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:13px;outline:none;}
.btn{background:var(--accent);color:#000;border:none;padding:11px 20px;border-radius:8px;font-family:'Syne',sans-serif;font-weight:700;font-size:14px;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px;justify-content:center;}
.btn:hover{filter:brightness(1.1);transform:translateY(-1px);}.btn:disabled{opacity:.4;cursor:not-allowed;transform:none;}
.btn-full{width:100%;padding:13px;margin-top:14px;}
.prog-wrap{margin-top:14px;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:12px;display:none;}
.prog-wrap.show{display:block;}
.prog-bg{height:5px;background:var(--border);border-radius:3px;overflow:hidden;margin-bottom:8px;}
.prog-fill{height:100%;background:var(--accent);border-radius:3px;transition:width .5s;}
.prog-text{font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted);margin-bottom:8px;}
.log-box{font-family:'JetBrains Mono',monospace;font-size:11px;max-height:120px;overflow-y:auto;display:flex;flex-direction:column;gap:1px;}
.done-banner{margin-top:14px;background:rgba(71,255,176,.06);border:1px solid rgba(71,255,176,.3);border-radius:8px;padding:14px 16px;display:none;flex-direction:column;gap:8px;}
.done-banner.show{display:flex;}
.done-title{font-size:15px;font-weight:700;color:var(--success);}
.dl-btn{display:flex;align-items:center;gap:8px;background:var(--surface);border:1px solid var(--success);border-radius:8px;padding:10px 16px;color:var(--success);text-decoration:none;font-family:'Syne',sans-serif;font-weight:700;font-size:13px;transition:.15s;}
.dl-btn:hover{background:rgba(71,255,176,.1);}
.err-box{display:none;margin-top:10px;background:rgba(255,71,71,.1);border:1px solid var(--danger);border-radius:8px;padding:10px 14px;color:var(--danger);font-size:13px;font-family:'JetBrains Mono',monospace;}
.err-box.show{display:block;}
.hint{font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;margin-bottom:10px;padding:8px 10px;background:var(--surface);border-radius:6px;border-left:3px solid var(--border);}
.pip-addbtn{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:9px 14px;color:var(--text);cursor:pointer;font-family:'Syne',sans-serif;font-weight:700;font-size:13px;transition:.15s;display:flex;align-items:center;gap:6px;}
.pip-addbtn:hover{border-color:var(--accent);color:var(--accent);}
.pip-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:12px;overflow:hidden;animation:fi .2s ease;}
.pip-head{padding:11px 16px;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border);background:rgba(255,255,255,.02);}
.pip-num{width:24px;height:24px;border-radius:50%;background:var(--accent);color:#000;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:800;font-family:'JetBrains Mono',monospace;flex-shrink:0;}
.pip-label{font-size:14px;font-weight:700;flex:1;}
.pip-body{padding:14px 16px;display:flex;flex-direction:column;gap:10px;}
.prev-badge{display:inline-flex;align-items:center;gap:6px;background:rgba(232,255,71,.08);border:1px solid rgba(232,255,71,.25);border-radius:6px;padding:7px 12px;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--accent);}
.logo-preview-wrap{margin-top:10px;}
.logo-preview-frame{position:relative;display:inline-block;width:100%;border-radius:8px;overflow:hidden;border:1px solid var(--border);background:#000;line-height:0;}
.logo-preview-frame .frame-img{display:block;width:100%;height:auto;user-select:none;pointer-events:none;}
.logo-drag-img{position:absolute;cursor:move;opacity:.92;user-select:none;touch-action:none;}
.logo-pos-badge{display:inline-flex;align-items:center;gap:5px;font-size:12px;font-family:'JetBrains Mono',monospace;background:rgba(71,255,176,.1);border:1px solid rgba(71,255,176,.3);border-radius:4px;padding:3px 8px;color:var(--success);}
@media(max-width:768px){
  /* Un-anchor the absolutely-centred logo so it participates in normal flow */
  .av-wrap{position:relative;left:auto;transform:none;flex:1;justify-content:center;}
  .sess-info{display:none;}
}
@media(max-width:600px){
  /* ── Header: two-row layout so nothing overlaps ── */
  .header{flex-wrap:wrap;padding:8px 14px;gap:4px 8px;align-items:center;}
  /* Row 1: ArhamVijja logo, full width, centred */
  .av-wrap{order:1;width:100%;flex:none;justify-content:center;padding:4px 0 2px;}
  /* Row 2: logo · badge · [spacer] · gurudev btn · theme toggle */
  .logo{order:11;font-size:17px;flex-shrink:0;}
  .badge{order:12;font-size:9px;padding:2px 5px;flex-shrink:0;}
  .sess-info{display:none;order:13;}
  #gd-mob-btn{order:14;margin-left:auto;flex-shrink:0;}
  #theme-toggle{order:15;width:28px;height:28px;flex-shrink:0;}
  .av-logo{height:28px;}
  /* ── Tab bar: sticky top accounts for taller two-row header (~80px) ── */
  .tabs{top:80px;}
  .tabs .tab{flex:none;font-size:12px;padding:10px 14px;min-width:unset;}
  /* ── Panels & cards ── */
  .panel{padding:12px;}
  .card-body{padding:12px 14px;}
  .card-head{padding:11px 14px;}
  /* ── Upload zone ── */
  .upload-zone{padding:22px 12px;}
  .upload-icon{font-size:28px;}
  .upload-title{font-size:13px;}
  .upload-sub{font-size:10px;}
  /* ── Time-range rows: shrink inputs, allow wrap ── */
  .rt{flex-wrap:wrap;gap:4px;}
  .ti{width:72px;font-size:12px;padding:6px 6px;}
  .rlbl{font-size:10px;}
  .rarr{font-size:11px;}
  /* ── Mode selector ── */
  .mode-row{flex-direction:column;}
  .mode-opt{min-width:0;}
  /* ── Corner grid: 2 columns on mobile ── */
  .corner-grid{grid-template-columns:1fr 1fr;}
  /* ── Merge rows ── */
  .merge-row{flex-wrap:wrap;}
  /* ── Buttons ── */
  .btn{font-size:13px;padding:10px 16px;}
  .btn-full{padding:12px;}
  /* ── Pipeline add buttons ── */
  .pip-addbtn{font-size:12px;padding:7px 10px;}
  /* ── Misc text ── */
  .hint{font-size:11px;}
  .done-title{font-size:13px;}
  .dl-btn{font-size:12px;padding:9px 12px;}
  .card-title{font-size:13px;}
  .card-sub{font-size:11px;}
}
@media(max-width:380px){
  /* ── Very small phones (SE, Moto G, etc.) ── */
  .logo{font-size:14px;}
  .badge{display:none;}
  .ti{width:62px;}
  .tabs{top:76px;}
  .tabs .tab{flex:none;font-size:11px;padding:9px 12px;min-width:unset;}
  .av-logo{height:24px;}
}
/* ── Gurudev mobile header button ── */
#gd-mob-btn{display:none;width:34px;height:34px;border-radius:50%;overflow:hidden;border:2px solid var(--border);padding:0;background:none;cursor:pointer;flex-shrink:0;transition:border-color .2s,box-shadow .2s;}
#gd-mob-btn img{width:100%;height:100%;object-fit:cover;display:block;}
#gd-mob-btn:hover{border-color:rgba(245,166,35,.7);box-shadow:0 0 8px rgba(245,166,35,.35);}
@media(max-width:960px){#gd-mob-btn{display:block;}}
</style>
</head>
<body>

<!-- Gurudev Video Modal -->
<div id="gd-modal" onclick="closeGdModal(event)">
  <span id="gd-modal-close" onclick="closeGdModal()">&#x2715;</span>
  <video id="gd-video" controls playsinline>
    <source src="/static/gurudev_video.mp4" type="video/mp4">
  </video>
</div>

<!-- Gurudev Photo Panel (fixed left, desktop only) -->
<div id="gurudev-panel" onclick="openGdModal()" title="Click to watch Gurudev's Ashirwaad">
  <img src="/static/gurudev_photo.jpg" alt="Gurudev Rishi Praveen">
  <div class="gd-label">Gurudev Rishi Praveen</div>
</div>

<!-- Header -->
<div class="header">
  <div class="logo">RishiPraveen <span>Editor</span></div>
  <span class="badge">CLOUD</span>
  <div class="av-wrap">
    <a href="https://www.arhamvijja.org/" target="_blank" rel="noopener" title="ArhamVijja Social Welfare Organisation">
      <img src="/static/arhamvijja_logo.png" class="av-logo" alt="ArhamVijja">
    </a>
  </div>
  <div class="sess-info" id="sess-info">Starting&#8230;</div>
  <button id="gd-mob-btn" onclick="openGdModal()" title="Gurudev Rishi Praveen — tap to watch Ashirwaad">
    <img src="/static/gurudev_photo.jpg" alt="Gurudev">
  </button>
  <button id="theme-toggle" onclick="toggleTheme()" title="Toggle light/dark mode">
    <svg id="theme-icon" width="15" height="15" viewBox="0 0 18 18" fill="none">
      <circle cx="9" cy="9" r="3.5" stroke="currentColor" stroke-width="1.4"/>
      <path d="M9 1v2M9 15v2M1 9h2M15 9h2M3.2 3.2l1.4 1.4M13.4 13.4l1.4 1.4M13.4 3.2l-1.4 1.4M3.2 13.4l1.4 1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
    </svg>
  </button>
</div>

<!-- Tab Bar -->
<div class="tabs">
  <button class="tab active" data-tab="files"    onclick="showTab('files')">&#128193; Files</button>
  <button class="tab" data-tab="cut"       onclick="showTab('cut')">&#9986;&#65039; Cut</button>
  <button class="tab" data-tab="merge"     onclick="showTab('merge')">&#128279; Merge</button>
  <button class="tab" data-tab="logo"      onclick="showTab('logo')">&#127991;&#65039; Logo</button>
  <button class="tab" data-tab="audio"     onclick="showTab('audio')">&#127925; Audio</button>
  <button class="tab" data-tab="mp3"       onclick="showTab('mp3')">&#128260; MP3</button>
  <button class="tab" data-tab="compress"  onclick="showTab('compress')">&#128065; Compress</button>
  <button class="tab" data-tab="pipeline"  onclick="showTab('pipeline')" style="color:var(--accent);">&#9654; Pipeline</button>
</div>

<!-- FILES PANEL -->
<div id="p-files" class="panel active">
  <div class="upload-zone" id="drop-zone" onclick="document.getElementById('file-inp').click()">
    <div class="upload-icon">&#128228;</div>
    <div class="upload-title">Tap or drag files to upload</div>
    <div class="upload-sub">MP4 MOV MKV AVI &nbsp;&#183;&nbsp; MP3 WAV FLAC M4A &nbsp;&#183;&nbsp; PNG JPG &mdash; up to 800 MB each</div>
  </div>
  <input type="file" id="file-inp" multiple
    accept="video/*,audio/*,image/*,.mp4,.mov,.mkv,.avi,.mp3,.aac,.wav,.flac,.m4a,.ogg,.png,.jpg,.jpeg"
    style="display:none">
  <div id="upload-items"></div>
  <div id="file-list" class="file-list"></div>
</div>

<!-- CUT VIDEO PANEL -->
<div id="p-cut" class="panel">
  <div class="card" style="border-color:rgba(255,71,71,.25);">
    <div class="card-head">
      <div class="card-icon" style="background:var(--danger);">&#9986;&#65039;</div>
      <div><div class="card-title">Cut Video</div>
      <div class="card-sub">Add ranges to REMOVE &mdash; everything else is kept and joined</div></div>
    </div>
    <div class="card-body">
      <div class="field">
        <label>Video File</label>
        <select id="cut-file"><option value="">&#8212; select video &#8212;</option></select>
      </div>
      <div style="font-size:12px;font-weight:700;color:var(--danger);margin-bottom:8px;font-family:'JetBrains Mono',monospace;">&#9986;&#65039; CUT SEGMENTS (ranges to REMOVE)</div>
      <div id="cut-ranges"></div>
      <button class="addbtn" style="border-color:rgba(255,71,71,.4);color:var(--danger);" onclick="cutAddRange()">&#9986;&#65039; Add Cut Segment</button>
      <div style="margin-top:16px;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:8px;letter-spacing:.5px;text-transform:uppercase;">&#9881;&#65039; Cut Mode</div>
      <div class="mode-row" style="margin-bottom:16px;">
        <label class="mode-opt">
          <input type="radio" name="cut-mode" value="fast" checked style="accent-color:var(--accent);">
          <div><div class="mode-title" style="color:var(--accent);">&#9889; Fast</div>
          <div class="mode-desc">Stream copy &mdash; instant, cuts &#177;1 sec</div></div>
        </label>
        <label class="mode-opt">
          <input type="radio" name="cut-mode" value="accurate" style="accent-color:var(--success);">
          <div><div class="mode-title" style="color:var(--success);">&#127919; Accurate</div>
          <div class="mode-desc">Re-encode &mdash; frame-perfect, slower</div></div>
        </label>
      </div>
      <div id="cut-err" class="err-box"></div>
      <button class="btn btn-full" style="background:var(--danger);color:#fff;" onclick="runCut()" id="cut-btn">&#9986;&#65039; Cut &amp; Download</button>
      <div class="prog-wrap" id="cut-prog">
        <div class="prog-bg"><div class="prog-fill" id="cut-fill" style="width:0%;background:var(--danger)"></div></div>
        <div class="prog-text" id="cut-prog-text">Processing&#8230;</div>
        <div class="log-box" id="cut-log"></div>
      </div>
      <div class="done-banner" id="cut-done"></div>
    </div>
  </div>
</div>

<!-- MERGE PANEL -->
<div id="p-merge" class="panel">
  <div class="card" style="border-color:rgba(71,179,255,.25);">
    <div class="card-head">
      <div class="card-icon" style="background:var(--blue);">&#128279;</div>
      <div><div class="card-title">Merge Videos</div>
      <div class="card-sub">Join multiple video files into one &mdash; joined in listed order</div></div>
    </div>
    <div class="card-body">
      <div class="hint">&#9889; <b>Lossless mode</b> (default): files must share codec &amp; resolution &mdash; instant join, no quality loss. &nbsp;&#11088; <b>Reference mode</b>: click &#11088; on one file to set it as reference &mdash; all clips are re-encoded &amp; scaled to match its resolution and bitrate.</div>
      <div style="font-size:12px;font-weight:700;color:var(--blue);margin-bottom:10px;font-family:'JetBrains Mono',monospace;">&#128279; VIDEO FILES (in join order)</div>
      <div id="merge-rows"></div>
      <button class="addbtn" style="border-color:rgba(71,179,255,.4);color:var(--blue);" onclick="mergeAddRow()">&#10133; Add Another Video</button>
      <div id="merge-err" class="err-box"></div>
      <button class="btn btn-full" style="background:var(--blue);color:#000;" onclick="runMerge()" id="merge-btn">&#128279; Merge &amp; Download</button>
      <div class="prog-wrap" id="merge-prog">
        <div class="prog-bg"><div class="prog-fill" id="merge-fill" style="width:0%;background:var(--blue)"></div></div>
        <div class="prog-text" id="merge-prog-text">Processing&#8230;</div>
        <div class="log-box" id="merge-log"></div>
      </div>
      <div class="done-banner" id="merge-done" style="border-color:rgba(71,179,255,.3);background:rgba(71,179,255,.06);"></div>
    </div>
  </div>
</div>

<!-- LOGO PANEL -->
<div id="p-logo" class="panel">
  <div class="card" style="border-color:rgba(232,255,71,.25);">
    <div class="card-head">
      <div class="card-icon" style="background:var(--accent);color:#000;">&#127991;&#65039;</div>
      <div><div class="card-title">Logo / Watermark Overlay</div>
      <div class="card-sub">Burn a logo onto a video &mdash; PNG with transparency works best</div></div>
    </div>
    <div class="card-body">
      <div class="field">
        <label>Video File</label>
        <select id="logo-video"><option value="">&#8212; select video &#8212;</option></select>
      </div>
      <div class="field">
        <label>Logo Image (PNG recommended)</label>
        <select id="logo-img"><option value="">&#8212; select image &#8212;</option></select>
      </div>
      <div class="field">
        <label>Position</label>
        <div class="corner-grid">
          <button class="corner-btn" data-c="tl" onclick="setCorner('tl')">Top-Left</button>
          <button class="corner-btn active" data-c="tr" onclick="setCorner('tr')">Top-Right &#11088;</button>
          <button class="corner-btn" data-c="center" onclick="setCorner('center')">&#10006; Center</button>
          <button class="corner-btn" data-c="bl" onclick="setCorner('bl')">Bot-Left</button>
          <button class="corner-btn" data-c="br" onclick="setCorner('br')">Bot-Right</button>
          <button class="corner-btn" style="visibility:hidden;"></button>
        </div>
      </div>
      <div class="field">
        <label>Logo Size (% of video width, 1&ndash;100)</label>
        <input type="number" id="logo-size" value="12" min="1" max="100" style="max-width:120px;">
      </div>
      <div style="font-size:12px;font-weight:700;color:var(--accent);margin-bottom:6px;font-family:'JetBrains Mono',monospace;">&#8987; LOGO VISIBILITY WINDOWS</div>
      <div class="hint">Leave empty to show logo for the <b style="color:var(--text);">entire video</b>. Add windows to restrict logo to specific time ranges.</div>
      <div id="logo-windows"></div>
      <button class="addbtn" style="border-color:rgba(232,255,71,.4);color:var(--accent);" onclick="logoAddWindow()">&#8987; Add Visibility Window</button>

      <!-- Position Preview -->
      <div style="font-size:12px;font-weight:700;color:var(--accent);margin-top:16px;margin-bottom:6px;font-family:'JetBrains Mono',monospace;">&#9315; POSITION PREVIEW <span style="font-weight:400;color:var(--muted);font-size:11px;">&mdash; drag logo to exact spot (optional)</span></div>
      <div class="hint">Select video &amp; logo above first. Load preview, then drag the logo image to where you want it.</div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;">
        <button class="btn" style="background:var(--surface);border:1px solid var(--border);color:var(--text);font-size:12px;padding:8px 14px;" id="logo-preview-btn" onclick="logoLoadPreview()">&#128444;&#65039; Load Position Preview</button>
        <label style="font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;display:flex;align-items:center;gap:5px;">at&nbsp;<input type="text" id="logo-preview-seek" value="5" placeholder="sec or HH:MM:SS" style="width:100px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:5px 8px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:12px;outline:none;">&nbsp;s</label>
        <span id="logo-preview-status" style="font-size:12px;color:var(--muted);font-family:'JetBrains Mono',monospace;"></span>
        <span id="logo-pos-badge" class="logo-pos-badge" style="display:none;">&#128204; Custom position active</span>
      </div>
      <div id="logo-preview-panel" class="logo-preview-wrap" style="display:none;">
        <div id="logo-preview-frame" class="logo-preview-frame">
          <img id="logo-frame-img" class="frame-img" draggable="false" alt="">
          <img id="logo-drag-img" class="logo-drag-img" draggable="false" alt="" style="display:none;">
        </div>
        <div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
          <button class="btn" style="font-size:12px;padding:9px 16px;background:var(--success);color:#000;" onclick="logoUseCustomPos()">&#9989; Use this position</button>
          <button class="btn" style="font-size:12px;padding:9px 16px;background:transparent;border:1px solid var(--border);color:var(--text);" onclick="logoClearCustomPos()">&#10005; Use corner setting instead</button>
        </div>
      </div>

      <div id="logo-err" class="err-box"></div>
      <button class="btn btn-full" onclick="runLogo()" id="logo-btn">&#127991;&#65039; Burn Logo &amp; Download</button>
      <div class="prog-wrap" id="logo-prog">
        <div class="prog-bg"><div class="prog-fill" id="logo-fill" style="width:0%;background:var(--accent)"></div></div>
        <div class="prog-text" id="logo-prog-text">Processing&#8230;</div>
        <div class="log-box" id="logo-log"></div>
      </div>
      <div class="done-banner" id="logo-done"></div>
    </div>
  </div>
</div>

<!-- AUDIO CUT PANEL -->
<div id="p-audio" class="panel">
  <div class="card" style="border-color:rgba(255,159,71,.25);">
    <div class="card-head">
      <div class="card-icon" style="background:var(--accent2);">&#127925;</div>
      <div><div class="card-title">Audio Cutter</div>
      <div class="card-sub">Cut segments from MP3, AAC, WAV, FLAC, M4A</div></div>
    </div>
    <div class="card-body">
      <div class="field">
        <label>Audio File</label>
        <select id="audio-file"><option value="">&#8212; select audio &#8212;</option></select>
      </div>
      <div style="font-size:12px;font-weight:700;color:var(--accent2);margin-bottom:8px;font-family:'JetBrains Mono',monospace;">&#9986;&#65039; CUT SEGMENTS (ranges to REMOVE)</div>
      <div id="audio-ranges"></div>
      <button class="addbtn" style="border-color:rgba(255,159,71,.4);color:var(--accent2);" onclick="audioAddRange()">&#9986;&#65039; Add Cut Segment</button>
      <div style="margin-top:16px;font-size:11px;font-weight:700;color:var(--muted);margin-bottom:8px;letter-spacing:.5px;text-transform:uppercase;">&#9881;&#65039; Cut Mode</div>
      <div class="mode-row" style="margin-bottom:16px;">
        <label class="mode-opt">
          <input type="radio" name="audio-mode" value="accurate" checked style="accent-color:var(--success);">
          <div><div class="mode-title" style="color:var(--success);">&#127919; Accurate</div>
          <div class="mode-desc">Re-encode at source bitrate &mdash; exact cuts</div></div>
        </label>
        <label class="mode-opt">
          <input type="radio" name="audio-mode" value="fast" style="accent-color:var(--accent);">
          <div><div class="mode-title" style="color:var(--accent);">&#9889; Fast</div>
          <div class="mode-desc">Stream copy &mdash; instant, near-exact</div></div>
        </label>
      </div>
      <div id="audio-err" class="err-box"></div>
      <button class="btn btn-full" style="background:var(--accent2);color:#000;" onclick="runAudio()" id="audio-btn">&#9986;&#65039; Cut Audio &amp; Download</button>
      <div class="prog-wrap" id="audio-prog">
        <div class="prog-bg"><div class="prog-fill" id="audio-fill" style="width:0%;background:var(--accent2)"></div></div>
        <div class="prog-text" id="audio-prog-text">Processing&#8230;</div>
        <div class="log-box" id="audio-log"></div>
      </div>
      <div class="done-banner" id="audio-done" style="border-color:rgba(255,159,71,.3);background:rgba(255,159,71,.06);"></div>
    </div>
  </div>
</div>

<!-- COMPRESS PANEL -->
<div id="p-compress" class="panel">
  <div class="card" style="border-color:rgba(71,179,255,.2);">
    <div class="card-head">
      <div class="card-icon" style="background:var(--blue);color:#000;">&#128065;</div>
      <div><div class="card-title">Video Compressor</div>
      <div class="card-sub">Reduce file size &mdash; audio copied as-is, zero audio quality loss</div></div>
    </div>
    <div class="card-body">
      <div class="hint">Video is re-encoded with CRF (quality-based compression). Audio stream is copied untouched. Choose a level &mdash; lower CRF = larger file but better picture quality.</div>
      <div class="field">
        <label>Video File</label>
        <select id="compress-file"><option value="">&#8212; select video &#8212;</option></select>
      </div>
      <div class="field">
        <label>Compression Level</label>
        <div class="mode-row">
          <label class="mode-opt">
            <input type="radio" name="compress-crf" value="18" style="accent-color:var(--blue);">
            <div><div class="mode-title" style="color:var(--blue);">&#127775; Light &mdash; CRF 18</div>
            <div class="mode-desc">Near-lossless &mdash; ~20% smaller, best picture</div></div>
          </label>
          <label class="mode-opt">
            <input type="radio" name="compress-crf" value="23" checked style="accent-color:var(--accent);">
            <div><div class="mode-title" style="color:var(--accent);">&#9881;&#65039; Balanced &mdash; CRF 23</div>
            <div class="mode-desc">Recommended &mdash; ~40% smaller, imperceptible loss</div></div>
          </label>
          <label class="mode-opt">
            <input type="radio" name="compress-crf" value="28" style="accent-color:var(--success);">
            <div><div class="mode-title" style="color:var(--success);">&#128190; Strong &mdash; CRF 28</div>
            <div class="mode-desc">Most compact &mdash; ~60% smaller, minor softening</div></div>
          </label>
        </div>
      </div>
      <div id="compress-err" class="err-box"></div>
      <button class="btn btn-full" style="background:var(--blue);color:#000;" onclick="runCompress()" id="compress-btn">&#128065; Compress &amp; Download</button>
      <div class="prog-wrap" id="compress-prog">
        <div class="prog-bg"><div class="prog-fill" id="compress-fill" style="width:0%;background:var(--blue)"></div></div>
        <div class="prog-text" id="compress-prog-text">Processing&#8230;</div>
        <div class="log-box" id="compress-log"></div>
      </div>
      <div class="done-banner" id="compress-done" style="border-color:rgba(71,179,255,.3);background:rgba(71,179,255,.06);"></div>
    </div>
  </div>
</div>

<!-- PIPELINE PANEL -->
<div id="p-pipeline" class="panel">
  <div style="background:rgba(232,255,71,.06);border:1px solid rgba(232,255,71,.2);border-radius:var(--radius);padding:12px 16px;margin-bottom:16px;font-size:12px;font-family:'JetBrains Mono',monospace;color:var(--muted);line-height:1.7;">
    <b style="color:var(--accent);">&#9654; Pipeline mode</b> &mdash; chain multiple operations into one automated sequence.<br>
    The output of each step automatically feeds into the next step.<br>
    Upload your files in <b style="color:var(--text);">&#128193; Files</b> first, then add steps below.
  </div>
  <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:16px;">
    <button class="pip-addbtn" onclick="pipAdd('cut_multi')">&#9986;&#65039; Cut Video</button>
    <button class="pip-addbtn" onclick="pipAdd('logo_multi')">&#127991;&#65039; Logo</button>
    <button class="pip-addbtn" onclick="pipAdd('merge')">&#128279; Merge</button>
    <button class="pip-addbtn" onclick="pipAdd('audio_cut_multi')">&#127925; Audio Cut</button>
    <button class="pip-addbtn" onclick="pipAdd('convert')">&#128260; To MP3</button>
    <button class="pip-addbtn" onclick="pipAdd('compress')">&#128065; Compress</button>
  </div>
  <div id="pip-steps"></div>
  <div id="pip-err" class="err-box" style="margin-bottom:12px;"></div>
  <button class="btn btn-full" style="background:var(--accent);color:#000;" onclick="runPipeline()" id="pipeline-btn">&#9654; Run Pipeline &amp; Download</button>
  <div class="prog-wrap" id="pipeline-prog">
    <div class="prog-bg"><div class="prog-fill" id="pipeline-fill" style="width:0%;background:var(--accent)"></div></div>
    <div class="prog-text" id="pipeline-prog-text">Processing&#8230;</div>
    <div class="log-box" id="pipeline-log"></div>
  </div>
  <div class="done-banner" id="pipeline-done"></div>
</div>

<!-- MP3 PANEL -->
<div id="p-mp3" class="panel">
  <div class="card" style="border-color:rgba(71,255,176,.2);">
    <div class="card-head">
      <div class="card-icon" style="background:var(--success);color:#000;">&#128260;</div>
      <div><div class="card-title">MP4 &#8594; MP3 Converter</div>
      <div class="card-sub">Extract audio from any video and save as MP3</div></div>
    </div>
    <div class="card-body">
      <div class="field">
        <label>Video File</label>
        <select id="mp3-file"><option value="">&#8212; select video &#8212;</option></select>
      </div>
      <div class="field">
        <label>MP3 Quality</label>
        <select id="mp3-quality">
          <option value="128k">128 kbps &mdash; smaller file, good for speech</option>
          <option value="192k" selected>192 kbps &mdash; balanced (recommended)</option>
          <option value="320k">320 kbps &mdash; highest quality</option>
        </select>
      </div>
      <div id="mp3-err" class="err-box"></div>
      <button class="btn btn-full" style="background:var(--success);color:#000;" onclick="runMp3()" id="mp3-btn">&#128260; Convert to MP3 &amp; Download</button>
      <div class="prog-wrap" id="mp3-prog">
        <div class="prog-bg"><div class="prog-fill" id="mp3-fill" style="width:0%;background:var(--success)"></div></div>
        <div class="prog-text" id="mp3-prog-text">Processing&#8230;</div>
        <div class="log-box" id="mp3-log"></div>
      </div>
      <div class="done-banner" id="mp3-done" style="border-color:rgba(71,255,176,.3);background:rgba(71,255,176,.06);"></div>
    </div>
  </div>
</div>

<script>
var SID   = '';
var FILES = [];
var CUT_RANGES   = [];
var MERGE_FILES  = ['', ''];
var MERGE_REF_IDX = -1;
var LOGO_WINDOWS = [];
var AUDIO_RANGES = [];
var LOGO_CORNER  = 'tr';
var logoCustomPos = null;   // null = use corner; {x,y} = % of video dims (logo centre)
var _logoDragging = false;
var _logoDragStart = {mx:0,my:0,lx:0,ly:0};
var _pipLogoDragging = null;
var _pipLogoPos = {};       // {stepId: {x,y} | null}
var _lastLogLen  = {};

// Gurudev modal
function openGdModal() {
  var m = document.getElementById('gd-modal');
  var v = document.getElementById('gd-video');
  m.classList.add('open');
  v.play();
  document.addEventListener('keydown', _onModalKey);
}
function closeGdModal(e) {
  if (e && e.target && e.target.id !== 'gd-modal' && e.target.id !== 'gd-modal-close') return;
  var m = document.getElementById('gd-modal');
  var v = document.getElementById('gd-video');
  m.classList.remove('open');
  v.pause(); v.currentTime = 0;
  document.removeEventListener('keydown', _onModalKey);
}
function _onModalKey(e) { if (e.key === 'Escape') { closeGdModal({target:{id:'gd-modal'}}); } }

// Tabs
function showTab(name) {
  document.querySelectorAll('.tab').forEach(function(t){ t.classList.toggle('active', t.dataset.tab===name); });
  document.querySelectorAll('.panel').forEach(function(p){ p.classList.remove('active'); });
  var panel = document.getElementById('p-'+name);
  if (panel) panel.classList.add('active');
  if (name !== 'files') refreshFileSelects();
}

// Session
async function initSession() {
  SID = localStorage.getItem('rp_sid') || '';
  if (SID) {
    try {
      var r = await fetch('/files/'+SID);
      if (r.ok) {
        var d = await r.json();
        FILES = d.files || [];
        renderFileList();
        setInfo(FILES.length + ' file(s) in session');
        return;
      }
    } catch(e) {}
  }
  try {
    var r = await fetch('/session', {method:'POST'});
    var d = await r.json();
    SID = d.session_id;
    localStorage.setItem('rp_sid', SID);
    FILES = [];
    renderFileList();
    setInfo('Session ready');
  } catch(e) { setInfo('Connection failed'); }
}
function setInfo(t) { var el = document.getElementById('sess-info'); if(el) el.textContent = t; }

// Upload
(function(){
  var dz = document.getElementById('drop-zone');
  dz.addEventListener('dragover', function(e){ e.preventDefault(); dz.classList.add('drag'); });
  dz.addEventListener('dragleave', function(){ dz.classList.remove('drag'); });
  dz.addEventListener('drop', function(e){ e.preventDefault(); dz.classList.remove('drag'); uploadFiles(Array.from(e.dataTransfer.files)); });
  document.getElementById('file-inp').addEventListener('change', function(e){ uploadFiles(Array.from(e.target.files)); e.target.value=''; });
})();

function uid4() { return 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'.replace(/x/g,function(){ return (Math.random()*16|0).toString(16); }); }

async function uploadFiles(files) { for(var i=0;i<files.length;i++) await uploadOne(files[i]); }

async function uploadOne(file) {
  var CHUNK = 4*1024*1024, totalChunks = Math.max(1,Math.ceil(file.size/CHUNK)), uploadId = uid4();
  var wrap = document.getElementById('upload-items');
  var el = document.createElement('div'); el.className='up-item';
  el.innerHTML='<div class="up-item-row"><span>'+esc(file.name)+'</span><span id="up-pct-'+uploadId+'">0%</span></div>'+
    '<div class="up-prog-track"><div class="up-prog-fill" id="up-bar-'+uploadId+'" style="width:0%"></div></div>';
  wrap.prepend(el);
  function setPct(p){var pct=Math.round(p*100)+'%';var b=document.getElementById('up-bar-'+uploadId);var t=document.getElementById('up-pct-'+uploadId);if(b)b.style.width=pct;if(t)t.textContent=pct;}
  try {
    for(var i=0;i<totalChunks;i++){
      var chunk=file.slice(i*CHUNK,(i+1)*CHUNK);
      var r=await fetch('/upload_chunk',{method:'POST',headers:{'Content-Type':'application/octet-stream','X-Session-Id':SID,'X-Upload-Id':uploadId,'X-Chunk-Index':String(i)},body:chunk});
      if(!r.ok){var e2=await r.json();throw new Error(e2.error||'Chunk failed');}
      setPct((i+1)/totalChunks);
    }
    var r2=await fetch('/upload_done',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:SID,upload_id:uploadId,filename:file.name,total_chunks:totalChunks})});
    var d=await r2.json();
    if(d.error) throw new Error(d.error);
    FILES=d.files||FILES; renderFileList(); setInfo(FILES.length+' file(s) in session');
    setTimeout(function(){el.remove();},1500);
  } catch(err){
    el.innerHTML='<div class="up-item-row"><span>'+esc(file.name)+'</span><span style="color:var(--danger)">'+esc(String(err.message))+'</span></div>';
    setTimeout(function(){el.remove();},5000);
  }
}

function renderFileList(){
  var el=document.getElementById('file-list');
  if(!FILES.length){el.innerHTML='<div class="empty-state">No files yet &mdash; tap above to upload</div>';return;}
  el.innerHTML=FILES.map(function(f){
    var isV=/\.(mp4|mov|mkv|avi|webm)$/i.test(f),isA=/\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f),isI=/\.(png|jpg|jpeg|webp)$/i.test(f);
    var icon=isV?'&#127916;':isA?'&#127925;':isI?'&#128444;&#65039;':'&#128196;';
    var badge=isV?'<span class="file-badge badge-video">VIDEO</span>':isA?'<span class="file-badge badge-audio">AUDIO</span>':isI?'<span class="file-badge badge-image">IMAGE</span>':'';
    return '<div class="file-item"><span class="file-icon">'+icon+'</span><span class="file-name">'+esc(f)+'</span>'+badge+'</div>';
  }).join('');
}

function fileOpts(list, sel){
  return list.map(function(f){ return '<option value="'+esc(f)+'"'+(f===sel?' selected':'')+'>'+esc(f)+'</option>'; }).join('');
}

function refreshFileSelects(){
  var vids=FILES.filter(function(f){return /\.(mp4|mov|mkv|avi|webm)$/i.test(f);});
  var auds=FILES.filter(function(f){return /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f);});
  var imgs=FILES.filter(function(f){return /\.(png|jpg|jpeg|webp)$/i.test(f);});
  function setOpts(id, list, blank){
    var el=document.getElementById(id); if(!el) return; var cur=el.value;
    el.innerHTML=(blank||'<option value="">&#8212; select &#8212;</option>')+fileOpts(list,cur);
  }
  setOpts('cut-file', vids);
  setOpts('logo-video', vids);
  setOpts('logo-img', imgs, '<option value="">&#8212; select image &#8212;</option>');
  setOpts('audio-file', auds, '<option value="">&#8212; select audio &#8212;</option>');
  setOpts('mp3-file', vids);
  setOpts('compress-file', vids);
  renderMergeRows();
}

// Logo position preview (shared drag handler for both main tab and pipeline)
function _applyDragMove(cx, cy){
  if(_logoDragging){
    var di=document.getElementById('logo-drag-img'),fr=document.getElementById('logo-preview-frame');
    if(di&&fr){
      var nx=Math.max(0,Math.min(_logoDragStart.lx+(cx-_logoDragStart.mx),fr.offsetWidth-di.offsetWidth));
      var ny=Math.max(0,Math.min(_logoDragStart.ly+(cy-_logoDragStart.my),fr.offsetHeight-di.offsetHeight));
      di.style.left=nx+'px';di.style.top=ny+'px';
    }
  }
  if(_pipLogoDragging!==null){
    var pid=_pipLogoDragging;
    var di=document.getElementById('pip-drag-'+pid),fr=document.getElementById('pip-prev-frame-'+pid);
    if(di&&fr){
      var nx=Math.max(0,Math.min(_logoDragStart.lx+(cx-_logoDragStart.mx),fr.offsetWidth-di.offsetWidth));
      var ny=Math.max(0,Math.min(_logoDragStart.ly+(cy-_logoDragStart.my),fr.offsetHeight-di.offsetHeight));
      di.style.left=nx+'px';di.style.top=ny+'px';
    }
  }
}
document.addEventListener('mousemove',function(e){_applyDragMove(e.clientX,e.clientY);});
document.addEventListener('touchmove',function(e){
  if(_logoDragging||_pipLogoDragging!==null){e.preventDefault();var t=e.touches[0];_applyDragMove(t.clientX,t.clientY);}
},{passive:false});
document.addEventListener('mouseup',function(){_logoDragging=false;_pipLogoDragging=null;});
document.addEventListener('touchend',function(){_logoDragging=false;_pipLogoDragging=null;});

function _placeLogoOnFrame(frameImg, dragImg, frameEl, sizePct){
  var fw=frameEl.offsetWidth;
  var logoW=Math.round(fw*sizePct/100);
  dragImg.style.width=logoW+'px'; dragImg.style.height='auto'; dragImg.style.display='block';
  requestAnimationFrame(function(){
    var lw=dragImg.offsetWidth||logoW, lh=dragImg.offsetHeight||Math.round(logoW*0.5);
    var fh=frameEl.offsetHeight;
    dragImg.style.left=Math.round((fw-lw)/2)+'px';
    dragImg.style.top=Math.round((fh-lh)/2)+'px';
  });
}
async function logoLoadPreview(){
  var vid=document.getElementById('logo-video').value;
  var logo=document.getElementById('logo-img').value;
  if(!vid||!logo){showErr('logo','Select both video and logo files first.');return;}
  var seekEl=document.getElementById('logo-preview-seek');
  var seek=(seekEl&&seekEl.value.trim())||'5';
  var btn=document.getElementById('logo-preview-btn');
  var status=document.getElementById('logo-preview-status');
  btn.disabled=true; btn.textContent='Loading…';
  try{
    var r=await fetch('/frame',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:SID,input:vid,logo:logo,seek:seek})});
    var d=await r.json();
    if(d.error)throw new Error(d.error);
    var frameImg=document.getElementById('logo-frame-img');
    var dragImg=document.getElementById('logo-drag-img');
    var frameEl=document.getElementById('logo-preview-frame');
    var sizePct=parseInt(document.getElementById('logo-size').value,10)||12;
    // Reset
    dragImg.style.display='none'; dragImg.onload=null; frameImg.onload=null;
    // Attach drag before setting src
    function _startLogoDrag(cx,cy){_logoDragging=true;_logoDragStart={mx:cx,my:cy,lx:parseInt(dragImg.style.left)||0,ly:parseInt(dragImg.style.top)||0};}
    dragImg.onmousedown=function(e){_startLogoDrag(e.clientX,e.clientY);e.preventDefault();};
    dragImg.ontouchstart=function(e){var t=e.touches[0];_startLogoDrag(t.clientX,t.clientY);e.preventDefault();};
    // Track both images loaded
    var _fDone=false,_lDone=false;
    function _checkBoth(){if(_fDone&&_lDone){_placeLogoOnFrame(frameImg,dragImg,frameEl,sizePct);}}
    frameImg.onload=function(){_fDone=true;document.getElementById('logo-preview-panel').style.display='block';_checkBoth();frameImg.onload=null;};
    dragImg.onload=function(){_lDone=true;_checkBoth();dragImg.onload=null;};
    // Set sources — data: URIs often decode synchronously, check complete after
    frameImg.src='data:image/jpeg;base64,'+d.frame;
    dragImg.src='data:'+d.logo_mime+';base64,'+d.logo;
    if(frameImg.complete&&frameImg.naturalWidth&&!_fDone){frameImg.onload=null;_fDone=true;document.getElementById('logo-preview-panel').style.display='block';_checkBoth();}
    if(dragImg.complete&&dragImg.naturalWidth&&!_lDone){dragImg.onload=null;_lDone=true;_checkBoth();}
    status.textContent='Frame at '+seek+'s · '+d.vid_w+'×'+d.vid_h;
  }catch(e){showErr('logo','Preview failed: '+e.message);}
  finally{btn.disabled=false;btn.innerHTML='🖼️ Load Position Preview';}
}
function logoUseCustomPos(){
  var di=document.getElementById('logo-drag-img'),fr=document.getElementById('logo-preview-frame');
  if(!di||!fr)return;
  var cx=(parseFloat(di.style.left)+di.offsetWidth/2)/fr.offsetWidth*100;
  var cy=(parseFloat(di.style.top)+di.offsetHeight/2)/fr.offsetHeight*100;
  logoCustomPos={x:Math.round(cx*10)/10,y:Math.round(cy*10)/10};
  document.getElementById('logo-pos-badge').style.display='inline-flex';
  document.getElementById('logo-preview-status').textContent=
    'Locked at ('+logoCustomPos.x+'%, '+logoCustomPos.y+'%)';
}
function logoClearCustomPos(){
  logoCustomPos=null;
  document.getElementById('logo-pos-badge').style.display='none';
  document.getElementById('logo-preview-status').textContent='Cleared — corner setting will be used.';
}

// Pipeline logo preview
async function pipLogoLoadPreview(id){
  var step=PIPELINE.find(function(s){return s.id===id;});
  if(!step)return;
  var vid=step.input,logo=step.logo;
  if(!vid||vid==='__prev__'||!logo){alert('Select video and logo files in this step first.');return;}
  var seekEl=document.getElementById('pip-prev-seek-'+id);
  var seek=(seekEl&&seekEl.value.trim())||'5';
  var btn=document.getElementById('pip-prev-btn-'+id);
  var status=document.getElementById('pip-prev-status-'+id);
  if(btn){btn.disabled=true;btn.textContent='Loading…';}
  try{
    var r=await fetch('/frame',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({session_id:SID,input:vid,logo:logo,seek:seek})});
    var d=await r.json();
    if(d.error)throw new Error(d.error);
    var frameImg=document.getElementById('pip-frame-'+id);
    var dragImg=document.getElementById('pip-drag-'+id);
    var frameEl=document.getElementById('pip-prev-frame-'+id);
    var sizePct=step.size_pct||12;
    dragImg.style.display='none'; dragImg.onload=null; frameImg.onload=null;
    function _startPipDrag(cx,cy){_pipLogoDragging=id;_logoDragStart={mx:cx,my:cy,lx:parseInt(dragImg.style.left)||0,ly:parseInt(dragImg.style.top)||0};}
    dragImg.onmousedown=function(e){_startPipDrag(e.clientX,e.clientY);e.preventDefault();};
    dragImg.ontouchstart=function(e){var t=e.touches[0];_startPipDrag(t.clientX,t.clientY);e.preventDefault();};
    var _fDone=false,_lDone=false;
    function _checkBoth(){if(_fDone&&_lDone){_placeLogoOnFrame(frameImg,dragImg,frameEl,sizePct);}}
    frameImg.onload=function(){_fDone=true;document.getElementById('pip-prev-panel-'+id).style.display='block';_checkBoth();frameImg.onload=null;};
    dragImg.onload=function(){_lDone=true;_checkBoth();dragImg.onload=null;};
    frameImg.src='data:image/jpeg;base64,'+d.frame;
    dragImg.src='data:'+d.logo_mime+';base64,'+d.logo;
    if(frameImg.complete&&frameImg.naturalWidth&&!_fDone){frameImg.onload=null;_fDone=true;document.getElementById('pip-prev-panel-'+id).style.display='block';_checkBoth();}
    if(dragImg.complete&&dragImg.naturalWidth&&!_lDone){dragImg.onload=null;_lDone=true;_checkBoth();}
    if(status)status.textContent='Frame at '+seek+'s · '+d.vid_w+'×'+d.vid_h;
  }catch(e){alert('Preview failed: '+e.message);}
  finally{if(btn){btn.disabled=false;btn.innerHTML='🖼️ Load Preview';}}
}
function pipLogoUsePos(id){
  var di=document.getElementById('pip-drag-'+id),fr=document.getElementById('pip-prev-frame-'+id);
  if(!di||!fr)return;
  var cx=(parseFloat(di.style.left)+di.offsetWidth/2)/fr.offsetWidth*100;
  var cy=(parseFloat(di.style.top)+di.offsetHeight/2)/fr.offsetHeight*100;
  _pipLogoPos[id]={x:Math.round(cx*10)/10,y:Math.round(cy*10)/10};
  var b=document.getElementById('pip-pos-badge-'+id);if(b)b.style.display='inline-flex';
  var s=document.getElementById('pip-prev-status-'+id);
  if(s)s.textContent='Locked at ('+_pipLogoPos[id].x+'%, '+_pipLogoPos[id].y+'%)';
}
function pipLogoClearPos(id){
  delete _pipLogoPos[id];
  var b=document.getElementById('pip-pos-badge-'+id);if(b)b.style.display='none';
  var s=document.getElementById('pip-prev-status-'+id);
  if(s)s.textContent='Cleared — corner setting will be used.';
}

// Cut Video
function cutAddRange(){CUT_RANGES.push({start:'00:00:00',end:'00:00:00'});renderCutRanges();}
function renderCutRanges(){
  var el=document.getElementById('cut-ranges');
  if(!CUT_RANGES.length){el.innerHTML='<div class="hint">No segments added &mdash; entire video will be kept. Click below to add ranges to remove.</div>';return;}
  el.innerHTML=CUT_RANGES.map(function(r,i){
    return '<div class="ri"><div class="rt">'+
      '<span class="ridx">'+(i+1)+'</span>'+
      '<span class="rlbl">Remove from</span>'+
      '<input class="ti" value="'+esc(r.start)+'" placeholder="00:00:00" onchange="CUT_RANGES['+i+'].start=this.value">'+
      '<span class="rarr">&#8594;</span>'+
      '<input class="ti" value="'+esc(r.end)+'" placeholder="00:00:00" onchange="CUT_RANGES['+i+'].end=this.value">'+
      '<button class="delbtn" onclick="CUT_RANGES.splice('+i+',1);renderCutRanges()" title="Remove">&#10005;</button>'+
      '</div></div>';
  }).join('');
}
async function runCut(){
  var inp=document.getElementById('cut-file').value;
  if(!inp){showErr('cut','Please select a video file first.');return;}
  var mode=document.querySelector('[name="cut-mode"]:checked').value;
  clearResult('cut');
  await runSteps('cut',[{op:'cut_multi',input:inp,ranges:CUT_RANGES,mode:mode}]);
}

// Merge
function mergeAddRow(){MERGE_FILES.push('');renderMergeRows();}
function mergeSetRef(i){
  MERGE_REF_IDX = (MERGE_REF_IDX===i) ? -1 : i;
  renderMergeRows();
}
function renderMergeRows(){
  var vids=FILES.filter(function(f){return /\.(mp4|mov|mkv|avi|webm)$/i.test(f);});
  var el=document.getElementById('merge-rows'); if(!el)return;
  el.innerHTML=MERGE_FILES.map(function(f,i){
    var opts='<option value="">&#8212; select video &#8212;</option>'+fileOpts(vids,f);
    var isRef=(MERGE_REF_IDX===i);
    var refB='<button class="refbtn'+(isRef?' active':'')+'" onclick="mergeSetRef('+i+')" title="'+(isRef?'Clear reference':'Set as reference video — all clips re-encoded to match')+'">&#11088;</button>';
    var del=i>=2?'<button class="delbtn" onclick="MERGE_FILES.splice('+i+',1);if(MERGE_REF_IDX==='+i+')MERGE_REF_IDX=-1;else if(MERGE_REF_IDX>'+i+')MERGE_REF_IDX--;renderMergeRows()" title="Remove">&#10005;</button>':'';
    return '<div class="merge-row">'+refB+'<select onchange="MERGE_FILES['+i+']=this.value">'+opts+'</select>'+del+'</div>';
  }).join('');
}
async function runMerge(){
  var inputs=MERGE_FILES.filter(Boolean);
  if(inputs.length<2){showErr('merge','Please select at least 2 video files.');return;}
  // Map MERGE_REF_IDX (index over all slots) to index within filtered inputs
  var refIdx=-1;
  if(MERGE_REF_IDX>=0){
    var count=0;
    for(var i=0;i<MERGE_FILES.length;i++){
      if(MERGE_FILES[i]){
        if(i===MERGE_REF_IDX){refIdx=count;break;}
        count++;
      }
    }
  }
  clearResult('merge');
  await runSteps('merge',[{op:'merge',inputs:inputs,ref_idx:refIdx}]);
}

// Logo
function setCorner(c){
  LOGO_CORNER=c;
  document.querySelectorAll('.corner-btn').forEach(function(b){b.classList.toggle('active',b.dataset.c===c);});
}
function logoAddWindow(){LOGO_WINDOWS.push({t_start:'',t_end:''});renderLogoWindows();}
function renderLogoWindows(){
  var el=document.getElementById('logo-windows'); if(!el)return;
  if(!LOGO_WINDOWS.length){el.innerHTML='';return;}
  el.innerHTML=LOGO_WINDOWS.map(function(w,i){
    return '<div class="ri"><div class="rt">'+
      '<span class="ridx">'+(i+1)+'</span>'+
      '<span class="rlbl">Show from</span>'+
      '<input class="ti" value="'+esc(w.t_start)+'" placeholder="00:00:00" onchange="LOGO_WINDOWS['+i+'].t_start=this.value">'+
      '<span class="rarr">to</span>'+
      '<input class="ti" value="'+esc(w.t_end)+'" placeholder="00:00:00" onchange="LOGO_WINDOWS['+i+'].t_end=this.value">'+
      '<button class="delbtn" onclick="LOGO_WINDOWS.splice('+i+',1);renderLogoWindows()" title="Remove">&#10005;</button>'+
      '</div></div>';
  }).join('');
}
async function runLogo(){
  var inp=document.getElementById('logo-video').value;
  var logo=document.getElementById('logo-img').value;
  if(!inp){showErr('logo','Please select a video file.');return;}
  if(!logo){showErr('logo','Please select a logo image.');return;}
  var size_pct=parseInt(document.getElementById('logo-size').value)||12;
  clearResult('logo');
  await runSteps('logo',[{op:'logo_multi',input:inp,logo:logo,corner:LOGO_CORNER,size_pct:size_pct,windows:LOGO_WINDOWS,
    pos_mode:logoCustomPos?'custom':'corner',
    px_pct:logoCustomPos?logoCustomPos.x:50,
    py_pct:logoCustomPos?logoCustomPos.y:50}]);
}

// Audio Cut
function audioAddRange(){AUDIO_RANGES.push({start:'00:00:00',end:'00:00:00'});renderAudioRanges();}
function renderAudioRanges(){
  var el=document.getElementById('audio-ranges');
  if(!AUDIO_RANGES.length){el.innerHTML='<div class="hint">No segments added &mdash; entire audio will be kept.</div>';return;}
  el.innerHTML=AUDIO_RANGES.map(function(r,i){
    return '<div class="ri"><div class="rt">'+
      '<span class="ridx">'+(i+1)+'</span>'+
      '<span class="rlbl">Remove from</span>'+
      '<input class="ti" value="'+esc(r.start)+'" placeholder="00:00:00" onchange="AUDIO_RANGES['+i+'].start=this.value">'+
      '<span class="rarr">&#8594;</span>'+
      '<input class="ti" value="'+esc(r.end)+'" placeholder="00:00:00" onchange="AUDIO_RANGES['+i+'].end=this.value">'+
      '<button class="delbtn" onclick="AUDIO_RANGES.splice('+i+',1);renderAudioRanges()" title="Remove">&#10005;</button>'+
      '</div></div>';
  }).join('');
}
async function runAudio(){
  var inp=document.getElementById('audio-file').value;
  if(!inp){showErr('audio','Please select an audio file first.');return;}
  var mode=document.querySelector('[name="audio-mode"]:checked').value;
  clearResult('audio');
  await runSteps('audio',[{op:'audio_cut_multi',input:inp,ranges:AUDIO_RANGES,mode:mode}]);
}

// MP3
async function runMp3(){
  var inp=document.getElementById('mp3-file').value;
  if(!inp){showErr('mp3','Please select a video file first.');return;}
  var quality=document.getElementById('mp3-quality').value;
  clearResult('mp3');
  await runSteps('mp3',[{op:'convert',input:inp,quality:quality}]);
}

// Compress
async function runCompress(){
  var inp=document.getElementById('compress-file').value;
  if(!inp){showErr('compress','Please select a video file first.');return;}
  var crf=parseInt(document.querySelector('[name="compress-crf"]:checked').value);
  clearResult('compress');
  await runSteps('compress',[{op:'compress',input:inp,crf:crf}]);
}

// Job runner
async function runSteps(section,steps){
  setRunning(section,true); showProg(section,true); setProgText(section,'Starting&#8230;'); clearLog(section);
  try {
    var r=await fetch('/run',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:SID,steps:steps})});
    var d=await r.json();
    if(d.error){showErr(section,d.error);setRunning(section,false);return;}
    pollJob(d.job_id,section,steps.length);
  } catch(e){showErr(section,'Network error: '+e.message);setRunning(section,false);}
}

function pollJob(jid,section,totalSteps){
  _lastLogLen[section]=0;
  var timer=setInterval(async function(){
    try{
      var r=await fetch('/job/'+jid); var j=await r.json();
      var pct=j.done?100:Math.round(((j.current_step||0)/Math.max(totalSteps,1))*90);
      setProgFill(section,pct);
      if(j.log&&j.log.length){setProgText(section,j.log[j.log.length-1]);appendLog(section,j.log);}
      if(j.done){
        clearInterval(timer); setRunning(section,false);
        if(j.error){showErr(section,j.error);}
        else{
          setProgFill(section,100); setProgText(section,'Done!');
          j.outputs.forEach(function(f){if(FILES.indexOf(f)===-1)FILES.push(f);});
          renderFileList(); showDone(section,j.outputs||[]);
        }
      }
    }catch(e){clearInterval(timer);showErr(section,'Poll error: '+e.message);setRunning(section,false);}
  },1000);
}

// UI helpers
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function showErr(section,msg){var el=document.getElementById(section+'-err');if(!el)return;el.textContent='Warning: '+msg;el.classList.add('show');}
function clearResult(section){
  var err=document.getElementById(section+'-err');if(err){err.textContent='';err.classList.remove('show');}
  var done=document.getElementById(section+'-done');if(done){done.classList.remove('show');done.innerHTML='';}
  showProg(section,false);
}
function showProg(section,show){var el=document.getElementById(section+'-prog');if(el)el.classList.toggle('show',show);if(show)setProgFill(section,0);}
function setProgFill(section,pct){var el=document.getElementById(section+'-fill');if(el)el.style.width=pct+'%';}
function setProgText(section,txt){var el=document.getElementById(section+'-prog-text');if(el)el.textContent=txt;}
function clearLog(section){var el=document.getElementById(section+'-log');if(el)el.innerHTML='';}
function appendLog(section,lines){
  var el=document.getElementById(section+'-log'); if(!el)return;
  var prev=_lastLogLen[section]||0;
  if(lines.length>prev){
    lines.slice(prev).forEach(function(l){
      var d=document.createElement('div'); d.textContent=l;
      d.style.color=l.indexOf('[OK]"')===0?'var(--success)':l.indexOf('▶')===0?'var(--accent)':'var(--muted)';
      el.appendChild(d);
    });
    el.scrollTop=el.scrollHeight; _lastLogLen[section]=lines.length;
  }
}
function setRunning(section,running){var btn=document.getElementById(section+'-btn');if(btn)btn.disabled=running;}
function showDone(section,outputs){
  var done=document.getElementById(section+'-done'); if(!done)return;
  var links=outputs.map(function(f){
    return '<a class="dl-btn" href="/download/'+SID+'/'+encodeURIComponent(f)+'" download="'+esc(f)+'">'+
           '<span>&#11015;&#65039;</span><span>'+esc(f)+'</span></a>';
  }).join('');
  done.innerHTML='<div class="done-title">&#9989; Done &mdash; click to download</div>'+links;
  done.classList.add('show');
}

// Theme toggle
function toggleTheme(){
  var isLight = document.documentElement.classList.toggle('light');
  localStorage.setItem('rp_theme', isLight ? 'light' : 'dark');
  var icon = document.getElementById('theme-icon');
  if(isLight){
    icon.innerHTML='<path d="M15 9A6 6 0 1 1 9 3a4.5 4.5 0 0 0 6 6z" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/>';
  } else {
    icon.innerHTML='<circle cx="9" cy="9" r="3.5" stroke="currentColor" stroke-width="1.4"/><path d="M9 1v2M9 15v2M1 9h2M15 9h2M3.2 3.2l1.4 1.4M13.4 13.4l1.4 1.4M13.4 3.2l-1.4 1.4M3.2 13.4l1.4 1.4" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>';
  }
}
(function(){
  var saved = localStorage.getItem('rp_theme');
  if(saved === 'light') toggleTheme();
})();

// ── Pipeline ──────────────────────────────────────────────────────────────────
var PIPELINE = [];
var _pipId = 0;
var _pipCutRanges  = {};  // stepId -> [{start,end}]
var _pipLogoWins   = {};  // stepId -> [{t_start,t_end}]

var PIP_LABELS = {cut_multi:'&#9986;&#65039; Cut Video', logo_multi:'&#127991;&#65039; Logo Burn',
  merge:'&#128279; Merge', audio_cut_multi:'&#127925; Audio Cut', convert:'&#128260; To MP3',
  compress:'&#128065; Compress'};

function pipAdd(op){
  var id = ++_pipId;
  var isFirst = PIPELINE.length === 0;
  var vids = FILES.filter(function(f){return /\.(mp4|mov|mkv|avi|webm)$/i.test(f);});
  var auds = FILES.filter(function(f){return /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f);});
  var imgs = FILES.filter(function(f){return /\.(png|jpg|jpeg|webp)$/i.test(f);});
  var step = {id:id, op:op};
  _pipCutRanges[id] = []; _pipLogoWins[id] = [];
  if(op==='cut_multi'){    step.input=isFirst?(vids[0]||''):'__prev__'; step.mode='fast'; }
  else if(op==='logo_multi'){ step.input=isFirst?(vids[0]||''):'__prev__'; step.logo=imgs[0]||''; step.corner='tr'; step.size_pct=12; }
  else if(op==='merge'){   step.inputs=isFirst?(vids.length>=2?vids.slice(0,2):['','']):['__prev__',vids[1]||'']; step.ref_idx=-1; }
  else if(op==='audio_cut_multi'){ step.input=isFirst?(auds[0]||''):'__prev__'; step.mode='accurate'; }
  else if(op==='convert'){ step.input=isFirst?(vids[0]||''):'__prev__'; step.quality='192k'; }
  else if(op==='compress'){ step.input=isFirst?(vids[0]||''):'__prev__'; step.crf=23; }
  PIPELINE.push(step); renderPipelineSteps();
  showTab('pipeline');
}

function pipRemove(id){ PIPELINE=PIPELINE.filter(function(s){return s.id!==id;}); delete _pipCutRanges[id]; delete _pipLogoWins[id]; renderPipelineSteps(); }
function pipMove(id, dir){ var i=PIPELINE.findIndex(function(s){return s.id===id;}); var ni=i+dir; if(ni<0||ni>=PIPELINE.length)return; var t=PIPELINE[i]; PIPELINE[i]=PIPELINE[ni]; PIPELINE[ni]=t; renderPipelineSteps(); }
function pipSet(id, key, val){ var s=PIPELINE.find(function(x){return x.id===id;}); if(s)s[key]=val; }
function pipMergeSet(id, i, val){ var s=PIPELINE.find(function(x){return x.id===id;}); if(s)s.inputs[i]=val; }
function pipMergeAdd(id){ var s=PIPELINE.find(function(x){return x.id===id;}); if(s){s.inputs.push('');renderPipelineSteps();} }
function pipMergeDel(id,i){ var s=PIPELINE.find(function(x){return x.id===id;}); if(s){if(s.ref_idx===i)s.ref_idx=-1;else if(s.ref_idx>i)s.ref_idx--;s.inputs.splice(i,1);renderPipelineSteps();} }
function pipMergeSetRef(id,i){ var s=PIPELINE.find(function(x){return x.id===id;}); if(s){s.ref_idx=(s.ref_idx===i)?-1:i;renderPipelineSteps();} }
function pipCutAdd(id){ _pipCutRanges[id].push({start:'00:00:00',end:'00:00:00'}); renderPipelineSteps(); }
function pipCutDel(id,i){ _pipCutRanges[id].splice(i,1); renderPipelineSteps(); }
function pipWinAdd(id){ _pipLogoWins[id].push({t_start:'',t_end:''}); renderPipelineSteps(); }
function pipWinDel(id,i){ _pipLogoWins[id].splice(i,1); renderPipelineSteps(); }

function pipInputField(step, idx){
  var vids=FILES.filter(function(f){return /\.(mp4|mov|mkv|avi|webm)$/i.test(f);});
  var auds=FILES.filter(function(f){return /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f);});
  var list = step.op==='audio_cut_multi' ? auds : vids;
  var prevOpt = idx>0 ? '<option value="__prev__"'+(step.input==='__prev__'?' selected':'')+'>&#8592; Output of Step '+idx+' (auto)</option>' : '';
  return '<div class="field"><label>Input File</label><select onchange="pipSet('+step.id+',\'input\',this.value)">'+
    prevOpt+'<option value="">&#8212; select &#8212;</option>'+fileOpts(list,step.input)+'</select></div>';
}

function renderPipelineSteps(){
  var el=document.getElementById('pip-steps'); if(!el)return;
  if(!PIPELINE.length){
    el.innerHTML='<div class="empty-state">No steps yet &mdash; click a button above to add operations</div>';
    return;
  }
  var vids=FILES.filter(function(f){return /\.(mp4|mov|mkv|avi|webm)$/i.test(f);});
  var auds=FILES.filter(function(f){return /\.(mp3|aac|wav|flac|m4a|ogg)$/i.test(f);});
  var imgs=FILES.filter(function(f){return /\.(png|jpg|jpeg|webp)$/i.test(f);});
  var n=PIPELINE.length;
  el.innerHTML=PIPELINE.map(function(step,idx){
    var id=step.id;
    var upBtn=idx>0?'<button class="icon-btn" onclick="pipMove('+id+',-1)" title="Move up">&#8593;</button>':'';
    var dnBtn=idx<n-1?'<button class="icon-btn" onclick="pipMove('+id+',1)" title="Move down">&#8595;</button>':'';
    var delBtn='<button class="icon-btn del" onclick="pipRemove('+id+')">&#128465;</button>';
    var acts='<div style="display:flex;gap:4px;">'+upBtn+dnBtn+delBtn+'</div>';
    var body='';

    if(step.op==='cut_multi'){
      var ranges=_pipCutRanges[id]||[];
      var rangeHtml=ranges.length?ranges.map(function(r,ri){
        return '<div class="ri" style="margin-bottom:6px;"><div class="rt">'+
          '<span class="ridx">'+(ri+1)+'</span><span class="rlbl">Remove</span>'+
          '<input class="ti" value="'+esc(r.start)+'" onchange="_pipCutRanges['+id+']['+ri+'].start=this.value">'+
          '<span class="rarr">&#8594;</span>'+
          '<input class="ti" value="'+esc(r.end)+'" onchange="_pipCutRanges['+id+']['+ri+'].end=this.value">'+
          '<button class="delbtn" onclick="pipCutDel('+id+','+ri+')">&#10005;</button></div></div>';
      }).join(''):'<div class="hint" style="margin-bottom:6px;">No segments &mdash; keeps full file</div>';
      body=pipInputField(step,idx)+rangeHtml+
        '<button class="addbtn" style="color:var(--danger);border-color:rgba(255,71,71,.35);" onclick="pipCutAdd('+id+')">&#9986; Add Cut Segment</button>'+
        '<div class="field" style="margin-top:10px;"><label>Mode</label><select onchange="pipSet('+id+',\'mode\',this.value)">'+
        '<option value="fast"'+(step.mode==='fast'?' selected':'')+'>&#9889; Fast &mdash; stream copy</option>'+
        '<option value="accurate"'+(step.mode==='accurate'?' selected':'')+'>&#127919; Accurate &mdash; re-encode</option></select></div>';
    } else if(step.op==='logo_multi'){
      var imgOpts='<option value="">&#8212; select image &#8212;</option>'+fileOpts(imgs,step.logo);
      var corners=[['tl','Top-Left'],['tr','Top-Right &#11088;'],['bl','Bot-Left'],['br','Bot-Right'],['center','Center']];
      var cGrid=corners.map(function(c){return '<button class="corner-btn'+(step.corner===c[0]?' active':'')+'" onclick="pipSet('+id+',\'corner\',\''+c[0]+'\');renderPipelineSteps()">'+c[1]+'</button>';}).join('');
      var wins=_pipLogoWins[id]||[];
      var winHtml=wins.length?wins.map(function(w,wi){
        return '<div class="ri" style="margin-bottom:6px;"><div class="rt">'+
          '<span class="ridx">'+(wi+1)+'</span><span class="rlbl">Show</span>'+
          '<input class="ti" value="'+esc(w.t_start)+'" placeholder="00:00:00" onchange="_pipLogoWins['+id+']['+wi+'].t_start=this.value">'+
          '<span class="rarr">to</span>'+
          '<input class="ti" value="'+esc(w.t_end)+'" placeholder="00:00:00" onchange="_pipLogoWins['+id+']['+wi+'].t_end=this.value">'+
          '<button class="delbtn" onclick="pipWinDel('+id+','+wi+')">&#10005;</button></div></div>';
      }).join(''):'<div class="hint" style="margin-bottom:6px;">No windows &mdash; logo shows entire video</div>';
      body=pipInputField(step,idx)+
        '<div class="field"><label>Logo Image</label><select onchange="pipSet('+id+',\'logo\',this.value)">'+imgOpts+'</select></div>'+
        '<div class="field"><label>Position</label><div class="corner-grid">'+cGrid+'</div></div>'+
        '<div class="field"><label>Size %</label><input type="number" value="'+(step.size_pct||12)+'" min="1" max="100" oninput="pipSet('+id+',\'size_pct\',parseInt(this.value))" style="max-width:90px;"></div>'+
        winHtml+'<button class="addbtn" style="color:var(--accent);border-color:rgba(232,255,71,.35);" onclick="pipWinAdd('+id+')">&#8987; Add Visibility Window</button>'+
        '<div style="font-size:12px;font-weight:700;color:var(--accent);margin-top:14px;margin-bottom:6px;font-family:\'JetBrains Mono\',monospace;">&#9315; POSITION PREVIEW <span style="font-weight:400;color:var(--muted);font-size:11px;">&mdash; drag to exact spot</span></div>'+
        '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px;">'+
        '<button class="btn" id="pip-prev-btn-'+id+'" onclick="pipLogoLoadPreview('+id+')" style="font-size:12px;padding:8px 12px;background:var(--surface);border:1px solid var(--border);color:var(--text);">&#128444;&#65039; Load Preview</button>'+
        '<label style="font-size:12px;color:var(--muted);font-family:\'JetBrains Mono\',monospace;display:flex;align-items:center;gap:4px;">at&nbsp;<input type="text" id="pip-prev-seek-'+id+'" value="5" placeholder="sec" style="width:70px;background:var(--surface);border:1px solid var(--border);border-radius:6px;padding:5px 7px;color:var(--text);font-family:\'JetBrains Mono\',monospace;font-size:12px;outline:none;">&nbsp;s</label>'+
        '<span id="pip-prev-status-'+id+'" style="font-size:12px;color:var(--muted);font-family:\'JetBrains Mono\',monospace;"></span>'+
        '<span id="pip-pos-badge-'+id+'" class="logo-pos-badge" style="display:none;">&#128204; Custom pos active</span>'+
        '</div>'+
        '<div id="pip-prev-panel-'+id+'" class="logo-preview-wrap" style="display:none;">'+
        '<div id="pip-prev-frame-'+id+'" class="logo-preview-frame">'+
        '<img id="pip-frame-'+id+'" class="frame-img" draggable="false" alt="">'+
        '<img id="pip-drag-'+id+'" class="logo-drag-img" draggable="false" alt="" style="display:none;">'+
        '</div>'+
        '<div style="margin-top:10px;display:flex;gap:8px;flex-wrap:wrap;">'+
        '<button class="btn" onclick="pipLogoUsePos('+id+')" style="font-size:12px;padding:8px 12px;background:var(--success);color:#000;">&#9989; Use this position</button>'+
        '<button class="btn" onclick="pipLogoClearPos('+id+')" style="font-size:12px;padding:8px 12px;background:transparent;border:1px solid var(--border);color:var(--text);">&#10005; Use corner instead</button>'+
        '</div></div>';
    } else if(step.op==='merge'){
      var pipRefIdx=step.ref_idx!=null?step.ref_idx:-1;
      var mergeRows=(step.inputs||[]).map(function(f,mi){
        var prevOpt=mi===0&&idx>0?'<option value="__prev__"'+(f==='__prev__'?' selected':'')+'>&#8592; Prev step output</option>':'';
        var mopts=prevOpt+'<option value="">&#8212; select &#8212;</option>'+fileOpts(vids,f);
        var isRef=(pipRefIdx===mi);
        var refB='<button class="refbtn'+(isRef?' active':'')+'" onclick="pipMergeSetRef('+id+','+mi+')" title="'+(isRef?'Clear reference':'Set as reference — all clips re-encoded to match')+'">&#11088;</button>';
        var delB=mi>=2?'<button class="delbtn" onclick="pipMergeDel('+id+','+mi+')">&#10005;</button>':'';
        return '<div class="merge-row">'+refB+'<select onchange="pipMergeSet('+id+','+mi+',this.value)">'+mopts+'</select>'+delB+'</div>';
      }).join('');
      body='<div style="font-size:12px;font-weight:700;color:var(--blue);margin-bottom:8px;font-family:\'JetBrains Mono\',monospace;">Video files to join (in order)</div>'+
        mergeRows+'<button class="addbtn" style="color:var(--blue);border-color:rgba(71,179,255,.35);" onclick="pipMergeAdd('+id+')">&#10133; Add File</button>';
    } else if(step.op==='audio_cut_multi'){
      var aranges=_pipCutRanges[id]||[];
      var arangeHtml=aranges.length?aranges.map(function(r,ri){
        return '<div class="ri" style="margin-bottom:6px;"><div class="rt">'+
          '<span class="ridx">'+(ri+1)+'</span><span class="rlbl">Remove</span>'+
          '<input class="ti" value="'+esc(r.start)+'" onchange="_pipCutRanges['+id+']['+ri+'].start=this.value">'+
          '<span class="rarr">&#8594;</span>'+
          '<input class="ti" value="'+esc(r.end)+'" onchange="_pipCutRanges['+id+']['+ri+'].end=this.value">'+
          '<button class="delbtn" onclick="pipCutDel('+id+','+ri+')">&#10005;</button></div></div>';
      }).join(''):'<div class="hint" style="margin-bottom:6px;">No segments &mdash; keeps full audio</div>';
      body=pipInputField(step,idx)+arangeHtml+
        '<button class="addbtn" style="color:var(--accent2);border-color:rgba(255,159,71,.35);" onclick="pipCutAdd('+id+')">&#9986; Add Cut Segment</button>'+
        '<div class="field" style="margin-top:10px;"><label>Mode</label><select onchange="pipSet('+id+',\'mode\',this.value)">'+
        '<option value="accurate"'+(step.mode==='accurate'?' selected':'')+'>&#127919; Accurate &mdash; re-encode</option>'+
        '<option value="fast"'+(step.mode==='fast'?' selected':'')+'>&#9889; Fast &mdash; stream copy</option></select></div>';
    } else if(step.op==='convert'){
      body=pipInputField(step,idx)+
        '<div class="field"><label>MP3 Quality</label><select onchange="pipSet('+id+',\'quality\',this.value)">'+
        '<option value="128k"'+(step.quality==='128k'?' selected':'')+'>128 kbps</option>'+
        '<option value="192k"'+(step.quality==='192k'?' selected':'')+'>192 kbps (recommended)</option>'+
        '<option value="320k"'+(step.quality==='320k'?' selected':'')+'>320 kbps</option></select></div>';
    } else if(step.op==='compress'){
      body=pipInputField(step,idx)+
        '<div class="field"><label>Compression Level</label><select onchange="pipSet('+id+',\'crf\',parseInt(this.value))">'+
        '<option value="18"'+(step.crf===18?' selected':'')+'>&#127775; Light (CRF 18) &mdash; ~20% smaller</option>'+
        '<option value="23"'+(step.crf===23?' selected':'')+'>&#9881;&#65039; Balanced (CRF 23) &mdash; ~40% smaller</option>'+
        '<option value="28"'+(step.crf===28?' selected':'')+'>&#128190; Strong (CRF 28) &mdash; ~60% smaller</option>'+
        '</select></div>';
    }

    return '<div class="pip-card">'+
      '<div class="pip-head"><span class="pip-num">'+(idx+1)+'</span>'+
      '<span class="pip-label">'+PIP_LABELS[step.op]+'</span>'+acts+'</div>'+
      '<div class="pip-body">'+body+'</div></div>';
  }).join('');
}

async function runPipeline(){
  if(!PIPELINE.length){showErr('pip','Add at least one step first.');return;}
  // Build steps payload, merging in per-step cut ranges and logo windows
  var steps=PIPELINE.map(function(step){
    var s=Object.assign({},step);
    if(s.op==='cut_multi')      s.ranges=(_pipCutRanges[step.id]||[]).slice();
    if(s.op==='audio_cut_multi')s.ranges=(_pipCutRanges[step.id]||[]).slice();
    if(s.op==='logo_multi'){
      s.windows=(_pipLogoWins[step.id]||[]).slice();
      var pp=_pipLogoPos[step.id];
      s.pos_mode=pp?'custom':'corner';
      s.px_pct=pp?pp.x:50;
      s.py_pct=pp?pp.y:50;
    }
    return s;
  });
  // Validate first step has a real file
  var first=steps[0];
  var firstInp = first.op==='merge' ? (first.inputs||[])[0] : first.input;
  if(!firstInp||firstInp==='__prev__'){showErr('pip','Step 1 needs a real file - it has no previous step to chain from.');return;}
  clearResult('pipeline'); clearResult('pip');
  await runSteps('pipeline',steps);
}

// Helper to clear pip-err separately
function showErr(section, msg){
  var key = section==='pip' ? 'pip-err' : section+'-err';
  var el=document.getElementById(key); if(!el)return;
  el.textContent='Warning: '+msg; el.classList.add('show');
}

// Icon btn style (reuse for pipeline)
document.head.insertAdjacentHTML('beforeend','<style>.icon-btn{background:none;border:1px solid var(--border);border-radius:5px;padding:4px 7px;color:var(--muted);cursor:pointer;font-size:13px;transition:.15s;}.icon-btn:hover{border-color:var(--text);color:var(--text);}.icon-btn.del:hover{border-color:var(--danger);color:var(--danger);}</style>');

// Init
renderCutRanges(); renderAudioRanges(); renderPipelineSteps(); initSession();
</script>
</body>
</html>"""

# - main -
if __name__ == '__main__':
    if not FFMPEG:
        print("\n[RishiPraveen Editor] WARNING: ffmpeg not found in PATH or ffmpeg_bin/")
        print("  On Linux/Docker: apt-get install -y ffmpeg")
        print("  On Windows: place ffmpeg.exe in ffmpeg_bin/ folder\n")
    else:
        print(f"[RishiPraveen Editor] ffmpeg: {FFMPEG}")

    missing = []
    if not os.path.exists(GURUDEV_VIDEO):
        missing.append('Gurudev_Ashirwaad.mp4')
    if not os.path.exists(GURUDEV_PHOTO):
        missing.append('gurudev_photo.jpg')
    if not os.path.exists(ARHAMVIJJA_LOGO):
        missing.append('arhamvijja_logo.png')
    if missing:
        print(f"[WARNING] Static files not found (will show broken image): {missing}")
        print(f"  Place them in: {SCRIPT_DIR}")

    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"\n[RishiPraveen Editor] Listening on http://{HOST}:{PORT}")
    if HOST == '0.0.0.0':
        import socket
        try:
            ip = socket.gethostbyname(socket.gethostname())
            print(f"[RishiPraveen Editor] Local network: http://{ip}:{PORT}")
        except Exception:
            pass
    print("[RishiPraveen Editor] Press Ctrl+C to stop\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[RishiPraveen Editor] Stopped.")

