from aegis_comms.format import html_to_mrkdwn


def test_bold():
    assert html_to_mrkdwn("<b>hi</b>") == "*hi*"
    assert html_to_mrkdwn("<strong>hi</strong>") == "*hi*"


def test_italic():
    assert html_to_mrkdwn("<i>hi</i>") == "_hi_"
    assert html_to_mrkdwn("<em>hi</em>") == "_hi_"


def test_code():
    assert html_to_mrkdwn("<code>x = 1</code>") == "`x = 1`"


def test_link():
    assert html_to_mrkdwn('<a href="https://x.com">click</a>') == "<https://x.com|click>"


def test_strip_unknown_tags():
    assert html_to_mrkdwn("<span>z</span>") == "z"


def test_entity_unescape():
    assert html_to_mrkdwn("a &amp; b &lt;c&gt;") == "a & b <c>"


def test_quote_and_apostrophe_entities():
    assert html_to_mrkdwn("&quot;hi&quot; &#39;yo&#39;") == "\"hi\" 'yo'"


def test_combined():
    assert (
        html_to_mrkdwn('<b>Bold</b> and <i>italic</i> and <a href="http://u">u</a>')
        == "*Bold* and _italic_ and <http://u|u>"
    )


def test_plain_text_passthrough():
    assert html_to_mrkdwn("just plain text") == "just plain text"
