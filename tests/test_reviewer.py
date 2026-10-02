import asyncio
import copy
import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from uuid import uuid4
from unittest.mock import AsyncMock, patch

import httpx
from openai import RateLimitError, APIConnectionError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from reviewer import db
from reviewer.app import app, estimate_cost, evaluate_run, request_check, retry_delay
from fixtures import examples
from reviewer.schema import Case, Evaluation, PROMPT_VERSION, CHECK_PROMPTS, citation_input, selection_schema, selected_evaluation, resolve_pointer, validate_evidence
from scripts.collect_evaluation import collect


class ReviewerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"REVIEWER_DB": f"{self.temp.name}/test.sqlite3", "OPENAI_API_KEY": ""})
        self.env.start()
        self.client_context = TestClient(app)
        self.client = self.client_context.__enter__()

    def tearDown(self):
        self.client_context.__exit__(None, None, None)
        self.env.stop()
        self.temp.cleanup()

    def fixture_run(self):
        run_id = uuid4().hex[:12]
        settings = {'model': 'test-model', 'prompt_version': PROMPT_VERSION, 'check_prompts': CHECK_PROMPTS, 'case_delay_seconds': 0, 'check_delay_seconds': 0, 'rate_limit_retries': 2,
                    'max_output_tokens': 6000, 'input_price': None, 'cached_input_price': None, 'output_price': None}
        with db.connect() as conn:
            conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)', (run_id, 'today', 'today', 'completed', 'openai', json.dumps(settings)))
            for case, evaluation in examples():
                conn.execute('INSERT INTO results (run_id,case_id,case_payload,status,evaluation) VALUES (?,?,?,?,?)',
                             (run_id, case.id, case.model_dump_json(), 'completed', evaluation.model_dump_json()))
        return run_id

    def test_dataset_and_benchmark_exclusion(self):
        cases = db.get_cases()
        self.assertEqual(len(cases), 50)
        self.assertEqual(len({c['id'] for c in cases}), 50)
        forbidden = {'reward_info', 'evaluation_criteria', 'review', 'raw_data', 'benchmark_reward'}
        def inspect(value):
            if isinstance(value, dict):
                self.assertFalse(set(value) & forbidden)
                for child in value.values(): inspect(child)
            elif isinstance(value, list):
                for child in value: inspect(child)
        for case in cases:
            inspect(case)
            self.assertTrue(case['initial_state'])
            self.assertTrue(case['final_state'])
            self.assertTrue(case['tool_calls'])
            self.assertEqual(case['provenance']['dataset'], 'τ³ Retail')
        poisoned = copy.deepcopy(cases[0])
        poisoned['final_state']['reward_info'] = {"reward": 1}
        response = self.client.post('/api/cases/import', json={"cases": [poisoned]})
        self.assertEqual(response.status_code, 422)
        poisoned = copy.deepcopy(cases[0])
        poisoned['provenance']['source_url'] = 'javascript:alert(1)'
        self.assertEqual(self.client.post('/api/cases/import', json={"cases": [poisoned]}).status_code, 422)

    def test_independent_signals_in_test_fixtures(self):
        run_id = self.fixture_run()
        run = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(run['counts']['completed'], 8)
        self.assertEqual(run['distributions']['compliance']['CRITICAL NON-COMPLIANCE'], 1)
        self.assertEqual(run['distributions']['resolution']['RESOLVED'], 3)
        self.assertEqual(run['metrics']['total_tokens'], 0)
        self.assertIsNone(run['metrics']['estimated_cost'])
        self.assertEqual(len(self.client.get('/api/cases').json()), 50)
        for field in run['distributions'].values(): self.assertEqual(sum(field.values()), 8)
        case = self.client.get(f'/api/runs/{run_id}/cases/example-002').json()
        self.assertEqual(case['evaluation']['resolution']['result'], 'RESOLVED')
        self.assertEqual(case['evaluation']['compliance']['result'], 'CRITICAL NON-COMPLIANCE')

    def test_source_citations_and_severity_are_enforced(self):
        case, evaluation = next(examples())
        changed = evaluation.model_dump()
        changed['resolution']['evidence'][0]['quote'] = 'fabricated quotation'
        with self.assertRaises(ValueError): validate_evidence(Evaluation.model_validate(changed), case)
        changed = evaluation.model_dump()
        changed['resolution']['evidence'][0]['path'] = '/transcript/999/content'
        with self.assertRaises(ValueError): validate_evidence(Evaluation.model_validate(changed), case)
        changed = evaluation.model_dump()
        changed['resolution']['evidence'][0]['path'] = '/provenance/dataset'
        with self.assertRaises(ValueError): validate_evidence(Evaluation.model_validate(changed), case)
        changed = evaluation.model_dump()
        changed['resolution'].update(result='UNRESOLVED', severity='none')
        with self.assertRaises(ValidationError): Evaluation.model_validate(changed)
        changed['resolution']['evidence'] = []
        with self.assertRaises(ValidationError): Evaluation.model_validate(changed)

    def test_source_selections_preserve_exact_original_evidence(self):
        for raw in db.get_cases():
            case = Case.model_validate(raw)
            payload, sources = citation_input(case)
            self.assertEqual(payload['id'], case.id)
            for source in sources:
                value = resolve_pointer(raw, source['path'])
                text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                self.assertIn(source['quote'], text)
            self.assertEqual(selection_schema(len(sources))['$defs']['Evidence']['properties']['source_id']['maximum'], len(sources) - 1)
        case, evaluation = next(examples())
        _, sources = citation_input(case)
        output = evaluation.model_dump()
        for signal in output.values():
            signal['evidence'] = [{'source_id': sources.index(e)} for e in signal['evidence']]
        self.assertEqual(selected_evaluation(json.dumps(output), sources, case), evaluation)
        for invalid in (-1, len(sources), True, 0.5, '0'):
            broken = copy.deepcopy(output)
            broken['resolution']['evidence'][0]['source_id'] = invalid
            with self.assertRaises(ValueError): selected_evaluation(json.dumps(broken), sources, case)
        broken = copy.deepcopy(output)
        broken['resolution']['severity'] = 'high'
        with self.assertRaises(ValidationError): selected_evaluation(json.dumps(broken), sources, case)

    def test_import_is_atomic_and_run_snapshots_are_immutable(self):
        run_id = self.fixture_run()
        original = self.client.get(f'/api/runs/{run_id}/cases/example-001').json()['case']
        modified = copy.deepcopy(original)
        modified['objective'] = 'A different customer request'
        self.assertEqual(self.client.post('/api/cases/import', json={"cases": [modified]}).status_code, 201)
        self.assertEqual(self.client.get('/api/cases/example-001').json()['case']['objective'], modified['objective'])
        self.assertEqual(self.client.get(f'/api/runs/{run_id}/cases/example-001').json()['case']['objective'], original['objective'])
        self.assertEqual(self.client.post('/api/cases/import', json={"cases": [modified, modified]}).status_code, 422)

    def test_missing_key_unknown_cases_and_cross_origin(self):
        self.assertEqual(self.client.post('/api/runs', json={"mode": "example"}).status_code, 422)
        self.assertEqual(self.client.post('/api/runs', json={}).status_code, 400)
        self.assertEqual(self.client.post('/api/runs', json={}, headers={"Origin": "https://untrusted.example"}).status_code, 403)
        self.assertEqual(self.client.get('/api/cases/missing').status_code, 404)
        self.assertEqual(self.client.get('/api/runs/missing').status_code, 404)
        with patch.dict(os.environ, {"OPENAI_API_KEY": "test-only"}):
            self.assertEqual(self.client.post('/api/runs', json={"case_ids": ["missing"]}).status_code, 404)
            self.assertEqual(self.client.post('/api/runs', json={"case_ids": ["same", "same"]}).status_code, 422)
        self.assertEqual(self.client.post('/api/runs', json={"input_price": -1}).status_code, 422)

    def test_restart_marks_incomplete_runs_without_losing_results(self):
        run_id = self.fixture_run()
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET latency_ms=100 WHERE run_id=?", (run_id,))
            conn.execute("UPDATE results SET status='pending', evaluation=NULL,latency_ms=0 WHERE run_id=? AND case_id='example-001'", (run_id,))
        db.initialize()
        run = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(run['status'], 'interrupted')
        self.assertEqual(run['counts']['completed'], 7)
        self.assertEqual(run['counts']['failed'], 1)
        self.assertEqual(run['metrics']['average_latency_ms'], 100)

    def test_evaluation_pipeline_accounts_for_rejected_output(self):
        run_id = self.fixture_run()
        with db.connect() as conn:
            settings = json.loads(conn.execute('SELECT config FROM runs WHERE id=?', (run_id,)).fetchone()[0])
            settings.update(model='test-model', input_price=1, cached_input_price=0.5, output_price=2)
            conn.execute("UPDATE runs SET status='running',mode='openai' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='pending',evaluation=NULL,check_results=NULL,input_tokens=0,cached_tokens=0,output_tokens=0,total_tokens=0,latency_ms=0,cost=NULL WHERE run_id=?", (run_id,))
        fixtures = {case.id: (case, evaluation) for case, evaluation in examples()}
        requests = []
        invalid_case = 'example-003'
        async def fake_create(**kwargs):
            requests.append(kwargs)
            payload = json.loads(kwargs['input'])
            case_id = payload['id']
            case, judgment = fixtures[case_id]
            _, sources = citation_input(case)
            key = kwargs['text']['format']['schema']['required'][0]
            result = {key: judgment.model_dump()[key]}
            for signal in result.values():
                signal['evidence'] = [{'source_id': sources.index(e)} for e in signal['evidence']]
            if case_id == invalid_case and key == 'factuality': result[key]['evidence'][0]['source_id'] = -1
            return SimpleNamespace(usage=SimpleNamespace(input_tokens=100, input_tokens_details=SimpleNamespace(cached_tokens=20), output_tokens=30,total_tokens=130), output_text=json.dumps(result), status='completed', id='mock-'+case_id, model='test-model-1')
        class FakeClient:
            def __init__(self, **kwargs): self.responses = SimpleNamespace(create=fake_create)
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
        with patch('reviewer.app.AsyncOpenAI', FakeClient): asyncio.run(evaluate_run(run_id, settings))
        run = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(run['status'], 'partial')
        self.assertEqual(run['counts']['completed'], 7)
        self.assertEqual(run['counts']['failed'], 1)
        self.assertEqual(run['metrics']['total_tokens'], 31 * 130)
        self.assertAlmostEqual(run['metrics']['estimated_cost'], 31 * 0.00015)
        self.assertEqual(sum(run['distributions']['resolution'].values()), 7)
        failed = self.client.get(f'/api/runs/{run_id}/cases/example-003').json()
        self.assertIsNone(failed['evaluation'])
        self.assertEqual(failed['input_tokens'], 300)
        self.assertIsNotNone(failed['error'])
        self.assertEqual(failed['diagnostics']['phase'], 'response')
        self.assertEqual(failed['diagnostics']['issues'][0]['reason'], 'ValueError')
        self.assertIn('"source_id": -1', failed['model_output'])
        self.assertTrue(all(r['store'] is False and r['text']['format']['strict'] is True for r in requests))
        self.assertTrue(all('reward_info' not in r['input'] for r in requests))

        settings['stop_on_error'] = True
        requests.clear()
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='pending',evaluation=NULL,check_results=NULL,input_tokens=0,cached_tokens=0,output_tokens=0,total_tokens=0,latency_ms=0,cost=NULL WHERE run_id=?", (run_id,))
        with patch('reviewer.app.AsyncOpenAI', FakeClient): asyncio.run(evaluate_run(run_id, settings))
        stopped = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(len(requests), 11)
        self.assertEqual(stopped['counts'], {'completed': 2, 'failed': 1, 'running': 0, 'pending': 5})

        invalid_case = None
        requests.clear()
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='pending' WHERE run_id=? AND status='failed'", (run_id,))
        with patch('reviewer.app.AsyncOpenAI', FakeClient): asyncio.run(evaluate_run(run_id, settings))
        self.assertEqual(len(requests), 22)
        self.assertEqual(json.loads(requests[0]['input'])['id'], 'example-003')
        self.assertEqual(self.client.get(f'/api/runs/{run_id}').json()['counts']['completed'], 8)

    def test_split_prompts_and_schemas_are_isolated(self):
        config = self.client.get('/api/config').json()
        self.assertNotIn('system_prompt', config)
        self.assertEqual(config['check_prompts'], CHECK_PROMPTS)
        case, evaluation = next(examples())
        _, sources = citation_input(case)
        for key, prompt in CHECK_PROMPTS.items():
            rubric = json.loads(prompt.split('Rubric:\n')[1])
            self.assertEqual(rubric['key'], key)
            schema = selection_schema(len(sources), key)
            self.assertEqual(schema['required'], [key])
            self.assertEqual(set(schema['properties']), {key})
            output = {key: evaluation.model_dump()[key]}
            output[key]['evidence'] = [{'source_id': sources.index(e)} for e in output[key]['evidence']]
            self.assertEqual(selected_evaluation(json.dumps(output), sources, case, key).model_dump()[key], evaluation.model_dump()[key])
            output[key]['severity'] = 'high'
            with self.assertRaises(ValidationError):
                selected_evaluation(json.dumps(output), sources, case, key)

    def test_rate_limit_retry_and_case_pacing_preserve_check_calls(self):
        run_id = self.fixture_run()
        with db.connect() as conn:
            settings = json.loads(conn.execute('SELECT config FROM runs WHERE id=?', (run_id,)).fetchone()[0])
            settings.update(case_delay_seconds=15, check_delay_seconds=2, input_price=1, cached_input_price=0.5, output_price=2)
            conn.execute("UPDATE results SET status='pending',evaluation=NULL WHERE run_id=? AND case_id IN ('example-001','example-002')", (run_id,))
        fixtures = {c.id: (c, e) for c, e in examples()}
        calls = []
        async def fake_create(**kwargs):
            case_id = json.loads(kwargs['input'])['id']
            key = kwargs['text']['format']['schema']['required'][0]
            self.assertEqual(kwargs['instructions'], CHECK_PROMPTS[key])
            calls.append((case_id, key))
            if calls.count(('example-001', 'compliance')) == 1 and (case_id, key) == ('example-001', 'compliance'):
                raise self.limit_error(headers={'retry-after': '7'})
            case, evaluation = fixtures[case_id]
            _, sources = citation_input(case)
            output = {key: evaluation.model_dump()[key]}
            output[key]['evidence'] = [{'source_id': sources.index(e)} for e in output[key]['evidence']]
            return SimpleNamespace(status='completed', output_text=json.dumps(output), id=f'{case_id}-{key}', model='test-model',
                                   usage=SimpleNamespace(input_tokens=100, output_tokens=30, total_tokens=130, input_tokens_details=SimpleNamespace(cached_tokens=20)))
        client = AsyncMock()
        client.__aenter__.return_value.responses.create = fake_create
        with patch('reviewer.app.AsyncOpenAI', return_value=client), patch('reviewer.app.asyncio.sleep', new_callable=AsyncMock) as sleep:
            asyncio.run(evaluate_run(run_id, settings))
            self.assertEqual([call.args[0] for call in sleep.await_args_list], [2, 7, 2, 2, 15, 2, 2, 2])
        self.assertEqual(len(calls), 9)
        self.assertEqual(calls.count(('example-001', 'resolution')), 1)
        result = self.client.get(f'/api/runs/{run_id}/cases/example-001').json()
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['total_tokens'], 520)
        self.assertAlmostEqual(result['cost'], 4 * .00015)
        self.assertEqual(set(result['check_results']), set(CHECK_PROMPTS))
        self.assertEqual(len({check['response_id'] for check in result['check_results'].values()}), 4)
        self.assertIsNone(result['error'])
        self.assertIsNone(result['diagnostics'])

    @staticmethod
    def limit_error(code='rate_limit_exceeded', headers=None):
        return RateLimitError('Test API limit', response=httpx.Response(429, headers=headers or {}, request=httpx.Request('POST', 'https://api.openai.com/v1/responses')),
                              body={'code': code, 'type': code})

    def test_rate_limit_pause_and_resume_preserve_completed_checks(self):
        run_id = self.fixture_run()
        case, evaluation = next(examples())
        _, sources = citation_input(case)
        settings = json.loads(self.client.get(f'/api/runs/{run_id}').text)['config']
        settings['check_prompts'] = CHECK_PROMPTS
        calls = []
        throttled = True
        async def fake_create(**kwargs):
            key = kwargs['text']['format']['schema']['required'][0]
            calls.append(key)
            if key == 'compliance' and throttled:
                raise self.limit_error(headers={'retry-after': '1'})
            output = {key: evaluation.model_dump()[key]}
            output[key]['evidence'] = [{'source_id': sources.index(e)} for e in output[key]['evidence']]
            return SimpleNamespace(status='completed', output_text=json.dumps(output), id=key, model='test-model',
                                   usage=SimpleNamespace(input_tokens=100, output_tokens=30, total_tokens=130, input_tokens_details=None))
        client = AsyncMock()
        client.__aenter__.return_value.responses.create = fake_create
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='pending',evaluation=NULL WHERE run_id=? AND case_id=?", (run_id, case.id))
        with patch('reviewer.app.AsyncOpenAI', return_value=client), patch('reviewer.app.asyncio.sleep', new_callable=AsyncMock) as sleep:
            asyncio.run(evaluate_run(run_id, settings))
            self.assertEqual(sleep.await_count, 2)
            run = self.client.get(f'/api/runs/{run_id}').json()
            self.assertEqual(run['status'], 'paused')
            self.assertEqual(run['counts']['failed'], 0)
            detail = self.client.get(f'/api/runs/{run_id}/cases/{case.id}').json()
            self.assertEqual(set(detail['check_results']), {'resolution'})
            self.assertEqual(detail['diagnostics']['phase'], 'rate_limit')
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('reviewer.app.start_worker') as start:
                self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume').status_code, 200)
                start.assert_called_once()
                self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume').status_code, 409)
            # Restart before resuming the worker must retain the same checkpoint.
            db.initialize()
            self.assertEqual(self.client.get(f'/api/runs/{run_id}').json()['status'], 'interrupted')
            with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('reviewer.app.start_worker'):
                self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume').status_code, 200)
            throttled = False
            asyncio.run(evaluate_run(run_id, settings))
        self.assertEqual(calls, ['resolution', 'compliance', 'compliance', 'compliance', 'compliance', 'factuality', 'communication'])
        detail = self.client.get(f'/api/runs/{run_id}/cases/{case.id}').json()
        self.assertEqual(detail['status'], 'completed')
        self.assertEqual(detail['total_tokens'], 520)

    def test_connection_interruptions_pause_and_older_connection_failures_resume(self):
        run_id = self.fixture_run()
        with db.connect() as conn:
            settings = json.loads(conn.execute('SELECT config FROM runs WHERE id=?', (run_id,)).fetchone()[0])
            conn.execute("UPDATE runs SET status='running' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='pending',evaluation=NULL WHERE run_id=? AND case_id='example-001'", (run_id,))
        client = AsyncMock()
        client.__aenter__.return_value.responses.create.side_effect = APIConnectionError(request=httpx.Request('POST', 'https://api.openai.com/v1/responses'))
        with patch('reviewer.app.AsyncOpenAI', return_value=client):
            asyncio.run(evaluate_run(run_id, settings))
        run = self.client.get(f'/api/runs/{run_id}').json()
        self.assertEqual(run['status'], 'paused')
        self.assertTrue(run['can_resume'])
        self.assertEqual(run['counts']['failed'], 0)
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status='partial' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='failed' WHERE run_id=? AND case_id='example-001'", (run_id,))
        self.assertTrue(self.client.get(f'/api/runs/{run_id}').json()['can_resume'])
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}), patch('reviewer.app.start_worker'):
            self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume').status_code, 200)
        with db.connect() as conn:
            conn.execute("UPDATE runs SET status='partial' WHERE id=?", (run_id,))
            conn.execute("UPDATE results SET status='failed',diagnostics=? WHERE run_id=? AND case_id='example-001'", (json.dumps({'phase':'structure'}), run_id))
        self.assertFalse(self.client.get(f'/api/runs/{run_id}').json()['can_resume'])
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'test-only'}):
            self.assertEqual(self.client.post(f'/api/runs/{run_id}/resume').status_code, 409)

    def test_quota_and_long_cooldowns_do_not_retry(self):
        for code in ('insufficient_quota', 'credit_balance_exhausted', 'organization_usage_limit_exceeded', 'project_spend_limit_exceeded', 'rate_limit_exceeded'):
            error = self.limit_error(code, {'retry-after': '600' if code == 'rate_limit_exceeded' else '1'})
            client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=error)))
            with patch('reviewer.app.asyncio.sleep', new_callable=AsyncMock) as sleep:
                with self.assertRaises(RateLimitError):
                    asyncio.run(request_check(client, 'unused', 'unused', 'resolution', {'rate_limit_retries': 5}))
                self.assertEqual(client.responses.create.await_count, 1)
                sleep.assert_not_awaited()
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(side_effect=self.limit_error(headers={'retry-after': '200'}))))
        with patch('reviewer.app.progress'), patch('reviewer.app.asyncio.sleep', new_callable=AsyncMock) as sleep:
            with self.assertRaises(RateLimitError):
                asyncio.run(request_check(client, 'unused', 'unused', 'resolution', {'rate_limit_retries': 5}))
            self.assertEqual(client.responses.create.await_count, 2)
            sleep.assert_awaited_once_with(200)
        self.assertEqual(retry_delay(self.limit_error(headers={'retry-after-ms': '1500'}), 0), 1.5)
        with patch('reviewer.app.random.uniform', return_value=0.5):
            self.assertEqual(retry_delay(self.limit_error(headers={'retry-after': 'invalid'}), 2), 8.5)

    def test_price_estimate_does_not_invent_prices(self):
        usage = dict(input_tokens=100, cached_tokens=20, output_tokens=30)
        self.assertIsNone(estimate_cost(usage, dict(input_price=None, cached_input_price=None, output_price=2)))
        self.assertAlmostEqual(estimate_cost(usage, dict(input_price=1, cached_input_price=None, output_price=2)), .00016)

    def test_collection_preserves_sources_and_requires_complete_results(self):
        case, evaluation = next(examples())
        ids = [c['id'] for c in db.get_cases()]
        settings = {'prompt_version': PROMPT_VERSION, 'model': 'test-model'}
        with db.connect() as conn:
            conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)', ('source', 'today', 'today', 'completed', 'openai', json.dumps(settings)))
            for case_id in ids:
                snapshot = case.model_copy(update={'id': case_id})
                conn.execute('INSERT INTO results (run_id,case_id,case_payload,status,evaluation,response_id,total_tokens,cost) VALUES (?,?,?,?,?,?,?,?)',
                             ('source', case_id, snapshot.model_dump_json(), 'completed', evaluation.model_dump_json(), 'response-'+case_id, 100, 0.01))
        ledger = {'attempts': [{'case_id': case_id, 'run_id': 'source', 'status': 'completed', 'prompt_version': PROMPT_VERSION} for case_id in ids]}
        with self.assertRaises(ValueError): collect({'attempts': ledger['attempts'][:-1]})
        collection_id = collect(ledger)
        self.assertEqual(collect(ledger), collection_id)
        summary = self.client.get('/api/runs/' + collection_id).json()
        self.assertEqual(summary['mode'], 'collection')
        self.assertEqual(summary['counts']['completed'], 50)
        self.assertEqual(summary['metrics']['total_tokens'], 5000)
        self.assertAlmostEqual(summary['metrics']['estimated_cost'], 0.5)
        self.assertEqual(summary['config']['source_runs'], dict.fromkeys(ids, 'source'))
        with db.connect() as conn:
            original = dict(conn.execute('SELECT * FROM results WHERE run_id=? AND case_id=?', ('source', ids[0])).fetchone())
            copied = dict(conn.execute('SELECT * FROM results WHERE run_id=? AND case_id=?', (collection_id, ids[0])).fetchone())
            original.pop('run_id'); copied.pop('run_id')
            self.assertEqual(original, copied)
            conn.execute("UPDATE results SET status='failed' WHERE run_id='source' AND case_id=?", (ids[0],))
        with self.assertRaises(ValueError): collect(ledger)


if __name__ == '__main__':
    unittest.main()
