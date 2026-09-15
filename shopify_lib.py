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
