"""Child test fixture: replace ONLY the external engine, run normal main.py.

The real Controller/Model Hub service/router/gateway/provider, Session/Harness,
Vibey tools and SQLite run unchanged. This is not real CPA acceptance evidence.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from tests import network_guard
sys.addaudithook(network_guard.audit)

from core.handlers.model_hub.adapter import RawOutcomeKind
from tests.scenario_harness.model_hub import ModelHubScenarioAdapter, ScenarioCallResult


class ScriptedEngine(ModelHubScenarioAdapter):
    async def invoke(self, source_id, model_id, request, stream, origin, *, on_admitted=None):
        messages = request.get("messages", [])
        has_result = any(item.get("role") == "tool" for item in messages)
        if not has_result:
            program = (
                "import os,sqlite3; from pathlib import Path; "
                "Path('笔记.md').write_text('可查询的任务笔记', encoding='utf-8'); "
                "db=sqlite3.connect('task.sqlite'); "
                "db.execute('CREATE TABLE evidence(value TEXT)'); "
                "db.execute(\"INSERT INTO evidence VALUES ('task-data')\"); db.commit(); db.close(); "
                "Path('task-env.json').write_text(__import__('json').dumps({"
                "'cwd':os.getcwd(),'home':os.environ['HOME'],"
                "'credential_present':'DEEPSEEK_API_KEY' in os.environ,"
                "'session':os.environ.get('AVIBE_SESSION_ID'),"
                "'java_home':os.environ.get('JAVA_HOME')}))"
            )
            if os.environ.get("TB_TEST_SCENARIO") == "slow-tool":
                program = "import time; from pathlib import Path\nwhile True:\n Path('still-writing').write_text(str(time.time_ns())); time.sleep(.02)"
            import shlex
            command = "python3 -c " + shlex.quote(program)
            if os.environ.get("TB_TEST_SCENARIO") == "watch":
                program = "import time; from pathlib import Path; time.sleep(4); Path('watch-note.txt').write_text('continued')"
                command = "vibe watch add --message 'The note is ready; finish.' -- python3 -c " + shlex.quote(program)
            delta = {"tool_calls": [{"index": 0, "id": "fixture-call", "type": "function",
                                     "function": {"name": "bash", "arguments": json.dumps({
                                         "command": command,
                                         "timeout": 20, "watch": False})}}]}
            reason = "tool_calls"
        else:
            scenario = os.environ.get("TB_TEST_SCENARIO")
            if scenario == "empty-response":
                delta, reason = {}, "stop"
            elif scenario == "thinking-length":
                delta, reason = {"reasoning_content": "private-reasoning-秘密"}, "length"
            else:
                delta, reason = {"content": "Finished integration fixture. fixture-private-key-not-real"}, "stop"
        rows = [
            {"id": "fixture-response", "model": model_id,
             "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
            {"id": "fixture-response", "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
             "usage": {"prompt_tokens": 100, "completion_tokens": 20}},
        ]
        body = ("".join("data: " + json.dumps(row) + "\n\n" for row in rows) + "data: [DONE]\n\n").encode()
        self.invoke_results.append(ScenarioCallResult(RawOutcomeKind.SUCCESS, status=200, body=body, stream_started=True))
        return await super().invoke(source_id, model_id, request, stream, origin, on_admitted=on_admitted)


if __name__ == "__main__":
    import vibe.model_hub_runtime
    adapter = ScriptedEngine(discovery_models=["fixture-model"])
    vibe.model_hub_runtime.get_model_hub_engine_adapter = lambda: adapter
    from scripts.terminal_bench.control import main
    raise SystemExit(main(["serve"]))
