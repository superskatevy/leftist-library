"""Fetching and parsing for the two source libraries (standard library only)."""
import hashlib
import http.client
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import urllib.robotparser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, urldefrag, urljoin, urlsplit, urlunsplit

USER_AGENT = "LeftistLibrary/1.0 (personal reading app)"
MIA_INDEX = "https://www.marxists.org/portugues/biblioteca.htm"
TAL_LISTING = "https://theanarchistlibrary.org/listing"
HOSTS = {
    "www.marxists.org": "mia",
    "marxists.org": "mia",
    "theanarchistlibrary.org": "tal",
}
MAX_BYTES = 80 * 1024 * 1024


class FetchError(Exception):
    pass


def clean_url(url):
    """Return a safe https URL on an allowed host, or raise FetchError."""
    url, _ = urldefrag(url.strip())
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        raise FetchError("Invalid address")
    if parts.scheme not in ("http", "https") or host not in HOSTS or port not in (None, 80, 443):
        raise FetchError("Address is outside the two source libraries")
    if host == "marxists.org":
        host = "www.marxists.org"
    path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
    query = quote(parts.query, safe="=&%+-._~")
    return urlunsplit(("https", host, path, query, ""))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Every redirect target is checked against the allowed hosts.
        return super().redirect_request(req, fp, code, msg, headers, clean_url(newurl))


class Fetcher:
    def __init__(self, cache_dir):
        self.cache = Path(cache_dir)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.opener = urllib.request.build_opener(_SafeRedirect)
        self._robots = {}
        self._last = {}
        self._lock = threading.Lock()

    def _download(self, url):
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with self.opener.open(request, timeout=30) as response:
                data = response.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise FetchError("File is too large")
                ctype = response.headers.get("Content-Type", "")
                final = response.geturl()
        except urllib.error.HTTPError as error:
            raise FetchError(f"The source site answered {error.code} for {url}")
        except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as error:
            raise FetchError(f"Could not reach {url} ({getattr(error, 'reason', error)})")
        return data, ctype, final

    def get(self, url, use_cache=True):
        """Return (bytes, content_type, final_url). Opened texts are kept on disk."""
        url = clean_url(url)
        key = self.cache / hashlib.sha256(url.encode()).hexdigest()
        meta = key.with_suffix(".meta")
        if use_cache and key.exists() and meta.exists():
            ctype, final = meta.read_text("utf-8").split("\n")[:2]
            return key.read_bytes(), ctype, final
        data, ctype, final = self._download(url)
        tmp = key.with_suffix(f".{threading.get_ident()}.tmp")
        tmp.write_bytes(data)
        tmp.replace(key)
        meta.write_text(f"{ctype}\n{final}", "utf-8")
        return data, ctype, final

    def _rules(self, host):
        if host not in self._robots:
            rules = urllib.robotparser.RobotFileParser()
            try:
                data, _, _ = self._download(f"https://{host}/robots.txt")
                rules.parse(data.decode("utf-8", "replace").splitlines())
            except FetchError:
                rules.parse([])
            self._robots[host] = rules
        return self._robots[host]

    def crawl(self, url):
        """Polite fetch for cataloguing: obeys robots.txt, about one request a second."""
        url = clean_url(url)
        host = urlsplit(url).hostname
        rules = self._rules(host)
        if not rules.can_fetch(USER_AGENT, url):
            raise FetchError(f"robots.txt on {host} does not allow automatic access to {url}")
        delay = max(1.0, float(rules.crawl_delay(USER_AGENT) or 0))
        with self._lock:
            wait = self._last.get(host, 0) + delay - time.monotonic()
            self._last[host] = time.monotonic() + max(0, wait)
        if wait > 0:
            time.sleep(wait)
        data, ctype, final = self._download(url)
        return decode(data, ctype), final


def decode(data, ctype=""):
    found = re.search(r"charset=[\"']?([\w-]+)", ctype or "", re.I)
    if not found:
        found = re.search(rb"charset=[\"']?([\w-]+)", data[:4096], re.I)
    charset = found.group(1) if found else "utf-8"
    if isinstance(charset, bytes):
        charset = charset.decode("ascii")
    if charset.lower() in ("iso-8859-1", "latin-1", "us-ascii"):
        charset = "cp1252"
    try:
        return data.decode(charset)
    except (UnicodeDecodeError, LookupError):
        return data.decode("cp1252", "replace")


def normalise(text):
    """Lower case without accents or punctuation, used for searching."""
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", text))


def squash(text):
    return " ".join(text.split())


class _Links(HTMLParser):
    """Collects every link with its text and the text before and after it."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self._before = []
        self._current = None
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip += 1
        elif tag == "a":
            self._close()
            href = dict(attrs).get("href")
            if href:
                self._current = {"href": href, "parts": [], "before": "".join(self._before), "after": []}
                self._before = []
        elif tag in ("br", "li", "tr", "td", "p", "div", "span", "em", "strong"):
            self.handle_data(" ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = max(0, self._skip - 1)
        elif tag == "a":
            self._close()
        elif tag in ("li", "tr", "td", "p", "div", "span", "em", "strong"):
            self.handle_data(" ")

    def handle_data(self, data):
        if self._skip:
            return
        if self._current:
            self._current["parts"].append(data)
        else:
            self._before.append(data)
            if self.links:
                self.links[-1]["after"].append(data)

    def _close(self):
        if self._current:
            self.links.append(self._current)
            self._current = None


def links(html):
    parser = _Links()
    parser.feed(html)
    parser.close()
    parser._close()
    return parser.links


# ---------- marxists.org (Portuguese section) ----------

def parse_mia_index(html, base=MIA_INDEX):
    """Return [(author name as listed, author page URL)]."""
    authors, seen = [], set()
    for link in links(html):
        name = squash("".join(link["parts"]))
        try:
            url = clean_url(urljoin(base, link["href"]))
        except (FetchError, ValueError):
            continue
        match = re.fullmatch(r"/portugues/([^/]+)/(index\.html?)?", urlsplit(url).path)
        if not match or not name or match.group(1) in ("admin", "tematica", "dicionario"):
            continue
        if (name, url) not in seen:
            seen.add((name, url))
            authors.append((name, url))
    return authors


FORMAT_LABEL = re.compile(r"^\(?\s*(html|pdf|epub|mobi|audiolivro.*)\s*\)?$", re.I)


def parse_mia_author(html, page_url):
    """Return [{title, url, year, pdf_url, marker}] for one author page."""
    works, by_url, before = [], {}, ""
    page_path = urlsplit(page_url).path
    for link in links(html):
        before += " " + link["before"]
        title = squash("".join(link["parts"]))
        try:
            url = clean_url(urljoin(page_url, link["href"]))
        except (FetchError, ValueError):
            continue
        path = urlsplit(url).path
        lower = path.lower()
        parts = path.split("/")[1:]
        if (HOSTS.get(urlsplit(url).hostname) != "mia" or len(parts) < 3 or parts[0] != "portugues"
                or parts[1] in ("admin", "dicionario", "img", "glossario")
                or (parts[1] == "tematica" and (len(parts) < 4 or parts[2] != "livros"))
                or not lower.endswith((".htm", ".html", ".pdf"))
                or path == page_path
                or (len(parts) == 3 and parts[2].lower().startswith("index."))):
            continue
        if not title or FORMAT_LABEL.match(title):
            if lower.endswith(".pdf") and works and not works[-1]["pdf_url"]:
                works[-1]["pdf_url"] = url
            continue
        years = re.findall(r"\b(?:1[5-9]\d\d|20\d\d)\b", before)
        marker = re.search(r"(?:^|\s)(M/E|M|E)\s*$", before)
        before = ""
        if url in by_url:
            continue
        work = {
            "title": title,
            "url": url,
            "year": years[-1] if years else "",
            "pdf_url": url if lower.endswith(".pdf") else "",
            "marker": marker.group(1) if marker else "",
        }
        by_url[url] = work
        works.append(work)
    return works


# ---------- theanarchistlibrary.org ----------

DATE_PAGES = re.compile(
    r"\s*(?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2}, \d{4})?"
    r"\s*(?:\d+ ?pp?\.)?\s*$"
)


def parse_tal_listing(html, base=TAL_LISTING):
    """Return ([{title, author, url}], highest listing page number)."""
    works, seen, last = [], set(), 1
    for link in links(html):
        try:
            url = clean_url(urljoin(base, link["href"]))
        except (FetchError, ValueError):
            continue
        parts = urlsplit(url)
        if HOSTS.get(parts.hostname) != "tal":
            continue
        page = re.search(r"(?:^|&)page=(\d+)", parts.query)
        if page and parts.path == "/listing":
            last = max(last, int(page.group(1)))
        if not re.fullmatch(r"/library/[^/.]+", parts.path) or url in seen:
            continue
        inside = squash("".join(link["parts"]))
        after = squash("".join(link["after"]))
        text = DATE_PAGES.sub("", inside)
        if after.startswith("—"):  # author written outside the link
            text = DATE_PAGES.sub("", f"{text} {after}")
        if " — " in text:
            title, author = text.rsplit(" — ", 1)
        else:
            title, author = text, ""
        if title:
            seen.add(url)
            works.append({"title": title.strip(), "author": author.strip(), "url": url})
    return works, last


# ---------- turning a source page into a clean reading view ----------

KEEP = set("a abbr b big blockquote br caption center cite code dd del dfn div dl dt em figure "
           "figcaption h1 h2 h3 h4 h5 h6 hr i img ins li ol p pre q s small span strong sub sup "
           "table tbody td tfoot th thead tr tt u ul".split())
DROP_WITH_CONTENT = {"script", "style", "noscript", "iframe", "object", "title", "template", "svg", "select"}
VOID = {"br", "hr", "img"}
BLOCKS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote", "pre", "dt", "dd", "br", "hr"}


class _Cleaner(HTMLParser):
    """Rebuilds a page from an allow-list, so no scripts or forms survive."""

    def __init__(self, base, rewrite):
        super().__init__(convert_charrefs=True)
        self.base, self.rewrite = base, rewrite
        self.out, self.stack, self.title = [], [], []
        self.skip = []

    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            self.skip.append(tag)
            return
        if self.skip or tag not in KEEP:
            return
        attrs = {k: v for k, v in attrs if v is not None}
        kept = []
        for name in ("id", "name", "title", "alt", "colspan", "rowspan"):
            if name in attrs:
                kept.append((name, attrs[name]))
        try:
            if tag == "a" and attrs.get("href"):
                href = attrs["href"].strip()
                if href.startswith("#"):
                    kept.append(("href", href))
                else:
                    full = urljoin(self.base, href)
                    if urlsplit(full).scheme in ("http", "https"):
                        local = self.rewrite(full)
                        if local:
                            kept.append(("href", local))
                            if urlsplit(full).path.lower().endswith(".pdf"):
                                # PDF viewers do not run inside the sandboxed reading frame.
                                kept += [("target", "_blank"), ("rel", "noopener")]
                        else:
                            kept += [("href", full), ("target", "_blank"), ("rel", "noopener noreferrer")]
            if tag == "img":
                src = clean_url(urljoin(self.base, attrs.get("src", "")))
                kept.append(("src", src))
        except (FetchError, ValueError):
            if tag == "img":
                return
        text = "".join(f' {k}="{escape(v)}"' for k, v in kept)
        self.out.append(f"<{tag}{text}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip[-1]:
                self.skip.pop()
            return
        if tag in self.stack:
            while self.stack:
                top = self.stack.pop()
                self.out.append(f"</{top}>")
                if top == tag:
                    break

    def handle_data(self, data):
        if self.skip:
            if self.skip[-1] == "title":
                self.title.append(data)
            return
        self.out.append(escape(data))


def escape(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def clean_html(html, base, rewrite=lambda url: None):
    """Return (title, safe body HTML)."""
    cleaner = _Cleaner(base, rewrite)
    cleaner.feed(html)
    cleaner.close()
    cleaner.out += [f"</{tag}>" for tag in reversed(cleaner.stack)]
    return squash("".join(cleaner.title)), "".join(cleaner.out)


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.pre = [], [], 0

    def handle_starttag(self, tag, attrs):
        if tag in DROP_WITH_CONTENT:
            self.skip.append(tag)
        elif tag in BLOCKS:
            self.out.append("\n")
        if tag == "pre":
            self.pre += 1

    def handle_endtag(self, tag):
        if self.skip and tag == self.skip[-1]:
            self.skip.pop()
        elif tag in BLOCKS:
            self.out.append("\n")
        if tag == "pre":
            self.pre = max(0, self.pre - 1)

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def to_text(html):
    parser = _Text()
    parser.feed(html)
    parser.close()
    text = "\n".join(line.strip() for line in "".join(parser.out).split("\n"))
    return re.sub(r"\n{3,}", "\n\n", text).strip() + "\n"
