# Leftist Library

One search box over two libraries: the Portuguese section of marxists.org and The Anarchist Library (English).

## Start

1. Install Python 3.9 or newer (python.org/downloads). On Windows, tick "Add Python to PATH". No other packages are needed.
2. Unzip the folder.
3. Windows: double-click `start-windows.bat`. Mac: right-click `start-mac.command` and choose Open
   (if macOS blocks it, run `python3 app.py` in Terminal from this folder). Linux: `./start-linux.sh`.
4. Your browser opens at http://127.0.0.1:8765. Keep the black window open while you read.

## First run

The app builds its catalogue from both sites at about one page per second, so allow roughly 10 minutes.
You can search while it runs. If you close it, it resumes next time. "Update catalogue" re-reads a source.

**If a source shows an error or 0 texts:** run `python app.py --check` (Mac/Linux: `python3`) and send the output
to whoever is fixing the app.

## Speed

Search and autofill run on your computer. A text is downloaded from its source the first time you open it
(or when you rest the pointer on it), then kept in `data/cache`, so it opens from disk afterwards.
Delete `data/cache` to free space or to get fresh copies.

## Formats

- Web page: a clean reading view. Chapter links stay inside the app.
- PDF: always offered for The Anarchist Library; on marxists.org only where a PDF is listed.
- Plain text: made from the web page. For a book split into chapters on marxists.org, it covers the index page only.

## Look

The fonts (Alegreya and Old Standard TT, SIL Open Font License) are included in `static/fonts`.
The site is always dark. The reading view offers Night, Sepia and Paper, text size and full screen.
Your settings and reading positions are kept in your browser. The three symbols are drawn
in `static/index.html`.

## Rights

The app is for personal reading. It runs only on your computer and shows each text with a link to its source.
"Free to read" does not always mean free to republish. marxists.org says some of its material is copyrighted
and marked as such, and asks to be credited with the URL (marxists.org/admin/legal/terms-of-use.htm).
Check this for both sites before putting the app on the public web.

## Matching authors

The two sites spell some names differently. Add pairs to `aliases.json` before building the catalogue
(to apply changes later, delete `data/catalog.db`).
