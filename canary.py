import os, sys, time, json, imaplib, email, argparse
import requests
from datetime import datetime, timezone, timedelta
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from playwright.sync_api import sync_playwright

TEST_EMAIL = os.environ["TEST_EMAIL"]
APP_PASSWORD = os.environ["APP_PASSWORD"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

LOGIN_URL = "https://www.ntd.com/"
SUBJECT_RESET = "Please reset your password"
SUBJECT_PASSWORDLESS = "Use this link to log in"
MAX_CHECKS = 5          # check the mailbox at most 5 times
CHECK_INTERVAL = 20     # seconds between checks
os.makedirs("screenshots", exist_ok=True)


# ---------- page helpers ----------
def click_first(page, candidates, timeout=4000):
    for sel in candidates:
        try:
            page.locator(sel).first.click(timeout=timeout)
            print(f"   click: {sel}")
            return True
        except Exception:
            continue
    raise RuntimeError(f"No clickable element found. Tried: {candidates}")


def fill_first(page, candidates, value, timeout=4000):
    for sel in candidates:
        try:
            page.locator(sel).first.fill(value, timeout=timeout)
            print(f"   fill: {sel}")
            return True
        except Exception:
            continue
    raise RuntimeError(f"No fillable element found. Tried: {candidates}")


def open_login(page):
    page.goto(LOGIN_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    if page.locator('input[placeholder*="Enter your email" i]').count() == 0:
        click_first(page, ['text=/sign in/i', 'text=/log in/i', 'text=/login/i'])
        page.wait_for_timeout(2000)
    fill_first(page, ['input[placeholder*="Enter your email" i]', 'input[type="email"]'],
               TEST_EMAIL)
    click_first(page, ['button:has-text("Continue")'])
    page.wait_for_selector('text=/forgot my password/i', timeout=15000)


# def trigger_reset(page):
#     open_login(page)
#     click_first(page, ['text=/forgot my password/i'])
#     page.wait_for_selector('text=/email has been sent/i', timeout=15000)


def trigger_passwordless(page):
    open_login(page)
    click_first(page, ['button:has-text("Log in without password")',
                       'text=/log in without password/i'])
    page.wait_for_selector('text=/email has been sent/i', timeout=15000)


# ---------- mailbox ----------
def decode_subject(msg):
    try:
        return str(make_header(decode_header(msg.get("Subject", ""))))
    except Exception:
        return msg.get("Subject", "")


def fetch_recent(since, folders=("INBOX", "[Gmail]/Spam")):
    results = []
    M = imaplib.IMAP4_SSL("imap.gmail.com")
    M.login(TEST_EMAIL, APP_PASSWORD)
    for folder in folders:
        status, _ = M.select(f'"{folder}"', readonly=True)
        if status != "OK":
            continue
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        _, ids = M.search(None, "SINCE", day)
        for i in ids[0].split():
            _, data = M.fetch(i, "(BODY.PEEK[HEADER])")
            msg = email.message_from_bytes(data[0][1])
            try:
                sent = parsedate_to_datetime(msg["Date"])
            except Exception:
                continue
            if sent >= since:
                results.append((folder, decode_subject(msg), msg.get("To", ""), sent))
    M.logout()
    return results


def wait_for_mail(subject_kw, since):
    for attempt in range(1, MAX_CHECKS + 1):
        for folder, subj, to, sent in fetch_recent(since):
            if subject_kw.lower() in subj.lower():
                return folder, subj, int((sent - since).total_seconds())
        print(f"   ...Not received yet (check {attempt}/{MAX_CHECKS}).")
        if attempt < MAX_CHECKS:
            time.sleep(CHECK_INTERVAL)
    return None


# ---------- alert + status ----------
def send_alert(text):
    print("\n" + "=" * 50 + "\nAlert:\n" + text + "\n" + "=" * 50)
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID,
                  "text": f"🔴 NTD email canary FAILED\n\n{text}"},
            timeout=10)
        print("   Telegram alert sent" if r.ok else f"   Telegram error: {r.status_code}")
    except Exception as e:
        print(f"   Telegram failed: {type(e).__name__}")


def save_status(results):
    os.makedirs("docs", exist_ok=True)
    path = "docs/status.json"
    data = {"history": []}
    if os.path.exists(path):
        data = json.load(open(path))
    now = datetime.now(timezone.utc).isoformat()
    data["last_run"] = now
    for r in results:
        data["history"].append({**r, "time": now})
    data["history"] = data["history"][-300:]
    json.dump(data, open(path, "w"), indent=2)


# ---------- main flow ----------
def run_check(page, name, trigger, subject_kw):
    print(f"\n▶ Test: {name}")
    since = datetime.now(timezone.utc) - timedelta(seconds=10)
    try:
        trigger(page)
        page.wait_for_timeout(2000)
    except Exception as e:
        try:
            page.screenshot(path=f"screenshots/{name}_FAIL.png")
        except Exception:
            pass
        return f"{name}: Webpage trigger failed - {e}"

    try:
        result = wait_for_mail(subject_kw, since)
    except Exception as e:
        return f"{name}: Could not read the mailbox - {type(e).__name__}: {e}"

    if result is None:
        return (f"{name}: Webpage said the email was sent, but nothing arrived after "
                f"{MAX_CHECKS} checks ({MAX_CHECKS * CHECK_INTERVAL}s).")
    folder, subj, delay = result
    print(f"   ✅ Received: '{subj}' folder={folder} delay={delay}s")
    if folder != "INBOX":
        return f"{name}: Email went to spam ({subj})"
    if delay > 120:
        return f"{name}: Email delayed {delay}s ({subj})"
    return None


def discover():
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    mails = fetch_recent(since)
    print(f"Last hour: {len(mails)} emails")
    for folder, subj, to, sent in sorted(mails, key=lambda x: x[3]):
        print(f" [{folder}] {sent:%H:%M:%S} | {subj}")
        
# def send_telegram(text):
#     try:
#         r = requests.post(
#             f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
#             data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
#             timeout=10)
#         print("   Telegram sent" if r.ok else f"   Telegram error: {r.status_code}")
#     except Exception as e:
#         print(f"   Telegram failed: {type(e).__name__}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", default="passwordless", choices=["passwordless"])
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--discover", action="store_true")
    args = ap.parse_args()

    if args.discover:
        return discover()

    checks = {
        "passwordless": ("Passwordless", trigger_passwordless, SUBJECT_PASSWORDLESS),
    }
    selected = list(checks)

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed,
                                    slow_mo=300 if args.headed else 0)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for key in selected:
            name, trig, kw = checks[key]
            err = run_check(page, name, trig, kw)
            results.append({"name": name, "ok": err is None, "detail": err or ""})
        browser.close()

    save_status(results)
    problems = [r["detail"] for r in results if not r["ok"]]
    if problems:
        send_alert("\n".join(problems))   # already reported via Telegram + status page
        return
    print("\n✅ All tests passed")


if __name__ == "__main__":
    main()