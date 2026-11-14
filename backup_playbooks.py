"""Back up the currently open Play Books Notes list using ADB.

Workflow: capture XML on the reader -> copy it to the PC -> read the rows ->
scroll -> repeat. Finally, turn the saved XML into Markdown, CSV and JSON.
The user opens the book and scrolls to the top before starting the script.
Requires Python 3.10+; uses only the Python standard library.
"""
import argparse
import csv
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime

PACKAGE = 'com.google.android.apps.books'


def find(root, suffix):
    """Find a UI element by the last part of its Android resource ID.

    Resource IDs identify controls independently of the interface language;
    for example, notes_list_view works with both Notes and Notizen.
    Return None if the control does not exist in this screen dump.
    """
    return next((n for n in root.iter('node') if n.get('resource-id', '').endswith(':id/' + suffix)), None)


def bounds(node):
    """Convert Android's '[left,top][right,bottom]' string to four pixels."""
    values = list(map(int, re.findall(r'\d+', node.get('bounds', ''))))
    if len(values) != 4:
        raise ValueError('Cannot read list bounds')
    return values


def parse(path):
    """Read one saved screen: book title, list element, rows, screen signature.

    A dump is a tree of UI controls. Each direct child of notes_list_view is
    one annotation row; its descendants contain chapter, location and text.
    Keep every text field as well as the familiar fields so unknown controls
    are preserved rather than silently discarded.
    """
    root = ET.parse(path).getroot()
    listing = find(root, 'notes_list_view')
    if listing is None or listing.get('package') != PACKAGE:
        raise ValueError('Play Books Notes list is not visible. Open Contents > Notes first.')
    # The book title is displayed in the toolbar above the Notes list.
    toolbar = find(root, 'toolbar')
    title = next((n.get('text') for n in toolbar.iter('node') if n.get('text')), '') if toolbar is not None else ''
    rows = []
    for row in listing:
        fields = {}
        for node in row.iter('node'):
            text = node.get('text', '')
            if text:
                # Multiple controls may have the same ID: retain all their text.
                fields.setdefault(node.get('resource-id', '').split('/')[-1] or 'unidentified', []).append(text)
        if not fields:
            continue
        # 'snippet' is the app's resource ID for the annotation text control;
        # the name alone does not mean that the captured text is shortened.
        rows.append({'chapter': '\n'.join(fields.get('title', [])),
                     'location': '\n'.join(fields.get('subtitle_1', [])),
                     'text': '\n'.join(fields.get('snippet', [])),
                     'fields': fields, 'bounds': row.get('bounds', '')})
    # Include geometry: identical text can remain visible while the list still moves.
    fingerprint = hashlib.sha256(json.dumps([(r['fields'], r['bounds']) for r in rows], ensure_ascii=False).encode()).hexdigest()
    return title, listing, rows, fingerprint


def export(folder):
    """Build readable exports entirely from the XML already saved on the PC.

    This also works offline and after an interrupted capture. Observations
    retain every row from every screen; annotations groups exact repetitions
    caused by overlapping scrolls. Raw XML is never modified.
    """
    observations = []
    for path in sorted(folder.glob('*.xml')):
        try:
            title, _, rows, _ = parse(path)
        except (ValueError, ET.ParseError) as exc:
            print(f'Skipping {path.name}: {exc}', file=sys.stderr)
            continue
        for row in rows:
            observations.append({'book': title, 'source': path.name, **row})
    # Raw observations retain all repetitions; the convenience view groups exact matches only.
    unique = {}
    for row in observations:
        # Ignore screen coordinates when grouping: the same annotation moves
        # vertically between captures. Include all text fields and the book.
        key = json.dumps([row['book'], row['fields']], ensure_ascii=False, sort_keys=True)
        if key not in unique:
            unique[key] = {**row, 'sources': []}
        unique[key]['sources'].append(row['source'])
    entries = list(unique.values())
    (folder / 'observations.json').write_text(json.dumps(observations, ensure_ascii=False, indent=2), encoding='utf-8')
    (folder / 'annotations.json').write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding='utf-8')
    # UTF-8 with a BOM lets Excel recognise German characters automatically.
    with (folder / 'annotations.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Book', 'Chapter', 'Location', 'Annotation text', 'All text fields', 'Source XML files'])
        for row in entries:
            writer.writerow([row['book'], row['chapter'], row['location'], row['text'], json.dumps(row['fields'], ensure_ascii=False), '; '.join(row['sources'])])
    text = ['# Play Books visible annotation text', '',
            'Text captured from the Notes list. This is not an importable app backup.',
            'Exact matching rows are grouped here; observations.json and raw XML preserve repetitions.', '']
    for row in entries:
        text += [f"## {row['book']} — {row['chapter']} — {row['location']}", '', row['text'] or '(No annotation text in this row)', '',
                 'All text fields: ' + json.dumps(row['fields'], ensure_ascii=False), '', 'Sources: ' + ', '.join(row['sources']), '']
    (folder / 'annotations.md').write_text('\n'.join(text), encoding='utf-8')
    print(f'Exported {len(entries)} distinct visible rows from {len(observations)} observations to {folder}')


def main():
    """Choose offline export or live capture, then save the resulting backup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--adb', default='adb', help='ADB executable path')
    parser.add_argument('--serial', help='Device serial, required when multiple devices are connected')
    parser.add_argument('--output', type=Path, default=Path.cwd() / 'playbooks-backups')
    parser.add_argument('--import-xml', type=Path, help='Offline export of XML files in this folder; does not connect to ADB')
    parser.add_argument('--max-screens', type=int, default=500)
    parser.add_argument('--delay', type=float, default=2.5, help='Seconds to settle after each swipe')
    parser.add_argument('--stable', type=int, default=3, help='Unchanged captures before stopping')
    parser.add_argument('--epub', type=Path, help='Create an annotated copy of this EPUB (offline mode)')
    parser.add_argument('--annotations', type=Path, help='Extractor JSON array to embed; requires --epub')
    parser.add_argument('--epub-output', type=Path, help='New EPUB path; default: INPUT-annotated.epub')
    args = parser.parse_args()
    # EPUB conversion is a separate offline mode. Return before creating a
    # capture folder or making any ADB connection; the live loop stays unchanged.
    if args.epub or args.annotations or args.epub_output:
        if not args.epub or not args.annotations or args.import_xml:
            parser.error('Use --epub and --annotations together, without --import-xml')
        from epub_annotations import embed_annotations
        try:
            embed_annotations(args.epub, args.annotations, args.epub_output)
        except (ValueError, OSError, ET.ParseError) as exc:
            parser.exit(1, f'EPUB conversion failed: {exc}\n')
        return
    if args.max_screens < 1 or args.delay < 0 or args.stable < 2:
        parser.error('Use max-screens >= 1, delay >= 0, stable >= 2')
    # A timestamp plus random suffix prevents separate runs overwriting backups.
    folder = args.output.resolve() / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    folder.mkdir(parents=True)
    if args.import_xml:
        # Offline mode copies the originals into a new run folder and exits
        # before any ADB discovery, device connection or scrolling happens.
        files = sorted(args.import_xml.glob('*.xml'))
        if not files:
            parser.error('No XML files in import folder')
        for path in files:
            shutil.copy2(path, folder / path.name)
        export(folder)
        return
    adb = shutil.which(args.adb)
    if not adb:
        parser.error('ADB not found. Use --adb C:\\adb\\adb.exe')
    base = [adb]

    def run(*parts):
        """Run one ADB command; raise on failure instead of continuing blindly.

        Pass arguments as a list, without a shell, so spaces in paths are safe.
        Once selected, the device serial is included in every device command.
        """
        result = subprocess.run(base + list(parts), capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip() or 'ADB command failed')
        return result.stdout

    # Only 'device' entries are ready; ignore offline/unauthorised devices.
    # Refuse to guess if more than one authorised device is connected.
    devices = run('devices').decode().splitlines()[1:]
    ready = [line.split()[0] for line in devices if len(line.split()) == 2 and line.split()[1] == 'device']
    if args.serial:
        if args.serial not in ready:
            parser.error('Requested device is not connected and authorised')
        serial = args.serial
    elif len(ready) == 1:
        serial = ready[0]
    else:
        parser.error('Connect and authorise one device, or specify --serial. Run adb devices to check.')
    base += ['-s', serial]
    # Reuse one uniquely named temporary XML on the reader during this run.
    # Each PC copy has its own sequential filename and remains in the backup.
    remote = '/sdcard/playbooks-capture-' + uuid.uuid4().hex + '.xml'
    status = 'interrupted'
    previous = None
    unchanged = 0
    book = None
    try:
        print('Keep the device awake and leave the Notes list open. Ctrl+C stops safely.')
        print(f'Backup folder: {folder}')
        for number in range(1, args.max_screens + 1):
            local = folder / f'notes-{number:04d}.xml'
            # Remove only our own temporary file so a failed dump cannot reuse stale XML.
            run('shell', 'rm', '-f', remote)
            run('shell', 'uiautomator', 'dump', remote)
            run('pull', remote, str(local))
            # Validate the captured UI before issuing another swipe. If the
            # user leaves the Notes list or changes books, stop the capture.
            title, listing, rows, fingerprint = parse(local)
            if book is None:
                book = title
            elif title != book:
                raise RuntimeError('Book changed; stopping before another swipe')
            # A matching signature means the last swipe caused no observable
            # movement. Require several such results to allow for e-ink lag.
            # The first capture establishes the baseline and counts as zero.
            unchanged = unchanged + 1 if fingerprint == previous else 0
            previous = fingerprint
            print(f'Screen {number}: {len(rows)} visible rows; unchanged {unchanged}/{args.stable}', flush=True)
            if unchanged >= args.stable:
                status = 'no_movement_detected'
                print('No movement detected after repeated swipes. Check the final screen is the bottom.')
                break
            if number == args.max_screens:
                status = 'screen_limit_reached'
                break
            # Calculate the gesture inside the current list, avoiding its tabs
            # and toolbar. This adapts to screen size and fixed orientation.
            x1, y1, x2, y2 = bounds(listing)
            if x2 - x1 < 80 or y2 - y1 < 100:
                raise RuntimeError('Notes list is too small for safe scrolling')
            x = (x1 + x2) // 2
            # A slow drag over 45% of the list leaves overlap and reduces fling/skipping.
            run('shell', 'input', 'swipe', str(x), str(int(y1 + (y2-y1)*0.78)), str(x), str(int(y1 + (y2-y1)*0.33)), '900')
            time.sleep(args.delay)
    except KeyboardInterrupt:
        print('\nStopped. Exporting captured screens.')
    except Exception as exc:
        status = 'error'
        print(f'Stopped: {exc}', file=sys.stderr)
    finally:
        # Always rebuild exports from everything captured so far, even after
        # Ctrl+C or an ADB error. Record why the loop ended for later review.
        (folder / 'run.json').write_text(json.dumps({'status': status, 'book': book, 'device': serial,
            'scope': 'Notes list text from current scroll position. Verify first and last entries.'}, ensure_ascii=False, indent=2), encoding='utf-8')
        export(folder)
    if status == 'error':
        sys.exit(1)


if __name__ == '__main__':
    main()
