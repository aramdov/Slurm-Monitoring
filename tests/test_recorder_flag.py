"""The recorder flag must be read when app.py decides, not when config is first imported.

test_firebird_node_names imports slurm (and so config) before any module sets
SLURM_DASHBOARD_DISABLE_RECORDER. A later `import app` then started the real
recorder, which ran scontrol and broke mocked tests about one run in six.
"""
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = """
import os, sys, threading
os.environ.pop('SLURM_DASHBOARD_DISABLE_RECORDER', None)
import config                      # imported early, flag unset
os.environ['SLURM_DASHBOARD_DISABLE_RECORDER'] = '1'
started = []
threading.Thread.start = lambda self: started.append(self._target.__name__)
import app
print(','.join(started))
"""


class RecorderFlagTest(unittest.TestCase):
    def test_flag_set_after_config_import_still_disables_recorder(self):
        env = {k: v for k, v in os.environ.items() if k != 'SLURM_DASHBOARD_DISABLE_RECORDER'}
        with tempfile.TemporaryDirectory() as tmp:
            env['SLURM_DASHBOARD_DB'] = os.path.join(tmp, 'h.db')
            out = subprocess.run([sys.executable, '-c', PROBE], cwd=ROOT, env=env,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), '', 'recorder started: ' + out.stdout)


if __name__ == '__main__':
    unittest.main()
