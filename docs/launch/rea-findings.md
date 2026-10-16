# Findings: live website page before the update

Target: `https://rarecreatures.app/apps/rh-bot/` (the owner's own public page). Method: public
browser observation (page text). REA's web tools need a Chrome DevTools endpoint on loopback;
none was running and starting one was out of scope, so REA was **BLOCKED** and the built-in
browser was used instead. Observed 2026-10-15.

| Finding | Verified against code | Action |
|---|---|---|
| "the Claude review, the risk manager, and then real orders" | False today: live `submit()` raises `NotImplementedError` | Reworded: paper only; real-order code unfinished and disabled |
| "whether it runs on paper or on real money is your call" | Real money is not available | Reworded with the user-risk disclaimer |
| "will not surface a candidate unless at least two strategies independently like it" | Outdated: strategies marked standalone (and launcher runs) trade alone; rule-based ones skip Claude | Reworded |
| Lists Form 4 insider, congressional, 13F, options flow, copy-trading as signals | Code exists but these are off and need paid data feeds | Labeled as off / needs data |
| "A $250 cash account cannot short" | Personal account detail | Generalized to small cash accounts |
| "Free and open source" | Not true until the public repo exists | Kept; links to the public repo once published |
| "Hard $60 limit", "three separate flags", "paper and live never share a process", "build fails if paper code can reach an order-placing tool" | True in code and tests | Kept |
| Name "RH bot" | Renamed | CTbot (Creature Trading Bot); old URL redirects |
