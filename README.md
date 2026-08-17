# vuot_link — Tự vượt link cuty.io / shrinkme.click

Tool vượt các trang interstitial của **cuty.io** (Cloudflare Turnstile) và
**shrinkme.click** (Google reCAPTCHA) + countdown, trả về **link đích thật**
(kèm raw text nếu đích là paste raw). Có 2 chế độ: **CLI** và **web app** (deploy
Railway, nhập link trên trình duyệt). Có **manual mode**: khi captcha không tự
pass, bạn tự giải captcha trong Chrome (local) hoặc qua **noVNC** (Railway) rồi
bấm "Tiếp tục", tool tự tiếp tục.

> ⚠️ **Lưu ý IP (quan trọng):** Captcha tự pass cần **IP residential**. Trên máy
> nhà bạn (IP residential) thì thường OK. Trên **Railway/server (IP datacenter)**
> thì Turnstile/reCAPTCHA thường **không tự pass** → 2 cách: (a) set `PROXY`
> residential (auto pass), hoặc (b) bật **manual mode + noVNC** để tự giải tay qua
> web.

## Cách hoạt động (đã kiểm chứng)
Khởi động **Chrome/Chromium thật** qua subprocess (chỉ `--remote-debugging-port`,
không qua Playwright launch → Cloudflare/Google không phát hiện automation) rồi
điều khiển qua CDP.
- **cuty.io**: Turnstile tự pass trong Chrome thật (~30s). Tool bấm
  `Continue → I am not a robot → Go ->` và bắt link đích khi URL rời host.
- **shrinkme.click**: reCAPTCHA — tool **click checkbox** tự động; chỉ tự pass
  khi IP/profile đủ trust, không thì bật **manual mode** để bạn giải tay.
- Trên server không màn hình, chạy **headed dưới Xvfb** (headless fail).

## Cài đặt (local)
```bash
pip install -r requirements.txt   # playwright, fastapi, uvicorn, python-multipart
```
Cần **Chrome** (Windows/Mac) hoặc **chromium** (Linux: `apt-get install chromium`),
hoặc set `CHROME_PATH` trỏ tới file chrome/chromium.

## Dùng CLI
```bash
python vuot_link.py https://cuty.io/xxxx                          # in link đích
python vuot_link.py https://shrinkme.click/xxxx --manual          # giải captcha tay
python vuot_link.py https://cuty.io/xxxx --open                   # mở link đích
python vuot_link.py -f links.txt                                  # nhiều link
python vuot_link.py https://cuty.io/xxxx --proxy http://user:pass@host:port
```
Output: link đích ra stdout (mỗi link 1 dòng). Nếu đích là raw text, in kèm
`---- raw text ----`. Dễ pipe: `python vuot_link.py https://cuty.io/x | xargs curl`.

`--manual`: khi captcha không tự pass, tool in hướng dẫn và **chờ Enter** — bạn
giải captcha trong cửa sổ Chrome rồi nhấn Enter, tool tự tiếp tục và bắt link đích.

Tùy chọn: `--open --headless --manual --timeout --profile --proxy --keep-open --invisible --no-text`.

## Dùng web app (local)
```bash
python app.py          # mở http://localhost:8080
```
Trang có ô nhập link + ô **"Manual nếu captcha k tự pass"**. Bấm "Lấy link":
- Auto pass → ra link đích (+ raw text).
- Nếu captcha không tự pass → hiện **"Cần giải captcha thủ công"** + nút
  **"Đã giải — Tiếp tục"**. Giải trong cửa sổ Chrome đang mở rồi bấm nút → tool
  tiếp tục.

### API
- `GET  /api/start?url=<link>&manual=1` → `{"started":true}` — chạy nền, trả ngay.
- `GET  /api/status` → `{"status":"running|needs_manual|done|error","destination","raw_text","error","manual_msg","log":[...]}` — poll mỗi ~1.2s.
- `POST /api/continue` → nhả manual gate (khi `status=needs_manual`).
- `GET  /api?url=<link>` → blocking, auto-only (cho script): `{"ok":true,"destination","raw_text"}`.
- `GET  /health` → `{"ok":true}`.

Env: `PORT` (8080), `PROXY`, `PROFILE_DIR`, `TIMEOUT` (240), `NOVNC` (`1` bật noVNC),
`NOVNC_DIR` (`/usr/share/novnc`), `VNC_HOST`/`VNC_PORT`.

## Deploy Railway (hướng dẫn chi tiết)

Repo đã sẵn sàng deploy — không cần sửa gì thêm:

| File | Vai trò |
|------|---------|
| `Dockerfile` | Image Python 3.11 + Chromium + Xvfb + x11vnc + noVNC + tini |
| `railway.toml` | Cấu hình Railway: healthcheck `/health` + restart policy |
| `start.sh` | Khởi Xvfb (đợi sẵn sàng) → x11vnc → uvicorn trên `$PORT` |

noVNC chạy **cùng port với app** (WS proxy `/vnc/ws`) nên không cần mở port thứ 2.

### Yêu cầu trước khi deploy

- 1 repo trên **GitHub** (public/private đều được).
- (Khuyến nghị) 1 **proxy residential** nếu muốn captcha tự pass — Bright Data, IPRoyal, Smartproxy...
- Tài khoản Railway (free/Pro đều chạy; Chrome cần RAM ≥ 512MB).

### Cách 1 — Deploy bằng Dashboard (khuyên dùng)

1. Đẩy repo lên GitHub:
   ```bash
   git init && git add . && git commit -m "deploy" && git branch -M main
   git remote add origin https://github.com/<user>/<repo>.git
   git push -u origin main
   ```
2. Mở [railway.com](https://railway.com) → **New Project** → **Deploy from GitHub repo** → chọn repo này. Railway tự nhận `Dockerfile`.
3. Cấu hình **Variables** (project → Settings → Variables):

   | Biến | Giá trị | Ghi chú |
   |------|---------|---------|
   | `PROXY` | `http://user:pass@residential-proxy:port` | Khuyên dùng để captcha auto-pass trên IP datacenter |
   | `NOVNC` | `1` | Đã set sẵn trong Dockerfile — giữ để dùng manual remote |
   | `TIMEOUT` | `240` | Tuỳ chọn |
   | `PROFILE_DIR` | `/data/profile` | Đã set sẵn trong Dockerfile |

   > Không cần set `PORT` — Railway tự cấp qua `$PORT`.

4. Gắn **Volume** (project → Settings → Volumes): mount `/data` → giữ profile Chrome qua các lần restart (giúp captcha dễ pass hơn sau khi "warm").
5. Chờ build xong (lần đầu vài phút vì cài Chromium). Railway cấp domain `https://<app>.up.railway.app` → mở ra là giao diện nhập link.

### Cách 2 — Deploy bằng Railway CLI

```bash
npm i -g @railway/cli
railway login
railway init
railway up                                   # build + deploy từ thư mục hiện tại
railway variables set PROXY="http://user:pass@residential-proxy:port" NOVNC=1
railway volume add -m /data                  # profile persistent (tuỳ chọn)
railway domain                               # lấy URL truy cập
```

### Kiểm tra sau deploy

```bash
# 1) Healthcheck
curl https://<app>.up.railway.app/health
# → {"ok":true}

# 2) Vượt 1 link (auto-only, blocking)
curl "https://<app>.up.railway.app/api?url=https://cuty.io/xxxx"
# → {"ok":true,"destination":"https://...","raw_text":null}

# 3) Dùng web: mở https://<app>.up.railway.app, dán link, bấm "Lấy link"
```

### Khi captcha không tự pass (IP datacenter yếu)

1. Mở web → tick ô **"Manual nếu captcha k tự pass"** → dán link → **"Lấy link"**.
2. Khi hiện **"Cần giải captcha thủ công"** → bấm **"Mở noVNC để điều khiển Chrome"**.
3. Giải captcha ngay trong noVNC → quay lại tab web → bấm **"Đã giải — Tiếp tục"** → tool tự bắt link đích.

### Xử lý lỗi thường gặp

| Triệu chứng | Nguyên nhân | Cách xử lý |
|-------------|-------------|------------|
| `/api` trả `{"ok":false}` | IP datacenter bị chặn captcha | Set `PROXY` residential, hoặc dùng manual + noVNC |
| Deploy fail, log `Xvfb failed` | Display ảo không khởi động được | Xem log `start.sh`, redeploy |
| Build chậm | Đang cài Chromium | Bình thường ở lần đầu; chờ vài phút |
| Hết RAM / bị kill | Chrome tốn RAM | Nâng RAM plan; chỉ chạy 1 link/lần |
| Log `stage captcha: đợi...` rồi timeout | Turnstile không tự pass | Dùng manual mode + noVNC |

### Chạy thử local bằng Docker (trước khi đẩy Railway)

```bash
docker build -t vuot-link .
docker run --rm -p 8080:8080 -e NOVNC=1 vuot-link
# mở http://localhost:8080
```

## Giới hạn
- **Hỗ trợ cuty.io / cuttty.com / shrinkme.click / shrinkme.io** (cùng họ).
  Shortener khác cần thêm handler (xem `bypass_one` trong `vuot_link.py`).
- **reCAPTCHA Enterprise (image challenge)** không tự giải được — cần Google
  login + IP trust, hoặc giải tay (manual mode). Tool **không** giải captcha
  trả phí.
- **Turnstile cần IP residential + profile warm.** IP datacenter → cần `PROXY`
  residential hoặc manual mode. Headless fail → phải headed (Xvfb trên server).
- Mỗi request launch 1 Chrome (~30-60s, tốn RAM) → web app có lock chạy
  **1 link/lần**. Cần song song thì chạy nhiều instance.
- Tool không gửi link đi đâu ngoài local/proxy.

## Cấu trúc
```
vuot_link.py      — tool chính + hàm bypass_url() + ManualGate (dùng lại cho web)
app.py            — web app FastAPI: trang nhập link, /api/start|status|continue,
                    /vnc/ws (WS proxy VNC cho noVNC manual remote)
Dockerfile        — image Railway (python + chromium + xvfb + x11vnc + novnc)
start.sh          — khởi Xvfb (đợi sẵn sàng) + x11vnc + uvicorn (entrypoint Docker)
railway.toml      — cấu hình Railway: healthcheck /health + restart policy
requirements.txt  — phụ thuộc
```
