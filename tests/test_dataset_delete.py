"""Reference-aware deletion frees files and preserves inputs, receipts and audit."""
import secrets

from fastapi.testclient import TestClient
import pytest

from backend import db, storage
from backend.app import app, store_experiments
from backend.security import hash_password


@pytest.fixture
def client(mysql_database):
    password = secrets.token_urlsafe(24)
    with db.connect() as con:
        for role in ('admin', 'client'):
            con.execute('INSERT INTO users VALUES(?,?,?,?,?,?,?)',
                        (role, role, role, None, hash_password(password), role, db.now()))
    with TestClient(app) as session:
        login = session.post('/api/login', json={'username': 'admin', 'password': password})
        session.headers['X-MK-CSRF'] = login.json()['csrf_token']
        session.test_password = password
        yield session


def create_dataset(client, name='待删除数据'):
    rows = [{'timestamp': 1767225600000 + i * 60000, 'open': 100, 'high': 102,
             'low': 99, 'close': 101, 'volume': 10} for i in range(3)]
    result = client.post('/api/datasets', json={'name': name, 'market': 'futures',
                         'symbols': ['BTCUSDT'], 'interval': '1m', 'candles': {'BTCUSDT': rows}})
    assert result.status_code == 201, result.text
    return result.json()


def test_delete_requires_admin_and_csrf(client):
    identity = create_dataset(client)['id']
    assert TestClient(app).delete('/api/datasets/' + identity).status_code == 401
    csrf = client.headers.pop('X-MK-CSRF')
    assert client.delete('/api/datasets/' + identity).status_code == 403
    client.headers['X-MK-CSRF'] = csrf
    login = client.post('/api/login', json={'username': 'client', 'password': client.test_password})
    client.headers['X-MK-CSRF'] = login.json()['csrf_token']
    assert client.delete('/api/datasets/' + identity).status_code == 403
    with db.connect() as con:
        assert con.execute('SELECT id FROM datasets WHERE id=?', (identity,)).fetchone()


def test_delete_unreferenced_catalogue_retains_shared_artifact_and_audit(client):
    first, second = create_dataset(client), create_dataset(client, '共享同一份行情')
    with db.connect() as con:
        artifact = db.parse(con.execute('SELECT candles FROM datasets WHERE id=?', (first['id'],)).fetchone()[0])
    expected = storage.read_candles(artifact)
    response = client.delete('/api/datasets/' + first['id'])
    assert response.status_code == 200 and response.json()['shared_files'] == 1
    assert client.delete('/api/datasets/' + first['id']).status_code == 404
    datasets = client.get('/api/datasets').json()
    assert [d['id'] for d in datasets] == [second['id']]
    assert datasets[0]['experiment_count'] == 0
    assert storage.read_candles(artifact) == expected
    with db.connect() as con:
        audit = con.execute("SELECT payload FROM audit_records WHERE action='dataset.delete' AND resource_id=?", (first['id'],)).fetchone()
        assert db.parse(audit['payload'])['hash'] == first['sha256']
    assert client.delete('/api/datasets/' + second['id']).status_code == 200
    assert not (storage.artifact_root() / artifact["path"]).exists()


def test_delete_keeps_download_receipt_without_dangling_dataset(client):
    dataset = create_dataset(client)
    with db.connect() as con:
        con.execute("INSERT INTO data_download_jobs(id,request_hash,requested_by,config,status,total_symbols,dataset_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    ('receipt', '0' * 64, 'admin', '{}', 'completed', 1, dataset['id'], db.now(), db.now()))
    assert client.delete('/api/datasets/' + dataset['id']).status_code == 200
    receipt = client.get('/api/datasets/download-jobs/receipt').json()
    assert receipt['status'] == 'completed' and receipt['dataset_id'] is None


def test_referenced_dataset_cannot_be_deleted_even_after_cancel(client):
    dataset = create_dataset(client)
    with db.connect() as con:
        con.execute('INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,created_at) VALUES(?,?,?,?,?,?,?,?)',
                    ('reference', '保留历史输入', 'momentum', dataset['id'], 'cancelled', '{}', '{}', db.now()))
    listed = client.get('/api/datasets').json()[0]
    assert listed['experiment_count'] == 1
    response = client.delete('/api/datasets/' + dataset['id'])
    assert response.status_code == 409 and '1 个实验引用' in response.json()['detail']
    with db.connect() as con:
        assert con.execute('SELECT id FROM datasets WHERE id=?', (dataset['id'],)).fetchone()
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='dataset.delete'").fetchone()[0] == 0


def test_deleted_dataset_cannot_be_used_by_prepared_experiment(client):
    dataset = create_dataset(client)
    assert client.delete('/api/datasets/' + dataset['id']).status_code == 200
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:
        store_experiments([{'dataset_id': dataset['id']}], actor_id='admin')
    assert error.value.status_code == 409


def test_download_wait_columns_upgrade_is_additive_and_repeatable(mysql_database):
    with db.connect() as con:
        con.execute('ALTER TABLE data_download_jobs DROP COLUMN waiting_reason, DROP COLUMN retry_at')
    db.init_schema()
    db.init_schema()
    with db.connect() as con:
        columns = {row['COLUMN_NAME'] for row in con.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='data_download_jobs'").fetchall()}
        assert {'waiting_reason', 'retry_at'} <= columns
        assert con.execute("SELECT COUNT(*) FROM schema_migrations WHERE version='mysql-003-download-waits'").fetchone()[0] == 1


def create_experiment(client, identity, status="completed", result=None, dataset=None):
    dataset = dataset or create_dataset(client)
    with db.connect() as con:
        con.execute('INSERT INTO experiments(id,name,strategy_id,dataset_id,status,config,provenance,result,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (identity, identity, 'momentum', dataset['id'], status, '{}', '{}', db.dump(result or {}), db.now()))
    return dataset


@pytest.mark.parametrize('status', ['queued', 'running'])
def test_active_experiment_delete_rejected(client, status):
    create_experiment(client, 'active', status)
    assert client.delete('/api/experiments/active').status_code == 409
    with db.connect() as con:
        assert con.execute('SELECT id FROM experiments WHERE id=?', ('active',)).fetchone()


def test_experiment_delete_frees_shared_results_after_last_reference(client):
    result = storage.store_result({'curve': [{'timestamp': 1767225600000, 'equity': 100}],
                                  'validation_curve': [], 'orders': [], 'decisions': []})
    dataset = create_experiment(client, 'first', result=result)
    create_experiment(client, 'second', result=result, dataset=dataset)
    paths = [storage.artifact_root() / m['path'] for m in result['artifacts'].values()]
    assert len(paths) == 4
    assert client.delete('/api/datasets/' + dataset['id']).status_code == 409
    response = client.delete('/api/experiments/first')
    assert response.status_code == 200 and response.json()['shared_files'] == 4
    assert all(path.exists() for path in paths)
    expected_bytes = sum(path.stat().st_size for path in paths)
    response = client.delete('/api/experiments/second')
    assert response.status_code == 200 and response.json()['freed_bytes'] == expected_bytes
    assert response.json()['deleted_files'] == 4
    assert not any(path.exists() for path in paths)
    assert client.delete('/api/experiments/second').status_code == 404
    assert client.get('/api/experiments/second').status_code == 404
    assert client.get('/api/datasets').json()[0]['experiment_count'] == 0
    assert client.delete('/api/datasets/' + dataset['id']).json()['deleted_files'] == 1
    with db.connect() as con:
        assert con.execute("SELECT COUNT(*) FROM audit_records WHERE action='experiment.delete'").fetchone()[0] == 2


def test_experiment_delete_requires_admin_and_csrf(client):
    create_experiment(client, 'private', status='failed')
    assert TestClient(app).delete('/api/experiments/private').status_code == 401
    csrf = client.headers.pop('X-MK-CSRF')
    assert client.delete('/api/experiments/private').status_code == 403
    client.headers['X-MK-CSRF'] = csrf
    login = client.post('/api/login', json={'username': 'client', 'password': client.test_password})
    client.headers['X-MK-CSRF'] = login.json()['csrf_token']
    assert client.delete('/api/experiments/private').status_code == 403


def test_failed_file_cleanup_retried_after_commit(client, monkeypatch):
    from backend import artifact_cleanup
    dataset = create_dataset(client)
    remove = artifact_cleanup._remove
    def denied(manifest):
        raise PermissionError('fixture')
    monkeypatch.setattr(artifact_cleanup, '_remove', denied)
    response = client.delete('/api/datasets/' + dataset['id'])
    assert response.status_code == 200 and response.json()['cleanup_pending']
    assert not client.get('/api/datasets').json()
    monkeypatch.setattr(artifact_cleanup, '_remove', remove)
    reports = artifact_cleanup.drain()
    assert len(reports) == 1
    assert next(iter(reports.values()))['deleted_files'] == 1
    assert artifact_cleanup.drain() == {}


def test_delete_transaction_rollback_never_unlinks(client, monkeypatch):
    from backend import artifact_cleanup
    from fastapi import HTTPException
    dataset = create_dataset(client)
    def failure(*args):
        raise HTTPException(503, 'fixture')
    monkeypatch.setattr(artifact_cleanup, 'enqueue', failure)
    assert client.delete('/api/datasets/' + dataset['id']).status_code == 503
    with db.connect() as con:
        manifest = db.parse(con.execute('SELECT candles FROM datasets WHERE id=?', (dataset['id'],)).fetchone()[0])
    assert storage.read_candles(manifest)


def test_cleanup_rechecks_new_reference_before_retry(client, monkeypatch):
    from backend import artifact_cleanup
    first = create_dataset(client)
    remove = artifact_cleanup._remove
    monkeypatch.setattr(artifact_cleanup, '_remove', lambda m: (_ for _ in ()).throw(PermissionError()))
    assert client.delete('/api/datasets/' + first['id']).json()['cleanup_pending']
    second = create_dataset(client)
    monkeypatch.setattr(artifact_cleanup, '_remove', remove)
    reports = artifact_cleanup.drain()
    assert next(iter(reports.values()))['shared_files'] == 1
    assert client.delete('/api/datasets/' + second['id']).json()['deleted_files'] == 1
