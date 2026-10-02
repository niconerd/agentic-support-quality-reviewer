"""Collect the validated conversation runs into one inspectable dashboard record."""
import json
from datetime import datetime, timezone
from uuid import uuid4

from dotenv import load_dotenv

from reviewer import db
from reviewer.schema import PROMPT_VERSION


def collect(ledger):
    selected = {a['case_id']: a['run_id'] for a in ledger['attempts']
                if a['status'] == 'completed' and a['prompt_version'] == PROMPT_VERSION}
    with db.connect() as conn:
        expected = {row['id'] for row in conn.execute('SELECT id FROM cases')}
        if len(expected) != 50 or set(selected) != expected:
            raise ValueError('All 50 corpus cases must validate under the current prompt before collection.')
        rows, settings = [], None
        for case_id, run_id in sorted(selected.items()):
            run = conn.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            row = conn.execute('SELECT * FROM results WHERE run_id=? AND case_id=?', (run_id, case_id)).fetchone()
            if not run or run['mode'] != 'openai' or not row or row['status'] != 'completed' or not row['evaluation']:
                raise ValueError('Collection source is not a completed live review.')
            config = json.loads(run['config'])
            if config['prompt_version'] != PROMPT_VERSION or (settings is not None and settings != config):
                raise ValueError('Collection sources must share the same evaluation configuration.')
            settings = config
            rows.append(dict(row))
        settings['source_runs'] = selected
        encoded = json.dumps(settings, sort_keys=True)
        existing = conn.execute("SELECT id FROM runs WHERE mode='collection' AND config=?", (encoded,)).fetchone()
        if existing:
            return existing['id']
        run_id = uuid4().hex[:12]
        timestamp = datetime.now(timezone.utc).isoformat()
        conn.execute('INSERT INTO runs VALUES (?,?,?,?,?,?)', (run_id, timestamp, timestamp, 'completed', 'collection', encoded))
        for row in rows:
            row['run_id'] = run_id
            columns = ','.join(row)
            conn.execute(f"INSERT INTO results ({columns}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
    return run_id


if __name__ == '__main__':
    load_dotenv(db.ROOT / '.env')
    ledger = json.loads((db.ROOT / 'data/conversation-retry.json').read_text())
    print(json.dumps({'collection_id': collect(ledger), 'new_api_calls': 0}))
