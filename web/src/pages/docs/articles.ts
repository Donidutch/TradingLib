/* The Documentation articles, copied from ui/js/pages/docs.js (static text by
 * design, SPEC §6); only the URL list in "Keyboard shortcuts" names this
 * app's routes. Blocks are ["h3" | "p", text] or ["ul", items]. */

export type Block = readonly ["h3" | "p", string] | readonly ["ul", readonly string[]];

export interface Article {
  id: string;
  group: "Methodology" | "Product guide";
  lead: string;
  detail: string;
  title: string;
  lede: string;
  body: readonly Block[];
}

export const ARTICLES: readonly Article[] = [
  {
    "id": "hypotheses",
    "group": "Methodology",
    "lead": "Hypotheses and experiments",
    "detail": "How evidence becomes reproducible research.",
    "title": "Hypotheses and experiments",
    "lede": "Research in TradingLib is a chain of records, not a notebook. Every claim starts as a hypothesis, is tested by experiments, and each experiment run is stored — including the ones that failed.",
    "body": [
      [
        "h3",
        "The chain"
      ],
      [
        "ul",
        [
          "A hypothesis states a mechanism and the condition that would falsify it. It lives in the Strategy Lab and carries a status (draft, testing, supported, rejected).",
          "An experiment is a concrete test of one hypothesis: a symbol set, a date range, a signal specification and the parameters being swept.",
          "Every run of an experiment is persisted with its parameters and its result, so the number of trials behind a finding is a count, not an estimate.",
          "Evidence can be attached to a hypothesis; the record keeps what supported it and what contradicted it."
        ]
      ],
      [
        "h3",
        "Why every trial is logged"
      ],
      [
        "p",
        "The datastrip's TRIALS LOGGED figure counts the experiment and backtest runs in the database. It exists so that the deflated Sharpe ratio in validation has a real trial count to deflate by: a strategy selected from many attempts must clear a higher bar than one tested once, and that only works if the attempts are counted honestly."
      ],
      [
        "h3",
        "Where to look"
      ],
      [
        "p",
        "Strategy Lab → Hypotheses and Experiments. The API behind them is /api/lab/hypotheses and /api/lab/experiments."
      ]
    ]
  },
  {
    "id": "backtests",
    "group": "Methodology",
    "lead": "Backtest integrity",
    "detail": "Costs, leakage, benchmark, and OOS rules.",
    "title": "Backtest integrity",
    "lede": "A backtest is only useful if it could not have known the future and did not trade for free. The backtester enforces both, and reports what it assumed.",
    "body": [
      [
        "h3",
        "Point-in-time data"
      ],
      [
        "ul",
        [
          "Universes store each member's added and removed dates, so a run over 2018 uses the membership of 2018 — including companies that were later delisted or acquired. Market & Data → Universes shows exactly which names that adds back.",
          "Snapshots freeze a membership on a date. When a snapshot exists for the as-of date it is used instead of reconstructing from membership dates, and the UI says which of the two you are looking at.",
          "Fundamentals are stored as reported; where a vendor only publishes restated values the data-quality page reports that rather than pretending they are point-in-time."
        ]
      ],
      [
        "h3",
        "Costs"
      ],
      [
        "p",
        "A run applies commission, spread and slippage assumptions and reports them with the result. A backtest with no cost model is not comparable to one with costs, so the assumptions travel with the record instead of living in someone's memory."
      ],
      [
        "h3",
        "Benchmark and out-of-sample"
      ],
      [
        "ul",
        [
          "Every backtest carries a benchmark (SPY by default; change it in Settings → Research defaults) and reports excess return against it, because a strategy that trails buy-and-hold is not a strategy.",
          "The in-sample/out-of-sample split is part of the run configuration, and validation gates read the out-of-sample statistics, never the in-sample ones."
        ]
      ],
      [
        "h3",
        "Where to look"
      ],
      [
        "p",
        "Strategy Lab → Backtests. The equity curve, the benchmark line and the run's statistics come from /api/lab/backtests/{id}/results."
      ]
    ]
  },
  {
    "id": "validation",
    "group": "Methodology",
    "lead": "Validation gates",
    "detail": "Promotion requirements and statistical tests.",
    "title": "Validation gates",
    "lede": "Promotion is a decision with a checklist. The gate suite is evaluated against a finished backtest and a strategy is promoted only when every gate passes.",
    "body": [
      [
        "h3",
        "What a gate is"
      ],
      [
        "p",
        "A gate is a named threshold applied to one statistic of a run — an out-of-sample Sharpe floor, a maximum drawdown ceiling, a minimum number of trades, a deflated Sharpe floor that accounts for how many trials were logged. The live suite, with each gate's threshold, is served by /api/lab/validation/gates; the page shows the suite this build actually applies rather than a picture of one."
      ],
      [
        "h3",
        "Why the trial count matters"
      ],
      [
        "p",
        "Testing enough variations will always produce one that looks good. The deflated Sharpe ratio discounts a result by the number of trials that produced it, which is why every experiment and backtest run is recorded even when it is abandoned."
      ],
      [
        "h3",
        "Promotion"
      ],
      [
        "p",
        "Promoting a strategy records who promoted it and against which report. After promotion the evaluation workspace tracks it forward and reports decay, so a strategy that stops working is visible rather than assumed to still hold."
      ],
      [
        "h3",
        "Where to look"
      ],
      [
        "p",
        "Strategy Lab → Validation and Evaluation."
      ]
    ]
  },
  {
    "id": "shortcuts",
    "group": "Product guide",
    "lead": "Keyboard shortcuts",
    "detail": "Command palette, navigation, and tables.",
    "title": "Keyboard shortcuts",
    "lede": "The console is navigable from the keyboard. These are the bindings this build actually registers — nothing here is aspirational.",
    "body": [
      [
        "h3",
        "Command palette"
      ],
      [
        "ul",
        [
          "⌘K / Ctrl-K — open or close the palette from anywhere.",
          "Type a ticker (for example NVDA) and press Enter to open Stock Research for it. Any ticker the data providers know will load.",
          "↑ / ↓ move the selection, Enter runs it, Escape closes the palette.",
          "The palette searches the real backend: workspaces and tabs, universes and watchlists (including by a symbol they contain), saved screens, and validated ticker symbols. \"Open active backtest\" asks the lab which run is active and opens it."
        ]
      ],
      [
        "h3",
        "Navigation"
      ],
      [
        "ul",
        [
          "Every page has a URL: /app/ (add ?symbol=NVDA to centre a ticker), /app/stock/NVDA, /app/docs/shortcuts, /app/settings. Pages not ported to this app yet keep their address in the vanilla console: /#tl/lab/backtests, /#agents, /#data/ingestion. They are shareable and the browser's Back button works.",
          "Tables and lists are focusable: Tab moves between rows, Enter or Space selects the focused row (the collections table on Market & Data is driven this way)."
        ]
      ],
      [
        "h3",
        "PICK mode"
      ],
      [
        "p",
        "The PICK control in the topbar turns panels into selectable items and Export builds a manifest of what you marked, so a set of panels can be handed to someone else exactly as you saw it."
      ]
    ]
  },
  {
    "id": "autonomy",
    "group": "Product guide",
    "lead": "Agent autonomy",
    "detail": "What agents can do and where humans approve.",
    "title": "Agent autonomy",
    "lede": "Agents run the research pipeline with real tools and a real budget. Everything they do is recorded, and the actions that matter stop for a human.",
    "body": [
      [
        "h3",
        "What an agent can do"
      ],
      [
        "ul",
        [
          "Call the library itself: run a screen, fetch data, run a backtest, save a finding. The tools are the same functions the UI calls, so an agent cannot reach data a person could not.",
          "Spend money, but only within the configured budget. The SPEND figure in the datastrip is the real accumulated cost of model calls against the daily budget, and a run that would exceed the budget is stopped rather than trimmed."
        ]
      ],
      [
        "h3",
        "What an agent cannot do"
      ],
      [
        "ul",
        [
          "Place a live order. Trading is paper or sandbox only, and no agent path reaches a live broker.",
          "Pass a Human Approval stage on its own. A pipeline that reaches one blocks until somebody approves or rejects it in Agent Center → Approval Review; rejecting ends the run."
        ]
      ],
      [
        "h3",
        "The audit log"
      ],
      [
        "p",
        "Every stage execution, tool call, artifact and approval decision is appended to the audit log. It is append-only: it answers \"what did this agent actually do\", which is the question that matters after the fact."
      ],
      [
        "h3",
        "Where to look"
      ],
      [
        "p",
        "Agent Center → Pipeline, Approval Review and Audit Log."
      ]
    ]
  }
];
