#!/usr/bin/env python3
"""
Duplicate a Meta ad set + ad via the Marketing API, run locally from your desktop.

Replicates the core logic of the "Ad Duplication Desk" artifact:
  1. Find the source ad set (by name) inside a campaign, read its targeting/
     optimization/placements.
  2. Read one ad inside that ad set as a template for Page, CTA, pixel event,
     format and creative structure.
  3. Find the creative asset (video/image) in the ad account's library by name.
  4. Create a new ad set (clone of source targeting) + a new ad (new copy/creative,
     same template settings), both PAUSED by default.

Requires: pip install requests
Auth: a Meta access token with ads_management on the ad account.
  Get one at https://developers.facebook.com/tools/explorer/ (System User
  token from Business Settings is recommended for anything beyond a quick test —
  user tokens expire in ~60 days).

Usage:
  export META_ACCESS_TOKEN=...
  python3 duplicate_ad.py \
    --account-id 1298298124998350 \
    --campaign-id 120210000000000000 \
    --source-adset-name "n-srm_ao_da_260619_handwrnk" \
    --new-adset-name "n-srm_ao_da_260912_handwrnk-winner_2hb" \
    --new-ad-name "260912_n-srm_ao_da_vd_hand-wrnk_jyj_2hb" \
    --daily-budget 2000 \
    --website-url "https://eqqualberryglobal.com/products/..." \
    --creative-name "hand-wrnk" \
    --headline "Finally, a fix" \
    --primary-text "..." \
    --status PAUSED
"""
import argparse
import sys

import requests

GRAPH = "https://graph.facebook.com/v21.0"


def api(method, path, token, **params):
    params["access_token"] = token
    r = requests.request(method, f"{GRAPH}/{path}", params=params if method == "GET" else None,
                          data=None if method == "GET" else params)
    data = r.json()
    if "error" in data:
        raise RuntimeError(f"{method} {path} failed: {data['error']}")
    return data


def find_adset_by_name(token, campaign_id, name):
    data = api("GET", f"{campaign_id}/adsets", token,
               fields="id,name,targeting,optimization_goal,billing_event,"
                      "bid_strategy,promoted_object,destination_type,"
                      "start_time,end_time,daily_budget", limit=200)
    for adset in data.get("data", []):
        if adset["name"] == name:
            return adset
    raise SystemExit(f"No ad set named '{name}' found in campaign {campaign_id}")


def find_template_ad(token, adset_id):
    data = api("GET", f"{adset_id}/ads", token,
               fields="id,name,creative{id,object_story_spec,call_to_action_type}",
               limit=5)
    ads = data.get("data", [])
    if not ads:
        raise SystemExit(f"Ad set {adset_id} has no ads to use as a template")
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
    raise SystemExit(f"No video/image asset matching '{name_query}' found in act_{account_id} library")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--account-id", required=True, help="Ad account ID, no 'act_' prefix")
    p.add_argument("--campaign-id", required=True)
    p.add_argument("--source-adset-name", required=True, help="Ad set to clone targeting/template from")
    p.add_argument("--new-adset-name", required=True)
    p.add_argument("--new-ad-name", required=True)
    p.add_argument("--daily-budget", type=int, required=True, help="USD/day, whole dollars")
    p.add_argument("--website-url", required=True)
    p.add_argument("--creative-name", required=True, help="Substring to match in the ad account's video/image library")
    p.add_argument("--headline", required=True)
    p.add_argument("--primary-text", required=True)
    p.add_argument("--start-iso", default=None, help='e.g. "2026-09-08T00:00:00" (account-local time, no timezone offset)')
    p.add_argument("--end-iso", default=None)
    p.add_argument("--status", default="PAUSED", choices=["PAUSED", "ACTIVE"])
    p.add_argument("--token", default=None, help="Falls back to $META_ACCESS_TOKEN")
    args = p.parse_args()

    import os
    token = args.token or os.environ.get("META_ACCESS_TOKEN")
    if not token:
        sys.exit("Set META_ACCESS_TOKEN or pass --token")

    source_adset = find_adset_by_name(token, args.campaign_id, args.source_adset_name)
    template_ad = find_template_ad(token, source_adset["id"])
    story_spec = template_ad["creative"]["object_story_spec"]
    page_id = story_spec.get("page_id")
    cta_type = template_ad["creative"].get("call_to_action_type", "SHOP_NOW")

    endpoint, asset = find_creative_asset(token, args.account_id, args.creative_name)

    # 1) new ad set, cloning targeting from the source
    adset_payload = dict(
        name=args.new_adset_name,
        campaign_id=args.campaign_id,
        daily_budget=args.daily_budget * 100,  # cents
        billing_event=source_adset["billing_event"],
        optimization_goal=source_adset["optimization_goal"],
        bid_strategy=source_adset.get("bid_strategy"),
        targeting=source_adset["targeting"],
        status="PAUSED",
    )
    if args.start_iso:
        adset_payload["start_time"] = args.start_iso
    if args.end_iso:
        adset_payload["end_time"] = args.end_iso
    import json
    adset_payload["targeting"] = json.dumps(adset_payload["targeting"])
    new_adset = api("POST", f"act_{args.account_id}/adsets", token, **adset_payload)
    print(f"Created ad set {new_adset['id']}")

    # 2) creative (video or image) + new copy
    link_data = {"link": args.website_url, "message": args.primary_text,
                 "name": args.headline, "call_to_action": json.dumps({"type": cta_type, "value": {"link": args.website_url}})}
    if endpoint == "advideos":
        link_data["video_id"] = asset["id"]
    else:
        link_data["image_hash"] = asset["hash"]
    new_story_spec = {"page_id": page_id, "link_data": link_data}
    creative = api("POST", f"act_{args.account_id}/adcreatives", token,
                   name=f"{args.new_ad_name} - creative",
                   object_story_spec=json.dumps(new_story_spec))
    print(f"Created creative {creative['id']}")

    # 3) ad
    ad = api("POST", f"act_{args.account_id}/ads", token,
             name=args.new_ad_name,
             adset_id=new_adset["id"],
             creative=json.dumps({"creative_id": creative["id"]}),
             status=args.status)
    print(f"Created ad {ad['id']}")
    print(f"Ads Manager: https://www.facebook.com/adsmanager/manage/ads/edit?act={args.account_id}&selected_ad_ids={ad['id']}")


if __name__ == "__main__":
    main()
