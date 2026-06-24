# Polymarket + Kalshi Arbitrage Scanner

<div align="center">

![Python](https://img.shields.io/badge/Python-3.10+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)
![Status](https://img.shields.io/badge/Status-Active-brightgreen.svg)
![Platforms](https://img.shields.io/badge/Platforms-Polymarket%20%7C%20Kalshi-orange.svg)

**Scanner-first arbitrage detection between Polymarket and Kalshi prediction markets**

[Features](#-features) • [Demo](#-demo) • [Quick Start](#-quick-start) • [Dashboard](#-live-dashboard) • [Configuration](#%EF%B8%8F-configuration)

**Author: [ImMike](https://github.com/ImMike)**

</div>

---

## 🎬 Demo

### 🎥 Video Demo

[**▶️ Watch Demo Video (Click to Download)**](https://github.com/ImMike/polymarket-arbitrage/raw/main/Polymarket-Arb-clip.mp4)

*Watch the bot in action - scanning 5,000+ markets and finding opportunities in real-time*

### Screenshots

<div align="center">

#### 📊 Real Market Data Mode
*Scanning 5,000+ live Polymarket markets*

![Live Data Dashboard](polymarket-live-data.png)

#### 🧪 Simulation Mode  
*Testing with simulated opportunities - 99.6% win rate, $573 profit*

![Simulated Data Dashboard](simulated-market-data.png)

</div>

---

## 🎯 Features

- **🔀 Cross-Platform Candidate Matching** - Suggests similar Polymarket/Kalshi markets for review
- **🔍 Bundle Long Detection** - Identifies when YES ask + NO ask is below $1.00 after fees
- **📊 Market Making Research** - Legacy detector is present but disabled by default
- **🛡️ Risk Management** - Position limits, loss limits, kill switch
- **📈 Live Dashboard** - Real-time web UI showing opportunities and bot activity
- **🔄 Dual Data Modes** - Switch between real market data and simulation
- **💰 Fee Accounting** - Realistic edge calculations including fees & gas costs
- **📝 Comprehensive Logging** - Detailed logs for trades, opportunities, and errors
- **🤖 Market Matching AI** - Automatically matches similar predictions across platforms using text similarity

---

## 🔄 Data Modes

The bot supports two data modes, configurable in `config.yaml`:

### 🧪 Simulation Mode (for demos & testing)

```yaml
mode:
  data_mode: "simulation"  # Generates fake data with opportunities
```

- Generates simulated order books with realistic price dynamics
- Periodically introduces mispricings to create arbitrage opportunities
- Perfect for **screenshots, demos, and testing strategies**
- Fast updates to see the bot in action

### 🌐 Real Mode (for live market data)

```yaml
mode:
  data_mode: "real"  # Fetches actual Polymarket data
```

- Connects to **Polymarket's Gamma API** for market discovery
- Fetches **real order books** from the configured CLOB API
- Scans **5,000+ markets** across all categories
- Real markets are highly efficient - arbitrage opportunities are rare!

---

## 📁 Project Structure

```
polymarket-arbitrage/
├── main.py                   # Main entry point
├── run_with_dashboard.py     # Bot + live dashboard
├── config.yaml               # Safe scanner configuration template
├── pyproject.toml            # uv project dependencies
├── uv.lock                   # Locked dependency graph
│
├── polymarket_client/        # Polymarket API client
│   ├── api.py               # REST + WebSocket integration
│   └── models.py            # Data classes
│
├── kalshi_client/            # Kalshi API client (NEW!)
│   ├── api.py               # Kalshi REST API integration
│   └── models.py            # Kalshi data classes
│
├── core/                     # Trading logic
│   ├── data_feed.py         # Real-time market data manager
│   ├── arb_engine.py        # Single-platform opportunity detection
│   ├── cross_platform_arb.py # Cross-platform arbitrage (NEW!)
│   ├── execution.py         # Order management
│   ├── risk_manager.py      # Risk limits & kill switch
│   └── portfolio.py         # Position & PnL tracking
│
├── dashboard/                # Web dashboard
│   ├── server.py            # FastAPI server
│   └── integration.py       # Bot-dashboard bridge
│
├── utils/                    # Utilities
│   ├── config_loader.py     # YAML config parser
│   ├── logging_utils.py     # Colored console logging
│   └── backtest.py          # Backtesting engine
│
├── tests/                    # Unit tests
│   ├── test_arb_engine.py
│   ├── test_risk_manager.py
│   └── test_portfolio.py
│
└── logs/                     # Log files (auto-created)
```

---

## 🚀 Quick Start

### 1. Clone & Install

```bash
git clone https://github.com/ImMike/polymarket-arbitrage.git
cd polymarket-arbitrage

# Create virtual environment (recommended)
uv sync
```

### 2. Configure

Edit `config.yaml` only for non-secret settings:

```yaml
mode:
  trading_mode: "scanner"     # scanner | paper | live
  data_mode: "real"           # Use "simulation" for demos
  cross_platform_enabled: true  # Enable Polymarket + Kalshi arbitrage
  kalshi_enabled: true        # Enable Kalshi monitoring

trading:
  min_edge: 0.01              # 1% minimum edge
  default_order_size: 5       # Start small
  bundle_short_enabled: false # Disabled by default
  mm_enabled: false           # Disabled by default

risk:
  max_position_per_market: 15
  max_global_exposure: 50
  max_daily_loss: 10
```

### 3. Run with Dashboard

```bash
# Run scanner with dashboard
uv run python run_with_dashboard.py

# Open http://localhost:8888 in your browser
```

### 4. Other Run Modes

```bash
# Bot only (no dashboard)
uv run python main.py

# Paper trading ledger (simulated orders/fills only)
uv run python main.py --paper

# Verbose logging
uv run python main.py -v

# Specify config file
uv run python main.py --config custom.yaml
```

---

## 📊 Live Dashboard

The dashboard provides real-time visibility into bot operations:

<div align="center">

| Metric | Description |
|--------|-------------|
| **Opportunities** | Bundle arb & market-making signals found |
| **Markets Monitored** | Total markets being scanned |
| **Order Books** | Markets with live price data |
| **Uptime** | Bot running time |
| **PnL** | Profit/Loss tracking |

</div>

Access at `http://localhost:8888` when running with `run_with_dashboard.py`

---

## 📈 Trading Strategies

### 🔀 Cross-Platform Arbitrage (NEW!)

Suggests when similar predictions may be priced differently on Polymarket vs Kalshi:

| Condition | Action | Profit |
|-----------|--------|--------|
| Polymarket YES cheaper than Kalshi YES | Buy on Polymarket, Sell on Kalshi | Price difference |
| Kalshi YES cheaper than Polymarket YES | Buy on Kalshi, Sell on Polymarket | Price difference |

**Example**: 
- "Will Trump win?" YES is **$0.52** on Polymarket
- Same prediction YES is **$0.58** on Kalshi
- **Profit opportunity**: Buy on Polymarket, sell on Kalshi = **6% edge** (minus fees)

The current matcher is a scanner aid. Treatable pairs should be manually reviewed for exact resolution rules before any future paper or live execution.

### Bundle Arbitrage

Detects when YES + NO tokens are mispriced within a single platform:

| Condition | Action | Profit |
|-----------|--------|--------|
| `ask_yes + ask_no < $1.00` | Buy both | $1 payout less fees/slippage |
| `bid_yes + bid_no > $1.00` | Disabled by default | Requires inventory/execution controls |

**Example**: If YES trades at $0.45 and NO at $0.52, buying both costs $0.97 but pays out $1.00 = **3% profit**

### Market Making

The market-making detector is legacy research code and is disabled by default. Do not enable it until paper logs prove fill behavior, inventory reconciliation, and loss controls are reliable.

```yaml
trading:
  mm_enabled: false
```

---

## ⚙️ Configuration

### Key Parameters

| Section | Parameter | Description | Default |
|---------|-----------|-------------|---------|
| `mode` | `trading_mode` | `"scanner"`, `"paper"`, or `"live"` | `scanner` |
| `mode` | `data_mode` | `"simulation"` or `"real"` | `real` |
| `mode` | `cross_platform_enabled` | Enable Polymarket + Kalshi | `true` |
| `mode` | `kalshi_enabled` | Enable Kalshi monitoring | `true` |
| `mode` | `min_match_similarity` | Market matching threshold | 0.6 |
| `trading` | `min_edge` | Min profit after fees | 0.01 (1%) |
| `trading` | `bundle_short_enabled` | Enable bundle short research path | false |
| `trading` | `min_spread` | Min spread for MM | 0.05 (5¢) |
| `trading` | `mm_enabled` | Enable market making research path | false |
| `risk` | `max_position_per_market` | Max $ per market | 200 |
| `risk` | `max_global_exposure` | Max total exposure | 5000 |
| `risk` | `max_daily_loss` | Stop-loss limit | 500 |

### Fee Configuration

```yaml
trading:
  maker_fee_bps: 0            # Polymarket maker fee (0%)
  taker_fee_bps: 0            # Polymarket taker fee (0%)
  estimated_gas_per_order: 0.001  # Polygon gas (minimal)
```

### Environment Variables

Store sensitive data in `.env.local`, your shell, keychain, or a secret manager. Keep tracked YAML blank and use `.env.example` as the template:

```bash
cp .env.example .env.local
```

---

## 🧪 Testing

```bash
# Run all tests
uv run pytest tests/ -v

# Run specific test
uv run pytest tests/test_arb_engine.py -v

# With coverage report
uv run pytest tests/ --cov=core --cov=polymarket_client
```

---

## 📊 How It Works

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                      CROSS-PLATFORM ARBITRAGE FLOW                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌──────────────┐         ┌───────────────┐         ┌──────────────┐       │
│  │  Polymarket  │────────▶│  Market       │◀────────│    Kalshi    │       │
│  │  5000+ mkts  │         │  Matcher      │         │  5000+ mkts  │       │
│  └──────────────┘         └───────┬───────┘         └──────────────┘       │
│         │                         │                        │                │
│         │                    Matched Pairs                 │                │
│         │                         │                        │                │
│         ▼                         ▼                        ▼                │
│  ┌──────────────┐         ┌───────────────┐         ┌──────────────┐       │
│  │  Data Feed   │────────▶│ Cross-Platform│◀────────│  Kalshi      │       │
│  │  (orderbooks)│         │  Arb Engine   │         │  Orderbooks  │       │
│  └──────────────┘         └───────┬───────┘         └──────────────┘       │
│         │                         │                        │                │
│         │                    Opportunities                 │                │
│         │                         │                        │                │
│         ▼                         ▼                        ▼                │
│  ┌──────────────┐         ┌───────────────┐         ┌──────────────┐       │
│  │  Dashboard   │◀────────│   Execution   │────────▶│  Portfolio   │       │
│  │  (live UI)   │         │   (orders)    │         │  (tracking)  │       │
│  └──────────────┘         └───────────────┘         └──────────────┘       │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## ⚠️ Important Notes

### About Real Markets

> **Real prediction markets are highly efficient.** Arbitrage opportunities are rare and fleeting. The bot is designed to catch them when they occur, but don't expect constant profits.

### Risk Warnings

1. **Start in scanner mode** - The default path never places real or simulated orders
2. **Move to paper mode intentionally** - Use `--paper` for simulated ledger/fill data
3. **Keep live disabled** - Live startup currently refuses until the Phase 6 execution layer exists
4. **Confirm venue access manually** - No geoblock/workaround assumptions belong in config or code
5. **Expect losses** - Trading always carries risk

### Polymarket Notes

- Polymarket uses a **hybrid model**: centralized order matching, on-chain settlement
- No gas fees for trading (Polymarket covers them)
- Funds are held in USDC on Polygon
- Future authenticated adapters should read credentials from `.env.local`, keychain, or a secret manager

### Kalshi Notes

- Kalshi is a **CFTC-regulated** US prediction market exchange
- Prices are in cents (e.g., 55¢ for YES)
- No authentication required for public market data
- Must be US-based to trade (KYC required)
- API documentation: [docs.kalshi.com](https://docs.kalshi.com)

---

## 🔧 Development

### Adding New Strategies

1. Add detection logic in `core/arb_engine.py`
2. Create `Opportunity` objects with entry/exit prices
3. Execution engine handles order placement

### Extending the Dashboard

The dashboard uses FastAPI + vanilla JS. Add new endpoints in `dashboard/server.py` and update the HTML in `get_embedded_html()`.

---

## 📄 License

MIT License - See [LICENSE](LICENSE) for details

---

## 👤 Author

**[ImMike](https://github.com/ImMike)**

- GitHub: [@ImMike](https://github.com/ImMike)

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit changes (`git commit -m 'Add amazing feature'`)
4. Push to branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

<div align="center">

**⚠️ Disclaimer**: This software is for educational purposes. Trading prediction markets involves risk of loss. Past performance does not guarantee future results. Always do your own research.

Made with ☕ and Python by [ImMike](https://github.com/ImMike)

</div>
