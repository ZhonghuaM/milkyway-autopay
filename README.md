# milkyway-autopay

Automation tooling for parallel test purchases in the Milky Way Idle test environment, built with Playwright.

## Status
This repository is intended for controlled testing workflows. It is not a general-purpose game botting framework.

## What this project does
- reuses saved authenticated browser state
- runs headless Playwright sessions
- supports parallel execution across multiple accounts
- opens checkout pages, updates quantity, submits payment, and recycles sessions over long runs
- writes run results to CSV for later inspection

## Repository contents
- `milkyway_autopay.py` - main command-line script
- `accounts.example.json` - example account configuration
- `requirements.txt` - Python dependencies
- `LICENSE-ADDITIONAL-TERMS.md` - additional use restrictions and acceptable-use notice

## Requirements
- Python 3.10+
- Google Chrome or Chromium for the initial login/bootstrap step
- Playwright Python package
- Playwright browser binaries

## Installation
Create or activate a Python environment, then install dependencies:

```bash
pip install -r requirements.txt
playwright install chromium
