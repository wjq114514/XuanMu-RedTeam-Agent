---
name: passive-domain-intel
description: Collect bounded passive DNS, RDAP, certificate-transparency, subdomain, and IP intelligence for one exact in-scope domain.
---

# passive-domain-intel

Use this skill for free-source, passive collection against one explicitly authorized domain. It does not brute-force names or probe discovered hosts.

## Help First

```sh
python3 .agents/skills/passive-domain-intel/scripts/collect.py --help
```

## Usage

```sh
python3 .agents/skills/passive-domain-intel/scripts/collect.py example.com \
  --output .xuanmu/outputs/osint/example-domain-intel
```

For repeatable offline parsing, provide a fixture directory containing any of `dns_A.txt`, `dns_AAAA.txt`, `dns_CNAME.txt`, `dns_MX.txt`, `dns_NS.txt`, `dns_SOA.txt`, `dns_TXT.txt`, `rdap.json`, and `crtsh.json`:

```sh
python3 .agents/skills/passive-domain-intel/scripts/collect.py example.com \
  --output .xuanmu/outputs/osint/example-domain-intel --fixtures /tmp/domain-fixtures
```

## Output

The output directory contains untouched response bodies and command output under `raw/`, a deterministic versioned `manifest.json` with normalized records and raw-file hashes, and a compact `summary.json` with bounded highlights. Read only `summary.json`. In a WorkProject, pass the manifest path directly to `import_osint_manifest`; do not read the manifest or stream raw responses into the conversation.
