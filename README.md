# AP inbox agent

A small AI agent for an accounts payable (AP) inbox. It:

- **classifies** every email and attachment (invoice, statement, payment reminder, remittance, other) and
  **extracts the key fields** of every invoice
- finds the **vendors chasing payment**, how far each went (**reminder → escalation → final notice**), and
  whether the invoices they chase are in the inbox
- **answers questions in a chat** (Hermes Agent in the terminal; Claude Code or a built-in chat also work)
- **never fails silently:** missing, unreadable or password-protected files, unsure answers, failed checks and
  failed model calls are all recorded and reported, and it can say **how accurate it is** and how that was
  measured

## What's where

| You asked for | File |
|---|---|
| Persona (soul) | [`hermes/SOUL.md`](hermes/SOUL.md) |
| Instructions | [`AGENTS.md`](AGENTS.md) (Hermes loads it from this folder); [`CLAUDE.md`](CLAUDE.md) for Claude Code |
| Skill | [`hermes/skills/ap-inbox-analyst/SKILL.md`](hermes/skills/ap-inbox-analyst/SKILL.md) (copy in `.claude/skills/`) |
| Tools (MCP) | [`ap_agent/mcp_server.py`](ap_agent/mcp_server.py) serves the 11 read-only tools in [`ap_agent/tools.py`](ap_agent/tools.py) |
| Code (pipeline) | [`ap_agent/`](ap_agent): `load` → `extract` → `classify` → `chasers` → `evaluate`, all run by `run_all` |
| Hermes setup | [`scripts/setup_hermes.py`](scripts/setup_hermes.py), example config in [`hermes/config.example.yaml`](hermes/config.example.yaml) |
| Tests | [`tests/`](tests) (18 unit tests, run by GitHub Actions on every push) |

No API keys and no inbox data are in the repo.

---

## Run it

### 1. Get the code and place the two files
You need Python 3.11+, git and an Anthropic API key. Data and keys live **next to** the repo, never inside it:
```
your-folder/
  .env                              ANTHROPIC_API_KEY=sk-ant-...
  incoming/ap_inbox_candidate.sql   <- the SQL file
  incoming/attachments.zip          <- the zip
  ap-inbox-agent/                   <- this repo
```
```bash
cd your-folder
git clone https://github.com/stefanel96/ap-inbox-agent.git
cd ap-inbox-agent
python -m venv .venv
.venv\Scripts\activate              # Windows   (macOS / Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

### 2. Unpack the attachments safely
The attachments are untrusted files. These scripts only list or copy them; they never open or run them:
```bash
python -I scripts/inspect_inputs.py ../incoming/attachments.zip ../incoming/ap_inbox_candidate.sql   # optional check
python -I scripts/safe_extract.py ../incoming/attachments.zip ../data/attachments
```

### 3. Run the pipeline once (about 1 hour, about $5)
```bash
python -m ap_agent.run_all              # load -> read files -> classify -> payment chasers -> report
python -m ap_agent.run_all --review     # also the accuracy sample (second opinion, about $2)
```
It writes `../data/ap_inbox.db`. If it stops (network, rate limits), run it again: it only does what has no result
yet, so nothing is paid twice. The SQL dump is parsed as text and never executed.

### 4. Chat with Hermes
Install [Hermes Agent](https://hermes-agent.nousresearch.com) (Windows: `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`),
open a new terminal in this folder, then:
```bash
python scripts/setup_hermes.py          # once (and after changing SOUL.md): MCP server, skill, model, persona
hermes config set ANTHROPIC_API_KEY sk-ant-...
hermes mcp test ap_inbox                # expect: Tools discovered: 11
python scripts/hermes_chat.py           # = hermes chat --provider anthropic -m claude-sonnet-5-5 --toolsets "mcp-ap_inbox,skills"
```
`setup_hermes.py` backs up your Hermes config, registers this repo's MCP server (Hermes starts it with this
repo's Python), adds the skill folder, sets Anthropic as the provider and copies `SOUL.md`. Only the inbox
tools and skills are enabled: no terminal, file or web tools.

**Without Hermes:** open the folder in Claude Code (`.mcp.json` registers the same server), or run the
built-in chat: `python -m ap_agent.chat_cli`.

**Try:**
- "Give me an overview of the inbox."
- "Which vendors are chasing us, and how far did they go?"
- "For the final notices: are the invoices they chase in the inbox?"
- "Does email msg_… look legitimate?"
- "What couldn't you read, and what should we do about it?"
- "How accurate are you, and how do you know?" · "What does it cost?"

### 5. Check it
```bash
pytest -q                               # 18 unit tests, no data or API needed
python scripts/check_mcp.py             # starts the MCP server, lists the 11 tools, calls a few
```

---

## How it works

```
 .sql dump ──► load.py ──► SQLite: emails, attachments (matched by Front file id)
 attachments ► extract.py ► each unique file read once: text · scans as images · Excel · Word · zip
                   │
                   ▼
           classify.py   Claude Haiku 5.5, fixed JSON schema → type, vendor, invoice fields, chase level
                   │     code checks: invoice math and dates · keyword rules as a second opinion ·
                   │     confidence < 0.6 → "unsure" · every problem → issues table
                   ▼
           chasers.py    highest chase level per vendor; chased invoice numbers → found / likely / not found
           evaluate.py   Claude Sonnet 5.5 re-reads a stratified sample → agreement, 95% ranges, calibration
                   │
                   ▼
 Hermes ◄── MCP: 11 read-only tools ◄── SQLite        SOUL.md · AGENTS.md · SKILL.md
```

- **The model reads and suggests; code checks and decides.** Totals, matching and chase levels are computed by code.
- **Read-only agent.** The tools open the database read-only; SQL is limited to SELECT, enforced by the database.
- **Every answer cites its source** (message ids, invoice numbers).

**Models:**
| Job | Model | Why |
|---|---|---|
| Classify ~5,700 emails + ~5,650 unique files, extract invoice fields | Haiku 5.5 | high volume, well-defined task, structured output, ~$0.0005 per item |
| Accuracy check on a sample | Sonnet 5.5 | stronger and independent: not the same model grading itself |
| Chat agent; fallback when Haiku declines | Sonnet 5.5 | reliable tool use; the chat sees only small tool results |

**Files:** text PDFs are read locally; scans go to the model as images (first 2 pages); Excel and Word through
parsers (no macros); zips are expanded; identical files are processed once. Password-protected and unknown
files, and attachments listed but not delivered, are recorded with the reason.

**Self-checks:** invoice number and total present, valid dates, due date after invoice date, subtotal + tax =
total; keyword rules disagreeing lowers confidence; "unsure" items are listed for review; accuracy measured on
a sample (above); unit tests in CI.

## Results on the task data

- **5,705 emails**, all classified: invoice 3,657 · other 1,121 · payment reminder 560 · remittance 198 · statement 169.
- **5,665 unique files**, all classified or explained: 4,692 text, 960 scans, 8 password-protected, 1 unsupported.
  82 attachments were listed but not delivered (each one is listed).
- **3,636 distinct invoices** (vendor + number) in 4,458 invoice documents; 162 fail a check.
- **561 chase emails from 137 vendors**; highest level: 96 reminder, 37 escalation, 4 final notice. Chased
  invoice numbers: 1,335 found, 32 likely, 1,313 not in this extract (it starts 24 Aug, so older ones are expected).
- **Accuracy** (agreement with Sonnet on a stratified sample; no hand labels yet): documents **92%**, email type
  **91%** (76% before the second version of the email rules), chase level **76%**.
- **Cost:** one full run **$4.97** (documents $3.16, emails $1.81); accuracy sample $2.22 (one-off); chat a few
  cents per question, more later in a long session (the conversation is re-sent each turn; 8 questions ≈ $1).

## Known weaknesses and next steps
1. **Chase level is the weakest part (76%).** Templated notices with standing policy text ("we won't deliver
   if past-due amounts aren't paid") read as escalation. Next: detect templates and judge the level by change
   over time; have a stronger model confirm only the emails flagged as chases.
2. **Vendor names.** The same invoice sometimes arrives under a brand and under its distributor or parent: 282
   copies with the same number and amount, about 7% of the invoice total. Next: a vendor master (aliases, domains,
   remit-to), so a new name is confirmed once.
3. **No hand labels yet.** Next: a few hundred items labeled by someone who knows AP, used as a regression test
   for every prompt or model change (the labeling tool is included: `python -m ap_agent.label`).
4. **Compare against the books.** Match invoices with the ERP (booked, paid), not just the inbox.
5. **Run incrementally** through the inbox API instead of a one-off export; post alerts to **Slack** through the
   Hermes gateway with the same persona, skill and read-only tools.
6. **Fraud signal.** Two "final notices" look like a fake-invoice scam; the agent flags the warning signs when
   asked. A rule for new vendor + mismatched domain + urgency would flag them automatically.
