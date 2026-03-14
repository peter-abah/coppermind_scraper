# /// script
# dependencies = [
#   "mwparserfromhell",
#   "pyyaml",
# ]
# ///

import os
import re

import mwparserfromhell
import yaml
from mwparserfromhell.nodes import Tag, Template, Wikilink

# Directory Configuration
INPUT_DIR = "coppermind_raw"
OUTPUT_DIR = "coppermind_vault"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# Templates that contain DATA we want to keep in the text/YAML
# This prevents reference IDs like 'mb1' or 'sa2' from leaking into your prose
DATA_WHITELIST = ["tag", "tag+", "date", "map ref", "lerasium", "atium", "harmonium"]

# Templates to ignore when determining the "Entity Type"
EXCLUDE_LIST = [
    "quote",
    "sidequote",
    "update",
    "partial",
    "complete",
    "notice",
    "navaid",
    "section",
    "holder",
    "for",
    "spoilers",
    "image",
    "stormlight",
    "mistborn",
    "cosmere",
    "elantris",
]


def clean_node(node):
    """
    Surgically cleans wiki-syntax nodes.
    - Extracts text from whitelisted tags (Mistborn, 1173, etc.)
    - Silently deletes reference and meta templates (book ref, wob ref)
    """
    if isinstance(node, Template):
        name = str(node.name).lower().strip()
        if name in DATA_WHITELIST:
            return str(node.get(1).value) if node.has(1) else ""
        return ""
    return str(node)


def table_to_markdown(tag_node):
    """
    Converts a MediaWiki table (parsed as a Tag) into a GFM Markdown table.
    Handles headers (!), cells (|), and internal cleaning.
    """
    text = str(tag_node.contents)
    rows = []
    # Split by row separators
    raw_rows = re.split(r"\n\|\-\s*", text)

    for row in raw_rows:
        # Match headers (!) or cells (|)
        cells = re.findall(r"(?:^|[!\x7c])\s*([^!\x7c\n]+)", row)

        # Clean each cell of remaining wiki-junk using our logic
        clean_cells = []
        for c in cells:
            cell_ast = mwparserfromhell.parse(c)
            # Preserve links in tables just like we do in YAML
            cleaned = "".join(
                str(n) if isinstance(n, Wikilink) else clean_node(n)
                for n in cell_ast.nodes
            ).strip()
            if cleaned:
                clean_cells.append(cleaned)

        if clean_cells:
            rows.append("| " + " | ".join(clean_cells) + " |")

    if len(rows) < 1:
        return ""

    # Create the GFM separator line
    header_count = rows[0].count("|") - 1
    separator = "|" + " --- |" * header_count
    rows.insert(1, separator)
    return "\n" + "\n".join(rows) + "\n"


def process_to_obsidian(raw_content, filename):
    """Parses raw wikitext into Obsidian-ready Markdown with interlinked YAML."""
    wikicode = mwparserfromhell.parse(raw_content)
    frontmatter = {"source_file": filename}

    # 1. INFOBOX / METADATA EXTRACTION
    templates = wikicode.filter_templates()
    primary = next(
        (t for t in templates if str(t.name).lower().strip() not in EXCLUDE_LIST), None
    )

    if primary:
        frontmatter["entity_type"] = str(primary.name).strip().lower()
        for param in primary.params:
            key = str(param.name).strip().lower()
            # Only process named parameters for the YAML header
            if not key.isdigit():
                val_code = mwparserfromhell.parse(param.value)
                # Preserving [[links]] for Obsidian Graph view
                clean_val = "".join(
                    str(n) if isinstance(n, Wikilink) else clean_node(n)
                    for n in val_code.nodes
                ).strip()
                if clean_val and clean_val not in [",", ""]:
                    frontmatter[key] = clean_val
        try:
            wikicode.remove(primary)
        except ValueError:
            pass
    else:
        frontmatter["entity_type"] = "article"

    # 2. TABLE PROCESSING
    # mwparserfromhell treats tables as Tag nodes starting with '{|'
    for tag in wikicode.filter_tags():
        if tag.tag == "{|":
            try:
                md_table = table_to_markdown(tag)
                wikicode.replace(tag, md_table)
            except:
                pass

    # 3. PROSE AND LAYOUT
    for temp in wikicode.filter_templates():
        name = str(temp.name).lower().strip()
        if name in ["quote", "sidequote"]:
            # Format quotes as proper Markdown blockquotes
            text = (
                "".join(
                    clean_node(n)
                    for n in mwparserfromhell.parse(temp.get(1).value).nodes
                )
                if temp.has(1)
                else ""
            )
            author = (
                "".join(
                    clean_node(n)
                    for n in mwparserfromhell.parse(temp.get(2).value).nodes
                )
                if temp.has(2)
                else ""
            )
            wikicode.replace(temp, f"\n> {text.strip()}\n> — {author.strip()}\n")
        elif name in ["columns", "list", "holder"]:
            # Unpack the content inside layout templates to prevent data loss
            content = str(temp.get(1).value) if temp.has(1) else ""
            wikicode.replace(temp, content)
        else:
            # Delete references (book ref, wob ref) to stop text leaks
            try:
                wikicode.remove(temp)
            except ValueError:
                pass

    # 4. FINAL CLEANUP
    prose = str(wikicode)

    # Convert HTML to Obsidian-friendly Callouts
    prose = re.sub(
        r"<div[^>]*>(.*?)</div>",
        r"\n> [!WARNING] Info\n> \1\n",
        prose,
        flags=re.DOTALL | re.IGNORECASE,
    )
    prose = re.sub(r"<span[^>]*>(.*?)</span>", r"**\1**", prose)

    # Bold, Italic, and Header conversion
    prose = re.sub(r"'''(.*?)'''", r"**\1**", prose)
    prose = re.sub(r"''(.*?)''", r"*\1*", prose)
    prose = re.sub(
        r"^==+ (.*?) ==+",
        lambda m: "#" * (m.group(0).count("=") // 2) + " " + m.group(1),
        prose,
        flags=re.MULTILINE,
    )

    # Remove transclusion artifacts (e.g., {{/table}})
    prose = re.sub(r"\{\{/.*?\}\}", "", prose)

    # Final whitespace trim
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()

    final_yaml = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
    return f"---\n{final_yaml}---\n\n{prose}"


def main():
    print(f"--- Processing raw files from {INPUT_DIR} ---")
    files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".txt")]

    processed_count = 0
    for filename in files:
        try:
            with open(os.path.join(INPUT_DIR, filename), "r", encoding="utf-8") as f:
                raw_content = f.read()

            processed_md = process_to_obsidian(raw_content, filename)

            output_name = filename.replace(".txt", ".md")
            with open(
                os.path.join(OUTPUT_DIR, output_name), "w", encoding="utf-8"
            ) as f:
                f.write(processed_md)

            processed_count += 1
            if processed_count % 100 == 0:
                print(f"Handled {processed_count} files...")

        except Exception as e:
            print(f"Failed to process {filename}: {e}")

    print(f"--- Done: {processed_count} files saved to '{OUTPUT_DIR}' ---")


if __name__ == "__main__":
    main()
