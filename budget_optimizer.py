"""Pure calculation helpers for the Meta budget-adjustment dashboard."""
import csv
import io
import os
import re
from datetime import date, datetime, timedelta


class BudgetDataError(ValueError):
    pass


RAW_ALIASES = {
    "date": {"date", "day", "날짜", "일자", "reporting starts", "reporting start"},
    "ad_name": {"ad", "ad name", "ad_name", "광고", "광고명", "소재명"},
    "spend": {"spend", "spend (usd)", "amount spent", "amount spent (usd)", "광고비", "비용"},
    "sales": {"amz sales", "amz.sales", "amazon sales", "amazon attribution sales", "amz_sales", "아마존 매출", "매출"},
}


def _normalized(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _number(value):
    text = re.sub(r"[^0-9.\-]", "", str(value or ""))
    try:
        return float(text) if text not in ("", "-", ".") else 0.0
    except ValueError:
        return 0.0


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%m/%d/%y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except ValueError:
            pass
    raise BudgetDataError(f"날짜를 읽지 못했습니다: {text!r}")


def read_raw_report(filename, file_obj):
    ext = os.path.splitext(filename)[1].lower()
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError as error:
            raise BudgetDataError("엑셀 파일을 읽으려면 openpyxl이 필요합니다.") from error
        try:
            rows = list(openpyxl.load_workbook(file_obj, read_only=True, data_only=True).active.iter_rows(values_only=True))
        except Exception as error:  # noqa: BLE001
            raise BudgetDataError(f"엑셀 파일을 열지 못했습니다: {error}") from error
    elif ext == ".csv":
        rows = list(csv.reader(io.StringIO(file_obj.read().decode("utf-8-sig"))))
    else:
        raise BudgetDataError("D.ROAS RAW 파일은 .xlsx, .xlsm 또는 .csv 형식이어야 합니다.")
    if not rows:
        raise BudgetDataError("D.ROAS RAW 파일이 비어 있습니다.")

    positions = {}
    for index, cell in enumerate(rows[0]):
        label = _normalized(cell)
        for field, aliases in RAW_ALIASES.items():
            if field not in positions and label in aliases:
                positions[field] = index
    missing = [field for field in RAW_ALIASES if field not in positions]
    if missing:
        labels = {"date": "날짜", "ad_name": "광고명", "spend": "Spend", "sales": "AMZ Sales"}
        raise BudgetDataError("RAW 파일에서 컬럼을 찾지 못했습니다: " + ", ".join(labels[x] for x in missing))

    result = []
    for row in rows[1:]:
        def cell(field):
            pos = positions[field]
            return row[pos] if pos < len(row) else None
        name = str(cell("ad_name") or "").strip()
        if not name:
            continue
        result.append({
            "date": _date(cell("date")), "ad_name": name,
            "spend": _number(cell("spend")), "sales": _number(cell("sales")),
        })
    if not result:
        raise BudgetDataError("RAW 파일에 분석할 광고 데이터가 없습니다.")
    return result


def summarize_ad_history(rows, ad_name, today):
    matched = sorted((r for r in rows if r["ad_name"].casefold() == ad_name.casefold()), key=lambda r: r["date"])
    spending = [r for r in matched if r["spend"] > 0]
    if not spending:
        return {
            "first_setup": today, "restart": today, "days": 1, "spend_since_restart": 0.0,
            "note": f"{ad_name}: 스펜딩 이력 없음 — 오늘 재개로 가정, 날짜 확인 필요",
            "needs_confirmation": True,
        }
    first = spending[0]["date"]
    restart = first
    off_period = None
    for previous, current in zip(spending, spending[1:]):
        if (current["date"] - previous["date"]).days >= 3:
            restart = current["date"]
            off_period = (previous["date"] + timedelta(days=1), current["date"] - timedelta(days=1))

    last_spend = spending[-1]["date"]
    needs_confirmation = (today - last_spend).days >= 3
    if needs_confirmation:
        restart = today
    spend_since = sum(r["spend"] for r in matched if r["date"] >= restart)
    note = ""
    if needs_confirmation:
        note = f"{ad_name}: 최초 세팅 {first.isoformat()}, 마지막 스펜딩 {last_spend.isoformat()} — 오늘 재개로 가정, 실제 재개일 확인 필요"
    elif off_period:
        note = (
            f"{ad_name}: 최초 세팅 {first.isoformat()}, {off_period[0].isoformat()}~"
            f"{off_period[1].isoformat()} OFF, {restart.isoformat()} 재개"
        )
    return {
        "first_setup": first, "restart": restart,
        "days": max(1, (today - restart).days + 1),
        "spend_since_restart": spend_since, "note": note,
        "needs_confirmation": needs_confirmation,
    }


def _meta_date(value):
    """Parse a Meta created_time/date value without failing the analysis."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).date()
    except (TypeError, ValueError):
        return None


def attach_raw_metrics(adsets, raw_rows, today=None):
    today = today or date.today()
    seven_start = today - timedelta(days=6)
    for item in adsets:
        histories = [summarize_ad_history(raw_rows, name, today) for name in item["active_ad_names"]]
        # 운영일수는 RAW의 최근 재개일, 광고세트 생성일, 활성 광고 생성일 중
        # 가장 최신 일자를 기준으로 잡는다. 같은 이름의 과거 광고 이력이
        # 현재 광고의 운영일수를 부풀리지 않도록 하기 위함이다.
        raw_controlling = max(histories, key=lambda h: h["restart"]) if histories else None
        meta_dates = [_meta_date(item.get("adset_created_time"))]
        meta_dates.extend(_meta_date(value) for value in item.get("active_ad_created_times", []))
        latest_meta_date = max((value for value in meta_dates if value), default=None)
        controlling_date = max(
            (value for value in (raw_controlling["restart"] if raw_controlling else None, latest_meta_date, today if not latest_meta_date else None) if value)
        )
        controlling_days = max(1, (today - controlling_date).days + 1)
        active_names = {name.casefold() for name in item["active_ad_names"]}
        recent = [r for r in raw_rows if r["ad_name"].casefold() in active_names and seven_start <= r["date"] <= today]
        spend_7d = sum(r["spend"] for r in recent)
        sales_7d = sum(r["sales"] for r in recent)
        item.update({
            "operating_days": controlling_days,
            "spend_since_restart": sum(
                r["spend"] for r in raw_rows
                if r["ad_name"].casefold() in active_names and r["date"] >= controlling_date
            ),
            "spend_7d": spend_7d,
            "droas_7d": (sales_7d / spend_7d) if spend_7d > 0 else None,
            "note": " / ".join(
                ([f"운영일수 기준일 {controlling_date.isoformat()} (광고세트/활성 광고/최근 재개 중 최신)"]
                 if latest_meta_date and (not raw_controlling or latest_meta_date >= raw_controlling["restart"]) else [])
                + [h["note"] for h in histories if h["note"]]
            ),
            "restart_confirmation_needed": any(h["needs_confirmation"] for h in histories),
        })
        item["bucket"] = "신규" if item["operating_days"] < 14 else "기존"
    return adsets


def _weighted_allocate(items, pool, weight_key, cap_key=None):
    allocations = {}
    remaining = list(items)
    remaining_pool = max(0.0, float(pool))
    while remaining:
        weight_sum = sum(max(0.0, float(item.get(weight_key) or 0)) for item in remaining)
        if weight_sum <= 0:
            equal = remaining_pool / len(remaining)
            for item in remaining:
                allocations[item["adset_id"]] = equal
            break
        capped = []
        for item in remaining:
            share = remaining_pool * float(item[weight_key]) / weight_sum
            cap = item.get(cap_key) if cap_key else None
            if cap is not None and share > cap:
                allocations[item["adset_id"]] = float(cap)
                remaining_pool -= float(cap)
                capped.append(item)
        if not capped:
            for item in remaining:
                allocations[item["adset_id"]] = remaining_pool * float(item[weight_key]) / weight_sum
            break
        remaining = [item for item in remaining if item not in capped]
    return allocations


def _round_allocations(items, target=None):
    for item in items:
        item["suggested_budget"] = max(0, int(round(item.get("suggested_budget", 0))))
    if target is None or not items:
        return
    difference = int(round(target)) - sum(item["suggested_budget"] for item in items)
    adjustable = [item for item in items if item.get("allocation_adjustable")]
    while difference and adjustable:
        changed = False
        for item in adjustable:
            if not difference:
                break
            step = 1 if difference > 0 else -1
            cap = item.get("increase_cap")
            within_cap = cap is None or item["suggested_budget"] + step <= cap
            if item["suggested_budget"] + step >= 0 and within_cap:
                item["suggested_budget"] += step
                difference -= step
                changed = True
        if not changed:
            break


def optimize(adsets, desired_total):
    desired_total = max(0, float(desired_total))
    new = [item for item in adsets if item["bucket"] == "신규"]
    existing = [item for item in adsets if item["bucket"] == "기존"]

    minimum = [item for item in new if item["operating_days"] <= 3 or item["spend_since_restart"] <= 300]
    assessable = [item for item in new if item not in minimum and item.get("cpa_3d") is not None]
    missing_cpa = [item for item in new if item not in minimum and item.get("cpa_3d") is None]
    avg_cpa = sum(item["cpa_3d"] for item in assessable) / len(assessable) if assessable else None
    off_cpa = max(2.0, avg_cpa * 1.3) if avg_cpa is not None else 2.0
    new_target = len(assessable + missing_cpa) * 300 + len(minimum) * 100

    fixed_new = []
    weighted_new = []
    for item in new:
        item["classification"] = ""
        item["reasons"] = []
        if item in minimum:
            item["suggested_budget"] = 100
            item["classification"] = "최소배정"
            item["reasons"].append("라이브 3일 이하 또는 재개 후 스펜딩 $300 이하")
            fixed_new.append(item)
        elif item in missing_cpa:
            item["suggested_budget"] = 100
            item["classification"] = "CPA 데이터 없음"
            item["reasons"].append("최근 3일 체크아웃 CPA 데이터 없음")
            fixed_new.append(item)
        else:
            cpa = item["cpa_3d"]
            if cpa >= off_cpa:
                item["classification"] = "OFF 후보"
                item["reasons"].append(f"CPA ${cpa:.2f} ≥ OFF 기준 ${off_cpa:.2f}")
                weighted_new.append(item)  # explicitly remains in 1/CPA weighting
            elif avg_cpa is not None and cpa > avg_cpa:
                item["suggested_budget"] = 100
                item["classification"] = "평균 CPA 초과"
                item["reasons"].append(f"CPA ${cpa:.2f} > 평균 ${avg_cpa:.2f}")
                fixed_new.append(item)
            else:
                weighted_new.append(item)
            item["inverse_cpa"] = 1 / cpa if cpa > 0 else 0
        if 7 <= item["operating_days"] < 14 and item.get("droas_7d") is not None and item["droas_7d"] < 0.20:
            item["classification"] = (item["classification"] + " · " if item["classification"] else "") + "OFF 검토 제안"
            item["reasons"].append("7일 D.ROAS 20% 미만")

    fixed_total = sum(item.get("suggested_budget", 0) for item in fixed_new)
    for item in weighted_new:
        item["increase_cap"] = float(item["current_budget"]) + 400
        item["allocation_adjustable"] = True
    weighted_allocations = _weighted_allocate(weighted_new, new_target - fixed_total, "inverse_cpa", "increase_cap")
    for item in weighted_new:
        item["suggested_budget"] = weighted_allocations.get(item["adset_id"], 0)
    _round_allocations(new, new_target)

    existing_target = max(0.0, desired_total - new_target)
    held = [item for item in existing if item.get("spend_7d", 0) <= 300]
    evaluable = [item for item in existing if item not in held and item.get("droas_7d") is not None]
    avg_droas = sum(item["droas_7d"] for item in evaluable) / len(evaluable) if evaluable else None
    off_existing = [item for item in evaluable if item["droas_7d"] <= 0.50]
    weighted_existing = [item for item in evaluable if item not in off_existing]
    for item in existing:
        item["classification"] = ""
        item["reasons"] = []
        if item in held or item.get("droas_7d") is None:
            item["suggested_budget"] = item["current_budget"]
            item["classification"] = "판정 보류"
            item["reasons"].append("최근 7일 광고비 $300 이하 또는 D.ROAS 데이터 없음")
        elif item in off_existing:
            item["suggested_budget"] = 0
            item["classification"] = "OFF 후보"
            item["reasons"].append("7일 D.ROAS 50% 이하")
        else:
            item["droas_weight"] = item["droas_7d"]
            item["allocation_adjustable"] = True
    hold_total = sum(item["suggested_budget"] for item in held)
    existing_allocations = _weighted_allocate(weighted_existing, existing_target - hold_total, "droas_weight")
    for item in weighted_existing:
        item["suggested_budget"] = existing_allocations.get(item["adset_id"], 0)
    _round_allocations(existing, existing_target)

    for item in adsets:
        if item.get("cpm_3d") is not None and item["cpm_3d"] >= 30:
            item["classification"] = (item["classification"] + " · " if item["classification"] else "") + "추가 모니터링"
            item["reasons"].append("CPM $30불 이상이지만 추가 모니터링")
        item["reason"] = " / ".join(item["reasons"])
        item.pop("reasons", None)

    summary = {
        "desired_total": desired_total,
        "new_count": len(new), "new_minimum_count": len(minimum), "new_assessable_count": len(assessable),
        "new_target": new_target, "avg_cpa": avg_cpa, "off_cpa": off_cpa,
        "existing_count": len(existing), "existing_target": existing_target,
        "existing_hold_count": len(held), "avg_droas": avg_droas,
        "new_allocated": sum(item["suggested_budget"] for item in new),
        "existing_allocated": sum(item["suggested_budget"] for item in existing),
        "warnings": [],
    }
    if existing_target < sum(item["current_budget"] for item in held):
        summary["warnings"].append("기존 버킷 보류 예산 합계가 기존 버킷 목표예산보다 큽니다.")
    if desired_total < new_target:
        summary["warnings"].append("희망 총예산이 신규 버킷 규칙상 목표예산보다 작습니다.")
    allocated_total = summary["new_allocated"] + summary["existing_allocated"]
    if int(round(allocated_total)) != int(round(desired_total)):
        gap = desired_total - allocated_total
        summary["warnings"].append(
            f"증액 상한 또는 보류 예산 때문에 목표 대비 ${abs(gap):.0f} "
            + ("미배분되었습니다." if gap > 0 else "초과 배정되었습니다.")
        )
    return adsets, summary
