"""Local dashboard for duplicating Meta ads — a desktop stand-in for the
'meta-ad-duplicator' artifact. Runs entirely on your machine at
http://127.0.0.1:5000 and calls the Meta Marketing API (and optionally the
Shopify Admin API for bridge pages) directly — no Claude session in the loop.

Run (one-time setup):
    cp .env.example .env   # then fill in your tokens in .env — it's git-ignored
    pip install -r webapp/requirements.txt

Run (every time after that — no need to re-export anything):
    python webapp/app.py
"""
import json
import os
import sys
import threading
import uuid
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, redirect, render_template, request, session

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import meta_lib  # noqa: E402
import shopify_lib  # noqa: E402

load_dotenv()  # reads .env in the repo root (or nearest parent) if present;
                # values already set in the shell (export/set) still win.

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", os.urandom(24))
DB_PATH = os.path.join(os.path.dirname(__file__), "requests.json")
LOCK = threading.Lock()

ACCOUNTS = {
    "1298298124998350": "EQQUALBERRY_AMAZON_US (USD)",
    "2406177369753142": "EQQUALBERRY_GLOBAL (USD)",
}

BULK_COLUMNS = [
    "캠페인 (이름 또는 ID)", "복제할 광고 세트", "새 광고 세트 이름", "새 광고 이름",
    "일일 예산", "웹사이트 URL", "소재", "헤드라인", "기본 텍스트",
    "광고 계정 ID (선택)", "생성 후 상태 (선택, active/paused)",
    "Shopify 복제할 상품 핸들 (선택)", "Shopify 제목 재지정 (선택)",
    "Shopify 태그 재지정 (선택, 쉼표 구분)", "Shopify 템플릿 재지정 (선택)",
    "Shopify 아마존 링크 재지정 (선택)",
]
BULK_REQUIRED = 9  # first 9 columns are mandatory
# Tokens people commonly type to mean "leave this blank" (a spreadsheet habit) —
# treated as empty rather than as a literal value (e.g. a Shopify handle "-").
BLANK_TOKENS = {"-", "--", "—", "n/a", "na", "없음", "none"}


def is_blank(value):
    return not value or value.strip().lower() in BLANK_TOKENS


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


def remove_request(req_id):
    with LOCK:
        items = [it for it in load_requests() if it["id"] != req_id]
        save_requests(items)


def new_entry(**fields):
    entry = {
        "id": str(uuid.uuid4()),
        "submittedAt": datetime.now().strftime("%m/%d %H:%M"),
        "status": "processing",
    }
    entry.update(fields)
    return entry


def run_duplicate(token, entry, *, account_id, campaign_id, source_adset_name,
                   new_adset_name, new_ad_name, daily_budget, website_url,
                   creative_name, headline, primary_text, after_status, cache=None):
    try:
        result = meta_lib.duplicate_ad(
            token,
            account_id=account_id,
            campaign_id=campaign_id,
            source_adset_name=source_adset_name,
            new_adset_name=new_adset_name,
            new_ad_name=new_ad_name,
            daily_budget=int(daily_budget),
            website_url=website_url,
            creative_name=creative_name,
            headline=headline,
            primary_text=primary_text,
            status=after_status,
            cache=cache,
        )
        entry["status"] = "done"
        entry["result"] = result
        return True, None
    except meta_lib.MetaApiError as e:
        entry["status"] = "error"
        entry["error"] = str(e)
        return False, str(e)
    except Exception as e:  # noqa: BLE001
        entry["status"] = "error"
        entry["error"] = f"예상치 못한 오류: {e}"
        return False, entry["error"]


def run_shopify_bridge(entry, *, new_ad_name, source_handle, title_override=None,
                        tags_override=None, template_override=None, amazon_link_override=None):
    """Only called after a successful Meta duplicate_ad. Downgrades a 'done'
    entry to 'incomplete' (never touches the already-created Meta ad) if the
    Shopify side fails, matching the original artifact's rule that Meta
    success + Shopify failure is never reported as fully 'done'."""
    shop = os.environ.get("SHOPIFY_SHOP")
    shopify_token = os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not shop or not shopify_token:
        entry["status"] = "incomplete"
        entry["shopifyError"] = "SHOPIFY_SHOP / SHOPIFY_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다."
        return
    try:
        bridge = shopify_lib.create_bridge_page(
            shop, shopify_token,
            source_handle=source_handle,
            new_handle=new_ad_name,
            title_override=title_override or None,
            tags_override=tags_override or None,
            template_override=template_override or None,
            amazon_link_override=amazon_link_override or None,
            storefront_domain=os.environ.get("SHOPIFY_STOREFRONT_DOMAIN"),
        )
        entry["resultShopifyUrl"] = bridge["url"]
    except shopify_lib.ShopifyApiError as e:
        entry["status"] = "incomplete"
        entry["shopifyError"] = str(e)
    except Exception as e:  # noqa: BLE001
        entry["status"] = "incomplete"
        entry["shopifyError"] = f"예상치 못한 오류: {e}"


@app.route("/")
def index():
    items = list(reversed(load_requests()))
    message = session.pop("flash_message", None)
    return render_template("index.html", accounts=ACCOUNTS, items=items,
                            message=message, bulk_columns=BULK_COLUMNS)


@app.route("/submit", methods=["POST"])
def submit():
    form = request.form
    entry = new_entry(
        campaign=form.get("campaign_id", ""),
        sourceAdset=form.get("source_adset_name", ""),
        adSetName=form.get("new_adset_name", ""),
        adName=form.get("new_ad_name", ""),
        budget=form.get("daily_budget", ""),
    )
    upsert(entry)

    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        entry["status"] = "error"
        entry["error"] = "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다. 터미널에서 export/set 하고 서버를 다시 시작하세요."
        upsert(entry)
        message = {"kind": "err", "text": entry["error"]}
    else:
        ok, err = run_duplicate(
            token, entry,
            account_id=form["account_id"], campaign_id=form["campaign_id"],
            source_adset_name=form["source_adset_name"], new_adset_name=form["new_adset_name"],
            new_ad_name=form["new_ad_name"], daily_budget=form["daily_budget"],
            website_url=form["website_url"], creative_name=form["creative_name"],
            headline=form["headline"], primary_text=form["primary_text"],
            after_status=form.get("after_status", "PAUSED"),
        )
        shopify_source_handle = form.get("shopify_source_handle", "").strip()
        if ok and shopify_source_handle:
            run_shopify_bridge(
                entry, new_ad_name=form["new_ad_name"], source_handle=shopify_source_handle,
                title_override=form.get("shopify_title_override", "").strip(),
                tags_override=form.get("shopify_tags_override", "").strip(),
                template_override=form.get("shopify_template_override", "").strip(),
                amazon_link_override=form.get("shopify_amazon_override", "").strip(),
            )
        upsert(entry)

        if not ok:
            message = {"kind": "err", "text": err}
        elif entry["status"] == "incomplete":
            message = {"kind": "err", "text": f"Meta 광고는 생성됐지만 Shopify 브릿지 페이지 실패: {entry['shopifyError']}"}
        else:
            message = {"kind": "ok", "text": f"생성 완료: 광고 세트 {entry['result']['ad_set_id']} / 광고 {entry['result']['ad_id']}"}

    session["flash_message"] = message
    return redirect("/")


@app.route("/submit_bulk", methods=["POST"])
def submit_bulk():
    text = request.form.get("bulk_text", "")
    rows = [(i, line.split("\t")) for i, line in enumerate(text.splitlines(), start=1) if line.strip()]

    token = os.environ.get("META_ACCESS_TOKEN")
    if not rows:
        message = {"kind": "err", "text": "붙여넣은 내용이 없습니다."}
    elif not token:
        message = {"kind": "err", "text": "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다. 터미널에서 export/set 하고 서버를 다시 시작하세요."}
    else:
        success, incomplete, fail, details = 0, 0, 0, []
        default_account = next(iter(ACCOUNTS))
        # Shared across every row in this batch: repeated campaign/ad-set/
        # creative-library lookups hit the Meta API once instead of once per
        # row, which is the main thing that trips their ad-account rate limit.
        meta_cache = {}
        for lineno, cols in rows:
            cols = [c.strip() for c in cols]
            if len(cols) < BULK_REQUIRED:
                fail += 1
                details.append(f"{lineno}행: 열이 {BULK_REQUIRED}개 필요한데 {len(cols)}개만 입력됨 — 건너뜀")
                continue
            if len(cols) > len(BULK_COLUMNS):
                extra = cols[len(BULK_COLUMNS):]
                details.append(
                    f"{lineno}행: 열이 {len(BULK_COLUMNS)}개보다 많이 입력되어 뒤쪽 값이 무시됨 "
                    f"({', '.join(repr(v) for v in extra if v)}) — 탭이 하나 더 들어갔을 수 있어요."
                )
            (campaign_id, source_adset_name, new_adset_name, new_ad_name, daily_budget,
             website_url, creative_name, headline, primary_text) = cols[:9]
            account_id = cols[9] if len(cols) > 9 and not is_blank(cols[9]) else default_account
            after_raw = cols[10].strip().lower() if len(cols) > 10 and not is_blank(cols[10]) else "paused"
            after_status = "ACTIVE" if after_raw in ("active", "활성화") else "PAUSED"
            shopify_source_handle = cols[11] if len(cols) > 11 and not is_blank(cols[11]) else ""
            shopify_title = cols[12] if len(cols) > 12 and not is_blank(cols[12]) else ""
            shopify_tags = cols[13] if len(cols) > 13 and not is_blank(cols[13]) else ""
            shopify_template = cols[14] if len(cols) > 14 and not is_blank(cols[14]) else ""
            shopify_amazon = cols[15] if len(cols) > 15 and not is_blank(cols[15]) else ""

            entry = new_entry(campaign=campaign_id, sourceAdset=source_adset_name,
                               adSetName=new_adset_name, adName=new_ad_name, budget=daily_budget)
            upsert(entry)
            if not daily_budget.isdigit():
                entry["status"] = "error"
                entry["error"] = f"예산은 숫자여야 합니다: '{daily_budget}'"
                upsert(entry)
                fail += 1
                details.append(f"{lineno}행 ({new_ad_name}): {entry['error']}")
                continue
            ok, err = run_duplicate(
                token, entry, account_id=account_id, campaign_id=campaign_id,
                source_adset_name=source_adset_name, new_adset_name=new_adset_name,
                new_ad_name=new_ad_name, daily_budget=daily_budget, website_url=website_url,
                creative_name=creative_name, headline=headline, primary_text=primary_text,
                after_status=after_status, cache=meta_cache,
            )
            if ok and shopify_source_handle:
                run_shopify_bridge(
                    entry, new_ad_name=new_ad_name, source_handle=shopify_source_handle,
                    title_override=shopify_title, tags_override=shopify_tags,
                    template_override=shopify_template, amazon_link_override=shopify_amazon,
                )
            upsert(entry)

            if not ok:
                fail += 1
                details.append(f"{lineno}행 ({new_ad_name}): {err}")
            elif entry["status"] == "incomplete":
                incomplete += 1
                details.append(f"{lineno}행 ({new_ad_name}): Meta 광고는 생성됨, Shopify 실패 — {entry['shopifyError']}")
            else:
                success += 1

        summary = f"대량 업로드 완료 — 성공 {success}건 / 미완료(Shopify) {incomplete}건 / 실패 {fail}건"
        message = {"kind": "ok" if (fail == 0 and incomplete == 0) else "err", "text": summary, "details": details}

    session["flash_message"] = message
    return redirect("/")


@app.route("/delete/<req_id>", methods=["POST"])
def delete(req_id):
    """Called via fetch() from the page's JS, not a form post — the row is
    removed from requests.json and the caller deletes the <tr> itself, so the
    page never navigates away (keeping whichever tab/scroll position it was on)."""
    remove_request(req_id)
    return {"ok": True}


if __name__ == "__main__":
    app.run(debug=True, port=5000)
