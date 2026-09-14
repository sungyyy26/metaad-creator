"""Local dashboard for duplicating Meta ads — a desktop stand-in for the
'meta-ad-duplicator' artifact. Runs entirely on your machine at
http://127.0.0.1:5000 and calls the Meta Marketing API directly (no Claude
session in the loop).

Run:
    export META_ACCESS_TOKEN=...   (Windows CMD: set META_ACCESS_TOKEN=...)
    pip install -r webapp/requirements.txt
    python webapp/app.py
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime

from flask import Flask, render_template, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import meta_lib  # noqa: E402

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), "requests.json")
LOCK = threading.Lock()

ACCOUNTS = {
    "1298298124998350": "EQQUALBERRY_AMAZON_US (USD)",
    "2406177369753142": "EQQUALBERRY_GLOBAL (USD)",
}


def load_requests():
    if not os.path.exists(DB_PATH):
        return []
    with open(DB_PATH, encoding="utf-8") as f:
        return json.load(f)


def save_requests(items):
    with open(DB_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def upsert(entry):
    with LOCK:
        items = load_requests()
        items = [it for it in items if it["id"] != entry["id"]]
        items.append(entry)
        save_requests(items)


@app.route("/")
def index():
    items = list(reversed(load_requests()))
    return render_template("index.html", accounts=ACCOUNTS, items=items, message=None)


@app.route("/submit", methods=["POST"])
def submit():
    form = request.form
    entry = {
        "id": str(uuid.uuid4()),
        "submittedAt": datetime.now().strftime("%m/%d %H:%M"),
        "campaign": form.get("campaign_id", ""),
        "sourceAdset": form.get("source_adset_name", ""),
        "adSetName": form.get("new_adset_name", ""),
        "adName": form.get("new_ad_name", ""),
        "budget": form.get("daily_budget", ""),
        "status": "processing",
    }
    upsert(entry)

    message = None
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        entry["status"] = "error"
        entry["error"] = "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다. 터미널에서 export/set 하고 서버를 다시 시작하세요."
    else:
        try:
            result = meta_lib.duplicate_ad(
                token,
                account_id=form["account_id"],
                campaign_id=form["campaign_id"],
                source_adset_name=form["source_adset_name"],
                new_adset_name=form["new_adset_name"],
                new_ad_name=form["new_ad_name"],
                daily_budget=int(form["daily_budget"]),
                website_url=form["website_url"],
                creative_name=form["creative_name"],
                headline=form["headline"],
                primary_text=form["primary_text"],
                status=form.get("after_status", "PAUSED"),
            )
            entry["status"] = "done"
            entry["result"] = result
            message = ("ok", f"생성 완료: 광고 세트 {result['ad_set_id']} / 광고 {result['ad_id']}")
        except meta_lib.MetaApiError as e:
            entry["status"] = "error"
            entry["error"] = str(e)
            message = ("err", str(e))
        except Exception as e:  # noqa: BLE001
            entry["status"] = "error"
            entry["error"] = f"예상치 못한 오류: {e}"
            message = ("err", entry["error"])

    upsert(entry)
    items = list(reversed(load_requests()))
    return render_template("index.html", accounts=ACCOUNTS, items=items, message=message)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
