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
import csv
import io
import json
import os
import re
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
UPLOADS_DB_PATH = os.path.join(os.path.dirname(__file__), "uploads.json")
LOCK = threading.Lock()

ACCOUNTS = {
    "1298298124998350": "EQQUALBERRY_AMAZON_US (USD)",
}
CANDIDATE_ACCOUNT_IDS = list(ACCOUNTS.keys())

# Matches the meta-ad-duplicator artifact's bulk paste column order exactly.
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


def split_bulk_rows(text):
    """Splits pasted bulk text into rows the way a spreadsheet's own
    tab-separated clipboard format does, not just on every newline: when a
    cell contains a real line break (a multi-line 기본 텍스트, e.g.), Excel and
    Google Sheets wrap that cell in double quotes when you copy it, so
    csv.reader (given tab as the delimiter) correctly treats the quoted
    newline as part of the same field instead of as a new row. A plain,
    unquoted cell parses exactly as a naive tab-split would."""
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    return [row for row in reader if any(cell.strip() for cell in row)]


class RowError(Exception):
    pass


# 예산 조정 탭: 업로드하는 파일마다 헤더 이름이 다를 수 있어서, 정확한 문구가
# 아니라 허용된 별칭 집합으로 컬럼을 찾는다. 값(딕셔너리 키)은 코드 내부용,
# 사람에게 보여줄 땐 BUDGET_FIELD_LABELS를 쓴다.
BUDGET_COLUMN_ALIASES = {
    "creative_name": {"소재", "소재명", "광고명"},
    "current_budget": {"기존", "기존예산", "기존 예산", "현재", "현재예산", "현재 예산"},
    "new_budget": {"변경", "변경예산", "변경 예산", "제안", "제안예산", "제안 예산"},
}
BUDGET_FIELD_LABELS = {
    "creative_name": "소재명",
    "current_budget": "기존 예산",
    "new_budget": "변경 예산",
}


def find_budget_columns(header_row):
    """Maps each required field to its 0-indexed column position by matching
    the header row's cell text against BUDGET_COLUMN_ALIASES, not a fixed
    column order — different teammates export this spreadsheet with
    different headers. Raises RowError naming exactly what's missing rather
    than silently guessing, since a wrong guess would mismatch real columns."""
    positions = {}
    for idx, cell in enumerate(header_row):
        label = (str(cell) if cell is not None else "").strip()
        for field, aliases in BUDGET_COLUMN_ALIASES.items():
            if field not in positions and label in aliases:
                positions[field] = idx
    missing = [field for field in BUDGET_COLUMN_ALIASES if field not in positions]
    if missing:
        raise RowError("헤더에서 다음 컬럼을 찾지 못했습니다: " + ", ".join(BUDGET_FIELD_LABELS[m] for m in missing))
    return positions


def parse_budget_amount(value):
    """Loosely parses a budget cell — strips currency symbols/commas/won
    signs and whitespace, since real spreadsheets format numbers
    inconsistently (e.g. '₩1,000', '1000.0', ' 1,000 ')."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-]", "", text)
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        return int(round(float(cleaned)))
    except ValueError:
        return None


def read_budget_rows(filename, file_obj):
    """Reads an uploaded .xlsx/.xlsm/.csv file into a list of
    {creative_name, current_budget, new_budget} dicts, using find_budget_columns
    for flexible header matching instead of assuming a fixed column order."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise RowError("엑셀(.xlsx) 파일을 읽으려면 pip install openpyxl 이 필요합니다 (webapp/requirements.txt에 포함되어 있습니다).")
        try:
            workbook = openpyxl.load_workbook(file_obj, read_only=True, data_only=True)
        except Exception as e:  # noqa: BLE001
            raise RowError(f"엑셀 파일을 열지 못했습니다: {e}")
        all_rows = list(workbook.active.iter_rows(values_only=True))
    elif ext == ".csv":
        text = file_obj.read().decode("utf-8-sig")
        all_rows = list(csv.reader(io.StringIO(text)))
    elif ext == ".xls":
        raise RowError("옛 .xls 형식은 지원하지 않습니다 — 엑셀에서 '다른 이름으로 저장 → .xlsx'로 저장한 뒤 다시 올려주세요.")
    else:
        raise RowError(f"지원하지 않는 파일 형식입니다: '{ext or '(확장자 없음)'}' — .xlsx 또는 .csv 파일을 올려주세요.")

    if not all_rows:
        raise RowError("파일에 내용이 없습니다.")
    header, data_rows = all_rows[0], all_rows[1:]
    positions = find_budget_columns(header)

    def cell(row, idx):
        return row[idx] if idx < len(row) else None

    results = []
    for row in data_rows:
        if row is None or all(c in (None, "") for c in row):
            continue
        creative_name = (str(cell(row, positions["creative_name"]) or "")).strip()
        if not creative_name:
            continue
        results.append({
            "creative_name": creative_name,
            "current_budget": parse_budget_amount(cell(row, positions["current_budget"])),
            "new_budget": parse_budget_amount(cell(row, positions["new_budget"])),
        })
    if not results:
        raise RowError("헤더 아래에 실제 데이터 행이 없습니다.")
    return results


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


def _load_json_list(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json_list(path, items):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def _upsert(path, entry):
    with LOCK:
        items = [it for it in _load_json_list(path) if it["id"] != entry["id"]]
        items.append(entry)
        _save_json_list(path, items)


def _remove(path, entry_id):
    with LOCK:
        items = [it for it in _load_json_list(path) if it["id"] != entry_id]
        _save_json_list(path, items)


def load_requests():
    return _load_json_list(DB_PATH)


def upsert(entry):
    _upsert(DB_PATH, entry)


def remove_request(req_id):
    _remove(DB_PATH, req_id)


def load_uploads():
    return _load_json_list(UPLOADS_DB_PATH)


def upsert_upload(entry):
    _upsert(UPLOADS_DB_PATH, entry)


def remove_upload(upload_id):
    _remove(UPLOADS_DB_PATH, upload_id)


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
    uploads = list(reversed(load_uploads()))
    message = session.pop("flash_message", None)

    return render_template("index.html", accounts=ACCOUNTS, items=items, uploads=uploads,
                            message=message, bulk_columns=BULK_COLUMNS)


def _manual_input_snapshot(form, headline, primary_text):
    """Captures everything /retry needs to resubmit a manual request exactly
    as it was first entered, without asking the user to retype it."""
    return {
        "campaign_id": form.get("campaign_id", ""),
        "account_override": form.get("account_override", "").strip(),
        "source_adset_name": form.get("source_adset_name", ""),
        "new_adset_name": form.get("new_adset_name", ""),
        "new_ad_name": form.get("new_ad_name", ""),
        "daily_budget": form.get("daily_budget", ""),
        "website_url": form.get("website_url", ""),
        "creative_name": form.get("creative_name", ""),
        "headline": headline,
        "primary_text": primary_text,
        "after_status": form.get("after_status", "PAUSED"),
        "shopify_source_handle": form.get("shopify_source_handle", "").strip(),
        "shopify_title": form.get("shopify_title_override", "").strip(),
        "shopify_tags": form.get("shopify_tags_override", "").strip(),
        "shopify_template": form.get("shopify_template_override", "").strip(),
        "shopify_amazon": form.get("shopify_amazon_override", "").strip(),
    }


@app.route("/submit", methods=["POST"])
def submit():
    form = request.form
    headline, primary_text = form.get("headline", ""), form.get("primary_text", "")
    entry = new_entry(
        campaign=form.get("campaign_id", ""),
        sourceAdset=form.get("source_adset_name", ""),
        adSetName=form.get("new_adset_name", ""),
        adName=form.get("new_ad_name", ""),
        budget=form.get("daily_budget", ""),
        input=_manual_input_snapshot(form, headline, primary_text),
    )
    upsert(entry)

    token = os.environ.get("META_ACCESS_TOKEN")
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
    rows = list(enumerate(split_bulk_rows(text), start=1))

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

            entry = new_entry(
                campaign=row["campaign_id"], sourceAdset=row["source_adset_name"],
                adSetName=row["new_adset_name"], adName=row["new_ad_name"],
                budget=row["daily_budget"],
                input={
                    "campaign_id": row["campaign_id"],
                    "account_override": "",
                    "source_adset_name": row["source_adset_name"],
                    "new_adset_name": row["new_adset_name"],
                    "new_ad_name": row["new_ad_name"],
                    "daily_budget": row["daily_budget"],
                    "website_url": row["website_url"],
                    "creative_name": row["creative_name"],
                    "headline": row["headline"],
                    "primary_text": row["primary_text"],
                    "start_iso": row["start_iso"],
                    "end_iso": row["end_iso"],
                    "after_status": row["after_status"],
                    "shopify_source_handle": row["shopify_source_handle"],
                    "shopify_title": row["shopify_title"],
                    "shopify_tags": row["shopify_tags"],
                    "shopify_template": row["shopify_template"],
                    "shopify_amazon": row["shopify_amazon"],
                },
            )
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


@app.route("/preview_bulk", methods=["POST"])
def preview_bulk():
    """Parses the pasted bulk text the same way /submit_bulk does, but only
    parses — never calls Meta/Shopify — so the page can show what each row
    will do before the user commits to running it."""
    text = request.form.get("bulk_text", "")
    rows = list(enumerate(split_bulk_rows(text), start=1))

    preview = []
    for lineno, cols in rows:
        try:
            row = parse_bulk_row(cols)
        except RowError as e:
            preview.append({"lineno": lineno, "ok": False, "error": str(e)})
            continue
        preview.append({
            "lineno": lineno,
            "ok": True,
            "campaign": row["campaign_id"],
            "source_adset": row["source_adset_name"] or "(광고만 추가 — 기존 세트 사용)",
            "new_adset": row["new_adset_name"],
            "new_ad_name": row["new_ad_name"],
            "daily_budget": row["daily_budget"],
            "website_url": row["website_url"],
            "creative_name": row["creative_name"],
            "headline": row["headline"],
            "primary_text": row["primary_text"],
            "start": row["start_iso"] or "즉시",
            "end": row["end_iso"] or "없음",
            "status": "즉시 활성화" if row["after_status"] == "ACTIVE" else "일시중지",
            "shopify_handle": row["shopify_source_handle"] or "-",
            "shopify_title": row["shopify_title"] or None,
            "warning": row["warning"],
        })

    ok_count = sum(1 for p in preview if p["ok"])
    return {"rows": preview, "total": len(rows), "ok_count": ok_count, "error_count": len(rows) - ok_count}


@app.route("/retry/<req_id>", methods=["POST"])
def retry(req_id):
    """Re-runs a request that previously failed outright, using the inputs
    captured when it was first submitted — no retyping. Only offered for
    status == 'error' (nothing was created yet); an 'incomplete' entry already
    has a real Meta ad, so re-running it would create a duplicate ad rather
    than fix anything."""
    items = load_requests()
    entry = next((it for it in items if it["id"] == req_id), None)
    if not entry or not entry.get("input") or entry.get("status") != "error":
        session["flash_message"] = {"kind": "err", "text": "이 항목은 재시도할 수 없습니다."}
        return redirect("/")

    inp = entry["input"]
    entry["submittedAt"] = datetime.now().strftime("%m/%d %H:%M")
    entry.pop("error", None)
    upsert(entry)

    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        entry["status"] = "error"
        entry["error"] = "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다. 터미널에서 export/set 하고 서버를 다시 시작하세요."
        upsert(entry)
        session["flash_message"] = {"kind": "err", "text": entry["error"]}
        return redirect("/")

    ok, err = run_duplicate(
        token, entry,
        campaign_id=inp["campaign_id"], account_override=inp.get("account_override") or None,
        source_adset_name=inp["source_adset_name"], new_adset_name=inp["new_adset_name"],
        new_ad_name=inp["new_ad_name"], daily_budget=inp["daily_budget"],
        website_url=inp["website_url"], creative_name=inp["creative_name"],
        headline=inp["headline"], primary_text=inp["primary_text"],
        start_iso=inp.get("start_iso"), end_iso=inp.get("end_iso"),
        after_status=inp.get("after_status", "PAUSED"),
    )
    if ok and inp.get("shopify_source_handle"):
        run_shopify_bridge(
            entry, new_ad_name=inp["new_ad_name"], source_handle=inp["shopify_source_handle"],
            title_override=inp.get("shopify_title"), tags_override=inp.get("shopify_tags"),
            template_override=inp.get("shopify_template"), amazon_link_override=inp.get("shopify_amazon"),
        )
    upsert(entry)

    if not ok:
        message = {"kind": "err", "text": err}
    elif entry["status"] == "incomplete":
        message = {"kind": "err", "text": f"Meta 광고는 생성됐지만 Shopify 브릿지 페이지 실패: {entry['shopifyError']}"}
    else:
        message = {"kind": "ok", "text": f"재시도 성공: 광고 세트 {entry['result']['ad_set_id']} / 광고 {entry['result']['ad_id']}"}

    session["flash_message"] = message
    return redirect("/")


@app.route("/delete/<req_id>", methods=["POST"])
def delete(req_id):
    """Called via fetch() from the page's JS, not a form post — the row is
    removed from requests.json and the caller deletes the <tr> itself, so the
    page never navigates away (keeping whichever tab/scroll position it was on)."""
    remove_request(req_id)
    return {"ok": True}


@app.route("/delete_all_requests", methods=["POST"])
def delete_all_requests():
    """Clears the request-history log only (never the upload log, and never
    anything already created in Meta/Shopify) — a real form post + redirect,
    confirmed client-side first since it's a one-shot bulk action."""
    _save_json_list(DB_PATH, [])
    session["flash_message"] = {"kind": "ok", "text": "요청 기록을 모두 삭제했습니다."}
    return redirect("/")


@app.route("/upload_creative", methods=["POST"])
def upload_creative():
    """Uploads a local video/image file straight into a Meta ad account's
    creative library — no public hosting needed, since this runs on your own
    machine and can send the file's bytes directly."""
    file = request.files.get("creative_file")
    account_id = request.form.get("upload_account_id", "")
    entry = new_entry(filename=(file.filename if file else ""), accountId=account_id)
    upsert_upload(entry)

    token = os.environ.get("META_ACCESS_TOKEN")
    if not file or not file.filename:
        entry["status"] = "error"
        entry["error"] = "업로드할 파일을 선택해주세요."
    elif not account_id:
        entry["status"] = "error"
        entry["error"] = "업로드할 광고 계정을 선택해주세요."
    elif not token:
        entry["status"] = "error"
        entry["error"] = "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다."
    else:
        try:
            result = meta_lib.upload_creative(token, account_id, file.filename, file.stream)
            entry["status"] = "done"
            entry["result"] = result
        except meta_lib.MetaApiError as e:
            entry["status"] = "error"
            entry["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            entry["status"] = "error"
            entry["error"] = f"예상치 못한 오류: {e}"
    upsert_upload(entry)

    message = ({"kind": "ok", "text": f"업로드 완료: {entry['result']['kind']} — {entry['result']['asset_id']}"}
               if entry["status"] == "done"
               else {"kind": "err", "text": entry.get("error", "알 수 없는 오류")})
    session["flash_message"] = message
    return redirect("/")


@app.route("/delete_upload/<upload_id>", methods=["POST"])
def delete_upload(upload_id):
    remove_upload(upload_id)
    return {"ok": True}


@app.route("/budget_lookup", methods=["POST"])
def budget_lookup():
    """Reads the uploaded 소재/기존예산/변경예산 file, finds every Meta campaign
    whose name contains both the 채널 and 제품군 keywords, then matches each
    file row's 소재명 against the ad (or creative) names inside just those
    campaigns — never calls Meta to change anything, only looks things up, so
    the results can be reviewed before /apply_budget_changes is ever called."""
    channel = request.form.get("channel", "").strip()
    product_group = request.form.get("product_group", "").strip()
    file = request.files.get("budget_file")

    if not channel:
        return {"ok": False, "error": "채널을 선택해주세요."}, 400
    if not file or not file.filename:
        return {"ok": False, "error": "엑셀(.xlsx) 또는 CSV 파일을 선택해주세요."}, 400

    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        return {"ok": False, "error": "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다. 터미널에서 export/set 하고 서버를 다시 시작하세요."}, 400

    try:
        rows = read_budget_rows(file.filename, file.stream)
    except RowError as e:
        return {"ok": False, "error": str(e)}, 400

    keywords = [channel, product_group] if product_group else [channel]
    cache = {}
    try:
        campaigns = meta_lib.find_matching_campaigns(token, CANDIDATE_ACCOUNT_IDS, keywords, cache=cache)
    except meta_lib.MetaApiError as e:
        return {"ok": False, "error": str(e)}, 400

    if not campaigns:
        return {
            "ok": True, "campaigns": [], "matches": [],
            "not_found": [r["creative_name"] for r in rows],
            "message": f"'{' / '.join(keywords)}' 키워드를 모두 포함하는 캠페인을 찾지 못했습니다.",
        }

    all_ads = []
    try:
        for c in campaigns:
            for ad in meta_lib.list_campaign_ads(token, c["id"], cache=cache):
                all_ads.append({**ad, "_campaign_name": c["name"]})
    except meta_lib.MetaApiError as e:
        return {"ok": False, "error": str(e)}, 400

    matches, not_found, seen = [], [], set()
    for row in rows:
        needle = row["creative_name"].lower()
        found_ads = [
            ad for ad in all_ads
            if needle in (ad.get("name") or "").lower()
            or needle in ((ad.get("creative") or {}).get("name") or "").lower()
        ]
        if not found_ads:
            not_found.append(row["creative_name"])
            continue
        for ad in found_ads:
            adset = ad.get("adset") or {}
            adset_id = adset.get("id")
            # Several ads can share one ad set (budget lives on the ad set, not
            # the ad) — only offer each ad set once per matched creative name.
            dedupe_key = (row["creative_name"], adset_id)
            if not adset_id or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            live_budget = int(adset["daily_budget"]) // 100 if adset.get("daily_budget") else None
            matches.append({
                "creative_name": row["creative_name"],
                "campaign_name": ad["_campaign_name"],
                "adset_id": adset_id,
                "adset_name": adset.get("name", ""),
                "ad_name": ad.get("name", ""),
                "file_current_budget": row["current_budget"],
                "live_current_budget": live_budget,
                "mismatch": (row["current_budget"] is not None and live_budget is not None
                             and row["current_budget"] != live_budget),
                "new_budget": row["new_budget"],
            })

    return {
        "ok": True,
        "campaigns": [{"id": c["id"], "name": c["name"]} for c in campaigns],
        "matches": matches,
        "not_found": not_found,
    }


@app.route("/apply_budget_changes", methods=["POST"])
def apply_budget_changes():
    """Actually writes the selected ad sets' daily budgets to Meta — only
    ever called after the user has reviewed /budget_lookup's results and
    explicitly picked which rows to apply, never automatically."""
    token = os.environ.get("META_ACCESS_TOKEN")
    if not token:
        return {"ok": False, "error": "META_ACCESS_TOKEN 환경변수가 설정되어 있지 않습니다."}, 400

    payload = request.get_json(silent=True) or {}
    items = payload.get("items", [])
    if not items:
        return {"ok": False, "error": "반영할 항목이 없습니다."}, 400

    results = []
    for item in items:
        adset_id = item.get("adset_id")
        new_budget = item.get("new_budget")
        if not adset_id or new_budget is None:
            results.append({"adset_id": adset_id, "ok": False, "error": "adset_id 또는 new_budget이 없습니다."})
            continue
        try:
            meta_lib.update_adset_budget(token, adset_id, new_budget)
            results.append({"adset_id": adset_id, "ok": True})
        except meta_lib.MetaApiError as e:
            results.append({"adset_id": adset_id, "ok": False, "error": str(e)})
    return {"ok": True, "results": results}


if __name__ == "__main__":
    app.run(debug=True, port=5000)
