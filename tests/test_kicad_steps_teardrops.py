"""A pad's teardrop setting is routing, not placement (24 Nov P0005 teardrop pass)."""
import unittest

from alx.providers.kicad_steps import board_steps

BOARD = '''(kicad_pcb (version 20260206) (generator "pcbnew")
  (footprint "FireFli_Resistors:R_0603_1608Metric" (layer "F.Cu") (at 10 20 90)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.8 0 90) (size 0.9 0.95) (layers "F.Cu" "F.Mask" "F.Paste")%s)))
'''
TEARDROPS = ''' (teardrops (best_length_ratio 0.5) (max_length 1) (best_width_ratio 1) (max_width 2)
      (curved_edges no) (filter_ratio 0.9) (enabled yes) (allow_two_segments yes) (prefer_zone_connections yes))'''


class PadTeardropsAreRouting(unittest.TestCase):
    def test_teardrop_setting_does_not_change_placement(self):
        self.assertEqual(board_steps(BOARD % '')[0], board_steps(BOARD % TEARDROPS)[0])

    def test_a_larger_pad_still_changes_placement(self):
        bigger = BOARD.replace('(size 0.9 0.95)', '(size 1.2 0.95)')
        self.assertNotEqual(board_steps(BOARD % '')[0], board_steps(bigger % '')[0])


if __name__ == '__main__':
    unittest.main()
