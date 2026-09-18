"""Local, offline ad-copy generator for EQQUALBERRY 소재명 — the same logic
used by the sungyyy26/copy-generator tool, ported here so this desktop app
can offer real "생성" (auto-generate) support for the headline / primary text
fields without calling Claude or any external API.

Parses a 소재명 built from the USP code registry (copy_generator_data/registry.json)
and produces headline + primary-text candidates from patterns mined from real
EQQUALBERRY_AMAZON_US Meta ads (copy_generator_data/copy_patterns.json).

Keep this file and copy_generator_data/ in sync with sungyyy26/copy-generator's
data/registry.json and data/copy_patterns.json when new product/부위/고민 codes
are added there.
"""
import json
import os
import re
import secrets
import threading
from pathlib import Path

DATA_DIR = Path(__file__).parent / "copy_generator_data"
_DATA_LOCK = threading.RLock()


class CopyGenerationError(ValueError):
    """Raised when a 소재명 can't be parsed into a known product/부위/고민 — the
    caller should fall back to asking for manual copy, same as before this
    feature existed."""


def _load_data():
    registry = json.loads((DATA_DIR / "registry.json").read_text(encoding="utf-8"))
    patterns = json.loads((DATA_DIR / "copy_patterns.json").read_text(encoding="utf-8"))
    part_by_code = {p["code"]: p for p in registry["part"]}
    concern_by_code = {c["code"]: c for c in registry["concern"]}
    return registry, patterns, part_by_code, concern_by_code


_REGISTRY, _PATTERNS, _PART_BY_CODE, _CONCERN_BY_CODE = _load_data()


def _validate_registry(value):
    if not isinstance(value, dict):
        raise CopyGenerationError("registry 데이터는 JSON 객체여야 합니다.")
    for key in ("part", "concern"):
        if not isinstance(value.get(key), list):
            raise CopyGenerationError(f"registry에 '{key}' 배열이 필요합니다.")
        for index, item in enumerate(value[key], start=1):
            if (
                not isinstance(item, dict)
                or not item.get("code")
                or not item.get("ko")
                or "en" not in item
            ):
                raise CopyGenerationError(
                    f"registry.{key}의 {index}번째 항목에는 code, ko, en 값이 필요합니다."
                )


def _validate_patterns(value):
    if not isinstance(value, dict):
        raise CopyGenerationError("copy_patterns 데이터는 JSON 객체여야 합니다.")
    required = (
        "products", "asin_map", "part_singular", "part_display",
        "concern_copy", "care_scope", "hook_shapes",
    )
    missing = [key for key in required if key not in value]
    if missing:
        raise CopyGenerationError("copy_patterns 필수 항목 누락: " + ", ".join(missing))
    if not isinstance(value["products"], dict) or not value["products"]:
        raise CopyGenerationError("copy_patterns.products에 제품 데이터가 필요합니다.")
    for code, product in value["products"].items():
        for key in ("name_ko", "short_ko", "headline_shapes", "body_templates"):
            if key not in product:
                raise CopyGenerationError(f"제품 '{code}'의 '{key}' 항목이 필요합니다.")
        if not isinstance(product["headline_shapes"], list) or not product["headline_shapes"]:
            raise CopyGenerationError(f"제품 '{code}'의 headline_shapes는 비어 있지 않은 배열이어야 합니다.")
        if not isinstance(product["body_templates"], list) or not product["body_templates"]:
            raise CopyGenerationError(f"제품 '{code}'의 body_templates는 비어 있지 않은 배열이어야 합니다.")
    for key in ("asin_map", "part_singular", "part_display", "concern_copy", "care_scope"):
        if not isinstance(value[key], dict):
            raise CopyGenerationError(f"copy_patterns.{key}는 JSON 객체여야 합니다.")
    if not isinstance(value["hook_shapes"], dict):
        raise CopyGenerationError("copy_patterns.hook_shapes는 JSON 객체여야 합니다.")


def _atomic_write_json(path, value):
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temp_path, path)


def update_database(payloads):
    """Validates and replaces registry/pattern JSON files, then hot-reloads.

    ``payloads`` is an iterable of ``(filename, decoded_json)`` pairs. It may
    contain the two existing files separately, or one bundle with ``registry``
    and/or ``copy_patterns`` keys. Existing data is retained for any side not
    present in the upload.
    """
    global _REGISTRY, _PATTERNS, _PART_BY_CODE, _CONCERN_BY_CODE

    registry = None
    patterns = None
    for filename, payload in payloads:
        lower_name = (filename or "").lower()
        if isinstance(payload, dict) and (
            "registry" in payload or "copy_patterns" in payload or "patterns" in payload
        ):
            registry = payload.get("registry", registry)
            patterns = payload.get("copy_patterns", payload.get("patterns", patterns))
        elif "registry" in lower_name:
            registry = payload
        elif "pattern" in lower_name:
            patterns = payload
        elif isinstance(payload, dict) and "products" in payload:
            patterns = payload
        elif isinstance(payload, dict) and "part" in payload and "concern" in payload:
            registry = payload
        else:
            raise CopyGenerationError(
                f"'{filename}'의 데이터 종류를 확인할 수 없습니다. "
                "파일명을 registry.json 또는 copy_patterns.json으로 지정해주세요."
            )

    if registry is None and patterns is None:
        raise CopyGenerationError("업데이트할 registry 또는 copy_patterns 데이터가 없습니다.")

    with _DATA_LOCK:
        current_registry, current_patterns, _, _ = _load_data()
        next_registry = registry if registry is not None else current_registry
        next_patterns = patterns if patterns is not None else current_patterns
        _validate_registry(next_registry)
        _validate_patterns(next_patterns)
        if registry is not None:
            _atomic_write_json(DATA_DIR / "registry.json", next_registry)
        if patterns is not None:
            _atomic_write_json(DATA_DIR / "copy_patterns.json", next_patterns)
        _REGISTRY, _PATTERNS, _PART_BY_CODE, _CONCERN_BY_CODE = _load_data()

    return {
        "updated": [
            name for name, present in (
                ("registry.json", registry is not None),
                ("copy_patterns.json", patterns is not None),
            ) if present
        ],
        "product_count": len(_PATTERNS["products"]),
        "part_count": len(_REGISTRY["part"]),
        "concern_count": len(_REGISTRY["concern"]),
    }


def export_database():
    """Returns a detached bundle suitable for editing and re-uploading."""
    with _DATA_LOCK:
        return json.loads(json.dumps({"registry": _REGISTRY, "copy_patterns": _PATTERNS}))


def _product_code_from_ad_name(name):
    tokens = str(name or "").strip().split("_")
    if tokens and re.fullmatch(r"\d{6,8}", tokens[0]):
        tokens = tokens[1:]
    if not tokens:
        return None
    code = _PATTERNS["asin_map"].get(tokens[0], tokens[0])
    return code if code in _PATTERNS["products"] else None


def _creative_texts(creative):
    headlines, bodies = [], []
    story = (creative or {}).get("object_story_spec") or {}
    for key in ("link_data", "video_data", "photo_data"):
        block = story.get(key) or {}
        if block.get("title"):
            headlines.append(str(block["title"]).strip())
        if block.get("message"):
            bodies.append(str(block["message"]).strip())
    feed = (creative or {}).get("asset_feed_spec") or {}
    headlines.extend(str(value.get("text") or "").strip() for value in feed.get("titles") or [])
    bodies.extend(str(value.get("text") or "").strip() for value in feed.get("bodies") or [])
    return [x for x in headlines if x], [x for x in bodies if x]


def merge_meta_copies(ads):
    """Append unique recent live Meta copy to the matching product pools."""
    global _REGISTRY, _PATTERNS, _PART_BY_CODE, _CONCERN_BY_CODE
    additions = {"headlines": 0, "primary_texts": 0}
    matched_ads = 0
    with _DATA_LOCK:
        next_patterns = json.loads(json.dumps(_PATTERNS))
        for ad in ads:
            product_code = _product_code_from_ad_name(ad.get("name"))
            if not product_code:
                continue
            headlines, bodies = _creative_texts(ad.get("creative") or {})
            if not headlines and not bodies:
                continue
            matched_ads += 1
            product = next_patterns["products"][product_code]
            for text in headlines:
                if text not in product["headline_shapes"]:
                    product["headline_shapes"].append(text)
                    additions["headlines"] += 1
            for text in bodies:
                if text not in product["body_templates"]:
                    product["body_templates"].append(text)
                    additions["primary_texts"] += 1
        _validate_patterns(next_patterns)
        next_patterns["_source"] = "Updated from recent Meta ad creatives via dashboard refresh."
        _atomic_write_json(DATA_DIR / "copy_patterns.json", next_patterns)
        _REGISTRY, _PATTERNS, _PART_BY_CODE, _CONCERN_BY_CODE = _load_data()
    return {**additions, "matched_ads": matched_ads, "fetched_ads": len(ads)}


def _parse_creative_name(name):
    tokens = name.strip().split("_")
    if not tokens or not tokens[0]:
        raise CopyGenerationError("소재명이 비어 있습니다.")

    idx = 0
    if re.fullmatch(r"\d{6}", tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        raise CopyGenerationError("제품 코드를 찾을 수 없습니다.")

    product_token = tokens[idx]
    idx += 1
    product_code = _PATTERNS["asin_map"].get(product_token, product_token)
    if product_code not in _PATTERNS["products"]:
        known = ", ".join(sorted(_PATTERNS["products"].keys()))
        raise CopyGenerationError(
            f"소재명 '{name}' 에서 알 수 없는 제품 코드 '{product_token}' 입니다. "
            f"등록된 제품 코드: {known} (또는 asin_map에 등록된 ASIN 코드)"
        )

    def next_token():
        nonlocal idx
        t = tokens[idx] if idx < len(tokens) else None
        idx += 1
        return t

    next_token()  # ao — unused for copy generation
    next_token()  # adtype — unused for copy generation
    next_token()  # platform — unused for copy generation
    usp_field = next_token()

    part_code = None
    concern_code = None
    hook_tokens = []
    if usp_field:
        segs = usp_field.split("-")
        if segs and segs[0] in _PART_BY_CODE:
            part_code = segs[0]
            rest = segs[1:]
            if rest and rest[0] in _CONCERN_BY_CODE:
                concern_code = rest[0]
                hook_tokens = rest[1:]
            else:
                hook_tokens = rest
        else:
            hook_tokens = segs

    return {
        "product_code": product_code,
        "part_code": part_code,
        "concern_code": concern_code,
        "hook_tokens": hook_tokens,
    }


def generate(creative_name):
    """Generates headline/primary-text candidates for a 소재명.

    Returns {"product_code", "product_name_ko", "headlines" (list, up to 3),
    "primary_texts" (list, up to 3), "cta"}. Raises CopyGenerationError with a
    Korean message when the 소재명 doesn't match a known product code — the
    caller should surface that message and let the user type copy manually,
    same as before this feature existed.
    """
    parsed = _parse_creative_name(creative_name)
    product = _PATTERNS["products"][parsed["product_code"]]

    part_singular = _PATTERNS["part_singular"].get(parsed["part_code"], "Skin")
    part_display = _PATTERNS["part_display"].get(parsed["part_code"], "Skin")
    concern_entry = _PATTERNS["concern_copy"].get(
        parsed["concern_code"], {"noun": "Skin Concerns"}
    )
    concern_noun = concern_entry["noun"]
    care_scope = (
        "body" if parsed["part_code"] in _PATTERNS["care_scope"]["body_parts"] else "face"
    )

    fmt = {
        "part": part_singular,
        "part_lower": part_display.lower(),
        "concern": concern_noun,
        "concern_lower": concern_noun.lower(),
        "concern_upper": "PORE" if parsed["concern_code"] == "pore" else "PLUMPING",
        "hero_ingredient": product["hero_ingredient"],
        "care_scope": care_scope,
    }

    headline_shapes = list(product["headline_shapes"])
    hook_text = " ".join(parsed["hook_tokens"]).lower()
    for key, shape in _PATTERNS["hook_shapes"].items():
        if key in hook_text:
            headline_shapes.insert(0, shape["headline"])
            break

    headlines = []
    for shape in headline_shapes:
        try:
            headlines.append(shape.format(**fmt))
        except KeyError:
            continue

    primary_texts = [tpl.format(**fmt) for tpl in product["body_templates"]]

    # A static database can still yield a different mix/order on each click.
    # Weekly JSON uploads expand this pool without requiring an AI/API call.
    rng = secrets.SystemRandom()
    rng.shuffle(headlines)
    rng.shuffle(primary_texts)
    headlines = headlines[:3]
    primary_texts = primary_texts[:3]

    part_entry = _PART_BY_CODE.get(parsed["part_code"])
    concern_entry_raw = _CONCERN_BY_CODE.get(parsed["concern_code"])

    return {
        "product_code": parsed["product_code"],
        "product_name_ko": product["name_ko"],
        "product_short_ko": product["short_ko"],
        "part_ko": part_entry["ko"] if part_entry else None,
        "part_en": part_entry["en"] if part_entry else None,
        "concern_ko": concern_entry_raw["ko"] if concern_entry_raw else None,
        "concern_en": concern_entry_raw["en"] if concern_entry_raw else None,
        "headlines": headlines,
        "primary_texts": primary_texts,
        "cta": product["cta"],
    }
