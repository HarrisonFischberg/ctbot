# Disclaimer and risk notice

Last updated: 15 October 2026

**Draft for legal review.** This notice was written alongside the software and has not been
reviewed by a lawyer. It does not replace the MIT License in [LICENSE](LICENSE), which governs
your use of the code.

## Read this before you run CTbot

CTbot ("the Software") is free, open-source software published by Rare Creatures LLC under the
MIT License. It is a tool you download and run yourself, on your own computer, with your own
accounts and your own API keys. There is no CTbot service: Rare Creatures LLC does not host it,
operate it for you, hold your money, see your accounts, or receive your data.

**By downloading, installing, configuring, running or modifying the Software, you accept that you
use it entirely at your own risk, and that you alone are responsible for every result of that
use.**

## Not investment advice

Nothing in the Software, its documentation, its dashboard, its strategy descriptions, its
simulated performance figures, or this repository is investment, financial, legal, tax or
accounting advice, or a recommendation to buy, sell or hold any security or other asset.
Rare Creatures LLC is not a registered investment adviser, broker-dealer, commodity trading
advisor or financial planner. The strategies in the Software are examples written for
experimentation. Consult a qualified, licensed professional before making investment decisions.

## Trading involves risk of loss

Trading stocks, exchange-traded funds and other securities involves substantial risk, including
the possible loss of all the money you invest. Leveraged funds (for example 2× or 3× funds) can
lose value much faster than the market they track. Automated trading adds further risks,
including software bugs, configuration mistakes, data errors, delayed or missing market data,
network or power failures, API changes or outages, and orders that execute differently from
what you intended.

Only use money you can afford to lose.

## Paper trading and real accounts

The Software can connect to a real Robinhood brokerage account through Robinhood's Model
Context Protocol (MCP) server, using a sign-in you complete yourself. Today it uses that
connection for read-only market data and account information, and it executes trades only in a
local paper (simulated) account. Code for sending real orders exists only as a disabled,
unfinished component that refuses to run.

You can change that. The Software is open source: you, or anyone whose modified version you
run, can enable, finish, alter or remove those safeguards. **Whether you use the Software with a
paper account, a real account, or both, and whether you use the published code or a modified
version, every risk and every consequence is yours, including any real-money loss.**

Paper results are not real results. Simulated fills ignore many real-world effects such as
partial fills, queue position, slippage beyond the model's estimate, trading halts, fees and
taxes. Past or simulated performance, including the 3-, 6- and 12-month figures the dashboard
shows, does not predict future results.

## AI-generated decisions

Some strategies ask an AI model (Anthropic's Claude) to review trade ideas. AI output can be
wrong, inconsistent, outdated or based on incomplete information. The Software does not
guarantee that any AI review is accurate, and an AI review is not advice.

## Your accounts, your terms, your laws

- You must be at least 18 years old, and old enough to enter into a binding contract where you
  live, to use the Software with any brokerage account.
- You are responsible for complying with the terms of every service you connect, including
  Robinhood's customer agreement and any rules it sets for automated or agent access, the
  Anthropic API terms, and the Collective2 terms, and with all laws and regulations that apply
  to you, including securities, tax and pattern-day-trader rules.
- You are responsible for the security of your computer, your API keys and your account
  credentials, and for any charges your keys incur (for example Anthropic API usage fees).

## No affiliation

CTbot is not affiliated with, endorsed by, or sponsored by Robinhood Markets, Inc. or its
affiliates, Anthropic PBC, or Collective2. "Robinhood", "Claude", "Anthropic" and "Collective2"
are trademarks of their respective owners and are used here only to describe what the Software
connects to.

## No warranty and limitation of liability

The Software is provided "as is" and "as available", without warranty of any kind, express or
implied, including the warranties of merchantability, fitness for a particular purpose,
accuracy, non-infringement, and uninterrupted or error-free operation.

To the fullest extent permitted by law, Rare Creatures LLC and the Software's contributors are
not liable for any loss or damage of any kind arising from or connected with the Software or its
use, including trading losses, lost profits, missed opportunities, data loss, fees, taxes, or
indirect, incidental, special, consequential or punitive damages, even if advised of the
possibility of such damage. Some jurisdictions do not allow certain exclusions or limitations, so
some of the above may not apply to you; in that case liability is limited to the smallest amount
the law allows.

You agree to indemnify and hold harmless Rare Creatures LLC and the Software's contributors from
any claim arising from your use of the Software, your trading, or your breach of this notice or
of any third party's terms, to the extent the law allows.

## Contact

Rare Creatures LLC, a limited liability company registered in the State of Colorado, United
States. Email: hello@rarecreatures.app
