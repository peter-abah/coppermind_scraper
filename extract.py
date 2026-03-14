import os
import re
import time

from curl_cffi import requests

URL = "https://coppermind.net/w/api.php"
OUTPUT_DIR = "coppermind_vault"

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


def parse_infobox_to_yaml(text):
    # Find the primary infobox template block
    match = re.search(
        r"^{{((?:Infobox|Character|Location|Item).*?)}}",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return text

    infobox_content = match.group(1)
    yaml_lines = ["---"]

    # Extract key-value pairs (e.g., | name = Kaladin )
    pairs = re.findall(r"\|\s*(.*?)\s*=\s*(.*)", infobox_content)
    for key, value in pairs:
        # Strip internal link brackets to prevent YAML syntax errors
        clean_value = re.sub(r"\[\[(.*?)\]\]", r"\1", value).strip()
        # Escape quotes just in case
        clean_value = clean_value.replace('"', "'")
        yaml_lines.append(f'{key.strip()}: "{clean_value}"')

    if len(yaml_lines) > 1:
        yaml_lines.append("---\n")
        yaml_frontmatter = "\n".join(yaml_lines)
        # Remove original infobox from body text
        text = text[match.end() :].strip()
        return yaml_frontmatter + "\n" + text

    return text


def mw_to_md(text):
    text = parse_infobox_to_yaml(text)
    text = re.sub(r"'''(.*?)'''", r"**\1**", text)  # Bold
    text = re.sub(r"''(.*?)''", r"*\1*", text)  # Italic
    text = re.sub(r"^=== (.*?) ===", r"### \1", text, flags=re.MULTILINE)  # H3
    text = re.sub(r"^== (.*?) ==", r"## \1", text, flags=re.MULTILINE)  # H2
    return text


print("Extracting pages and compiling YAML metadata...")

while True:
    response = requests.get(URL, params=params, impersonate="chrome120")

    if response.status_code != 200:
        print(f"Blocked or Failed: HTTP {response.status_code}")
        break

    data = response.json()
    pages = data.get("query", {}).get("pages", {})

    for page_info in pages.values():
        title = page_info.get("title", "Unknown")
        try:
            content = page_info["revisions"][0]["*"]

            filename = clean_filename(title)
            filepath = os.path.join(OUTPUT_DIR, f"{filename}.md")

            md_content = mw_to_md(content)

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(md_content)

        except KeyError:
            continue

    if "continue" in data:
        params.update(data["continue"])
        print("Batch processed. Fetching next 50 pages...")
        time.sleep(1.5)
    else:
        print(f"Extraction complete. Your vault is ready in '{OUTPUT_DIR}'.")
        break
