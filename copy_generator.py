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
import re
from pathlib import Path

DATA_DIR = Path(__file__).parent / "copy_generator_data"


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
        if len(headlines) >= 3:
            break

    primary_texts = [tpl.format(**fmt) for tpl in product["body_templates"]][:3]

    return {
        "product_code": parsed["product_code"],
        "product_name_ko": product["name_ko"],
        "product_short_ko": product["short_ko"],
        "headlines": headlines,
        "primary_texts": primary_texts,
        "cta": product["cta"],
    }
