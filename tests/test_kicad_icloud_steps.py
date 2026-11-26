"""iCloud conflict copies stay out of the step check, and new work copies out of iCloud (26 Nov)."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from alx.config.settings import KiCadSettings
from alx.providers.kicad import KiCadProvider
from alx.providers.kicad_steps import fingerprints, original_of, step_status
from alx.specialists.board_agent import _read_key

TOP = '(kicad_sch (version 20250114) (sheet (property "Sheetfile" "Charger.kicad_sch")))\n'


class StepCheckIgnoresICloudCopies(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.project = Path(self.folder.name)
        (self.project / 'top.kicad_sch').write_text(TOP)
        (self.project / 'Charger.kicad_sch').write_text('(kicad_sch (version 20250114))\n')

    def tearDown(self):
        self.folder.cleanup()

    def test_conflict_copies_leave_the_schematic_unchanged(self):
        before = fingerprints(self.project, 'board.kicad_pcb')['schematic']
        (self.project / 'Charger 2.kicad_sch').write_text('(kicad_sch (version 20250114))\n')
        (self.project / 'top 3.kicad_sch').write_text(TOP)
        self.assertEqual(fingerprints(self.project, 'board.kicad_pcb')['schematic'], before)

    def test_a_sheet_the_hierarchy_names_still_counts(self):
        before = fingerprints(self.project, 'board.kicad_pcb')['schematic']
        (self.project / 'top.kicad_sch').write_text(TOP.replace('Charger.kicad_sch', 'Charger 2.kicad_sch'))
        (self.project / 'Charger 2.kicad_sch').write_text('(kicad_sch (version 20250114))\n')
        after = fingerprints(self.project, 'board.kicad_pcb')['schematic']
        self.assertNotEqual(after, before)
        (self.project / 'Charger 2.kicad_sch').write_text('(kicad_sch (version 20250114) (junction))\n')
        self.assertNotEqual(fingerprints(self.project, 'board.kicad_pcb')['schematic'], after)

    def test_a_real_schematic_change_still_counts(self):
        before = fingerprints(self.project, 'board.kicad_pcb')['schematic']
        (self.project / 'Charger.kicad_sch').write_text('(kicad_sch (version 20250114) (junction))\n')
        self.assertNotEqual(fingerprints(self.project, 'board.kicad_pcb')['schematic'], before)


    def test_approvals_recorded_with_the_copies_still_hold(self):
        """Approvals given before 26 Nov hold a schematic digest that includes iCloud's copies."""
        (self.project / 'Charger 2.kicad_sch').write_text('(kicad_sch (version 20250114))\n')
        then = fingerprints(self.project, 'board.kicad_pcb')
        original = dict(original_of(then), schematic='0' * 64, placement='1' * 64)
        approvals = {'schematic': {'schematic': then['schematic_with_copies']},
                     'placement': {'schematic': then['schematic_with_copies'], 'placement': then['placement']}}
        self.assertEqual(step_status(original, approvals, then),
                         {'schematic': 'approved', 'placement': 'approved', 'routing': 'unchanged'})
        (self.project / 'Charger.kicad_sch').write_text('(kicad_sch (version 20250114) (junction))\n')
        changed = step_status(original, approvals, fingerprints(self.project, 'board.kicad_pcb'))
        self.assertEqual(changed['schematic'], 'needs_approval')

    def test_a_sheet_named_only_inside_a_longer_name_is_still_a_copy(self):
        before = fingerprints(self.project, 'board.kicad_pcb')['schematic']
        (self.project / 'top.kicad_sch').write_text(TOP.replace(
            '(sheet', '(sheet (property "Sheetfile" "MyCharger 2.kicad_sch")) (sheet'))
        (self.project / 'MyCharger 2.kicad_sch').write_text('x')
        after = fingerprints(self.project, 'board.kicad_pcb')['schematic']
        (self.project / 'Charger 2.kicad_sch').write_text('(kicad_sch (version 20250114))\n')
        self.assertNotEqual(before, after)
        self.assertEqual(fingerprints(self.project, 'board.kicad_pcb')['schematic'], after)

    def test_the_original_keeps_only_the_steps(self):
        self.assertEqual(set(original_of(fingerprints(self.project, 'board.kicad_pcb'))),
                         {'schematic', 'placement', 'routing'})

class WorkCopiesOutOfICloud(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.storage = Path(self.folder.name).resolve()

    def tearDown(self):
        self.folder.cleanup()

    def provider(self, environment=None):
        settings = KiCadSettings.from_environment(environment or {}, self.storage)
        return KiCadProvider(settings, execute_cli=None)

    def test_default_folder_is_left_alone_by_icloud(self):
        self.assertEqual(self.provider().work_root, self.storage / 'kicad.nosync')


class WorkCopyReadsAreNeverKept(unittest.TestCase):
    def test_both_folders_count_as_the_work_copy(self):
        for folder in ('kicad', 'kicad.nosync'):
            read = {"action": "read_reference", "path": "/x/.alx/runtime/%s/abc/project/a.kicad_sch" % folder}
            self.assertIsNone(_read_key("kicad", read), folder)
        self.assertIsNotNone(_read_key("kicad", {"action": "read_reference", "path": "/ds/a.pdf"}))


if __name__ == '__main__':
    unittest.main()
