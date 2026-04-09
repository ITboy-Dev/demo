# Freelancer Monitor v2.0 — Projects + Contests

A 24/7 AI-powered system that monitors **both** Freelancer.com projects (via RSS) **and** contests (via scraping + API), analyzes them with **DeepSeek AI**, and sends instant **email alerts**.

---

## Why v2.0?

> **The Problem:** Freelancer.com's RSS feed (`rss.xml`) **only returns projects** — even when you add `contestSkills` or `job_type=contest` parameters, they're silently ignored. Contests are a completely separate system.

> **The Fix:** v2.0 uses a **dual-source approach**:
> 1. **RSS feed** for projects (as before)
> 2. **Web scraping + Contest API** for contests — the only reliable way to get them

---

## How It Works

```
 PROJECTS:  RSS Feed ──────────┐
                                ├──> DeepSeek AI ──> Email Alert
 CONTESTS:  Scrape + API ─────┘
                ↺ every 30 seconds
```

- Checks **every 30 seconds** inside each GitHub Actions run
- Each run loops for **5 minutes** (~10 checks per invocation)
- Different **color-coded emails**: purple for projects, amber/red for contests
- Tracks seen items in `seen_ids.json` (auto-committed back to repo)

---

## Quick Setup

### 1. Push to GitHub

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO.git
# Copy files into repo
git add . && git commit -m "Initial setup" && git push
```

### 2. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret Name          | Value                                           |
|----------------------|-------------------------------------------------|
| `DEEPSEEK_API_KEY`   | Your DeepSeek API key (`sk-...`)                |
| `FREELANCER_RSS_URL` | _(Optional)_ Custom RSS URL for projects        |
| `SENDER_EMAIL`       | Your Gmail address                              |
| `GMAIL_APP_PASSWORD` | 16-character Gmail App Password                 |
| `RECEIVER_EMAIL`     | Email to receive alerts                         |

### 3. Get a Gmail App Password

1. Go to [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
2. Select **Mail** → **Other** → name it "Freelancer Monitor"
3. Copy the 16-character password
4. Add as `GMAIL_APP_PASSWORD` secret

### 4. Enable the Workflow

- Go to **Actions** tab → Enable workflows
- The monitor runs automatically every 5 minutes

---

## File Structure

```
├── main.py                          # Core monitor (projects + contests)
├── requirements.txt                 # Python dependencies
├── seen_ids.json                    # Auto-managed state (don't edit)
├── README.md                        # This file
└── .github/
    └── workflows/
        └── monitor.yml              # GitHub Actions config
```

---

## Run Locally

```bash
pip install -r requirements.txt

# Set environment variables (PowerShell)
$env:DEEPSEEK_API_KEY="sk-..."
$env:SENDER_EMAIL="you@gmail.com"
$env:GMAIL_APP_PASSWORD="abcd efgh ijkl mnop"
$env:RECEIVER_EMAIL="you@gmail.com"

python main.py
```

---

## Email Types

| Type | Subject | Color |
|------|---------|-------|
| Project | `[PROJECT ALERT] Title...` | Purple gradient |
| Contest | `[CONTEST ALERT] Title...` | Amber/Red gradient |

Each email includes: listing title, skills, budget, description preview, and AI strategic brief.

---

## License

MIT
