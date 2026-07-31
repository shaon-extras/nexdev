import os
import re
import json
import time
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
import pytz

PANEL_URL = "https://customer.nesco.gov.bd/pre/panel"
DB_FILE = "meter_history.json"
CONFIG_FILE = "meter_config.json"
PROXY_FILE = "proxy.txt"
RUN_LOG_FILE = "run_log.json"

BD_TZ = pytz.timezone('Asia/Dhaka')
session = requests.Session()

# ---- PROXY SETUP ----
proxy_url = None
if os.path.exists(PROXY_FILE):
    with open(PROXY_FILE, "r") as f:
        proxy_url = f.read().strip()
if not proxy_url:
    proxy_url = os.getenv("PROXY_URL")

if proxy_url:
    session.proxies = {"http": proxy_url, "https": proxy_url}
    print(f"🔒 Using proxy: {proxy_url}")
else:
    print("🔓 No proxy configured — using direct connection")

# ---- HELPER FUNCTIONS ----
def get_meter_numbers():
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            return list(config.keys())
    except FileNotFoundError:
        try:
            with open("meters.txt", "r") as f:
                return [line.strip() for line in f if line.strip()]
        except FileNotFoundError:
            return ["37005309", "37006814", "37001280", "37009693", "37005104", "37002391"]

def fetch_nesco_data(cust_no, retries=3):
    headers = {"User-Agent": "Mozilla/5.0"}
    for attempt in range(retries):
        try:
            r1 = session.get(PANEL_URL, headers=headers, timeout=45)
            soup_page = BeautifulSoup(r1.text, "html.parser")
            token_tag = soup_page.find("input", {"name": "_token"})
            if not token_tag:
                return None
            data = {
                "_token": token_tag["value"],
                "cust_no": cust_no.strip(),
                "submit": "রিচার্জ হিস্ট্রি"
            }
            r2 = session.post(PANEL_URL, headers=headers, data=data, timeout=60)
            soup = BeautifulSoup(r2.text, "html.parser")
            balance_anchor = soup.find(string=re.compile("অবশিষ্ট ব্যালেন্স"))
            if not balance_anchor:
                return None
            label = balance_anchor.find_parent("label")
            balance_value = float(label.find_next_sibling("div").find("input")["value"])
            date_str = label.find("span").text.strip()
            dt = datetime.strptime(date_str, "%d %B %Y %I:%M:%S %p")
            formatted_date = dt.strftime("%Y-%m-%d")
            return {"balance": balance_value, "date": formatted_date}
        except Exception as e:
            print(f"   ⚠️ Attempt {attempt+1}/{retries} failed: {e}")
            if attempt < retries - 1:
                wait = (attempt + 1) * 2
                print(f"   🔄 Retrying in {wait}s...")
                time.sleep(wait)
            else:
                print(f"   ❌ All retries exhausted for {cust_no}")
                return None
    return None

def main():
    # ---- Load existing database ----
    if os.path.exists(DB_FILE):
        with open(DB_FILE, "r") as f:
            full_db = json.load(f)
    else:
        full_db = {"meter_data": {}, "last_run": {}}

    meter_data = full_db.get("meter_data", {})
    last_run = full_db.get("last_run", {})

    # ---- MIGRATE OLD FORMAT TO NEW ----
    for cust_no, value in list(meter_data.items()):
        if isinstance(value, list):
            history = value
            monthly_total = sum(entry.get("usage", 0) for entry in history)
            last_balance = history[-1]["balance"] if history else 0.0
            meter_data[cust_no] = {
                "history": history,
                "monthly_total": monthly_total,
                "last_balance": last_balance,
                "monthly_totals": []   # <-- new field
            }
            print(f"🔄 Migrated meter {cust_no} to new format")
        elif isinstance(value, dict):
            if "history" not in value:
                value["history"] = []
            if "monthly_total" not in value:
                value["monthly_total"] = 0.0
            if "last_balance" not in value:
                value["last_balance"] = 0.0
            if "monthly_totals" not in value:
                value["monthly_totals"] = []   # <-- ensure this exists

    now_bd = datetime.now(BD_TZ)
    now_bd_str = now_bd.strftime("%Y-%m-%d %H:%M:%S")
    today_bd = now_bd.date()

    run_log = {"timestamp": now_bd_str, "meters": {}}

    meters = get_meter_numbers()
    print(f"⏰ Runner Time (BD): {now_bd_str}")

    # ---- Warm‑up ----
    if proxy_url:
        try:
            session.head(PANEL_URL, timeout=30)
            print("🌐 Proxy connection warmed up.")
        except:
            pass

    # ---- Process each meter ----
    for cust_no in meters:
        print(f"\n🔍 Checking meter: {cust_no}")

        if cust_no not in meter_data:
            meter_data[cust_no] = {
                "history": [],
                "monthly_total": 0.0,
                "last_balance": 0.0,
                "monthly_totals": []
            }

        meter = meter_data[cust_no]
        history = meter["history"]
        monthly_total = meter.get("monthly_total", 0.0)

        # ---- Reset monthly_total on the 1st and store previous month ----
        if today_bd.day == 1:
            # Store previous month's total before reset
            if monthly_total > 0:
                # Compute previous month label (e.g., "2026-07")
                first_day_current = today_bd.replace(day=1)
                prev_month_date = first_day_current - timedelta(days=1)
                prev_month_label = prev_month_date.strftime("%Y-%m")
                if "monthly_totals" not in meter:
                    meter["monthly_totals"] = []
                meter["monthly_totals"].append({
                    "month": prev_month_label,
                    "usage": monthly_total
                })
                print(f"   📊 Stored previous month ({prev_month_label}) usage: {monthly_total}")
            monthly_total = 0.0
            print(f"   📅 New month – reset monthly_total to 0")

        current_data = fetch_nesco_data(cust_no)

        if not current_data:
            last_run[cust_no] = now_bd_str
            run_log["meters"][cust_no] = {
                "success": False,
                "error": "Scraping failed (timeout or no data)",
                "balance_fetched": None
            }
            meter["monthly_total"] = monthly_total
            continue

        # ---- Success ----
        web_balance = current_data["balance"]
        web_date = current_data["date"]
        print(f"   📅 Scraped Date: {web_date}, Balance: {web_balance}")

        # ---- Calculate usage (based on previous entry, not today) ----
        prev_entry = None
        for entry in reversed(history):
            if entry["balance_date"] != web_date:
                prev_entry = entry
                break

        if prev_entry:
            prev_balance = prev_entry["balance"]
            if web_balance <= prev_balance:
                usage = round(prev_balance - web_balance, 2)
            else:
                usage = 0.0
        else:
            usage = 0.0

        monthly_total += usage

        # ---- Check if today's entry already exists ----
        existing_idx = None
        for i, entry in enumerate(history):
            if entry["balance_date"] == web_date:
                existing_idx = i
                break

        new_entry = {
            "balance_date": web_date,
            "balance": web_balance,
            "usage": usage,
            "recorded_at": now_bd_str
        }

        if existing_idx is not None:
            # Update existing entry
            history[existing_idx] = new_entry
            print(f"   🔄 Updated existing entry for {web_date}")
        else:
            # Append new entry
            history.append(new_entry)
            print(f"   ➕ Added new entry for {web_date}")

        # Trim to last 7 entries
        if len(history) > 7:
            history = history[-7:]

        meter["history"] = history
        meter["monthly_total"] = monthly_total
        meter["last_balance"] = web_balance

        last_run[cust_no] = now_bd_str

        run_log["meters"][cust_no] = {
            "success": True,
            "error": None,
            "balance_fetched": web_balance,
            "balance_changed": (usage != 0.0)
        }

        print(f"   ✅ Usage: {usage} | Monthly total: {monthly_total}")

    # ---- Save ----
    full_db["meter_data"] = meter_data
    full_db["last_run"] = last_run
    with open(DB_FILE, "w") as f:
        json.dump(full_db, f, indent=4)

    with open(RUN_LOG_FILE, "w") as f:
        json.dump(run_log, f, indent=4)

    print("\n✅ Database updated successfully!")
    print(f"📝 Run log written to {RUN_LOG_FILE}")

if __name__ == "__main__":
    main()
