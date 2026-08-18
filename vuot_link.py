#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
vuot_link.py — Vượt link cuty.io / shrinkme.click (+ cuttty.com), in link đích (+ raw text).

Cách hoạt động (đã kiểm chứng):
  - Tự khởi động Chrome/Chromium THẬT qua subprocess với --remote-debugging-port
    (KHÔNG qua Playwright launch → Cloudflare Turnstile không phát hiện automation),
    rồi kết nối qua CDP (connect_over_cdp) để điều khiển. Trong Chrome thật:
      * Turnstile (cuty) tự pass (~30 giây).
      * reCAPTCHA (shrinkme) — click checkbox; chỉ tự pass khi IP/profile đủ trust,
        không thì cần giải thủ công (xem manual mode bên dưới).
  - Tự đi qua các trang interstitial: bấm Continue -> đợi captcha -> bấm tiếp ->
    bắt link đích khi URL rời host service.
  - UI tiếng Anh (ép --lang=en-US) để nút luôn là "Continue" / "I am not a robot".

Manual mode (khi captcha không tự pass):
  - `--manual` (CLI): tool dừng lại, in hướng dẫn, chờ bạn giải captcha trong cửa sổ
    Chrome rồi nhấn Enter → tool tự tiếp tục và bắt link đích.
  - Web app: truyền `manual_gate` (WebManualGate) — web page hiện nút "Tiếp tục",
    user giải captcha trong Chrome/noVNC rồi bấm nút đó.

Dùng CLI:
  python vuot_link.py <url>                 # cuty.io hoặc shrinkme.click
  python vuot_link.py https://cuty.io/xxxx --open
  python vuot_link.py https://shrinkme.click/xxxx --manual   # giải captcha thủ công
  python vuot_link.py -f links.txt

Dùng như thư viện (cho web app):
  from vuot_link import bypass_url
  dest, raw_text = bypass_url("https://cuty.io/xxxx", proxy="http://user:pass@host:port",
                               manual=True, manual_gate=my_gate)

Cài đặt:
  pip install playwright
  (Cần Chrome/Chromium. Trên Linux: apt-get install chromium, hoặc set CHROME_PATH.)
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

# Windows (console tiếng Việt = cp1258): ký tự Unicode như "\u2192", "\u1ed7" làm
# print() ra stdout/stderr bị UnicodeEncodeError. Ép UTF-8 để log không crash và
# tiếng Việt/mũi tên hiển thị đúng (trên Linux/Railway vốn đã UTF-8).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.stderr.write("Thiếu Playwright. Cài: pip install playwright\n")
    sys.exit(2)

# Host coi là interstitial (chưa phải link đích). Bao gồm cả domain service
# (shrinkme.io là trang chủ/dashboard của shrinkme — không phải link đích).
INTER_HOSTS = ("cuty.io", "cuttty.com", "shrinkme.click", "shrinkme.io", "shrinkme.me")

# Ứng cử viên trình duyệt (Windows Chrome/Edge, Linux chromium/chrome).
BROWSER_CANDIDATES = [
    os.environ.get("CHROME_PATH") or "",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
    "/usr/bin/google-chrome",
    "/usr/bin/google-chrome-stable",
    "/snap/bin/chromium",
]

# Nhãn nút/link thường gặp ở stage cuối (đa ngôn ngữ).
NEXT_LABELS = [
    "Get Link", "Get link", "Get the Link", "Continue", "Click here to continue",
    "Go to Link", "Go to link", "Go to URL", "Go ->", "Download",
    "Lấy link", "Lấy Link", "Tiếp tục", "Vào link", "Đi đến link", "Vào trang",
    "Continuer", "Obtenir le lien", "Télécharger",
]


def is_interstitial(url: str) -> bool:
    if not url or url.startswith(("about:", "chrome:", "data:", "blob:")):
        return True
    host = (urlsplit(url).hostname or "").lower()
    if not host:
        return True
    return any(host == h or host.endswith("." + h) for h in INTER_HOSTS)


def is_error_page(page) -> bool:
    """Trang đang là error page của Chrome (navigation lỗi, vd ERR_UNEXPECTED = 'error code 9')."""
    try:
        u = (page.url or "").lower()
    except Exception:
        u = ""
    return u.startswith("chrome-error://") or "chromewebdata" in u


# Quảng cáo nặng trên cuty.io/cuttty.com (Netpub, CleverCore, AdsCoreLoader,
# vaudona, llvpn, justkoalas) hay làm renderer Chrome crash vì OOM trên container
# RAM thấp (Railway) → "Aw, snap! error code 9". Các domain này KHÔNG liên quan
# tới vhit (/_v/s.js) hay Turnstile (cùng host cuty.io/cuttty.com/cdn.cuty.io),
# nên block chúng không ảnh hưởng tới việc enable nút.
HEAVY_AD_DOMAINS = (
    "fstatic.netpub.media",        # Netpub banner ads (nhiều slot, nặng nhất)
    "scripts.cleverwebserver.com",  # CleverCore loader (hay crash renderer)
    "sads.adsboosters.xyz",         # AdsCoreLoader (hay crash renderer)
    "kk.vaudona.com",               # popunder
    "llvpn.com",                    # tag.min.js
    "justkoalas.com",               # popstate redirect hijack
)


def setup_ad_blocking(context, log) -> bool:
    """Block ad nặng qua route interception: fulfill rỗng (status 200, body rỗng)
    thay vì abort → script vẫn "load" thành công (no-op), không trigger onerror,
    khó bị phát hiện adblock. Trả True nếu thiết lập được.

    Tắt bằng env BLOCK_ADS=0 (cho debug). Mặc định bật."""
    if os.environ.get("BLOCK_ADS", "1") != "1":
        return False

    def handler(route):
        try:
            u = route.request.url
        except Exception:
            u = ""
        if any(d in u for d in HEAVY_AD_DOMAINS):
            try:
                route.fulfill(status=200, body=b"", content_type="application/javascript")
                return
            except Exception:
                try:
                    route.abort()
                except Exception:
                    pass
                return
        try:
            route.continue_()
        except Exception:
            pass

    try:
        context.route("**/*", handler)
        return True
    except Exception as e:
        log(f"  ! không thiết lập được ad-block: {e!r}")
        return False


def find_browser() -> str:
    for c in BROWSER_CANDIDATES:
        if c and os.path.exists(c):
            return c
    sys.stderr.write("Không tìm thấy Chrome/Chromium. Cài Chrome hoặc set CHROME_PATH.\n")
    sys.exit(2)


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_open(port: int, timeout: float = 30.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=2):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def clean_stale_locks(profile_dir: str):
    """Xoá lock cũ của Chrome (SingletonLock/Cookie/Socket) do lần force-kill
    trước để lại — nếu không Chrome kế tiếp sẽ không mở được debug port."""
    try:
        p = Path(profile_dir)
        if not p.is_dir():
            return
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            f = p / name
            try:
                if f.exists() or f.is_symlink():
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


def launch_browser(profile: str, port: int, headless: bool,
                   invisible: bool = False, proxy: str | None = None) -> subprocess.Popen:
    exe = find_browser()
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--lang=en-US",
        "--disable-features=PrivacySandboxSettings4",
        "--window-size=1280,820",
        # Cần thiết khi chạy trong container (Linux, root, /dev/shm nhỏ):
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",            # container Linux không có GPU; vô hại trên Win/Mac
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
    if headless:
        args.append("--headless=new")
    elif invisible:
        # HEADED nhưng đẩy cửa sổ ra khỏi màn hình: Turnstile vẫn pass (tab vẫn
        # "visible") nhưng người dùng không thấy cửa sổ. (Off-screen hay fail với
        # cuty — không khuyến nghị, xem README.)
        args += ["--window-position=-32000,-32000"]
    args.append("about:blank")
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------- Hàm đọc/trả lời page (chống navigation destroying context) ----------
def safe_eval(page, js, *args, tries=8):
    for _ in range(tries):
        try:
            return page.evaluate(js, *args) if args else page.evaluate(js)
        except Exception as e:
            s = str(e)
            if "destroyed" in s or "Target closed" in s or "Execution context" in s:
                time.sleep(1.0)
                continue
            return None
    return None


# ---------- Tìm nút chính (chạy chung cho cuty #submit-button + shrinkme .btn-primary) ----------
# Mức ưu tiên: 1) cuty #submit-button (có data-ref), 2) nút/a .btn-primary|btn-success
# với nhãn "continue/get link/go ->", 3) bất kỳ nút/a có nhãn đúng NEXT_LABELS.
_BTN_MATCH_JS = r"""(() => {
  const seen = (b) => b && b.offsetParent !== null;
  let b = document.querySelector('#submit-button');   // cuty: luôn dùng nếu có
  const cand = Array.from(document.querySelectorAll('button, input[type=submit], a[role=button]')).filter(seen);
  const primaryRe = /btn-primary|btn-success/;
  const contRe = /^(click here to continue|continue|get link|get the link|go to link|go to url|go ->|lấy link|vào link|vào trang|tiếp tục)$/i;
  const finalRe = /^(get link|get the link|go to link|go ->|lấy link|vào link|download)$/i;
  if (!b) {
    b = cand.find(x => primaryRe.test((x.className||'').toString()) && contRe.test((x.textContent||'').trim()))
      || cand.find(x => contRe.test((x.textContent||'').trim()))
      || cand.find(x => finalRe.test((x.textContent||'').trim()));
  }
  return b;
})()"""

BTN_JS = "() => { const b = " + _BTN_MATCH_JS + "; if (!b) return null; return {id:b.id||'', cls:(b.className||'').toString().slice(0,40), disabled:!!b.disabled, text:(b.textContent||'').trim().slice(0,40), ref:b.getAttribute('data-ref')||''}; }"


def button_state(page):
    return safe_eval(page, BTN_JS)


def click_main_button(page):
    """Bấm nút chính (chỉ khi không disabled). Trả text nút hoặc None."""
    return safe_eval(page, "() => { const b = " + _BTN_MATCH_JS + "; if (!b || b.disabled) return null; b.click(); return (b.textContent||'').trim().slice(0,40); }")


def has_turnstile(page) -> bool:
    """Có widget Cloudflare Turnstile thật trên trang không."""
    v = safe_eval(page, r"""() => !!(document.querySelector('#turnstile-container')
        || document.querySelector('.cf-turnstile')
        || document.querySelector('iframe[src*="challenges.cloudflare.com"]'))""")
    return bool(v)


def has_recaptcha(page) -> bool:
    """Có widget Google reCAPTCHA thật trên trang không (shrinkme dùng v2/Enterprise)."""
    v = safe_eval(page, r"""() => !!(document.querySelector('.g-recaptcha,#g-recaptcha')
        || document.querySelector('iframe[src*="recaptcha"],iframe[src*="recaptcha.net"]'))""")
    return bool(v)


def has_captcha(page) -> bool:
    return has_turnstile(page) or has_recaptcha(page)


def click_recaptcha_anchor(page) -> bool:
    """Click checkbox reCAPTCHA (auto-trigger; chỉ pass khi IP/profile đủ trust)."""
    for f in page.frames:
        u = f.url or ""
        if "/recaptcha/api2/anchor" in u:
            try:
                f.click("#recaptcha-anchor", timeout=4000)
                return True
            except Exception:
                return False
    return False


def find_dest_in_pages(ctx) -> str | None:
    """Quét tất cả tab (kể cả popup) tìm URL đã rời host interstitial."""
    try:
        pages = list(ctx.pages)
    except Exception:
        return None
    for pg in pages:
        try:
            u = pg.url
        except Exception:
            u = None
        if u and not is_interstitial(u) and u.startswith("http"):
            return u
    return None


def extract_raw_text(ctx, dest: str, log) -> str | None:
    """Nếu trang đích là trang raw text (vd pastefy/.../raw, pastebin/raw),
    trả về nội dung text. Dùng tab đã ở dest, hoặc mở tab mới."""
    page = None
    opened_new = False
    for pg in ctx.pages:
        try:
            if (pg.url or "") == dest:
                page = pg
                break
        except Exception:
            pass
    if page is None:
        try:
            page = ctx.new_page()
            opened_new = True
            page.goto(dest, wait_until="load", timeout=30000)
        except Exception as e:
            log(f"  ! không tải được trang đích để đọc text: {e!r}")
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            return None
    try:
        info = safe_eval(page, r"""() => ({
            ct: document.contentType || '',
            text: (document.body && document.body.innerText) || ''
        })""", tries=4)
    except Exception:
        info = None
    if opened_new:
        try:
            page.close()
        except Exception:
            pass
    if not info:
        return None
    ct = (info.get("ct") or "").lower()
    text = (info.get("text") or "").strip()
    is_raw = (ct.startswith("text/") and not ct.startswith("text/html")) \
             or ct == "application/json" \
             or (dest.rstrip("/").endswith("/raw") and "<html" not in text.lower() and len(text) < 20000)
    if is_raw and text:
        return text
    return None


def wait_captcha_token(page, timeout: float, is_done=None) -> str:
    """Đợi captcha sinh token (Turnstile hoặc reCAPTCHA). Trả loại:len, '' nếu hết giờ."""
    end = time.time() + timeout
    while time.time() < end:
        if is_done and is_done():
            return ""
        t = safe_eval(page, r"""() => {
          const cf=document.querySelector('[name="cf-turnstile-response"]');
          if (cf && cf.value && cf.value.length > 10) return 'cf:'+cf.value.length;
          const g=document.querySelector('textarea[name="g-recaptcha-response"],#g-recaptcha-response');
          if (g && g.value && g.value.length > 10) return 'g:'+g.value.length;
          return '';
        }""")
        if t:
            return t
        time.sleep(1)
    return ""


def wait_button_enabled(page, timeout: float, is_done=None) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if is_done and is_done():
            return False
        b = button_state(page)
        if not b:
            return False
        if not b["disabled"]:
            return True
        time.sleep(1)
    return False


def extract_external_link(page) -> str | None:
    hrefs = safe_eval(page, r"""() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)""") or []
    for h in hrefs:
        if not h or not h.startswith("http") or is_interstitial(h):
            continue
        host = (urlsplit(h).hostname or "").lower()
        if any(s in host for s in ("facebook.", "twitter.", "x.com", "linkedin.",
                                   "t.me", "telegram.", "reddit.", "pinterest.")):
            continue
        if any(h.endswith(p) for p in ("/payout-rates", "/subscription", "/register",
                                        "/privacy", "/terms")):
            continue
        return h
    return None


def click_any_advance(page) -> bool:
    """Dự phòng: bấm nút/link theo nhãn khi không có #submit-button."""
    for label in NEXT_LABELS:
        for role in ("button", "link"):
            ok = safe_eval(page, r"""(lbl, role) => {
                const els = Array.from(document.querySelectorAll(role === 'button' ? 'button,a[role=button]' : 'a'));
                const m = els.find(e => (e.textContent||'').trim().toLowerCase() === lbl.toLowerCase());
                if (m) { m.click(); return true; }
                return false;
            }""", label, role)
            if ok:
                return True
    return False


# ---------- Manual gate: khi captcha không tự pass, chờ user tự giải ----------
class ManualGate:
    """Mặc định cho CLI: in hướng dẫn rồi chờ Enter. Web app dùng subclass
    chờ event (POST /api/continue)."""
    def __init__(self, log=print):
        self.log = log

    def wait(self, message: str, timeout: float | None = None) -> bool:
        self.log("  [!] " + message)
        self.log("      Giải xong trong cửa sổ Chrome rồi nhấn Enter để tool tiếp tục.")
        try:
            input()
        except EOFError:
            pass
        return True


def bypass_one(ctx, url: str, args, log) -> str | None:
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    try:
        page.on("dialog", lambda d: d.accept())
    except Exception:
        pass

    found = {"url": None}

    cancel_event = getattr(args, "cancel_event", None)

    def is_cancelled():
        return cancel_event is not None and cancel_event.is_set()

    def check(u):
        if u and not is_interstitial(u) and u.startswith("http"):
            found["url"] = u

    def on_frame(frame, owner):
        if frame != owner.main_frame:
            return
        try:
            check(frame.url)
        except Exception:
            pass

    page.on("framenavigated", lambda f: on_frame(f, page))
    try:
        ctx.on("page", lambda np: np.on("framenavigated", lambda f: on_frame(f, np)))
    except Exception:
        pass

    try:
        # Mở link đầu với retry: proxy/redirect lần đầu hay gặp error page
        # (ERR_UNEXPECTED = "error code 9", renderer crash do ads OOM). Reload ngay
        # thường crash lại (ads vẫn nặng) → chỉ goto lại sau khi chờ memory release.
        for attempt in range(1, 4):
            if is_cancelled():
                break
            try:
                log(f"→ Mở: {url}" + (f" (thử {attempt}/3)" if attempt > 1 else ""))
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
            except Exception as e:
                log(f"  ! goto lỗi: {e!r}")
            if not is_error_page(page):
                break
            log(f"  ~ error page (renderer crash?) — chờ 5s rồi goto lại ({attempt}/3) ...")
            time.sleep(5)

        deadline = time.time() + args.timeout
        last_sig = None
        stall = 0
        stage = 0
        last_clicked_sig = None
        same_clicks = 0
        error_reloads = 0

        while time.time() < deadline:
            if is_cancelled():
                log("  ~ đã hủy bởi người dùng.")
                break
            # Tự phục hồi nếu trang rơi vào error page giữa chừng.
            if is_error_page(page):
                error_reloads += 1
                if error_reloads > 6:
                    log("  ! error page liên tục — dừng auto.")
                    break
                log(f"  ~ error page giữa chừng — reload ({error_reloads}) ...")
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                except Exception as e:
                    log(f"  ! reload lỗi: {e!r}")
                time.sleep(5)
                continue
            d = find_dest_in_pages(ctx) or found["url"]
            if d:
                return d
            try:
                cur = page.url
            except Exception:
                cur = None
            if cur and not is_interstitial(cur):
                return cur

            ext = extract_external_link(page)
            if ext:
                return ext

            def is_done():
                return bool(found["url"] or find_dest_in_pages(ctx) or is_cancelled())

            btn = button_state(page)
            sig = json.dumps(btn, sort_keys=True) if btn else "no-btn"
            gate = getattr(args, "gate", None)

            # Không có nút nhưng có captcha (vd Cloudflare "Just a moment" gate
            # cả trang trước khi cuty render nút Continue) → đợi pass, không bỏ cuộc.
            if not btn and has_captcha(page):
                kind = "reCAPTCHA" if has_recaptcha(page) else "Turnstile"
                if not stall:
                    log(f"  ~ captcha gate ({kind}) trước khi có nút — đợi tự pass...")
                if has_recaptcha(page):
                    click_recaptcha_anchor(page)
                remaining = max(0.0, deadline - time.time())
                tok = wait_captcha_token(page, min(95.0, remaining), is_done)
                if not tok and args.manual and gate is not None:
                    if not gate.wait(f"Captcha {kind} (gate trang) không tự pass. Giải trong Chrome/noVNC rồi bấm 'Tiếp tục'.",
                                     timeout=max(0.0, deadline - time.time())):
                        break
                    tok = wait_captcha_token(page, 8.0, is_done)
                stall = 0
                last_sig = None
                time.sleep(2)
                continue

            if btn:
                if btn["disabled"]:
                    ref = btn.get("ref") or ""
                    txt = (btn.get("text") or "").lower()
                    captcha_here = "captcha" in ref or ("robot" in txt) or has_captcha(page)
                    # Chỉ chờ captcha khi có widget thật (tránh nhầm nút "Please wait"
                    # countdown của stage cuối chỉ là redirect).
                    if captcha_here:
                        kind = "reCAPTCHA" if has_recaptcha(page) else ("Turnstile" if has_turnstile(page) else "captcha")
                        log(f"  ~ stage captcha ({kind}): đợi tự pass (Chrome thật)...")
                        if has_recaptcha(page):
                            click_recaptcha_anchor(page)  # trigger checkbox (auto-pass nếu đủ trust)
                        remaining = max(0.0, deadline - time.time())
                        tok = wait_captcha_token(page, min(95.0, remaining), is_done)
                        if not tok and args.manual and gate is not None:
                            if not gate.wait(
                                f"Captcha {kind} không tự pass. Hãy giải trong cửa sổ Chrome"
                                f" (hoặc qua noVNC), rồi bấm 'Tiếp tục'.",
                                timeout=max(0.0, deadline - time.time())):
                                break
                            tok = wait_captcha_token(page, 8.0, is_done)
                    # Đợi nút enable (countdown ngắn).
                    wait_button_enabled(page, min(30.0, deadline - time.time()), is_done)
                    btn = button_state(page)

                if btn and not btn["disabled"]:
                    click_sig = json.dumps(btn, sort_keys=True)
                    if click_sig == last_clicked_sig:
                        same_clicks += 1
                    else:
                        same_clicks = 1
                        last_clicked_sig = click_sig
                    if same_clicks > 3:
                        log("  ~ nút không đổi sau nhiều lần bấm — dừng auto.")
                        if args.manual and gate is not None:
                            if not gate.wait("Auto trượt. Hãy tự click trong Chrome để tới link đích, rồi bấm 'Tiếp tục'.",
                                             timeout=max(0.0, deadline - time.time())):
                                break
                            stall = 0
                            continue
                        break
                    stage += 1
                    log(f"  • stage {stage}: bấm nút (ref={btn.get('ref')!r}, text={btn.get('text')!r})")
                    click_main_button(page)
                    t0 = time.time()
                    nav_seen = False
                    while time.time() < deadline:
                        if is_cancelled():
                            break
                        d = find_dest_in_pages(ctx) or found["url"]
                        if d:
                            return d
                        try:
                            nu = page.url
                        except Exception:
                            nu = None
                        if nu and not is_interstitial(nu):
                            return nu
                        ext = extract_external_link(page)
                        if ext:
                            return ext
                        b2 = button_state(page)
                        if b2 and json.dumps(b2, sort_keys=True) != sig:
                            break
                        if not b2:
                            if not nav_seen:
                                nav_seen = True
                                time.sleep(1.5)
                                continue
                            time.sleep(1.5)
                            d = find_dest_in_pages(ctx)
                            if d:
                                return d
                            if not button_state(page) and not extract_external_link(page):
                                break
                        time.sleep(0.5)
                        if time.time() - t0 > 35:
                            break
                    stall = 0
                    continue

            if sig == last_sig:
                stall += 1
                if stall >= 3:
                    if click_any_advance(page):
                        log("  ~ bấm nút dự phòng theo nhãn")
                        time.sleep(3)
                        stall = 0
                        continue
                    if args.manual and gate is not None:
                        if not gate.wait("Auto trượt. Hãy tự click captcha/nút trong Chrome để tới link đích, rồi bấm 'Tiếp tục'.",
                                         timeout=max(0.0, deadline - time.time())):
                            break
                        stall = 0
                        continue
                    break
            else:
                stall = 0
            last_sig = sig
            time.sleep(5)

        return found["url"]
    except Exception as e:
        log(f"  ! lỗi: {e!r}")
        return found["url"]


def bypass_url(url: str, profile_dir: str | None = None, headless: bool = False,
               invisible: bool = False, proxy: str | None = None,
               timeout: float = 180.0, manual: bool = False, no_text: bool = False,
               log=print, keep_open: bool = False,
               manual_gate=None, cancel_event=None, proc_holder=None) -> tuple[str | None, str | None]:
    """Vượt 1 link (cuty.io / shrinkme.click...), trả (link_đích, raw_text|None).
    Dùng lại được cho web app. `manual_gate`: đối tượng ManualGate (mặc định CLI input
    nếu manual=True mà không truyền gate)."""
    profile_dir = profile_dir or str(Path.home() / ".vuot-link-profile")
    Path(profile_dir).mkdir(parents=True, exist_ok=True)
    clean_stale_locks(profile_dir)
    if not url.startswith("http"):
        url = "https://" + url
    gate = manual_gate or (ManualGate(log) if manual else None)
    port = free_port()
    proc = launch_browser(profile_dir, port, headless, invisible, proxy)
    if proc_holder is not None:
        proc_holder["proc"] = proc
    try:
        if not port_open(port, 30):
            log("Không mở được debug port Chrome.")
            return (None, None)
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            if setup_ad_blocking(ctx, log):
                log("  • ad-block: đã block quảng cáo nặng (chống crash renderer OOM).")
            args_ns = SimpleNamespace(manual=manual, timeout=timeout, gate=gate, cancel_event=cancel_event)
            dest = bypass_one(ctx, url, args_ns, log)
            raw = None
            if dest and not no_text:
                raw = extract_raw_text(ctx, dest, log)
            try:
                browser.close()
            except Exception:
                pass
        return (dest, raw)
    finally:
        if proc_holder is not None and proc_holder.get("proc") is proc:
            proc_holder.pop("proc", None)
        if not keep_open:
            try:
                proc.terminate()
                proc.wait(timeout=8)
            except Exception:
                pass
            # Đảm bảo Chrome chết — terminate có thể bị kẹt (zombie trong container).
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
            except Exception:
                pass


def main():
    ap = argparse.ArgumentParser(
        prog="vuot_link.py",
        description="Tự vượt link cuty.io / cuttty.com, in ra link đích.",
    )
    ap.add_argument("urls", nargs="*", help="Một hoặc nhiều link cuty.io cần vượt.")
    ap.add_argument("-f", "--file", help="Đọc link từ file (mỗi dòng 1 link).")
    ap.add_argument("--open", action="store_true",
                    help="Mở link đích trong trình duyệt mặc định sau khi lấy xong.")
    ap.add_argument("--headless", action="store_true",
                    help="Chạy headless (cảnh báo: Turnstile hay fail).")
    ap.add_argument("--manual", action="store_true",
                    help="Nếu auto trượt, dừng lại cho người dùng tự giải captcha trong Chrome.")
    ap.add_argument("--timeout", type=float, default=240.0,
                    help="Giới hạn thời gian mỗi link (giây). Mặc định: 240.")
    ap.add_argument("--profile", default=None,
                    help="Thư mục profile. Mặc định: ~/.vuot-link-profile.")
    ap.add_argument("--proxy", default=None,
                    help="Proxy cho Chrome (vd http://user:pass@host:port). Cần residential để pass Turnstile trên server.")
    ap.add_argument("--keep-open", action="store_true",
                    help="Đừng đóng Chrome khi xong (debug).")
    ap.add_argument("--invisible", action="store_true",
                    help="Ẩn cửa sổ Chrome off-screen. CẢNH BÁO: Turnstile cuty hay fail khi ẩn.")
    ap.add_argument("--no-text", action="store_true",
                    help="Không trích xuất text nội dung nếu trang đích là raw text.")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.file:
        try:
            for line in Path(args.file).read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    urls.append(line)
        except OSError as e:
            sys.stderr.write(f"Không đọc được file {args.file}: {e}\n")
            sys.exit(2)
    if not urls:
        ap.print_help(sys.stderr)
        sys.exit(2)

    def log(m):
        print(m, file=sys.stderr, flush=True)

    if args.headless:
        log("• Chế độ headless (cảnh báo: Turnstile hay fail với cuty).")
    elif args.invisible and not args.manual:
        log("• Cửa sổ Chrome ẩn off-screen (cảnh báo: Turnstile cuty hay fail khi ẩn).")
    else:
        log("• Cửa sổ Chrome hiện trên màn hình (mặc định, ổn định với Turnstile).")

    ok_all = True
    for url in urls:
        dest, raw = bypass_url(
            url, profile_dir=args.profile, headless=args.headless,
            invisible=args.invisible, proxy=args.proxy, timeout=args.timeout,
            manual=args.manual, no_text=args.no_text, log=log, keep_open=args.keep_open,
        )
        if dest:
            print(dest, flush=True)
            if raw:
                print("---- raw text ----", flush=True)
                print(raw, flush=True)
                print("------------------", flush=True)
            if args.open:
                try:
                    webbrowser.open(dest)
                except Exception:
                    pass
        else:
            print(f"[!] Không lấy được link đích cho: {url}", file=sys.stderr)
            ok_all = False
    sys.exit(0 if ok_all else 1)


if __name__ == "__main__":
    main()
