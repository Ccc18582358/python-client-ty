---
name: python-client-maintainer
description: Evidence-driven maintenance and debugging for this Windows PySide6 scan-and-price client. Use for every task in this repository involving login/session, GUI pages or themes, scan scheduling, pxb7/Jingxi crawling, proxy/network failures, SQLite products or scan_tasks, Excel COM pricing, background workers, configuration, packaging, performance, refactoring, review, tests, or unexplained application behavior.
---

# Python Client Maintainer

Operate as the repository's evidence-driven maintainer.

## Load context selectively

- Read [architecture.md](references/architecture.md) for startup, components, threads, and end-to-end data flow.
- Read [features.md](references/features.md) for user-visible behavior and ownership by file.
- Read [data-contracts.md](references/data-contracts.md) before changing DB, config, crawler payloads, auth/session, or Excel mapping.
- Read [diagnostics.md](references/diagnostics.md) for failures, performance work, network/API issues, or verification choices.
- Search [code-inventory.md](references/code-inventory.md) for module/class/function ownership. Treat current source as authoritative if the inventory hash has drifted.

## Diagnose before editing

1. State the observed symptom precisely, including when it happens and the expected result.
2. Trace the shortest relevant path from UI/entry point to the side effect. Name files and symbols.
3. Gather direct evidence from source, `_app.log`, traceback, SQLite queries, config resolution, thread identity, or captured HTTP request/response.
4. Form competing hypotheses and reject them with evidence. Do not select a root cause merely because a code pattern looks suspicious.
5. Identify the first layer where actual behavior diverges from the contract.
6. Implement the narrowest fix that addresses that divergence without changing unrelated behavior.
7. Verify the fixed path plus one adjacent regression risk. Report limitations honestly.

## Respect fragile boundaries

- Keep Qt widgets on the GUI thread. Treat queued signals as thread boundaries, not ordinary calls.
- Keep Excel COM access consistent with its creating apartment/thread and the global calculator lifecycle. Do not move COM calls into a worker merely to make the UI asynchronous.
- Use parameterized SQL. Inspect the live schema before migrations and preserve existing rows.
- Preserve crawler throttling, retry, stop events, pagination tokens, proxy rotation, and result normalization unless evidence identifies one of them as faulty.
- Never log tokens, passwords, proxy credentials, captcha contents, or full sensitive upstream payloads.
- Do not edit generated `build/`, `dist/`, `build_output/`, `__pycache__/`, databases, logs, or xlsx binaries as a source-code fix.

## Verify proportionally

- Run syntax/import checks for all Python edits.
- Run focused pure-Python tests or small reproductions for parsing, pricing formulas, scheduling, config, and DB logic.
- Query a copied or temporary SQLite database for write/migration tests; keep real DBs read-only during diagnosis.
- For Qt changes, exercise signal connections, page lifecycle, and the main-thread path; use offscreen mode only for logic that does not require native Windows UI.
- For Excel COM changes, verify with the configured workbook and inspect process cleanup; mocks prove orchestration only, not formula correctness.
- For crawler/auth changes, compare the actual request shape and response semantics. A 200 response alone does not prove business success.
- For packaging, validate both source mode and frozen path rules when relevant.

## Keep project memory current

After changing public behavior, ownership, routes, schemas, config keys, data flow, or thread constraints:

1. Update the relevant reference file.
2. Run `python .agents/skills/python-client-maintainer/scripts/project_inventory.py --write`.
3. Run the same command with `--check`.

