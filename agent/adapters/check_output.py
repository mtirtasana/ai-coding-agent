"""Bounded local check logs and explicit, retrievable model-facing excerpts."""
import json
import hashlib
from collections import OrderedDict
from uuid import uuid4

from agent.core.types import ToolResult
from .ranges import line_range


class CheckOutputStore:
    def __init__(self, max_entries=8, max_chars=1024 * 1024, archive=None):
        self.entries = OrderedDict()
        self.max_entries, self.max_chars = max_entries, max_chars
        self.archive = archive

    def present(self, result, raw=None, metadata=None):
        """Preserve execution metadata; scanner output always remains verbatim."""
        data = json.loads(result.content)
        def attach(report, captured, details):
            if not isinstance(report, dict) or self.archive is None:
                return
            if isinstance(captured, dict):
                for key in ('fix', 'lint'):
                    value = captured.get(key)
                    if isinstance(value, dict):
                        attach(report.get(key), value.get('raw'), {'command_sha256': hashlib.sha256(
                            json.dumps(value.get('command', [])).encode()).hexdigest()})
            elif isinstance(captured, bytes):
                try:
                    from .artifacts import receipt_view
                    receipt = self.archive.put('check', captured, {
                        **(details or {}), **{key: report[key] for key in ('check', 'exit_code', 'passed', 'timed_out',
                         'capture_complete', 'output_truncated', 'cancelled', 'elapsed_seconds') if key in report}})
                    report['receipt'] = receipt_view(receipt)
                except (OSError, ValueError):
                    report['archive_error'] = 'Capture was not archived; status and inline output remain available.'
        attach(data, raw, metadata)

        def project(report):
            if not isinstance(report, dict):
                return report
            report = dict(report)
            for key in ('fix', 'lint'):
                if key in report:
                    report[key] = project(report[key])
            output = report.get('output')
            limit = 1200 if report.get('passed') is True else 6000
            if (report.get('check') not in {'test', 'lint', 'lint_fix'} or
                    not isinstance(output, str) or len(output) <= limit):
                return report
            # Logs exceeding the local bound remain in the result rather than
            # promising retrieval that cannot be fulfilled.
            if len(output) > self.max_chars:
                return report
            identifier = uuid4().hex
            self.entries[identifier] = output
            while (len(self.entries) > self.max_entries or
                   sum(map(len, self.entries.values())) > self.max_chars):
                self.entries.popitem(last=False)
            excerpt = (output[-limit:] if report.get('passed') is True else
                       output[:limit // 2] + '\n[... omitted output ...]\n' + output[-limit // 2:])
            report.update(output=excerpt, output_excerpt=True, captured_output_chars=len(output),
                          output_id=identifier,
                          output_note='Excerpt only. Use read_check_output for captured lines; logs may expire. Capture truncation is separate.')
            return report

        return ToolResult(result.ok, json.dumps(project(data), ensure_ascii=False))

    def read(self, output_id, start_line, end_line):
        if output_id not in self.entries:
            raise ValueError('Check output is unavailable or expired; no command was rerun.')
        return json.dumps({'output_id': output_id,
                           **line_range(self.entries[output_id], start_line, end_line)}, ensure_ascii=False)


def register_output_reader(registry, store):
    from .tools import spec
    from .ranges import MAX_LINE_NUMBER
    line = {'type': 'integer', 'minimum': 1, 'maximum': MAX_LINE_NUMBER}
    registry.register(spec('read_check_output',
                           'Read captured check output lines without rerunning a command (max 200 lines/12000 characters).',
                           {'output_id': {'type': 'string'}, 'start_line': line, 'end_line': line},
                           read_only=True), store.read)
