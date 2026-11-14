"""Context packets use invented text: no device, network or personal book needed."""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

import backup_playbooks
from prepare_context import INSTRUCTIONS, prepare_context, text_blocks


class PrepareContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.epub = self.folder / 'book.epub'
        self.annotations = self.folder / 'notes.json'
        self.destination = self.folder / 'packet'
        with zipfile.ZipFile(self.epub, 'w') as archive:
            archive.writestr('mimetype', 'application/epub+zip')
            archive.writestr('META-INF/container.xml', '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/book.opf"/></rootfiles></container>')
            archive.writestr('EPUB/book.opf', '''<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Beispielbuch</dc:title><dc:creator>Testautor</dc:creator><dc:identifier id="id">urn:test:context</dc:identifier><dc:language>de</dc:language></metadata><manifest><item id="one" href="one.xhtml" media-type="application/xhtml+xml"/><item id="two" href="two.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="one"/><itemref idref="two"/></spine></package>''')
            archive.writestr('EPUB/one.xhtml', '<html><head/><body><h1>Erstes Kapitel</h1><p>Vorheriger Absatz.</p><p>Ein klarer <em>Gedanke</em> hilft weiter.</p><p>Nächster Absatz.</p><h2>Neuer Abschnitt</h2><p>Wiederholt wiederholt.</p></body></html>')
            archive.writestr('EPUB/two.xhtml', '<html><head/><body><h1>Zweites Kapitel</h1><p>Wiederholt.</p></body></html>')

    def save(self, rows):
        self.annotations.write_text(json.dumps(rows, ensure_ascii=False), encoding='utf-8')

    def prepare(self, rows, **kwargs):
        self.save(rows)
        return prepare_context(self.epub, self.annotations, self.destination, **kwargs)

    def test_context_notes_language_and_inputs_preserved(self):
        rows = [{'book': 'Beispielbuch', 'text': 'klarer Gedanke', 'chapter': 'Erstes Kapitel',
                 'location': 'auf Seite 7', 'fields': {'note': ['Meine Notiz']}}]
        self.save(rows)
        hashes = [hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.epub, self.annotations)]
        _, packet = prepare_context(self.epub, self.annotations, self.destination)
        entry = packet['entries'][0]
        self.assertEqual(packet['language'], 'de')
        self.assertEqual(entry['notes'], ['Meine Notiz'])
        self.assertEqual(entry['locations'], ['auf Seite 7'])
        self.assertEqual(entry['candidates'][0]['headings'], ['Erstes Kapitel'])
        contexts = [packet['passages'][r['id']]['text'] for r in entry['candidates'][0]['paragraphs']]
        self.assertEqual(contexts, ['Vorheriger Absatz.', 'Ein klarer Gedanke hilft weiter.', 'Nächster Absatz.'])
        self.assertEqual(packet['source_rows'], rows)
        self.assertEqual(hashes, [hashlib.sha256(p.read_bytes()).hexdigest() for p in (self.epub, self.annotations)])
        self.assertEqual(list(self.destination.glob('*.epub')), [])
        markdown = (self.destination / 'context.md').read_text(encoding='utf-8')
        self.assertIn('Meine Notiz', markdown)
        self.assertIn(entry['id'], markdown)

    def test_repeats_group_but_different_comments_survive_and_ids_are_stable(self):
        rows = [{'text': 'klarer Gedanke', 'location': 'Seite 7'},
                {'text': 'klarer Gedanke', 'chapter': 'Erstes Kapitel'},
                {'text': 'klarer Gedanke', 'fields': {'note': ['Andere Sicht']}}]
        _, packet = self.prepare(rows)
        self.assertEqual(len(packet['entries']), 2)
        self.assertEqual(packet['entries'][0]['source_rows'], [1, 2])
        self.assertEqual(packet['counts']['grouped_repetitions'], 1)
        self.save(list(reversed(rows)))
        _, reordered = prepare_context(self.epub, self.annotations, self.folder / 'reordered')
        self.assertEqual({e['id'] for e in packet['entries']}, {e['id'] for e in reordered['entries']})

    def test_partial_word_selection_uses_full_context_without_rewriting_selection(self):
        _, packet = self.prepare([{'text': 'klarer Ged', 'fields': {'note': '？'}}], neighbors=0)
        entry = packet['entries'][0]
        self.assertEqual(entry['selections'], ['klarer Ged'])
        self.assertEqual(entry['notes'], ['？'])
        self.assertEqual(len(packet['passages']), 1)
        self.assertIn('Ein klarer Gedanke hilft weiter.', next(iter(packet['passages'].values()))['text'])
        self.assertNotIn('question mark', INSTRUCTIONS.lower())
        self.assertNotIn('Fragezeichen', INSTRUCTIONS)

    def test_uncertain_missing_and_note_only_entries_are_explicit(self):
        _, packet = self.prepare([{'text': 'Wiederholt'}, {'text': 'Unbekannte Stelle'},
                                  {'text': '', 'fields': {'note': ['Nicht verlieren']}}, {'text': ''}])
        self.assertEqual([e['status'] for e in packet['entries']], ['ambiguous', 'not_found', 'note_only', 'empty'])
        self.assertEqual(len(packet['entries'][0]['candidates']), 2)
        self.assertEqual(packet['entries'][1]['candidates'], [])
        markdown = (self.destination / 'context.md').read_text(encoding='utf-8')
        self.assertIn('AMBIGUOUS CANDIDATE', markdown)
        self.assertIn('Nicht verlieren', markdown)
        self.assertEqual(len(packet['source_rows']), 4)

    def test_nested_blocks_do_not_duplicate_or_drop_outer_text(self):
        body = ET.fromstring('<body>Intro<ul><li>Start<p>Inner <em>text</em>.</p>Ende</li></ul>Schluss</body>')
        blocks, _ = text_blocks(body)
        self.assertEqual(''.join(b['text'] for b in blocks), ''.join(body.itertext()))
        self.assertEqual([b['text'] for b in blocks], ['Intro', 'Start', 'Inner text.', 'Ende', 'Schluss'])

    def test_split_parts_carry_all_references_and_cover_every_entry_once(self):
        _, packet = self.prepare([{'text': 'klarer Gedanke'}, {'text': 'Vorheriger Absatz'},
                                  {'text': 'Nächster Absatz'}, {'text': 'Wiederholt'}], part_chars=3500)
        self.assertGreater(len(packet['parts']), 1)
        entry_ids = []
        for part in packet['parts']:
            markdown = (self.destination / part['file']).read_text(encoding='utf-8')
            entry_ids.extend(re.findall(r'^### (H-[a-f0-9]+)$', markdown, re.M))
            refs = set(re.findall(r'P-[a-f0-9]+', markdown))
            definitions = set(re.findall(r'^### (P-[a-f0-9]+)$', markdown, re.M))
            self.assertEqual(refs, definitions)
            self.assertEqual(part['characters'], len(markdown))
            if part['exceeds_target']:
                self.assertEqual(len(re.findall(r'^### H-', markdown, re.M)), 1)
        self.assertCountEqual(entry_ids, [e['id'] for e in packet['entries']])

    def test_refuses_existing_output_or_malformed_notes(self):
        self.destination.mkdir()
        sentinel = self.destination / 'keep.txt'
        sentinel.write_text('unchanged', encoding='utf-8')
        self.save([{'text': 'klarer'}])
        with self.assertRaises(ValueError):
            prepare_context(self.epub, self.annotations, self.destination)
        self.assertEqual(sentinel.read_text(encoding='utf-8'), 'unchanged')
        self.save([{'text': '', 'fields': {'note': 42}}])
        with self.assertRaisesRegex(ValueError, 'fields.note'):
            prepare_context(self.epub, self.annotations, self.folder / 'invalid')
        self.assertFalse((self.folder / 'invalid').exists())

    def test_cli_preparation_never_discovers_or_connects_to_adb(self):
        self.save([{'text': 'klarer Gedanke'}])
        args = ['backup_playbooks.py', '--prepare-only', '--epub', str(self.epub),
                '--annotations', str(self.annotations), '--context-output', str(self.destination)]
        with patch.object(sys, 'argv', args), patch.object(backup_playbooks.shutil, 'which', side_effect=AssertionError('ADB discovery forbidden')), \
                patch.object(subprocess, 'run', side_effect=AssertionError('External processes forbidden')):
            backup_playbooks.main()
        self.assertTrue((self.destination / 'context.md').exists())

    def test_context_flags_require_prepare_mode_and_reject_epub_output(self):
        for flags in (['--context-neighbors', '0'], ['--prepare-only', '--epub-output', 'output.epub']):
            with self.subTest(flags=flags), patch.object(sys, 'argv', ['backup_playbooks.py'] + flags):
                with self.assertRaises(SystemExit) as error:
                    backup_playbooks.main()
                self.assertEqual(error.exception.code, 2)


if __name__ == '__main__':
    unittest.main()
