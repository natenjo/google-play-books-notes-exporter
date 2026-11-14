"""Small invented books test anchoring and file preservation without private data."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
import xml.etree.ElementTree as ET

from epub_annotations import (CALIBRE_PATH, calibre_cfi, calibre_text_runs,
                              embed_annotations, normalize, read_book,
                              read_calibre_annotations, serialize_calibre)


class EpubAnnotationsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.book = self.folder / 'original.epub'
        self.rows = self.folder / 'rows.json'
        self.output = self.folder / 'annotated.epub'
        # Inline markup and soft hyphens exercise offsets across DOM nodes.
        with zipfile.ZipFile(self.book, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
            archive.writestr('META-INF/container.xml', '''<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/book.opf"/></rootfiles></container>''')
            archive.writestr('EPUB/book.opf', '''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Test Book</dc:title><dc:identifier id="bookid">urn:test:book</dc:identifier></metadata><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>''')
            archive.writestr('EPUB/one.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body>\n<h1>First</h1><p>😀 The quick <em>brown</em>\n fox and soft\u00adhyphen.</p><p>Repeat Repeat</p></body></html>')
            archive.writestr('EPUB/two.xhtml', '<html xmlns="http://www.w3.org/1999/xhtml"><body><h1>Second</h1><p>Repeat</p></body></html>')

    def write_rows(self, rows):
        self.rows.write_text(json.dumps(rows), encoding='utf-8')

    def convert(self, rows):
        self.write_rows(rows)
        return embed_annotations(self.book, self.rows, self.output)

    def test_offsets_comments_duplicates_and_original_bytes(self):
        before = hashlib.sha256(self.book.read_bytes()).hexdigest()
        row = {'book': 'Test Book', 'chapter': 'First', 'text': 'quick brown fox and softhyphen',
               'fields': {'note': ['My actual comment']}}
        _, report = self.convert([row, row])
        self.assertEqual(report['summary']['embedded'], 1)
        self.assertEqual(report['summary']['duplicate'], 1)
        self.assertEqual(hashlib.sha256(self.book.read_bytes()).hexdigest(), before)
        with zipfile.ZipFile(self.book) as source, zipfile.ZipFile(self.output) as output:
            self.assertIsNone(output.testzip())
            self.assertEqual(output.infolist()[0].filename, 'mimetype')
            self.assertEqual(output.infolist()[0].compress_type, zipfile.ZIP_STORED)
            for name in source.namelist():
                self.assertEqual(source.read(name), output.read(name))
            annotation_set = json.loads(output.read('META-INF/annotations.json'))
            self.assertEqual(annotation_set['type'], 'AnnotationSet')
            item = annotation_set['items'][0]
            self.assertEqual(item['body']['value'], 'My actual comment')
            self.assertTrue(item['created'].endswith('Z'))
            _, _, documents = read_book(source)
            selector = item['target']['selector'][0]
            actual = documents[0]['text'][selector['start']:selector['end']]
            self.assertEqual(normalize(actual)[0], row['text'])
            self.assertEqual(json.loads(output.read('META-INF/playbooks-source.json')), [row, row])
            calibre = read_calibre_annotations(output)
            self.assertEqual(len(calibre), 1)
            self.assertEqual(calibre[0]['notes'], 'My actual comment')
            self.assertEqual(calibre[0]['uuid'], item['id'].removeprefix('urn:uuid:'))
            self.assertEqual(calibre[0]['spine_name'], 'EPUB/one.xhtml')
            self.assertEqual(calibre[0]['spine_index'], 0)
            self.assertEqual(calibre[0]['start_cfi'], '/2/4/4/1:7')
            self.assertEqual(normalize(calibre[0]['highlighted_text'])[0], row['text'])

    def test_uncertain_and_empty_rows_are_preserved_without_false_highlights(self):
        rows = [{'text': 'Repeat'}, {'text': 'not in this book'}, {'text': ''}]
        _, report = self.convert(rows)
        self.assertEqual(report['summary']['ambiguous'], 1)
        self.assertEqual(report['summary']['not_found'], 1)
        self.assertEqual(report['summary']['empty_text'], 1)
        self.assertEqual(len(report['rows'][0]['candidates']), 3)
        self.assertIn('context', report['rows'][0]['candidates'][0])
        with zipfile.ZipFile(self.output) as archive:
            self.assertEqual(json.loads(archive.read('META-INF/annotations.json'))['items'], [])
            self.assertEqual(json.loads(archive.read('META-INF/playbooks-source.json')), rows)

    def test_chapter_disambiguates_without_inventing_a_comment(self):
        self.convert([{'text': 'Repeat', 'chapter': 'Second'}])
        with zipfile.ZipFile(self.output) as archive:
            item = json.loads(archive.read('META-INF/annotations.json'))['items'][0]
            self.assertEqual(item['target']['source'], 'EPUB/two.xhtml')
            self.assertNotIn('body', item)

    def test_refuses_original_path_and_existing_output(self):
        self.write_rows([{'text': 'quick'}])
        original = self.book.read_bytes()
        with self.assertRaises(ValueError):
            embed_annotations(self.book, self.rows, self.book)
        self.output.write_bytes(b'keep me')
        with self.assertRaises(ValueError):
            embed_annotations(self.book, self.rows, self.output)
        self.assertEqual(self.output.read_bytes(), b'keep me')
        self.assertEqual(self.book.read_bytes(), original)

    def test_invalid_json_fails_before_writing_epub(self):
        self.rows.write_text('[{} {}]', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'line 1'):
            embed_annotations(self.book, self.rows, self.output)
        self.assertFalse(self.output.exists())

    def test_rejects_multiple_books(self):
        with self.assertRaisesRegex(ValueError, 'multiple books'):
            self.convert([{'book': 'A'}, {'book': 'B'}])

    def test_stable_ids_across_conversions(self):
        row = {'text': 'quick brown fox'}
        self.convert([row])
        second = self.folder / 'second.epub'
        embed_annotations(self.book, self.rows, second)
        with zipfile.ZipFile(self.output) as a, zipfile.ZipFile(second) as b:
            first_id = json.loads(a.read('META-INF/annotations.json'))['items'][0]['id']
            second_id = json.loads(b.read('META-INF/annotations.json'))['items'][0]['id']
            self.assertEqual(first_id, second_id)

    def test_cfis_handle_utf16_and_inline_text_boundaries(self):
        body = ET.fromstring('<body><p>A😀<em>B</em>C<span>.</span>Tail</p></body>')
        document = {'path': 'test.xhtml', 'calibre_runs': calibre_text_runs(body)}
        # Python positions count the emoji once; browser CFI offsets count it twice.
        self.assertEqual(calibre_cfi(document, 1), '/2/4/2/1:1')
        self.assertEqual(calibre_cfi(document, 2, end=True), '/2/4/2/1:3')
        # Start after inline markup must exclude B and the preceding period.
        self.assertEqual(calibre_cfi(document, 3), '/2/4/2/3:0')
        self.assertEqual(calibre_cfi(document, 5), '/2/4/2/5:0')
        self.assertEqual(calibre_cfi(document, 9, end=True), '/2/4/2/1:8')
        # A point inside the emphasis element uses its own element path.
        self.assertEqual(calibre_cfi(document, 2), '/2/4/2/2/1:0')

    def test_existing_calibre_bookmarks_and_notes_survive(self):
        existing = [{'type': 'bookmark', 'title': 'Keep me', 'pos': 'epubcfi(/2/2/4)',
                     'pos_type': 'epubcfi', 'timestamp': '2020-01-01T00:00:00Z'}]
        with zipfile.ZipFile(self.book, 'a') as archive:
            archive.writestr(CALIBRE_PATH, serialize_calibre(existing))
        self.convert([{'text': 'quick'}])
        with zipfile.ZipFile(self.book) as original, zipfile.ZipFile(self.output) as output:
            self.assertEqual(read_calibre_annotations(original), existing)
            annotations = read_calibre_annotations(output)
            self.assertEqual(annotations[0], existing[0])
            self.assertEqual(len(annotations), 2)
            self.assertEqual(output.namelist().count(CALIBRE_PATH), 1)

    def test_legacy_calibre_data_is_not_silently_replaced(self):
        with zipfile.ZipFile(self.book, 'a') as archive:
            archive.writestr(CALIBRE_PATH, b'legacy bookmark data')
        with self.assertRaisesRegex(ValueError, 'legacy'):
            self.convert([{'text': 'quick'}])
        self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
