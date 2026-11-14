# Google Play Books - Android Notes Exporter

## Why
This tool can be handy, if you run into sync issues of google play books and don't find an option to backup/export your annotations & highlights you made on your Google Play Books Android app (e.g. [see](https://www.reddit.com/r/GooglePlayBooks/comments/1xq08bf/noteshighlights_from_android_boox_go_6_not/) [these](https://support.google.com/googleplay/thread/453109335/notes-highlights-not-syncing-across-devices-in-google-play-books-page-position-sync-works-fine) [issues](https://support.google.com/googleplay/thread/383293083/play-books-highlight-sync-issue?hl=en))

## Prerequisits
- Android Device
   - with Google Play Books which has notes and/or highlights you want to export
   - Developer Mode activated on your Android device

- Computer
   - Python 3.10+ 
   - Android Platform Tools (adb)

- USB Cable to connect Android and computer



## How to use

1. Connect BOOX over USB, enable USB debugging and approve the computer on the device.
2. Open the desired book in Play Books, then go to the nots overview via the burger menue (which you can access in the "page scrollig" mode reachable via a single tap on any page)
3. Scroll to the very top yourself. Keep this screen open, and the orientation fixed throughout capture.
4. In PowerShell , run:

```powershell
python .\backup_playbooks.py --adb C:\adb\adb.exe --output E:\downloads\boox\backups
```

The script captures XML, immediately pulls it to the PC, then slowly swipes upward inside the Notes list. Coordinates come from the actual XML, not a fixed screen resolution. 

Each run creates a separate timestamped folder containing raw XML, readable annotations.md, Excel-friendly annotations.csv, annotations.json, all ungrouped observations.json, and run.json with the stop reason. 

It starts at the CURRENT scroll position, not automatically at the beginning. Three unchanged captures following swipes indicate no detected movement, not proof of completeness. Verify the first and last captured entries. Default maximum is 500 screens; reaching that limit is recorded in run.json.

The export preserves annotation text as provided by the Notes list.  This backup cannot restore annotations into Play Books. Exact identical rows are grouped for readability; all original observations remain available, including incomplete edge rows. Different annotations with identical text/fields can be grouped, and partial versions of a row may appear separately.

Options:

```powershell
python .\backup_playbooks.py --help
python .\backup_playbooks.py --delay 4 --max-screens 1000 --output E:\downloads\boox\backups
python .\backup_playbooks.py --serial DEVICE_SERIAL --output E:\downloads\boox\backups
```

Re-export existing XML offline (never connects to the device):

```powershell
python .\backup_playbooks.py --import-xml E:\downloads\boox --output E:\downloads\boox\backups
```

No automatic wake, settings changes, scrolling to the top, or navigation into individual notes is performed. If the list disappears or the book title changes, the script stops.

## Embed annotations in a separate EPUB

Provide the original EPUB and an `annotations.json` export. This mode works offline and does not use ADB.
Keep `epub_annotations.py` beside `backup_playbooks.py`.

```powershell
python .\backup_playbooks.py --epub "C:\Books\My book.epub" --annotations "C:\Backups\final.json"
```

This creates `My book-annotated.epub` beside the original, plus
`My book-annotated.annotations-report.json`. To choose another output path:

```powershell
python .\backup_playbooks.py --epub "C:\Books\My book.epub" --annotations "C:\Backups\final.json" --epub-output "C:\Backups\My annotated book.epub"
```

The original EPUB and input JSON are not modified. Existing output files are
not overwritten; choose a different name for another conversion. An input
EPUB that already contains our W3C annotation set or source archive is rejected.

The new EPUB contains annotations in **both formats**:

- **EPUB Annotations 1.0:** an `AnnotationSet` in `META-INF/annotations.json`, following
the [EPUB Annotations 1.0 Working Draft of 15 October 2026](https://www.w3.org/TR/2026/WD-epub-anno-10-20261015/).
  This specification is still a draft, and display depends on reader support.
- **Calibre:** highlights and notes in `META-INF/calibre_bookmarks.txt`, using
  Calibre's JSON/base64 encoding, document-local CFI ranges, spine references and
  yellow highlight style. These can be read by the Calibre E-book viewer.

Both formats are written by default with the same command. Open the **new output EPUB directly** in the
Calibre E-book viewer. An older library copy of the book does not automatically
receive these annotations. Highlights are editable reader annotations.

Existing Calibre bookmarks/highlights are retained alongside the added
highlights. The original EPUB version and all chapter, image, font and
stylesheet contents are preserved. Only the new copy's Calibre annotation file
is created or updated. Note: this does not import annotations back into Google Play Books if you reupload the epub.

### How matching works

`epub_annotations.py` reads XHTML documents in the EPUB spine, then searches for
each selected passage. It normalizes whitespace and ignores discretionary soft
hyphens and invisible separators, while retaining a map to original character
offsets. Unique matches become `TextPositionSelector` ranges in the original
body text. Chapter headings help narrow repeated passages. Page numbers
are preserved in the report, but are not treated as reliable EPUB locations.
Spelling, punctuation and letter case are not fuzzy-matched.

Comments from `fields.note` become annotation bodies; a highlighted passage is
not itself treated as a comment. Original colors and annotation dates are absent
from the extraction format. Readers use their default highlight styling, and
the required `created` value records conversion time, not original reading time.
Stable annotation UUIDs are derived from publication identity, text range and
comment, so repeated captures of the same range/comment are grouped.

Calibre CFI generation maps the same matched ranges to DOM text nodes. It counts
browser offsets in UTF-16 units (important for emoji), handles selections across
inline elements and keeps starts at text-node boundaries from including preceding
inline text. The matching report also records the number of Calibre highlights
added and existing Calibre entries retained. Format references:
[Calibre's annotation writer](https://github.com/kovidgoyal/calibre/blob/master/src/calibre/gui2/viewer/annotations.py),
[Calibre's CFI implementation](https://github.com/kovidgoyal/calibre/blob/master/src/pyj/read_book/cfi.pyj).

The report records each input row as `embedded`, `duplicate`, `empty_text`,
`not_found` or `ambiguous`. Ambiguous matches include candidate locations and
surrounding text; they are not silently attached to an arbitrary occurrence.
Empty or unresolved rows are retained in `META-INF/playbooks-source.json`
alongside every original input row, so nothing is discarded. 
