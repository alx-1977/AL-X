"""Direct coverage of the bounded reference-facts provider (D-061)."""
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.providers.reference_file import ReferenceError, read_reference


class ReferenceFileTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.home = Path(self.directory.name).resolve()
        home = patch('alx.providers.reference_file.Path.home', return_value=self.home)
        home.start()
        self.addCleanup(home.stop)

    def read(self, path, **arguments):
        return read_reference({'path': str(path), **arguments})

    def test_credentials_are_refused_before_reading(self):
        for name in ('.ssh/id_rsa', '.env', '.env.local', 'tokens.json',
                     'Library/Keychains/login.keychain-db'):
            with self.subTest(name=name), self.assertRaisesRegex(ReferenceError, 'path_denied'):
                self.read(self.home / name)
        secret = self.home / '.env'
        secret.write_text('private')
        alias = self.home / 'reference.txt'
        alias.symlink_to(secret)
        with self.assertRaisesRegex(ReferenceError, 'path_denied'):
            self.read(alias)

    def test_icloud_placeholder_and_text_folder_facts(self):
        cloud = self.home / 'Library/Mobile Documents/com~apple~CloudDocs'
        cloud.mkdir(parents=True)
        placeholder = cloud / '.board.PcbDoc.icloud'
        placeholder.touch()
        for path in (placeholder, cloud / 'board.PcbDoc'):
            self.assertEqual(self.read(path)['kind'], 'not_downloaded')
        text = cloud / 'notes.txt'
        text.write_text('coverlay 0.05 mm')
        self.assertEqual(self.read(text)['text'], 'coverlay 0.05 mm')
        facts = self.read(cloud)
        self.assertEqual(facts['kind'], 'folder')
        self.assertIn({'name': 'notes.txt', 'kind': 'file'}, facts['entries'])

    def test_image_and_zip_entry_facts(self):
        png = b'\x89PNG\r\n\x1a\n' + b'\0' * 8 + struct.pack('>II', 320, 200)
        image = self.home / 'board.png'
        image.write_bytes(png)
        expected = dict(kind='image', media_type='image/png', width=320, height=200)
        self.assertEqual(self.read(image), expected)
        archive = self.home / 'references.zip'
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('board.png', png)
            bundle.writestr('top.gbr', 'X1Y2D01*')
        self.assertEqual(self.read(archive)['entries'], [
            {'name': 'board.png', 'bytes': len(png)}, {'name': 'top.gbr', 'bytes': 8}])
        self.assertEqual(self.read(archive, entry='board.png'), dict(expected, entry='board.png'))
        self.assertEqual(self.read(archive, entry='top.gbr')['text'], 'X1Y2D01*')

    def test_pdf_returns_per_page_text(self):
        from pypdf import PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
        writer = PdfWriter()
        page = writer.add_blank_page(100, 100)
        font = DictionaryObject({NameObject('/Type'): NameObject('/Font'),
            NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
        page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'):
            DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b'BT /F1 12 Tf (Coverlay) Tj ET')
        page[NameObject('/Contents')] = writer._add_object(stream)
        pdf = self.home / 'report.pdf'
        with pdf.open('wb') as output:
            writer.write(output)
        facts = self.read(pdf)
        self.assertEqual(facts['kind'], 'pdf')
        self.assertEqual(facts['page_count'], 1)
        self.assertEqual(facts['pages'][0]['page'], 1)
        self.assertIn('Coverlay', facts['pages'][0]['text'])
        self.assertFalse(facts['truncated'])
