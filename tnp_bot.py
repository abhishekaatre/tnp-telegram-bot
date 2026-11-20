#!/usr/bin/env python3
"""
TnP -> Telegram notifier.

Checks your Training & Placement (TnP) website, finds notices/jobs that weren't
there last time, and sends each new one to you on Telegram.

Usage:
  python tnp_bot.py --chat-id      # helper: prints your Telegram chat ID
  python tnp_bot.py --once         # check once and exit (used by GitHub Actions / cron)
  python tnp_bot.py                # keep running, checking every CHECK_INTERVAL_MIN minutes
  python tnp_bot.py --test         # send a test message + show what items the bot can see

All settings come from environment variables or a .env file (see .env.example).
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "seen.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_MSGS_PER_RUN = 10


# ---------------------------------------------------------------- config ----
def load_dotenv(path: Path) -> None:
    """Tiny .env loader so you don't need python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        os.environ.setdefault(key, val)


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def env_bool(name: str, default: bool) -> bool:
    v = env(name)
    return default if not v else v.lower() in ("1", "true", "yes", "on")


# -------------------------------------------------------------- telegram ----
def tg_api(method: str, **params):
    token = env("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set. See README step 1.")
    r = requests.post(f"https://api.telegram.org/bot{token}/{method}",
                      data=params, timeout=30)
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram error: {data}")
    return data["result"]


def send(text: str, to_channel: bool = True) -> None:
    chat_id = env("TELEGRAM_CHAT_ID")
    if not chat_id:
        sys.exit("TELEGRAM_CHAT_ID is not set. Run: python tnp_bot.py --chat-id")
    tg_api("sendMessage", chat_id=chat_id, text=text[:4000],
           disable_web_page_preview="true")
    if to_channel:
        send_channel(text)


def send_channel(text: str) -> None:
    """Post to the friends' group/channel. Problems here never stop your own alerts."""
    channel = env("TELEGRAM_CHANNEL_ID")
    if not channel:
        return
    try:
        tg_api("sendMessage", chat_id=channel, text=text[:4000],
               disable_web_page_preview="true")
    except Exception as e:
        m = re.search(r"'migrate_to_chat_id': (-?\d+)", str(e))
        try:
            if not m:
                raise
            # the group was upgraded by Telegram and got a new ID: follow it
            tg_api("sendMessage", chat_id=m.group(1), text=text[:4000],
                   disable_web_page_preview="true")
        except Exception as e2:
            print("channel send failed:", e2)


def print_chat_id() -> None:
    updates = tg_api("getUpdates")
    chats = {}
    for u in updates:
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("username") or chat.get("title") or chat.get("first_name")
    if not chats:
        print("No messages found. Open your bot in Telegram, press START / send it 'hi', "
              "then run this again.")
        return
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}    ({name})")


# ----------------------------------------------------------------- fetch ----
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = UA
    s.verify = env_bool("VERIFY_SSL", True)
    if not s.verify:
        requests.packages.urllib3.disable_warnings()  # college sites often have bad certs
    return s


def login(s: requests.Session) -> None:
    """Log in by filling the site's own login form (keeps hidden/CSRF fields)."""
    login_url = env("LOGIN_URL")
    user, pwd = env("TNP_USERNAME"), env("TNP_PASSWORD")
    if not login_url:
        return
    if not (user and pwd):
        raise RuntimeError("LOGIN_URL is set but TNP_USERNAME / TNP_PASSWORD are missing.")

    r = s.get(login_url, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")

    pw_input = soup.find("input", {"type": "password"})
    form = pw_input.find_parent("form") if pw_input else soup.find("form")
    if not form:
        raise RuntimeError("Couldn't find a login form on LOGIN_URL.")

    payload = {}
    for inp in form.find_all(["input", "select", "textarea"]):
        name = inp.get("name")
        if not name:
            continue
        itype = (inp.get("type") or "text").lower()
        if itype in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        if itype in ("submit", "button", "image"):
            continue
        payload[name] = inp.get("value", "")

    user_field = env("USERNAME_FIELD")
    pass_field = env("PASSWORD_FIELD")
    if not pass_field:
        pass_field = pw_input.get("name") if pw_input else "password"
    if not user_field:
        for inp in form.find_all("input"):
            t = (inp.get("type") or "text").lower()
            if t in ("text", "email", "tel", "number") and inp.get("name"):
                user_field = inp["name"]
                break
    if not user_field:
        raise RuntimeError("Couldn't detect the username field; set USERNAME_FIELD in .env.")

    payload[user_field] = user
    payload[pass_field] = pwd
    # include the submit button's name/value if it has one (ASP.NET sites need this)
    btn = form.find(["input", "button"], {"type": "submit"})
    if btn and btn.get("name"):
        payload[btn["name"]] = btn.get("value", "")

    action = urljoin(r.url, form.get("action") or r.url)
    method = (form.get("method") or "post").lower()
    resp = (s.post if method == "post" else s.get)(
        action, data=payload if method == "post" else None,
        params=payload if method == "get" else None, timeout=30)
    resp.raise_for_status()

    after = BeautifulSoup(resp.text, "html.parser")
    if after.find("input", {"type": "password"}) and "logout" not in resp.text.lower():
        raise RuntimeError("Login seems to have failed (still seeing a password box). "
                           "Check username/password, or the site may use a captcha.")


def fetch_pages(s: requests.Session) -> list[tuple[str, str]]:
    urls = [u.strip() for u in env("TNP_URL").split(",") if u.strip()]
    if not urls:
        sys.exit("TNP_URL is not set. Put the notices/jobs page URL in .env")
    pages = []
    for u in urls:
        r = s.get(u, timeout=45)
        r.raise_for_status()
        pages.append((r.url, r.text))
    return pages


# --------------------------------------------------------------- extract ----
def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def extract_bitmesra(page_url: str, soup: BeautifulSoup) -> list[dict] | None:
    """Special parser for the BIT Mesra placement portal (tp.bitmesra.co.in)."""
    items = []
    # Dashboard: table of jobs -> Company | Dead Line | Posted On | Updates / View & Apply
    for tr in soup.select("table#job-listings tbody tr"):
        tds = tr.find_all("td")
        if len(tds) < 3:
            continue
        company, deadline, posted = (clean(td.get_text(" ")) for td in tds[:3])
        apply = tr.find("a", href=re.compile(r"job/info/"))
        link = urljoin(page_url, apply["href"]) if apply else page_url
        key_src = f"{company}|{posted}".lower()
        items.append({
            "key": "job:" + hashlib.sha1(key_src.encode()).hexdigest()[:16],
            "text": company,
            "link": link,
            "msg": (f"🆕 NEW JOB on TnP portal\n\n🏢 {company}\n"
                    f"⏰ Deadline: {deadline}\n📅 Posted: {posted}\n\n🔗 Apply: {link}"),
        })
    # Notifications page: Info (title + text) | Post Date
    for tr in soup.select("table#newsevents tbody tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        a = tds[0].find("a")
        title = clean(a.get_text(" ")) if a else ""
        p = tds[0].find("p")
        body = clean(p.get_text(" ")) if p else clean(tds[0].get_text(" "))
        when = clean(tds[1].get_text(" ")) if len(tds) > 1 else ""
        link = urljoin(page_url, a["href"]) if a and a.get("href") else page_url
        full = f"{title} {body} {when}"
        items.append({
            "key": "news:" + hashlib.sha1(full.lower().encode()).hexdigest()[:16],
            "text": f"{title} — {body}",
            "link": link,
            "msg": (f"📢 TnP UPDATE\n\n{title}\n\n{body[:900]}\n\n🕒 {when}\n🔗 {link}"),
        })
    return items or None


def extract_items(page_url: str, html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        junk.decompose()

    if not env("ITEM_SELECTOR"):
        special = extract_bitmesra(page_url, soup)
        if special:
            return special

    selector = env("ITEM_SELECTOR")
    if selector:
        nodes = soup.select(selector)
    else:
        # auto mode: table rows first, then list items with links, then links
        nodes = [tr for tr in soup.find_all("tr") if tr.find("td")]
        if len(nodes) < 3:
            nodes = [li for li in soup.find_all("li") if li.find("a")]
        if len(nodes) < 3:
            nodes = soup.find_all("a")

    min_len = int(env("MIN_TEXT_LEN", "8"))
    items, seen_keys = [], set()
    for n in nodes:
        text = clean(n.get_text(" "))
        if len(text) < min_len:
            continue
        a = n if n.name == "a" else n.find("a", href=True)
        link = ""
        if a is not None and a.get("href") and not a["href"].startswith(("javascript:", "#")):
            link = urljoin(page_url, a["href"])
        key = hashlib.sha1(text.lower().encode()).hexdigest()[:16]
        if key in seen_keys:
            continue
        seen_keys.add(key)
        items.append({"key": key, "text": text, "link": link})

    if not items:
        # nothing structured found: watch the whole page text instead
        body = clean(soup.get_text(" "))
        items.append({"key": "page:" + hashlib.sha1(body.encode()).hexdigest()[:16],
                      "text": "The TnP page content changed.", "link": page_url})
    return items


# ----------------------------------------------------------------- state ----
def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {"seen": [], "last_error": ""}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ main ----
def check_once(verbose: bool = False) -> None:
    state = load_state()
    first_run = not state["seen"]
    try:
        s = make_session()
        login(s)
        items = []
        for url, html in fetch_pages(s):
            items.extend(extract_items(url, html))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        print(f"[{datetime.now():%H:%M}] ERROR {msg}")
        if msg != state.get("last_error"):  # alert once per new kind of error
            try:
                send(f"⚠️ TnP bot problem:\n{msg[:500]}", to_channel=False)
            except Exception:
                pass
            state["last_error"] = msg
            save_state(state)
        raise SystemExit(1)

    state["last_error"] = ""
    seen = set(state["seen"])
    new = [it for it in items if it["key"] not in seen]

    if verbose:
        print(f"Found {len(items)} items on the page(s).")  # no job names: logs are public

    if first_run:
        send(f"✅ TnP bot is live. Tracking {len(items)} existing items; "
             f"I'll message you when something new appears.")
    else:
        for it in new[:MAX_MSGS_PER_RUN]:
            body = it.get("msg")
            if not body:
                body = f"🔔 New on TnP\n\n{it['text'][:700]}"
                if it["link"]:
                    body += f"\n\n🔗 {it['link']}"
            send(body)
            time.sleep(1)
        if len(new) > MAX_MSGS_PER_RUN:
            send(f"…and {len(new) - MAX_MSGS_PER_RUN} more new items. Check the portal.")

    # keep every key still on the page (so old items never re-alert), plus recent others
    current = [it["key"] for it in items]
    cur_set = set(current)
    older = [k for k in state["seen"] if k not in cur_set][-2000:]
    state["seen"] = older + current
    save_state(state)
    print(f"[{datetime.now():%Y-%m-%d %H:%M}] checked: {len(items)} items, {len(new)} new"
          + (" (first run, baseline saved)" if first_run else ""))


def main() -> None:
    load_dotenv(HERE / ".env")
    p = argparse.ArgumentParser(description="TnP -> Telegram notifier")
    p.add_argument("--once", action="store_true", help="check once and exit")
    p.add_argument("--chat-id", action="store_true", help="print your Telegram chat ID")
    p.add_argument("--test", action="store_true", help="send a test message and list items")
    p.add_argument("--hello", action="store_true", help="post a hello message to the group/channel")
    a = p.parse_args()

    if a.chat_id:
        print_chat_id()
    elif a.hello:
        send_channel("👋 Hello! This group will now get new TnP portal jobs and updates automatically.")
    elif a.test:
        send("👋 Test message from your TnP bot. It works!")
        s = make_session()
        login(s)
        for url, html in fetch_pages(s):
            items = extract_items(url, html)
            print(f"\n{url}: {len(items)} items")
            for it in items[:20]:
                print("  -", it["text"][:110], ("→ " + it["link"]) if it["link"] else "")
    elif a.once:
        check_once(verbose=True)
    else:
        interval = float(env("CHECK_INTERVAL_MIN", "15"))
        print(f"Running. Checking every {interval:g} min. Press Ctrl+C to stop.")
        while True:
            try:
                check_once()
            except SystemExit:
                pass
            time.sleep(interval * 60)


if __name__ == "__main__":
    main()
