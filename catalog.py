"""The merged catalogue: a small SQLite database."""
import json
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import sources
from sources import normalise

SCHEMA = """
CREATE TABLE IF NOT EXISTS authors(id INTEGER PRIMARY KEY, key TEXT UNIQUE, name TEXT, norm TEXT);
CREATE TABLE IF NOT EXISTS pages(author_id INTEGER, url TEXT, UNIQUE(author_id, url));
CREATE TABLE IF NOT EXISTS works(id INTEGER PRIMARY KEY, source TEXT, url TEXT UNIQUE, title TEXT,
  year TEXT, pdf_url TEXT, byline TEXT, norm TEXT, title_norm TEXT);
CREATE TABLE IF NOT EXISTS work_authors(work_id INTEGER, author_id INTEGER, UNIQUE(work_id, author_id));
CREATE INDEX IF NOT EXISTS by_author ON work_authors(author_id);
CREATE TABLE IF NOT EXISTS done(url TEXT PRIMARY KEY, source TEXT);
"""


class Catalog:
    def __init__(self, path, fetcher, aliases_file=None):
        self.path = str(path)
        self.fetcher = fetcher
        self.aliases = {}
        if aliases_file and Path(aliases_file).exists():
            raw = json.loads(Path(aliases_file).read_text("utf-8"))
            self.aliases = {self._key(a): self._key(b) for a, b in raw.items()}
        self.write_lock = threading.RLock()
        self.status = {s: {"running": False, "message": "Not started", "errors": 0} for s in ("mia", "tal")}
        with closing(self._db()) as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript(SCHEMA)

    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def _key(name):
        return " ".join(sorted(normalise(name).split()))

    def _author(self, db, name):
        if name.count(",") == 1:  # "Goldman, Emma" -> "Emma Goldman"
            last, first = (p.strip() for p in name.split(","))
            name = f"{first} {last}"
        key = self._key(name)
        key = self.aliases.get(key, key)
        if not key:
            return None
        db.execute("INSERT OR IGNORE INTO authors(key, name, norm) VALUES(?,?,?)", (key, name, normalise(name)))
        row = db.execute("SELECT id, norm FROM authors WHERE key=?", (key,)).fetchone()
        # Remember alternative spellings so that both can be searched.
        missing = [w for w in normalise(name).split() if w not in row["norm"].split()]
        if missing:
            db.execute("UPDATE authors SET norm=? WHERE id=?", (row["norm"] + " " + " ".join(missing), row["id"]))
        return row["id"]

    def _work(self, db, source, work, author_ids, byline):
        title_norm = normalise(work["title"])
        db.execute(
            "INSERT INTO works(source,url,title,year,pdf_url,byline,norm,title_norm) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(url) DO UPDATE SET title=excluded.title, "
            "pdf_url=CASE WHEN excluded.pdf_url<>'' THEN excluded.pdf_url ELSE works.pdf_url END",
            (source, work["url"], work["title"], work.get("year", ""), work.get("pdf_url", ""), byline,
             f"{title_norm} {normalise(byline)}", title_norm))
        work_id = db.execute("SELECT id FROM works WHERE url=?", (work["url"],)).fetchone()[0]
        for author_id in author_ids:
            db.execute("INSERT OR IGNORE INTO work_authors VALUES(?,?)", (work_id, author_id))

    # ----- importing -----

    def add_mia_authors(self, authors):
        with self.write_lock, closing(self._db()) as db, db:
            for name, url in authors:
                author_id = self._author(db, name)
                if author_id:
                    db.execute("INSERT OR IGNORE INTO pages VALUES(?,?)", (author_id, url))

    def add_mia_page(self, url, works):
        with self.write_lock, closing(self._db()) as db, db:
            owners = db.execute("SELECT a.id, a.name FROM pages p JOIN authors a ON a.id=p.author_id "
                                "WHERE p.url=?", (url,)).fetchall()
            for work in works:
                chosen = owners
                if len(owners) > 1 and work["marker"]:  # e.g. the shared Marx/Engels page
                    initials = work["marker"].split("/")
                    chosen = [o for o in owners if o["name"].split()[-1][:1] in initials] or owners
                byline = ", ".join(o["name"] for o in chosen)
                self._work(db, "mia", work, [o["id"] for o in chosen], byline)
            if works:
                db.execute("INSERT OR IGNORE INTO done VALUES(?, 'mia')", (url,))

    def add_tal_page(self, url, works):
        with self.write_lock, closing(self._db()) as db, db:
            for work in works:
                author_id = self._author_plain(db, work["author"])
                self._work(db, "tal", work, [author_id] if author_id else [], work["author"])
            db.execute("INSERT OR IGNORE INTO done VALUES(?, 'tal')", (url,))

    def _author_plain(self, db, name):
        # Names on this site are already "First Last"; commas separate co-authors.
        if "," in name:
            key = self._key(name)
            db.execute("INSERT OR IGNORE INTO authors(key,name,norm) VALUES(?,?,?)", (key, name, normalise(name)))
            return db.execute("SELECT id FROM authors WHERE key=?", (key,)).fetchone()[0]
        return self._author(db, name) if name else None

    def link_coauthors(self):
        """Also list co-written texts under each co-author who has a page of their own."""
        with self.write_lock, closing(self._db()) as db, db:
            known = {r["key"]: r["id"] for r in db.execute("SELECT id,key FROM authors")}
            rows = db.execute("SELECT id, byline FROM works WHERE source='tal'").fetchall()
            for row in rows:
                parts = re.split(r",\s*|\s+(?:and|&)\s+", row["byline"])
                if len(parts) < 2:
                    continue
                for part in parts:
                    key = self._key(part)
                    author_id = known.get(self.aliases.get(key, key))
                    if author_id:
                        db.execute("INSERT OR IGNORE INTO work_authors VALUES(?,?)", (row["id"], author_id))

    def _done(self, url):
        with closing(self._db()) as db:
            return db.execute("SELECT 1 FROM done WHERE url=?", (url,)).fetchone() is not None

    def import_mia(self):
        status = self.status["mia"]
        status["message"] = "Reading the author list"
        html, final = self.fetcher.crawl(sources.MIA_INDEX)
        authors = sources.parse_mia_index(html, final)
        if not authors:
            raise sources.FetchError("No authors found: the page layout may have changed")
        self.add_mia_authors(authors)
        urls = sorted({url for _, url in authors})
        for number, url in enumerate(urls, 1):
            status["message"] = f"Author pages {number} of {len(urls)}"
            if not self._done(url):
                self._try(status, lambda: self.index_mia_page(url, polite=True))

    def index_mia_page(self, url, polite=False):
        if polite:
            html, final = self.fetcher.crawl(url)
        else:
            data, ctype, final = self.fetcher.get(url, use_cache=False)
            html = sources.decode(data, ctype)
        self.add_mia_page(url, sources.parse_mia_author(html, final))

    def import_tal(self):
        status = self.status["tal"]
        page, last = 1, 1
        while page <= last:
            url = f"{sources.TAL_LISTING}?page={page}"
            status["message"] = f"Listing page {page}" + (f" of {last}" if page > 1 else "")
            # Page 1 is always read, to learn how many pages there are now.
            if page == 1 or not self._done(url):
                def step(url=url):
                    nonlocal last
                    html, final = self.fetcher.crawl(url)
                    works, found_last = sources.parse_tal_listing(html, final)
                    if not works:
                        raise sources.FetchError("No texts found: the page layout may have changed")
                    last = max(last, found_last)
                    self.add_tal_page(url, works)
                if page == 1:
                    step()
                else:
                    self._try(status, step)
            else:
                last = max(last, page)
            page += 1
        self.link_coauthors()

    def _try(self, status, action):
        try:
            action()
        except sources.FetchError as error:
            status["errors"] += 1
            status["last_error"] = str(error)
            if status["errors"] >= 25:
                raise sources.FetchError(f"Stopped after repeated errors. Last one: {error}")

    def start_import(self, source, refresh=False):
        status = self.status[source]
        with self.write_lock:
            if status["running"]:
                return
            status.update(running=True, errors=0, message="Starting")
            status.pop("last_error", None)

        def run():
            try:
                if refresh:
                    with self.write_lock, closing(self._db()) as db, db:
                        db.execute("DELETE FROM done WHERE source=?", (source,))
                (self.import_mia if source == "mia" else self.import_tal)()
                failed = status["errors"]
                status["message"] = f"Finished ({failed} pages failed; update to retry)" if failed else "Finished"
            except Exception as error:  # shown to the user in the app
                status["message"] = f"Stopped: {error}"
            finally:
                status["running"] = False

        threading.Thread(target=run, daemon=True).start()

    # ----- reading -----

    def counts(self):
        with closing(self._db()) as db:
            out = {s: db.execute("SELECT COUNT(*) FROM works WHERE source=?", (s,)).fetchone()[0]
                   for s in ("mia", "tal")}
            out["authors"] = db.execute("SELECT COUNT(*) FROM authors").fetchone()[0]
        return out

    def search(self, query, authors=6, works=10):
        q = normalise(query)
        words = q.split()
        if not words:
            return {"authors": [], "works": []}
        where = " AND ".join(["' '||norm LIKE ?"] * len(words))
        # Each typed word must match the start of a word; "_" is escaped.
        args = [f"% {w.replace('_', chr(92) + '_')}%" for w in words]
        where = where.replace("LIKE ?", "LIKE ? ESCAPE '\\'")
        prefix = q.replace("_", "\\_") + "%"
        with closing(self._db()) as db:
            found_authors = db.execute(
                "SELECT a.id, a.name, COUNT(w.work_id) AS count, "
                "EXISTS(SELECT 1 FROM pages p WHERE p.author_id=a.id) AS has_page "
                f"FROM authors a LEFT JOIN work_authors w ON w.author_id=a.id WHERE {where} "
                "GROUP BY a.id HAVING count>0 OR has_page ORDER BY count DESC, a.name LIMIT ?",
                (*args, authors)).fetchall()
            found_works = db.execute(
                f"SELECT id, title, byline, year, source FROM works WHERE {where} "
                "ORDER BY title_norm<>?, title_norm NOT LIKE ? ESCAPE '\\', length(title), title LIMIT ?",
                (*args, q, prefix, works)).fetchall()
        return {"authors": [dict(r) for r in found_authors], "works": [dict(r) for r in found_works]}

    def author(self, author_id):
        with closing(self._db()) as db:
            row = db.execute("SELECT id, name FROM authors WHERE id=?", (author_id,)).fetchone()
            if not row:
                return None
            pending = [r["url"] for r in db.execute(
                "SELECT url FROM pages WHERE author_id=? AND url NOT IN (SELECT url FROM done)", (author_id,))]
        warning = ""
        for url in pending:  # not catalogued yet: fetch this author right now
            try:
                self.index_mia_page(url)
            except sources.FetchError as error:
                warning = str(error)
        with closing(self._db()) as db:
            works = db.execute(
                "SELECT w.id, w.title, w.byline, w.year, w.source FROM works w "
                "JOIN work_authors x ON x.work_id=w.id WHERE x.author_id=? ORDER BY w.title_norm",
                (author_id,)).fetchall()
            pages = [r["url"] for r in db.execute("SELECT url FROM pages WHERE author_id=?", (author_id,))]
        return {**dict(row), "works": [dict(w) for w in works], "pages": pages, "warning": warning}

    def work(self, work_id):
        with closing(self._db()) as db:
            row = db.execute("SELECT id,title,byline,year,source,url,pdf_url FROM works WHERE id=?",
                             (work_id,)).fetchone()
            if not row:
                return None
            authors = db.execute("SELECT a.id, a.name FROM authors a JOIN work_authors x ON x.author_id=a.id "
                                 "WHERE x.work_id=?", (work_id,)).fetchall()
        work = {**dict(row), "authors": [dict(a) for a in authors]}
        if work["source"] == "tal":
            work["pdf_url"] = work["url"] + ".pdf"
        work["has_html"] = not work["url"].lower().endswith(".pdf")
        return work
