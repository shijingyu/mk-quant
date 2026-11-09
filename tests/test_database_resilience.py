"""A temporary database outage must not become a permanent worker shutdown."""
from contextlib import contextmanager

import pytest
from pymysql.err import OperationalError, InterfaceError, ProgrammingError

from backend import db, worker
from test_maker_runtime import maker, feed, command, read, start


@pytest.mark.parametrize('error,expected',[
    (OperationalError(2006, 'gone'), True),
    (OperationalError(2013, 'lost commit acknowledgement'), True),
    (OperationalError(2003, 'refused'), True),
    (OperationalError(1205, 'lock timeout'), True),
    (OperationalError(1213, 'deadlock'), True),
    (InterfaceError(0, ''), True),
    (OperationalError(1045, 'bad credentials'), False),
    (OperationalError(1026, 'disk full'), False),
    (ProgrammingError(1146, 'missing table'), False),
    (ValueError('strategy failed'), False),
])
def test_only_known_transient_database_errors_are_retried(error, expected):
    assert worker.transient_database_error(error) == expected


def test_retry_backoff_is_bounded_and_does_not_retry_one_shot(monkeypatch):
    delays = []
    monkeypatch.setattr(worker.time, 'sleep', delays.append)
    calls = 0
    def operation():
        nonlocal calls
        calls += 1
        if calls < 8:
            raise OperationalError(2013, 'lost connection')
        return 'reloaded'
    assert worker.retry_database(operation, 'test') == 'reloaded'
    assert delays == [1, 2, 4, 8, 15, 15, 15]
    calls = 0
    with pytest.raises(OperationalError):
        worker.retry_database(operation, 'test', once=True)
    assert calls == 1


class StopTest(BaseException):
    pass


def test_runtime_keeps_children_and_resets_timeout_grace_after_db_recovery(monkeypatch):
    clock = {'now': 0}
    monkeypatch.setattr(worker.time, 'monotonic', lambda: clock['now'])
    monkeypatch.setattr(worker.time, 'sleep', lambda delay: clock.update(now=clock['now'] + delay))
    children = []
    class Child:
        terminated = False
        def poll(self): return None
        def terminate(self): self.terminated = True
    def spawn(*args, **kwargs):
        child = Child(); children.append(child); return child
    monkeypatch.setattr(worker.subprocess, 'Popen', spawn)
    class Connection:
        def execute(self, *args): return self
        def fetchall(self): return [{'account_id': 'account'}]
    @contextmanager
    def connect(): yield Connection()
    monkeypatch.setattr(db, 'connect', connect)
    checks = []
    monkeypatch.setattr(worker, 'stop_unresponsive_account', lambda aid, child, started: checks.append((started, clock['now'])))
    calls = 0
    def service(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            clock['now'] += 120
            raise OperationalError(2006, 'MySQL restarted')
        if calls == 3:
            assert len(children) == 3 and not any(c.terminated for c in children)
        if calls == 4: raise StopTest()
    monkeypatch.setattr(worker, 'service', service)
    with pytest.raises(StopTest): worker.runtime_loop()
    assert len(children) == 3   # same feed, warmer and account; no duplicate actor
    assert checks and checks[0][0] == checks[0][1]  # grace reset before watchdog
    assert all(c.terminated for c in children)  # actual shutdown still cleans up


@pytest.mark.parametrize('role', ['market', 'backtest'])
def test_coordinator_retries_transient_failure_but_propagates_bug(monkeypatch, role):
    calls = 0
    def iteration(once):
        nonlocal calls
        calls += 1
        if calls == 1: raise OperationalError(2006, 'MySQL unavailable')
        raise ValueError('non-database programming failure')
    monkeypatch.setattr(worker, '_'+role+'_loop', iteration)
    monkeypatch.setattr(worker.time, 'sleep', lambda delay: None)
    with pytest.raises(ValueError): getattr(worker, role+'_loop')()
    assert calls == 2


def test_account_reloads_ambiguous_committed_fill_without_duplicate_orders(maker, monkeypatch):
    start(maker)
    feed(maker, 1, high=104, low=96)
    with db.connect() as con:
        con.execute('UPDATE instances SET lease_until=0 WHERE account_id=?', (maker['id'],))
    original = worker.runtime_tick
    calls = 0
    committed = None
    def lost_acknowledgement(account_id, owner):
        nonlocal calls, committed
        calls += 1
        original(account_id, owner)
        if calls == 1:
            committed = read(maker)
            assert len(committed['fills']) == 2
            raise OperationalError(2013, 'commit succeeded but acknowledgement was lost')
        return False
    monkeypatch.setattr(worker, 'runtime_tick', lost_acknowledgement)
    monkeypatch.setattr(worker.time, 'sleep', lambda delay: None)
    worker.account_loop(maker['id'])
    after = read(maker)
    assert calls == 2
    assert after['fills'] == committed['fills']
    assert after['orders'] == committed['orders']
    assert after['snapshot']['book'] == committed['snapshot']['book']
    assert after['instance']['observed_state'] == 'running'
    assert after['instance']['error'] is None


def test_real_strategy_error_stops_account_without_retry(maker, monkeypatch):
    calls = 0
    def failed_strategy(account_id, owner):
        nonlocal calls
        calls += 1
        db.acquire_lease(account_id, owner)
        raise ValueError('invalid strategy logic')
    monkeypatch.setattr(worker, 'runtime_tick', failed_strategy)
    worker.account_loop(maker['id'])
    row = read(maker)['instance']
    assert calls == 1
    assert row['desired_state'] == 'stopped' and row['observed_state'] == 'error'
    assert row['error'] == 'invalid strategy logic'


def test_backtest_disconnect_reaps_child_then_reconciles_interrupted_job(monkeypatch):
    """A coordinator retry must not create another child for ambiguous work."""
    from datetime import datetime, timezone
    state = {'claimed': False, 'failed': False, 'services': 0}
    children = []
    class Child:
        ended = False
        def poll(self): return 0 if self.ended else None
        def terminate(self): self.ended = True
        def wait(self, timeout=None): assert self.ended; return 0
    def spawn(*args, **kwargs):
        child = Child(); children.append(child); return child
    monkeypatch.setattr(worker.subprocess, 'Popen', spawn)
    def service(*args):
        state['services'] += 1
        if state['services'] == 2:
            assert len(children) == 1 and children[0].ended
    monkeypatch.setattr(worker, 'service', service)
    monkeypatch.setattr(worker.downloads, 'claim', lambda owner: (None, False))
    def monitor(*args): raise OperationalError(2006, 'coordinator lost DB')
    monkeypatch.setattr(worker, '_monitor_job', monitor)
    def no_process(*args): raise ProcessLookupError()
    monkeypatch.setattr(worker.os, 'kill', no_process)
    class Result:
        def __init__(self, value): self.value = value
        def fetchone(self): return self.value
        def fetchall(self): return self.value
    class Connection:
        def execute(self, query, params=None):
            if query.startswith('SELECT id,worker_pid'):
                return Result([{'id': 'job', 'worker_pid': 100,
                                'started_at': datetime.fromtimestamp(worker.time.time()-120, timezone.utc).isoformat()}]
                              if state['claimed'] and not state['failed'] else [])
            if query.startswith("UPDATE experiments SET status='failed'"):
                assert children[0].ended
                state['failed'] = True
            if query.startswith("SELECT id FROM experiments WHERE status='running'"):
                return Result({'id': 'job'} if state['claimed'] and not state['failed'] else None)
            if query.startswith("SELECT id FROM experiments WHERE status='queued'"):
                return Result(None if state['claimed'] else {'id': 'job'})
            if query.startswith("UPDATE experiments SET status='running'"):
                state['claimed'] = True
            return Result(None)
    @contextmanager
    def connect(): yield Connection()
    monkeypatch.setattr(db, 'connect', connect)
    def sleep(delay):
        if delay == .8: raise StopTest()
    monkeypatch.setattr(worker.time, 'sleep', sleep)
    with pytest.raises(StopTest): worker.backtest_loop()
    assert len(children) == 1 and children[0].ended
    assert state['failed'] and state['services'] == 2


def test_strategy_failure_survives_database_outage_without_reexecuting(maker, monkeypatch):
    """Keep the original failure in memory; retry its recording, not strategy."""
    original_connect = db.connect
    state = {'calls': 0, 'fail_record': True}
    def failed_strategy(account_id, owner):
        state['calls'] += 1
        db.acquire_lease(account_id, owner)
        # Simulate a prolonged database outage: ownership remains ours but its
        # expiry passes before error persistence can successfully reconnect.
        maker['clock']['now'] += 120
        @contextmanager
        def reconnect():
            if state['fail_record']:
                state['fail_record'] = False
                raise OperationalError(2003, 'database temporarily down')
            with original_connect() as con: yield con
        monkeypatch.setattr(db, 'connect', reconnect)
        raise ValueError('strategy logic failed before DB outage')
    monkeypatch.setattr(worker, 'runtime_tick', failed_strategy)
    monkeypatch.setattr(worker.time, 'sleep', lambda delay: None)
    worker.account_loop(maker['id'])
    after = read(maker)['instance']
    assert state['calls'] == 1 and not state['fail_record']
    assert after['desired_state'] == 'stopped' and after['observed_state'] == 'error'
    assert after['error'] == 'strategy logic failed before DB outage'
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='instance.fail'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM logs WHERE account_id=? AND level='error'", (maker['id'],)).fetchone()[0] == 1


def test_strategy_failure_does_not_stop_replacement_owner(maker, monkeypatch):
    def replaced_strategy(account_id, owner):
        db.acquire_lease(account_id, owner)
        with db.connect() as con:
            con.execute("UPDATE instances SET owner='replacement',fence=fence+1,desired_state='running',observed_state='running',error=NULL WHERE account_id=?", (account_id,))
        raise ValueError('old actor failed')
    monkeypatch.setattr(worker, 'runtime_tick', replaced_strategy)
    worker.account_loop(maker['id'])
    row = read(maker)['instance']
    assert row['owner'] == 'replacement'
    assert row['desired_state'] == 'running' and row['observed_state'] == 'running'
    assert row['error'] is None


def test_failure_commit_ack_loss_records_only_one_audit_and_log(maker, monkeypatch):
    original_connect = db.connect
    calls = 0
    def failed_strategy(account_id, owner):
        nonlocal calls
        calls += 1
        db.acquire_lease(account_id, owner)
        lost_ack = {'pending': True}
        @contextmanager
        def connection():
            with original_connect() as con: yield con
            if lost_ack['pending']:
                lost_ack['pending'] = False
                raise OperationalError(2013, 'failure transaction committed; ack lost')
        monkeypatch.setattr(db, 'connect', connection)
        raise ValueError('strategy error')
    monkeypatch.setattr(worker, 'runtime_tick', failed_strategy)
    monkeypatch.setattr(worker.time, 'sleep', lambda delay: None)
    worker.account_loop(maker['id'])
    assert calls == 1
    assert read(maker)['instance']['desired_state'] == 'stopped'
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='instance.fail'").fetchone()[0] == 1
        assert con.execute("SELECT COUNT(*) FROM logs WHERE account_id=? AND level='error'", (maker['id'],)).fetchone()[0] == 1
