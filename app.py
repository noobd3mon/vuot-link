#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
app.py — Web UI + API vượt link (cuty.io / shrinkme.click...). Có 2 chế độ:
  - Auto: cố gắng tự pass captcha (Turnstile tự pass trong Chrome thật; reCAPTCHA
    chỉ tự pass khi IP/profile đủ trust).
  - Manual: khi captcha không tự pass, web page hiện nút "Tiếp tục" — bạn giải
    captcha trong cửa sổ Chrome (local) hoặc qua noVNC (Railway) rồi bấm nút đó,
    tool tự tiếp tục và bắt link đích.

Chạy local:
  pip install -r requirements.txt
  python app.py            # http://localhost:8080

Deploy Railway: xem README. Cần set PROXY (residential) để captcha tự pass trên IP
datacenter, hoặc bật NOVNC=1 (đã sẵn trong Dockerfile) để manual mode remote.

Env:
  PORT        cổng HTTP (Railway tự set, mặc định 8080)
  PROXY       proxy cho Chrome, vd http://user:pass@host:port  (residential!)
  PROFILE_DIR thư mục profile Chrome (mặc định /data/profile — mount volume)
  TIMEOUT     giây giới hạn mỗi link (mặc định 240 — có cả thời gian chờ manual)
  NOVNC       "1" bật noVNC (manual mode remote; Dockerfile đã set sẵn)
  NOVNC_DIR   thư mục static noVNC (mặc định /usr/share/novnc)
  VNC_HOST/VNC_PORT  VNC server cho WS proxy (mặc định 127.0.0.1:5900)
"""
import asyncio
import os
import threading

from fastapi import FastAPI, Form, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.staticfiles import StaticFiles

from vuot_link import bypass_url, ManualGate

app = FastAPI(title="Vượt link cuty.io / shrinkme.click")

# Một link lúc (Chrome tốn RAM). Lock async toàn app, dùng cho cả job manual lẫn /api.
_LOCK = asyncio.Lock()

PROFILE_DIR = os.environ.get("PROFILE_DIR", "/data/profile")
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
# Giữ reference task để tránh GC (Python docs: task không có ref có bị thu gom).
_current_task: "asyncio.Task | None" = None


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


async def _job(url: str, manual: bool):
    global _GATE
    async with _LOCK:
        _reset_state(url)
        gate = WebManualGate(_log) if manual else None
        _GATE = gate
        try:
            dest, raw = await asyncio.to_thread(
                bypass_url, url,
                profile_dir=PROFILE_DIR,
                headless=False, invisible=False, proxy=PROXY,
                timeout=TIMEOUT, manual=manual, no_text=False,
                log=_log, manual_gate=gate,
            )
            if dest:
                _STATE["status"] = "done"
                _STATE["destination"] = dest
                _STATE["raw_text"] = raw
            else:
                _STATE["status"] = "error"
                _STATE["error"] = "Không lấy được link đích."
        except Exception as e:
            _STATE["status"] = "error"
            _STATE["error"] = repr(e)
        finally:
            _GATE = None


def _snap():
    return {k: v for k, v in _STATE.items() if k != "log"} | {"log": list(_STATE["log"][-30:])}


# ---------- Trang ----------
INDEX_HTML = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Vượt link cuty.io / shrinkme.click</title>
<style>
  body{font-family:system-ui,Segoe UI,Arial,sans-serif;max-width:760px;margin:40px auto;padding:0 16px;color:#222}
  h1{font-size:1.4rem}
  form{display:flex;gap:8px;flex-wrap:wrap}
  input[name=url]{flex:1;min-width:260px;padding:10px;font-size:15px;border:1px solid #ccc;border-radius:8px}
  button{padding:10px 18px;font-size:15px;border:0;border-radius:8px;background:#2563eb;color:#fff;cursor:pointer}
  button:disabled{background:#999;cursor:default}
  button.green{background:#16a34a}
  #out{margin-top:20px;white-space:pre-wrap;word-break:break-all;font-family:ui-monospace,Consolas,monospace}
  a.dest{color:#2563eb;font-size:18px}
  .err{color:#c00}.muted{color:#777;font-size:13px}
  pre{background:#f6f8fa;padding:12px;border-radius:8px;overflow:auto;max-height:300px}
  .manual{border:1px solid #f59e0b;background:#fffbeb;padding:14px;border-radius:10px;margin:12px 0}
  .log{font-size:12px;color:#555;background:#fafafa;border:1px solid #eee;border-radius:8px;padding:8px;max-height:160px;overflow:auto;white-space:pre-wrap}
</style></head><body>
<h1>Vượt link cuty.io / shrinkme.click</h1>
<form id="f">
  <input name="url" placeholder="https://cuty.io/... hoặc https://shrinkme.click/..." autocomplete="off" required>
  <label style="font-size:13px;display:flex;align-items:center;gap:4px"><input type="checkbox" id="manual" checked> Manual nếu captcha k tự pass</label>
  <button id="go">Lấy link</button>
</form>
<div id="out" class="muted">Dán link rồi bấm "Lấy link".</div>
<script>
const f=document.getElementById('f'),go=document.getElementById('go'),out=document.getElementById('out');
let poll=null;
async function start(){
  const url=f.url.value.trim(); if(!url) return;
  const manual=document.getElementById('manual').checked;
  go.disabled=true; out.className='muted'; out.innerHTML='Đang khởi động Chrome và vượt link...';
  try{
    const r=await fetch('/api/start?url='+encodeURIComponent(url)+'&manual='+(manual?1:0));
    const j=await r.json();
    if(!j.started){out.className='err';out.textContent='Busy: đang có 1 link chạy. Chờ xong rồi thử lại.';go.disabled=false;return;}
    if(poll) clearInterval(poll);
    poll=setInterval(status,1200); status();
  }catch(e){out.className='err';out.textContent='Lỗi mạng: '+e;go.disabled=false;}
}
async function status(){
  try{
    const j=await (await fetch('/api/status')).json();
    let h='';
    if(j.status==='running'||j.status==='needs_manual'){
      h+='<div class="muted">Đang chạy... '+esc(j.url||'')+'</div>';
    }
    if(j.status==='needs_manual'){
      h+='<div class="manual"><b>Cần giải captcha thủ công</b><br>'+esc(j.manual_msg||'')+'<br>';
      if(window._NOVNC){
        const vu='/vnc/vnc.html?autoconnect=1&path=vnc/ws&resize=scale&host='
          +encodeURIComponent(location.hostname)+'&port='+encodeURIComponent(location.port||'443');
        h+='<a href="'+vu+'" target="_blank" style="display:inline-block;margin-top:8px">Mở noVNC để điều khiển Chrome →</a><br>';
      } else {
        h+='<span class="muted">Giải trong cửa sổ Chrome đang mở trên máy này.</span><br>';
      }
      h+='<button class="green" onclick="cont()" style="margin-top:8px">Đã giải — Tiếp tục</button></div>';
    }
    if(j.status==='done'){
      h+='<div>Link đích:</div><a class="dest" href="'+esc(j.destination)+'" target="_blank">'+esc(j.destination)+'</a>';
      if(j.raw_text){h+='<div style="margin-top:14px">Raw text:</div><pre>'+esc(j.raw_text)+'</pre>';}
      stop();
    }
    if(j.status==='error'){h+='<div class="err">Lỗi: '+esc(j.error||'')+'</div>';stop();}
    if(j.log&&j.log.length){h+='<div class="log">'+j.log.map(esc).join('\\n')+'</div>';}
    out.className='';out.innerHTML=h;
  }catch(e){}
}
async function cont(){
  try{await fetch('/api/continue',{method:'POST'});}catch(e){}
}
function stop(){clearInterval(poll);poll=null;go.disabled=false;}
function esc(s){return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
f.onsubmit=e=>{e.preventDefault();start();};
window._NOVNC=__NOVNC__;
</script>
</body></html>"""


@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML.replace("__NOVNC__", "true" if NOVNC else "null")


@app.get("/api/start")
async def api_start(url: str = Query(...), manual: int = Query(...)):
    global _current_task
    # Kiểm tra task còn sống (không dùng _LOCK.locked() — race giữa check và acquire).
    if _current_task is not None and not _current_task.done():
        return JSONResponse({"started": False, "error": "busy"}, status_code=409)
    _current_task = asyncio.create_task(_job(url, bool(manual)))
    return {"started": True}


@app.get("/api/status")
def api_status():
    return _snap()


@app.post("/api/continue")
def api_continue():
    if _GATE is not None and _STATE.get("status") == "needs_manual":
        _GATE.release()
        return {"ok": True}
    return {"ok": False, "reason": "not waiting"}


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
