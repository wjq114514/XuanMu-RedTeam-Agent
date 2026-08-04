---
name: web-archive-intel
description: Collect bounded passive URL history from Wayback CDX and Common Crawl for one exact in-scope URL prefix.
---

# web-archive-intel

Use this skill to collect historical URL metadata for one explicitly authorized HTTP(S) URL prefix. Collection is passive and does not fetch archived page content or probe discovered URLs.

## Help First

```sh
python3 .agents/skills/web-archive-intel/scripts/collect.py --help
```

## Usage

```sh
python3 .agents/skills/web-archive-intel/scripts/collect.py https://example.com/ \
  --output .xuanmu/outputs/osint/example-archive-intel
```

For repeatable offline parsing, provide `wayback.json`, `commoncrawl-indexes.json`, and `commoncrawl.ndjson` fixtures:

```sh
python3 .agents/skills/web-archive-intel/scripts/collect.py https://example.com/ \
  --output .xuanmu/outputs/osint/example-archive-intel --fixtures /tmp/archive-fixtures
```

## Output

The output directory contains untouched CDX responses under `raw/`, a deterministic versioned `manifest.json` with normalized URLs, hosts, paths, captures, and raw-file hashes, and a compact `summary.json` with bounded high-signal URLs. Read only `summary.json`. In a WorkProject, pass the manifest path directly to `import_osint_manifest`; do not read the manifest or stream raw responses into the conversation.
