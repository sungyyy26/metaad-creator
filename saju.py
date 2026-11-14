"""사주팔자 계산 엔진 — 재미용 사주 보기(웹 대시보드 🔮)에서 쓴다.

정확도를 위해 지키는 것:
- 연주는 1월 1일/설날이 아니라 입춘(태양 황경 315°)에서 바뀐다.
- 월주는 절기 경계(황경 315°부터 30°씩)로 정한다 — 태양 황경을 Meeus 공식으로
  직접 계산하므로(오차 수 분~15분) 절기 표가 필요 없다.
- 일주는 율리우스 적일(JDN) 기준 60갑자 순환(2000-01-01 = 무오).
- 시주는 한국 표준시(동경 135°)와 서울 경도(약 127°) 차이 30분을 보정하고,
  1987·1988년 서머타임을 되돌린다. 보정 시각 23:00부터는 다음 날 일주(자시 시작).
- 음력 생일은 korean_lunar_calendar(한국천문연구원 데이터)로 양력 변환.
"""
import hashlib
import math
from datetime import date, datetime, timedelta

STEMS = "갑을병정무기경신임계"
STEMS_HANJA = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "자축인묘진사오미신유술해"
BRANCHES_HANJA = "子丑寅卯辰巳午未申酉戌亥"
ANIMALS = ["쥐", "소", "호랑이", "토끼", "용", "뱀", "말", "양", "원숭이", "닭", "개", "돼지"]
STEM_COLORS = ["푸른", "푸른", "붉은", "붉은", "황금", "황금", "하얀", "하얀", "검은", "검은"]

ELEMENTS = ["목", "화", "토", "금", "수"]
ELEMENT_HANJA = ["木", "火", "土", "金", "水"]
# 지지의 오행과, 십성을 볼 때 쓰는 본기(정기) 천간
BRANCH_ELEMENT = [4, 2, 0, 0, 2, 1, 1, 2, 3, 3, 2, 4]
BRANCH_MAIN_STEM = [9, 5, 0, 1, 4, 2, 3, 5, 6, 7, 4, 8]

TEN_GODS = [("비견", "겁재"), ("식신", "상관"), ("편재", "정재"), ("편관", "정관"), ("편인", "정인")]
TEN_GOD_GROUPS = ["비겁", "식상", "재성", "관성", "인성"]

KST_OFFSET = timedelta(hours=9)
LMT_CORRECTION = timedelta(minutes=30)
# (시작, 끝) KST 벽시계 — 이 구간의 시각은 실제보다 1시간 빠르게 적혀 있다.
KOREA_DST = [
    (datetime(1987, 5, 10, 2), datetime(1987, 10, 11, 3)),
    (datetime(1988, 5, 8, 2), datetime(1988, 10, 9, 3)),
]


class SajuInputError(ValueError):
    pass


# ---------- 천문: 태양 황경 ----------

def _julian_day(dt_utc):
    """UTC datetime -> 율리우스일(소수 포함)."""
    y, m = dt_utc.year, dt_utc.month
    d = dt_utc.day + (dt_utc.hour + dt_utc.minute / 60 + dt_utc.second / 3600) / 24
    if m <= 2:
        y, m = y - 1, m + 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def sun_longitude(jd):
    """겉보기 태양 황경(도). Meeus, Astronomical Algorithms 25장 저정밀 공식."""
    t = (jd - 2451545.0) / 36525
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m)
         + (0.019993 - 0.000101 * t) * math.sin(2 * m) + 0.000289 * math.sin(3 * m))
    omega = math.radians(125.04 - 1934.136 * t)
    return (l0 + c - 0.00569 - 0.00478 * math.sin(omega)) % 360


def _jd_when_longitude(target, jd_guess):
    """jd_guess 근처에서 태양 황경이 target이 되는 순간(율리우스일)."""
    jd = jd_guess
    for _ in range(50):
        diff = (target - sun_longitude(jd) + 540) % 360 - 180
        if abs(diff) < 1e-6:
            break
        jd += diff * 365.2422 / 360
    return jd


def jd_to_kst(jd):
    days = jd - 2440587.5  # 1970-01-01T00:00Z
    return datetime(1970, 1, 1) + timedelta(days=days) + KST_OFFSET


# ---------- 입력 정리 ----------

def to_solar(calendar, year, month, day, leap=False):
    """음력이면 양력 date로 변환. 범위 밖이거나 없는 날짜면 SajuInputError."""
    if calendar == "lunar":
        from korean_lunar_calendar import KoreanLunarCalendar
        cal = KoreanLunarCalendar()
        if not cal.setLunarDate(year, month, day, bool(leap)):
            raise SajuInputError("존재하지 않는 음력 날짜예요. 윤달 여부를 확인해주세요.")
        return date.fromisoformat(cal.SolarIsoFormat())
    try:
        return date(year, month, day)
    except ValueError:
        raise SajuInputError("존재하지 않는 날짜예요.")


def _undo_dst(local):
    for start, end in KOREA_DST:
        if start <= local < end:
            return local - timedelta(hours=1)
    return local


# ---------- 기둥 ----------

def pillar_index(stem, branch):
    """(천간, 지지) -> 60갑자 순번(0=갑자)."""
    return (6 * stem - 5 * branch) % 60


def pillar(idx):
    stem, branch = idx % 10, idx % 12
    return {
        "index": idx, "stem": stem, "branch": branch,
        "hangul": STEMS[stem] + BRANCHES[branch],
        "hanja": STEMS_HANJA[stem] + BRANCHES_HANJA[branch],
    }


def day_pillar_index(d):
    jdn = d.toordinal() + 1721425
    return (jdn - 11) % 60


def ten_god(day_stem, other_stem):
    me, other = day_stem // 2, other_stem // 2
    rel = (other - me) % 5
    same_polarity = (day_stem % 2) == (other_stem % 2)
    return TEN_GODS[rel][0 if same_polarity else 1], rel


def build_chart(birth):
    """birth: {calendar: 'solar'|'lunar', year, month, day, leap, hour, minute,
    time_unknown, gender: 'M'|'F'|''} -> 명식 dict."""
    solar = to_solar(birth.get("calendar", "solar"), int(birth["year"]), int(birth["month"]),
                     int(birth["day"]), birth.get("leap"))
    if not (1900 <= solar.year <= 2049):
        raise SajuInputError("1900~2049년생까지 볼 수 있어요.")
    time_unknown = bool(birth.get("time_unknown"))
    if time_unknown:
        local = datetime(solar.year, solar.month, solar.day, 12)
    else:
        hour, minute = int(birth.get("hour", 0)), int(birth.get("minute", 0))
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise SajuInputError("태어난 시간을 다시 확인해주세요.")
        local = datetime(solar.year, solar.month, solar.day, hour, minute)
    local = _undo_dst(local)
    jd = _julian_day(local - KST_OFFSET)
    lon = sun_longitude(jd)

    # 월: 입춘(315°)부터 30°씩. 0=인월 … 11=축월
    month_idx = int(((lon - 315) % 360) // 30)
    saju_year = solar.year - 1 if (solar.month <= 2 and month_idx >= 10) else solar.year
    year_idx = (saju_year - 4) % 60
    year_stem = year_idx % 10
    month_stem = ((year_stem % 5) * 2 + 2 + month_idx) % 10
    month_branch = (month_idx + 2) % 12

    lmt = local - LMT_CORRECTION
    if time_unknown:
        day_date = solar
    else:
        day_date = lmt.date() + timedelta(days=1 if lmt.hour >= 23 else 0)
    day_idx = day_pillar_index(day_date)
    day_stem = day_idx % 10

    pillars = {
        "year": pillar(year_idx),
        "month": pillar(pillar_index(month_stem, month_branch)),
        "day": pillar(day_idx),
        "hour": None,
    }
    if not time_unknown:
        hb = ((lmt.hour * 60 + lmt.minute + 60) // 120) % 12
        hs = ((day_stem % 5) * 2 + hb) % 10
        pillars["hour"] = pillar(pillar_index(hs, hb))

    # 오행 개수(8글자, 시주 모르면 6글자)와 각 글자의 십성
    counts = [0] * 5
    cells = []
    for pos in ("hour", "day", "month", "year"):
        p = pillars[pos]
        if not p:
            continue
        counts[p["stem"] // 2] += 1
        counts[BRANCH_ELEMENT[p["branch"]]] += 1
        stem_god = "일간(나)" if pos == "day" else ten_god(day_stem, p["stem"])[0]
        branch_god = ten_god(day_stem, BRANCH_MAIN_STEM[p["branch"]])[0]
        cells.append({
            "pos": pos, **p,
            "stem_element": p["stem"] // 2, "branch_element": BRANCH_ELEMENT[p["branch"]],
            "stem_god": stem_god, "branch_god": branch_god,
        })

    # 신강/신약: 나를 돕는 글자(비겁·인성)의 비중. 월지 3, 일지 2, 나머지 1.
    weights = {("month", "branch"): 3, ("day", "branch"): 2}
    support = total = 0
    group_weight = [0] * 5
    for c in cells:
        for part, stem in (("stem", c["stem"]), ("branch", BRANCH_MAIN_STEM[c["branch"]])):
            if c["pos"] == "day" and part == "stem":
                continue
            w = weights.get((c["pos"], part), 1)
            rel = ten_god(day_stem, stem)[1]
            group_weight[rel] += w
            total += w
            if rel in (0, 4):
                support += w
    ratio = support / total if total else 0.5
    strength = "신강" if ratio >= 0.6 else "신약" if ratio < 0.4 else "중화"

    me = day_stem // 2
    if strength == "신강":
        candidates = [(me + 1) % 5, (me + 2) % 5]   # 힘을 빼줄 식상·재성
    elif strength == "신약":
        candidates = [(me + 4) % 5, me]               # 힘을 보태줄 인성·비겁
    else:
        candidates = [counts.index(min(counts))]
    lucky_element = min(candidates, key=lambda e: counts[e])

    return {
        "solar_date": solar.isoformat(),
        "time_unknown": time_unknown,
        "gender": birth.get("gender") or "",
        "pillars": pillars,
        "cells": cells,
        "day_master": day_stem,
        "day_animal": f"{STEM_COLORS[day_stem]} {ANIMALS[day_idx % 12]}",
        "year_animal": ANIMALS[year_idx % 12],
        "element_counts": counts,
        "strength": strength,
        "support_ratio": round(ratio, 2),
        "dominant_group": TEN_GOD_GROUPS[max(range(5), key=lambda g: group_weight[g])],
        "lucky_element": lucky_element,
        "weak_element": counts.index(min(counts)),
        "strong_element": counts.index(max(counts)),
        "_jd": jd,
        "_lon": lon,
    }


# ---------- 대운 ----------

def daewoon(chart, today=None):
    """10년 단위 대운 8개와 현재 대운. 성별이 없으면 None."""
    gender = chart.get("gender")
    if gender not in ("M", "F"):
        return None
    year_yang = chart["pillars"]["year"]["stem"] % 2 == 0
    forward = (year_yang and gender == "M") or (not year_yang and gender == "F")
    lon, jd = chart["_lon"], chart["_jd"]
    if forward:
        target = (15 + 30 * math.ceil((lon - 15) / 30 + 1e-9)) % 360
        term_jd = _jd_when_longitude(target, jd + ((target - lon) % 360) * 365.2422 / 360)
        days = term_jd - jd
    else:
        target = (15 + 30 * math.floor((lon - 15) / 30)) % 360
        term_jd = _jd_when_longitude(target, jd - ((lon - target) % 360) * 365.2422 / 360)
        days = jd - term_jd
    start_age = max(1, round(days / 3))
    month_idx = chart["pillars"]["month"]["index"]
    step = 1 if forward else -1
    birth_year = int(chart["solar_date"][:4])
    today = today or date.today()
    korean_age = today.year - birth_year + 1
    periods = []
    for k in range(1, 9):
        p = pillar((month_idx + step * k) % 60)
        age = start_age + 10 * (k - 1)
        god, rel = ten_god(chart["day_master"], p["stem"])
        periods.append({**p, "age": age, "start_year": birth_year + age - 1,
                        "ten_god": god, "group": TEN_GOD_GROUPS[rel],
                        "current": age <= korean_age < age + 10})
    return {"forward": forward, "start_age": start_age, "periods": periods}


# ---------- 오늘의 운세 ----------

def is_six_harmony(a, b):
    return (a + b) % 12 == 1


def is_clash(a, b):
    return (a - b) % 12 == 6


def is_three_harmony(a, b):
    return a != b and a % 4 == b % 4


GOOD_GODS = {"식신": 4, "정재": 4, "정관": 4, "정인": 4, "편재": 3, "비견": 3, "편인": 3, "상관": 2, "편관": 2, "겁재": 2}


def daily_fortune(chart, on_date, seed=""):
    """on_date의 일진과 내 사주를 비교한 하루 운세(점수는 1~5)."""
    today_idx = day_pillar_index(on_date)
    today = pillar(today_idx)
    god, rel = ten_god(chart["day_master"], today["stem"])
    my_branch = chart["pillars"]["day"]["branch"]
    harmony = is_six_harmony(today["branch"], my_branch) or is_three_harmony(today["branch"], my_branch)
    clash = is_clash(today["branch"], my_branch)
    today_elements = {today["stem"] // 2, BRANCH_ELEMENT[today["branch"]]}
    lucky_hit = chart["lucky_element"] in today_elements

    base = GOOD_GODS[god] + (1 if harmony else 0) - (1 if clash else 0) + (1 if lucky_hit else 0)
    digest = hashlib.sha256(f"{seed}|{on_date.isoformat()}".encode()).digest()

    def score(bonus, i):
        return max(1, min(5, base - 1 + bonus + (digest[i] % 3) - 1))

    area_bonus = {  # 그날의 십성이 특히 힘을 주는 영역
        0: {"love": 0, "work": 0, "money": -1, "health": 1},
        1: {"love": 1, "work": 1, "money": 0, "health": 0},
        2: {"love": 0, "work": 0, "money": 1, "health": 0},
        3: {"love": 0, "work": 1, "money": 0, "health": -1},
        4: {"love": 0, "work": 0, "money": 0, "health": 1},
    }[rel]
    scores = {area: score(b, i) for i, (area, b) in enumerate(area_bonus.items())}
    return {
        "date": on_date.isoformat(), "pillar": today, "ten_god": god, "group": TEN_GOD_GROUPS[rel],
        "harmony": harmony, "clash": clash, "lucky_hit": lucky_hit,
        "scores": scores, "overall": round(sum(scores.values()) / 4),
        "variant": digest[10] % 2,
    }


# ---------- 궁합 ----------

def compatibility(a, b):
    score = 60
    notes = []
    sa, sb = a["day_master"], b["day_master"]
    ea, eb = sa // 2, sb // 2
    rel = (eb - ea) % 5
    if abs(sa - sb) == 5:
        score += 18; notes.append("stem_harmony")
    if rel == 0:
        score += 5; notes.append("same_element")
    elif rel in (1, 4):
        score += 12; notes.append("generate")
    else:
        score -= 6; notes.append("control")
    ba, bb = a["pillars"]["day"]["branch"], b["pillars"]["day"]["branch"]
    if is_six_harmony(ba, bb):
        score += 12; notes.append("branch_harmony")
    elif is_three_harmony(ba, bb):
        score += 8; notes.append("branch_trio")
    elif is_clash(ba, bb):
        score -= 12; notes.append("branch_clash")
    if a["weak_element"] == b["strong_element"]:
        score += 7; notes.append("a_filled")
    if b["weak_element"] == a["strong_element"]:
        score += 7; notes.append("b_filled")
    ya, yb = a["pillars"]["year"]["branch"], b["pillars"]["year"]["branch"]
    if is_six_harmony(ya, yb) or is_three_harmony(ya, yb):
        score += 4; notes.append("zodiac_good")
    elif is_clash(ya, yb):
        score -= 4; notes.append("zodiac_clash")
    return {"score": max(35, min(99, score)), "notes": notes, "element_relation": rel}
