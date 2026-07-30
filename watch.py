#!/usr/bin/env python3
"""
CGV 상영 스케줄 감시기.

Cloudflare 봇 차단 때문에 일반 HTTP 클라이언트로는 403이 떨어진다.
그래서 Playwright로 실제 Chromium을 띄우고, 페이지 컨텍스트 안에서
CGV 내부 API(/api/v1/booking/searchSchByMov)를 fetch 한다.

감지 대상 두 가지:
  A. 새 회차 등장  - 직전 스냅샷에 없던 상영 회차가 생긴 경우 (= 새 날짜 예매 오픈)
  B. 잔여좌석 증가 - 매진/임박 회차에 취소표가 풀린 경우
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.sync_api import sync_playwright, Error as PWError

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "config.json")
# 로컬(맥)과 GitHub Actions가 각자 스냅샷을 들고 있어야 서로 덮어쓰지 않는다.
STATE_PATH = os.path.join(BASE, os.environ.get("STATE_FILE", "state.json"))
LOG_PATH = os.path.join(BASE, "watch.log")

KST = ZoneInfo("Asia/Seoul")
HOME = "https://cgv.co.kr/"
BOOK_URL = "https://cgv.co.kr/cnm/movieBook/movie"
API_PATH = (
    "/api/v1/booking/searchSchByMov"
    "?coCd=A420&siteNo={site}&scnYmd={ymd}&movNo={mov}&rtctlScopCd=08"
)
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
LOG_MAX_BYTES = 2 * 1024 * 1024


def log(msg):
    stamp = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{stamp}] {msg}"
    print(line, flush=True)
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def notify(cfg, title, body, priority=4, tags="clapper", click=None):
    """ntfy로 푸시 전송. 실패해도 감시 자체는 계속 돌아야 하므로 예외를 삼킨다."""
    # 알림 주소는 비밀이라 코드/설정 파일에 두지 않는다.
    # 맥에서는 launchd 환경변수로, GitHub Actions에서는 Secrets로 주입한다.
    server = (
        os.environ.get("NTFY_SERVER") or cfg.get("ntfy_server") or "https://ntfy.sh"
    ).rstrip("/")
    topic = os.environ.get("NTFY_TOPIC") or cfg.get("ntfy_topic")
    if not topic:
        log("!! NTFY_TOPIC 환경변수가 없음 - 푸시 건너뜀")
        return False

    headers = {
        "Title": title.encode("utf-8"),
        "Priority": str(priority),
        "Tags": tags,
        "Markdown": "yes",
    }
    if click:
        headers["Click"] = click

    req = urllib.request.Request(
        f"{server}/{topic}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status < 300
    except (urllib.error.URLError, OSError) as e:
        log(f"!! ntfy 전송 실패: {e}")
        return False


def hhmm(t):
    """CGV는 '0630', 심야는 '2500'(=다음날 01:00) 형태로 준다."""
    if not t or len(t) < 3:
        return t or "??:??"
    t = t.zfill(4)
    h, m = int(t[:2]), t[2:]
    if h >= 24:
        return f"{h - 24:02d}:{m}(익일)"
    return f"{h:02d}:{m}"


def ymd_label(ymd):
    try:
        d = datetime.strptime(ymd, "%Y%m%d").date()
        return f"{d.month}/{d.day}({'월화수목금토일'[d.weekday()]})"
    except ValueError:
        return ymd


def fetch_schedules(page, site, mov, ymds):
    """페이지 컨텍스트에서 여러 날짜를 한 번에 조회. (ymd -> rows) 반환."""
    return page.evaluate(
        """async ({site, mov, ymds, path}) => {
            const out = {};
            for (const ymd of ymds) {
                const url = path.replace('{site}', site)
                                .replace('{ymd}', ymd)
                                .replace('{mov}', mov);
                try {
                    const res = await fetch(url, {headers: {'Accept': 'application/json'}});
                    if (!res.ok) { out[ymd] = {error: 'HTTP ' + res.status}; continue; }
                    const j = await res.json();
                    if (j.statusCode !== 0) { out[ymd] = {error: j.statusMessage || 'statusCode ' + j.statusCode}; continue; }
                    out[ymd] = {rows: (j.data || []).map(s => ({
                        scnsNo: s.scnsNo, scnsNm: s.scnsNm, expoScnsNm: s.expoScnsNm,
                        fmt: s.movkndDsplNm, start: s.scnsrtTm, end: s.scnendTm,
                        free: parseInt(s.frSeatCnt, 10), total: parseInt(s.stcnt, 10),
                        movNm: s.movNm, siteNm: s.siteNm,
                    }))};
                } catch (e) {
                    out[ymd] = {error: String(e)};
                }
            }
            return out;
        }""",
        {"site": site, "mov": mov, "ymds": ymds, "path": API_PATH},
    )


def pick_dates(known_ymds, today, days_ahead, pad, full_scan):
    """매 실행마다 30일 전부 긁으면 요청이 과하다.
    평소엔 '이미 회차가 열린 날짜 + 그 뒤 pad일'만 보고,
    주기적으로만 전체 구간을 훑는다."""
    if full_scan or not known_ymds:
        return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(days_ahead)]

    todays = today.strftime("%Y%m%d")
    live = sorted(y for y in known_ymds if y >= todays)
    if not live:
        return [(today + timedelta(days=i)).strftime("%Y%m%d") for i in range(days_ahead)]

    last = datetime.strptime(live[-1], "%Y%m%d").date()
    tail = [(last + timedelta(days=i)).strftime("%Y%m%d") for i in range(1, pad + 1)]
    limit = (today + timedelta(days=days_ahead - 1)).strftime("%Y%m%d")
    return sorted(set(live + tail + [todays]) - {y for y in tail if y > limit})


def run_target(page, cfg, state, target):
    label = target.get("label") or target.get("movNm", "?")
    site = target["siteNo"]
    mov = target["movNo"]
    match = (target.get("screen_match") or "").upper()
    days_ahead = int(target.get("days_ahead", 30))
    pad = int(target.get("lookahead_pad", 3))

    key = f"{mov}|{site}"
    targets = state.setdefault("targets", {})
    # 최초 실행에서는 이미 열려 있던 회차 전부가 '새 회차'로 잡힌다.
    # 그러면 알림 폭탄이 되므로 첫 회는 기준선만 만든다.
    first_run = key not in targets
    prev = targets.setdefault(key, {})
    runs = state.setdefault("runs", 0)
    full_every = int(cfg.get("full_scan_every_n_runs", 10))
    full_scan = (runs % full_every == 0)

    today = datetime.now(KST).date()
    ymds = pick_dates(
        {k.split("_", 1)[0] for k in prev}, today, days_ahead, pad, full_scan
    )
    log(f"[{label}] {'전체' if full_scan else '증분'} 스캔 {len(ymds)}일 ({ymds[0]}~{ymds[-1]})")

    result = fetch_schedules(page, site, mov, ymds)

    errors = [f"{y}:{v['error']}" for y, v in result.items() if "error" in v]
    if errors and len(errors) == len(ymds):
        raise RuntimeError(f"전 날짜 조회 실패 - {errors[0]}")
    if errors:
        log(f"[{label}] 일부 날짜 조회 실패: {', '.join(errors[:3])}")

    ncfg = cfg.get("notify", {})
    want_new = ncfg.get("new_showtime", True)
    want_free = ncfg.get("seats_freed", True)
    watch_at_or_below = int(ncfg.get("seats_freed_when_prev_at_or_below", 5))
    min_gain = int(ncfg.get("seats_freed_min_gain", 1))
    cooldown = int(ncfg.get("cooldown_seconds", 900))

    now = int(time.time())
    new_hits, free_hits = [], []
    seen_now = {}

    for ymd in ymds:
        entry = result.get(ymd, {})
        if "rows" not in entry:
            # 조회 실패한 날짜는 스냅샷에서 지우지 않는다 (사라진 걸로 오인하면 안 됨)
            for k in prev:
                if k.startswith(ymd + "_"):
                    seen_now[k] = prev[k]
            continue

        for s in entry["rows"]:
            screen = s.get("expoScnsNm") or s.get("scnsNm") or ""
            if match and match not in (s.get("scnsNm", "") + " " + screen + " " + (s.get("fmt") or "")).upper():
                continue

            k = f"{ymd}_{s['scnsNo']}_{s['start']}"
            cur = {
                "free": s["free"], "total": s["total"],
                "screen": s.get("scnsNm"), "fmt": s.get("fmt"),
                "start": s["start"], "movNm": s.get("movNm"),
                "siteNm": s.get("siteNm"),
            }
            old = prev.get(k)
            cur["last_notified"] = (old or {}).get("last_notified", 0)
            seen_now[k] = cur

            if old is None:
                if want_new and not first_run:
                    new_hits.append((ymd, cur))
                    cur["last_notified"] = now
                continue

            gain = s["free"] - old.get("free", 0)
            if (
                want_free
                and gain >= min_gain
                and old.get("free", 0) <= watch_at_or_below
                and now - cur["last_notified"] >= cooldown
            ):
                free_hits.append((ymd, cur, old.get("free", 0)))
                cur["last_notified"] = now

    state["targets"][key] = seen_now

    movnm = target.get("movNm", "")
    sitenm = target.get("siteNm", "")

    if new_hits:
        new_hits.sort(key=lambda x: (x[0], x[1]["start"]))
        by_day = {}
        for ymd, s in new_hits:
            by_day.setdefault(ymd, []).append(s)
        lines = []
        for ymd in sorted(by_day):
            times = " ".join(
                f"{hhmm(s['start'])}({s['free']}석)" for s in sorted(by_day[ymd], key=lambda x: x["start"])
            )
            lines.append(f"**{ymd_label(ymd)}**  {times}")
        notify(
            cfg,
            f"🎟 예매 오픈 - {movnm} {label.split('·')[-1].strip()}",
            f"{sitenm}\n새 회차 {len(new_hits)}개\n\n" + "\n".join(lines),
            priority=5,
            tags="clapper,rotating_light",
            click=BOOK_URL,
        )
        log(f"[{label}] >>> 새 회차 {len(new_hits)}개 알림 발송")

    if free_hits:
        free_hits.sort(key=lambda x: (x[0], x[1]["start"]))
        lines = [
            f"**{ymd_label(ymd)} {hhmm(s['start'])}**  {before}석 → **{s['free']}석**"
            for ymd, s, before in free_hits
        ]
        notify(
            cfg,
            f"🔓 취소표 - {movnm}",
            f"{sitenm}\n\n" + "\n".join(lines),
            priority=4,
            tags="ticket",
            click=BOOK_URL,
        )
        log(f"[{label}] >>> 취소표 {len(free_hits)}건 알림 발송")

    if first_run:
        log(f"[{label}] 기준선 저장 완료 - 회차 {len(seen_now)}개. 다음 실행부터 변화를 알린다.")
    elif not new_hits and not free_hits:
        log(f"[{label}] 변화 없음 (감시 중인 회차 {len(seen_now)}개)")


def main():
    cfg = load_json(CONFIG_PATH, None)
    if cfg is None:
        log(f"!! config.json 을 읽을 수 없음: {CONFIG_PATH}")
        return 2

    state = load_json(STATE_PATH, {})
    fails = state.get("consecutive_failures", 0)
    fail_at = int(cfg.get("alert_after_consecutive_failures", 5))

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                ],
            )
            ctx = browser.new_context(
                user_agent=UA,
                locale="ko-KR",
                timezone_id="Asia/Seoul",
                viewport={"width": 1440, "height": 900},
            )
            ctx.add_init_script(
                "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            )
            page = ctx.new_page()
            page.goto(HOME, wait_until="domcontentloaded", timeout=45000)

            body = page.evaluate("document.body ? document.body.innerText.slice(0,200) : ''")
            if "비정상적으로" in body or "이용이 제한" in body:
                raise RuntimeError("Cloudflare 차단 페이지에 걸림")

            for target in cfg.get("targets", []):
                run_target(page, cfg, state, target)

            ctx.close()
            browser.close()

    except (PWError, RuntimeError, KeyError) as e:
        fails += 1
        state["consecutive_failures"] = fails
        state["last_error"] = f"{type(e).__name__}: {e}"
        save_json(STATE_PATH, state)
        log(f"!! 실행 실패 ({fails}회 연속): {e}")
        if fails == fail_at:
            notify(
                cfg,
                "⚠️ CGV 감시기 고장",
                f"{fails}회 연속 실패했어. CGV가 API를 바꿨을 수 있음.\n\n`{type(e).__name__}: {e}`",
                priority=4,
                tags="warning",
            )
        return 1

    state["consecutive_failures"] = 0
    state.pop("last_error", None)
    state["runs"] = state.get("runs", 0) + 1
    state["last_ok"] = datetime.now(KST).isoformat(timespec="seconds")
    save_json(STATE_PATH, state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
