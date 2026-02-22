# Binance Futures: MA Crossover + ADX Strategy Bot
This repository contains a professional-grade automated trading bot designed for Binance Futures. It uses a triple-confirmation strategy involving Moving Average crossovers and the ADX (Average Directional Index) to identify high-probability trends.

Strategy Overview
The bot follows a rigorous entry logic to minimize "choppy" market fake-outs:

- Trend Confirmation: Price must be above/below the 100 SMA.

- Momentum Entry: 9 SMA crossing the 18 SMA.

- Strength Filter: ADX > 25 (Ensures the trend is strong enough to trade).

- Exit Logic: Multi-stage Take Profit (TP) and trailing Stop Loss (SL).

Security First
This bot uses environment variables to protect your API keys. Never hardcode your keys directly into the notebook.

- Rename .env.example to .env.

- Input your Binance API Key and Secret into the .env file.

- Ensure .env is listed in your .gitignore to prevent accidental uploads.

Installation & Setup
Clone the repository:

Bash
git clone https://github.com/your-username/your-repo-name.git
cd your-repo-name
Install Dependencies:

Bash
pip install pandas ta binance-futures-connector python-dotenv
Configure the Bot:
Open the Jupyter Notebook and adjust the tp_levels, leverage, and symbol in the Configuration cell.

Key Features:
- Hedge Mode Support: Configures the account automatically.

- Trailing Stop Loss: Dynamically adjusts to lock in profit.

- Risk Management: Daily trade limits to prevent over-trading.

-Robust Connectivity: Built-in retry logic for API rate limits and network errors.

⚠️ Disclaimer
This software is for educational purposes only. Do not trade money you cannot afford to lose. Use at your own risk.