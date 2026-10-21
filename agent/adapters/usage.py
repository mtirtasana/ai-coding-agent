"""Host-local usage metadata. No messages, tool arguments, keys or endpoints enter SQLite."""
import argparse
import shlex
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass, field
from datetime import date, datetime, time as daytime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from .user_paths import user_directory


PERIODS = ('session', 'today', 'week', 'month', '7d', '30d', 'all', 'custom')
DIMENSIONS = ('provider', 'profile', 'model', 'interface', 'kind')
COUNTS = ('calls', 'responses', 'unfinished', 'failed', 'interrupted')
TOKEN_FIELDS = ('input_tokens', 'output_tokens', 'cached_input_tokens')
MAX_INT = 2**63 - 1


@dataclass
class UsageSession:
    id: str = field(default_factory=lambda: str(uuid4()))

    def reset(self):
        self.id = str(uuid4())


def period_bounds(period, start=None, end=None, now=None):
    now = time.time() if now is None else now
    if period not in PERIODS:
        raise ValueError('Unknown usage period.')
    local = datetime.fromtimestamp(now)
    def midnight(day):
        return datetime.combine(day, daytime.min).timestamp()
    if start is not None or end is not None:
        if period != 'custom' or start is None or end is None:
            raise ValueError('Custom dates require both --from and --to, without another period.')
        try:
            if not all(isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}', value) for value in (start, end)):
                raise ValueError()
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first > last:
                raise ValueError()
            return midnight(first), midnight(last + timedelta(days=1))
        except (TypeError, ValueError, OverflowError):
            raise ValueError('Use ordered YYYY-MM-DD dates for --from and --to.') from None
    if period == 'custom':
        raise ValueError('Custom usage requires --from and --to.')
    if period in {'session', 'all'}:
        return None, None
    if period in {'7d', '30d'}:
        return now - (7 if period == '7d' else 30) * 86400, now
    today = local.date()
    first = today if period == 'today' else (today - timedelta(days=today.weekday()) if period == 'week' else today.replace(day=1))
    return midnight(first), now


class UsageParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError(message)


def parse_usage(text, default='session'):
    parser = UsageParser(add_help=False)
    parser.add_argument('period', nargs='?', choices=PERIODS)
    parser.add_argument('--from', dest='start')
    parser.add_argument('--to', dest='end')
    for key in DIMENSIONS:
        parser.add_argument('--' + key)
    args = parser.parse_args(shlex.split(text))
    value = {k: v for k, v in vars(args).items() if v is not None}
    if args.start or args.end:
        if args.period not in {None, 'custom'}:
            raise ValueError('Choose a preset period or custom dates, not both.')
        value['period'] = 'custom'
    value.setdefault('period', default)
    return value


def validate_query(value, session_id=None, now=None):
    if not isinstance(value, dict) or set(value) - {'period', 'start', 'end', 'sessionId', *DIMENSIONS}:
        raise ValueError('Invalid usage query.')
    value = dict(value)
    for key, item in value.items():
        if not isinstance(item, str) or not item or len(item) > 256:
            raise ValueError('Usage query values must be bounded strings.')
    value.setdefault('period', 'session')
    if value.get('interface') not in {None, 'cli', 'vscode'} or value.get('kind') not in {None, 'agent', 'completion'}:
        raise ValueError('Invalid usage interface or category.')
    bounds = period_bounds(value['period'], value.get('start'), value.get('end'), now)
    if value['period'] == 'session':
        value['sessionId'] = value.get('sessionId') or session_id
        if not value['sessionId']:
            raise ValueError('Session statistics need an active conversation; choose today or another historical period.')
    return value, bounds


def empty_group():
    return {**dict.fromkeys(COUNTS, 0), **{field + suffix: 0 for field in TOKEN_FIELDS for suffix in ('_sum', '_known')}}


def row_metrics(row):
    result = empty_group()
    result.update(calls=1, responses=row['responded'], unfinished=int(row['status'] == 'pending'),
                  failed=int(row['status'] == 'failed'), interrupted=int(row['status'] == 'interrupted'))
    for key in TOKEN_FIELDS:
        if row[key] is not None:
            result[key + '_sum'], result[key + '_known'] = row[key], 1
    return result


def public_group(value):
    return {**{key: value[key] for key in COUNTS}, 'tokens': {
        key: {'reported_sum': value[key + '_sum'] if value[key + '_known'] else None,
              'responses_with_value': value[key + '_known']} for key in TOKEN_FIELDS}}


class UsageJournal:
    def __init__(self, path=None, warn=lambda message: None, clock=time.time, max_memory=5000):
        self.path = Path(path) if path is not None else user_directory('state') / 'usage.sqlite3'
        self.warn, self.clock = warn, clock
        self.memory = {}
        self.lock = threading.RLock()
        self.storage_incomplete = False
        self.warned = False
        self.max_memory = max_memory
        self.dropped = 0

    def warning(self):
        self.storage_incomplete = True
        if not self.warned:
            self.warned = True
            self.warn('Usage history storage is unavailable or incomplete. Coding continues; current-process totals remain in memory.')

    def connect(self, write=False):
        if write:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            connection = sqlite3.connect(self.path, timeout=0.25, isolation_level=None)
        else:
            connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True,
                                         timeout=0.25, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version not in {0, 1}:
                raise sqlite3.DatabaseError('Unsupported usage schema.')
            if write:
                connection.execute('PRAGMA journal_mode=WAL')
                connection.execute('BEGIN IMMEDIATE')
                connection.execute('''CREATE TABLE IF NOT EXISTS calls (
                    request_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, run_id TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, status TEXT NOT NULL, responded INTEGER NOT NULL,
                    provider TEXT NOT NULL, profile TEXT NOT NULL, model TEXT NOT NULL,
                    interface TEXT NOT NULL, kind TEXT NOT NULL,
                    input_tokens INTEGER, output_tokens INTEGER, cached_input_tokens INTEGER)''')
                connection.execute('CREATE INDEX IF NOT EXISTS usage_started ON calls(started)')
                connection.execute('CREATE INDEX IF NOT EXISTS usage_session ON calls(session_id)')
                connection.execute('PRAGMA user_version=1')
                connection.execute('COMMIT')
                if self.path.exists():
                    self.path.chmod(0o600)
            elif version != 1:
                raise sqlite3.DatabaseError('Usage schema is not initialized.')
            return connection
        except BaseException:
            connection.close()
            raise

    def persist(self, row):
        try:
            with closing(self.connect(True)) as connection:
                keys = ('request_id', 'session_id', 'run_id', 'started', 'finished', 'status', 'responded',
                        'provider', 'profile', 'model', 'interface', 'kind', *TOKEN_FIELDS)
                values = [row[key] for key in keys]
                connection.execute('''INSERT INTO calls (
                    request_id,session_id,run_id,started,finished,status,responded,provider,profile,model,
                    interface,kind,input_tokens,output_tokens,cached_input_tokens)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET
                    finished=excluded.finished,status=excluded.status,responded=excluded.responded,
                    input_tokens=excluded.input_tokens,output_tokens=excluded.output_tokens,
                    cached_input_tokens=excluded.cached_input_tokens''', values)
            row['_persisted'] = True
        except (OSError, sqlite3.Error):
            row['_persisted'] = False
            self.warning()

    def start(self, session_id, run_id, provider, profile, model, interface, kind):
        labels = (session_id, run_id, provider, profile, model, interface, kind)
        if any(not isinstance(label, str) or not label or len(label) > 256 for label in labels):
            raise ValueError('Invalid usage labels.')
        if interface not in {'cli', 'vscode'} or kind not in {'agent', 'completion'}:
            raise ValueError('Invalid usage source.')
        row = {'request_id': str(uuid4()), 'session_id': session_id, 'run_id': run_id,
               'started': self.clock(), 'finished': None, 'status': 'pending', 'responded': 0,
               'provider': provider, 'profile': profile, 'model': model, 'interface': interface, 'kind': kind,
               **dict.fromkeys(TOKEN_FIELDS, None), '_persisted': False}
        with self.lock:
            self.memory[row['request_id']] = row
            self.persist(row)
            while len(self.memory) > self.max_memory:
                self.memory.pop(next(iter(self.memory)))
                self.dropped += 1
                self.warning()
                if self.dropped == 1:
                    self.warn('Usage buffer limit reached; some unpersisted metadata was dropped. History is incomplete.')
        return row['request_id']

    def finish(self, request_id, usage=None, status='completed'):
        with self.lock:
            row = self.memory.get(request_id)
            if row is None:
                return  # Already durable/finalized, or explicitly reported as dropped.
            row.update(finished=self.clock(), status=status, responded=int(status == 'completed'), _persisted=False)
            for key in TOKEN_FIELDS:
                count = getattr(usage, key, None)
                row[key] = count if type(count) is int and 0 <= count <= MAX_INT else None
            self.persist(row)
            if row['_persisted']:
                self.memory.pop(request_id)

    def prune(self, before):
        """Explicit maintenance only; never invoked by ordinary reports or coding."""
        boundary, _ = period_bounds('custom', before, before)
        with self.lock:
            if not self.path.exists():
                return 0
            with closing(self.connect(True)) as connection:
                deleted = connection.execute("DELETE FROM calls WHERE started < ? AND status != 'pending'", (boundary,)).rowcount
            self.memory = {key: row for key, row in self.memory.items() if row['started'] >= boundary or row['status'] == 'pending'}
            return deleted

    def volatile_calls(self):
        """Read-only snapshots of writes that could not reach SQLite, for sibling workers."""
        with self.lock:
            return [{key: value for key, value in row.items() if not key.startswith('_')}
                    for row in self.memory.values() if not row['_persisted']]

    def query(self, options=None, session_id=None, volatile_calls=None):
        options, (start, end) = validate_query(options or {}, session_id, self.clock())
        overlays = {}
        for row in volatile_calls or []:
            if (not isinstance(row, dict) or set(row) != {'request_id', 'session_id', 'run_id', 'started', 'finished',
                    'status', 'responded', *DIMENSIONS, *TOKEN_FIELDS}
                    or any(not isinstance(row.get(key), str) or not row[key] or len(row[key]) > 256
                           for key in ('request_id', 'session_id', 'run_id', *DIMENSIONS))
                    or type(row['started']) not in (int, float) or not 0 <= row['started'] <= MAX_INT
                    or row['finished'] is not None and (type(row['finished']) not in (int, float) or not 0 <= row['finished'] <= MAX_INT)
                    or row['status'] not in {'pending', 'completed', 'failed', 'interrupted'}
                    or type(row['responded']) is not int or row['responded'] != int(row['status'] == 'completed')
                    or row['interface'] not in {'cli', 'vscode'} or row['kind'] not in {'agent', 'completion'}
                    or any(row[key] is not None and (type(row[key]) is not int or not 0 <= row[key] <= MAX_INT) for key in TOKEN_FIELDS)):
                raise ValueError('Invalid volatile usage metadata.')
            overlays[row['request_id']] = dict(row)
        parameters = []
        for value in [options.get(key) for key in DIMENSIONS] + [
                options.get('sessionId') if options['period'] == 'session' else None, start, end]:
            parameters += [value, value]
        groups = {}
        available = False
        with self.lock:
            overlays.update({key: row for key, row in self.memory.items() if not row['_persisted']})
            dirty = list(overlays.values())
            try:
                if self.path.exists():
                    with closing(self.connect()) as connection:
                        connection.execute('CREATE TEMP TABLE omitted (id TEXT PRIMARY KEY)')
                        connection.executemany('INSERT INTO omitted VALUES (?)', [(r['request_id'],) for r in dirty])
                        rows = connection.execute('''SELECT provider,profile,model,interface,kind,
                            COUNT(*) AS calls,SUM(responded) AS responses,SUM(status='pending') AS unfinished,
                            SUM(status='failed') AS failed,SUM(status='interrupted') AS interrupted,
                            COALESCE(SUM(input_tokens),0) AS input_tokens_sum,COUNT(input_tokens) AS input_tokens_known,
                            COALESCE(SUM(output_tokens),0) AS output_tokens_sum,COUNT(output_tokens) AS output_tokens_known,
                            COALESCE(SUM(cached_input_tokens),0) AS cached_input_tokens_sum,COUNT(cached_input_tokens) AS cached_input_tokens_known
                            FROM calls WHERE request_id NOT IN (SELECT id FROM omitted)
                            AND (? IS NULL OR provider=?) AND (? IS NULL OR profile=?) AND (? IS NULL OR model=?)
                            AND (? IS NULL OR interface=?) AND (? IS NULL OR kind=?) AND (? IS NULL OR session_id=?)
                            AND (? IS NULL OR started>=?) AND (? IS NULL OR started<?)
                            GROUP BY provider,profile,model,interface,kind''', parameters)
                        for row in rows:
                            groups[tuple(row[d] for d in DIMENSIONS)] = {key: row[key] for key in empty_group()}
                    available = True
            except (OSError, sqlite3.Error):
                groups.clear()
                self.warning()
            pending = dirty if available else {**self.memory, **overlays}.values()
            for row in pending:
                if any(row[key] != options[key] for key in DIMENSIONS if key in options):
                    continue
                if options['period'] == 'session' and row['session_id'] != options['sessionId']:
                    continue
                if start is not None and not start <= row['started'] < end:
                    continue
                key = tuple(row[d] for d in DIMENSIONS)
                target = groups.setdefault(key, empty_group())
                for metric, count in row_metrics(row).items():
                    target[metric] += count
        totals = empty_group()
        result_groups = []
        for labels, metrics in sorted(groups.items()):
            for key, count in metrics.items():
                totals[key] += count
            result_groups.append({**dict(zip(DIMENSIONS, labels, strict=True)), **public_group(metrics)})
        return {'period': options['period'], 'timezone': datetime.fromtimestamp(self.clock()).astimezone().tzname(),
                'start': None if start is None else datetime.fromtimestamp(start, timezone.utc).isoformat(),
                'end_exclusive': None if end is None else datetime.fromtimestamp(end, timezone.utc).isoformat(),
                'storage_incomplete': self.storage_incomplete or bool(dirty),
                'totals': public_group(totals), 'groups': result_groups}


class MeteredProvider:
    def __init__(self, provider, journal, session_id, *, provider_name, model, profile='ad-hoc',
                 interface='cli', kind='agent', run_id=None):
        self.provider, self.journal, self.session_id = provider, journal, session_id
        # Legacy flags have no label length bound; accounting must not prevent inference.
        def bounded(value):
            return value if len(value) <= 256 else value[:223] + '~' + sha256(value.encode()).hexdigest()[:32]
        self.labels = (run_id or str(uuid4()), bounded(provider_name), bounded(profile), bounded(model), interface, kind)

    def generate(self, messages, tools):
        request_id = self.journal.start(self.session_id, *self.labels)
        try:
            response = self.provider.generate(messages, tools)
        except BaseException as exc:
            self.journal.finish(request_id, status='interrupted' if isinstance(exc, (KeyboardInterrupt, SystemExit)) else 'failed')
            raise
        self.journal.finish(request_id, getattr(response, 'usage', None))
        return response


def format_usage(report):
    def tokens(group):
        result = []
        for token_field in TOKEN_FIELDS:
            item = group['tokens'][token_field]
            result.append(f"{token_field}: {item['reported_sum'] if item['reported_sum'] is not None else 'unknown'} "
                          f"({item['responses_with_value']}/{group['responses']} responses)")
        return '; '.join(result)
    total = report['totals']
    lines = [f"Recorded usage: {report['period']} | execution-host timezone: {report['timezone']}"]
    if report['start'] is not None:
        lines.append(f"UTC boundaries: {report['start']} <= call start < {report['end_exclusive']}")
    if not total['calls']:
        lines.append('No usage recorded for this selection.')
    else:
        lines += [f"Model calls: {total['calls']} | responses: {total['responses']} | unfinished: {total['unfinished']} | failed: {total['failed']} | interrupted: {total['interrupted']}", tokens(total)]
        for group in report['groups']:
            lines.append(' / '.join(group[key] for key in DIMENSIONS) + f" | calls: {group['calls']} | " + tokens(group))
    if report['storage_incomplete']:
        lines.append('History persistence is incomplete; totals include available current-process metadata.')
    return '\n'.join(lines)
