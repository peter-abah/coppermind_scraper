# coppermind-vault

Downloads the [Coppermind wiki](https://coppermind.net) and converts it into a structured Obsidian-ready Markdown vault. Built as a lore reference and grounded knowledge base for an AI agent building a Cosmere simulation game.

## Requirements

- [uv](https://docs.astral.sh/uv/)
- Python 3.11+

No setup needed — `uv run` handles dependencies from the inline `# /// script` headers.

## Usage

```bash
# First time — full download (~7600 pages, 20–30 min) then process
uv run extractor.py
uv run processor.py

# Weekly update
uv run extractor.py --since last
uv run processor.py

# Single file (useful when tweaking the processor)
uv run processor.py --file "Allomancy.txt"

# Find pages with missing transcluded subpage content
uv run processor.py --scan-gaps
```

### Extractor flags

| Flag | Description |
| --- | --- |
| *(none)* | Full download, skips existing files, resumes if interrupted |
| `--since DATE` | Only fetch pages changed since `DATE` (`YYYY-MM-DD`, ISO 8601, or `last`) |
| `--force` | Re-download even if local file already exists |

### Processor flags

| Flag | Description |
| --- | --- |
| *(none)* | Process all files in `coppermind_raw/` |
| `--file FILENAME` | Process a single file |
| `--scan-gaps` | Report files with missing transcluded subpage content |

## Output format

Each file gets YAML frontmatter extracted from the page infobox, plus a `links:` list of every `[[wikilink]]` target for Obsidian graph traversal and agent context chaining.

Inline source tags are preserved in prose so an agent can distinguish canon sources:

| Tag | Source |
| --- | --- |
| `[MB1]`–`[MB3]` | Mistborn Era 1 |
| `[MB4]`–`[MB7]` | Mistborn Era 2 |
| `[SA1]`–`[SA5]` | Stormlight Archive |
| `[WoB]` | Word of Brandon — author Q&A, not in-book |
| `[MB1-ep]` | Epigraph — in-book but as an in-world document |

Add new book codes to the `BOOK_CODES` dict in `processor.py` if needed.

## Directory structure

```
extractor.py
processor.py
coppermind_raw/          # gitignored — raw .txt wikitext
coppermind_vault/        # gitignored — processed .md files
extractor_state.json     # gitignored — resume state
extractor_last_run.json  # gitignored — timestamp for --since last
```

## .gitignore

```gitignore
coppermind_raw/
coppermind_vault/
extractor_state.json
extractor_last_run.json
__pycache__/
.venv/
```

---

Data from [Coppermind](https://coppermind.net), licensed [CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/). This repo contains no wiki content.
