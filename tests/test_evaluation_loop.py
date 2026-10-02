import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import evaluate_until_error as runner


class EvaluationLoopTest(unittest.TestCase):
    def test_stops_at_failure_and_retries_that_case_before_continuing(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(runner, 'LEDGER', Path(directory) / 'ledger.json'), contextlib.redirect_stdout(io.StringIO()):
            requested = []
            fail = True

            def api(path, body=None):
                if path == '/api/config':
                    return {'prompt_version': 'test', 'pacing': {'case_delay_seconds': 0}}
                if path == '/api/cases':
                    return [{'id': 'a'}, {'id': 'b'}, {'id': 'c'}]
                if body:
                    requested.extend(body['case_ids'])
                    return {'id': body['case_ids'][0]}
                if '/cases/' in path:
                    return {'diagnostics': {'phase': 'evidence'}}
                failed = path.endswith('/b') and fail
                return {'status': 'failed' if failed else 'completed', 'metrics': {},
                        'results': [{'status': 'failed' if failed else 'completed', 'error': 'Invalid quote' if failed else None}]}

            with patch.object(runner, 'api', side_effect=api):
                self.assertEqual(runner.main(), 1)
                self.assertEqual(requested, ['a', 'b'])
                fail = False
                self.assertEqual(runner.main(), 0)
                self.assertEqual(requested, ['a', 'b', 'b', 'c'])
            self.assertEqual(len(json.loads(runner.LEDGER.read_text())['attempts']), 4)
