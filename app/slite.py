"""Big Rocks note on Slite: pick the current cycle's note, parse it into big rocks + outputs.

The note is hand-written prose, so the parser stays deliberately tolerant: a section it
cannot read keeps its raw markdown (`raw`) instead of being dropped.
"""
import logging
import re

import requests

import config

log = logging.getLogger("sentinel.slite")
API = "https://api.slite.com/v1"

# Legend of the note itself, see the top of any Big Rocks note.
STATUS = {"✅": "done", "🚧": "wip", "⛔️": "blocked", "⛔": "blocked", "📥": "todo"}
TAGS = {"📍": "must_have", "🏢": "company", "🤝": "collaborative", "⚡️": "quick_win", "⚡": "quick_win"}
STATUS_RE = re.compile("|".join(map(re.escape, STATUS)))
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$")
OWNER_RE = re.compile(r"^[_*\s]*Owner[_*\s]*@\s*([^_*\n]+?)[_*\s]*$", re.I)
LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)]+)\)")
BULLET_RE = re.compile(r"^[-*+]\s+")


def _headers() -> dict:
    if not config.SLITE_API_KEY:
        raise RuntimeError("SLITE_API_KEY missing from .env")
    return {"x-slite-api-key": config.SLITE_API_KEY}


def current_note(note_id: str = "", parent_id: str = "", match: str = "big rocks") -> dict:
    """The note to read: `note_id` wins, otherwise the newest child of `parent_id` matching `match`."""
    if note_id:
        return note(note_id)
    if not parent_id:
        raise RuntimeError("cycle.slite needs note_id or parent_id in config.yaml")
    r = requests.get(f"{API}/notes/{parent_id}/children", headers=_headers(), timeout=20)
    r.raise_for_status()
    candidates = [
        n for n in r.json().get("notes", [])
        if match.lower() in n["title"].lower() and not n["title"].lower().startswith("archive")
    ]
    if not candidates:
        raise RuntimeError(f"no note matching {match!r} under {parent_id}")
    newest = max(candidates, key=lambda n: n.get("createdAt") or "")
    return note(newest["id"])


def note(note_id: str) -> dict:
    r = requests.get(f"{API}/notes/{note_id}", headers=_headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def _clean(text: str) -> str:
    """Slite exports escape markdown (`\\_`, `\\*`); undo that before parsing."""
    return re.sub(r"\\(.)", r"\1", text)


def _strip_marks(text: str) -> str:
    return re.sub(r"[*_]{1,3}", "", text).strip()


def parse(content: str) -> list[dict]:
    """Note markdown → [{title, tags, owner, outputs, links, raw}], one entry per heading."""
    lines = _clean(content).splitlines()
    sections, current = [], None
    for line in lines:
        heading = HEADING_RE.match(line.strip())
        if heading:
            current = {"title": heading.group(1), "body": []}
            sections.append(current)
        elif current is not None:
            current["body"].append(line)
    return [_section(s) for s in sections]


def _section(section: dict) -> dict:
    title, tags = _title(section["title"])
    rock = {"title": title, "tags": tags, "owner": "", "outputs": [], "links": [], "raw": "\n".join(section["body"]).strip()}
    for line in section["body"]:
        stripped = line.strip()
        if not stripped or set(stripped) <= {"-", "_", "*"}:
            continue
        if not rock["owner"]:
            owner = OWNER_RE.match(stripped)
            if owner:
                rock["owner"] = owner.group(1).strip()
                continue
        status = STATUS_RE.search(stripped)
        if status:
            rock["outputs"].append(_output(stripped, status.group(0)))
            continue
        if BULLET_RE.match(stripped) and rock["outputs"]:
            # plain bullet under an output ("Crawlers:" → feeds, forums)
            rock["outputs"][-1]["notes"].append(_strip_marks(BULLET_RE.sub("", stripped)))
            continue
        for label, url in LINK_RE.findall(stripped):
            rock["links"].append({"label": _strip_marks(label) or url, "url": url})
    return rock


def _title(raw: str) -> tuple[str, list[str]]:
    title, tags = _strip_marks(raw), []
    for emoji, tag in TAGS.items():
        if emoji in title:
            tags.append(tag)
            title = title.replace(emoji, "")
    return re.sub(r"\s+", " ", title).strip(), tags


def _output(line: str, emoji: str) -> dict:
    label = BULLET_RE.sub("", line).replace(emoji, " ", 1)
    for other in STATUS:  # a line never carries two statuses, but never render a stray emoji
        label = label.replace(other, " ")
    links = [{"label": _strip_marks(t) or u, "url": u} for t, u in LINK_RE.findall(label)]
    label = LINK_RE.sub(r"\1", label)
    label = re.sub(r"\s+", " ", _strip_marks(label)).strip(" :")
    return {"status": STATUS[emoji], "label": label, "notes": [], "links": links}
