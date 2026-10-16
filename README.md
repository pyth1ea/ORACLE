# ORACLE — Quick Start

**What it is:** a personal forecasting system. Every morning around 7 AM, Claude runs ORACLE's daily cycle:
it pulls the news and economic data, scores the day's events, runs the agent simulation, checks which past
predictions came true, updates every forecast, writes a briefing and refreshes the dashboard.

## Daily use
- **On your phone:** open https://claude.ai/artifact/WeU1NVenCGHXuHv8jVjpuM while signed in to claude.ai. It's private
  to you and republished after every morning run. Use "Add to Home Screen" so it opens like an app.
- **On this PC:** double-click `dashboard/index.html`.
- **Today's briefing:** the top of the dashboard, or the newest file in `briefings/`.

## Reading modes
Use the switch at the top of the dashboard:
- **Plain English** (default): everyday words, a "How to read this page" guide, and the briefing's plain-English summary first.
- **Guided:** full detail, with dotted words you can tap or hover for a definition.
- **Technical:** full detail with all formulas and scores, no definitions.
Every mode has a **Glossary** at the bottom.

## How ORACLE thinks
- **OMEN** measures **pressure** (anger, chaos, economic pain pushing for change).
- **CLIO** measures **friction** (laws, institutions, history resisting change).
- **ORACLE** predicts where pressure overcomes friction.

| On the dashboard | What it means |
|---|---|
| **Timeline volatility** ("How shaky are things?") | One number, V (0 = stable, 1 = collapse) = OMEN's push ÷ (push + CLIO's hold). Steady (Alpha) < 0.40 < Tense but holding (Gamma) < 0.60 < Breaking point (Beta) < 0.75 < Collapse (Omega). The **historical scale** puts 20 past eras (1861, 1929–33, 1968, 1979, 2008, 2020, the French Revolution, Rome…) on the same ruler, so you can see which moment today resembles. The **shock watch** turns V into a checkable 7-day forecast that gets graded automatically. |
| **Timeline** | A real time axis. Open predictions sit where they resolve; ✓/✗ marks show past calls that were right or wrong; diamonds are catalysts. Toggle each type on or off. |
| **Systems** | Pieces of the status quo. The blue wall is friction, the orange ball is pressure. Statuses: Holding → Strained → On the edge → Tipping. |
| **Catalyst registry** | Upcoming moments (Fed meetings, data releases, the election, Iran's decision) with the odds of each outcome and IF/THEN effects. |
| **Predictions** | Specific yes/no questions. Blue = CLIO, orange = OMEN, black bar = ORACLE's final call, yellow square = Sandbox, ring = prediction markets. |
| **Agent ecosystem** | The Sandbox's 11 simulated actors (swing voters, both party bases, protesters, Wall Street, the Fed, courts, Iran…) and where they're heading. |
| **7-day review** | What resolved this week and which engine was closer, the biggest forecast moves, live numbers vs. predictions. |
| **Scorecard** | Accuracy (Brier score: 0 = perfect, 0.25 = coin flip). Engine weights shift toward whichever engine keeps being right. |

## Requirements for the daily run
- Your PC must be on and the **Claude desktop app open** at 7 AM. If it isn't, the run happens the next time you open the app.
- Python 3.12 is installed for your user account (`%LOCALAPPDATA%\Programs\Python\Python312`). Don't uninstall it.
- Manage the schedule under **Scheduled** in the Claude app sidebar ("ORACLE daily forecast run").

## Folder guide
| Path | Purpose |
|---|---|
| `ORACLE.md` | The rulebook every run follows |
| `data/oracle.json` | Predictions, systems, catalysts, weights, scores (the main database) |
| `data/events.json` | Scored news events |
| `data/model.json` | The Sandbox's agents and settings |
| `data/feed/`, `data/metrics.json` | Daily news shortlist and raw economic numbers |
| `clio/`, `omen/` | CLIO's history library, OMEN's risk watchlist |
| `briefings/` | One briefing per day |
| `scorecard.md` | Accuracy report (generated automatically) |
| `tools/` | `feed.py` (news + data), `sim.py` (Sandbox), `core.py` (all the math), `build.py` (dashboard) |

Not financial advice.
