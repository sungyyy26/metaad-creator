"""Pure calculation helpers for the Meta budget-adjustment dashboard."""
import csv
import io
import os
import re
from datetime import date, datetime, timedelta
from statistics import median


class BudgetDataError(ValueError):
    pass


RAW_ALIASES = {
    "date": {"date", "day", "날짜", "일자", "reporting starts", "reporting start"},
    "ad_name": {
        "ad", "ad name", "ad_name", "광고", "광고명", "소재명",
        "attribution", "attribution name", "attribution_name", "어트리뷰션", "어트리뷰션명",
    },
    "sales": {
        "amz sales", "amz.sales", "amazon sales", "amazon attribution sales", "amz_sales",
        "14 day total sales", "total sales", "아마존 매출", "매출",
    },
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
        if "date" not in positions and ("date" in label or "날짜" in label or "일자" in label):
            positions["date"] = index
        if "ad_name" not in positions and "attribution" in label and "name" in label:
            positions["ad_name"] = index
        if "sales" not in positions and "sales" in label and any(token in label for token in ("amz", "amazon", "total")):
            positions["sales"] = index
    missing = [field for field in RAW_ALIASES if field not in positions]
    if missing:
        labels = {"date": "날짜", "ad_name": "어트리뷰션명", "sales": "AMZ Sales"}
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
            "date": _date(cell("date")), "ad_name": name, "sales": _number(cell("sales")),
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
    # "D-7" follows the ops convention requested here: anchor date and the
    # preceding seven dates, e.g. 09/11-09/18 (8 inclusive calendar dates).
    seven_start = today - timedelta(days=7)
    for item in adsets:
        spend_rows = []
        for row in item.pop("_meta_spend_rows", []):
            spend_rows.append({
                "date": _date(row.get("date")), "ad_name": row.get("ad_name", ""),
                "spend": _number(row.get("spend")),
            })
        histories = [summarize_ad_history(spend_rows, name, today) for name in item["active_ad_names"]]
        # 운영일수는 Meta Spend의 최근 재개일, 광고세트 생성일, 활성 광고 생성일 중
        # 가장 최신 일자를 기준으로 잡는다. 같은 이름의 과거 광고 이력이
        # 현재 광고의 운영일수를 부풀리지 않도록 하기 위함이다.
        spend_controlling = max(histories, key=lambda h: h["restart"]) if histories else None
        meta_dates = [_meta_date(item.get("adset_created_time"))]
        meta_dates.extend(_meta_date(value) for value in item.get("active_ad_created_times", []))
        latest_meta_date = max((value for value in meta_dates if value), default=None)
        controlling_date = max(
            (value for value in (spend_controlling["restart"] if spend_controlling else None, latest_meta_date, today if not latest_meta_date else None) if value)
        )
        controlling_days = max(1, (today - controlling_date).days + 1)
        active_names = {name.casefold() for name in item["active_ad_names"]}
        recent_sales = [r for r in raw_rows if r["ad_name"].casefold() in active_names and seven_start <= r["date"] <= today]
        recent_spend = [r for r in spend_rows if r["ad_name"].casefold() in active_names and seven_start <= r["date"] <= today]
        spend_7d = sum(r["spend"] for r in recent_spend)
        sales_7d = sum(r["sales"] for r in recent_sales)
        item.update({
            "operating_days": controlling_days,
            "spend_since_restart": sum(
                r["spend"] for r in spend_rows
                if r["ad_name"].casefold() in active_names and r["date"] >= controlling_date
            ),
            "spend_7d": spend_7d,
            "droas_7d": (sales_7d / spend_7d) if spend_7d > 0 else None,
            "amz_sales_7d": sales_7d,
            "note": " / ".join(
                ([f"운영일수 기준일 {controlling_date.isoformat()} (광고세트/활성 광고/최근 재개 중 최신)"]
                 if latest_meta_date and (not spend_controlling or latest_meta_date >= spend_controlling["restart"]) else [])
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


def _capped_weighted_allocate(items, pool, weight_key, lower_key=None, upper_key=None):
    """Like _weighted_allocate but supports both a floor and a ceiling per
    item, iteratively pinning whichever items hit either bound and
    redistributing the remainder among the rest."""
    allocations = {}
    remaining = list(items)
    remaining_pool = max(0.0, float(pool))
    while remaining:
        weight_sum = sum(max(0.0, float(item.get(weight_key) or 0)) for item in remaining)
        if weight_sum <= 0:
            equal = remaining_pool / len(remaining)
            for item in remaining:
                allocations[item["adset_id"]] = max(0.0, equal)
            break
        pinned = []
        for item in remaining:
            share = remaining_pool * float(item[weight_key]) / weight_sum
            upper = item.get(upper_key) if upper_key else None
            lower = item.get(lower_key) if lower_key else None
            bound = None
            if upper is not None and share > upper:
                bound = float(upper)
            elif lower is not None and share < lower:
                bound = float(lower)
            if bound is not None:
                allocations[item["adset_id"]] = bound
                remaining_pool -= bound
                pinned.append(item)
        if not pinned:
            for item in remaining:
                allocations[item["adset_id"]] = remaining_pool * float(item[weight_key]) / weight_sum
            break
        remaining = [item for item in remaining if item not in pinned]
        if remaining_pool <= 0 and remaining:
            for item in remaining:
                allocations[item["adset_id"]] = 0.0
            remaining = []
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
            floor = item.get("decrease_floor")
            within_cap = cap is None or item["suggested_budget"] + step <= cap
            within_floor = floor is None or item["suggested_budget"] + step >= floor
            if item["suggested_budget"] + step >= 0 and within_cap and within_floor:
                item["suggested_budget"] += step
                difference -= step
                changed = True
        if not changed:
            break


def _matches_detail_filter(item, filter_value):
    """Match a custom budget filter against type and Meta naming fields."""
    needle = str(filter_value or "").strip().casefold()
    if not needle:
        return False
    if str(item.get("type") or "").casefold() == needle:
        return True
    values = [item.get("campaign_name"), item.get("adset_name")]
    values.extend(item.get("active_ad_names") or [])
    return any(needle in str(value or "").casefold() for value in values)


def _apply_detail_budgets(adsets, desired_total, detail_budgets, summary):
    """Keep optional DA/PA/PM pools isolated while preserving base weights.

    Filters are checked in the user's order and the first match wins. DA/PA
    match the derived type directly; every other value is a case-insensitive
    substring search across campaign, ad-set and active-ad names. Unmatched
    rows share the remainder of the total budget.
    """
    if isinstance(detail_budgets, dict):
        detail_budgets = [
            {"filter": key, "budget": value} for key, value in detail_budgets.items()
        ]
    requested = []
    for entry in detail_budgets or []:
        filter_value = str(entry.get("filter") or "").strip()
        if filter_value and entry.get("budget") is not None:
            requested.append({"filter": filter_value, "budget": max(0.0, float(entry["budget"]))})
    if not requested:
        return

    pools = {entry["filter"]: [] for entry in requested}
    pools["기타"] = []
    for item in adsets:
        group = next(
            (entry["filter"] for entry in requested if _matches_detail_filter(item, entry["filter"])),
            "기타",
        )
        item["allocation_group"] = group
        pools[group].append(item)

    remainder = max(0.0, float(desired_total) - sum(entry["budget"] for entry in requested))
    targets = {entry["filter"]: entry["budget"] for entry in requested}
    targets["기타"] = remainder
    allocated = {}
    for group, items in pools.items():
        target = targets.get(group, 0.0)
        if not items:
            allocated[group] = 0.0
            if target > 0:
                summary["warnings"].append(f"{group} 세부 예산 ${target:,.0f}을 배분할 광고세트가 없습니다.")
            continue

        candidates = [item for item in items if item.get("allocation_adjustable")]
        fixed = [item for item in items if item not in candidates]
        fixed_total = sum(max(0.0, float(item.get("suggested_budget") or 0)) for item in fixed)
        adjustable_target = max(0.0, target - fixed_total)
        if fixed_total > target:
            summary["warnings"].append(
                f"{group} 고정·보류 예산 ${fixed_total:,.0f}이 세부 목표 ${target:,.0f}을 초과합니다."
            )
        for item in candidates:
            base = max(0.0, float(item.get("suggested_budget") or 0))
            if base <= 0:
                if item.get("cpa_3d"):
                    base = 1 / float(item["cpa_3d"])
                elif item.get("droas_7d") is not None:
                    base = max(0.0, float(item["droas_7d"]))
                else:
                    base = max(1.0, float(item.get("current_budget") or 0))
            item["_detail_weight"] = base
            item["allocation_adjustable"] = True
        allocations = _weighted_allocate(candidates, adjustable_target, "_detail_weight")
        for item in candidates:
            item["suggested_budget"] = allocations.get(item["adset_id"], 0)
            item.pop("_detail_weight", None)
        _round_allocations(candidates, adjustable_target)
        allocated[group] = sum(item["suggested_budget"] for item in items)
        if not candidates and target > fixed_total:
            summary["warnings"].append(
                f"{group} 세부 목표 중 ${target - fixed_total:,.0f}은 조정 가능한 광고세트가 없어 미배분되었습니다."
            )

    summary["detail_budgets"] = requested
    summary["detail_allocated"] = allocated
    summary["new_allocated"] = sum(item["suggested_budget"] for item in adsets if item["bucket"] == "신규")
    summary["existing_allocated"] = sum(item["suggested_budget"] for item in adsets if item["bucket"] == "기존")


def optimize(adsets, desired_total, detail_budgets=None):
    desired_total = max(0, float(desired_total))
    new = [item for item in adsets if item["bucket"] == "신규"]
    existing = [item for item in adsets if item["bucket"] == "기존"]

    def minimum_budget(item):
        return 100 * max(1, int(item.get("active_ad_count") or 0))

    minimum = [item for item in new if item["operating_days"] <= 3 or item["spend_since_restart"] <= 300]
    assessable = [item for item in new if item not in minimum and item.get("cpa_3d") is not None]
    missing_cpa = [item for item in new if item not in minimum and item.get("cpa_3d") is None]
    avg_cpa = sum(item["cpa_3d"] for item in assessable) / len(assessable) if assessable else None
    off_cpa = max(2.0, avg_cpa * 1.3) if avg_cpa is not None else 2.0
    new_target = (
        sum(max(300, minimum_budget(item)) for item in assessable + missing_cpa)
        + sum(minimum_budget(item) for item in minimum)
    )

    fixed_new = []
    weighted_new = []
    for item in new:
        item["classification"] = ""
        item["reasons"] = []
        if item in minimum:
            item["suggested_budget"] = minimum_budget(item)
            item["classification"] = "최소배정"
            item["reasons"].append(
                f"라이브 3일 이하 또는 재개 후 스펜딩 $300 이하 · "
                f"활성 소재 {max(1, int(item.get('active_ad_count') or 0))}개 × $100"
            )
            fixed_new.append(item)
        elif item in missing_cpa:
            item["suggested_budget"] = minimum_budget(item)
            item["classification"] = "CPA 데이터 없음"
            item["reasons"].append(
                f"PDT D-3 체크아웃 CPA 데이터 없음 · 활성 소재 "
                f"{max(1, int(item.get('active_ad_count') or 0))}개 × $100"
            )
            fixed_new.append(item)
        else:
            cpa = item["cpa_3d"]
            if cpa >= off_cpa:
                item["classification"] = "OFF 후보"
                item["reasons"].append(f"CPA ${cpa:.2f} ≥ OFF 기준 ${off_cpa:.2f}")
                weighted_new.append(item)  # explicitly remains in 1/CPA weighting
            elif avg_cpa is not None and cpa > avg_cpa:
                item["suggested_budget"] = minimum_budget(item)
                item["classification"] = "평균 CPA 초과"
                item["reasons"].append(
                    f"CPA ${cpa:.2f} > 평균 ${avg_cpa:.2f} · 활성 소재 "
                    f"{max(1, int(item.get('active_ad_count') or 0))}개 × $100"
                )
                fixed_new.append(item)
            else:
                weighted_new.append(item)
            item["inverse_cpa"] = 1 / cpa if cpa > 0 else 0
        if 7 <= item["operating_days"] < 14 and item.get("droas_7d") is not None and item["droas_7d"] < 0.20:
            item["classification"] = (item["classification"] + " · " if item["classification"] else "") + "OFF 검토 제안"
            item["reasons"].append("PDT D-7 D.ROAS 20% 미만")

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
    # The peer average remains a reference metric; the actual OFF threshold is
    # fixed at 30% regardless of the current portfolio average.
    off_droas = 0.30
    off_existing = [item for item in evaluable if item["droas_7d"] <= off_droas]
    weighted_existing = [item for item in evaluable if item not in off_existing]
    for item in existing:
        item["classification"] = ""
        item["reasons"] = []
        if item in held or item.get("droas_7d") is None:
            item["suggested_budget"] = item["current_budget"]
            item["classification"] = "판정 보류"
            item["reasons"].append("PDT D-7 광고비 $300 이하 또는 D.ROAS 데이터 없음")
        elif item in off_existing:
            item["suggested_budget"] = 0
            item["classification"] = "OFF 후보"
            item["reasons"].append("PDT D-7 D.ROAS 30% 이하 (고정 OFF 기준)")
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
        "existing_hold_count": len(held), "avg_droas": avg_droas, "off_droas": off_droas,
        "current_total": sum(float(item.get("current_budget") or 0) for item in adsets),
        "new_allocated": sum(item["suggested_budget"] for item in new),
        "existing_allocated": sum(item["suggested_budget"] for item in existing),
        "warnings": [],
    }
    if existing_target < sum(item["current_budget"] for item in held):
        summary["warnings"].append("기존 버킷 보류 예산 합계가 기존 버킷 목표예산보다 큽니다.")
    if desired_total < new_target:
        summary["warnings"].append("희망 총예산이 신규 버킷 규칙상 목표예산보다 작습니다.")
    _apply_detail_budgets(adsets, desired_total, detail_budgets, summary)
    allocated_total = summary["new_allocated"] + summary["existing_allocated"]
    if int(round(allocated_total)) != int(round(desired_total)):
        gap = desired_total - allocated_total
        summary["warnings"].append(
            f"증액 상한 또는 보류 예산 때문에 목표 대비 ${abs(gap):.0f} "
            + ("미배분되었습니다." if gap > 0 else "초과 배정되었습니다.")
        )
    return adsets, summary


def _confidence(item):
    """How much to trust this item's own D-7 D.ROAS: 0 (just launched, no
    real signal yet) to 1 (7+ days live AND $300+ D-7 spend, fully mature).
    Requires both a time gate and a volume gate — a set can be old but
    low-spend (still thin data) or high-spend but very new (still noisy)."""
    day_conf = min(1.0, (item.get("operating_days") or 0) / 7.0)
    spend_conf = min(1.0, (item.get("spend_7d") or 0) / 300.0)
    return max(0.0, min(day_conf, spend_conf))


def optimize_v2(adsets, desired_total, detail_budgets=None):
    """Confidence-blended D.ROAS allocator ('신규 로직').

    Unlike optimize(), new and existing sets are judged on the same metric
    (D.ROAS) instead of switching from CPA to D.ROAS at day 14. A set's own
    D-7 D.ROAS is blended with a CPA-derived prior in proportion to how much
    real data it has (_confidence) — CPA is only ever a stand-in for D.ROAS
    while data is thin, never the primary judge, since the two don't
    reliably correlate. Allocation weight, the OFF gate, and how far a
    budget can move are all scaled by that same confidence, so a noisy new
    set can't swing wildly while a proven winner can be pushed hard.
    """
    desired_total = max(0, float(desired_total))

    def floor_budget(item):
        return 100 * max(1, int(item.get("active_ad_count") or 0))

    for item in adsets:
        item["confidence"] = _confidence(item)

    # --- CPA -> D.ROAS prior, calibrated from this run's own mature sets ---
    calib = [
        item for item in adsets
        if item["confidence"] >= 0.8 and item.get("cpa_3d") and item["cpa_3d"] > 0
        and item.get("droas_7d") is not None
    ]
    prior_a = prior_b = None
    if len(calib) >= 3:
        xs = [1 / item["cpa_3d"] for item in calib]
        ys = [item["droas_7d"] for item in calib]
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        var_x = sum((x - mean_x) ** 2 for x in xs)
        if var_x > 1e-9:
            prior_b = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / var_x
            prior_a = mean_y - prior_b * mean_x
    droas_pool = [item["droas_7d"] for item in adsets if item.get("droas_7d") is not None]
    fallback_prior = (sum(droas_pool) / len(droas_pool)) if droas_pool else 0.0

    def prior_droas(item):
        if prior_a is not None and item.get("cpa_3d") and item["cpa_3d"] > 0:
            return max(0.0, prior_a + prior_b * (1 / item["cpa_3d"]))
        return fallback_prior

    for item in adsets:
        conf = item["confidence"]
        raw = item["droas_7d"] if item.get("droas_7d") is not None else 0.0
        item["effective_droas"] = conf * raw + (1 - conf) * prior_droas(item)

    # --- OFF gate: relative (portfolio median) AND absolute floor must both trip ---
    gated = [item for item in adsets if item["confidence"] >= 0.5 and item.get("droas_7d") is not None]
    median_droas = median(item["droas_7d"] for item in gated) if gated else None
    off_relative = median_droas * 0.8 if median_droas is not None else None
    off_absolute = 0.30

    brand_new, off_items, circuit_breaker, pool_items = [], [], [], []
    for item in adsets:
        item["classification"] = ""
        item["reasons"] = []
        conf = item["confidence"]
        active_count = max(1, int(item.get("active_ad_count") or 0))
        if (item.get("operating_days") or 0) <= 2:
            item["suggested_budget"] = floor_budget(item)
            item["classification"] = "신규 보호(최소예산)"
            item["reasons"].append(f"세팅 2일 이내 · 활성 소재 {active_count}개 × $100 고정")
            brand_new.append(item)
        elif (
            conf >= 0.5 and item.get("droas_7d") is not None and off_relative is not None
            and item["droas_7d"] <= off_relative and item["droas_7d"] <= off_absolute
        ):
            item["suggested_budget"] = 0
            item["classification"] = "OFF 후보"
            item["reasons"].append(
                f"D.ROAS {item['droas_7d'] * 100:.0f}% ≤ 중앙값×0.8({off_relative * 100:.0f}%) 및 "
                f"절대 하한({off_absolute * 100:.0f}%) 동시 충족"
            )
            off_items.append(item)
        elif conf < 0.5 and item.get("cpa_3d") is not None and item["cpa_3d"] >= 2.0:
            item["suggested_budget"] = 0
            item["classification"] = "OFF 후보 (CPA 조기경보)"
            item["reasons"].append(f"실측 D.ROAS 신뢰도 확보 전 · CPA ${item['cpa_3d']:.2f} ≥ $2.00 조기 경고")
            circuit_breaker.append(item)
        else:
            pool_items.append(item)

    fixed_total = sum(item["suggested_budget"] for item in brand_new + off_items + circuit_breaker)
    pool_target = max(0.0, desired_total - fixed_total)

    for item in pool_items:
        conf = item["confidence"]
        base_current = item["current_budget"] if item.get("current_budget") else floor_budget(item)
        cap_pct = 0.30 + conf * (1.00 - 0.30)
        item["_v2_weight"] = max(0.0, item["effective_droas"]) ** (1 + conf)
        item["_v2_lower"] = max(floor_budget(item), base_current * (1 - cap_pct))
        item["_v2_upper"] = base_current * (1 + cap_pct)
        item["_v2_cap_pct"] = cap_pct
        item["allocation_adjustable"] = True
        item["increase_cap"] = item["_v2_upper"]
        item["decrease_floor"] = item["_v2_lower"]

    allocations = _capped_weighted_allocate(pool_items, pool_target, "_v2_weight", "_v2_lower", "_v2_upper")
    for item in pool_items:
        item["suggested_budget"] = allocations.get(item["adset_id"], 0)
        conf = item["confidence"]
        item["classification"] = (
            "증액" if item["suggested_budget"] > item["current_budget"]
            else "감액" if item["suggested_budget"] < item["current_budget"] else "유지"
        )
        item["reasons"].append(
            f"신뢰도 {conf * 100:.0f}% · 유효 D.ROAS {item['effective_droas'] * 100:.0f}% · "
            f"가중치 지수 {1 + conf:.2f} · 허용폭 ±{item['_v2_cap_pct'] * 100:.0f}%"
        )

    _round_allocations(brand_new + off_items + circuit_breaker)
    _round_allocations(pool_items, pool_target)

    for item in adsets:
        if item.get("cpm_3d") is not None and item["cpm_3d"] >= 30:
            item["classification"] = (item["classification"] + " · " if item["classification"] else "") + "추가 모니터링"
            item["reasons"].append("CPM $30불 이상이지만 추가 모니터링")
        item["reason"] = " / ".join(item["reasons"])
        item.pop("reasons", None)
        item.pop("allocation_adjustable", None)
        for key in ("_v2_weight", "_v2_lower", "_v2_upper", "_v2_cap_pct", "increase_cap", "decrease_floor"):
            item.pop(key, None)

    summary = {
        "desired_total": desired_total,
        "logic": "v2",
        "brand_new_count": len(brand_new),
        "off_count": len(off_items) + len(circuit_breaker),
        "pool_count": len(pool_items),
        "pool_target": pool_target,
        "median_droas_7d": median_droas,
        "off_relative": off_relative,
        "off_absolute": off_absolute,
        "current_total": sum(float(item.get("current_budget") or 0) for item in adsets),
        "warnings": [],
    }
    if fixed_total > desired_total:
        summary["warnings"].append("신규 보호·OFF 고정 예산 합계가 희망 총예산보다 큽니다.")
    _apply_detail_budgets(adsets, desired_total, detail_budgets, summary)
    allocated_total = sum(item["suggested_budget"] for item in adsets)
    if int(round(allocated_total)) != int(round(desired_total)):
        gap = desired_total - allocated_total
        summary["warnings"].append(
            f"증감 상한 또는 최소예산 때문에 목표 대비 ${abs(gap):.0f} "
            + ("미배분되었습니다." if gap > 0 else "초과 배정되었습니다.")
        )
    return adsets, summary
