"""
Freelancer Monitor v2.0 — Projects + Contests
-----------------------------------------------
Monitors BOTH Freelancer.com RSS feed (projects) AND
contest browse page (contests) for new listings.
Uses Groq AI to analyze each listing and sends
instant email alerts.

Key insight: Freelancer's RSS feed ONLY contains /projects/.
Contests are a separate system and must be scraped from
/contest/browse/ — the RSS "contestSkills" param is silently ignored.
"""

import os
import re
import sys
import time
import json
import logging
import smtplib
import hashlib
import feedparser
import requests
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timezone

# ─── Logging ───────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("freelancer-monitor")

# ─── Configuration ─────────────────────────────────────────
import urllib.parse

raw_keys = os.getenv("GROQ_API_KEYS", os.getenv("GROQ_API_KEY", ""))
API_KEYS = [k.strip() for k in re.split(r'[,\n\s]+', raw_keys) if k.strip()]
CURRENT_KEY_INDEX = 0

GROQ_API_KEY       = os.getenv("GROQ_API_KEY", "") # Fallback compatibility
RSS_URL            = os.getenv("FREELANCER_RSS_URL",
                       "https://www.freelancer.com/rss.xml"
                       "?query=Typescript%20Tailwind%20CSS%20Node.js%20VPS%20PHP"
                       "%20JavaScript%20Python%20WordPress%20HTML%20Web%20Development")

# Parse RSS_URL to extract the search keywords so we can filter contests organically too
parsed_rss = urllib.parse.urlparse(RSS_URL)
rss_qs = urllib.parse.parse_qs(parsed_rss.query)
raw_query = rss_qs.get("query", [""])[0]
SKILLS_FILTER = [s.strip().lower() for s in raw_query.split() if s.strip()]

FALLBACK_RSS_URL = "https://www.freelancer.com/rss.xml?query=Graphic%20Design%20Photoshop%20Illustrator%20Logo"
FALLBACK_SKILLS = ["illustrator", "photoshop", "design", "graphic", "logo"]

CONTEST_BROWSE_URL = "https://www.freelancer.com/contest/browse/"
SENDER_EMAIL       = os.getenv("SENDER_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
RECEIVER_EMAIL     = os.getenv("RECEIVER_EMAIL", "")

STATE_FILE         = "seen_ids.json"
LOOP_DURATION_SEC  = 5 * 60        # Run for 5 minutes per GitHub Actions invocation
CHECK_INTERVAL_SEC = 30            # Check every 30 seconds (aggressive)
GROQ_API_URL       = "https://api.groq.com/openai/v1/chat/completions"

# Request headers to avoid being blocked
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ─── State Management ─────────────────────────────────────
def load_seen_ids() -> dict:
    """Load previously seen IDs from state file. Returns dict with 'projects' and 'contests' sets."""
    default = {"projects": [], "contests": []}
    if not os.path.exists(STATE_FILE):
        return {"projects": set(), "contests": set()}
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
        return {
            "projects": set(data.get("projects", [])),
            "contests": set(data.get("contests", []))
        }
    except (json.JSONDecodeError, IOError):
        return {"projects": set(), "contests": set()}


def save_seen_ids(seen: dict):
    """Persist seen IDs to state file."""
    data = {
        "projects": sorted(seen["projects"]),
        "contests": sorted(seen["contests"])
    }
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=2)


def generate_id(text: str) -> str:
    """Generate a unique hash ID from any string."""
    return hashlib.md5(text.encode()).hexdigest()[:12]


# ─── RSS Feed (Projects) ──────────────────────────────────
def fetch_projects(feed_url: str = RSS_URL) -> list:
    """Fetch projects from a Freelancer RSS feed."""
    try:
        feed = feedparser.parse(feed_url)
        if feed.bozo:
            log.warning(f"  RSS parse warning: {feed.bozo_exception}")

        projects = []
        for entry in feed.entries:
            link = entry.get("link", "")
            title = entry.get("title", "Untitled")
            description = entry.get("summary", entry.get("description", ""))

            # Extract skills from categories
            skills = ", ".join(
                tag.get("term", "") for tag in entry.get("tags", [])
            ) or "Not listed"

            # Extract budget from description if available
            budget = ""
            budget_match = re.search(r'\(Budget:\s*([^)]+)\)', description)
            if budget_match:
                budget = budget_match.group(1)

            # Clean description — remove budget suffix
            clean_desc = re.sub(r'\s*\(Budget:.*?\)\s*$', '', description)
            # Remove "..." truncation artifacts
            clean_desc = re.sub(r'\.\.\.\s*$', '...', clean_desc)

            projects.append({
                "type": "PROJECT",
                "id": generate_id(link),
                "title": title,
                "link": link,
                "description": clean_desc,
                "skills": skills,
                "budget": budget,
                "pub_date": entry.get("published", ""),
            })

        return projects
    except Exception as e:
        log.error(f"  RSS fetch error for {feed_url}: {e}")
        return []


# ─── Contest Scraper ───────────────────────────────────────
def fetch_contests() -> list:
    """Scrape contests from Freelancer's contest browse page.
    Freelancer.com does NOT include contests in their RSS feed.
    The only way to get them is to scrape /contest/browse/."""
    log.info("[CONTESTS] Scraping contest browse page...")
    try:
        resp = requests.get(CONTEST_BROWSE_URL, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        contests = []

        # Method 1: Find contest cards/links by URL pattern
        contest_links = set()
        for a_tag in soup.find_all("a", href=True):
            href = a_tag["href"]
            # Contest URLs look like: /contest/name-here-12345 or /contest/name-here-12345.html
            if re.match(r'^/contest/[\w-]+-\d+', href):
                full_url = f"https://www.freelancer.com{href}"
                if full_url not in contest_links:
                    contest_links.add(full_url)

                    # Try to extract title from the link text
                    title = a_tag.get_text(strip=True)
                    if not title or len(title) < 3:
                        # Try parent element
                        parent = a_tag.find_parent()
                        if parent:
                            title = parent.get_text(strip=True)

                    # Try to find description near the link
                    desc = ""
                    parent_card = a_tag.find_parent(["div", "li", "article", "section"])
                    if parent_card:
                        desc_elem = parent_card.find(["p", "span"], class_=lambda x: x and ("desc" in str(x).lower() or "detail" in str(x).lower()))
                        if desc_elem:
                            desc = desc_elem.get_text(strip=True)
                        if not desc:
                            # Get all text from parent card but limit it
                            all_text = parent_card.get_text(" ", strip=True)
                            if len(all_text) > len(title) + 10:
                                desc = all_text

                    # Extract contest ID from URL
                    contest_id_match = re.search(r'-(\d+)(?:\.html)?$', href)
                    cid = contest_id_match.group(1) if contest_id_match else generate_id(full_url)

                    contests.append({
                        "type": "CONTEST",
                        "id": f"contest_{cid}",
                        "title": title or "Untitled Contest",
                        "link": full_url,
                        "description": desc,
                        "skills": "",
                        "budget": "",
                        "pub_date": "",
                    })

        # Method 2: Also try the Freelancer API endpoint (public, no auth needed for listing)
        try:
            api_url = "https://www.freelancer.com/api/contests/0.1/contests/?compact=true&limit=20&contest_statuses[]=active"
            api_resp = requests.get(api_url, headers=HEADERS, timeout=10)
            if api_resp.status_code == 200:
                api_data = api_resp.json()
                if "result" in api_data and "contests" in api_data["result"]:
                    for c in api_data["result"]["contests"]:
                        cid = str(c.get("id", ""))
                        title = c.get("title", "Untitled")
                        desc = c.get("description", "")
                        seo_url = c.get("seo_url", "")
                        link = f"https://www.freelancer.com/contest/{seo_url}" if seo_url else f"https://www.freelancer.com/contest/{cid}"
                        budget = ""
                        if c.get("prize"):
                            currency = c.get("currency", {}).get("code", "USD")
                            budget = f"{currency} {c['prize']}"
                        skills_list = [s.get("name", "") for s in c.get("jobs", [])]

                        item_id = f"contest_{cid}"
                        # Don't add duplicates from scraping
                        if not any(x["id"] == item_id for x in contests):
                            contests.append({
                                "type": "CONTEST",
                                "id": item_id,
                                "title": title,
                                "link": link,
                                "description": desc[:500] if desc else "",
                                "skills": ", ".join(skills_list),
                                "budget": budget,
                                "pub_date": "",
                            })
                    log.info(f"  API returned {len(api_data['result']['contests'])} contest(s).")
        except Exception as api_err:
            log.debug(f"  API fallback failed (non-critical): {api_err}")

        # Filter contests by skills using the keywords extracted from the RSS feed
        filtered_contests = []
        for c in contests:
            text_to_search = (c["title"] + " " + c["description"] + " " + c["skills"]).lower()
            if not SKILLS_FILTER:
                filtered_contests.append(c)
                continue
                
            matched = False
            for skill in SKILLS_FILTER:
                # Use regex with word boundaries for accurate keyword matching
                if re.search(r'\b' + re.escape(skill) + r'\b', text_to_search):
                    matched = True
                    break
                    
            if not matched:
                # Try explicit contest Graphic Design fallbacks
                for skill in FALLBACK_SKILLS:
                    if re.search(r'\b' + re.escape(skill) + r'\b', text_to_search):
                        matched = True
                        break
                        
            if matched:
                filtered_contests.append(c)

        log.info(f"  Total: {len(filtered_contests)} contest(s) matching skills found (out of {len(contests)} scraped).")
        return filtered_contests
    except Exception as e:
        log.error(f"  Contest scrape error: {e}")
        return []


# ─── Groq AI Analysis ─────────────────────────────────
def analyze_with_groq(item: dict) -> str:
    """Send listing details to Groq for strategic analysis.
    Rotates API keys if rate limits or errors occur."""
    global CURRENT_KEY_INDEX
    
    if not API_KEYS:
        log.warning("  GROQ_API_KEYS not set -- skipping AI analysis.")
        return "<em>AI analysis unavailable (API keys not configured).</em>"

    listing_type = item["type"]
    prompt = (
        f"You are a professional developer and {'contest strategist' if listing_type == 'CONTEST' else 'freelance bid expert'}. "
        f"Analyze this Freelancer.com {listing_type.lower()}. Give me a 3-point summary:\n"
        "1) What is the exact deliverable?\n"
        "2) What tech/tools are best for this?\n"
        f"3) A 'killer strategy' to {'win this contest' if listing_type == 'CONTEST' else 'land this project'}.\n\n"
        "Keep it short, punchy, and actionable.\n\n"
        f"**{listing_type} Title:** {item['title']}\n\n"
        f"**Description:**\n{item['description'][:800]}"
    )
    if item.get("budget"):
        prompt += f"\n\n**Budget:** {item['budget']}"
    if item.get("skills"):
        prompt += f"\n**Skills Required:** {item['skills']}"

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.5,
        "max_tokens": 500,
    }

    # Attempt to call Groq, rotating through available API keys
    for attempt in range(len(API_KEYS)):
        current_key = API_KEYS[CURRENT_KEY_INDEX]
        headers = {
            "Authorization": f"Bearer {current_key}",
            "Content-Type": "application/json",
        }

        try:
            log.info(f"  Requesting Groq analysis (Attempt {attempt+1}/{len(API_KEYS)} using Key #{CURRENT_KEY_INDEX + 1})...")
            resp = requests.post(GROQ_API_URL, headers=headers, json=payload, timeout=30)
            
            if resp.status_code == 429 or resp.status_code == 402:
                log.warning(f"  Key #{CURRENT_KEY_INDEX + 1} hit limit/error ({resp.status_code}). Rotating to next key...")
                CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS)
                continue
                
            resp.raise_for_status()
            data = resp.json()
            analysis = data["choices"][0]["message"]["content"]
            log.info("  AI analysis received successfully.")
            return analysis
            
        except requests.exceptions.Timeout:
            log.warning(f"  Groq API timed out on Key #{CURRENT_KEY_INDEX + 1}. Rotating...")
            CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS)
        except requests.exceptions.RequestException as e:
            log.warning(f"  Groq API request error on Key #{CURRENT_KEY_INDEX + 1}: {e}. Rotating...")
            CURRENT_KEY_INDEX = (CURRENT_KEY_INDEX + 1) % len(API_KEYS)
        except (KeyError, IndexError) as e:
            log.error(f"  Unexpected API response format: {e}")
            break

    return "<em>AI analysis unavailable (All API keys failed or hit limits).</em>"


# ─── Email Notification ───────────────────────────────────
def build_digest_email_html(items_with_briefs: list) -> str:
    """Build a formatted HTML digest email body."""
    now = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    
    blocks = []
    for item_data in items_with_briefs:
        item = item_data["item"]
        ai_brief = item_data["ai_brief"]
        
        ai_html = ai_brief.replace("\n", "<br>")
        is_contest = item["type"] == "CONTEST"
        gradient = "linear-gradient(135deg,#f59e0b,#ef4444)" if is_contest else "linear-gradient(135deg,#6366f1,#8b5cf6)"
        accent = "#f59e0b" if is_contest else "#a78bfa"
        badge_bg = "#451a03" if is_contest else "#1e1b4b"
        badge_text = "#fbbf24" if is_contest else "#c4b5fd"
        icon = "CONTEST" if is_contest else "PROJECT"
        cta_text = "ENTER CONTEST" if is_contest else "BID ON PROJECT"
        
        block = f"""
            <!-- Project/Contest Block -->
            <tr>
              <td style="padding:28px 32px 16px; background:#1a1a2e; border-top:1px solid #2a2a4a; margin-top:20px; display:block;">
                <table width="100%" cellpadding="0" cellspacing="0"><tr>
                  <td>
                    <h2 style="margin:0; color:#e2e8f0; font-size:18px; line-height:1.4;">
                      {item['title']}
                    </h2>
                  </td>
                  <td align="right" valign="top">
                    <span style="background:{badge_bg}; color:{badge_text}; padding:6px 14px; border-radius:20px; font-size:12px; font-weight:700; letter-spacing:1px; white-space:nowrap;">
                      {icon}
                    </span>
                  </td>
                </tr></table>
                <p style="margin:8px 0 0; color:#94a3b8; font-size:13px;">
                  <strong style="color:{accent};">Skills:</strong> {item.get('skills') or 'Not specified'}
                </p>
                {f'<p style="margin:4px 0 0; color:#94a3b8; font-size:13px;"><strong style="color:{accent};">Budget:</strong> {item["budget"]}</p>' if item.get('budget') else ''}
              </td>
            </tr>
            
            {f'''<tr>
              <td style="padding:0 32px 16px; background:#1a1a2e; display:block;">
                <div style="background:#12121c; border:1px solid #2a2a4a; border-radius:8px; padding:16px;">
                  <h3 style="margin:0 0 8px; color:#64748b; font-size:12px; text-transform:uppercase; letter-spacing:1px;">
                    Description
                  </h3>
                  <p style="color:#94a3b8; font-size:13px; line-height:1.6; margin:0;">
                    {item["description"][:300]}{"..." if len(item.get("description","")) > 300 else ""}
                  </p>
                </div>
              </td>
            </tr>''' if item.get('description') else ''}

            <!-- AI Analysis -->
            <tr>
              <td style="padding:0 32px 20px; background:#1a1a2e; display:block;">
                <div style="background:#12121c; border:1px solid #2a2a4a; border-radius:8px; padding:20px;">
                  <h3 style="margin:0 0 12px; color:{accent}; font-size:14px; text-transform:uppercase; letter-spacing:1px;">
                    &#129302; AI Strategic Brief
                  </h3>
                  <div style="color:#cbd5e1; font-size:14px; line-height:1.7;">
                    {ai_html}
                  </div>
                </div>
              </td>
            </tr>

            <!-- CTA Button -->
            <tr>
              <td style="padding:0 32px 28px; background:#1a1a2e; display:block;" align="center">
                <a href="{item['link']}" style="display:inline-block; background:{gradient}; color:#fff; text-decoration:none; padding:14px 36px; border-radius:8px; font-size:15px; font-weight:600; letter-spacing:0.5px;">
                  {cta_text} &#8594;
                </a>
              </td>
            </tr>
        """
        blocks.append(block)

    all_blocks_html = "\n".join(blocks)
    
    count = len(items_with_briefs)
    first_type = items_with_briefs[0]['item']['type']
    
    if first_type == "CONTEST":
        title_text = f"&#127942; {count} NEW FREELANCER CONTESTS" if count > 1 else "&#127942; NEW FREELANCER CONTEST"
        subject = f"[CONTESTS] {count} New Contests Matched!" if count > 1 else f"[CONTEST] {items_with_briefs[0]['item']['title']}"
    else:
        title_text = f"&#128640; {count} NEW FREELANCER PROJECTS" if count > 1 else "&#128640; NEW FREELANCER PROJECT"
        subject = f"[PROJECTS] {count} New Projects Matched!" if count > 1 else f"[PROJECT] {items_with_briefs[0]['item']['title']}"

    return f"""\
    <html>
    <body style="margin:0; padding:0; background:#0f0f13; font-family:'Segoe UI',Arial,sans-serif;">
      <table width="100%" cellpadding="0" cellspacing="0" style="background:#0f0f13; padding:30px 0;">
        <tr><td align="center">
          <table width="600" cellpadding="0" cellspacing="0" style="background:#12121c; border-radius:12px; overflow:hidden; border:1px solid #2a2a4a;">
            <!-- Header -->
            <tr>
              <td style="background:linear-gradient(135deg,#3b82f6,#8b5cf6); padding:28px 32px; display:block;">
                <table width="100%" cellpadding="0" cellspacing="0"><tr>
                  <td>
                    <h1 style="margin:0; color:#fff; font-size:20px; font-weight:700;">
                      {title_text}
                    </h1>
                    <p style="margin:6px 0 0; color:rgba(255,255,255,0.8); font-size:13px;">
                      {now}
                    </p>
                  </td>
                </tr></table>
              </td>
            </tr>
            
            {all_blocks_html}

            <!-- Footer -->
            <tr>
              <td style="padding:18px 32px; background:#0f0f13; border-top:1px solid #2a2a4a; display:block;">
                <p style="margin:0; color:#64748b; font-size:12px; text-align:center;">
                  Freelancer Monitor v2.0 &#8226; {first_type} Batched Delivery &#8226; Powered by Groq AI
                </p>
              </td>
            </tr>

          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

def send_digest_email(items_with_briefs: list):
    """Send alert email via Gmail SMTP containing multiple aggregated items."""
    if not all([SENDER_EMAIL, GMAIL_APP_PASSWORD, RECEIVER_EMAIL]):
        log.error("  Email credentials not fully configured. Skipping.")
        return False
        
    count = len(items_with_briefs)
    first_type = items_with_briefs[0]['item']['type']
    subject = f"[CONTESTS] {count} New Contests Matched!" if first_type == "CONTEST" and count > 1 else (
              f"[PROJECTS] {count} New Projects Matched!" if first_type == "PROJECT" and count > 1 else 
              f"[{first_type}] {items_with_briefs[0]['item']['title']}")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = f"Freelancer Monitor <{SENDER_EMAIL}>"
    msg["To"]      = RECEIVER_EMAIL

    html_body = build_digest_email_html(items_with_briefs)
    msg.attach(MIMEText(html_body, "html"))

    try:
        log.info(f"  Sending batched email to {RECEIVER_EMAIL} ({count} items)...")
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(SENDER_EMAIL, GMAIL_APP_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECEIVER_EMAIL, msg.as_string())
        log.info("  Batched email sent successfully!")
        return True
    except smtplib.SMTPAuthenticationError:
        log.error("  SMTP auth failed. Check SENDER_EMAIL & GMAIL_APP_PASSWORD.")
        return False
    except Exception as e:
        log.error(f"  Email sending failed: {e}")
        return False


# ─── Process a Batch ─────────────────────────────
def process_batch(items: list, seen: dict):
    """Analyze and notify about a batch of projects or contests."""
    items_with_briefs = []
    
    for item in items:
        log.info(f"  Processing [{item['type']}] {item['title']}...")
        ai_brief = analyze_with_groq(item)
        items_with_briefs.append({"item": item, "ai_brief": ai_brief})
        # After successful analysis (even if fallback error message), mark as seen
        if item["type"] == "PROJECT":
            seen["projects"].add(item["id"])
        else:
            seen["contests"].add(item["id"])
    
    if items_with_briefs:
        send_digest_email(items_with_briefs)
    
    save_seen_ids(seen)


# ─── Main Loop ─────────────────────────────────────────────
def run_monitor():
    """Main monitoring loop — checks every CHECK_INTERVAL_SEC for LOOP_DURATION_SEC total."""
    log.info("=" * 60)
    log.info("  Freelancer Monitor v2.0 -- Projects + Contests")
    log.info("=" * 60)

    # Validate config
    missing = []
    if not API_KEYS:           missing.append("GROQ_API_KEYS (or GROQ_API_KEY)")
    if not SENDER_EMAIL:       missing.append("SENDER_EMAIL")
    if not GMAIL_APP_PASSWORD: missing.append("GMAIL_APP_PASSWORD")
    if not RECEIVER_EMAIL:     missing.append("RECEIVER_EMAIL")
    if missing:
        log.warning(f"  Missing env vars: {', '.join(missing)}")
        log.warning("  Monitor will run but some features may be disabled.")

    log.info(f"  RSS URL: {RSS_URL[:80]}...")
    log.info(f"  Check interval: {CHECK_INTERVAL_SEC}s | Loop duration: {LOOP_DURATION_SEC}s")

    seen = load_seen_ids()
    log.info(f"  Loaded {len(seen['projects'])} seen projects, {len(seen['contests'])} seen contests.")

    start_time = time.time()
    check_count = 0
    new_projects = 0
    new_contests = 0

    while (time.time() - start_time) < LOOP_DURATION_SEC:
        check_count += 1
        elapsed = int(time.time() - start_time)
        log.info(f"\n--- Check #{check_count} (elapsed: {elapsed}s / {LOOP_DURATION_SEC}s) ---")

        new_items_this_cycle = []

        # ── Check Projects (RSS) ──
        log.info("[PROJECTS] Fetching Primary RSS feed...")
        try:
            projects = fetch_projects(RSS_URL)
            for item in projects:
                if item["id"] not in seen["projects"]:
                    new_items_this_cycle.append(item)
                    
            # Fallback if we found 0 new projects, or less than a healthy batch (e.g. < 4)
            # The user requested explicitly: "send about 4 of those..."
            new_primary_projects = len([i for i in new_items_this_cycle if i["type"] == "PROJECT"])
            if new_primary_projects < 4:
                log.info(f"  Only {new_primary_projects} primary projects found. Searching Graphic Design fallback...")
                fallback_projects = fetch_projects(FALLBACK_RSS_URL)
                for item in fallback_projects:
                    if item["id"] not in seen["projects"]:
                        new_items_this_cycle.append(item)
        except Exception as e:
            log.error(f"  Project check error: {e}", exc_info=True)

        # ── Check Contests (Scrape + API) ──
        try:
            contests = fetch_contests()
            for item in contests:
                if item["id"] not in seen["contests"]:
                    new_items_this_cycle.append(item)
        except Exception as e:
            log.error(f"  Contest check error: {e}", exc_info=True)

        if new_items_this_cycle:
            # Separate into independent systems
            prj_items = [i for i in new_items_this_cycle if i["type"] == "PROJECT"]
            cnt_items = [i for i in new_items_this_cycle if i["type"] == "CONTEST"]
            
            # --- Dedicated Projects System ---
            if prj_items:
                batch_prj = prj_items[:12]
                log.info(f"  Found {len(prj_items)} new PROJECTS. Processing batch of {len(batch_prj)} items...")
                process_batch(batch_prj, seen)
                log.info(f"  Project Batch processed. {len(prj_items) - len(batch_prj)} projects remaining in backlog.")
                new_projects += len(batch_prj)
                
            # --- Dedicated Contests System ---
            if cnt_items:
                batch_cnt = cnt_items[:12]
                log.info(f"  Found {len(cnt_items)} new CONTESTS. Processing batch of {len(batch_cnt)} items...")
                process_batch(batch_cnt, seen)
                log.info(f"  Contest Batch processed. {len(cnt_items) - len(batch_cnt)} contests remaining in backlog.")
                new_contests += len(batch_cnt)
                
        else:
            log.info("  No new items found. Backlog is clear.")

        # Summary for this check
        log.info(f"  Check #{check_count} done. Processed this session: {new_projects} projects, {new_contests} contests.")

        # Sleep until next check
        remaining = LOOP_DURATION_SEC - (time.time() - start_time)
        if remaining > CHECK_INTERVAL_SEC:
            log.info(f"  Sleeping {CHECK_INTERVAL_SEC}s...")
            time.sleep(CHECK_INTERVAL_SEC)
        else:
            break

    log.info(f"\n{'=' * 60}")
    log.info(f"  Session complete!")
    log.info(f"  Checks: {check_count} | New projects: {new_projects} | New contests: {new_contests}")
    log.info(f"{'=' * 60}")


if __name__ == "__main__":
    run_monitor()
