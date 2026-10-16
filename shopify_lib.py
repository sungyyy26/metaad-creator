"""Shopify Admin GraphQL helpers for creating the 'bridge page' (an UNLISTED
duplicate of a source product, retitled/rehandled for one ad) — mirrors the
Shopify step of the meta-ad-duplicator artifact's processing instructions."""
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

# Shopify drops each API version roughly a year after release, so a hardcoded
# version here will eventually 404 with a bare "Not Found" body. Override with
# SHOPIFY_API_VERSION if this one has since been retired — check
# https://shopify.dev/docs/api/admin-graphql for the current supported list.
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-10")


class ShopifyApiError(RuntimeError):
    pass


class ShopifyNotFoundError(ShopifyApiError):
    """Raised specifically when a lookup (product handle, page tag) finds no
    match — as opposed to any other ShopifyApiError (auth, network, GraphQL
    errors), which callers should treat as a hard failure rather than a
    reason to try a different resource type."""
    pass


def _gql(shop, token, query, variables=None):
    url = f"https://{shop}/admin/api/{API_VERSION}/graphql.json"
    r = requests.post(
        url,
        json={"query": query, "variables": variables or {}},
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
    )
    try:
        data = r.json()
    except ValueError:
        raise ShopifyApiError(
            f"Shopify가 JSON이 아닌 응답을 반환했습니다 (HTTP {r.status_code}) — "
            f"SHOPIFY_SHOP 도메인이나 API 버전({API_VERSION})이 잘못됐을 수 있습니다. "
            f"응답 앞부분: {r.text[:200]!r}"
        )
    if "errors" in data:
        raise ShopifyApiError(str(data["errors"]))
    if "data" not in data:
        raise ShopifyApiError(f"예상치 못한 Shopify 응답 (HTTP {r.status_code}): {data}")
    return data["data"]


def get_product_by_handle(shop, token, handle):
    query = """
    query($handle: String!) {
      productByHandle(handle: $handle) {
        id
        title
        handle
        images(first: 50) { edges { node { id } } }
      }
    }
    """
    data = _gql(shop, token, query, {"handle": handle})
    product = data.get("productByHandle")
    if not product:
        raise ShopifyNotFoundError(f"핸들 '{handle}'에 해당하는 상품을 찾지 못했습니다.")
    return product


def duplicate_product(shop, token, product_gid, new_title):
    query = """
    mutation($productId: ID!, $newTitle: String!) {
      productDuplicate(productId: $productId, newTitle: $newTitle, includeImages: true, newStatus: DRAFT) {
        newProduct { id title handle images(first: 50) { edges { node { id } } } }
        userErrors { field message }
      }
    }
    """
    data = _gql(shop, token, query, {"productId": product_gid, "newTitle": new_title})
    result = data["productDuplicate"]
    if result["userErrors"]:
        raise ShopifyApiError("; ".join(e["message"] for e in result["userErrors"]))
    return result["newProduct"]


def update_product(shop, token, product_gid, *, handle=None, tags=None,
                    template_suffix=None, amazon_link=None, status=None):
    fields = {"id": product_gid}
    if handle:
        fields["handle"] = handle
    if tags is not None:
        fields["tags"] = tags
    if template_suffix:
        fields["templateSuffix"] = template_suffix
    if status:
        fields["status"] = status
    metafields = []
    if amazon_link:
        metafields.append({
            "namespace": "custom", "key": "amazon_link",
            "value": amazon_link, "type": "url",
        })
    if metafields:
        fields["metafields"] = metafields

    query = """
    mutation($input: ProductInput!) {
      productUpdate(input: $input) {
        product { id handle }
        userErrors { field message }
      }
    }
    """
    data = _gql(shop, token, query, {"input": fields})
    result = data["productUpdate"]
    if result["userErrors"]:
        raise ShopifyApiError("; ".join(e["message"] for e in result["userErrors"]))
    return result["product"]


STATUS_LABELS = {"ACTIVE": "활성", "DRAFT": "초안", "UNLISTED": "미게시"}

PRODUCT_SEARCH_QUERY = """
query($query: String, $first: Int!) {
  products(first: $first, query: $query, sortKey: UPDATED_AT, reverse: true) {
    edges { node {
      id title handle status tags templateSuffix
      onlineStorePreviewUrl
      featuredImage { url }
      media(first: 50) {
        edges { node { id alt ... on MediaImage { image { url } } } }
      }
    } }
  }
}
"""

PRODUCTS_BY_IDS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product {
      id title handle status tags templateSuffix descriptionHtml onlineStorePreviewUrl
      featuredImage { url }
      media(first: 50) {
        edges { node { id alt ... on MediaImage { image { url } } } }
      }
    }
  }
}
"""

PRODUCT_UPDATE_MUTATION = """
mutation($input: ProductInput!) {
  productUpdate(input: $input) {
    product { id }
    userErrors { field message }
  }
}
"""

FIND_FILE_QUERY = """
query($query: String!) {
  files(first: 10, query: $query) {
    edges { node { id alt ... on MediaImage { image { url } } } }
  }
}
"""

RECENT_IMAGE_FILES_QUERY = """
query {
  files(first: 250, sortKey: CREATED_AT, reverse: true, query: "media_type:IMAGE") {
    edges { node { id alt ... on MediaImage { image { url } } } }
  }
}
"""

PRODUCT_CREATE_MEDIA_MUTATION = """
mutation($productId: ID!, $media: [CreateMediaInput!]!) {
  productCreateMedia(productId: $productId, media: $media) {
    media { id }
    mediaUserErrors { field message }
  }
}
"""

PRODUCT_REORDER_MEDIA_MUTATION = """
mutation($id: ID!, $moves: [MoveInput!]!) {
  productReorderMedia(id: $id, moves: $moves) {
    mediaUserErrors { field message }
  }
}
"""

PRODUCT_DELETE_MEDIA_MUTATION = """
mutation($mediaIds: [ID!]!, $productId: ID!) {
  productDeleteMedia(mediaIds: $mediaIds, productId: $productId) {
    deletedMediaIds
    mediaUserErrors { field message }
  }
}
"""


def _normalize_product(product):
    product = dict(product)
    edges = (product.get("media") or {}).get("edges", [])
    product["media"] = [
        {
            "id": edge["node"]["id"],
            "alt": edge["node"].get("alt"),
            "url": (edge["node"].get("image") or {}).get("url"),
        }
        for edge in edges
    ]
    return product


def _base_filename(url):
    if not url:
        return ""
    filename = urlparse(url).path.rsplit("/", 1)[-1]
    stem = re.sub(r"\.[A-Za-z0-9]+$", "", filename)
    return re.sub(
        r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
        "", stem, flags=re.I,
    )


def media_display_name(media):
    return (media.get("alt") or "").strip() or _base_filename(media.get("url"))


def _map_file_node(node):
    url = (node.get("image") or {}).get("url")
    return {
        "id": node.get("id"), "alt": node.get("alt"), "url": url,
        "displayName": (node.get("alt") or "").strip() or _base_filename(url),
    }


def search_files(shop, token, query):
    # Run Shopify's filename search and the alt-text fallback concurrently so
    # type-ahead results stay responsive in the media editor.
    with ThreadPoolExecutor(max_workers=2) as executor:
        filename_future = executor.submit(_gql, shop, token, FIND_FILE_QUERY, {"query": query})
        recent_future = executor.submit(_gql, shop, token, RECENT_IMAGE_FILES_QUERY)
        by_filename = filename_future.result()
        recent = recent_future.result()
    needle = query.strip().lower()
    matches = [_map_file_node(e["node"]) for e in by_filename["files"]["edges"]]
    recent_files = [_map_file_node(e["node"]) for e in recent["files"]["edges"]]
    alt_matches = [
        file for file in recent_files
        if needle in ((file["displayName"] or "").lower())
    ]
    seen = {item["id"] for item in matches}
    matches.extend(item for item in alt_matches if item["id"] not in seen)
    return matches


def _find_file_url(shop, token, names, cache):
    for name in names:
        if name not in cache:
            files = search_files(shop, token, name)
            exact = next((f for f in files if f["displayName"] == name), None)
            cache[name] = (exact or (files[0] if files else {})).get("url")
        if cache[name]:
            return cache[name]
    return None


def parse_include_exclude(raw):
    """Splits a comma-separated condition value into (include, exclude) lists —
    a value prefixed with '*' means "must NOT contain this" instead of "must
    contain this". Mirrors the same syntax in the standalone shopify-editor
    Node app's 조건 검색 (e.g. "Amazon, *UK")."""
    include, exclude = [], []
    for piece in (raw or "").split(","):
        trimmed = piece.strip()
        if not trimmed:
            continue
        if trimmed.startswith("*"):
            value = trimmed[1:].strip().lower()
            if value:
                exclude.append(value)
        else:
            include.append(trimmed.lower())
    return include, exclude


def extract_handle(value):
    """Accepts either a bare handle or a full product URL and returns just
    the handle (the last path segment, query string stripped)."""
    v = (value or "").strip()
    if not v:
        return ""
    if "://" in v:
        v = v.split("?", 1)[0].rstrip("/")
        v = v.rsplit("/", 1)[-1]
    return v


def search_products(shop, token, conditions):
    """conditions: {title, tags, template, statuses: [...], handles: [...]}.
    status/handle are exact values so they're safe to filter with Shopify's
    own search syntax server-side; title/tags/template use substring
    "포함" matching (plus '*'-exclusion) so they're always filtered here in
    Python after a broad fetch. That fetch is capped at the first 250
    products (most-recently-updated first) — smaller than the standalone
    shopify-editor Node app's 20-page/5000 cap, so a condition matching more
    than 250 recently-updated products may miss older matches."""
    query_parts = []
    statuses = conditions.get("statuses") or []
    if statuses:
        query_parts.append("(" + " OR ".join(f"status:{s}" for s in statuses) + ")")
    handles = [extract_handle(h) for h in (conditions.get("handles") or [])]
    handles = [h for h in handles if h]
    if handles:
        query_parts.append("(" + " OR ".join(f"handle:'{h}'" for h in handles) + ")")
    query_str = " AND ".join(query_parts) or None

    data = _gql(shop, token, PRODUCT_SEARCH_QUERY, {"query": query_str, "first": 250})
    products = [_normalize_product(edge["node"]) for edge in data["products"]["edges"]]

    title_include, title_exclude = parse_include_exclude(conditions.get("title"))
    if title_include or title_exclude:
        def title_ok(p):
            t = p["title"].lower()
            return all(n in t for n in title_include) and all(n not in t for n in title_exclude)
        products = [p for p in products if title_ok(p)]

    tags_include, tags_exclude = parse_include_exclude(conditions.get("tags"))
    if tags_include or tags_exclude:
        def tags_ok(p):
            ptags = [t.lower() for t in (p.get("tags") or [])]
            include_ok = any(any(n in t for t in ptags) for n in tags_include) if tags_include else True
            exclude_ok = all(not any(n in t for t in ptags) for n in tags_exclude)
            return include_ok and exclude_ok
        products = [p for p in products if tags_ok(p)]

    tmpl_include, tmpl_exclude = parse_include_exclude(conditions.get("template"))
    if tmpl_include or tmpl_exclude:
        def tmpl_ok(p):
            t = (p.get("templateSuffix") or "").lower()
            return all(n in t for n in tmpl_include) and all(n not in t for n in tmpl_exclude)
        products = [p for p in products if tmpl_ok(p)]

    return products


def fetch_products_by_ids(shop, token, ids):
    """Re-fetches live product state right before preview/apply — never
    trusts the search-time snapshot, since other edits may have landed on
    the product in between."""
    data = _gql(shop, token, PRODUCTS_BY_IDS_QUERY, {"ids": ids})
    return [_normalize_product(n) for n in data["nodes"] if n]


def _compute_tag_change(current_tags, mods):
    """Returns (new_tags, changed) after applying mods['tagMode'] — mirrors
    the Node shopify-editor's 추가(add)/교체(replace)/삭제(remove) semantics,
    including the literal "전체" keyword meaning "replace the whole list"."""
    tags = list(current_tags or [])
    mode = mods.get("tagMode")
    if mode == "add":
        for t in [x.strip() for x in (mods.get("tagsValue") or "").split(",") if x.strip()]:
            if t not in tags:
                tags.append(t)
    elif mode == "replace":
        old_raw = (mods.get("tagsOld") or "").strip()
        new_raw = mods.get("tagsNew") or ""
        if old_raw == "전체":
            tags = [t.strip() for t in new_raw.split(",") if t.strip()]
        else:
            olds = [t.strip() for t in old_raw.split(",") if t.strip()]
            news = [t.strip() for t in new_raw.split(",") if t.strip()]
            for old, new in zip(olds, news):
                if old in tags:
                    tags[tags.index(old)] = new
    elif mode == "remove":
        for t in [x.strip() for x in (mods.get("tagsValue") or "").split(",") if x.strip()]:
            if t in tags:
                tags.remove(t)
    return tags, (tags != list(current_tags or []))


def _split_values(value):
    return [piece.strip() for piece in str(value or "").split(",") if piece.strip()]


def _expand_media_ops(media_ops):
    expanded = []
    for op in media_ops or []:
        if op.get("mode") == "move":
            expanded.append(dict(op))
            continue
        infos = _split_values(op.get("info"))
        orders = _split_values(op.get("order"))
        new_infos = _split_values(op.get("newInfo"))
        count = max(len(infos), len(orders), len(new_infos), 1)
        for index in range(count):
            expanded.append({
                "mode": op.get("mode"),
                "info": infos[index] if index < len(infos) else "",
                "order": orders[index] if index < len(orders) else "",
                "moveTo": op.get("moveTo") or "",
                "newInfo": new_infos[index] if index < len(new_infos) else "",
            })
    return expanded


def _to_position(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _derive_media_plan(shop, token, media, op, cache):
    mode = op.get("mode")
    info = (op.get("info") or "").strip()
    order = _to_position(op.get("order"))
    move_to = _to_position(op.get("moveTo"))

    if mode == "move":
        if not order or not move_to:
            return {"action": "error", "reason": "이동 순서 값이 올바르지 않음"}
        if order < 1 or order > len(media):
            return {"action": "skip", "reason": f"{order}번 위치에 이미지가 없음"}
        item = media[order - 1]
        if info and media_display_name(item) != info:
            return {"action": "skip", "reason": f"{order}번 위치의 이미지가 다름 (실제: '{media_display_name(item) or '제목 없음'}')"}
        if move_to < 1 or move_to > len(media):
            return {"action": "skip", "reason": f"{move_to}번은 잘못된 위치 (전체 {len(media)}개)"}
        if order == move_to:
            return {"action": "skip", "reason": f"이미 {move_to}번 위치 (변경 없음)"}
        next_media = list(media)
        moved = next_media.pop(order - 1)
        next_media.insert(move_to - 1, moved)
        return {
            "action": "apply", "reason": f"이미지 순서 변경: {order}번 → {move_to}번",
            "next_media": next_media,
            "mutation": {"kind": "move", "media_id": item["id"], "position": move_to},
        }

    if mode == "delete":
        index = None
        if order:
            if order < 1 or order > len(media):
                return {"action": "skip", "reason": f"{order}번 위치에 이미지가 없음"}
            index = order - 1
            if info and media_display_name(media[index]) != info:
                return {"action": "skip", "reason": f"{order}번 위치의 이미지가 다름 (실제: '{media_display_name(media[index]) or '제목 없음'}')"}
        elif info:
            index = next((i for i, item in enumerate(media) if media_display_name(item) == info), None)
            if index is None:
                return {"action": "skip", "reason": f"삭제할 이미지를 찾지 못함: '{info}'"}
        else:
            return {"action": "error", "reason": "삭제할 이미지 또는 순서를 입력해주세요"}
        target = media[index]
        next_media = list(media)
        next_media.pop(index)
        return {
            "action": "apply", "reason": f"이미지 삭제: {order}번" if order else f"이미지 삭제: '{info}'",
            "next_media": next_media,
            "mutation": {"kind": "delete", "media_id": target["id"]},
        }

    if mode == "overwrite":
        new_info = (op.get("newInfo") or "").strip()
        if not info or not new_info:
            return {"action": "error", "reason": "교체할 기존 이미지와 새 이미지를 입력해주세요"}
        if order:
            if order < 1 or order > len(media):
                return {"action": "skip", "reason": f"{order}번 위치에 이미지가 없음"}
            index = order - 1
            if media_display_name(media[index]) != info:
                return {"action": "skip", "reason": f"{order}번 위치의 이미지가 다름 (실제: '{media_display_name(media[index]) or '제목 없음'}')"}
        else:
            index = next((i for i, item in enumerate(media) if media_display_name(item) == info), None)
            if index is None:
                return {"action": "skip", "reason": f"교체할 이미지를 찾지 못함: '{info}'"}
        if any(media_display_name(item) == new_info for item in media):
            return {"action": "skip", "reason": f"이미 등록된 이미지: '{new_info}'"}
        new_url = _find_file_url(shop, token, [new_info], cache)
        if not new_url:
            return {"action": "error", "reason": f"쇼피파이 파일에서 이미지를 찾지 못함: '{new_info}'"}
        pending = {"id": "__pending__", "alt": new_info, "url": new_url}
        next_media = list(media)
        old_id = next_media[index]["id"]
        next_media[index] = pending
        return {
            "action": "apply", "reason": f"이미지 교체: '{info}' → '{new_info}' ({index + 1}번)",
            "next_media": next_media,
            "mutation": {"kind": "replace", "url": new_url, "alt": new_info, "position": index + 1, "old_id": old_id},
        }

    if mode == "insert":
        if not info:
            return {"action": "error", "reason": "추가할 이미지를 입력해주세요"}
        if any(media_display_name(item) == info for item in media):
            return {"action": "skip", "reason": f"이미 등록된 이미지: '{info}'"}
        url = _find_file_url(shop, token, [info], cache)
        if not url:
            return {"action": "error", "reason": f"쇼피파이 파일에서 이미지를 찾지 못함: '{info}'"}
        position = max(1, min(len(media) + 1, order or len(media) + 1))
        pending = {"id": "__pending__", "alt": info, "url": url}
        next_media = list(media)
        next_media.insert(position - 1, pending)
        return {
            "action": "apply", "reason": f"이미지 추가: '{info}' ({position}번)",
            "next_media": next_media,
            "mutation": {"kind": "insert", "url": url, "alt": info, "position": position},
        }

    return {"action": "error", "reason": "이미지 수정 방식을 선택해주세요"}


def _media_snapshot(media):
    return [
        {"url": item.get("url"), "name": media_display_name(item)}
        for item in media
    ]


def _preview_media_ops(shop, token, product, media_ops):
    media = list(product.get("media") or [])
    before = _media_snapshot(media)
    parts = []
    cache = {}
    for op in _expand_media_ops(media_ops):
        plan = _derive_media_plan(shop, token, media, op, cache)
        parts.append({"field": "media", "action": plan["action"], "reason": plan["reason"]})
        if plan["action"] == "apply":
            media = plan["next_media"]
    return parts, {"before": before, "after": _media_snapshot(media)}


def _media_errors(result, key):
    errors = result[key].get("mediaUserErrors") or []
    if errors:
        raise ShopifyApiError("; ".join(error["message"] for error in errors))


def _execute_media_mutation(shop, token, product_id, mutation):
    kind = mutation["kind"]
    if kind == "move":
        result = _gql(shop, token, PRODUCT_REORDER_MEDIA_MUTATION, {
            "id": product_id,
            "moves": [{"id": mutation["media_id"], "newPosition": str(mutation["position"] - 1)}],
        })
        _media_errors(result, "productReorderMedia")
        return None
    if kind == "delete":
        result = _gql(shop, token, PRODUCT_DELETE_MEDIA_MUTATION, {
            "productId": product_id, "mediaIds": [mutation["media_id"]],
        })
        _media_errors(result, "productDeleteMedia")
        return None

    result = _gql(shop, token, PRODUCT_CREATE_MEDIA_MUTATION, {
        "productId": product_id,
        "media": [{
            "originalSource": mutation["url"],
            "mediaContentType": "IMAGE",
            "alt": mutation["alt"],
        }],
    })
    _media_errors(result, "productCreateMedia")
    new_id = result["productCreateMedia"]["media"][0]["id"]
    reordered = _gql(shop, token, PRODUCT_REORDER_MEDIA_MUTATION, {
        "id": product_id,
        "moves": [{"id": new_id, "newPosition": str(mutation["position"] - 1)}],
    })
    _media_errors(reordered, "productReorderMedia")
    if kind == "replace":
        deleted = _gql(shop, token, PRODUCT_DELETE_MEDIA_MUTATION, {
            "productId": product_id, "mediaIds": [mutation["old_id"]],
        })
        _media_errors(deleted, "productDeleteMedia")
    return new_id


def _apply_media_ops(shop, token, product, media_ops):
    media = list(product.get("media") or [])
    parts = []
    cache = {}
    for op in _expand_media_ops(media_ops):
        plan = _derive_media_plan(shop, token, media, op, cache)
        part = {"field": "media", "action": plan["action"], "reason": plan["reason"]}
        if plan["action"] == "apply":
            try:
                new_id = _execute_media_mutation(shop, token, product["id"], plan["mutation"])
                media = plan["next_media"]
                if new_id:
                    for item in media:
                        if item["id"] == "__pending__":
                            item["id"] = new_id
                            break
            except ShopifyApiError as error:
                part = {"field": "media", "action": "error", "reason": str(error)}
        parts.append(part)
    return parts


def _overall(parts):
    if any(part["action"] == "error" for part in parts):
        return "error"
    if any(part["action"] == "apply" for part in parts):
        return "apply"
    return "skip"


def evaluate_modifications(product, mods, shop=None, token=None):
    """Computes what would happen to `product` under `mods` without calling
    the API — used for both the step-3 preview and (as the first half of)
    the actual apply. Each field that's a no-op is marked 'skip' with a
    reason instead of being silently sent to Shopify, matching the Node
    app's 건너뜀 behavior (e.g. a title identical to the current one)."""
    parts = []
    detail = {}

    title_val = (mods.get("title") or "").strip()
    if title_val:
        if title_val == product.get("title"):
            parts.append({"field": "title", "action": "skip", "reason": "제목 변경 (변경 없음)"})
        else:
            parts.append({"field": "title", "action": "apply", "reason": "제목 변경"})
            detail["title"] = {"before": product.get("title"), "after": title_val}

    desc_val = mods.get("description")
    if desc_val is not None and desc_val.strip():
        if desc_val == (product.get("descriptionHtml") or ""):
            parts.append({"field": "description", "action": "skip", "reason": "설명 변경 (변경 없음)"})
        else:
            parts.append({"field": "description", "action": "apply", "reason": "설명 변경"})
            detail["description"] = {"before": product.get("descriptionHtml") or "", "after": desc_val}

    if mods.get("tagMode"):
        new_tags, changed = _compute_tag_change(product.get("tags"), mods)
        if changed:
            parts.append({"field": "tags", "action": "apply", "reason": "태그 변경"})
            detail["tags"] = {"before": product.get("tags") or [], "after": new_tags}
        else:
            parts.append({"field": "tags", "action": "skip", "reason": "태그 변경 (변경 없음 또는 대상 없음)"})

    if mods.get("mediaOps"):
        if not shop or not token:
            parts.append({"field": "media", "action": "error", "reason": "Shopify 연결 정보가 없습니다."})
        else:
            media_parts, media_detail = _preview_media_ops(shop, token, product, mods["mediaOps"])
            parts.extend(media_parts)
            if any(part["action"] == "apply" for part in media_parts):
                detail["media"] = media_detail

    if not parts:
        return {"overall": "skip", "reason": "적용할 수정사항 없음", "parts": [], "detail": {}}
    overall = _overall(parts)
    return {"overall": overall, "parts": parts, "detail": detail}


def apply_modifications(shop, token, product, mods):
    """Runs evaluate_modifications, and if it decided anything should
    actually change, sends one productUpdate mutation. Returns the same
    shape as evaluate_modifications, with overall possibly promoted to
    'error' if Shopify rejects the mutation."""
    plan = evaluate_modifications(product, mods, shop, token)
    if plan["overall"] == "error":
        return plan
    fields = {"id": product["id"]}
    if "title" in plan["detail"]:
        fields["title"] = plan["detail"]["title"]["after"]
    if "description" in plan["detail"]:
        fields["descriptionHtml"] = plan["detail"]["description"]["after"]
    if "tags" in plan["detail"]:
        fields["tags"] = plan["detail"]["tags"]["after"]
    if len(fields) > 1:
        data = _gql(shop, token, PRODUCT_UPDATE_MUTATION, {"input": fields})
        result = data["productUpdate"]
        if result["userErrors"]:
            plan["overall"] = "error"
            plan["error"] = "; ".join(e["message"] for e in result["userErrors"])
            return plan

    non_media_parts = [part for part in plan["parts"] if part.get("field") != "media"]
    media_parts = _apply_media_ops(shop, token, product, mods.get("mediaOps") or [])
    plan["parts"] = non_media_parts + media_parts
    plan["overall"] = _overall(plan["parts"])
    if plan["overall"] == "error":
        plan["error"] = "; ".join(
            part["reason"] for part in plan["parts"] if part["action"] == "error"
        )
    return plan


PAGE_SEARCH_QUERY = """
query($query: String, $first: Int!) {
  pages(first: $first, query: $query, sortKey: UPDATED_AT, reverse: true) {
    edges { node {
      id title handle isPublished templateSuffix
      metafields(first: 20) { edges { node { namespace key type value } } }
    } }
  }
}
"""

PAGES_BY_IDS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Page {
      id title handle isPublished templateSuffix
      metafields(first: 20) { edges { node { namespace key type value } } }
    }
  }
}
"""

PAGE_UPDATE_MUTATION = """
mutation($id: ID!, $page: PageUpdateInput!) {
  pageUpdate(id: $id, page: $page) {
    page { id }
    userErrors { field message }
  }
}
"""

MEDIA_IMAGES_BY_IDS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on MediaImage { id alt image { url } }
  }
}
"""


def _normalize_page(page):
    """Confirmed live against this store's metafield definitions (ownerType
    PAGE): custom.tag (single_line_text_field — one value, unlike a
    product's multi-value tags list), custom.pixel_content_name
    (single_line_text_field), custom.landing_images (list.file_reference —
    a JSON array of MediaImage gids), custom.amazon_url (url)."""
    page = dict(page)
    metafields = {
        edge["node"]["key"]: edge["node"]["value"]
        for edge in (page.get("metafields") or {}).get("edges", [])
        if edge["node"]["namespace"] == "custom"
    }
    page["tag"] = metafields.get("tag", "")
    page["pixel_content_name"] = metafields.get("pixel_content_name", "")
    try:
        page["landing_image_ids"] = json.loads(metafields["landing_images"]) if metafields.get("landing_images") else []
    except (TypeError, ValueError):
        page["landing_image_ids"] = []
    return page


def search_pages(shop, token, conditions):
    """conditions: {title, tag, pixel_content_name, handles}. handle is an
    exact value so it's safe to filter with Shopify's own search syntax
    server-side; title/tag/pixel_content_name use substring "포함" matching
    (plus '*'-exclusion) so they're always filtered here in Python after a
    broad fetch, same approach as search_products — capped at the first 250
    pages (most-recently-updated first)."""
    handles = [extract_handle(h) for h in (conditions.get("handles") or [])]
    handles = [h for h in handles if h]
    query_str = ("(" + " OR ".join(f"handle:'{h}'" for h in handles) + ")") if handles else None

    data = _gql(shop, token, PAGE_SEARCH_QUERY, {"query": query_str, "first": 250})
    pages = [_normalize_page(edge["node"]) for edge in data["pages"]["edges"]]

    title_include, title_exclude = parse_include_exclude(conditions.get("title"))
    if title_include or title_exclude:
        def title_ok(p):
            t = p["title"].lower()
            return all(n in t for n in title_include) and all(n not in t for n in title_exclude)
        pages = [p for p in pages if title_ok(p)]

    tag_include, tag_exclude = parse_include_exclude(conditions.get("tag"))
    if tag_include or tag_exclude:
        def tag_ok(p):
            t = (p.get("tag") or "").lower()
            return all(n in t for n in tag_include) and all(n not in t for n in tag_exclude)
        pages = [p for p in pages if tag_ok(p)]

    pcn_include, pcn_exclude = parse_include_exclude(conditions.get("pixel_content_name"))
    if pcn_include or pcn_exclude:
        def pcn_ok(p):
            t = (p.get("pixel_content_name") or "").lower()
            return all(n in t for n in pcn_include) and all(n not in t for n in pcn_exclude)
        pages = [p for p in pages if pcn_ok(p)]

    return pages


def fetch_pages_by_ids(shop, token, ids):
    """Re-fetches live page state right before preview/apply — never trusts
    the search-time snapshot, mirroring fetch_products_by_ids."""
    data = _gql(shop, token, PAGES_BY_IDS_QUERY, {"ids": ids})
    return [_normalize_page(n) for n in data["nodes"] if n]


def _page_media_list(shop, token, page):
    """Resolves a page's custom.landing_images gid list into the same shape
    _derive_media_plan already expects ({id, alt, url} dicts) — media_display_name
    then falls back to the url's filename when a MediaImage has no alt text,
    same as the product media editor."""
    ids = page.get("landing_image_ids") or []
    if not ids:
        return []
    data = _gql(shop, token, MEDIA_IMAGES_BY_IDS_QUERY, {"ids": ids})
    nodes_by_id = {n["id"]: n for n in data["nodes"] if n}
    result = []
    for gid in ids:
        node = nodes_by_id.get(gid)
        if not node:
            continue  # the file was deleted from Shopify's library since being linked here
        result.append({"id": gid, "alt": node.get("alt"), "url": (node.get("image") or {}).get("url")})
    return result


def evaluate_page_modifications(page, mods, shop=None, token=None):
    """Page counterpart of evaluate_modifications. Narrower than the product
    version by design: no description/상태 editing, and 태그 is a single
    custom.tag value replaced outright (mods['tagMode'] == 'replace' with
    mods['tagsNew']) rather than the product's add/replace/remove-against-a-
    list semantics. Media still edits via the same insert/replace/delete/move
    vocabulary as products, just applied to the custom.landing_images gid
    list instead of native product media."""
    parts = []
    detail = {}

    title_val = (mods.get("title") or "").strip()
    if title_val:
        if title_val == page.get("title"):
            parts.append({"field": "title", "action": "skip", "reason": "제목 변경 (변경 없음)"})
        else:
            parts.append({"field": "title", "action": "apply", "reason": "제목 변경"})
            detail["title"] = {"before": page.get("title"), "after": title_val}

    if mods.get("tagMode") == "replace":
        new_tag = (mods.get("tagsNew") or "").strip()
        current_tag = page.get("tag") or ""
        if new_tag and new_tag != current_tag:
            parts.append({"field": "tag", "action": "apply", "reason": "태그 변경"})
            detail["tag"] = {"before": current_tag, "after": new_tag}
        else:
            parts.append({"field": "tag", "action": "skip", "reason": "태그 변경 (변경 없음 또는 입력 없음)"})

    if mods.get("mediaOps"):
        if not shop or not token:
            parts.append({"field": "media", "action": "error", "reason": "Shopify 연결 정보가 없습니다."})
        else:
            media = _page_media_list(shop, token, page)
            before = _media_snapshot(media)
            cache = {}
            media_parts = []
            for op in _expand_media_ops(mods["mediaOps"]):
                plan = _derive_media_plan(shop, token, media, op, cache)
                media_parts.append({"field": "media", "action": plan["action"], "reason": plan["reason"]})
                if plan["action"] == "apply":
                    media = plan["next_media"]
            parts.extend(media_parts)
            if any(part["action"] == "apply" for part in media_parts):
                detail["media"] = {"before": before, "after": _media_snapshot(media)}
                detail["landing_image_ids"] = [item["id"] for item in media]

    if not parts:
        return {"overall": "skip", "reason": "적용할 수정사항 없음", "parts": [], "detail": {}}
    overall = _overall(parts)
    return {"overall": overall, "parts": parts, "detail": detail}


def apply_page_modifications(shop, token, page, mods):
    """Runs evaluate_page_modifications, and if it decided anything should
    actually change, sends one pageUpdate mutation (title + both metafields
    together — no incremental per-op media mutations needed, since the whole
    landing_images list is just written out once at the end)."""
    plan = evaluate_page_modifications(page, mods, shop, token)
    if plan["overall"] == "error":
        return plan

    page_fields = {}
    if "title" in plan["detail"]:
        page_fields["title"] = plan["detail"]["title"]["after"]
    metafields = []
    if "tag" in plan["detail"]:
        metafields.append({"namespace": "custom", "key": "tag", "type": "single_line_text_field",
                           "value": plan["detail"]["tag"]["after"]})
    if "landing_image_ids" in plan["detail"]:
        metafields.append({"namespace": "custom", "key": "landing_images", "type": "list.file_reference",
                           "value": json.dumps(plan["detail"]["landing_image_ids"])})
    if metafields:
        page_fields["metafields"] = metafields

    if page_fields:
        data = _gql(shop, token, PAGE_UPDATE_MUTATION, {"id": page["id"], "page": page_fields})
        result = data["pageUpdate"]
        if result["userErrors"]:
            plan["overall"] = "error"
            plan["error"] = "; ".join(e["message"] for e in result["userErrors"])
    return plan


def get_page_by_tag(shop, token, tag_value):
    """Finds the Page (shop.pages, i.e. /pages/<handle> — not a product) whose
    custom.tag metafield equals tag_value exactly. Confirmed live against this
    store's metafield definitions: Pages have their own custom.tag
    (single_line_text_field) and custom.amazon_url (url) metafields, distinct
    from the Product metafields used by create_bridge_page below."""
    query = """
    query($search: String!) {
      pages(first: 5, query: $search) {
        edges {
          node {
            id
            handle
            title
            body
            templateSuffix
            isPublished
            metafields(first: 20) {
              edges { node { namespace key type value } }
            }
          }
        }
      }
    }
    """
    search = f"metafield:custom.tag:{tag_value}"
    data = _gql(shop, token, query, {"search": search})
    edges = data["pages"]["edges"]
    if not edges:
        raise ShopifyNotFoundError(f"태그 '{tag_value}'에 해당하는 Shopify 페이지를 찾지 못했습니다.")
    return edges[0]["node"]


def create_page_bridge(shop, token, *, source_tag, new_handle, new_tag,
                        amazon_url_override=None, storefront_domain=None):
    """Duplicates a Shopify Page found by its custom.tag metafield. Shopify's
    Admin API has no pageDuplicate mutation (unlike productDuplicate), so this
    copies the source page's content into a new one via pageCreate, then sets
    the new page's handle + custom.tag metafield to new_tag (always kept in
    sync with each other, matching how the ad's own website URL embeds this
    same identifier) and its custom.amazon_url metafield to the attribution
    link. Every other metafield on the source (pixel content name, landing
    images, SEO-hide, etc.) is carried over unchanged."""
    source = get_page_by_tag(shop, token, source_tag)

    metafields = []
    for edge in source["metafields"]["edges"]:
        mf = edge["node"]
        if mf["namespace"] == "custom" and mf["key"] == "tag":
            value = new_tag
        elif mf["namespace"] == "custom" and mf["key"] == "amazon_url" and amazon_url_override:
            value = amazon_url_override
        else:
            value = mf["value"]
        metafields.append({"namespace": mf["namespace"], "key": mf["key"], "value": value, "type": mf["type"]})

    present = {(m["namespace"], m["key"]) for m in metafields}
    if ("custom", "tag") not in present:
        metafields.append({"namespace": "custom", "key": "tag", "value": new_tag, "type": "single_line_text_field"})
    if amazon_url_override and ("custom", "amazon_url") not in present:
        metafields.append({"namespace": "custom", "key": "amazon_url", "value": amazon_url_override, "type": "url"})

    query = """
    mutation($page: PageCreateInput!) {
      pageCreate(page: $page) {
        page { id handle title }
        userErrors { field message }
      }
    }
    """
    variables = {"page": {
        "title": source["title"],
        "handle": new_handle,
        "body": source["body"],
        "isPublished": source["isPublished"],
        "templateSuffix": source["templateSuffix"],
        "metafields": metafields,
    }}
    data = _gql(shop, token, query, variables)
    result = data["pageCreate"]
    if result["userErrors"]:
        raise ShopifyApiError("; ".join(e["message"] for e in result["userErrors"]))
    new_page = result["page"]

    domain = storefront_domain or shop
    return {
        "page_id": new_page["id"],
        "handle": new_page["handle"],
        "url": f"https://{domain}/pages/{new_page['handle']}",
    }


def create_bridge_page(shop, token, *, source_handle, new_handle, title_override=None,
                        tags_override=None, template_override=None,
                        amazon_link_override=None, storefront_domain=None):
    """Duplicate source_handle's product as an UNLISTED page, rehandled to
    new_handle. Raises ShopifyApiError if the source is missing or the image
    count doesn't match after duplication (a sign the copy is incomplete)."""
    source = get_product_by_handle(shop, token, source_handle)
    source_image_count = len(source["images"]["edges"])

    new_title = title_override or source["title"]
    new_product = duplicate_product(shop, token, source["id"], new_title)
    new_image_count = len(new_product["images"]["edges"])
    if new_image_count != source_image_count:
        raise ShopifyApiError(
            f"이미지 복제 불일치: 원본 {source_image_count}장, 복제본 {new_image_count}장 — "
            f"Shopify 관리자에서 상품 '{new_product['handle']}'을 직접 확인하세요."
        )

    tags = None
    if tags_override:
        tags = [t.strip() for t in tags_override.split(",") if t.strip()]

    updated = update_product(
        shop, token, new_product["id"],
        handle=new_handle,
        tags=tags,
        template_suffix=template_override,
        amazon_link=amazon_link_override,
        # Confirmed against this store's live schema: ProductStatus.UNLISTED
        # ("active but needs a direct link; excluded from search/collections")
        # is exactly the bridge-page behavior wanted. Only available from API
        # version 2025-10 onward (older versions silently coerce it to ACTIVE).
        status="UNLISTED",
    )

    # Fall back to the full *.myshopify.com domain (always a valid, resolvable
    # storefront URL) rather than guessing at a custom domain by stripping
    # ".myshopify.com" — that would drop the TLD entirely (e.g. "teststore"
    # instead of "teststore.myshopify.com" or "teststore.com").
    domain = storefront_domain or shop
    return {
        "product_id": updated["id"],
        "handle": updated["handle"],
        "url": f"https://{domain}/products/{updated['handle']}",
        "image_count": new_image_count,
    }


def delete_product(shop, token, product_id):
    """Permanently deletes a bridge product — used to roll back a row whose
    Meta ad was created but something else in the same row failed, so the
    whole thing can be undone instead of leaving an orphaned duplicate."""
    query = """
    mutation($input: ProductDeleteInput!) {
      productDelete(input: $input) {
        deletedProductId
        userErrors { field message }
      }
    }
    """
    data = _gql(shop, token, query, {"input": {"id": product_id}})
    result = data["productDelete"]
    if result["userErrors"]:
        raise ShopifyApiError("; ".join(e["message"] for e in result["userErrors"]))
    return result["deletedProductId"]


def delete_page(shop, token, page_id):
    """Permanently deletes a bridge page — same rollback role as
    delete_product, for the page-duplication path."""
    query = """
    mutation($id: ID!) {
      pageDelete(id: $id) {
        deletedPageId
        userErrors { field message }
      }
    }
    """
    data = _gql(shop, token, query, {"id": page_id})
    result = data["pageDelete"]
    if result["userErrors"]:
        raise ShopifyApiError("; ".join(e["message"] for e in result["userErrors"]))
    return result["deletedPageId"]
