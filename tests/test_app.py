"""Offline tests with small sample pages. Run: python -m unittest discover tests"""
import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app  # noqa: E402
import sources  # noqa: E402

INDEX = """<ul><li><a href="/portugues/index.htm">Home</a></li>
<li><a href="goldman/index.htm">Goldman, Emma</a> <a href="dicionario/verbetes/g/g.htm">Biografia</a></li>
<li><a href="marx/index.htm"><img src="x.webp"></a> <a href="marx/index.htm">Marx, Karl</a></li>
<li><a href="marx/index.htm">Engels, Friedrich</a></li></ul>"""
MARX = """<a href="../biblioteca.htm">Biblioteca</a><ul>
<li>1845 M <a href="1845/tesfeuer.htm">Teses sobre Feuerbach</a></li>
<li>1848 - fev M/E <a href="1848/manifesto/index.htm">Manifesto do Partido Comunista</a>
 <a href="1848/manifesto/index.htm"><img></a> <a href="1848/manifesto/m.pdf"><img></a></li>
<table><tr><td>1880</td><td>E</td><td><a href="1880/socialismo/index.htm">Do Socialismo Utópico</a></td></tr></table>
<li><a href="/archive/marx/index.htm">English section</a></li><li><a href="#topp">Início</a></li></ul>"""
GOLDMAN = """<p>1911 <a href="1911/anarquismo.htm">Anarquismo: o que significa</a></p>"""
LISTING = """<a href="/listing?page=2">2</a>
<a href="https://theanarchistlibrary.org/library/emma-goldman-marriage-and-love">
<strong>Marriage and Love</strong> — <em>Emma Goldman</em> <span>Mar 15, 2009 9 pp.</span></a>
<a href="/library/kropotkin-conquest">The Conquest of Bread</a> — Peter Kropotkin Jan 2, 2010 200 pp.
<a href="/library/x-joint">Joint Letter — Part One</a> — Emma Goldman and Peter Kropotkin Jan 3, 2010 2 pp."""


class ParserTests(unittest.TestCase):
    def test_index(self):
        found = sources.parse_mia_index(INDEX)
        self.assertEqual([n for n, _ in found], ["Goldman, Emma", "Marx, Karl", "Engels, Friedrich"])

    def test_author_page(self):
        works = sources.parse_mia_author(MARX, "https://www.marxists.org/portugues/marx/index.htm")
        self.assertEqual([(w["year"], w["marker"]) for w in works], [("1845", "M"), ("1848", "M/E"), ("1880", "E")])
        self.assertTrue(works[1]["pdf_url"].endswith("/m.pdf"))

    def test_listing(self):
        works, last = sources.parse_tal_listing(LISTING)
        self.assertEqual(last, 2)
        self.assertEqual([w["author"] for w in works],
                         ["Emma Goldman", "Peter Kropotkin", "Emma Goldman and Peter Kropotkin"])
        self.assertEqual(works[0]["title"], "Marriage and Love")

    def test_allowed_addresses(self):
        for bad in ("file:///etc/passwd", "http://127.0.0.1/", "https://evil.example/",
                    "https://theanarchistlibrary.org.evil.example/", "https://www.marxists.org:8080/",
                    "https://theanarchistlibrary.org@evil.example/"):
            with self.assertRaises(sources.FetchError, msg=bad):
                sources.clean_url(bad)

    def test_cleaning(self):
        _, body = sources.clean_html(
            '<title>T</title><script>bad()</script><p onclick="x()">Olá <a href="javascript:x()">a</a>'
            '<a href="cap2.htm">next</a><img src="https://evil.example/t.gif"><form><input></form></p>',
            "https://www.marxists.org/portugues/a/b.htm", app.local_link)
        for banned in ("script", "onclick", "javascript", "evil", "<form", "<input"):
            self.assertNotIn(banned, body)
        self.assertIn("/view?url=https%3A%2F%2Fwww.marxists.org%2Fportugues%2Fa%2Fcap2.htm", body)

    def test_text_and_encoding(self):
        raw = '<meta charset="iso-8859-1"><p>Revolução</p><p>Fim</p>'.encode("cp1252")
        self.assertEqual(sources.to_text(sources.decode(raw)), "Revolução\n\nFim\n")


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.server, cat = app.make_server(cls.tmp.name, 0)
        cat.add_mia_authors(sources.parse_mia_index(INDEX))
        base = "https://www.marxists.org/portugues/"
        cat.add_mia_page(base + "marx/index.htm", sources.parse_mia_author(MARX, base + "marx/index.htm"))
        cat.add_mia_page(base + "goldman/index.htm", sources.parse_mia_author(GOLDMAN, base + "goldman/index.htm"))
        cat.add_tal_page("p1", sources.parse_tal_listing(LISTING)[0])
        cat.link_coauthors()
        cls.cat = cat
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        with urllib.request.urlopen(urllib.request.Request(self.url + path, headers=headers or {})) as r:
            return r.read()

    def test_authors_merge_across_sites(self):
        found = json.loads(self.get("/api/search?q=emma+gold"))["authors"]
        self.assertEqual((found[0]["name"], found[0]["count"]), ("Emma Goldman", 3))
        works = json.loads(self.get(f"/api/author/{found[0]['id']}"))["works"]
        self.assertEqual({w["source"] for w in works}, {"mia", "tal"})

    def test_shared_page_and_spellings(self):
        counts = {a["name"]: a["count"] for q in ("marx", "engels") for a in self.cat.search(q)["authors"]}
        self.assertEqual(counts, {"Karl Marx": 2, "Friedrich Engels": 2})

    def test_accents_and_odd_input(self):
        self.assertEqual(self.cat.search("utopico")["works"][0]["title"], "Do Socialismo Utópico")
        for q in ("", "%", "_", "'; DROP TABLE works;--", '"'):
            self.cat.search(q)
        self.assertEqual(self.cat.counts()["mia"], 4)

    def test_page_and_fonts(self):
        for line in (Path(app.HERE) / "static" / "fonts.css").read_text().splitlines():
            path = line.split("url(")[1].split(")")[0]
            self.assertEqual(self.get(path)[:4], b"wOF2")
        self.assertIn(b"<title>Leftist Library</title>", self.get("/"))

    def test_pdf_links_and_themes(self):
        _, body = sources.clean_html('<a href="a.pdf">p</a><a href="b.htm">h</a>',
                                     "https://www.marxists.org/portugues/a/c.htm", app.local_link)
        self.assertEqual(body.count('target="_blank"'), 1)
        self.assertIn(b"data-theme=sepia", app.reading_page("t", "", "https://theanarchistlibrary.org/library/x").encode())

    def test_protection(self):
        for path, headers in (("/api/status", {"Host": "evil.example"}),
                              ("/view?url=http%3A%2F%2F127.0.0.1%2F", None)):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.get(path, headers)
            self.assertIn(caught.exception.code, (403, 502))
        with self.assertRaises(urllib.error.HTTPError):
            urllib.request.urlopen(urllib.request.Request(self.url + "/api/import/tal", data=b""))


if __name__ == "__main__":
    unittest.main()
