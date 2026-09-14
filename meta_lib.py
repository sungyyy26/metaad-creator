"""Shared Meta Marketing API helpers used by both the CLI script (duplicate_ad.py)
and the local web dashboard (webapp/app.py)."""
import json

import requests

GRAPH = "https://graph.facebook.com/v21.0"


class MetaApiError(RuntimeError):
    pass


def api(method, path, token, **params):
    params["access_token"] = token
    r = requests.request(method, f"{GRAPH}/{path}", params=params if method == "GET" else None,
                          data=None if method == "GET" else params)
    data = r.json()
    if "error" in data:
        raise MetaApiError(f"{method} {path} failed: {data['error'].get('message', data['error'])}")
    return data


def find_campaign_by_name(token, account_id, name):
    data = api("GET", f"act_{account_id}/campaigns", token, fields="id,name", limit=500)
    for campaign in data.get("data", []):
        if campaign["name"] == name:
            return campaign["id"]
    raise MetaApiError(f"'{name}' 이름의 캠페인을 act_{account_id} 계정에서 찾지 못했습니다.")


def resolve_campaign_id(token, account_id, campaign_id_or_name):
    """The Graph API only accepts numeric object IDs, but ad ops usually thinks
    in campaign names — so if this doesn't look like an ID, look it up by name
    within the given ad account."""
    if campaign_id_or_name.isdigit():
        return campaign_id_or_name
    return find_campaign_by_name(token, account_id, campaign_id_or_name)


def find_adset_by_name(token, campaign_id, name):
    data = api("GET", f"{campaign_id}/adsets", token,
               fields="id,name,targeting,optimization_goal,billing_event,"
                      "bid_strategy,promoted_object,destination_type,"
                      "start_time,end_time,daily_budget", limit=200)
    for adset in data.get("data", []):
        if adset["name"] == name:
            return adset
    raise MetaApiError(f"'{campaign_id}' 캠페인 안에서 '{name}' 이름의 광고 세트를 찾지 못했습니다.")


def find_template_ad(token, adset_id):
    data = api("GET", f"{adset_id}/ads", token,
               fields="id,name,creative{id,object_story_spec,call_to_action_type}",
               limit=5)
    ads = data.get("data", [])
    if not ads:
        raise MetaApiError(f"광고 세트 {adset_id} 안에 템플릿으로 쓸 광고가 없습니다.")
    return ads[0]


def find_creative_asset(token, account_id, name_query):
    for endpoint in ("advideos", "adimages"):
        data = api("GET", f"act_{account_id}/{endpoint}", token,
                   fields="id,name,hash" if endpoint == "adimages" else "id,title",
                   limit=100)
        for item in data.get("data", []):
            label = item.get("name") or item.get("title") or ""
            if name_query.lower() in label.lower():
                return endpoint, item
    raise MetaApiError(f"'{name_query}'와(과) 일치하는 영상/이미지를 act_{account_id} 라이브러리에서 찾지 못했습니다.")


def duplicate_ad(token, *, account_id, campaign_id, source_adset_name, new_adset_name,
                  new_ad_name, daily_budget, website_url, creative_name, headline,
                  primary_text, start_iso=None, end_iso=None, status="PAUSED"):
    """Clone source_adset_name's targeting into a new ad set, then create a new ad
    with a new creative inside it. Returns dict with the created object IDs."""
    campaign_id = resolve_campaign_id(token, account_id, campaign_id)
    source_adset = find_adset_by_name(token, campaign_id, source_adset_name)
    template_ad = find_template_ad(token, source_adset["id"])
    story_spec = template_ad["creative"]["object_story_spec"]
    page_id = story_spec.get("page_id")
    cta_type = template_ad["creative"].get("call_to_action_type", "SHOP_NOW")

    endpoint, asset = find_creative_asset(token, account_id, creative_name)

    adset_payload = dict(
        name=new_adset_name,
        campaign_id=campaign_id,
        daily_budget=daily_budget * 100,  # cents
        billing_event=source_adset["billing_event"],
        optimization_goal=source_adset["optimization_goal"],
        bid_strategy=source_adset.get("bid_strategy"),
        targeting=json.dumps(source_adset["targeting"]),
        status="PAUSED",
    )
    if start_iso:
        adset_payload["start_time"] = start_iso
    if end_iso:
        adset_payload["end_time"] = end_iso
    new_adset = api("POST", f"act_{account_id}/adsets", token, **adset_payload)

    link_data = {
        "link": website_url,
        "message": primary_text,
        "name": headline,
        "call_to_action": json.dumps({"type": cta_type, "value": {"link": website_url}}),
    }
    if endpoint == "advideos":
        link_data["video_id"] = asset["id"]
    else:
        link_data["image_hash"] = asset["hash"]
    new_story_spec = {"page_id": page_id, "link_data": link_data}
    creative = api("POST", f"act_{account_id}/adcreatives", token,
                   name=f"{new_ad_name} - creative",
                   object_story_spec=json.dumps(new_story_spec))

    ad = api("POST", f"act_{account_id}/ads", token,
             name=new_ad_name,
             adset_id=new_adset["id"],
             creative=json.dumps({"creative_id": creative["id"]}),
             status=status)

    return {
        "ad_set_id": new_adset["id"],
        "creative_id": creative["id"],
        "ad_id": ad["id"],
        "ads_manager_url": (
            f"https://www.facebook.com/adsmanager/manage/ads/edit?"
            f"act={account_id}&selected_ad_ids={ad['id']}"
        ),
    }
