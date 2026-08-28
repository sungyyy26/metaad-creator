#!/usr/bin/env python3
"""Recompute the EQQUALBERRY Buy Box artifact's page-state JSON from fresh
Helium 10 buy box data.

Usage:
    python3 buybox_update.py <current_artifact.html> <raw_data.json> <output.html>

raw_data.json shape (produced by the caller from live Helium 10 tool calls):
{
  "seller_id": "AFZNN5H6MIT2Z",
  "fetched_at": "2026-08-28T10:00:00+00:00",
  "summary": <get_buybox_summary data, marketplace=US>,
  "histories": {
    "<ASIN>": <search_buybox_history data>   // only for ASINs with >1 seller
  }
}

This script only updates competitive fields (status, sellers, win7d,
competitor, competitorPrice, lastWinPrice, lastWinAt) and the meta summary
block. Price and historical price-point arrays are carried over unchanged
from the previous artifact, since they come from a separate pricing feed.
"""
import json
import re
import sys
from datetime import datetime, timezone, timedelta

OWN_SELLER_ID = "AFZNN5H6MIT2Z"
PDT = timezone(timedelta(hours=-7))

# Static registry of the 15 ASINs tracked in this dashboard, grouped by
# product line. Only used to know which ASINs belong to which line/name;
# price and history data is preserved from the previous artifact state.
PRODUCT_REGISTRY = {
    "vita": ["B0D8W1YVBX", "B0F1BZLNQJ", "B0H6P3SR4C", "B0FQ9MM42M", "B0H79D8C5K", "B0H7GFV782", "B0H5C999NM"],
    "nad": ["B0FGQ3J31K", "B0FQBRVDTY", "B0H7BRJSC9", "B0FQBL8S3K", "B0H75B6Z8J", "B0H4L5BDP1"],
    "bak": ["B0D8W2W8C2", "B0FQBT6WD9"],
}

def extract_page_state(html: str) -> dict:
    m = re.search(
        r'(<script type="application/json" id="page-state">)(.*?)(</script>)',
        html,
        re.S,
    )
    if not m:
        raise ValueError("page-state script block not found in artifact HTML")
    return json.loads(m.group(2))


def inject_page_state(html: str, state: dict) -> str:
    payload = json.dumps(state, ensure_ascii=False)

    def repl(m):
        return m.group(1) + payload + m.group(3)

    return re.sub(
        r'(<script type="application/json" id="page-state">)(.*?)(</script>)',
        repl,
        html,
        count=1,
        flags=re.S,
    )


def dominant_competitor(rows):
    counts = {}
    for r in rows:
        w = r.get("buy_box_winner")
        if w and w != OWN_SELLER_ID:
            counts[w] = counts.get(w, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def last_own_win(rows):
    own_rows = [r for r in rows if r.get("buy_box_winner") == OWN_SELLER_ID]
    if not own_rows:
        return None
    return max(own_rows, key=lambda r: r.get("time_of_offer_change", 0))


def fmt_last_win_at(epoch_seconds: int) -> str:
    utc_dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    pdt_dt = utc_dt.astimezone(PDT)
    return (
        f"{utc_dt.strftime('%m/%d %H:%M')} UTC "
        f"({pdt_dt.strftime('%m/%d %H:%M')} PDT)"
    )


def clear_competitive_fields(product: dict) -> None:
    for k in ("competitor", "competitorPrice", "lastWinPrice", "lastWinAt"):
        product.pop(k, None)


def set_competitor_fields(product: dict, competitor: str, rows: list) -> None:
    if competitor:
        product["competitor"] = competitor
    product["competitorPrice"] = None
    own_win = last_own_win(rows)
    if own_win:
        product["lastWinPrice"] = product.get("price")
        product["lastWinAt"] = fmt_last_win_at(own_win["time_of_offer_change"])
    else:
        product.pop("lastWinPrice", None)
        product.pop("lastWinAt", None)


def classify(asin: str, histories: dict, losing_asins: dict, product: dict) -> dict:
    hist = histories.get(asin)
    if not hist or not hist.get("rows"):
        # No competing offers observed in the window -> single-seller, uncontested.
        product["sellers"] = 1
        product.pop("win7d", None)
        clear_competitive_fields(product)
        product["status"] = "good"
        return product

    rows = sorted(hist["rows"], key=lambda r: r.get("time_of_offer_change", 0))
    latest = rows[-1]
    product["sellers"] = latest.get("number_of_sellers", 2)

    win_rate = hist.get("buybox_win_percentage")
    if win_rate is not None:
        product["win7d"] = round(win_rate, 4)
    else:
        product.pop("win7d", None)

    # Helium 10 Alerts' own "currently losing" signal takes priority: this is
    # an ASIN we do not currently hold the Buy Box on at all.
    if asin in losing_asins:
        product["status"] = "critical"
        set_competitor_fields(product, losing_asins[asin], rows)
        return product

    # Otherwise, flag active same-day flapping between us and a competitor
    # (repeated back-and-forth within the most recent day of events) as
    # "rotating". An occasional single-day dip that has since resolved is
    # still reported as "good".
    latest_date = latest["event_date"]
    today_rows = [r for r in rows if r["event_date"] == latest_date]
    own_today = sum(1 for r in today_rows if r["buy_box_winner"] == OWN_SELLER_ID)
    other_today = len(today_rows) - own_today
    if own_today >= 2 and other_today >= 2:
        product["status"] = "rotating"
        set_competitor_fields(product, dominant_competitor(today_rows), rows)
        return product

    product["status"] = "good"
    clear_competitive_fields(product)
    return product


def build_callout(flagged: list) -> str:
    if not flagged:
        return ""
    parts = []
    for name, asin, product in flagged:
        win_pct = round(product.get("win7d", 0) * 100)
        lines = []
        competitor = product.get("competitor")
        competitor_link = (
            f'<a href="https://www.amazon.com/sp?seller={competitor}" target="_blank" '
            f'rel="noopener">{competitor}</a>'
            if competitor
            else "알 수 없는 셀러"
        )
        if product["status"] == "critical":
            lines.append(
                f"<li>현재 바이박스를 {competitor_link}에게 완전히 내준 상태 "
                f"(최근 7일 점유율 약 {win_pct}%)</li>"
            )
        else:
            lines.append(
                f"<li>상대 셀러 {competitor_link}와 하루 종일 몇 분 단위로 바이박스 로테이션 중 "
                f"(최근 7일 점유율 약 {win_pct}%)</li>"
            )
            lines.append(
                "<li>스냅샷이 ✅로 찍혀도 직접 열어보면 이미 상대 셀러로 넘어가 있을 확률이 더 높음</li>"
            )
        if product.get("lastWinPrice") is not None:
            lines.append(
                f'<li>실시간 가격 확인 불가 — 마지막 낙찰가 <b>${product["lastWinPrice"]}</b> '
                f'({product.get("lastWinAt", "")})</li>'
            )
        parts.append(
            f'<div class="callout-title"><b>{name}</b> · <code>{asin}</code></div><ul>'
            + "".join(lines)
            + "</ul>"
        )
    return "".join(parts)


def update(state: dict, summary: dict, histories: dict) -> dict:
    losing_asins = {
        row["asin"]: row.get("current_winner")
        for row in (summary.get("losing_asins") or [])
    }

    all_products = []
    for line, asins in PRODUCT_REGISTRY.items():
        line_products = {p["asin"]: p for p in state["products"].get(line, [])}
        updated_line = []
        for asin in asins:
            product = line_products.get(asin, {"asin": asin, "name": asin, "price": None})
            product = classify(asin, histories, losing_asins, product)
            updated_line.append(product)
            all_products.append((product.get("name", asin), asin, product))
        state["products"][line] = updated_line

    good = sum(1 for _, _, p in all_products if p["status"] == "good")
    rotating = [(n, a, p) for n, a, p in all_products if p["status"] == "rotating"]
    critical = [(n, a, p) for n, a, p in all_products if p["status"] == "critical"]
    total = len(all_products)

    now_utc = datetime.now(timezone.utc)
    now_pdt = now_utc.astimezone(PDT)

    state["meta"]["dateLabel"] = now_pdt.strftime("%Y-%m-%d")
    state["meta"]["timeLabel"] = f"{now_pdt.hour:02d}시–{(now_pdt.hour + 1) % 24:02d}시 (PDT)"
    state["meta"]["registeredCount"] = total
    state["meta"]["overallPct"] = f"{good / total * 100:.1f}%" if total else "0.0%"
    state["meta"]["overallSub"] = f"{good} 점유 / {len(rotating) + len(critical)} 로테이션·불안정"
    state["meta"]["rotatingCount"] = f"{len(rotating)}개"
    state["meta"]["rotatingSub"] = ", ".join(n for n, _, _ in rotating) if rotating else "없음"
    state["meta"]["recentLossCount"] = f"{len(critical)}건"
    state["meta"]["recentLossSub"] = (
        ", ".join(n for n, _, _ in critical) if critical else "공식 손실 이벤트 없음"
    )
    state["meta"]["calloutHtml"] = build_callout(rotating + critical)

    return state


def main():
    if len(sys.argv) != 4:
        print(__doc__)
        sys.exit(1)
    current_html_path, raw_data_path, output_html_path = sys.argv[1:4]

    with open(current_html_path, "r", encoding="utf-8") as f:
        html = f.read()
    with open(raw_data_path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    state = extract_page_state(html)
    state = update(state, raw.get("summary", {}), raw.get("histories", {}))
    new_html = inject_page_state(html, state)

    with open(output_html_path, "w", encoding="utf-8") as f:
        f.write(new_html)

    print(f"Updated {output_html_path}: {state['meta']['overallSub']} ({state['meta']['overallPct']})")


if __name__ == "__main__":
    main()
