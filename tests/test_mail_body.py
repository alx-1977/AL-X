"""Body text read from a MIME message.

A multipart/alternative whose text/plain part is "." or otherwise empty used
to hide the text/html part. These tests build that shape with the standard
library and read it through the one extractor `read_mail_message` uses.
"""

from __future__ import annotations

import tempfile
import unittest
from email import policy
from email.message import EmailMessage
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.parser import BytesParser
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alx.contracts import MailReference  # noqa: E402
from alx.providers import ICloudMailAdapter, SQLiteMailObservationState  # noqa: E402
from alx.providers.icloud_mail import _plain_body  # noqa: E402


TRACKING = "1Z999AA10123456784"
CSS = (
    "body { color: #333; font-family: Arial; }\n"
    ".header { background: #cc0000; }\n"
)


def parsed(raw: bytes):
    return BytesParser(policy=policy.default).parsebytes(raw)


def body(raw: bytes) -> str:
    return _plain_body(parsed(raw))


def alternative(plain: str, html: str, *, plain_charset: str = "utf-8",
                html_charset: str = "utf-8") -> bytes:
    message = EmailMessage()
    message["Subject"] = "Shipment"
    message["From"] = "Shipper <shipper@example.test>"
    message.set_content(plain, subtype="plain", charset=plain_charset)
    message.add_alternative(html, subtype="html", charset=html_charset)
    return message.as_bytes()


def single(content: str, subtype: str, charset: str = "utf-8") -> bytes:
    message = EmailMessage()
    message["Subject"] = "Note"
    message["From"] = "Shipper <shipper@example.test>"
    message.set_content(content, subtype=subtype, charset=charset)
    return message.as_bytes()


def digikey_html() -> str:
    return (
        "<html><head><style type=\"text/css\">\n"
        f"{CSS}"
        "</style></head><body>\n"
        "<p>Your order has shipped.</p>\n"
        f"<p>Tracking number {TRACKING}</p>\n"
        "</body></html>"
    )


class FakeImap:
    def __init__(self, raw: bytes) -> None:
        self.raw = raw

    def login(self, _address, _secret):
        return "OK", []

    def logout(self):
        return "BYE", []

    def select(self, _mailbox, readonly=False):
        return "OK", [b"1"]

    def response(self, name):
        return name, [b"777"]

    def uid(self, operation, *_values):
        if operation == "fetch":
            return "OK", [(b"metadata", self.raw), b")"]
        raise AssertionError(operation)


class MailBodyExtractionTests(unittest.TestCase):
    def test_literal_less_than_and_apostrophe_do_not_expose_style(self) -> None:
        text = body(single(
            "<p>Price < 5 and it's discounted.</p>"
            "<style>CSS_RULE</style><p>Tracking ABC123</p>",
            "html",
        ))
        self.assertIn("Price < 5 and it's discounted.", text)
        self.assertIn("Tracking ABC123", text)
        for hidden in ("CSS_RULE", "<p>", "</p>", "<style>", "</style>"):
            self.assertNotIn(hidden, text)

    def test_literal_less_than_stays_visible_around_excluded_markup(self) -> None:
        for literal in (
            "< 5", "<5", "<=5", "<'value'", '<"value"', "<é", "<Ω",
            "<ſtyle>", "<ſcript>",
            "< /style>", "< /script>", "< /STYLE >", "<\t/style>", "<",
        ):
            for quote in ("'", '"'):
                for excluded in (
                    "<style>CSS_RULE</style>",
                    "<script>JS_RULE</script>",
                    "<!-- COMMENT_RULE -->",
                ):
                    with self.subTest(literal=literal, quote=quote, excluded=excluded):
                        visible = f"Price {literal} and {quote}discounted"
                        text = body(single(
                            f"<p>{visible}{excluded} offer{quote}</p><p>Tracking ABC123</p>",
                            "html",
                        ))
                        self.assertEqual(
                            text, f"{visible}  offer{quote}  Tracking ABC123",
                        )

    def test_literal_less_than_immediately_before_excluded_markup(self) -> None:
        for excluded in (
            "<style>CSS_RULE</style>",
            "<script>JS_RULE</script>",
            "<!-- COMMENT_RULE -->",
        ):
            for quote in ("'", '"'):
                with self.subTest(excluded=excluded, quote=quote):
                    text = body(single(
                        f"<p>Price {quote}discounted <{excluded} offer{quote}</p>",
                        "html",
                    ))
                    self.assertEqual(text, f"Price {quote}discounted <  offer{quote}")

    def test_literal_less_than_at_end_of_html_is_visible(self) -> None:
        # MIMEText preserves the actual end of input without set_content's newline.
        for visible in ("Price <", "It's discounted <", 'Say "discounted" <'):
            with self.subTest(visible=visible):
                self.assertEqual(body(MIMEText(visible, "html", "utf-8").as_bytes()), visible)

    def test_spaced_noise_closers_do_not_end_excluded_blocks(self) -> None:
        for name in ("style", "script"):
            for closer in (f"< /{name}>", f"< /{name.upper()} >", f"<\t/{name}>"):
                with self.subTest(name=name, closer=closer):
                    text = body(single(
                        f"<p>Before</p><{name}>HIDDEN {closer} STILL_HIDDEN"
                        f"</{name}><p>Tracking ABC123</p>",
                        "html",
                    ))
                    self.assertEqual(text, "Before   Tracking ABC123")

    def test_digikey_shaped_alternative_uses_html_and_drops_css(self) -> None:
        raw = alternative(".", digikey_html())
        text = body(raw)
        self.assertEqual(
            text,
            "Your order has shipped.\n"
            f"Tracking number {TRACKING}",
        )
        self.assertNotIn("font-family", text)
        self.assertNotIn("Arial", text)
        self.assertNotIn("#cc0000", text)
        self.assertNotIn(".header", text)

    def test_read_mail_message_returns_the_html_text(self) -> None:
        """The capability reads this body through the adapter, not a second path."""
        raw = alternative(".", digikey_html())
        with tempfile.TemporaryDirectory() as directory:
            state = SQLiteMailObservationState(Path(directory) / "observations.sqlite3")
            try:
                account = ICloudMailAdapter(
                    "imap.example.test", 993, "friedl@example.test", "secret", state, 1,
                    connection_factory=lambda *args, **kwargs: FakeImap(raw),
                )
                content = account.read(MailReference("INBOX", "777", "1"))
                self.assertEqual(content.body, body(raw))
                self.assertIn(TRACKING, content.body)
            finally:
                state.close()

    def test_empty_plain_part_falls_back_to_html(self) -> None:
        text = body(alternative("", "<p>Recovered invoice 134891093</p>"))
        self.assertEqual(text, "Recovered invoice 134891093")

    def test_whitespace_plain_part_falls_back_to_html(self) -> None:
        text = body(alternative(" \t\n", "<p>Recovered from a blank plain part</p>"))
        self.assertEqual(text, "Recovered from a blank plain part")

    def test_substantive_plain_part_is_preferred(self) -> None:
        plain = "Hello  world\n\nSecond line of the notice"
        html = (
            "<style>.x{color:red}</style>"
            "<p>HTML version that must not replace a real plain part</p>"
        )
        text = body(alternative(plain, html))
        self.assertEqual(text, "Hello  world\nSecond line of the notice")
        self.assertNotIn("color", text)
        self.assertNotIn("must not replace", text)

    def test_substantive_plain_part_keeps_its_charset(self) -> None:
        plain = "Größe des vollständigen Textes steht hier."
        html = "<p>HTML darf diesen Text nicht ersetzen, er ist lang genug.</p>"
        text = body(alternative(
            plain, html, plain_charset="iso-8859-1", html_charset="iso-8859-1",
        ))
        self.assertEqual(text, plain)

    def test_html_fallback_keeps_its_charset(self) -> None:
        text = body(alternative(
            ".",
            "<p>Größe tracking GR123456789</p>",
            html_charset="iso-8859-1",
        ))
        self.assertEqual(text, "Größe tracking GR123456789")

    def test_style_and_script_contents_are_excluded(self) -> None:
        html = (
            "<html><head>"
            "<STYLE type=\"text/css\">.secret { color: red; }</STYLE>"
            "<script type=\"text/javascript\">var leak = \"SECRETJS\";</script>"
            "</head><body>"
            "<!-- hidden comment SECRETCOMMENT -->"
            "<p>Visible tracking AB123</p>"
            "<SCRIPT>alert(\"NOALERT\")</SCRIPT >"
            "</body></html>"
        )
        text = body(single(html, "html"))
        self.assertEqual(text, "Visible tracking AB123")
        for leaked in ("secret", "SECRETJS", "SECRETCOMMENT", "NOALERT", "alert"):
            self.assertNotIn(leaked, text)

        single_quoted = body(single(
            "<style type='text/css'>SECRET CSS</style><p>Visible</p>",
            "html",
        ))
        self.assertEqual(single_quoted, "Visible")

        only_style = body(alternative(".", "<style>only css</style>"))
        self.assertEqual(only_style, "")

    def test_uppercase_unclosed_and_self_closing_noise_tags(self) -> None:
        unclosed = body(single(
            "<p>Visible</p><STYLE type=\"text/css\">body{color:red} SECRETUNCLOSED",
            "html",
        ))
        self.assertEqual(unclosed, "Visible")

        truncated = body(single("<p>Visible</p><script type=\"text/javascript\"", "html"))
        self.assertEqual(truncated, "Visible")

        closed_later = body(single(
            "<p>Before</p><style>css SECRET<p>Inside</p></style><p>After</p>",
            "html",
        ))
        self.assertEqual(closed_later, "Before   After")
        self.assertNotIn("Inside", closed_later)
        self.assertNotIn("SECRET", closed_later)

        self_closing = body(single(
            "<p>Before</p><style type=\"text/css\" /><script src=\"app.js\"/>"
            "<STYLE data-x=\"a>b\"/><p>After</p>",
            "html",
        ))
        self.assertEqual(self_closing, "Before     After")
        self.assertNotIn("app.js", self_closing)
        self.assertNotIn("text/css", self_closing)

    def test_comments_do_not_swallow_or_leak_text(self) -> None:
        hidden = body(single(
            "<!-- <style>HIDDEN</style><script>HIDDENJS</script> -->"
            "<p>Shown tracking SHOW123456</p>",
            "html",
        ))
        self.assertEqual(hidden, "Shown tracking SHOW123456")

        script_hides_comment = body(single(
            "<script type=\"text/javascript\"><!--\n"
            "var SECRETJS = 1;\n"
            "// --></script><p>Visible OK123</p>",
            "html",
        ))
        self.assertEqual(script_hides_comment, "Visible OK123")

        unclosed_comment = body(single(
            "<p>Shown</p><!-- hidden SECRETUNCLOSED\n<p>Not shown</p>",
            "html",
        ))
        self.assertEqual(unclosed_comment, "Shown")

        conditional = body(single(
            "<!--[if mso]>\n"
            "<style>body{font-family:Arial}</style>\n"
            "<![endif]-->\n"
            "<!--[if !mso]><!--><p>Kept outside</p><!--<![endif]-->",
            "html",
        ))
        self.assertEqual(conditional, "Kept outside")
        self.assertNotIn("font-family", conditional)
        self.assertNotIn("Arial", conditional)

    def test_quoted_attribute_does_not_end_a_script(self) -> None:
        text = body(single(
            "<script type=\"text/javascript\" data-note=\"a>b\">"
            "var SECRET=1;</script><p>Visible</p>",
            "html",
        ))
        self.assertEqual(text, "Visible")

    def test_tag_prefix_is_not_a_style_element(self) -> None:
        text = body(single("<p>Keep</p><stylesheet>still here</stylesheet>", "html"))
        self.assertEqual(text, "Keep  still here")

    def test_quoted_attribute_markers_keep_surrounding_text(self) -> None:
        """Markers inside a quoted attribute are not style, script, or comments."""
        for title in ("Use <style> here", "Use <script> here", "Use <!-- here"):
            for html in (
                f'Lead<div title="{title}">Tracking ABC123</div>Tail',
                f"Lead<div title='{title}'>Tracking ABC123</div>Tail",
            ):
                with self.subTest(html=html):
                    self.assertEqual(
                        body(single(html, "html")),
                        "Lead Tracking ABC123 Tail",
                    )

        mixed_quotes = (
            '<div title="It\'s <style> here">Tracking ABC123</div>',
            """<div title='Say "<script>"'>Tracking ABC123</div>""",
            '<div title="Use <style> here">Tracking ABC123</div></style>',
            "<div title='Use <script> here'>Tracking ABC123</div></script>",
            '<div title="Use <!-- here">Tracking ABC123</div><!-- end -->',
        )
        for html in mixed_quotes:
            with self.subTest(html=html):
                self.assertEqual(body(single(html, "html")), "Tracking ABC123")

        surrounded = body(single(
            "<p>Before</p>"
            '<div title="Use <style> here">Tracking ABC123</div>'
            "<p>After</p>",
            "html",
        ))
        self.assertEqual(
            surrounded,
            "Before" + " " * 2 + "Tracking ABC123" + " " * 2 + "After",
        )
        later = body(single(
            "<p>Before</p>"
            "<div title='Use <script> here'>Tracking ABC123</div></script>"
            "<p>After</p>",
            "html",
        ))
        self.assertEqual(
            later,
            "Before" + " " * 2 + "Tracking ABC123" + " " * 3 + "After",
        )

    def test_hyphenated_and_longer_names_are_ordinary_tags(self) -> None:
        kept = "Keep" + " " * 2 + "still here"
        for html in (
            "<p>Keep</p><style-wrapper>still here</style-wrapper>",
            "<p>Keep</p><STYLE-WRAPPER>still here</STYLE-WRAPPER>",
            "<p>Keep</p><script-block>still here</script-block>",
            "<p>Keep</p><scripts>still here</scripts>",
            '<p>Keep</p><style-wrapper class="a>b">still here</style-wrapper>',
            """<p>Keep</p><scripts data-note='a>b'>still here</scripts>""",
        ):
            with self.subTest(html=html):
                self.assertEqual(body(single(html, "html")), kept)

        beside_real_style = body(single(
            '<style type="text/css">SECRET</style>'
            "<style-wrapper>Tracking ABC123</style-wrapper>",
            "html",
        ))
        self.assertEqual(beside_real_style, "Tracking ABC123")
        self.assertNotIn("SECRET", beside_real_style)

        beside_real_script = body(single(
            '<script type="text/javascript">SECRETJS</script>'
            "<scripts>Tracking ABC123</scripts>",
            "html",
        ))
        self.assertEqual(beside_real_script, "Tracking ABC123")
        self.assertNotIn("SECRETJS", beside_real_script)

        for html in (
            "<style>SECRET</style-wrapper>HIDDEN</style><p>Shown</p>",
            "<style>SECRET</STYLE-WRAPPER>HIDDEN</style><p>Shown</p>",
            "<script>SECRETJS</script-block>HIDDENJS</script><p>Shown</p>",
            "<script>SECRETJS</scripts>HIDDENJS</script><p>Shown</p>",
        ):
            with self.subTest(html=html):
                text = body(single(html, "html"))
                self.assertEqual(text, "Shown")
                self.assertNotIn("SECRET", text)
                self.assertNotIn("HIDDEN", text)

    def test_noise_markup_is_not_split_into_visible_text(self) -> None:
        split = body(single(
            "<div <style>SECRET</style><p>Shown</p>",
            "html",
        ))
        self.assertEqual(split, "Shown")
        self.assertNotIn("SECRET", split)

        comparison = body(single(
            "a < b <style>SECRET</style> tail",
            "html",
        ))
        self.assertEqual(comparison, "a < b" + " " * 3 + "tail")
        self.assertNotIn("SECRET", comparison)

        for html in (
            '<p>Before</p><style>SECRET</style title="a>b"><p>Shown</p>',
            "<p>Before</p><script>SECRETJS</script title='a>b'><p>Shown</p>",
            '<style data-x="</style>">SECRET</style><p>Shown</p>',
            "<script data-x='</script>'>SECRETJS</script><p>Shown</p>",
        ):
            with self.subTest(html=html):
                text = body(single(html, "html"))
                self.assertNotIn("SECRET", text)
                self.assertNotIn("a>b", text)
                if html.startswith("<p>Before</p>"):
                    self.assertEqual(text, "Before" + " " * 3 + "Shown")
                else:
                    self.assertEqual(text, "Shown")

    def test_whitespace_collapses_as_before(self) -> None:
        self.assertEqual(
            body(single("<p>Hello</p><p>World</p>", "html")),
            "Hello  World",
        )
        self.assertEqual(
            body(single("<p>Hello</p>\n<p>World</p>", "html")),
            "Hello\nWorld",
        )
        self.assertEqual(body(single("Hello<br>World", "html")), "Hello World")
        self.assertEqual(
            body(single("<p>Tom &amp; Jerry</p>", "html")),
            "Tom & Jerry",
        )
        self.assertEqual(
            body(single("<p>Hello</p><style>SECRET</style><p>World</p>", "html")),
            "Hello   World",
        )
        self.assertEqual(
            body(single("<p>Use &lt;style&gt;secret&lt;/style&gt; carefully</p>", "html")),
            "Use <style>secret</style> carefully",
        )

    def test_short_plain_is_kept_when_html_is_not_longer(self) -> None:
        self.assertEqual(
            body(alternative("Invoice 42", "<p>Invoice 42</p>")),
            "Invoice 42",
        )
        same = "1234567890123456789"
        self.assertEqual(len(same), 19)
        self.assertEqual(body(alternative(same, f"<p>{same}</p>")), same)
        self.assertEqual(body(single("Invoice 42", "plain")), "Invoice 42")
        self.assertEqual(body(single(".", "plain")), ".")

    def test_short_plain_yields_when_html_text_is_longer(self) -> None:
        self.assertEqual(
            body(alternative("Invoice 42", "<p>Invoice 42 is ready to view</p>")),
            "Invoice 42 is ready to view",
        )
        plain = "1234567890123456789"
        html_text = "y" * 20
        self.assertEqual(body(alternative(plain, f"<p>{html_text}</p>")), html_text)
        kept = "12345678901234567890"
        self.assertEqual(len(kept), 20)
        self.assertEqual(body(alternative(kept, "<p>" + ("y" * 40) + "</p>")), kept)

    def test_punctuation_plain_yields_to_html(self) -> None:
        text = body(alternative("." * 20, "<p>Real content here</p>"))
        self.assertEqual(text, "Real content here")

    def test_nested_alternative_ignores_attachment_plain(self) -> None:
        html = (
            "<html><head><style>.a{color:red}</style></head>"
            "<body><p>Nested tracking NEST123456789</p></body></html>"
        )
        related = MIMEMultipart("related")
        related.attach(MIMEText(html, "html", "utf-8"))
        image = MIMEImage(b"\x89PNG\r\n", _subtype="png")
        image.add_header("Content-ID", "<logo>")
        image.add_header("Content-Disposition", "inline", filename="logo.png")
        related.attach(image)
        alternative_part = MIMEMultipart("alternative")
        alternative_part.attach(MIMEText(".", "plain", "utf-8"))
        alternative_part.attach(related)
        outer = MIMEMultipart("mixed")
        outer.attach(alternative_part)
        note = MIMEText(
            "Attached note that must stay out of the body text entirely.",
            "plain",
            "utf-8",
        )
        note.add_header("Content-Disposition", "attachment", filename="note.txt")
        outer.attach(note)

        text = body(outer.as_bytes())
        self.assertEqual(text, "Nested tracking NEST123456789")
        self.assertNotIn("color", text)
        self.assertNotIn("Attached note", text)

    def test_html_only_nested_message_uses_html_text(self) -> None:
        outer = MIMEMultipart("mixed")
        outer.attach(MIMEText("<style>cssrule</style><p>Only html</p>", "html", "utf-8"))
        text = body(outer.as_bytes())
        self.assertEqual(text, "Only html")
        self.assertNotIn("cssrule", text)

    def test_joined_plain_parts_stay_when_together_they_are_substantive(self) -> None:
        outer = MIMEMultipart("mixed")
        outer.attach(MIMEText(".", "plain", "utf-8"))
        outer.attach(MIMEText(
            "This second plain part is long enough to keep.", "plain", "utf-8",
        ))
        outer.attach(MIMEText(
            "<p>HTML SHOULD NOT WIN BECAUSE PLAIN IS LONG ENOUGH</p>", "html", "utf-8",
        ))
        self.assertEqual(
            body(outer.as_bytes()),
            ".\nThis second plain part is long enough to keep.",
        )

    def test_there_is_one_body_extraction_path(self) -> None:
        source = (
            Path(__file__).resolve().parents[1] / "src" / "alx" / "providers" / "icloud_mail.py"
        ).read_text(encoding="utf-8")
        self.assertEqual(source.count("def _plain_body("), 1)
        self.assertEqual(source.count("_plain_body("), 2)
        self.assertEqual(source.count("def _html_to_text("), 1)
        self.assertEqual(source.count("_html_to_text("), 2)
        self.assertNotIn("def _html_body", source)
        self.assertNotIn("_legacy", source)


if __name__ == "__main__":
    unittest.main()
