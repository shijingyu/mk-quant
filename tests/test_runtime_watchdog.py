from datetime import datetime,timezone
import time
import pytest
from backend import db
from backend.worker import account_heartbeat_expired, stop_unresponsive_account
from test_maker_runtime import maker


def stamp(seconds):return datetime.fromtimestamp(seconds,timezone.utc).isoformat()

@pytest.mark.parametrize('lease,heartbeat,uptime,expired',[
    (0,999,100,False),       # former warmup revocation, but live heartbeat
    (0,900,10,False),        # startup must not inherit old account's timeout
    (1030,900,100,False),    # fresh lease wins even if display heartbeat lags
    (990,900,100,False),     # existing 15-second expiration grace
    (980,950,100,True),      # actual stuck actor is still isolated
    (0,None,100,True),      # never-heartbeating process is not immortal
    (0,'bad timestamp',100,True),
])
def test_watchdog_requires_expiry_stale_heartbeat_and_startup_grace(lease,heartbeat,uptime,expired):
    value=stamp(heartbeat) if isinstance(heartbeat,(int,float)) else heartbeat
    assert account_heartbeat_expired({'lease_until':lease,'last_heartbeat':value},0,now=1000,monotonic_now=uptime)==expired

class Child:
    killed=False
    def poll(self):return None if not self.killed else -9
    def kill(self):self.killed=True
    def wait(self,timeout):assert self.killed;return -9


def test_watchdog_preserves_fresh_handoff_and_persists_real_timeout(maker):
    child=Child();now=time.time();old=time.monotonic()-100
    with db.connect() as con:
        con.execute("UPDATE instances SET desired_state='running',observed_state='running',owner='actor',fence=5,lease_until=0,last_heartbeat=? WHERE account_id=?",(stamp(now),maker['id']))
    assert not stop_unresponsive_account(maker['id'],child,old)
    assert not child.killed
    with db.connect() as con:
        con.execute('UPDATE instances SET last_heartbeat=? WHERE account_id=?',(stamp(now-100),maker['id']))
    assert stop_unresponsive_account(maker['id'],child,old) and child.killed
    with db.connect() as con:
        row=con.execute('SELECT * FROM instances WHERE account_id=?',(maker['id'],)).fetchone()
        assert row['observed_state']=='error' and row['desired_state']=='stopped'
        assert row['owner'] is None and row['fence']==6
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='instance.heartbeat_timeout'").fetchone()[0]==1
        assert con.execute("SELECT COUNT(*) FROM logs WHERE level='error' AND account_id=?",(maker['id'],)).fetchone()[0]==1
