"""HTML→Slack-mrkdwn conversion.

AEGIS message bodies are authored in a light HTML dialect (a small set
of light tags). Slack uses its own `mrkdwn` flavour. `html_to_mrkdwn` converts
the tags AEGIS actually emits and strips everything else, so the same message
body can be posted to either channel.

Mapping:
  <b>/<strong>            -> *…*
  <i>/<em>                -> _…_
  <code>                  -> `…`
  <a href="URL">TEXT</a>  -> <URL|TEXT>
  any other tag           -> stripped (inner text kept)
  HTML entities           -> unescaped (&amp;->&, &lt;-><, …)
"""

from __future__ import annotations

from html.parser import HTMLParser

# tag -> (open marker, close marker) for the simple wrapping tags.
_WRAP = {
    "b": ("*", "*"),
    "strong": ("*", "*"),
    "i": ("_", "_"),
    "em": ("_", "_"),
    "code": ("`", "`"),
}


class _MrkdwnParser(HTMLParser):
    """Convert a light-HTML fragment to Slack mrkdwn.

    `convert_charrefs=True` (the default) means `handle_data` already receives
    entity-unescaped text, so no separate `html.unescape` pass is needed.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._link_href: str | None = None
        self._link_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _WRAP:
            self._out.append(_WRAP[tag][0])
        elif tag == "a":
            href = next((v for k, v in attrs if k.lower() == "href"), None)
            self._link_href = href
            self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _WRAP:
            self._out.append(_WRAP[tag][1])
        elif tag == "a":
            text = "".join(self._link_text)
            if self._link_href:
                self._out.append(f"<{self._link_href}|{text}>")
            else:
                self._out.append(text)
            self._link_href = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._link_href is not None:
            self._link_text.append(data)
        else:
            self._out.append(data)

    def result(self) -> str:
        # Flush an unterminated <a> (defensive — malformed input).
        if self._link_href is not None:
            self._out.append(f"<{self._link_href}|{''.join(self._link_text)}>")
            self._link_href = None
            self._link_text = []
        return "".join(self._out)


def html_to_mrkdwn(text: str) -> str:
    """Convert a light-HTML message body to Slack mrkdwn."""
    if not text:
        return ""
    parser = _MrkdwnParser()
    parser.feed(text)
    parser.close()
    return parser.result()
