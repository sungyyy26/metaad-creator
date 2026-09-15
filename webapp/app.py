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
CANDIDATE_ACCOUNT_IDS = list(ACCOUNTS.keys())

# Matches the meta-ad-duplicator artifact's bulk paste column order exactly
# (minus the Slack-notify column, which this local tool doesn't send).
BULK_COLUMNS = [
    "캠페인 (필수)",
    "복제할 광고 (필수 — 이미 있거나 같은 배치의 다른 행이 만드는 광고 세트에 광고만 추가하려면 비워두세요)",
    "새 광고 세트 이름 (필수)",
    "새 광고 이름 (필수)",
    "일일 예산 (필수)",
    "웹사이트 URL (필수)",
    "소재 (필수)",
    "헤드라인 (실제 문구를 입력하세요 — 이 로컬 도구는 '생성' 자동 작성을 지원하지 않습니다)",
    "기본 텍스트 (실제 문구를 입력하세요 — 이 로컬 도구는 '생성' 자동 작성을 지원하지 않습니다)",
    "시작 (비워두면 즉시; 형식 YYYY/MM/DD H(:MM)AM/PM, 예: 2026/09/08 12AM — 해당 광고 계정의 Meta 시간대 기준)",
    "종료 (비워두면 종료일 없음; 형식은 시작과 동일, 예: 2026/09/08 11:59PM)",
    "생성 후 상태 (비워두면 일시중지)",
    "복제할 Shopify 상품 핸들 (선택 — 비워두면 브릿지 페이지 없음; 제목이 아니라 핸들)",
    "Shopify 제목 재지정 (선택)",
    "Shopify 태그 재지정 (선택, 쉼표로 구분)",
    "Shopify 테마 템플릿 재지정 (선택)",
    "Shopify 아마존 어트리뷰션 링크 재지정 (선택)",
]
# 0-indexed column -> human label, for the required-field check. Column 1
# (복제할 광고) is deliberately NOT required — leaving it blank is the
# "ads-only" mode (add just an ad into an existing/batch-created ad set).
REQUIRED_BULK_COLS = {
    0: "캠페인", 2: "새 광고 세트 이름", 3: "새 광고 이름",
    4: "일일 예산", 5: "웹사이트 URL", 6: "소재",
}
MIN_BULK_COLS = 7  # through "소재" — everything required is in the first 7

# Tokens people commonly type to mean "leave this blank" (a spreadsheet habit) —
# treated as empty rather than as a literal value (e.g. a Shopify handle "-").
BLANK_TOKENS = {"-", "--", "—", "n/a", "na", "없음", "none"}
GENERATE_TOKENS = {"생성", "generate"}


def is_blank(value):
    return not value or value.strip().lower() in BLANK_TOKENS


def is_generate_placeholder(value):
    return (value or "").strip().lower() in GENERATE_TOKENS


def parse_schedule_datetime(value):
    """'YYYY/MM/DD H(:MM)AM/PM' (e.g. '2026/09/08 12AM') -> ISO 8601 with no
    timezone offset, which Meta then interprets in the ad account's own
    configured timezone."""
    normalized = value.strip().upper()
    for fmt in ("%Y/%m/%d %I:%M%p", "%Y/%m/%d %I%p"):
        try:
            return datetime.strptime(normalized, fmt).strftime("%Y-%m-%dT%H:%M:%S")
        except ValueError:
            continue
    raise ValueError(f"'{value}' 형식을 인식하지 못했습니다 (예: 2026/09/08 12AM 또는 2026/09/08 11:59PM)")


class RowError(Exception):
    pass


def parse_bulk_row(cols):
    """Parses one tab-separated bulk-upload row into the fields duplicate_ad()
    needs, or raises RowError with a message describing exactly what's wrong."""
    cols = [c.strip() for c in cols]
    if len(cols) < MIN_BULK_COLS:
        raise RowError(f"열이 최소 {MIN_BULK_COLS}개(캠페인~소재) 필요한데 {len(cols)}개만 입력됨")

    missing = [label for idx, label in REQUIRED_BULK_COLS.items()
               if idx >= len(cols) or is_blank(cols[idx])]
    if missing:
        raise RowError(f"필수 항목이 비어있음: {', '.join(missing)}")

    def get(i):
        return cols[i] if i < len(cols) else ""

    daily_budget = cols[4]
    if not daily_budget.isdigit():
        raise RowError(f"예산은 숫자여야 합니다: '{daily_budget}'")

    headline, primary_text = get(7), get(8)
    for label, value in (("헤드라인", headline), ("기본 텍스트", primary_text)):
        if is_generate_placeholder(value):
            raise RowError(
                f"{label}에 '생성'이 입력되어 있는데, 이 로컬 도구는 Claude를 호출할 수 없어 자동 카피 생성을 지원하지 않습니다 — "
                f"실제 문구를 직접 입력하거나 미리 작성한 텍스트를 붙여넣어 주세요."
            )
        if is_blank(value):
            raise RowError(f"{label}가 비어있습니다 — 실제 문구를 입력해주세요 (자동 생성은 지원되지 않습니다).")

    try:
        start_iso = parse_schedule_datetime(get(9)) if not is_blank(get(9)) else None
        end_iso = parse_schedule_datetime(get(10)) if not is_blank(get(10)) else None
    except ValueError as e:
        raise RowError(str(e))

    after_raw = get(11)
    after_status = "ACTIVE" if (not is_blank(after_raw) and after_raw.strip().lower() in ("active", "활성화")) else "PAUSED"

    warning = None
    extra = [v for v in cols[len(BULK_COLUMNS):] if v.strip()]
    if extra:
        warning = (f"열이 {len(BULK_COLUMNS)}개보다 많이 입력되어 뒤쪽 값이 무시됨 "
                   f"({', '.join(repr(v) for v in extra)}) — 탭이 하나 더 들어갔을 수 있어요.")

    return dict(
        campaign_id=cols[0],
        source_adset_name="" if is_blank(get(1)) else get(1),
        new_adset_name=cols[2],
        new_ad_name=cols[3],
        daily_budget=daily_budget,
        website_url=cols[5],
        creative_name=cols[6],
        headline=headline,
        primary_text=primary_text,
        start_iso=start_iso,
        end_iso=end_iso,
        after_status=after_status,
        shopify_source_handle="" if is_blank(get(12)) else get(12),
        shopify_title="" if is_blank(get(13)) else get(13),
        shopify_tags="" if is_blank(get(14)) else get(14),
        shopify_template="" if is_blank(get(15)) else get(15),
        shopify_amazon="" if is_blank(get(16)) else get(16),
        warning=warning,
    )


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


def run_duplicate(token, entry, *, campaign_id, source_adset_name, new_adset_name,
                   new_ad_name, daily_budget, website_url, creative_name, headline,
                   primary_text, after_status, account_override=None, start_iso=None,
                   end_iso=None, cache=None, batch_adsets=None):
    try:
        result = meta_lib.duplicate_ad(
            token,
            campaign_id=campaign_id,
            candidate_account_ids=CANDIDATE_ACCOUNT_IDS,
            account_override=account_override or None,
            source_adset_name=source_adset_name,
            new_adset_name=new_adset_name,
            new_ad_name=new_ad_name,
            daily_budget=int(daily_budget),
            website_url=website_url,
            creative_name=creative_name,
            headline=headline,
            primary_text=primary_text,
            start_iso=start_iso,
            end_iso=end_iso,
            status=after_status,
            cache=cache,
            batch_adsets=batch_adsets,
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
    headline, primary_text = form.get("headline", ""), form.get("primary_text", "")
    generate_field = next((label for label, v in (("헤드라인", headline), ("기본 텍스트", primary_text))
                           if is_generate_placeholder(v)), None)
    if not token:
        entry["status"] = "error"
        entry["error"] = "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다. 터미널에서 export/set 하고 서버를 다시 시작하세요."
        upsert(entry)
        message = {"kind": "err", "text": entry["error"]}
    elif generate_field:
        entry["status"] = "error"
        entry["error"] = (f"{generate_field}에 '생성'이 입력되어 있는데, 이 로컬 도구는 자동 카피 생성을 지원하지 않습니다 — "
                          f"실제 문구를 입력해주세요.")
        upsert(entry)
        message = {"kind": "err", "text": entry["error"]}
    else:
        ok, err = run_duplicate(
            token, entry,
            campaign_id=form["campaign_id"], account_override=form.get("account_override", "").strip(),
            source_adset_name=form["source_adset_name"], new_adset_name=form["new_adset_name"],
            new_ad_name=form["new_ad_name"], daily_budget=form["daily_budget"],
            website_url=form["website_url"], creative_name=form["creative_name"],
            headline=headline, primary_text=primary_text,
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
        # Shared across every row in this batch: repeated campaign/ad-set/
        # creative-library lookups hit the Meta API once instead of once per
        # row (main thing that trips their ad-account rate limit), and newly
        # created ad sets are tracked here so a later "ads-only" row can find
        # one this same batch just made.
        meta_cache = {}
        batch_adsets = {}
        for lineno, cols in rows:
            try:
                row = parse_bulk_row(cols)
            except RowError as e:
                fail += 1
                details.append(f"{lineno}행: {e}")
                continue

            entry = new_entry(campaign=row["campaign_id"], sourceAdset=row["source_adset_name"],
                               adSetName=row["new_adset_name"], adName=row["new_ad_name"],
                               budget=row["daily_budget"])
            upsert(entry)

            ok, err = run_duplicate(
                token, entry, campaign_id=row["campaign_id"],
                source_adset_name=row["source_adset_name"], new_adset_name=row["new_adset_name"],
                new_ad_name=row["new_ad_name"], daily_budget=row["daily_budget"],
                website_url=row["website_url"], creative_name=row["creative_name"],
                headline=row["headline"], primary_text=row["primary_text"],
                start_iso=row["start_iso"], end_iso=row["end_iso"],
                after_status=row["after_status"], cache=meta_cache, batch_adsets=batch_adsets,
            )
            if ok and row["shopify_source_handle"]:
                run_shopify_bridge(
                    entry, new_ad_name=row["new_ad_name"], source_handle=row["shopify_source_handle"],
                    title_override=row["shopify_title"], tags_override=row["shopify_tags"],
                    template_override=row["shopify_template"], amazon_link_override=row["shopify_amazon"],
                )
            upsert(entry)

            if row["warning"]:
                details.append(f"{lineno}행: {row['warning']}")
            if not ok:
                fail += 1
                details.append(f"{lineno}행 ({row['new_ad_name']}): {err}")
            elif entry["status"] == "incomplete":
                incomplete += 1
                details.append(f"{lineno}행 ({row['new_ad_name']}): Meta 광고는 생성됨, Shopify 실패 — {entry['shopifyError']}")
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
