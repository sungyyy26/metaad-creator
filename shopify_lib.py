"""Shopify Admin GraphQL helpers for creating the 'bridge page' (an UNLISTED
duplicate of a source product, retitled/rehandled for one ad) — mirrors the
Shopify step of the meta-ad-duplicator artifact's processing instructions."""
import os

import requests

# Shopify drops each API version roughly a year after release, so a hardcoded
# version here will eventually 404 with a bare "Not Found" body. Override with
# SHOPIFY_API_VERSION if this one has since been retired — check
# https://shopify.dev/docs/api/admin-graphql for the current supported list.
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2025-10")


class ShopifyApiError(RuntimeError):
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
        raise ShopifyApiError(f"핸들 '{handle}'에 해당하는 상품을 찾지 못했습니다.")
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
      featuredImage { url }
    } }
  }
}
"""

PRODUCTS_BY_IDS_QUERY = """
query($ids: [ID!]!) {
  nodes(ids: $ids) {
    ... on Product { id title handle status tags templateSuffix descriptionHtml }
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
    products = [edge["node"] for edge in data["products"]["edges"]]

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
    return [n for n in data["nodes"] if n]


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


def evaluate_modifications(product, mods):
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

    if not parts:
        return {"overall": "skip", "reason": "적용할 수정사항 없음", "parts": [], "detail": {}}
    overall = "apply" if any(p["action"] == "apply" for p in parts) else "skip"
    return {"overall": overall, "parts": parts, "detail": detail}


def apply_modifications(shop, token, product, mods):
    """Runs evaluate_modifications, and if it decided anything should
    actually change, sends one productUpdate mutation. Returns the same
    shape as evaluate_modifications, with overall possibly promoted to
    'error' if Shopify rejects the mutation."""
    plan = evaluate_modifications(product, mods)
    if plan["overall"] != "apply":
        return plan
    fields = {"id": product["id"]}
    if "title" in plan["detail"]:
        fields["title"] = plan["detail"]["title"]["after"]
    if "description" in plan["detail"]:
        fields["descriptionHtml"] = plan["detail"]["description"]["after"]
    if "tags" in plan["detail"]:
        fields["tags"] = plan["detail"]["tags"]["after"]
    data = _gql(shop, token, PRODUCT_UPDATE_MUTATION, {"input": fields})
    result = data["productUpdate"]
    if result["userErrors"]:
        plan["overall"] = "error"
        plan["error"] = "; ".join(e["message"] for e in result["userErrors"])
    return plan


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
