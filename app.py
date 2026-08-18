#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Web UI + API vượt link (cuty.io / shrinkme.click...). Có 2 chế độ:
  - Auto: cố gắng tự pass captcha (Turnstile tự pass trong Chrome thật; reCAPTCHA
    chỉ tự pass khi IP/profile đủ trust).
  - Manual: khi captcha không tự pass, web page hiện nút "Tiếp tục" — bạn giải
    captcha trong cửa sổ Chrome (local) hoặc qua noVNC (Railway) rồi bấm nút đó,
    tool tự tiếp tục và bắt link đích.

Có **queue** (xếp hàng): nộp nhiều link cùng lúc, tool xử lý tuần tự 1 link/lần
(Chrome tốn RAM), kết quả tích lũy trong session để copy. Mọi state ở RAM (mất
khi restart — Railway hay restart thì nộp lại).

Chạy local:
  pip install -r requirements.txt
  python app.py            # http://localhost:8080

Deploy Railway: xem README. Cần set PROXY (residential) để captcha tự pass trên IP
datacenter, hoặc bật NOVNC=1 (đã sẵn trong Dockerfile) để manual mode remote.

Env:
  PORT        cổng HTTP (Railway tự set, mặc định 8080)
  PROXY       proxy cho Chrome, vd http://user:pass@host:port  (residential!)
  PROFILE_DIR thư mục profile Chrome (mặc định ~/.vuot-link-profile khi local; /data/profile khi Docker/Railway)
  TIMEOUT     giây giới hạn mỗi link (mặc định 240 — có cả thời gian chờ manual)
  NOVNC       "1" bật noVNC (manual mode remote; Dockerfile đã set sẵn)
  NOVNC_DIR   thư mục static noVNC (mặc định /usr/share/novnc)
  VNC_HOST/VNC_PORT  VNC server cho WS proxy (mặc định 127.0.0.1:5900)
"""
import asyncio
import os
import threading
import time
import uuid

from fastapi import FastAPI, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.staticfiles import StaticFiles

from vuot_link import bypass_url, ManualGate

app = FastAPI(title="Vượt link cuty.io / shrinkme.click")

# Một link lúc (Chrome tốn RAM). Lock async toàn app, dùng cho cả job manual lẫn /api.
_LOCK = asyncio.Lock()

# None → bypass_url tự dùng ~/.vuot-link-profile (local). Railway/Docker set env PROFILE_DIR=/data/profile.
PROFILE_DIR = os.environ.get("PROFILE_DIR") or None
PROXY = os.environ.get("PROXY") or None
TIMEOUT = float(os.environ.get("TIMEOUT", "240"))
NOVNC = os.environ.get("NOVNC", "") == "1"     # bật noVNC (Railway/Docker) cho manual remote
VNC_HOST = os.environ.get("VNC_HOST", "127.0.0.1")
VNC_PORT = int(os.environ.get("VNC_PORT", "5900"))
NOVNC_DIR = os.environ.get("NOVNC_DIR", "/usr/share/novnc")


def _log(m: str):
    print(m, flush=True)
    line = m.rstrip()
    snap = _STATE["log"]
    snap.append(line)
    if len(snap) > 200:
        del snap[: len(snap) - 200]


# ---------- Trạng thái job (poll từ web) ----------
_STATE = {
    "status": "idle",        # idle | running | needs_manual | done | error
    "url": None,
    "destination": None,
    "raw_text": None,
    "error": None,
    "manual_msg": None,
    "log": [],
}
_GATE: "WebManualGate | None" = None
# Queue (xếp hàng): worker tuần tự xử lý 1 link/lần (Chrome tốn RAM).
_queue: list[dict] = []        # pending: {id,url,manual,added_at}
_results: list[dict] = []      # done:    {id,url,destination,raw_text,status,error,completed_at}
_worker_task: "asyncio.Task | None" = None
_RESULTS_CAP = 200             # FIFO, trim kết quả về 200 (chống tràn RAM)
_cancel_event = threading.Event()
_proc_holder = {}


class WebManualGate(ManualGate):
    """Khi captcha không tự pass: báo state needs_manual, chờ /api/continue."""
    def __init__(self, log):
        super().__init__(log)
        self.event = threading.Event()

    def wait(self, message, timeout=None):
        _STATE["status"] = "needs_manual"
        _STATE["manual_msg"] = message
        self.log("  [!] cần manual: " + message)
        got = self.event.wait(timeout)          # None = chờ tới khi /api/continue
        self.event.clear()
        _STATE["status"] = "running"
        _STATE["manual_msg"] = None
        return bool(got)

    def release(self):
        self.event.set()


def _reset_state(url):
    _STATE.update(
        status="running", url=url, destination=None, raw_text=None,
        error=None, manual_msg=None, log=[],
    )


def _set_idle():
    _STATE.update(
        status="idle", url=None, destination=None, raw_text=None,
        error=None, manual_msg=None, log=[],
    )


async def _queue_worker():
    """Xử lý _queue tuần tự (1 link/lần). Chạy đến khi queue rỗng rồi exit;
    _ensure_worker() khởi lại khi có item mới. Giữ _LOCK trong khi xử lý để
    /api (blocking) không chạy song song 2 Chrome (OOM)."""
    global _GATE
    try:
        while _queue:
            item = _queue.pop(0)
            _cancel_event.clear()              # cancel per-item, không leak sang item kế
            _reset_state(item["url"])
            gate = WebManualGate(_log) if item["manual"] else None
            _GATE = gate
            try:
                async with _LOCK:
                    dest, raw = await asyncio.to_thread(
                        bypass_url, item["url"],
                        profile_dir=PROFILE_DIR, headless=False, invisible=False,
                        proxy=PROXY, timeout=TIMEOUT, manual=item["manual"],
                        no_text=False, log=_log, manual_gate=gate,
                        cancel_event=_cancel_event, proc_holder=_proc_holder,
                    )
                if _cancel_event.is_set():
                    status, error = "canceled", "Đã hủy."
                elif dest:
                    status, error = "done", None
                else:
                    status, error = "error", "Không lấy được link đích."
                _results.append({
                    "id": item["id"], "url": item["url"],
                    "destination": dest, "raw_text": raw,
                    "status": status, "error": error,
                    "completed_at": time.time(),
                })
            except Exception as e:
                _results.append({
                    "id": item["id"], "url": item["url"], "destination": None,
                    "raw_text": None, "status": "error", "error": repr(e),
                    "completed_at": time.time(),
                })
            finally:
                _GATE = None
                if len(_results) > _RESULTS_CAP:
                    del _results[: len(_results) - _RESULTS_CAP]
    finally:
        # Queue rỗng (hoặc bị clear khi cancel) → về idle.
        _set_idle()


def _ensure_worker():
    """Khởi worker nếu chưa chạy hoặc đã xong. An toàn gọi nhiều lần."""
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_queue_worker())


def _snap():
    return {k: v for k, v in _STATE.items() if k != "log"} | {"log": list(_STATE["log"][-30:])}


# ---------- Trang ----------
INDEX_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vượt link cuty.io / shrinkme.click (queue)</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:900px;margin:32px auto;padding:0 16px;color:#222}
  h1{font-size:1.4rem} h2{font-size:1.05rem;margin:0 0 8px}
  textarea{width:100%;min-height:96px;padding:10px;font-size:14px;border:1px solid #ccc;border-radius:8px;box-sizing:border-box;font-family:inherit}
  form{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  form>label{font-size:13px;display:flex;align-items:center;gap:4px}
  button{padding:10px 16px;font-size:14px;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}
  button:disabled{background:#999;cursor:default}
  button.green{background:#16a34a} button.red{background:#dc2626} button.gray{background:#6b7280}
  button.sm{padding:4px 10px;font-size:12px}
  a.dest{color:#2563eb}
  .err{color:#c00}.muted{color:#777;font-size:13px}.ok{color:#16a34a}
  pre{background:#f6f8fa;padding:10px;border-radius:8px;overflow:auto;max-height:200px;font-size:12px}
  .manual{border:1px solid #f59e0b;background:#fffbeb;padding:14px;border-radius:10px;margin:10px 0}
  .log{font-size:12px;color:#555;background:#fafafa;border:1px solid #eee;border-radius:8px;padding:8px;max-height:140px;overflow:auto;white-space:pre-wrap}
  .section{margin-top:22px}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{border:1px solid #e5e7eb;padding:6px 8px;text-align:left;vertical-align:top}
  th{background:#f9fafb;font-weight:600}
  td.u{word-break:break-all;max-width:260px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  td.d{word-break:break-all}
  .badge{display:inline-block;padding:1px 7px;border-radius:999px;font-size:11px;font-weight:600}
  .badge.done{background:#dcfce7;color:#166534}
  .badge.error{background:#fee2e2;color:#991b1b}
  .badge.canceled{background:#f3f4f6;color:#6b7280}
  .badge.manual{background:#fef3c7;color:#92400e}
  ul{margin:6px 0;padding-left:20px}
  ul li{font-size:13px;font-family:ui-monospace,Consolas,monospace;word-break:break-all;margin:2px 0}
</style></head><body>
<h1>Vượt link cuty.io / shrinkme.click <span class="muted" style="font-size:.8em">(xếp hàng)</span></h1>
<form id="f">
  <textarea id="urls" placeholder="Dán 1 hoặc nhiều link, mỗi dòng 1 link&#10;vd: https://cuty.io/xxxx&#10;    https://shrinkme.click/yyyy" required></textarea>
  <label><input type="checkbox" id="manual" checked> Manual nếu captcha k tự pass</label>
  <button id="go">Thêm vào hàng</button>
  <button type="button" id="cancel" class="red">Hủy &amp; xóa hàng</button>
</form>
<div id="out" class="muted" style="margin-top:10px">Dán link rồi bấm "Thêm vào hàng". Queue xử lý tuần tự 1 link/lần.</div>
<div id="current" class="section"></div>
<div id="pending" class="section"></div>
<div id="results" class="section"></div>
<script>
const f=document.getElementById('f'),urlsEl=document.getElementById('urls'),out=document.getElementById('out');
const curEl=document.getElementById('current'),pendEl=document.getElementById('pending'),resEl=document.getElementById('results');
let poll=null;
async function start(){
  const urls=urlsEl.value.split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean);
  if(!urls.length){out.className='err';out.textContent='Dán ít nhất 1 link.';return;}
  const manual=document.getElementById('manual').checked;
  out.className='muted';out.textContent='Đang thêm vào hàng...';
  try{
    const r=await fetch('/api/queue',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({urls:urls,manual:manual})});
    const j=await r.json();
    if(!r.ok){out.className='err';out.textContent='Lỗi: '+(j.detail||JSON.stringify(j));return;}
    out.className='muted';
    out.textContent='Đã thêm '+j.queued+' link vào hàng (đang chờ: '+j.pending+').';
    if(urls.length===1) urlsEl.value='';
    ensurePoll(); status();
  }catch(e){out.className='err';out.textContent='Lỗi mạng: '+e;}
}
async function status(){
  try{
    const j=await (await fetch('/api/queue')).json();
    renderCurrent(j.current||{});
    renderPending(j.pending||[]);
    renderResults(j.results||[]);
  }catch(e){}
}
function renderCurrent(c){
  if(!c||!c.url||c.status==='idle'||c.status==='done'||c.status==='error'||c.status==='canceled'){
    curEl.innerHTML='';return;
  }
  let h='<h2>Đang xử lý</h2><div class="muted">'+esc(c.url)+'</div>';
  if(c.status==='needs_manual'){
    h+='<div class="manual"><b>Cần giải captcha thủ công</b><br>'+esc(c.manual_msg||'')+'<br>';
    if(window._NOVNC){
      const vu='/vnc/vnc.html?autoconnect=1&path=vnc/ws&resize=scale&host='
        +encodeURIComponent(location.hostname)+'&port='+encodeURIComponent(location.port||'443');
      h+='<a href="'+vu+'" target="_blank" style="display:inline-block;margin-top:8px">Mở noVNC để điều khiển Chrome →</a><br>';
    } else {
      h+='<span class="muted">Giải trong cửa sổ Chrome đang mở trên máy này.</span><br>';
    }
    h+='<button class="green" onclick="cont()" style="margin-top:8px">Đã giải — Tiếp tục</button></div>';
  }
  if(c.log&&c.log.length){h+='<div class="log">'+c.log.map(esc).join('\\n')+'</div>';}
  curEl.innerHTML=h;
}
function renderPending(p){
  if(!p.length){pendEl.innerHTML='';return;}
  let h='<h2>Hàng chờ ('+p.length+')</h2><ul>';
  p.forEach(it=>{h+='<li>'+esc(it.url)+(it.manual?' <span class="badge manual">manual</span>':'')+'</li>';});
  pendEl.innerHTML=h+'</ul>';
}
function renderResults(r){
  if(!r.length){resEl.innerHTML='';return;}
  let h='<h2>Kết quả ('+r.length+' gần nhất)</h2>';
  h+='<table><thead><tr><th>Link vào</th><th>Link đích</th><th>Trạng thái</th><th>Lỗi / raw</th></tr></thead><tbody>';
  r.forEach(it=>{
    h+='<tr><td class="u">'+esc(it.url)+'</td>';
    if(it.destination){
      h+='<td class="d"><a class="dest" href="'+esc(it.destination)+'" target="_blank">'+esc(it.destination)+'</a>'
        +' <button class="sm" data-dest="'+esc(it.destination)+'" onclick="copy(this)">Copy</button></td>';
    } else { h+='<td class="d muted">—</td>'; }
    h+='<td><span class="badge '+esc(it.status)+'">'+esc(it.status)+'</span></td>';
    h+='<td class="d">';
    if(it.error){h+='<span class="err">'+esc(it.error)+'</span>';}
    if(it.raw_text){h+='<details style="margin-top:4px"><summary>raw text</summary><pre>'+esc(it.raw_text)+'</pre></details>';}
    h+='</td></tr>';
  });
  h+='</tbody></table>';
  h+='<button class="gray sm" onclick="clearResults()" style="margin-top:8px">Xóa kết quả</button>';
  resEl.innerHTML=h;
}
async function cont(){try{await fetch('/api/continue',{method:'POST'});}catch(e){}}
async function doCancel(){
  try{await fetch('/api/queue/cancel',{method:'POST'});}catch(e){}
  out.className='muted';out.textContent='Đã hủy item hiện tại và xóa hàng chờ.';
  status();
}
async function clearResults(){
  try{await fetch('/api/queue/clear-results',{method:'POST'});}catch(e){}
  status();
}
function copy(btn){
  const text=btn.dataset.dest||'';
  navigator.clipboard.writeText(text).then(()=>{
    const old=btn.textContent;btn.textContent='Đã copy!';setTimeout(()=>{btn.textContent=old;},1200);
  }).catch(()=>{btn.textContent='Lỗi copy';setTimeout(()=>{btn.textContent='Copy';},1200);});
}
function ensurePoll(){if(!poll){poll=setInterval(status,1500);}}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
f.onsubmit=e=>{e.preventDefault();start();};
document.getElementById('cancel').onclick=doCancel;
window._NOVNC=__NOVNC__;
ensurePoll(); status();   // luôn poll để thấy state hiện tại (job đang chạy từ trước)
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML.replace("__NOVNC__", "true" if NOVNC else "null")


# ---------- Queue API (chính) ----------
class QueueReq(BaseModel):
    urls: list[str]
    manual: bool = False


@app.post("/api/queue")
async def api_queue_post(req: QueueReq):
    """Nộp nhiều link vào hàng. Worker xử lý tuần tự. Trả số link đã thêm + đang chờ."""
    clean = [u.strip() for u in req.urls if u and u.strip()]
    added = [{"id": uuid.uuid4().hex, "url": u, "manual": req.manual, "added_at": time.time()} for u in clean]
    _queue.extend(added)
    _ensure_worker()
    return {
        "queued": len(added),
        "pending": len(_queue),
        "running": _worker_task is not None and not _worker_task.done(),
    }


@app.get("/api/queue")
def api_queue_get():
    """State đầy đủ cho UI poll: current item + pending + results."""
    return {
        "current": _snap(),
        "pending": [{"url": i["url"], "manual": i["manual"]} for i in _queue],
        "pending_count": len(_queue),
        "results": list(_results[-50:]),
        "results_total": len(_results),
        "running": _worker_task is not None and not _worker_task.done(),
    }


@app.post("/api/queue/cancel")
def api_queue_cancel():
    """Hủy item hiện tại (kill Chrome, release manual gate) + clear hàng chờ."""
    cleared = len(_queue)
    _cancel_event.set()
    _queue.clear()
    proc = _proc_holder.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass
    if _GATE is not None:
        try:
            _GATE.release()
        except Exception:
            pass
    return {"ok": True, "cleared_pending": cleared}


@app.post("/api/queue/clear-results")
def api_queue_clear_results():
    _results.clear()
    return {"ok": True}


# ---------- Backward-compat (script cũ) ----------
@app.get("/api/start")
async def api_start(url: str = Query(...), manual: int = Query(...)):
    """Enqueue 1 link (không còn 409 busy — queue luôn nhận). Script cũ vẫn chạy."""
    url = (url or "").strip()
    if not url:
        return JSONResponse({"started": False, "error": "missing url"}, status_code=400)
    _queue.append({"id": uuid.uuid4().hex, "url": url, "manual": bool(manual), "added_at": time.time()})
    _ensure_worker()
    return {"started": True, "queued": True, "pending": len(_queue)}


@app.get("/api/status")
def api_status():
    """State của item đang xử lý (cho script cũ poll theo từng link)."""
    return _snap()


@app.post("/api/continue")
def api_continue():
    """Release manual gate của item hiện tại."""
    if _GATE is not None and _STATE.get("status") == "needs_manual":
        _GATE.release()
        return {"ok": True}
    return {"ok": False, "reason": "not waiting"}


@app.post("/api/cancel")
def api_cancel():
    """Alias của /api/queue/cancel (hủy current + clear pending)."""
    return api_queue_cancel()


# ---------- noVNC: WS proxy → VNC server (manual mode remote, cùng port với app) ----------
@app.websocket("/vnc/ws")
async def vnc_ws(ws: WebSocket):
    await ws.accept()
    try:
        reader, writer = await asyncio.open_connection(VNC_HOST, VNC_PORT)
    except Exception:
        await ws.close(code=1011, reason="VNC server không chạy (NOVNC chưa bật?)")
        return

    async def ws_to_vnc():
        try:
            while True:
                data = await ws.receive_bytes()
                writer.write(data)
                await writer.drain()
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    async def vnc_to_ws():
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:
            pass

    t1 = asyncio.create_task(ws_to_vnc())
    t2 = asyncio.create_task(vnc_to_ws())
    try:
        # Khi một hướng đóng (ws ngắt hoặc VNC đóng), huỷ hướng kia + dọn sạch.
        done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass


# Serve noVNC client tĩnh (chỉ khi dir tồn tại — Linux/Docker cài gói novnc).
if os.path.isdir(NOVNC_DIR):
    app.mount("/vnc", StaticFiles(directory=NOVNC_DIR, html=True), name="vnc")


# ---------- Đường đơn giản (auto-only, blocking) cho script/API ----------
@app.get("/api")
async def api_get(url: str = Query(...)):
    async with _LOCK:
        dest, raw = await asyncio.to_thread(
            bypass_url, url, profile_dir=PROFILE_DIR, headless=False, invisible=False,
            proxy=PROXY, timeout=TIMEOUT, manual=False, no_text=False, log=_log)
    if not dest:
        return JSONResponse({"ok": False, "error": "Không lấy được link đích (captcha có thể không pass — thử chế độ manual qua web)."}, status_code=502)
    return {"ok": True, "destination": dest, "raw_text": raw}


@app.post("/api")
async def api_post(url: str = Form(...)):
    return await api_get(url)


@app.get("/health")
def health():
    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
