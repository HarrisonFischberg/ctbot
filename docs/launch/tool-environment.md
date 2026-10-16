# Tool environment (2026-10-15)

| Tool | Available | Version / how verified | Used for |
|---|---|---|---|
| Caveman | Yes | plugin `caveman` 3.2.0 (`installed_plugins.json`) | Status messages only; never for legal or consent text |
| Replica | Yes | plugin `replica-skill` 1.0.0 | `replica-brand/sweep.py` (rename leftovers and personal details in published files: 0 hits); `replica-design/contrast.py` (token pairs). Recon/architect/build stages not applicable: existing app, no reference app to rebuild |
| REA | BLOCKED | `rea` MCP tools respond, but web tools need a loopback Chrome DevTools endpoint and none was running | Built-in browser used for the public page observation instead (`docs/launch/rea-findings.md`) |
| `/goal` | Not verified | Not used | Work ran as one supervised session at the owner's direct request; no autonomous loop |
| Ralph loop | Partly | `ralph-loop` plugin 1.0.0 (same-context); fresh-context quickstart not cloned | Not used, for the same reason |
| fixing-accessibility | Yes | `~/.claude/skills/fixing-accessibility` (MIT) | Edit-time rules: dialog focus, accessible names, skip link |
| web-quality-audit | Yes | `~/.claude/skills/web-quality-audit` (MIT) | Rendered-page checks in the built-in browser (contrast of every visible text element, keyboard flow). Lighthouse not run |
| ui-ux-pro-max | Yes | `~/.claude/skills/ui-ux-pro-max` | Not needed: existing design system kept |
