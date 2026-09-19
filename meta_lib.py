"""Shared Meta Marketing API helpers used by both the CLI script (duplicate_ad.py)
and the local web dashboard (webapp/app.py)."""
import json
import os
import time

import requests

GRAPH = "https://graph.facebook.com/v21.0"
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".m4v", ".webm", ".mkv"}

# Ad-account/app-level throttling codes Meta expects callers to back off and
# retry rather than treat as a hard failure (e.g. code 17 "User request limit
# reached", subcode 2446079 "Ad Account Has Too Many API Calls").
RATE_LIMIT_CODES = {4, 17, 32, 613}
RATE_LIMIT_RETRY_DELAYS = (30, 60, 120)  # seconds; gives up after these are exhausted


class MetaApiError(RuntimeError):
    pass


def _format_error(method, path, err):
    parts = [str(err.get("message", err))]
    for key in ("error_user_title", "error_user_msg"):
        if err.get(key) and err[key] not in parts:
            parts.append(err[key])
    detail = " — ".join(parts)
    tags = [f"{k}={err[k]}" for k in ("code", "error_subcode", "fbtrace_id") if err.get(k) is not None]
    if tags:
        detail += " (" + ", ".join(tags) + ")"
    return f"{method} {path} failed: {detail}"


def api(method, path, token, **params):
    params["access_token"] = token
    delays = (0,) + RATE_LIMIT_RETRY_DELAYS
    for attempt, delay in enumerate(delays):
        if delay:
            time.sleep(delay)
        r = requests.request(method, f"{GRAPH}/{path}", params=params if method == "GET" else None,
                              data=None if method == "GET" else params)
        data = r.json()
        if "error" not in data:
            return data
        err = data["error"]
        is_rate_limited = err.get("code") in RATE_LIMIT_CODES
        if is_rate_limited and attempt < len(delays) - 1:
            continue
        raise MetaApiError(_format_error(method, path, err))


def _cached(cache, key, fetch):
    """cache is a plain dict the caller can share across several duplicate_ad()
    calls in the same run (e.g. one bulk-upload batch) so repeated lookups of
    the same campaign/ad-set/creative-library hit the API once instead of once
    per row. None of these cached results are things this tool ever mutates
    mid-run (we don't create campaigns or creative assets, and ad sets created
    mid-run are tracked separately via `batch_adsets` — see duplicate_ad), so
    there's no staleness risk within a single run."""
    if cache is None:
        return fetch()
    if key not in cache:
        cache[key] = fetch()
    return cache[key]


def resolve_campaign_and_account(token, campaign_id_or_name, candidate_account_ids,
                                  account_override=None, cache=None):
    """Figures out both the numeric campaign_id and which ad account owns it.

    - If account_override is given, trust it and only look inside that account.
    - Else if campaign_id_or_name is already numeric, ask Meta which account
      owns that campaign directly (one call, no guessing).
    - Else (a campaign name, no override), search each candidate account's
      campaign list for an exact name match — this is what makes the bulk
      upload's "campaign" column work with just a name, matching how ad ops
      actually thinks, without requiring them to also specify the account.
    """
    if account_override:
        if campaign_id_or_name.isdigit():
            return account_override, campaign_id_or_name
        campaigns = _cached(cache, f"campaigns:{account_override}", lambda: api(
            "GET", f"act_{account_override}/campaigns", token, fields="id,name", limit=500,
        ).get("data", []))
        for c in campaigns:
            if c["name"] == campaign_id_or_name:
                return account_override, c["id"]
        raise MetaApiError(f"'{campaign_id_or_name}' 이름의 캠페인을 act_{account_override} 계정에서 찾지 못했습니다.")

    if campaign_id_or_name.isdigit():
        data = _cached(cache, f"campaign_owner:{campaign_id_or_name}", lambda: api(
            "GET", campaign_id_or_name, token, fields="account_id"))
        owner = str(data.get("account_id", "")).replace("act_", "")
        if not owner:
            raise MetaApiError(f"캠페인 {campaign_id_or_name}의 소유 광고 계정을 확인하지 못했습니다.")
        return owner, campaign_id_or_name

    for account_id in candidate_account_ids:
        campaigns = _cached(cache, f"campaigns:{account_id}", lambda account_id=account_id: api(
            "GET", f"act_{account_id}/campaigns", token, fields="id,name", limit=500,
        ).get("data", []))
        for c in campaigns:
            if c["name"] == campaign_id_or_name:
                return account_id, c["id"]
    tried = ", ".join(candidate_account_ids)
    raise MetaApiError(f"'{campaign_id_or_name}' 이름의 캠페인을 어느 계정에서도 찾지 못했습니다 (확인한 계정: {tried}).")


def _paginate(token, path, **params):
    """GETs `path` and follows Graph API cursor pagination until exhausted,
    returning every page's `data` entries concatenated. Reuses api()'s own
    rate-limit retry for every page rather than duplicating it."""
    items = []
    page_params = dict(params)
    while True:
        data = api("GET", path, token, **page_params)
        items.extend(data.get("data", []))
        paging = data.get("paging", {})
        after = paging.get("cursors", {}).get("after")
        if not paging.get("next") or not after:
            return items
        page_params["after"] = after


def list_campaigns(token, account_id, cache=None):
    """Every campaign (id, name) in an ad account — used by the budget-
    adjustment tab to search by keyword, unlike resolve_campaign_and_account's
    exact-name lookup."""
    return _cached(cache, f"all_campaigns:{account_id}", lambda: _paginate(
        token, f"act_{account_id}/campaigns", fields="id,name", limit=200))


def find_matching_campaigns(token, account_ids, keywords, exclusions=None, cache=None):
    """Campaign names containing every keyword and none of the exclusions."""
    needles = [k.strip().casefold() for k in keywords if k and k.strip()]
    blocked = [k.strip().casefold() for k in (exclusions or []) if k and k.strip()]
    matches = []
    for account_id in account_ids:
        for c in list_campaigns(token, account_id, cache=cache):
            name_lower = c["name"].casefold()
            if all(k in name_lower for k in needles) and not any(k in name_lower for k in blocked):
                matches.append({"id": c["id"], "name": c["name"], "account_id": account_id})
    return matches


def list_recent_ad_copies(token, account_ids, since, limit_per_account=500):
    """Fetch recent Meta ad names and creative copy for the local copy DB."""
    collected = []
    for account_id in account_ids:
        rows = api(
            "GET", f"act_{account_id}/ads", token,
            fields="id,name,created_time,updated_time,effective_status,"
                   "creative{id,object_story_spec,asset_feed_spec}",
            limit=limit_per_account,
        ).get("data", [])
        for row in rows:
            stamp = str(row.get("updated_time") or row.get("created_time") or "")[:10]
            if stamp and stamp >= since:
                collected.append(row)
    collected.sort(
        key=lambda row: row.get("updated_time") or row.get("created_time") or "",
        reverse=True,
    )
    return collected


def list_campaign_ads(token, campaign_id, cache=None):
    """Every ad in a campaign, with its ad set's id/name/daily_budget and the
    ad's own creative name, so a spreadsheet's 소재명 column can be matched
    against either the ad name or the creative name."""
    return _cached(cache, f"campaign_ads:{campaign_id}", lambda: _paginate(
        token, f"{campaign_id}/ads", limit=200,
        fields="id,name,adset{id,name,daily_budget},creative{name}"))


def list_campaign_adsets(token, campaign_id, cache=None):
    """Every ad set in a campaign for direct budget lookup by ad-set name."""
    return _cached(cache, f"campaign_adsets:{campaign_id}", lambda: _paginate(
        token, f"{campaign_id}/adsets", limit=200,
        fields="id,name,daily_budget"))


def list_campaign_active_adsets(token, campaign_id, cache=None):
    """Ad sets in a campaign, including delivery state for budget analysis."""
    return _cached(cache, f"campaign_active_adsets:{campaign_id}", lambda: _paginate(
        token, f"{campaign_id}/adsets", limit=200,
        fields="id,name,status,effective_status,daily_budget,created_time"))


def list_account_active_adsets(token, account_id, campaign_ids, cache=None):
    """Fetch active ad sets once per account, then retain selected campaigns."""
    wanted = {str(value) for value in campaign_ids}
    campaign_key = ",".join(sorted(wanted))
    rows = _cached(cache, f"active_adsets_account:{account_id}:{campaign_key}", lambda: _paginate(
        token, f"act_{account_id}/adsets", limit=500,
        fields="id,name,status,effective_status,daily_budget,created_time,campaign_id",
        filtering=json.dumps([
            {"field": "campaign.id", "operator": "IN", "value": sorted(wanted)},
            {"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]},
        ]),
    ))
    return [row for row in rows if str(row.get("campaign_id")) in wanted]


def list_account_active_ads(token, account_id, campaign_ids, cache=None):
    """Fetch ads that belong to active ad sets once per account.

    PAUSED ads are included so an ACTIVE ad set is not omitted merely because
    its individual ads are currently paused. The caller separately identifies
    the actually ACTIVE ads when it needs active creative names.
    """
    wanted = {str(value) for value in campaign_ids}
    campaign_key = ",".join(sorted(wanted))
    rows = _cached(cache, f"active_ads_account:{account_id}:{campaign_key}", lambda: _paginate(
        token, f"act_{account_id}/ads", limit=500,
        fields="id,name,status,effective_status,created_time,campaign_id,"
               "adset{id,name,status,effective_status,daily_budget,created_time,campaign_id}",
        filtering=json.dumps([
            {"field": "campaign.id", "operator": "IN", "value": sorted(wanted)},
            {"field": "effective_status", "operator": "IN", "value": ["ACTIVE", "PAUSED"]},
        ]),
    ))
    return [row for row in rows if str(row.get("campaign_id")) in wanted]


def list_adset_ads(token, adset_id, cache=None):
    return _cached(cache, f"adset_ads:{adset_id}", lambda: _paginate(
        token, f"{adset_id}/ads", limit=200,
        fields="id,name,status,effective_status,created_time"))


def get_adset_insights(token, adset_id, since, until):
    rows = _paginate(
        token, f"{adset_id}/insights", limit=200,
        fields="spend,cpm,actions,cost_per_action_type",
        time_range=json.dumps({"since": since, "until": until}),
        level="adset",
    )
    if not rows:
        return {"spend": 0.0, "cpm": None, "cpa": None}
    row = rows[0]
    spend = float(row.get("spend") or 0)
    cpm = float(row["cpm"]) if row.get("cpm") not in (None, "") else None
    checkout_types = (
        "omni_initiated_checkout", "initiate_checkout",
        "offsite_conversion.fb_pixel_initiate_checkout",
    )
    costs = {item.get("action_type"): item.get("value") for item in row.get("cost_per_action_type") or []}
    cpa = next((float(costs[action]) for action in checkout_types if costs.get(action) not in (None, "")), None)
    if cpa is None:
        actions = {item.get("action_type"): float(item.get("value") or 0) for item in row.get("actions") or []}
        checkouts = next((actions[action] for action in checkout_types if actions.get(action)), 0)
        cpa = spend / checkouts if checkouts else None
    return {"spend": spend, "cpm": cpm, "cpa": cpa}


def _insight_metrics(row):
    if not row:
        return {"spend": 0.0, "cpm": None, "cpa": None}
    spend = float(row.get("spend") or 0)
    cpm = float(row["cpm"]) if row.get("cpm") not in (None, "") else None
    checkout_types = (
        "omni_initiated_checkout", "initiate_checkout",
        "offsite_conversion.fb_pixel_initiate_checkout",
    )
    costs = {item.get("action_type"): item.get("value") for item in row.get("cost_per_action_type") or []}
    cpa = next((float(costs[action]) for action in checkout_types if costs.get(action) not in (None, "")), None)
    if cpa is None:
        actions = {item.get("action_type"): float(item.get("value") or 0) for item in row.get("actions") or []}
        checkouts = next((actions[action] for action in checkout_types if actions.get(action)), 0)
        cpa = spend / checkouts if checkouts else None
    return {"spend": spend, "cpm": cpm, "cpa": cpa}


def get_account_adset_insights(token, account_id, campaign_ids, since, until):
    """One account-level D-3 request instead of one request per ad set."""
    rows = _paginate(
        token, f"act_{account_id}/insights", limit=500,
        fields="adset_id,spend,cpm,actions,cost_per_action_type",
        time_range=json.dumps({"since": since, "until": until}),
        filtering=json.dumps([{
            "field": "campaign.id", "operator": "IN", "value": [str(x) for x in campaign_ids],
        }]),
        level="adset",
    )
    return {str(row.get("adset_id")): _insight_metrics(row) for row in rows if row.get("adset_id")}


def get_account_daily_ad_spend(token, account_id, campaign_ids, since, until):
    """One account-level daily ad-spend request for selected campaigns."""
    rows = _paginate(
        token, f"act_{account_id}/insights", limit=500,
        fields="adset_id,ad_id,ad_name,spend,date_start",
        time_range=json.dumps({"since": since, "until": until}),
        filtering=json.dumps([{
            "field": "campaign.id", "operator": "IN", "value": [str(x) for x in campaign_ids],
        }]),
        time_increment=1,
        level="ad",
    )
    grouped = {}
    for row in rows:
        adset_id = str(row.get("adset_id") or "")
        if not adset_id or not row.get("ad_name") or not row.get("date_start"):
            continue
        grouped.setdefault(adset_id, []).append({
            "date": row["date_start"], "ad_name": row["ad_name"],
            "spend": float(row.get("spend") or 0),
        })
    return grouped


def get_adset_daily_ad_spend(token, adset_id, since, until):
    """Daily Meta spend by ad for operating-day and D.ROAS calculations."""
    rows = _paginate(
        token, f"{adset_id}/insights", limit=500,
        fields="ad_id,ad_name,spend,date_start",
        time_range=json.dumps({"since": since, "until": until}),
        time_increment=1,
        level="ad",
    )
    return [
        {
            "date": row.get("date_start"),
            "ad_name": row.get("ad_name", ""),
            "spend": float(row.get("spend") or 0),
        }
        for row in rows if row.get("ad_name") and row.get("date_start")
    ]


def ensure_adset_active(token, adset_id):
    """Re-activate after a budget write if Meta changed delivery state."""
    state = api("GET", adset_id, token, fields="id,status,effective_status")
    if state.get("status") != "ACTIVE":
        api("POST", adset_id, token, status="ACTIVE")
    verified = api("GET", adset_id, token, fields="id,status,effective_status")
    return verified


def update_adset_budget(token, adset_id, daily_budget):
    """Sets an ad set's daily budget (major currency units -> Meta's cents)."""
    api("POST", adset_id, token, daily_budget=int(daily_budget) * 100)


def find_adset_by_name(token, campaign_id, name, cache=None):
    adsets = _cached(cache, f"adsets:{campaign_id}", lambda: api(
        "GET", f"{campaign_id}/adsets", token,
        fields="id,name,targeting,optimization_goal,billing_event,"
               "bid_strategy,promoted_object,destination_type,"
               "start_time,end_time,daily_budget", limit=200,
    ).get("data", []))
    for adset in adsets:
        if adset["name"] == name:
            return adset
    raise MetaApiError(f"'{campaign_id}' 캠페인 안에서 '{name}' 이름의 광고 세트를 찾지 못했습니다.")


def find_template_ad(token, adset_id, cache=None):
    def fetch():
        data = api("GET", f"{adset_id}/ads", token,
                   fields="id,name,creative{id,object_story_spec,call_to_action_type}",
                   limit=5)
        ads = data.get("data", [])
        if not ads:
            raise MetaApiError(f"광고 세트 {adset_id} 안에 템플릿으로 쓸 광고가 없습니다.")
        return ads[0]
    return _cached(cache, f"template_ad:{adset_id}", fetch)


def find_creative_asset(token, account_id, name_query, cache=None):
    def fetch_library():
        library = []
        for endpoint in ("advideos", "adimages"):
            data = api("GET", f"act_{account_id}/{endpoint}", token,
                       fields="id,name,hash" if endpoint == "adimages" else "id,title,picture",
                       limit=100)
            library.extend((endpoint, item) for item in data.get("data", []))
        return library

    library = _cached(cache, f"creative_library:{account_id}", fetch_library)
    for endpoint, item in library:
        label = item.get("name") or item.get("title") or ""
        if name_query.lower() in label.lower():
            return endpoint, item
    raise MetaApiError(f"'{name_query}'와(과) 일치하는 영상/이미지를 act_{account_id} 라이브러리에서 찾지 못했습니다.")


def duplicate_ad(token, *, campaign_id, candidate_account_ids, source_adset_name, new_adset_name,
                  new_ad_name, daily_budget, website_url, creative_name, headline,
                  primary_text, start_iso=None, end_iso=None, status="PAUSED",
                  account_override=None, cache=None, batch_adsets=None):
    """Two modes, chosen by whether source_adset_name is given:

    - Normal (source_adset_name given): clone its targeting/optimization/promoted
      object into a brand-new ad set (new_adset_name), then create the new ad
      inside it.
    - "Ads-only" (source_adset_name blank): don't create a new ad set at all —
      add just the new ad into an ad set that's already named new_adset_name,
      either one that already exists in Meta, or one an earlier row in the same
      bulk batch just created (tracked via `batch_adsets`, since the ad-set-list
      cache snapshot taken at the start of the batch won't see it). The target
      ad set's daily_budget is force-overwritten to daily_budget afterwards,
      matching the original artifact's rule for this path.

    Pass a shared `cache` dict across a batch to fetch each campaign/ad-set/
    creative-library lookup once instead of once per row.
    """
    account_id, campaign_id = resolve_campaign_and_account(
        token, campaign_id, candidate_account_ids, account_override=account_override, cache=cache)

    ads_only = not source_adset_name or not source_adset_name.strip()

    if ads_only:
        batch_key = f"{account_id}:{campaign_id}:{new_adset_name}"
        existing_id = (batch_adsets or {}).get(batch_key)
        if existing_id:
            target_adset_id = existing_id
        else:
            target_adset_id = find_adset_by_name(token, campaign_id, new_adset_name, cache=cache)["id"]
        template_ad = find_template_ad(token, target_adset_id, cache=cache)
    else:
        source_adset = find_adset_by_name(token, campaign_id, source_adset_name, cache=cache)
        template_ad = find_template_ad(token, source_adset["id"], cache=cache)

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
        if source_adset.get("promoted_object"):
            adset_payload["promoted_object"] = json.dumps(source_adset["promoted_object"])
        if source_adset.get("destination_type"):
            adset_payload["destination_type"] = source_adset["destination_type"]
        if start_iso:
            adset_payload["start_time"] = start_iso
        if end_iso:
            adset_payload["end_time"] = end_iso
        new_adset = api("POST", f"act_{account_id}/adsets", token, **adset_payload)
        target_adset_id = new_adset["id"]
        if batch_adsets is not None:
            batch_adsets[f"{account_id}:{campaign_id}:{new_adset_name}"] = target_adset_id

    story_spec = template_ad["creative"]["object_story_spec"]
    page_id = story_spec.get("page_id")
    cta_type = template_ad["creative"].get("call_to_action_type", "SHOP_NOW")

    endpoint, asset = find_creative_asset(token, account_id, creative_name, cache=cache)

    call_to_action = json.dumps({"type": cta_type, "value": {"link": website_url}})
    if endpoint == "advideos":
        # Video ads use a distinct `video_data` object instead of `link_data` —
        # Meta rejects `video_id` inside `link_data` (error_subcode 1443050).
        video_data = {
            "video_id": asset["id"],
            "title": headline,
            "message": primary_text,
            "call_to_action": call_to_action,
        }
        if asset.get("picture"):
            video_data["image_url"] = asset["picture"]
        new_story_spec = {"page_id": page_id, "video_data": video_data}
    else:
        link_data = {
            "link": website_url,
            "message": primary_text,
            "name": headline,
            "image_hash": asset["hash"],
            "call_to_action": call_to_action,
        }
        new_story_spec = {"page_id": page_id, "link_data": link_data}
    creative = api("POST", f"act_{account_id}/adcreatives", token,
                   name=f"{new_ad_name} - creative",
                   object_story_spec=json.dumps(new_story_spec))

    ad = api("POST", f"act_{account_id}/ads", token,
             name=new_ad_name,
             adset_id=target_adset_id,
             creative=json.dumps({"creative_id": creative["id"]}),
             status=status)

    if ads_only:
        # Always overwrite the shared ad set's budget to what this request asked
        # for, even if unchanged — matches the original artifact's rule for the
        # ads-only path.
        api("POST", target_adset_id, token, daily_budget=daily_budget * 100)

    return {
        "ad_set_id": target_adset_id,
        "creative_id": creative["id"],
        "ad_id": ad["id"],
        "account_id": account_id,
        "ads_manager_url": (
            f"https://www.facebook.com/adsmanager/manage/ads/edit?"
            f"act={account_id}&selected_ad_ids={ad['id']}"
        ),
    }


def upload_creative(token, account_id, filename, file_obj):
    """Uploads a local file straight into the ad account's video/image library
    (no public URL needed — unlike a Claude session with no local filesystem
    access, this desktop tool can just send the file's bytes directly).
    Video vs. image is picked by file extension. Returns a dict describing the
    created asset; for images, 'asset_id' is the image hash used elsewhere as
    `image_hash` in an ad creative's link_data; for videos, it's the video id
    used as `video_id`.
    """
    ext = os.path.splitext(filename)[1].lower()
    is_video = ext in VIDEO_EXTENSIONS
    endpoint = "advideos" if is_video else "adimages"
    field = "source" if is_video else "filename"

    upload_data = {"access_token": token}
    if is_video:
        # Meta supports an explicit name/title for videos.  Set both to the
        # original local filename so the asset can be found later using the
        # exact same 소재명.  Images take their library name from the filename
        # in the multipart tuple below.
        upload_data.update({"name": filename, "title": filename})

    r = requests.post(
        f"{GRAPH}/act_{account_id}/{endpoint}",
        files={field: (filename, file_obj)},
        data=upload_data,
    )
    data = r.json()
    if "error" in data:
        raise MetaApiError(_format_error("POST", f"act_{account_id}/{endpoint}", data["error"]))

    if is_video:
        return {"kind": "video", "asset_id": data.get("id"), "filename": filename}

    images = data.get("images", {})
    info = images.get(filename) or next(iter(images.values()), {})
    if not info.get("hash"):
        raise MetaApiError(f"이미지 업로드 응답에서 hash를 찾지 못했습니다: {data}")
    return {"kind": "image", "asset_id": info["hash"], "filename": filename, "url": info.get("url")}
