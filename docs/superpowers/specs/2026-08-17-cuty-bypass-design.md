# Tool tự vượt link cuty.io — Thiết kế

Ngày: 2026-08-17
Tác giả: ZCode (theo yêu cầu người dùng)

## Mục tiêu
Tạo tool CLI chạy lại được nhiều lần: nhận một link `cuty.io` (mỗi lần một link khác
nhưng cùng dạng cuty.io) và trả về link đích thật sau khi vượt qua các trang interstitial
(quảng cáo + countdown + captcha Cloudflare Turnstile).

## Quy trình thực tế của cuty.io (đã khảo sát)
1. `https://cuty.io/<id>` → 302 → `https://cuttty.com/<id>?auth_token=<token>` → 302 →
   về `cuty.io/<id>` → kết thúc ở trang đích `cuttty.com/<id>`.
2. Trang 1: form `#free-submit-form` với nút `#submit-button` (label "Continue").
   Form có trường ẩn token Cloudflare Turnstile. Bấm Continue chỉ thành công khi
   Turnstile đã sinh token.
3. Trang sau: interstitial thứ hai với countdown (thường ~10s) + có thể thêm một
   Turnstile + nút "Get Link" / "Continue" dẫn tới link đích, hoặc tự redirect.
4. Cuối cùng: browser được điều hướng tới link đích (host khác cuty.io/cuttty.com),
   hoặc một nút/link trên trang cuối chứa link đích.

Lưu ý: lớp `img-inter` (overlay full-page) che nút Continue — phải click qua DOM/CUA
hoặc chờ overlay hết, không phải locator action thường.

## Nền tảng (đã kiểm chứng)
- Python 3 + Playwright (chỉ dùng `connect_over_cdp` để điều khiển).
- **Tự khởi động Chrome/Edge thật qua subprocess** với `--remote-debugging-port=<port>`
  và một user-data-dir riêng, KHÔNG qua `playwright.launch_*` (vì launch qua Playwright
  bị Cloudflare Turnstile phát hiện automation → không sinh token). Sau đó kết nối
  qua `connect_over_cdp`. Trong Chrome "thật" này, Turnstile tự pass (~30s).
- Profile persistent riêng (`~/.vuot-link-profile`) để tích lũy cookie/fingerprint,
  các lần sau càng dễ pass.
- UI ép tiếng Anh (`--lang=en-US`) để nút luôn "Continue"/"I am not a robot".

## Tại sao không dùng Playwright launch / patchright
- `playwright.launch_persistent_context` (dù có stealth): Turnstile KHÔNG render
  iframe, không sinh token (Cloudflare phát hiện `--enable-automation`).
- `patchright` (stealth fork): `window.turnstile` vẫn `undefined`, token = 0 sau 50s.
- **Chrome subprocess + connect_over_cdp**: `window.turnstile` = "object", token
  dài 794 ký tự, nút enable → PASS. Đây là cách duy nhất hoạt động.

## Quy trình thực tế (đã xác nhận với link mẫu)
1. `cuty.io/<id>` → redirect → `cuttty.com/<id>` (stage 1, nút `#submit-button`
   `data-ref="first"`, "Continue", enable sau ~3s).
2. Bấm → POST (cùng URL) → stage 2 (nút `data-ref="captcha"`, "I am not a robot",
   disabled). Có widget Turnstile (`#turnstile-container[data-sitekey]`). Turnstile
   tự pass trong Chrome thật (~30s) → nút enable.
3. Bấm → stage 3 (nút `data-ref="show"`, "Go ->"). Bấm → trình duyệt điều hướng
   tới link đích (URL rời host cuty.io/cuttty.com).

URL KHÔNG đổi giữa stage 1-2-3 (POST cùng URL) → phát hiện stage theo `data-ref`/
text/disabled của nút, KHÔNG theo URL. Link đích bắt khi URL (của tab bất kỳ, kể cả
popup) thoát host interstitial.

## Giao diện (CLI)
```
python vuot_link.py <url-cuty> [<url-cuty> ...] [tùy chọn]
```
Tùy chọn:
- `--open`            : mở link đích trong trình duyệt mặc định sau khi lấy xong.
- `--headless`        : chạy headless (cảnh báo: Turnstile hay fail).
- `--timeout <s>`     : giới hạn thời gian mỗi link (mặc định 120s).
- `--no-manual`       : tắt fallback "người dùng tự click captcha".
- `--profile <dir>`   : thư mục profile (mặc định ~/.vuot-link-profile).

Output: in link đích ra stdout (mỗi link 1 dòng). Link lỗi in ra stderr.

## Luồng xử lý chính (mỗi link)
1. Khởi động Chrome headed + persistent profile + stealth cơ bản:
   - arg `--disable-blink-features=AutomationControlled`
   - init script xóa `navigator.webdriver`.
2. `page.goto(url)`, theo dõi cả navigation.
3. Vòng lặp vượt interstitial (tối đa N stage):
   a. Nếu `page.url` đã thoát cuty.io/cuttty.com → đó là link đích, kết thúc.
   b. Đảm bảo Turnstile đã pass cho trang hiện tại:
      - Đợi tối đa ~10s cho `cf-turnstile-response` có giá trị (auto-pass).
      - Nếu chưa có, tìm iframe `challenges.cloudflare.com` và click checkbox.
      - Nếu vẫn chưa có và `--no-manual` = false: in hướng dẫn, chờ người dùng
        giải captcha trong browser rồi nhấn Enter trong terminal.
   c. Tìm nút tiếp theo để bấm: `#submit-button`, hoặc nút/link có text
      "Get Link"/"Continue"/"Go to"/"Download"/"Lấy link" (click qua DOM CUA nếu
      locator bị overlay che). Bấm và chờ stage kế tiếp.
   d. Nếu không tìm được nút trong ~15s và URL vẫn interstitial → báo lỗi/thoát.
4. Khi URL thoát interstitial → in link đích. (Tuỳ) mở trong browser mặc định.
5. Đóng trang (giữ context/profile).

## Chống rác / an toàn
- Bỏ qua popup/tab quảng cáo cuty mở ra: chỉ theo dõi tab chính, không click vào
  tab phụ, không tải resource广告 ảnh.
- Trang cuối có thể là "Get Link" button dẫn ra ngoài — phát hiện cả hai:
  navigation rời host cuty, hoặc href của nút cuối trỏ ra host ngoài.

## Trường hợp lỗi
- Hết timeout mà chưa ra link đích → in lỗi stderr, exit code != 0.
- Turnstile không pass kể cả sau fallback thủ công → báo rõ.

## Phạm vi / ngoài phạm vi
- Chỉ hỗ trợ cuty.io (và cuttty.com). Không mở rộng sang shortener khác trong tool này.
- Không tự bypass captcha bằng dịch vụ trả phí (không cần API key).
- Không lưu trữ/đăng lại link đích bất kỳ đâu ngoài local.

## Giả định
- Máy có Python 3 + Playwright đã cài (`pip install playwright`).
- Đã cài Chrome trên Windows (đã xác nhận).
- Lần đầu có thể phải click captcha 1 cái; các lần sau Chrome nhớ profile sẽ mượt hơn.

