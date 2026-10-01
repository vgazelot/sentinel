"""Markdown → HTML for text written by people or Claude (verdicts, GitHub comments)."""
import re

import markdown

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
    return markdown.markdown(_separate_lists(text or ""), extensions=["fenced_code", "tables", "sane_lists"])


# `path/to/file.ext:42` (optionally `:42-50`) written by Claude or a reviewer: the panel resolves it
# to the diff line below, so it becomes a link. Rendered HTML is walked tag by tag to leave markup —
# and anything already inside an <a> — untouched; refs inside <code> are the common case.
_TAG = re.compile(r"<[^>]+>")
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
    out, pos, in_link = [], 0, False
    for tag in _TAG.finditer(html):
        chunk = html[pos:tag.start()]
        out.append(chunk if in_link else _FILE_REF.sub(_file_ref_anchor, chunk))
        out.append(tag.group())
        lowered = tag.group().lower()
        if lowered.startswith("<a ") or lowered == "<a>":
            in_link = True
        elif lowered.startswith("</a"):
            in_link = False
        pos = tag.end()
    tail = html[pos:]
    out.append(tail if in_link else _FILE_REF.sub(_file_ref_anchor, tail))
    return "".join(out)
