#!/usr/bin/env python3
"""
Duplicate a Meta ad set + ad via the Marketing API, run locally from your desktop.

See meta_lib.duplicate_ad for the actual API logic (shared with webapp/app.py).

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
import os
import sys

from dotenv import load_dotenv

import meta_lib

load_dotenv()  # picks up META_ACCESS_TOKEN from a .env file if present


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

    token = args.token or os.environ.get("META_ACCESS_TOKEN")
    if not token:
        sys.exit("Set META_ACCESS_TOKEN or pass --token")

    result = meta_lib.duplicate_ad(
        token,
        campaign_id=args.campaign_id,
        candidate_account_ids=[args.account_id],
        account_override=args.account_id,
        source_adset_name=args.source_adset_name,
        new_adset_name=args.new_adset_name,
        new_ad_name=args.new_ad_name,
        daily_budget=args.daily_budget,
        website_url=args.website_url,
        creative_name=args.creative_name,
        headline=args.headline,
        primary_text=args.primary_text,
        start_iso=args.start_iso,
        end_iso=args.end_iso,
        status=args.status,
    )
    print(f"Created ad set {result['ad_set_id']}")
    print(f"Created creative {result['creative_id']}")
    print(f"Created ad {result['ad_id']}")
    print(f"Ads Manager: {result['ads_manager_url']}")


if __name__ == "__main__":
    main()
