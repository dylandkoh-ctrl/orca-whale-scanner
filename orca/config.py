"""Central configuration for Orca.

Everything tunable lives here so the rest of the code reads cleanly and the
Streamlit sidebar can override these defaults at runtime.

SCOPE: single-match bets for a given day — moneyline ("USA Win: No"), totals
("O/U 2.5: Over"), both-teams-to-score, spreads — NOT tournament futures.
"""

# --- API hosts (all free, public, no auth for reads) ---
GAMMA_HOST = "https://gamma-api.polymarket.com"
CLOB_HOST = "https://clob.polymarket.com"
DATA_HOST = "https://data-api.polymarket.com"

# --- Discovery scope ---
# All World Cup events carry this Gamma tag; we page through it and keep the
# events that kick off on the target date.
WC_TAG_ID = "102232"          # "FIFA World Cup"
DISCOVERY_MAX_EVENTS = 600    # safety cap on tag pagination

# Which per-match event families to scan. Keys are matched against the suffix
# after the team names in an event title (the base moneyline event has no
# suffix -> "Moneyline"). Flip these on/off to widen or narrow coverage.
# Player Props / Corners are huge (200+ markets each) so they're off by default.
MARKET_GROUPS = {
    "Moneyline": True,        # Will <team> win? / draw?  (Yes/No)
    "More Markets": True,     # spreads, O/U totals, both-teams-to-score
    "Halftime Result": False,
    "Second Half Result": False,
    "First Team to Score": False,
    "Exact Score": False,
    "Total Corners": False,
    "Player Props": False,
}

# --- Detection thresholds (USD) ---
# Calibrated to single-match scale (moneyline whales run $400K-$1M; totals/props
# top out near $50-90K per wallet). All editable live in the sidebar.
# Trigger A: any single wallet >= this on one bet.
SINGLE_WHALE_USD = 100_000
# Trigger B: combined exposure on one bet >= this, counting only wallets that
# individually clear the noise floor.
COMBINED_USD = 200_000
NOISE_FLOOR_USD = 10_000
# Trigger C: conviction clustering — >= CLUSTER_MIN_WALLETS wallets each
# >= CLUSTER_WALLET_USD on the *same* bet.
CLUSTER_WALLET_USD = 25_000
CLUSTER_MIN_WALLETS = 3
# Trigger D (held for a later phase): single live fill >= this on /trades.
LARGE_PRINT_USD = 50_000

# --- Fetch behaviour ---
HOLDERS_LIMIT = 100          # holders per token to pull from /holders
HTTP_TIMEOUT = 20.0          # seconds
HTTP_MAX_RETRIES = 4         # exponential backoff attempts on 429/5xx
HTTP_MAX_WORKERS = 12        # concurrent requests (holders + profiling)
# Profile only the largest flagged wallets, not every wallet over the noise
# floor — the long tail of small holders adds little and costs 3 calls each.
PROFILE_MAX_WALLETS = 40
DISCOVERY_TTL = 600          # seconds to cache the event list (changes slowly)
HOLDERS_TTL = 60             # seconds to cache a market's holders
PROFILE_TTL = 300            # seconds to cache a wallet profile

# A wallet whose total Polymarket portfolio value exceeds this is treated as a
# market-maker / Polymarket system wallet rather than a real bettor (the negRisk
# maker shows ~$16B; real whales are single-digit millions). Such wallets hold
# every outcome and have empty public profiles, so they're hidden by default.
MAKER_VALUE_USD = 50_000_000

# --- Persistence ---
DB_PATH = "orca.db"
