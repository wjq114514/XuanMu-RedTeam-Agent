---
name: nmap
description: Staged Nmap workflow for host discovery, TCP and UDP scanning, service detection, targeted NSE checks, durable XML output, and project asset handoff.
---

# Nmap Reconnaissance

Use Nmap as a staged measurement tool. Start with the smallest scan that answers the current question, preserve durable output, and expand only from observed results or an explicit task requirement.

## Required Inputs

Before scanning, establish:

- exact target IPs, CIDRs, or hostnames from declared project scope
- whether discovered addresses may only be recorded or may also be actively scanned
- required protocols and expected depth
- scan time budget and command timeout
- output directory and unique scan basename
- responsible role: CIE for discovery and inventory, CPE for vulnerability-oriented validation

Do not silently expand from a hostname, certificate, redirect, DNS answer, or discovered host into active scanning outside declared scope.

## Environment Preflight

Run bounded checks before the first scan:

```sh
id
nmap --version
ip route get TARGET
```

Use the installed Nmap version and `nmap --help` when an option is uncertain. Confirm the selected interface and route for multi-homed hosts.

- With raw-socket privileges, prefer `-sS` for TCP discovery scans.
- Without raw-socket privileges, use `-sT`; do not repeatedly retry a privileged scan.
- Use `-n` for address scans unless DNS resolution is part of the task. Record hostname resolution separately when it matters.

## Output Contract

Every material scan must use a unique task-scoped basename with `-oA`:

```sh
mkdir -p .xuanmu/outputs/nmap/JOB
nmap ... -oA .xuanmu/outputs/nmap/JOB/STAGE TARGETS
```

This produces normal, grepable, and XML reports. Record the exact command, Nmap version, start time, exit code, target set, and output basename.

Only treat XML as importable when the command completed successfully and the file ends with a complete `</nmaprun>` document. A partial XML file is evidence of an interrupted stage, not a completed inventory.

## Stage 1: Host Discovery

Do host discovery separately for multi-host scope.

- On a directly connected Ethernet network, use ARP discovery where applicable: `-sn -PR --reason`.
- On routed networks, use a bounded combination of ICMP and TCP discovery suited to the environment, such as `-sn -PE -PS22,80,443 -PA80,443 --reason`.
- Classify results as `up`, `down`, or `inconclusive`; do not equate no response with a confirmed down host.
- Do not apply `-Pn` automatically to an entire CIDR. Use it for an explicit host or a reviewed set when discovery is known to be filtered.

For a single explicitly assigned host, a bounded `-Pn` scan is acceptable when host discovery would not answer the task.

## Stage 2: Initial TCP Inventory

Scan common TCP ports first on confirmed or explicitly assigned hosts:

```sh
nmap -sS -n --top-ports 1000 --reason -T3 --max-retries 2 --host-timeout 8m -oA BASENAME TARGETS
```

Use `-sT` instead of `-sS` when raw sockets are unavailable. Keep timing at `-T3` by default. Increase timing or rates only when the task and network conditions justify it.

Do not use `--min-rate` as a default. It can reduce result quality on filtered or lossy networks and creates unnecessary traffic.

## Stage 3: Full TCP Coverage

Run `-p-` only when full TCP coverage is required or the initial inventory indicates that broader coverage is valuable.

- Prefer one host or a small reviewed batch per command.
- Keep `--max-retries` and `--host-timeout` explicit.
- Split a large target set instead of increasing timing until one command becomes unbounded.
- Preserve filtered-port summaries and timeout details as coverage evidence.

If a full scan times out, rerun only the incomplete host or port range. Do not restart completed targets.

## Stage 4: Service And Version Detection

Run service detection only against observed open ports:

```sh
nmap -sV --version-light -n -p PORTS --reason --host-timeout 8m -oA BASENAME TARGETS
```

- Build `PORTS` from the prior stage instead of rescanning all ports.
- Use default version intensity first; increase intensity only for unresolved services that matter.
- Treat product and version strings as fingerprints, not proof, until corroborated.
- Run OS detection separately and only when needed. Record low accuracy or insufficient-port warnings rather than forcing a conclusion.

## Stage 5: UDP

UDP is expensive and ambiguous. Start with service-driven ports or a bounded top-port set:

```sh
nmap -sU -n --top-ports 50 --reason --version-light --max-retries 2 --host-timeout 8m -oA BASENAME TARGETS
```

Interpret states precisely:

- `open`: an application response supports an open classification.
- `open|filtered`: no definitive response; do not report it as open or closed.
- `closed`: an ICMP unreachable response supports a closed classification.
- `filtered`: filtering evidence exists, but service state is not known.

Expand UDP coverage only for relevant hosts or protocols. Do not run broad TCP and UDP scans concurrently by default.

## Stage 6: NSE

Select NSE scripts from confirmed service context.

1. CIE may use bounded discovery, banner, and safe/default scripts for inventory.
2. CPE owns `vuln`, authentication, brute-force, exploit-oriented, and intrusive checks.
3. Prefer exact script names or a narrow reviewed expression over broad categories.
4. Set `--script-timeout` explicitly and provide required script arguments explicitly.
5. Never use `--script=all` as a default workflow.
6. A script label such as `VULNERABLE` is a suspected finding until its preconditions, target identity, output evidence, and false-positive limits are reviewed.

Run scripts against the specific ports and services they apply to. Do not repeat service discovery across unrelated ports merely to run one script.

## Recovery And Retesting

When a stage fails or times out:

- retain its normal and XML artifacts, but mark incomplete XML as non-importable
- identify whether the cause was permissions, route, DNS, filtering, rate, retries, host timeout, script timeout, or process interruption
- narrow the next command to the affected target, protocol, port range, or script
- downgrade `-sS` to `-sT` only for missing raw-socket permission
- do not change several timing and discovery controls at once; keep the retest interpretable

## Project Handoff

After each completed stage:

- record network and domain assets separately from host services
- preserve TCP and UDP service identities distinctly
- create domain `resolves_to` network and network `hosts` service relationships when supported
- keep newly discovered assets marked as discovered; do not silently promote them into active scope
- reference output basenames instead of copying large raw output into assets or blackboard nodes
- distinguish confirmed open services, ambiguous states, useful negatives, timeouts, and untested coverage

For project coordination, create an Intent before the scan and a linked Fact after interpreting the result. CIE hands service-specific validation leads to CPE rather than confirming vulnerabilities from reconnaissance alone.
