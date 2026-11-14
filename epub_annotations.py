"""Embed extracted Play Books highlights in a NEW EPUB, without editing its text.

Implements the 15 October 2026 EPUB Annotations 1.0 Working Draft:
https://www.w3.org/TR/2026/WD-epub-anno-10-20261015/
Also embeds Calibre-native highlights in META-INF/calibre_bookmarks.txt.

No ADB or third-party Python packages are needed. This module is called by
backup_playbooks.py when --epub and --annotations are supplied together.
"""
from datetime import datetime, timezone
import hashlib
import base64
import json
from pathlib import Path
import posixpath
from urllib.parse import quote, unquote, urlsplit
import uuid
import xml.etree.ElementTree as ET
import zipfile

OPF = '{http://www.idpf.org/2007/opf}'
DC = '{http://purl.org/dc/elements/1.1/}'
CONTAINER = '{urn:oasis:names:tc:opendocument:xmlns:container}'
ANNOTATIONS_PATH = 'META-INF/annotations.json'
ARCHIVE_PATH = 'META-INF/playbooks-source.json'
CALIBRE_PATH = 'META-INF/calibre_bookmarks.txt'
CALIBRE_MAGIC = b'encoding=json+base64:\n'


def utf16_length(text):
    """Browser/Calibre offsets count UTF-16 units: an emoji occupies two."""
    return len(text.encode('utf-16-le')) // 2


def calibre_text_runs(body):
    """Map body textContent offsets to Calibre's document-local CFI positions.

    Calibre uses /2 for the HTML element and /4 for its body. Descendant element
    steps are twice their one-based ELEMENT sibling number. Its CFI encoder
    groups a parent's direct text nodes into one virtual /1 text stream, skipping
    intervening elements' text. Thus a tail after <em> belongs to the surrounding
    paragraph's stream, not to the <em> element. Keep that stream's UTF-16 offset
    separate from the whole body's Python character offset used for matching.

    References: calibre src/pyj/read_book/cfi.pyj (encode, adjust_node_for_text_offset,
    node_for_text_offset) and src/pyj/read_book/annotations.pyj (add_highlight).
    No nodes or IDs are inserted into the original XHTML.
    """
    runs = []
    position = 0

    def add(text, parent_path, direct_offset, text_index):
        nonlocal position
        if text:
            runs.append({'start': position, 'end': position + len(text),
                         'path': parent_path + '/1', 'direct_offset': direct_offset, 'text': text,
                         'node_path': parent_path + '/' + str(text_index * 2 + 1)})
            position += len(text)

    def walk(element, path):
        add(element.text, path, 0, 0)
        direct_offset = utf16_length(element.text or '')
        text_index = int(bool(element.text))
        for number, child in enumerate(element, 1):
            walk(child, path + '/' + str(number * 2))
            add(child.tail, path, direct_offset, text_index)
            direct_offset += utf16_length(child.tail or '')
            text_index += int(bool(child.tail))

    walk(body, '/2/4')
    return runs


def calibre_cfi(document, position, end=False):
    """Encode one endpoint; an exclusive end may sit just after its text node."""
    for run in document['calibre_runs']:
        contains = run['start'] < position <= run['end'] if end else run['start'] <= position < run['end']
        if contains:
            # A grouped offset exactly at the end of an earlier sibling text
            # node resolves there, BEFORE any intervening inline element. For
            # a start at the beginning of a later tail, use Calibre's explicit
            # odd text-node step instead, so that preceding <em>/<span> text
            # does not accidentally become part of the highlight.
            if not end and position == run['start'] and run['direct_offset']:
                return run['node_path'] + ':0'
            offset = run['direct_offset'] + utf16_length(run['text'][:position - run['start']])
            return run['path'] + ':' + str(offset)
    raise ValueError(f'Cannot create Calibre CFI for {document["path"]} at {position}')


def read_calibre_annotations(archive):
    """Retain existing Calibre notes/bookmarks when adding our highlights.

    Refuse legacy or unrecognised data rather than replacing it silently.
    The modern on-disk format is a magic header followed by base64-encoded JSON.
    """
    if CALIBRE_PATH not in archive.namelist():
        return []
    raw = archive.read(CALIBRE_PATH)
    if not raw.startswith(CALIBRE_MAGIC):
        raise ValueError('EPUB contains legacy/unrecognised Calibre bookmarks; use a clean original')
    try:
        data = json.loads(base64.b64decode(b''.join(raw[len(CALIBRE_MAGIC):].split()), validate=True))
    except (ValueError, UnicodeError) as exc:
        raise ValueError('Existing Calibre annotation data is invalid') from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise ValueError('Existing Calibre annotation data must be an array of objects')
    return data


def serialize_calibre(annotations):
    """Match Calibre's save_annots_to_epub format, including 80-byte line wraps."""
    encoded = base64.b64encode(json.dumps(annotations, ensure_ascii=False).encode('utf-8'))
    return CALIBRE_MAGIC + b'\n'.join(encoded[i:i + 80] for i in range(0, len(encoded), 80))


def normalize(text):
    """Return searchable text AND a map back to original character positions.

    Android and EPUB may disagree about line wrapping, nonbreaking spaces and
    discretionary (soft) hyphens. Ignore these differences while searching,
    but keep an original offset for every resulting character. Selectors must
    use offsets in the ORIGINAL DOM textContent, not in this search string.
    Punctuation, letter case and actual spelling are deliberately preserved.
    """
    characters, positions = [], []
    for position, character in enumerate(text):
        if character in '\u00ad\u200b\ufeff':
            continue
        if character.isspace():
            if characters and characters[-1] != ' ':
                characters.append(' ')
                positions.append(position)
        else:
            characters.append(character)
            positions.append(position)
    if characters and characters[-1] == ' ':
        characters.pop()
        positions.pop()
    return ''.join(characters), positions


def local_name(element):
    return element.tag.rsplit('}', 1)[-1]


def resource_path(base, href):
    """Resolve an EPUB-relative URL without extracting ZIP contents to disk."""
    url = urlsplit(href)
    if url.scheme or url.netloc or url.path.startswith('/'):
        raise ValueError(f'Expected a local EPUB resource: {href}')
    path = posixpath.normpath(posixpath.join(base, unquote(url.path)))
    if path == '..' or path.startswith('../'):
        raise ValueError(f'Resource escapes the EPUB container: {href}')
    return path


def read_book(archive):
    """Read package metadata and text in spine order; leave all bytes unchanged.

    ElementTree itertext() concatenates descendant text and tails, matching
    body.textContent for XML/XHTML. It adds no artificial paragraph spaces.
    Only spine XHTML documents are eligible annotation targets.
    """
    container = ET.fromstring(archive.read('META-INF/container.xml'))
    rootfile = container.find(f'{CONTAINER}rootfiles/{CONTAINER}rootfile')
    if rootfile is None:
        raise ValueError('EPUB has no package document')
    package_path = resource_path('', rootfile.attrib['full-path'])
    package = ET.fromstring(archive.read(package_path))
    manifest = package.find(OPF + 'manifest')
    spine = package.find(OPF + 'spine')
    metadata = package.find(OPF + 'metadata')
    if manifest is None or spine is None or metadata is None:
        raise ValueError('EPUB package is missing metadata, manifest or spine')
    items = {item.get('id'): item for item in manifest}
    documents = []
    for spine_index, itemref in enumerate(spine):
        item = items.get(itemref.get('idref'))
        if item is None:
            raise ValueError('Spine references a missing manifest item')
        if item.get('media-type') != 'application/xhtml+xml':
            continue
        path = resource_path(posixpath.dirname(package_path), item.attrib['href'])
        root = ET.fromstring(archive.read(path))
        body = next((e for e in root.iter() if local_name(e) == 'body'), None)
        if body is None:
            continue
        original = ''.join(body.itertext())
        searchable, offsets = normalize(original)
        headings = [normalize(''.join(e.itertext()))[0] for e in body.iter()
                    if local_name(e) in ('h1', 'h2', 'h3', 'h4', 'h5', 'h6')]
        documents.append({'path': path, 'text': original, 'search': searchable,
                          'offsets': offsets, 'headings': headings,
                          'spine_index': spine_index, 'calibre_runs': calibre_text_runs(body)})
    about = {}
    for name in ('title', 'publisher', 'creator', 'date'):
        values = [''.join(e.itertext()) for e in metadata.findall(DC + name)]
        if values:
            about['dc:' + name] = values if name == 'creator' else values[0]
    # Publication identity is used only to generate stable annotation UUIDs.
    identifier = package.get('unique-identifier')
    identity = next((''.join(e.itertext()) for e in metadata.findall(DC + 'identifier')
                     if e.get('id') == identifier), json.dumps(about, sort_keys=True))
    if not documents:
        raise ValueError('No XHTML spine documents found')
    return about, identity, documents


def locate(text, chapter, documents):
    """Find all exact normalized occurrences, then use chapter only to narrow.

    Never use Android's page number as an EPUB offset: reflowed pages are not
    stable locations. Never fuzzy-match or silently choose the first of several
    candidates. Uncertain matches remain in the report and source archive.
    """
    needle = normalize(text)[0]
    if not needle:
        return []
    matches = []
    for document in documents:
        start = 0
        while True:
            index = document['search'].find(needle, start)
            if index < 0:
                break
            offsets = document['offsets']
            matches.append({'path': document['path'], 'start': offsets[index],
                            'end': offsets[index + len(needle) - 1] + 1})
            start = index + 1
    if len(matches) > 1 and chapter:
        heading = normalize(chapter)[0]
        paths = {d['path'] for d in documents if heading in d['headings']}
        narrowed = [match for match in matches if match['path'] in paths]
        if narrowed:
            matches = narrowed
    return matches


def load_rows(path):
    """Read the extractor's JSON array. Invalid merges fail with a useful error."""
    try:
        rows = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except json.JSONDecodeError as exc:
        raise ValueError(f'Invalid annotation JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}') from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError('Annotation JSON must be an array of extractor row objects')
    for index, row in enumerate(rows, 1):
        for field in ('text', 'chapter', 'book', 'location'):
            if field in row and not isinstance(row[field], str):
                raise ValueError(f'Row {index}: {field} must be a string')
        if not isinstance(row.get('fields', {}), dict):
            raise ValueError(f'Row {index}: fields must be an object')
    return rows


def embed_annotations(epub, annotations, destination=None):
    """Create a separate EPUB and matching report; refuse ALL overwrites.

    Highlights are ranges, not visible modifications to chapter HTML. Actual
    comments come from fields.note; the selected passage is not mislabelled as
    a comment. The source JSON is also archived so unresolved/empty rows survive.
    """
    epub, annotations = Path(epub).resolve(), Path(annotations).resolve()
    destination = Path(destination).resolve() if destination else epub.with_name(epub.stem + '-annotated.epub')
    report_path = destination.with_suffix('.annotations-report.json')
    if destination.suffix.lower() != '.epub':
        raise ValueError('Output filename must end in .epub')
    if destination in (epub, annotations) or destination.exists() or report_path.exists():
        raise ValueError('Output EPUB/report already exists or equals an input; choose a new --epub-output')
    rows = load_rows(annotations)
    books = {normalize(row.get('book', ''))[0] for row in rows if row.get('book')}
    if len(books) > 1:
        raise ValueError('JSON contains multiple books; provide annotations for one EPUB only')
    generated = datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00', 'Z')
    source_digest = hashlib.sha256(epub.read_bytes()).hexdigest()
    with zipfile.ZipFile(epub) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise ValueError('EPUB contains duplicate ZIP entries')
        if ANNOTATIONS_PATH in archive.namelist() or ARCHIVE_PATH in archive.namelist():
            raise ValueError('EPUB already contains annotations/source data; use the original book')
        if archive.read('mimetype') != b'application/epub+zip':
            raise ValueError('Invalid EPUB mimetype')
        about, identity, documents = read_book(archive)
        calibre_items = read_calibre_annotations(archive)
        calibre_existing_count = len(calibre_items)
        document_by_path = {document['path']: document for document in documents}
        if books and normalize(about.get('dc:title', ''))[0] not in books:
            raise ValueError('Book title in JSON does not match EPUB metadata')
        items, results, seen = [], [], {}
        for number, row in enumerate(rows, 1):
            selected = row.get('text', '')
            candidates = locate(selected, row.get('chapter', ''), documents)
            result = {'row': number, 'chapter': row.get('chapter', ''),
                      'location': row.get('location', ''), 'text': selected}
            if not normalize(selected)[0]:
                result['status'] = 'empty_text'
            elif not candidates:
                result['status'] = 'not_found'
            elif len(candidates) > 1:
                # Include nearby text so a human can distinguish repeated words
                # without having to inspect raw XHTML and count characters.
                for candidate in candidates:
                    document = next(d for d in documents if d['path'] == candidate['path'])
                    candidate['context'] = document['text'][max(0, candidate['start'] - 100):candidate['end'] + 100]
                result.update(status='ambiguous', candidates=candidates)
            else:
                match = candidates[0]
                note = row.get('fields', {}).get('note', [])
                if isinstance(note, list) and all(isinstance(value, str) for value in note):
                    comment = '\n'.join(note)
                elif isinstance(note, str):
                    comment = note
                else:
                    raise ValueError(f'Row {number}: fields.note must be a string or string array')
                # The same row may appear in multiple captures. Group only if
                # the actual text range AND comment are identical.
                key = json.dumps([identity, match, comment], sort_keys=True, ensure_ascii=False)
                annotation_id = str(uuid.uuid5(uuid.NAMESPACE_URL, key))
                if key in seen:
                    result.update(status='duplicate', annotation=seen[key])
                else:
                    target = {'source': quote(match['path'], safe='/'), 'selector': [
                        {'type': 'TextPositionSelector', 'start': match['start'], 'end': match['end']}]}
                    annotation = {'id': 'urn:uuid:' + annotation_id, 'type': 'Annotation',
                                  'created': generated, 'motivation': 'highlighting', 'target': target}
                    if comment:
                        annotation['body'] = {'type': 'TextualBody', 'format': 'text/plain', 'value': comment}
                    # A parallel Calibre entry points to exactly the same range.
                    # Use the book's actual text (including its whitespace) as
                    # highlighted_text, and only put the user's comment in notes.
                    document = document_by_path[match['path']]
                    calibre_annotation = {
                        'type': 'highlight', 'uuid': annotation_id, 'timestamp': generated,
                        'highlighted_text': document['text'][match['start']:match['end']],
                        'start_cfi': calibre_cfi(document, match['start']),
                        'end_cfi': calibre_cfi(document, match['end'], end=True),
                        'spine_name': document['path'], 'spine_index': document['spine_index'],
                        'style': {'type': 'builtin', 'kind': 'color', 'which': 'yellow'},
                    }
                    if comment:
                        calibre_annotation['notes'] = comment
                    if row.get('chapter'):
                        calibre_annotation['toc_family_titles'] = [row['chapter']]
                    # UUIDs are stable across runs. Keep pre-existing bookmarks
                    # and unrelated highlights, replacing only this exact UUID.
                    calibre_items = [a for a in calibre_items if a.get('uuid') != annotation_id]
                    calibre_items.append(calibre_annotation)
                    items.append(annotation)
                    seen[key] = annotation['id']
                    result.update(status='embedded', annotation=annotation['id'])
                result['match'] = match
            results.append(result)
        annotation_set = {'@context': 'https://www.w3.org/ns/epub-anno.jsonld',
                          'id': 'urn:uuid:' + str(uuid.uuid4()), 'type': 'AnnotationSet',
                          'generated': generated, 'about': about, 'items': items}
        summary = {status: sum(r['status'] == status for r in results)
                   for status in ('embedded', 'duplicate', 'empty_text', 'not_found', 'ambiguous')}
        report = {'specification': 'https://www.w3.org/TR/2026/WD-epub-anno-10-20261015/',
                  'source_epub_sha256': source_digest, 'summary': summary,
                  'calibre': {'embedded_highlights': len(items), 'existing_entries': calibre_existing_count,
                              'path': CALIBRE_PATH},
                  'created_timestamp': 'Conversion time; original annotation dates are absent from extractor JSON.',
                  'notes': 'Only unique exact matches after whitespace/invisible-character normalization are anchored. '
                           'Unresolved rows remain in META-INF/playbooks-source.json. Original colors are unavailable; '
                           'readers use their default highlight style. Reader support for this draft varies.',
                  'rows': results}
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Reserve both filenames with exclusive creation. Existing files,
        # including the original book, can never be truncated by this code.
        with destination.open('xb') as output:
            try:
                with report_path.open('x', encoding='utf-8') as report_file:
                    with zipfile.ZipFile(output, 'w') as target_zip:
                        target_zip.comment = archive.comment
                        # EPUB requires mimetype first and stored uncompressed.
                        mime_info = archive.getinfo('mimetype')
                        target_zip.writestr(mime_info, archive.read('mimetype'), compress_type=zipfile.ZIP_STORED)
                        for info in archive.infolist():
                            if info.filename not in ('mimetype', CALIBRE_PATH):
                                target_zip.writestr(info, archive.read(info.filename))
                        for path, data in ((ANNOTATIONS_PATH, annotation_set), (ARCHIVE_PATH, rows)):
                            target_zip.writestr(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'),
                                               compress_type=zipfile.ZIP_DEFLATED)
                        target_zip.writestr(CALIBRE_PATH, serialize_calibre(calibre_items),
                                           compress_type=zipfile.ZIP_DEFLATED)
                    json.dump(report, report_file, ensure_ascii=False, indent=2)
            except Exception:
                # A partially written new EPUB is not a valid deliverable.
                # Only remove the file this invocation just created.
                output.close()
                destination.unlink()
                raise
    print(f'Annotated EPUB: {destination}')
    print(f'Matching report: {report_path}')
    print(' | '.join(f'{name}: {count}' for name, count in summary.items()))
    return destination, report
