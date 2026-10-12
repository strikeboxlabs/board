"""Durable fail-stop latch for intents, including temporary API outages."""
import json
from pathlib import Path
import uuid


class IntentFailures:
    def __init__(self, backend):
        self.backend = backend
        self.cache = {}

    def state(self, project_id):
        if project_id not in self.cache:
            path = Path(self.backend.reports_dir(project_id)) / 'intent-errors.json'
            try:
                data = json.loads(path.read_text())
                if not isinstance(data, dict) or not all(isinstance(v, dict) and isinstance(v.get('error'), str) for v in data.values()):
                    raise ValueError('Invalid intent error state')
            except FileNotFoundError:
                data = {}
            self.cache[project_id] = data
        return self.cache[project_id]

    def record(self, project_id, intent_id, worker, error):
        data = self.state(project_id)
        data[intent_id] = {'worker': worker, 'error': error[:2000]}
        path = Path(self.backend.reports_dir(project_id)) / 'intent-errors.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(json.dumps(data, sort_keys=True) + '\n')
        temp.replace(path)
