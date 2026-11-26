"""Leftist Library: run `python app.py` and use it in your browser."""
import argparse
import json
import re
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

if sys.version_info < (3, 9):
    sys.exit("Please install Python 3.9 or newer (python.org/downloads).")

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import sources  # noqa: E402
from catalog import Catalog  # noqa: E402

READER_CSS = (HERE / "static" / "fonts.css").read_text("utf-8") + (HERE / "static" / "reader.css").read_text("utf-8")
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline'; font-src 'self'; img-src https://www.marxists.org "
            "https://theanarchistlibrary.org; frame-ancestors 'self'")
APP_CSP = ("default-src 'self'; img-src 'self' data:; script-src 'self' 'unsafe-inline'; "
           "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'")


def local_link(url):
    """Links to other pages of the two libraries stay inside the app."""
    try:
        safe = sources.clean_url(url)
    except (sources.FetchError, ValueError):
        return None
    path = urlsplit(safe).path.lower()
    last = path.rsplit("/", 1)[-1]
    if not (path.endswith((".htm", ".html", ".pdf")) or "." not in last):
        return None
    fragment = urlsplit(url).fragment
    return "/view?url=" + quote(safe, safe="") + (("#" + quote(fragment)) if fragment else "")


def reading_page(title, body, source_url):
    # The language lets the browser hyphenate correctly.
    lang = "pt" if "marxists.org/portugues/" in source_url else "en"
    return (f"<!doctype html><html lang='{lang}'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{sources.escape(title)}</title><style>{READER_CSS}</style></head><body>"
            f"<div class='src'>Source: <a href='{sources.escape(source_url)}' target='_blank' "
            f"rel='noopener noreferrer'>{sources.escape(source_url)}</a></div>{body}</body></html>")


class Handler(BaseHTTPRequestHandler):
    catalog = None
    fetcher = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def send(self, body, ctype="application/json", status=200, headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def local_only(self):
        # Refuse requests that arrive under any other host name.
        host = self.headers.get("Host", "")
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def do_GET(self):
        if not self.local_only():
            return self.send({"error": "forbidden"}, status=403)
        parts = urlsplit(self.path)
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        try:
            self.route(parts.path, query)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except sources.FetchError as error:
            self.problem(parts.path, str(error), 502)
        except (KeyError, ValueError):
            self.problem(parts.path, "That request was not understood.", 400)
        except Exception as error:
            self.problem(parts.path, f"Unexpected error: {error}", 500)

    def problem(self, path, message, status):
        if path.startswith("/api/"):
            return self.send({"error": message}, status=status)
        page = (f"<!doctype html><meta charset='utf-8'><style>{READER_CSS}</style>"
                f"<h2>This could not be opened</h2><p>{sources.escape(message)}</p>")
        self.send(page, "text/html; charset=utf-8", status, {"Content-Security-Policy": PAGE_CSP})

    def route(self, path, query):
        catalog = self.catalog
        if path == "/":
            return self.send((HERE / "static" / "index.html").read_bytes(), "text/html; charset=utf-8",
                             headers={"Content-Security-Policy": APP_CSP})
        if path == "/fonts.css":
            return self.send((HERE / "static" / "fonts.css").read_bytes(), "text/css; charset=utf-8")
        font = re.fullmatch(r"/fonts/([a-z0-9-]+\.woff2)", path)
        if font and (HERE / "static" / "fonts" / font.group(1)).is_file():
            return self.send((HERE / "static" / "fonts" / font.group(1)).read_bytes(), "font/woff2",
                             headers={"Cache-Control": "max-age=86400"})
        if path == "/api/status":
            return self.send({"counts": catalog.counts(), "sources": catalog.status})
        if path == "/api/search":
            limit = (100, 300) if query.get("all") else (6, 10)
            return self.send(catalog.search(query.get("q", "")[:200], *limit))
        match = re.fullmatch(r"/api/(author|work)/(\d{1,12})", path)
        if match:
            found = getattr(catalog, match.group(1))(int(match.group(2)))
            return self.send(found) if found else self.send({"error": "Not found"}, status=404)
        match = re.fullmatch(r"/(html|pdf|txt|prefetch)/(\d{1,12})", path)
        if match:
            work = catalog.work(int(match.group(2)))
            if not work:
                return self.problem(path, "Not found", 404)
            return getattr(self, "show_" + match.group(1))(work)
        if path == "/view":
            return self.show_url(query["url"])
        self.problem(path, "Not found", 404)

    def text_page(self, work):
        """Return (bytes, content type, final URL) of the readable version."""
        if work["source"] == "tal":
            try:
                return self.fetcher.get(work["url"] + ".bare.html")
            except sources.FetchError:
                return self.fetcher.get(work["url"] + ".html")
        return self.fetcher.get(work["url"])

    def show_html(self, work):
        self.render(*self.text_page(work), source=work["url"], title=work["title"])

    def show_url(self, url):
        url = sources.clean_url(url)
        if re.fullmatch(r"https://theanarchistlibrary\.org/library/[^/.]+", url):
            return self.render(*self.text_page({"source": "tal", "url": url}), source=url)
        self.render(*self.fetcher.get(url), source=url)

    def render(self, data, ctype, final, source, title=""):
        if data[:5] == b"%PDF-":
            return self.send(data, "application/pdf", headers={"Content-Disposition": "inline"})
        if "html" not in ctype.lower() and not final.lower().endswith((".htm", ".html")):
            raise sources.FetchError("This file type cannot be shown here. Open it on the source site.")
        page_title, body = sources.clean_html(sources.decode(data, ctype), final, local_link)
        self.send(reading_page(title or page_title, body, source), "text/html; charset=utf-8",
                  headers={"Content-Security-Policy": PAGE_CSP})

    def show_pdf(self, work):
        if not work["pdf_url"]:
            raise sources.FetchError("The source library lists no PDF for this text.")
        data, _, _ = self.fetcher.get(work["pdf_url"])
        if data[:5] != b"%PDF-":
            raise sources.FetchError("The source site did not return a PDF for this text.")
        self.send(data, "application/pdf", headers={"Content-Disposition": "inline"})

    def show_txt(self, work):
        if not work["has_html"]:
            raise sources.FetchError("This text exists only as a PDF.")
        data, ctype, _ = self.text_page(work)
        if data[:5] == b"%PDF-":
            raise sources.FetchError("This text exists only as a PDF.")
        text = f"{work['title']}\n{work['byline']}\nSource: {work['url']}\n\n"
        text += sources.to_text(sources.decode(data, ctype))
        name = re.sub(r"[^a-z0-9]+", "-", sources.normalise(work["title"]))[:60].strip("-") or "text"
        # Names with non-Latin letters only fall back to "text".
        self.send(text, "text/plain; charset=utf-8",
                  headers={"Content-Disposition": f'inline; filename="{name}.txt"'})

    def show_prefetch(self, work):
        """Load a text into the cache before it is clicked."""
        try:
            if work["has_html"]:
                self.text_page(work)
        except sources.FetchError:
            pass
        self.send({"ok": True})

    def do_POST(self):
        # The custom header stops other websites from triggering imports.
        if not self.local_only() or self.headers.get("X-BookSearch") != "1":
            self.close_connection = True
            return self.send({"error": "forbidden"}, status=403)
        match = re.fullmatch(r"/api/import/(mia|tal)", self.path)
        if not match:
            self.close_connection = True
            return self.send({"error": "Not found"}, status=404)
        self.catalog.start_import(match.group(1), refresh=True)
        self.send({"ok": True})


def make_server(data_dir, port):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    fetcher = sources.Fetcher(data_dir / "cache")
    catalog = Catalog(data_dir / "catalog.db", fetcher, HERE / "aliases.json")
    handler = type("BoundHandler", (Handler,), {"catalog": catalog, "fetcher": fetcher})
    return ThreadingHTTPServer(("127.0.0.1", port), handler), catalog


def check():
    """Quick test that both sites can be reached and understood."""
    import tempfile
    with tempfile.TemporaryDirectory() as folder:
        fetcher = sources.Fetcher(folder)
        ok = True
        try:
            html, final = fetcher.crawl(sources.MIA_INDEX)
            authors = sources.parse_mia_index(html, final)
            print(f"marxists.org: {len(authors)} authors found")
            url = next((u for _, u in authors if u.endswith("/goldman/index.htm")), authors[0][1])
            html, final = fetcher.crawl(url)
            works = sources.parse_mia_author(html, final)
            print(f"  {url}: {len(works)} texts; first: {[w['title'] for w in works[:3]]}")
            ok &= bool(authors and works)
        except Exception as error:
            print("marxists.org FAILED:", error)
            ok = False
        try:
            html, final = fetcher.crawl(sources.TAL_LISTING)
            works, last = sources.parse_tal_listing(html, final)
            print(f"theanarchistlibrary.org: {len(works)} texts on page 1 of {last}")
            for work in works[:3]:
                print(f"  {work['title']!r} by {work['author']!r}")
            if works:
                for suffix in (".bare.html", ".pdf"):
                    data, _, _ = fetcher.get(works[0]["url"] + suffix)
                    print(f"  {suffix}: {len(data)} bytes")
            ok &= bool(works) and all(w["author"] for w in works[:3])
        except Exception as error:
            print("theanarchistlibrary.org FAILED:", error)
            ok = False
    print("All checks passed." if ok else "Something failed: copy this output and send it to be fixed.")
    return 0 if ok else 1


def main():
    options = argparse.ArgumentParser(description=__doc__)
    options.add_argument("--port", type=int, default=8765)
    options.add_argument("--data", default=str(HERE / "data"))
    options.add_argument("--no-browser", action="store_true")
    options.add_argument("--no-import", action="store_true")
    options.add_argument("--check", action="store_true", help="test the connection to both sites")
    args = options.parse_args()
    if args.check:
        sys.exit(check())
    try:
        server, catalog = make_server(args.data, args.port)
    except OSError as error:
        sys.exit(f"Could not start on port {args.port} ({error}). Is the app already open? Try --port 8766.")
    if not args.no_import:
        for source in ("mia", "tal"):
            catalog.start_import(source)  # resumes where it stopped
    address = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Leftist Library is running at {address}\nKeep this window open. Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.5, webbrowser.open, [address]).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
