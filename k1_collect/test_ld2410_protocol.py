"""Parser checks using the manufacturer's examples and explicitly corrupted copies."""
import unittest

from ld2410_protocol import HEADER,FOOTER,scan_reports

BASIC=bytes.fromhex('f4 f3 f2 f1 0d 00 02 aa 02 51 00 00 00 00 3b 00 00 55 00 f8 f7 f6 f5')
ENGINEERING=bytes.fromhex('f4 f3 f2 f1 23 00 01 aa 03 1e 00 3c 00 00 39 00 00 08 08 3c 22 05 03 03 04 03 06 05 00 00 39 10 13 06 06 08 04 60 01 55 00 f8 f7 f6 f5')


class ProtocolChecks(unittest.TestCase):
    def test_manufacturer_basic_example(self):
        frames,summary=scan_reports(BASIC)
        self.assertTrue(summary['framing_checks_passed'])
        self.assertEqual(frames[0]['static_energy'],59)

    def test_manufacturer_engineering_example(self):
        frames,summary=scan_reports(ENGINEERING)
        self.assertTrue(summary['framing_checks_passed'])
        self.assertEqual(len(frames[0]['moving_gates']),9)
        self.assertEqual(frames[0]['light'],96)
        self.assertEqual(frames[0]['out_pin'],1)

    def test_high_bit_corruption_is_rejected_without_repair(self):
        bad=bytearray(ENGINEERING)
        bad[6]=0x81
        frames,summary=scan_reports(bytes(bad))
        self.assertEqual(frames,[])
        self.assertEqual(summary['invalid_candidate_count'],1)

    def test_outer_footer_corruption(self):
        bad=BASIC[:-1]+b'\xfd'
        self.assertFalse(scan_reports(bad)[1]['framing_checks_passed'])

    def test_recover_valid_frame_after_corrupted_candidate(self):
        raw=BASIC[:-1]+b'\xfd'+ENGINEERING
        frames,summary=scan_reports(raw)
        self.assertEqual(len(frames),1)
        self.assertGreater(summary['invalid_candidate_count'],0)

    def test_capture_boundary_partial_frame(self):
        raw=BASIC[8:]+BASIC*3+BASIC[:12]
        frames,summary=scan_reports(raw)
        self.assertEqual(len(frames),3)
        self.assertTrue(summary['framing_checks_passed'])

    def test_unexplained_internal_gap(self):
        frames,summary=scan_reports(BASIC+b'garbage'+BASIC)
        self.assertEqual(len(frames),2)
        self.assertFalse(summary['framing_checks_passed'])

    def test_long_unparsed_suffix(self):
        self.assertFalse(scan_reports(BASIC+b'x'*30)[1]['framing_checks_passed'])

    def test_calibration_state_is_not_a_person_state(self):
        data=bytearray(BASIC)
        data[8]=4
        frames,summary=scan_reports(bytes(data))
        self.assertTrue(summary['framing_checks_passed'])
        self.assertEqual(frames[0]['state_name'],'底噪检测中')


if __name__=='__main__':
    unittest.main()
