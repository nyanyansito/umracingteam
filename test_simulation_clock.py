import unittest
from unittest.mock import patch
from simulation_clock import SimulationClock


class SimulationClockTests(unittest.TestCase):
    def test_host_pause_does_not_skip_virtual_time_or_create_catchup_burst(self):
        with patch("simulation_clock.time.monotonic", side_effect=[0, 5, 5.1]), patch("simulation_clock.time.sleep") as sleep:
            clock = SimulationClock(.1)
            clock.advance()
            self.assertAlmostEqual(clock.elapsed, .1)
            self.assertAlmostEqual(sleep.call_args.args[0], .1)
            clock.advance()
            self.assertAlmostEqual(clock.elapsed, .2)
            self.assertAlmostEqual(sleep.call_args.args[0], .1)

    def test_normal_schedule_accounts_for_work_time(self):
        with patch("simulation_clock.time.monotonic", side_effect=[0, .02, .12]), patch("simulation_clock.time.sleep") as sleep:
            clock = SimulationClock(.1)
            clock.advance()
            clock.advance()
            self.assertAlmostEqual(clock.elapsed, .2)
            for call in sleep.call_args_list:
                self.assertAlmostEqual(call.args[0], .08)


if __name__ == "__main__":
    unittest.main()
