# milkyway-autopay

Playwright automation for parallel test purchases in the Milky Way Idle test environment.

## Features
- parallel multi-account execution
- headless browser runs
- saved browser state reuse
- configurable loop count, quantity, and refresh cadence

## Setup
1. Install Python dependencies.
2. Save authenticated browser state for each account.
3. Create `accounts.json` from `accounts.example.json`.
4. Run the script.

## Examples
```bash
python milkyway_autopay.py save-state --cdp-url http://127.0.0.1:9222 --output mwidle_state_11111.json
python milkyway_autopay.py save-state --cdp-url http://127.0.0.1:9223 --output mwidle_state_12345.json
python milkyway_autopay.py run --accounts accounts.json --loops 100 --quantity 99 --delay-sec 0.1
