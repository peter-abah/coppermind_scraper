# /// script
# dependencies = [
#   "mwparserfromhell",
#   "pyyaml",
# ]
# ///
"""
Coppermind wikitext processor.

Converts raw Coppermind wikitext files into Obsidian-ready Markdown with:
  - YAML frontmatter (infobox metadata, entity type, extracted wikilinks)
  - Inline source tags [MB1], [SA2], [WoB] preserved for citation tracing
  - GFM tables converted from wiki table syntax
  - All reference/navigation templates stripped, prose preserved

Usage:
    python processor.py                  # process all files in coppermind_raw/
    python processor.py --scan-gaps      # report files with transcluded subpage warnings
    python processor.py --file Allomancy.txt  # process a single file
"""

import argparse
import os
import re
from collections import Counter

import mwparserfromhell
import yaml
from mwparserfromhell.nodes import Template, Wikilink

INPUT_DIR = "coppermind_raw"
OUTPUT_DIR = "coppermind_vault"

os.makedirs(OUTPUT_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Book code mapping for inline source tags
# e.g. {{book ref|mb1|7}} → [MB1]  {{book ref|sa2|68}} → [SA2]
# Codes are normalised to uppercase for readability.
# ---------------------------------------------------------------------------
BOOK_CODES = {
    # Mistborn Era 1
    "mb1": "MB1",
    "mistborn1": "MB1",
    "mb2": "MB2",
    "mistborn2": "MB2",
    "mb3": "MB3",
    "mistborn3": "MB3",
    # Mistborn Era 2
    "mb4": "MB4",
    "mb5": "MB5",
    "mb6": "MB6",
    "mb7": "MB7",
    # Stormlight
    "sa1": "SA1",
    "sa2": "SA2",
    "sa3": "SA3",
    "sa4": "SA4",
    "sa5": "SA5",
    "twok": "SA1",
    "wor": "SA2",
    "ob": "SA3",
    "row": "SA4",
    "wind": "SA5",
    # Other Cosmere
    "elantris": "EL",
    "warbreaker": "WB",
    "emperors soul": "ES",
    "mistborn": "MB1",  # generic fallback
    "tress": "TRESS",
    "yumi": "YUMI",
    "sunlit": "SUNLIT",
    "iote": "IOTE",
    # Non-Cosmere
    "reckoners": "REC",
    "alcatraz": "ALC",
    "skyward": "SKY",
}

# Templates whose first positional argument is meaningful prose data.
DATA_WHITELIST = {"tag", "tag+", "date", "map ref", "lerasium", "atium", "harmonium"}

# Templates that are structurally never infoboxes.
NEVER_INFOBOX = {
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
    "columns",
    "list",
    "book ref",
    "wob ref",
    "epigraph ref",
    "url ref",
    "file ref",
    "anchor",
    "tag",
    "tag+",
    "date",
    "map ref",
}

INFOBOX_MIN_NAMED_PARAMS = 2
UNPACK_TEMPLATES = {"columns", "list", "holder", "for", "section"}
QUOTE_TEMPLATES = {"quote", "sidequote"}


# ---------------------------------------------------------------------------
# Node-level helpers
# ---------------------------------------------------------------------------


def clean_node(node) -> str:
    if isinstance(node, Template):
        name = str(node.name).lower().strip()
        if name in DATA_WHITELIST:
            return str(node.get(1).value) if node.has(1) else ""
        return ""
    return str(node)


def clean_wikitext_fragment(fragment: str, preserve_links: bool = False) -> str:
    ast = mwparserfromhell.parse(fragment)
    parts = []
    for n in ast.nodes:
        if preserve_links and isinstance(n, Wikilink):
            parts.append(str(n))
        else:
            parts.append(clean_node(n))
    return "".join(parts).strip()


def extract_wikilinks(wikicode) -> list[str]:
    """
    Return a sorted, deduplicated list of all wikilink targets in the page.
    Uses the canonical target (before the |), strips section anchors (#).
    These are written to YAML frontmatter for Obsidian graph resolution
    and agent context chaining.
    """
    seen = set()
    links = []
    for node in wikicode.filter_wikilinks():
        target = str(node.title).strip()
        target = target.split("#")[0].strip()  # drop section anchors
        target = target.split("|")[0].strip()  # drop any inline display text
        if target and not target.lower().startswith("category:") and target not in seen:
            seen.add(target)
            links.append(target)
    return sorted(links)


# ---------------------------------------------------------------------------
# Source tag rendering
# ---------------------------------------------------------------------------


def render_book_ref(temp) -> str:
    """
    {{book ref|mb1|7}} → [MB1]
    Falls back to the raw book code if not in BOOK_CODES.
    """
    book_raw = str(temp.get(1).value).strip().lower() if temp.has(1) else ""
    code = BOOK_CODES.get(book_raw, book_raw.upper())
    return f"[{code}]" if code else ""


def render_wob_ref(temp) -> str:
    """{{wob ref|5269}} → [WoB]"""
    return "[WoB]"


def render_epigraph_ref(temp) -> str:
    """{{epigraph ref|mb3|32}} → [MB3-ep]"""
    book_raw = str(temp.get(1).value).strip().lower() if temp.has(1) else ""
    code = BOOK_CODES.get(book_raw, book_raw.upper())
    return f"[{code}-ep]" if code else "[ep]"


# ---------------------------------------------------------------------------
# Table conversion
# ---------------------------------------------------------------------------

_PIPE_SENTINEL = "\x00PIPE\x00"


def _protect_wikilink_pipes(text: str) -> str:
    def replace_inner(m: re.Match) -> str:
        return m.group(0).replace("|", _PIPE_SENTINEL)

    return re.sub(r"\[\[[^\]]*\|[^\]]*\]\]", replace_inner, text)


def _restore_wikilink_pipes(text: str) -> str:
    return text.replace(_PIPE_SENTINEL, "|")


def _parse_table_row(row: str) -> list[str]:
    cells = re.findall(r"(?:^|\n)\s*(?:!|\|)\s*([^!\|\n]+)", row)
    clean_cells = []
    for c in cells:
        restored = _restore_wikilink_pipes(c)
        cleaned = clean_wikitext_fragment(restored, preserve_links=True)
        if cleaned.strip():
            clean_cells.append(cleaned)
    real_cells = [
        c
        for c in clean_cells
        if re.search(r"[^\w\s='\"\-#;:.!|]", c)
        or not re.match(r"^[\w]+\s*=", c.split()[0] if c.split() else "")
    ]
    return real_cells


def wiki_table_to_markdown(table_text: str) -> str:
    protected = _protect_wikilink_pipes(table_text)
    raw_rows = re.split(r"\n\s*\|\-[^\n]*", protected)
    parsed_rows = [_parse_table_row(r) for r in raw_rows]
    parsed_rows = [r for r in parsed_rows if r]

    if not parsed_rows:
        return ""

    col_count = Counter(len(r) for r in parsed_rows).most_common(1)[0][0]
    if col_count < 1:
        return ""

    header = parsed_rows[0]
    data_rows = [r for r in parsed_rows[1:] if len(r) == col_count]

    if len(header) != col_count and data_rows:
        header = data_rows.pop(0)

    if not header:
        return ""

    separator = "| " + " | ".join(["---"] * len(header)) + " |"
    md_rows = ["| " + " | ".join(header) + " |", separator] + [
        "| " + " | ".join(r) + " |" for r in data_rows
    ]
    return "\n" + "\n".join(md_rows) + "\n"


def extract_cell_template_tables(raw: str) -> str:
    """
    Pre-pass: replace wiki tables built from repeated X/cell templates
    (e.g. {{metal/cell|...}}) with clean flat GFM tables.
    These 2D grid layouts cannot be handled by the generic converter.
    """
    known_headers = {
        "metal/cell": ["Metal", "Misting Title", "Effect"],
        "magic/cell": ["Magic", "Type", "Description"],
    }

    def replace_if_cell_template_table(m: re.Match) -> str:
        block = m.group(0)
        template_names = re.findall(r"\{\{(\w+/\w+)\|", block)
        if not template_names:
            return block

        dominant = Counter(template_names).most_common(1)[0][0]
        if template_names.count(dominant) < 2:
            return block

        pattern = r"\{\{" + re.escape(dominant) + r"\|([^}]+)\}\}"
        matches = re.findall(pattern, block)
        if not matches:
            return block

        rows_data = [[p.strip() for p in match.split("|")] for match in matches]
        col_count = Counter(len(r) for r in rows_data).most_common(1)[0][0]
        rows_data = [r for r in rows_data if len(r) == col_count]

        if dominant in known_headers and len(known_headers[dominant]) == col_count:
            header_cols = known_headers[dominant]
        else:
            header_cols = [f"Column {i + 1}" for i in range(col_count)]

        header = "| " + " | ".join(header_cols) + " |"
        sep = "| " + " | ".join(["---"] * col_count) + " |"
        data = ["| " + " | ".join(r) + " |" for r in rows_data]
        return "\n" + "\n".join([header, sep] + data) + "\n"

    return re.sub(r"\{\|.*?\|\}", replace_if_cell_template_table, raw, flags=re.DOTALL)


# ---------------------------------------------------------------------------
# Infobox detection
# ---------------------------------------------------------------------------


def detect_infobox(templates):
    for t in templates:
        name = str(t.name).lower().strip()
        if name in NEVER_INFOBOX:
            continue
        named_params = [p for p in t.params if not str(p.name).strip().isdigit()]
        if len(named_params) >= INFOBOX_MIN_NAMED_PARAMS:
            return t
    return None


# ---------------------------------------------------------------------------
# Main processor
# ---------------------------------------------------------------------------


def process_to_obsidian(raw_content: str, filename: str) -> str:
    """Converts a raw Coppermind wikitext page to Obsidian-ready Markdown."""

    # ------------------------------------------------------------------
    # Pre-pass 1: strip transclusion-scope tags on raw string
    # ------------------------------------------------------------------
    raw_content = re.sub(
        r"<noinclude>(.*?)</noinclude>",
        r"\1",
        raw_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    raw_content = re.sub(
        r"<includeonly>.*?</includeonly>",
        "",
        raw_content,
        flags=re.DOTALL | re.IGNORECASE,
    )
    raw_content = re.sub(
        r"<onlyinclude>(.*?)</onlyinclude>",
        r"\1",
        raw_content,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # ------------------------------------------------------------------
    # Pre-pass 2: extract cell-template tables before parsing
    # ------------------------------------------------------------------
    raw_content = extract_cell_template_tables(raw_content)

    wikicode = mwparserfromhell.parse(raw_content)
    frontmatter = {"source_file": filename}

    # ------------------------------------------------------------------
    # 1. TRANSCLUSION DETECTION (before template loop consumes them)
    # ------------------------------------------------------------------
    for temp in list(wikicode.filter_templates()):
        name = str(temp.name).strip()
        if name.startswith("/"):
            subpage = name.lstrip("/").strip() or "content"
            placeholder = (
                f"\n> [!WARNING] Transcluded subpage not available "
                f"\u2014 fetch **{subpage}** separately.\n"
            )
            try:
                wikicode.replace(temp, placeholder)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # 2. WIKILINK EXTRACTION (before templates are removed)
    # Capture all linked pages for the YAML frontmatter.
    # ------------------------------------------------------------------
    linked_pages = extract_wikilinks(wikicode)

    # ------------------------------------------------------------------
    # 3. INFOBOX EXTRACTION
    # ------------------------------------------------------------------
    templates = wikicode.filter_templates()
    primary = detect_infobox(templates)

    if primary:
        raw_type = str(primary.name).strip().lower()
        frontmatter["entity_type"] = raw_type.replace(" ", "_")
        for param in primary.params:
            key = str(param.name).strip().lower().replace(" ", "_")
            if not key.isdigit():
                clean_val = clean_wikitext_fragment(
                    str(param.value), preserve_links=True
                )
                if clean_val and clean_val not in [",", ""]:
                    frontmatter[key] = clean_val
        try:
            wikicode.remove(primary)
        except ValueError:
            pass
    else:
        frontmatter["entity_type"] = "article"

    # Write linked pages to frontmatter after entity_type so it groups naturally
    if linked_pages:
        frontmatter["links"] = linked_pages

    # ------------------------------------------------------------------
    # 4. TEMPLATE HANDLING
    # ------------------------------------------------------------------
    for temp in wikicode.filter_templates():
        name = str(temp.name).lower().strip()

        if name in QUOTE_TEMPLATES:
            text = (
                clean_wikitext_fragment(str(temp.get(1).value)) if temp.has(1) else ""
            )
            author = (
                clean_wikitext_fragment(str(temp.get(2).value)) if temp.has(2) else ""
            )
            replacement = (
                f"\n> {text}\n> \u2014 {author}\n" if author else f"\n> {text}\n"
            )
            wikicode.replace(temp, replacement)

        elif name in UNPACK_TEMPLATES:
            content = str(temp.get(1).value) if temp.has(1) else ""
            wikicode.replace(temp, content)

        elif name == "book ref":
            # Preserve as inline source tag: [MB1], [SA2] etc.
            try:
                wikicode.replace(temp, render_book_ref(temp))
            except ValueError:
                pass

        elif name == "wob ref":
            # Preserve as inline [WoB] tag — marks Word of Brandon citations
            try:
                wikicode.replace(temp, render_wob_ref(temp))
            except ValueError:
                pass

        elif name == "epigraph ref":
            # Preserve as [MB3-ep] etc. — marks epigraph sources
            try:
                wikicode.replace(temp, render_epigraph_ref(temp))
            except ValueError:
                pass

        elif "/" in name:
            try:
                pos_params = [
                    str(p.value).strip()
                    for p in temp.params
                    if str(p.name).strip().isdigit()
                ]
                if name.startswith("row/") and len(pos_params) >= 2:
                    key = pos_params[0]
                    val = clean_wikitext_fragment(pos_params[1], preserve_links=True)
                    wikicode.replace(temp, f"**{key}**: {val}")
                else:
                    wikicode.replace(temp, " | ".join(pos_params) if pos_params else "")
            except ValueError:
                pass

        else:
            try:
                wikicode.remove(temp)
            except ValueError:
                pass

    # ------------------------------------------------------------------
    # 5. STRINGIFY
    # ------------------------------------------------------------------
    prose = str(wikicode)

    # ------------------------------------------------------------------
    # 6. POST-STRINGIFY NOINCLUDE CLEANUP (belt-and-suspenders)
    # ------------------------------------------------------------------
    prose = re.sub(
        r"<noinclude>(.*?)</noinclude>", r"\1", prose, flags=re.DOTALL | re.IGNORECASE
    )
    prose = re.sub(
        r"<includeonly>.*?</includeonly>", "", prose, flags=re.DOTALL | re.IGNORECASE
    )
    prose = re.sub(
        r"<onlyinclude>(.*?)</onlyinclude>",
        r"\1",
        prose,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # ------------------------------------------------------------------
    # 7. WIKI TABLE CONVERSION
    # ------------------------------------------------------------------
    def replace_table(m: re.Match) -> str:
        result = wiki_table_to_markdown(m.group(0))
        if result:
            return result
        print(f"  [WARN] Could not convert table in {filename}, keeping raw.")
        return m.group(0)

    prose = re.sub(r"\{\|.*?\|\}", replace_table, prose, flags=re.DOTALL)

    # ------------------------------------------------------------------
    # 8. HTML CLEANUP
    # ------------------------------------------------------------------
    prose = re.sub(
        r"<div[^>]*>(.*?)</div>",
        r"\n> [!INFO]\n> \1\n",
        prose,
        flags=re.DOTALL | re.IGNORECASE,
    )
    prose = re.sub(r"<span[^>]*>(.*?)</span>", r"**\1**", prose, flags=re.IGNORECASE)
    prose = re.sub(r"<[^>]+/\s*>", "", prose)
    prose = re.sub(
        r"<references\s*>.*?</references>", "", prose, flags=re.DOTALL | re.IGNORECASE
    )

    # ------------------------------------------------------------------
    # 9. WIKI FORMATTING TO MARKDOWN
    # ------------------------------------------------------------------
    prose = re.sub(r"'''(.*?)'''", r"**\1**", prose)
    prose = re.sub(r"''(.*?)''", r"*\1*", prose)

    def convert_header(m: re.Match) -> str:
        level = len(m.group(1))
        return "#" * level + " " + m.group(2).strip()

    prose = re.sub(
        r"^(={2,})\s*(.*?)\s*\1\s*$", convert_header, prose, flags=re.MULTILINE
    )

    prose = re.sub(
        r"^(\*+)(?!\*)", lambda m: "-" * len(m.group(1)), prose, flags=re.MULTILINE
    )

    def convert_external_link(m: re.Match) -> str:
        url = m.group(1)
        label = m.group(2).strip().strip("'")
        return f"[{label}]({url})" if label else url

    prose = re.sub(r"\[(\bhttps?://\S+)\s+([^\]]+)\]", convert_external_link, prose)

    # ------------------------------------------------------------------
    # 10. STRIP WIKI METADATA LINES
    # ------------------------------------------------------------------
    prose = re.sub(
        r"^\[\[category:[^\]]*\]\]\s*$", "", prose, flags=re.MULTILINE | re.IGNORECASE
    )
    prose = re.sub(r"^\[\[[a-z]{2}:[^\]]*\]\]\s*$", "", prose, flags=re.MULTILINE)
    prose = re.sub(r"^:Category:[^\n]*$", "", prose, flags=re.MULTILINE | re.IGNORECASE)

    # ------------------------------------------------------------------
    # 11. FINAL WHITESPACE
    # ------------------------------------------------------------------
    prose = re.sub(r"\n{3,}", "\n\n", prose).strip()

    final_yaml = yaml.dump(frontmatter, sort_keys=False, allow_unicode=True)
    return f"---\n{final_yaml}---\n\n{prose}"


# ---------------------------------------------------------------------------
# Gap scanner
# ---------------------------------------------------------------------------


def scan_gaps(vault_dir: str) -> None:
    """
    Report all files in the vault that contain transcluded subpage warnings.
    These are content gaps where a {{/subpage}} transclusion was detected
    but the subpage content could not be included.
    """
    print(f"Scanning '{vault_dir}' for transcluded subpage gaps...\n")
    gap_files = []
    for fname in os.listdir(vault_dir):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(vault_dir, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        warnings = re.findall(
            r"\[!WARNING\] Transcluded subpage not available.*?fetch \*\*(.+?)\*\*",
            content,
        )
        if warnings:
            gap_files.append((fname, warnings))

    if not gap_files:
        print("No gaps found — all pages are complete.")
        return

    print(f"Found {len(gap_files)} files with missing transcluded content:\n")
    for fname, subpages in sorted(gap_files):
        subpage_list = ", ".join(subpages)
        print(f"  {fname:50s}  missing: {subpage_list}")

    print(f"\nTo fetch these subpages, run the extractor for each missing page title.")
    print(
        f"Example: the subpage 'table' for 'Allomancy' lives at title 'Allomancy/table'."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Process Coppermind raw wikitext into Obsidian Markdown.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--scan-gaps",
        action="store_true",
        help="Scan the vault for files with missing transcluded subpage content.",
    )
    parser.add_argument(
        "--file",
        metavar="FILENAME",
        help="Process a single file instead of the whole input directory.",
    )
    args = parser.parse_args()

    if args.scan_gaps:
        scan_gaps(OUTPUT_DIR)
        return

    if args.file:
        files = [args.file]
        single = True
    else:
        files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".txt")]
        single = False

    print(f"--- Processing {len(files)} file(s) from '{INPUT_DIR}' ---")
    processed, failed = 0, 0

    for filename in files:
        try:
            filepath = (
                os.path.join(INPUT_DIR, filename)
                if not single
                else os.path.join(INPUT_DIR, filename)
            )
            with open(filepath, "r", encoding="utf-8") as f:
                raw_content = f.read()

            processed_md = process_to_obsidian(raw_content, filename)

            output_name = filename.replace(".txt", ".md")
            with open(
                os.path.join(OUTPUT_DIR, output_name), "w", encoding="utf-8"
            ) as f:
                f.write(processed_md)

            processed += 1
            if not single and processed % 100 == 0:
                print(f"  Processed {processed} files...")

        except Exception as e:
            print(f"  [ERROR] Failed to process {filename}: {e}")
            failed += 1

    print(
        f"--- Done: {processed} processed, {failed} failed. Output in '{OUTPUT_DIR}' ---"
    )


if __name__ == "__main__":
    main()
