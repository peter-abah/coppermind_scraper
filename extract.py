# /// script
# dependencies = [
#   "curl_cffi",
# ]
# ///

import os
import re
import time
from curl_cffi import requests

URL = "https://coppermind.net/w/api.php"
# We change the directory name to reflect that this is RAW data
OUTPUT_DIR = "coppermind_raw"

os.makedirs(OUTPUT_DIR, exist_ok=True)

params = {
    "action": "query",
    "generator": "allpages",
    "gaplimit": "50",
    "prop": "revisions",
    "rvprop": "content",
    "format": "json",
}

def clean_filename(title):
    return re.sub(r'[\\/*?:"<>|]', "", title)

print(f"Directing raw wikitext extraction to '{OUTPUT_DIR}'...")

while True:
    try:
        response = requests.get(URL, params=params, impersonate="chrome120")
        if response.status_code != 200:
            print(f"Extraction interrupted: HTTP {response.status_code}")
            break

        data = response.json()
        pages = data.get("query", {}).get("pages", {})

        for page_info in pages.values():
            title = page_info.get("title", "Unknown")
            if "revisions" not in page_info:
                continue

            # Extract exactly what the API returns, no modification
            raw_wikitext = page_info["revisions"][0]["*"]
            filename = clean_filename(title)
            filepath = os.path.join(OUTPUT_DIR, f"{filename}.txt")

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(raw_wikitext)

        if "continue" in data:
            params.update(data["continue"])
            print(f"Downloaded batch. Total pages saved: {len(os.listdir(OUTPUT_DIR))}")
            time.sleep(1.0)
        else:
            print("Extraction complete. Raw vault is ready.")
            break

    except Exception as e:
        print(f"Error: {e}")
        break
