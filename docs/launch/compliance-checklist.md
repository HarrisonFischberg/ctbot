# Launch-readiness checklist (20 safeguards)

Engineering checks only. PASS means the listed check passed, not legal approval. Scope: the
CTbot repository (local software) and its page on rarecreatures.app. Owner: Rare Creatures LLC.
Checked 2026-10-15.

| # | Safeguard | Status | Applicability and evidence |
|---|---|---|---|
| 1 | Privacy policy | NEEDS REVIEW | `PRIVACY.md` drafted from the actual data flows (`docs/launch/data-inventory.md`); linked from README and dashboard footer; site privacy page section added. Needs legal review. |
| 2 | Terms of service | NEEDS REVIEW | `TERMS.md` + `DISCLAIMER.md` + MIT `LICENSE`. No accounts, so no assent capture. Liability, indemnity and governing-law language needs legal review. |
| 3 | Refund policy | NOT APPLICABLE | Free software; no payments, subscriptions or trials (`TERMS.md` §5 says so). |
| 4 | Cookie policy | PASS | Dashboard and site set no cookies (`grep` of `src/`, site privacy page). localStorage keys (`ctbot.*`, site `theme`) listed in `PRIVACY.md` and the site privacy page. |
| 5 | Cookie consent banner | NOT APPLICABLE | No optional tracking, analytics, pixels or third-party scripts in the dashboard or the site; only functional preference storage. |
| 6 | Form consents | PASS | Forms: Settings key fields (purpose stated, values never echoed), Invest sheet (amount, max loss with a "No limit" option, take profit default "No target"), confirm dialogs. No marketing or optional consents; nothing prechecked. |
| 7 | No unnecessary data | PASS | Dashboard shows only the account's last 4 digits; Claude receives no account identifiers; logs and data scanned for key/token values: none (2026-10-15). |
| 8 | Audit third-party SDKs | PASS | Inventory in `docs/launch/asset-licenses.md`; outbound hosts: Robinhood MCP, Anthropic, Collective2 only. `pip-audit` 2.10.1 on 2026-10-15 over every installed package (incl. optional research extras): no known vulnerabilities. vectorbt Commons Clause noted. |
| 9 | Remove dark patterns | PASS | Invest/Stop/Settings/Disconnect reviewed: plain labels, one confirm to stop, no urgency or guilt copy, summary shown before investing, defaults favour safety (15% max loss, removable). |
| 10 | Remove hidden fees | PASS | README "What it costs": Anthropic usage, Robinhood, Collective2. CTbot itself is free. Dashboard header shows Claude reviews used vs the daily cap. |
| 11 | Remove fake reviews | PASS | No testimonials, ratings, logos or user counts in repo, dashboard or site page. |
| 12 | Remove unsupported claims | PASS | `docs/launch/claims-evidence.md`; simulated returns labeled as simulated and not a promise; site claims checked against code; "Linux" qualified as untested. |
| 13 | Accessibility alt text | PASS | Decorative logo `aria-hidden`; charts have `role="img"` + `aria-label`; icon-only buttons (move handle, close) have accessible names. Rendered check: see `docs/launch/activity.md`. |
| 14 | Fix color contrast | PASS | Failing pairs fixed in both themes (muted text, light-theme green/amber/red, text on green buttons); pairs re-checked with Replica `contrast.py` (all AA). Rendered check in activity log. |
| 15 | Keyboard navigation | PASS | Skip link; tabs and buttons are native; Invest sheet focuses the amount, keeps Tab inside, Escape closes, focus returns to the opener; cards move with arrow keys on the ⠿ handle. |
| 16 | Add business details | PASS | Rare Creatures LLC, Colorado, hello@rarecreatures.app in README, LICENSE, legal docs and site. No residential address. |
| 17 | Age consent for kids' data | PASS | For adults (18+) only, stated in DISCLAIMER, TERMS, PRIVACY; brokerage accounts require adults; the publisher collects no data from anyone. |
| 18 | Unsubscribe link in emails | NOT APPLICABLE | CTbot sends no email; the publisher keeps no mailing list for it. |
| 19 | License fonts and images | PASS | System fonts only, no images; dependency licenses inventoried; MIT LICENSE added. |
| 20 | Data deletion request | PASS | No publisher-held data. Local deletion: Settings → Remove (keys), Settings → Disconnect (deletes Robinhood token; tested in `tests/test_settings.py`), folder deletion steps in `PRIVACY.md`. |
