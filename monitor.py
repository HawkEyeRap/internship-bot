"""Summer 2027 SWE internship monitor -> Telegram alerts. Runs on GitHub Actions.

Sources:
  1. Simplify/Pitt CSC Summer 2027 list
  2. Direct checks of every company career board that appears anywhere in Simplify's data
     (Greenhouse, Lever, Ashby, Workday, SmartRecruiters): about 2,300+ companies, auto-updated
  3. Anything extra you add to companies.json
"""
import html, json, os, re, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

SEEN_FILE = "seen.json"
SIMPLIFY_URL = ("https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/"
                "dev/.github/scripts/listings.json")
DRY_RUN = os.environ.get("DRY_RUN") == "1"  # print instead of sending (for testing)
THREADS = 32

INTERN = re.compile(r"\bintern(ship)?s?\b|\bco-?op\b", re.I)
ROLE = re.compile(r"software|\bswe\b|engineer|developer|back-?end|front-?end|full[\s-]?stack|"
                  r"machine learning|\bml\b|\bai\b|infrastructure|platform|mobile|ios|android", re.I)
NOT_SWE = re.compile(r"mechanical|electrical|civil|chemical|manufactur|industrial|process eng|"
                     r"quality eng|field eng|sales eng|structural|environmental|hardware|"
                     r"mechatronic|aerospace|biomedical|nuclear|mining|petroleum|construction", re.I)
# Summer 2027 only: drop other years and non-summer terms (titles with no term/year are kept,
# since almost everything posted this cycle is for Summer 2027)
OTHER_YEAR = re.compile(r"\b20(1\d|2[0-6]|2[89]|3\d)\b")
NON_SUMMER = re.compile(r"\bfall\b|\bautumn\b|\bspring\b|\bwinter\b|off[\s-]?cycle|"
                        r"\bco-?op\b|year[\s-]?long|12[\s-]?month|6[\s-]?month", re.I)
SUMMER = re.compile(r"\bsummer\b", re.I)
# Undergrad-eligible: drop grad-only roles unless the title also says undergrad/bachelor's
GRAD_ONLY = re.compile(r"ph\.?\s?d|doctora|post-?doc|master'?s|\bm\.?s\.?\b|\bmba\b|"
                       r"\bgraduate\b|grad student|\bmsc\b|\bm\.?eng\b", re.I)
UNDERGRAD = re.compile(r"undergrad|bachelor|\bbs\b|\bb\.s\.", re.I)

BOARD_PATTERNS = {
    "greenhouse": [re.compile(r"(?:boards|job-boards)(?:\.eu)?\.greenhouse\.io/(?!embed)([\w-]+)", re.I),
                   re.compile(r"greenhouse\.io/embed/job_app\?for=([\w-]+)", re.I)],
    "lever": [re.compile(r"jobs\.lever\.co/([\w-]+)", re.I)],
    "ashby": [re.compile(r"jobs\.ashbyhq\.com/([\w.%-]+)", re.I)],
    "smartrecruiters": [re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([\w-]+)", re.I)],
}
WORKDAY = re.compile(r"https?://([\w-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([\w-]+)")


# ---------- helpers ----------
def http_json(url, body=None):
    headers = {"User-Agent": "Mozilla/5.0 (internship-monitor)", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=20) as r:
        return json.load(r)


def undergrad_ok(title):
    return not GRAD_ONLY.search(title) or bool(UNDERGRAD.search(title))


def summer_2027_ok(title):
    if OTHER_YEAR.search(title):
        return False
    return not NON_SUMMER.search(title) or bool(SUMMER.search(title))


def wanted(title):
    if not INTERN.search(title) or not ROLE.search(title) or NOT_SWE.search(title):
        return False
    return summer_2027_ok(title) and undergrad_ok(title)


def job(id_, company, title, url, location):
    return {"id": id_, "company": company, "title": title, "url": url, "location": location or ""}


# ---------- source: Simplify list + board discovery ----------
def load_simplify():
    """Returns (current Summer 2027 SWE jobs, {source_key: (kind, args, company_name)})."""
    jobs, boards = [], {}
    for j in http_json(SIMPLIFY_URL):
        url, name = j.get("url", ""), j.get("company_name", "")
        for kind, pats in BOARD_PATTERNS.items():
            for p in pats:
                m = p.search(url)
                if m:
                    slug = m.group(1)
                    boards.setdefault(f"{kind}:{slug.lower()}", (kind, (slug,), name))
        m = WORKDAY.search(url)
        if m and m.group(3).lower() not in ("job", "wday", "details"):
            t, wd, site = m.groups()
            boards.setdefault(f"workday:{t}:{site}".lower(), ("workday", (t, wd, site), name))

        if not j.get("active", True) or not j.get("is_visible", True):
            continue
        if "Summer 2027" not in (j.get("terms") or []):
            continue
        degrees = j.get("degrees") or []
        if degrees and not any(d in ("Bachelor's", "Associate's") for d in degrees):
            continue  # grad-only (Master's/PhD/MBA)
        title, cat = j.get("title", ""), (j.get("category") or "").lower()
        if not undergrad_ok(title):
            continue
        if "software" in cat:
            pass
        elif ("ai" in cat or "machine learning" in cat) and ROLE.search(title) and not NOT_SWE.search(title):
            pass
        else:
            continue
        jobs.append(job(f"sim:{j.get('id') or url}", name, title, url,
                        ", ".join(j.get("locations") or [])))
    return jobs, boards


# ---------- sources: company career boards ----------
def greenhouse(name, slug):
    data = http_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [job(f"gh:{slug}:{j['id']}", name, j["title"], j["absolute_url"],
                (j.get("location") or {}).get("name"))
            for j in data.get("jobs", []) if wanted(j["title"])]


def lever(name, slug):
    data = http_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [job(f"lv:{slug}:{j['id']}", name, j["text"], j["hostedUrl"],
                (j.get("categories") or {}).get("location"))
            for j in data if wanted(j.get("text", ""))]


def ashby(name, slug):
    data = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [job(f"ab:{slug}:{j.get('id') or j.get('jobUrl')}", name, j["title"], j.get("jobUrl", ""),
                j.get("location"))
            for j in data.get("jobs", []) if wanted(j["title"])]


def smartrecruiters(name, slug):
    data = http_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?q=intern&limit=100")
    out = []
    for j in data.get("content", []):
        if wanted(j.get("name", "")):
            loc = j.get("location") or {}
            out.append(job(f"sr:{slug}:{j['id']}", name, j["name"],
                           f"https://jobs.smartrecruiters.com/{slug}/{j['id']}",
                           ", ".join(x for x in (loc.get("city"), loc.get("region")) if x)))
    return out


def workday(name, tenant, wd, site):
    base = f"https://{tenant}.{wd}.myworkdayjobs.com"
    out = []
    for offset in (0, 20, 40):  # up to 60 "intern" results
        data = http_json(f"{base}/wday/cxs/{tenant}/{site}/jobs",
                         {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": "intern"})
        posts = data.get("jobPostings", [])
        for j in posts:
            path = j.get("externalPath", "")
            if path and wanted(j.get("title", "")):
                out.append(job(f"wd:{tenant}:{path}", name, j["title"], f"{base}/{site}{path}",
                               j.get("locationsText")))
        if len(posts) < 20:
            break
    return out


FETCHERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby,
            "smartrecruiters": smartrecruiters, "workday": workday}


def manual_boards():
    """Extra companies from companies.json."""
    boards = {}
    try:
        cfg = json.load(open("companies.json"))
    except FileNotFoundError:
        return boards
    for kind in ("greenhouse", "lever", "ashby", "smartrecruiters"):
        for slug in cfg.get(kind, []):
            boards[f"{kind}:{slug.lower()}"] = (kind, (slug,), slug)
    for url in cfg.get("workday", []):  # paste any Workday careers URL
        m = WORKDAY.search(url)
        if m:
            t, wd, site = m.groups()
            boards[f"workday:{t}:{site}".lower()] = ("workday", (t, wd, site), t)
    return boards


# ---------- dedupe + Telegram ----------
def keys_for(j):
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    return (j["id"], f"t:{norm(j['company'])}|{norm(j['title'])}")


def send(text):
    if DRY_RUN:
        print(text, "\n---")
        return
    body = urllib.parse.urlencode({
        "chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text,
        "parse_mode": "HTML", "disable_web_page_preview": "true"}).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=30).read()
    time.sleep(1.1)  # stay under Telegram's rate limit


def notify(jobs):
    chunk = f"🚨 <b>{len(jobs)} new SWE internship(s)</b>\n\n"
    for j in jobs:
        line = (f"<b>{html.escape(j['company'])}</b> — {html.escape(j['title'])}\n"
                f"📍 {html.escape(j['location'][:80])}\n"
                f"👉 <a href=\"{html.escape(j['url'], quote=True)}\">Apply here</a>\n\n")
        if len(chunk) + len(line) > 3800:
            send(chunk)
            chunk = ""
        chunk += line
    if chunk:
        send(chunk)


# ---------- main ----------
def main():
    first_run = not os.path.exists(SEEN_FILE)
    seen = set() if first_run else set(json.load(open(SEEN_FILE)))

    results = {}  # source_key -> list of jobs
    simplify_jobs, boards = load_simplify()
    results["simplify"] = simplify_jobs
    boards.update(manual_boards())

    failures = []
    with ThreadPoolExecutor(THREADS) as pool:
        futs = {pool.submit(FETCHERS[kind], name, *args): key
                for key, (kind, args, name) in boards.items()}
        for f in as_completed(futs):
            try:
                results[futs[f]] = f.result()
            except Exception as e:
                failures.append(f"{futs[f]}: {e}")

    new, batch = [], set()
    for src, jobs in results.items():
        baseline = f"src:{src}" not in seen  # first time seeing this source: record silently
        for j in jobs:
            ks = keys_for(j)
            if not baseline and not any(k in seen or k in batch for k in ks):
                new.append(j)
            batch.update(ks)
        seen.add(f"src:{src}")
    seen |= batch

    total = sum(len(v) for v in results.values())
    print(f"Checked {len(boards)} company career sites + Simplify list. "
          f"{len(results) - 1} sites OK, {len(failures)} unreachable. "
          f"{total} matching postings, {len(new)} new.")
    for line in failures[:25]:
        print("  unreachable:", line)

    if first_run:
        send(f"✅ Internship monitor is live. Watching {len(results) - 1} company career sites "
             f"plus the Simplify list ({total} current SWE internship postings saved as a baseline). "
             f"You'll be pinged for anything new.")
    elif new:
        notify(new)

    with open(SEEN_FILE, "w") as f:
        json.dump(sorted(seen), f, indent=0)


if __name__ == "__main__":
    main()
