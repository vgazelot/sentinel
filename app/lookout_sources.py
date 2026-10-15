"""Lookout sources: where the tech news comes from — Hacker News, Lobsters, Reddit, GitHub trending, any
RSS/Atom feed, the releases of the tools you run. Each read returns `Candidate`s; lookout.py merges them
(one row per thing, whatever the link) and has Claude keep the few worth your time.

Everything read here is written by strangers. It only reaches Claude as fenced material, with no tool,
and only http(s) links are kept (a `javascript:` link from a feed must not become a clickable row).
Pages are read from public addresses only: a link posted on Hacker News must not make Sentinel read a
service on your network.
"""
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, parse_qsl, urlencode, urljoin, urlparse

import feedparser
import requests

import github

log = logging.getLogger("sentinel.lookout")
UA = "sentinel-lookout/1.0 (self-hosted tech watch)"
TIMEOUT = 15
MAX_PAGE = 1_500_000
READ_SECONDS = 30           # a whole page read, not per chunk: a server dripping bytes must not hold the Lookout
TRENDING = "https://mshibanami.github.io/GitHubTrendingRSS/{since}/{lang}.xml"


@dataclass
class Candidate:
    key: str
    kind: str                 # repo, release, article, discussion
    title: str
    url: str
    source: str               # hackernews, lobsters, reddit:<sub>, trending, feed:<name>, releases
    discussion: str = ""      # where people talk about it
    points: int | None = None
    comments: int | None = None
    published: str = ""       # UTC %Y-%m-%dT%H:%M:%SZ
    summary: str = ""         # a few lines of plain text
    body: str = ""            # release notes, a feed's full text: read instead of fetching the page


# --- One key per thing ---------------------------------------------------------------------------
_GITHUB = re.compile(r"^https?://(?:www\.)?github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?"
                     r"(?:/(?:tree|blob|releases|wiki)(?:/[^?#]*)?)?/?(?:[?#].*)?$", re.I)
_NOT_OWNERS = {"orgs", "topics", "features", "sponsors", "marketplace", "collections", "trending", "apps", "settings",
               "about", "pricing", "enterprise", "site", "explore", "events", "solutions", "resources", "security",
               "login", "notifications", "codespaces", "customer-stories", "readme"}
_TRACKING = re.compile(r"^(utm_\w*|ref|ref_src|source|fbclid|gclid|mc_cid|mc_eid)$", re.I)
_DISCUSSION_HOSTS = ("reddit.com", "redd.it", "news.ycombinator.com", "lobste.rs")


def github_repo(url: str) -> str:
    """owner/repo when the link is a repository (or its code, releases, wiki), "" otherwise: an issue or
    a pull request is an article of its own."""
    m = _GITHUB.match((url or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if m and m.group(1).lower() not in _NOT_OWNERS else ""


def canonical(url: str) -> str:
    """The same repository shared as its README, a release page or a tree link is one thing; tracking
    parameters and www. never make two."""
    repo = github_repo(url)
    if repo:
        return f"github:{repo.lower()}"
    p = urlparse((url or "").strip())
    host = (p.hostname or "").lower().removeprefix("www.")
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k)])
    return f"url:{host}{p.path.rstrip('/')}" + (f"?{query}" if query else "")


def kind_of(url: str) -> str:
    if github_repo(url):
        return "repo"
    host = (urlparse(url).hostname or "").lower()
    return "discussion" if any(host == h or host.endswith("." + h) for h in _DISCUSSION_HOSTS) else "article"


def web_link(url: str) -> bool:
    return (url or "").lower().startswith(("https://", "http://"))


# --- Text out of HTML ------------------------------------------------------------------------------
class _Reader(HTMLParser):
    """Visible text, one block per line — only the <main>/<article> part when there is one worth reading (a
    page's menus are not what Claude should read first) — and every link with its text and the text that
    follows it (a newsletter: the link, then why it is worth reading)."""
    HIDDEN = {"script", "style", "noscript", "svg", "template", "iframe", "nav", "footer"}
    MAIN = {"main", "article"}
    BLOCKS = {"p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "blockquote", "br", "tr", "td",
              "th", "section", "article", "dd", "dt", "hr", "table", "figure"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.main: list[str] = []
        self.links: list[dict] = []
        self._hidden = 0
        self._main = 0
        self._anchor = False

    def handle_starttag(self, tag, attrs):
        if tag in self.HIDDEN:
            self._hidden += 1
        elif tag in self.BLOCKS:
            self._add("\n")
        self._main += tag in self.MAIN
        if tag == "a" and not self._hidden:
            self.links.append({"href": dict(attrs).get("href") or "", "text": "", "after": ""})
            self._anchor = True

    def handle_endtag(self, tag):
        if tag in self.HIDDEN and self._hidden:
            self._hidden -= 1
        elif tag in self.BLOCKS:
            self._add("\n")
        if tag in self.MAIN and self._main:
            self._main -= 1
        if tag == "a":
            self._anchor = False

    def handle_data(self, data):
        if self._hidden:
            return
        self._add(data)
        if self.links:
            link = self.links[-1]
            if self._anchor:
                link["text"] += data
            elif len(link["after"]) < 600:
                link["after"] += data

    def _add(self, text: str):
        self.parts.append(text)
        if self._main:
            self.main.append(text)

    def text(self) -> str:
        def clean(parts):
            lines = (" ".join(line.split()) for line in "".join(parts).splitlines())
            return "\n".join(line for line in lines if line)
        main = clean(self.main)
        return main if len(main) >= 500 else clean(self.parts)


def _read(markup: str) -> _Reader:
    reader = _Reader()
    try:
        reader.feed(markup or "")
        reader.close()
    except Exception as e:  # noqa: BLE001 — a page that does not parse reads as what was parsed so far
        log.debug("html parse stopped: %s", e)
    return reader


def html_text(markup: str, chars: int) -> str:
    return _read(markup).text()[:chars]


_MD_NOISE = re.compile(r"!\[[^\]]*\]\([^)]*\)|<img[^>]*>|<!--.*?-->", re.S)


def md_text(markdown: str) -> str:
    """A README or release notes: badges and images out, HTML tags read as text."""
    return html_text(_MD_NOISE.sub("", markdown or "").replace("\n", "<br>"), 1_000_000)


# --- Reads ---------------------------------------------------------------------------------------
def _get(url: str, **params) -> requests.Response:
    r = requests.get(url, params=params or None, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r


def _entries(url: str) -> list:
    feed = feedparser.parse(_get(url).content)
    if not feed.entries and feed.get("bozo"):
        raise RuntimeError(f"not a feed: {feed.get('bozo_exception')}")
    return feed.entries


def _utc(dt: datetime | None) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else ""


def _from_iso(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat((value or "").replace("Z", "+00:00"))
    except ValueError:
        return None


def _entry_time(e) -> datetime | None:
    t = e.get("published_parsed") or e.get("updated_parsed")
    return datetime(*t[:6], tzinfo=timezone.utc) if t else None


def _entry_html(e) -> str:
    return (e.get("content") or [{}])[0].get("value") or e.get("summary") or ""


def hackernews(cfg: dict) -> list[Candidate]:
    """Stories of the last `days` past `min_points` (Algolia's Hacker News search, no key needed)."""
    since = int((datetime.now(timezone.utc) - timedelta(days=cfg.get("days", 3))).timestamp())
    hits = _get("https://hn.algolia.com/api/v1/search", tags="story", hitsPerPage=200,
                numericFilters=f"created_at_i>{since},points>={int(cfg.get('min_points', 150))}").json()["hits"]
    out = []
    for h in hits:
        thread = f"https://news.ycombinator.com/item?id={h['objectID']}"
        url = h.get("url") or thread
        text = html_text(h.get("story_text") or "", 6000)
        out.append(Candidate(canonical(url), kind_of(url), h.get("title") or "", url, "hackernews", thread,
                             h.get("points"), h.get("num_comments"),
                             _utc(datetime.fromtimestamp(h["created_at_i"], timezone.utc)) if h.get("created_at_i") else "",
                             text[:400], text))
    return out


def lobsters(cfg: dict) -> list[Candidate]:
    """The latest stories of some tags, past `min_score` (a story still climbing comes back next read)."""
    tags = ",".join(cfg.get("tags") or ["devops"])
    out = []
    for s in _get(f"https://lobste.rs/t/{tags}.json").json():
        if (s.get("score") or 0) < int(cfg.get("min_score", 15)):
            continue
        url = s.get("url") or s.get("comments_url") or ""
        text = (s.get("description_plain") or "")[:6000]
        out.append(Candidate(canonical(url), kind_of(url), s.get("title") or "", url, "lobsters", s.get("comments_url") or "",
                             s.get("score"), s.get("comment_count"), _utc(_from_iso(s.get("created_at"))), text[:400], text))
    return out


_REDDIT_LINK = re.compile(r'<a href="([^"]+)">\[link\]</a>')


def reddit(subs: list[str], cfg: dict) -> list[Candidate]:
    """The top of some subreddits over `period`, in one RSS read (`r/a+b`: a second read right after the
    first gets a 429): a link post points to its link, a text post (or an image) to its thread."""
    limit = int(cfg.get("limit", 25)) * len(subs)
    url = f"https://www.reddit.com/r/{'+'.join(subs)}/top/.rss?t={cfg.get('period', 'week')}&limit={limit}"
    out = []
    for e in _entries(url):
        thread, content = e.get("link") or "", _entry_html(e)
        m = _REDDIT_LINK.search(content)
        link = m.group(1) if m and kind_of(m.group(1)) != "discussion" else thread
        sub = (e.get("tags") or [{}])[0].get("term") or "+".join(subs)
        # the post's own text: the thread page is an app shell that reads as "Reddit"
        text = html_text(content.rsplit("submitted by", 1)[0], 6000)
        out.append(Candidate(canonical(link), kind_of(link), e.get("title") or "", link, f"reddit:{sub}", thread,
                             published=_utc(_entry_time(e)), summary=text[:400], body=text if link == thread else ""))
    return out


def trending(lang: str, cfg: dict) -> list[Candidate]:
    """GitHub trending for a language, through a public RSS mirror of the page (GitHub has no API for it)."""
    out = []
    for e in _entries(TRENDING.format(since=cfg.get("since", "weekly"), lang=lang)):
        url = e.get("link") or ""
        repo = github_repo(url)
        if not repo:
            continue
        out.append(Candidate(canonical(url), "repo", repo, url, "trending",
                             summary=html_text(_entry_html(e), 2000).split("\n")[0][:300]))
    return out


def feed(spec: dict) -> list[Candidate]:
    """Any RSS/Atom feed, entries of the last `days`. `expand: true` reads a newsletter issue as the links
    it shares (one issue, ten articles): each becomes a candidate of its own."""
    url = spec["url"]
    name = feed_name(spec)
    cutoff = datetime.now(timezone.utc) - timedelta(days=spec.get("days", 7))
    out = []
    for e in _entries(url):
        when = _entry_time(e)
        if when and when < cutoff:
            continue
        if spec.get("expand"):
            out += shared_links(_entry_html(e), e.get("link") or url, name, _utc(when))
            continue
        link = e.get("link") or ""
        text = html_text(_entry_html(e), 6000)
        out.append(Candidate(canonical(link), kind_of(link), e.get("title") or link, link, f"feed:{name}",
                             published=_utc(when), summary=text[:400], body=text))
    return out


def feed_name(spec: dict) -> str:
    return spec.get("name") or (urlparse(spec["url"]).hostname or "feed").removeprefix("www.")


def shared_links(content: str, base: str, name: str, published: str) -> list[Candidate]:
    """The links of a newsletter issue that point away from it, with their text: a short anchor ("here",
    "link") or a link back to the newsletter itself is not an article."""
    own = (urlparse(base).hostname or "").removeprefix("www.")
    out = []
    for link in _read(content).links:
        href, text = urljoin(base, link["href"]), " ".join(link["text"].split())
        if len(text) < 15 or not web_link(href) or (urlparse(href).hostname or "").removeprefix("www.") == own:
            continue
        out.append(Candidate(canonical(href), kind_of(href), text[:200], href, f"feed:{name}", published=published,
                             summary=" ".join(link["after"].split())[:400]))
    return out


_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")
_SECURITY = re.compile(r"\bsecurity\b|\bCVE-\d{4}-\d+|\bvulnerab", re.I)


def releases(repo: str, cfg: dict) -> list[Candidate]:
    """The releases of a tool you run over the last `days`: a new minor or major version, or a patch that
    fixes a vulnerability. Pre-releases and ordinary patches stay out."""
    cutoff = _utc(datetime.now(timezone.utc) - timedelta(days=cfg.get("days", 14)))
    out = []
    for r in github.rest(f"/repos/{repo}/releases", per_page=10).json():
        if r.get("draft") or r.get("prerelease") or (r.get("published_at") or "") < cutoff:
            continue
        tag, body = r.get("tag_name") or "", r.get("body") or ""
        m = _VERSION.search(tag)
        if m and int(m.group(3) or 0) and not _SECURITY.search(body):
            continue
        name = (r.get("name") or "").strip()
        title = f"{repo} {tag}" + (f" — {name}" if name and name.lstrip("v") != tag.lstrip("v") else "")
        text = md_text(body)
        out.append(Candidate(f"release:{repo.lower()}@{tag}", "release", title, r.get("html_url") or f"https://github.com/{repo}/releases",
                             "releases", published=r.get("published_at") or "", summary=text[:400], body=text[:8000]))
    return out


def jobs(sources: dict) -> list[tuple[str, Callable[[], list[Candidate]]]]:
    """Every read, one per feed, subreddit, trending list and repository: a feed that fails must not hide
    the others, and its failure says which one."""
    def opts(name: str) -> dict | None:
        value = sources.get(name)
        return (value if isinstance(value, dict) else {}) if value else None

    out: list[tuple[str, Callable[[], list[Candidate]]]] = []
    if (o := opts("hackernews")) is not None:
        out.append(("hackernews", lambda o=o: hackernews(o)))
    if (o := opts("lobsters")) is not None:
        out.append(("lobsters", lambda o=o: lobsters(o)))
    if (o := opts("reddit")) is not None and o.get("subreddits"):
        out.append(("reddit", lambda o=o: reddit(o["subreddits"], o)))
    if (o := opts("github_trending")) is not None:
        out += [(f"trending:{lang}", lambda lang=lang, o=o: trending(lang, o)) for lang in o.get("languages") or ["all"]]
    for f in sources.get("feeds") or []:
        spec = f if isinstance(f, dict) else {"url": f}
        out.append((f"feed:{feed_name(spec)}", lambda spec=spec: feed(spec)))
    rel = sources.get("releases") or {}
    rel = {"repos": rel} if isinstance(rel, list) else rel
    out += [(f"releases:{repo}", lambda repo=repo, rel=rel: releases(repo, rel)) for repo in rel.get("repos") or []]
    return out


# --- What Claude reads about one find ---------------------------------------------------------------
def public(host: str) -> bool:
    """Every address the name resolves to is on the internet: no loopback, private, link-local (the cloud
    metadata address) or reserved one."""
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError):
        return False
    return bool(infos) and all(ipaddress.ip_address(i[4][0].split("%")[0]).is_global for i in infos)


def page_text(url: str, chars: int = 3000) -> str:
    """The readable text of a public web page, "" when it cannot be read: http(s) only, every redirect hop
    checked again, HTML only, 1.5 MB and READ_SECONDS at most."""
    deadline = time.monotonic() + READ_SECONDS
    for _ in range(5):
        p = urlparse(url)
        if p.scheme not in ("http", "https") or not p.hostname or not public(p.hostname):
            return ""
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT, stream=True, allow_redirects=False)
        except requests.RequestException:
            return ""
        with r:
            if r.is_redirect:
                url = urljoin(url, r.headers.get("Location") or "")
                continue
            kind = r.headers.get("Content-Type", "").lower()
            if r.status_code >= 400 or "html" not in kind:
                return ""
            raw = b""
            for chunk in r.iter_content(65536):
                raw += chunk
                if len(raw) > MAX_PAGE or time.monotonic() > deadline:
                    break
            return html_text(raw.decode(r.encoding if "charset" in kind else "utf-8", errors="replace"), chars)
    return ""


def _count(path: str, **params) -> int | None:
    """How many entries a GitHub list has, from the last page number of a one-per-page read."""
    try:
        r = github.rest(path, per_page=1, **params)
    except requests.RequestException:
        return None
    last = r.links.get("last", {}).get("url")
    return int(parse_qs(urlparse(last).query)["page"][0]) if last else len(r.json())


def repo_material(repo: str, readme_chars: int = 2500, releases_n: int = 3) -> tuple[str, dict]:
    """What tells a project apart from a weekend repository — age, activity, contributors, releases,
    license — then its README. Returns the text and the facts shown on the find."""
    info = github.rest(f"/repos/{repo}").json()
    facts = {"stars": info.get("stargazers_count"), "created": (info.get("created_at") or "")[:10],
             "pushed": (info.get("pushed_at") or "")[:10], "license": (info.get("license") or {}).get("spdx_id") or "",
             "language": info.get("language") or "", "archived": bool(info.get("archived")),
             "contributors": _count(f"/repos/{repo}/contributors", anon=1)}
    try:
        rels = [f"{r['tag_name']} ({(r.get('published_at') or '')[:10]})"
                for r in github.rest(f"/repos/{repo}/releases", per_page=releases_n).json()]
    except requests.RequestException:
        rels = []
    try:
        readme = md_text(github.rest(f"/repos/{repo}/readme", accept="application/vnd.github.raw").text)
    except requests.RequestException:
        readme = "(no README)"
    lines = [f"Repository {info.get('full_name', repo)}: {info.get('description') or ''}",
             f"{facts['stars']} stars · {info.get('forks_count')} forks · {info.get('open_issues_count')} open issues · "
             f"{facts['contributors'] or '?'} contributors · created {facts['created']} · last push {facts['pushed']} · "
             f"license {facts['license'] or 'none'}" + (" · ARCHIVED" if facts["archived"] else ""),
             f"topics: {', '.join(info.get('topics') or []) or '—'} · homepage: {info.get('homepage') or '—'}",
             f"latest releases: {', '.join(rels) or 'none'}", "README:", readme[:readme_chars]]
    return "\n".join(lines), facts
