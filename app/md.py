"""Markdown → HTML for text written by people or Claude (verdicts, GitHub comments)."""
import re

import markdown
import nh3

# Claude's verdicts quote release notes and CI logs, GitHub comments are whatever people typed:
# python-markdown passes raw HTML through, so the output keeps a plain allowlist and nothing else
# (an <img onerror> there would run in this page, which can approve PRs on your behalf)
TAGS = {"p", "br", "hr", "strong", "em", "b", "i", "del", "code", "pre", "blockquote", "ul", "ol", "li",
        "h1", "h2", "h3", "h4", "h5", "h6", "table", "thead", "tbody", "tr", "th", "td", "a"}

_LIST_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _separate_lists(text: str) -> str:
    """python-markdown needs a blank line before a list (GitHub doesn't): insert it after a plain line."""
    out, prev, fenced = [], "", False
    for line in text.splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and _LIST_ITEM.match(line) and prev.strip() and not _LIST_ITEM.match(prev):
            out.append("")
        out.append(line)
        prev = line
    return "\n".join(out)


def render(text: str) -> str:
    html = markdown.markdown(_separate_lists(text or ""), extensions=["fenced_code", "tables", "sane_lists"])
    # the addresses are linked before the cleaning: what that makes goes through the allowlist too
    return nh3.clean(_texts(html, _link_url), tags=TAGS, attributes={"a": {"href", "title"}},
                     url_schemes={"http", "https", "mailto"}, set_tag_attribute_values={"a": {"target": "_blank"}})


# Rendered HTML is walked tag by tag to leave markup untouched; each text run knows the link it sits in.
_TAG = re.compile(r"<[^>]+>")
_HREF = re.compile(r'\bhref="([^"]*)"')


def _texts(html: str, sub) -> str:
    """Each run of text through sub(text, href) — href: the link it sits in, else None."""
    out, pos, href = [], 0, None
    for tag in _TAG.finditer(html):
        out += [sub(html[pos:tag.start()], href), tag.group()]
        lowered = tag.group().lower()
        if lowered.startswith("<a ") or lowered == "<a>":
            m = _HREF.search(tag.group())
            href = m.group(1) if m else ""
        elif lowered.startswith("</a"):
            href = None
        pos = tag.end()
    out.append(sub(html[pos:], href))
    return "".join(out)


# A bare address becomes a link; one shown as itself (bare, <url>, [url](url)) reads short when it is a pull request
# (repo#N) or a story (sc-N), the whole address behind it. The text is HTML-escaped: `&amp;` is the one entity an
# address keeps, and closing punctuation is not part of it.
_URL = re.compile(r"https?://(?:[^\s<>\"'&]|&amp;)*[^\s<>\"'&.,;:!?)\]]")
_SHORT = ((re.compile(r"https://github\.com/[\w.-]+/([\w.-]+)/pull/(\d+)(?!\w)"), r"\1#\2"),
          (re.compile(r"https://app\.shortcut\.com/[\w-]+/story/(\d+)(?!\w)"), r"sc-\1"))


def _short(url: str) -> str:
    for pattern, label in _SHORT:
        m = pattern.match(url)
        if m:
            return m.expand(label)
    return url


def _link_url(text: str, href: str | None) -> str:
    if href is None:
        return _URL.sub(lambda m: f'<a href="{m.group()}">{_short(m.group())}</a>', text)
    return _short(text) if text == href else text


# `path/to/file.ext:42` (optionally `:42-50`) written by Claude or a reviewer: the panel resolves it
# to the diff line below, so it becomes a link — not inside another link; refs inside <code> are the common case.
_FILE_REF = re.compile(
    r"(?<![\w/.\-])"                                        # not mid-path, mid-URL or mid-word
    r"((?:[\w.+\-]+/)*[\w+\-]+(?:\.[\w+\-]+)*\.[A-Za-z]\w{0,9})"  # a/b/file.ext (at least one dot, alpha extension)
    r":(\d+)(?:[-–](\d+))?"
    r"(?![\w/])"
)


def _file_ref_anchor(m: re.Match) -> str:
    end = f' data-end="{m.group(3)}"' if m.group(3) else ""
    return (f'<a class="fileref" href="#" data-path="{m.group(1)}" data-line="{m.group(2)}"{end}'
            f' title="Jump to this line in the diff below">{m.group(0)}</a>')


def link_file_refs(html: str) -> str:
    return _texts(html, lambda text, href: text if href is not None else _FILE_REF.sub(_file_ref_anchor, text))
