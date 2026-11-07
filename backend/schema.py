"""Version 1 schema. MySQL 5.7+ syntax, explicit InnoDB and binary UTF-8 keys."""

TABLES = (
    "users", "strategies", "strategy_versions", "sessions", "accounts", "instances",
    "commands", "datasets", "experiments", "presets", "logs", "ledger", "runtime_events",
    "services", "market_cache", "orders", "fills", "audit_records", "binance_order_intents",
    "schema_migrations", "data_download_jobs", "strategy_archives", "account_execution_settings",
)

_TABLES = {
    "strategy_archives": """
        strategy_id VARCHAR(64) PRIMARY KEY, deleted_by VARCHAR(64) NOT NULL,
        deleted_at VARCHAR(40) NOT NULL,
        CONSTRAINT fk_archived_strategy FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    """,
    "account_execution_settings": """
        account_id VARCHAR(64) PRIMARY KEY, leverage INT NOT NULL, updated_at VARCHAR(40) NOT NULL,
        CONSTRAINT fk_execution_settings_account FOREIGN KEY(account_id) REFERENCES accounts(id)
    """,
    "users": """
        id VARCHAR(64) PRIMARY KEY, username VARCHAR(120) NOT NULL UNIQUE,
        name VARCHAR(255) NOT NULL, phone VARCHAR(40), password VARCHAR(255) NOT NULL,
        role ENUM('admin','client') NOT NULL, created_at VARCHAR(40) NOT NULL
    """,
    "strategies": """
        id VARCHAR(64) PRIMARY KEY, manifest LONGTEXT NOT NULL, source LONGTEXT NOT NULL,
        source_hash CHAR(64) NOT NULL, trusted TINYINT NOT NULL DEFAULT 0,
        created_at VARCHAR(40) NOT NULL
    """,
    "strategy_versions": """
        id CHAR(64) PRIMARY KEY, strategy_id VARCHAR(64) NOT NULL, version VARCHAR(191) NOT NULL,
        manifest LONGTEXT NOT NULL, source LONGTEXT NOT NULL, source_hash CHAR(64) NOT NULL,
        created_at VARCHAR(40) NOT NULL, UNIQUE KEY strategy_source_once(strategy_id,source_hash),
        CONSTRAINT fk_strategy_version_strategy FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    """,
    "sessions": """
        token_hash CHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL,
        csrf VARCHAR(128) NOT NULL, expires DOUBLE NOT NULL, KEY session_expiry(expires),
        CONSTRAINT fk_session_user FOREIGN KEY(user_id) REFERENCES users(id)
    """,
    "accounts": """
        id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, name VARCHAR(255) NOT NULL,
        mode ENUM('paper','live') NOT NULL, route CHAR(32) NOT NULL UNIQUE,
        strategy_id VARCHAR(64) NOT NULL, config LONGTEXT NOT NULL, symbols LONGTEXT NOT NULL,
        initial_equity DOUBLE NOT NULL, metrics LONGTEXT NOT NULL, credentials LONGTEXT,
        external_uid VARCHAR(191) UNIQUE, check_result LONGTEXT,
        source VARCHAR(32) NOT NULL DEFAULT 'local', created_at VARCHAR(40) NOT NULL,
        CONSTRAINT fk_account_user FOREIGN KEY(user_id) REFERENCES users(id),
        CONSTRAINT fk_account_strategy FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    """,
    "instances": """
        account_id VARCHAR(64) PRIMARY KEY, desired_state VARCHAR(32) NOT NULL DEFAULT 'stopped',
        observed_state VARCHAR(32) NOT NULL DEFAULT 'stopped', owner VARCHAR(128),
        fence BIGINT NOT NULL DEFAULT 0, lease_until DOUBLE NOT NULL DEFAULT 0,
        last_heartbeat VARCHAR(40), error TEXT, config_revision BIGINT NOT NULL DEFAULT 1,
        CONSTRAINT fk_instance_account FOREIGN KEY(account_id) REFERENCES accounts(id)
    """,
    "commands": """
        id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) NOT NULL, action VARCHAR(32) NOT NULL,
        status VARCHAR(32) NOT NULL DEFAULT 'pending', requested_by VARCHAR(64) NOT NULL,
        created_at VARCHAR(40) NOT NULL, completed_at VARCHAR(40), error TEXT, result LONGTEXT,
        KEY command_account_status(account_id,status,created_at),
        CONSTRAINT fk_command_account FOREIGN KEY(account_id) REFERENCES accounts(id),
        CONSTRAINT fk_command_user FOREIGN KEY(requested_by) REFERENCES users(id)
    """,
    "datasets": """
        id VARCHAR(64) PRIMARY KEY, name VARCHAR(255) NOT NULL, market VARCHAR(32) NOT NULL,
        `interval` VARCHAR(12) NOT NULL, symbols LONGTEXT NOT NULL, candles LONGTEXT NOT NULL,
        sha256 CHAR(64) NOT NULL, source VARCHAR(64) NOT NULL, created_at VARCHAR(40) NOT NULL
    """,
    "experiments": """
        id VARCHAR(64) PRIMARY KEY, name VARCHAR(255) NOT NULL, strategy_id VARCHAR(64) NOT NULL,
        dataset_id VARCHAR(64) NOT NULL, status VARCHAR(32) NOT NULL,
        progress DOUBLE NOT NULL DEFAULT 0, config LONGTEXT NOT NULL, provenance LONGTEXT NOT NULL,
        result LONGTEXT, error TEXT, cancel_requested TINYINT NOT NULL DEFAULT 0, worker_pid BIGINT,
        created_at VARCHAR(40) NOT NULL, started_at VARCHAR(40), completed_at VARCHAR(40),
        KEY experiment_queue(status,cancel_requested,created_at),
        CONSTRAINT fk_experiment_strategy FOREIGN KEY(strategy_id) REFERENCES strategies(id),
        CONSTRAINT fk_experiment_dataset FOREIGN KEY(dataset_id) REFERENCES datasets(id)
    """,
    "presets": """
        id VARCHAR(64) PRIMARY KEY, strategy_id VARCHAR(64) NOT NULL, name VARCHAR(255) NOT NULL,
        parameters LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL, backtest_config LONGTEXT NULL,
        CONSTRAINT fk_preset_strategy FOREIGN KEY(strategy_id) REFERENCES strategies(id)
    """,
    "logs": """
        id BIGINT PRIMARY KEY AUTO_INCREMENT, level VARCHAR(32) NOT NULL, strategy_id VARCHAR(64),
        account_id VARCHAR(64), message TEXT NOT NULL, details LONGTEXT,
        source VARCHAR(32) NOT NULL DEFAULT 'system', created_at VARCHAR(40) NOT NULL,
        KEY log_filter(level,strategy_id,created_at)
    """,
    "ledger": """
        id VARCHAR(64) PRIMARY KEY, account_id VARCHAR(64) NOT NULL, kind VARCHAR(32) NOT NULL,
        `reference` VARCHAR(191) UNIQUE, payload LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
        KEY ledger_account_kind(account_id,kind,created_at),
        CONSTRAINT fk_ledger_account FOREIGN KEY(account_id) REFERENCES accounts(id)
    """,
    "runtime_events": """
        id BIGINT PRIMARY KEY AUTO_INCREMENT, account_id VARCHAR(64) NOT NULL, fence BIGINT NOT NULL,
        event VARCHAR(64) NOT NULL, payload LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
        KEY runtime_account(account_id,created_at),
        CONSTRAINT fk_runtime_account FOREIGN KEY(account_id) REFERENCES accounts(id)
    """,
    "services": """
        name VARCHAR(64) PRIMARY KEY, owner VARCHAR(128) NOT NULL,
        heartbeat VARCHAR(40) NOT NULL, status VARCHAR(32) NOT NULL
    """,
    "market_cache": """
        market VARCHAR(32) NOT NULL, symbol VARCHAR(32) NOT NULL, `interval` VARCHAR(12) NOT NULL,
        candles LONGTEXT NOT NULL, filters LONGTEXT NOT NULL, fetched_at DOUBLE NOT NULL,
        PRIMARY KEY(market,symbol,`interval`)
    """,
    "orders": """
        id CHAR(64) PRIMARY KEY, account_id VARCHAR(64) NOT NULL, external_id VARCHAR(128),
        client_order_id VARCHAR(128) NOT NULL, symbol VARCHAR(32) NOT NULL,
        side VARCHAR(16) NOT NULL, status VARCHAR(32) NOT NULL, payload LONGTEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
        UNIQUE KEY order_client_once(account_id,client_order_id),
        KEY order_account_status(account_id,status,created_at),
        CONSTRAINT fk_order_account FOREIGN KEY(account_id) REFERENCES accounts(id)
    """,
    "fills": """
        id CHAR(64) PRIMARY KEY, account_id VARCHAR(64) NOT NULL, order_id CHAR(64) NOT NULL,
        external_id VARCHAR(128) NOT NULL, symbol VARCHAR(32) NOT NULL,
        quantity DECIMAL(38,18) NOT NULL, price DECIMAL(38,18) NOT NULL,
        fee DECIMAL(38,18) NOT NULL, realized_pnl DECIMAL(38,18) NOT NULL,
        payload LONGTEXT NOT NULL, created_at VARCHAR(40) NOT NULL,
        UNIQUE KEY fill_external_once(account_id,external_id),
        KEY fill_account_time(account_id,created_at),
        CONSTRAINT fk_fill_account FOREIGN KEY(account_id) REFERENCES accounts(id),
        CONSTRAINT fk_fill_order FOREIGN KEY(order_id) REFERENCES orders(id)
    """,
    "audit_records": """
        id BIGINT PRIMARY KEY AUTO_INCREMENT, actor_id VARCHAR(64), action VARCHAR(64) NOT NULL,
        resource_type VARCHAR(64) NOT NULL, resource_id VARCHAR(128), payload LONGTEXT NOT NULL,
        created_at VARCHAR(40) NOT NULL, KEY audit_actor_time(actor_id,created_at),
        KEY audit_resource(resource_type,resource_id,created_at)
    """,
    "binance_order_intents": """
        account VARCHAR(191) NOT NULL, client_id VARCHAR(128) NOT NULL, payload LONGTEXT NOT NULL,
        status VARCHAR(32) NOT NULL, result LONGTEXT, updated_at DOUBLE NOT NULL,
        PRIMARY KEY(account,client_id)
    """,
    "schema_migrations": """
        version VARCHAR(64) PRIMARY KEY, applied_at VARCHAR(40) NOT NULL
    """,
    "data_download_jobs": """
        id VARCHAR(64) PRIMARY KEY, request_key VARCHAR(128) UNIQUE,
        request_hash CHAR(64) NOT NULL, requested_by VARCHAR(64) NOT NULL,
        config LONGTEXT NOT NULL, status VARCHAR(32) NOT NULL DEFAULT 'queued',
        active_slot VARCHAR(32) UNIQUE, progress DOUBLE NOT NULL DEFAULT 0,
        current_symbol VARCHAR(32), downloaded_bars BIGINT NOT NULL DEFAULT 0,
        completed_symbols INT NOT NULL DEFAULT 0, total_symbols INT NOT NULL,
        attempts INT NOT NULL DEFAULT 0, owner VARCHAR(128), lease_until DOUBLE NOT NULL DEFAULT 0,
        worker_pid BIGINT, dataset_id VARCHAR(64), error TEXT, waiting_reason TEXT, retry_at DOUBLE,
        created_at VARCHAR(40) NOT NULL, started_at VARCHAR(40), updated_at VARCHAR(40) NOT NULL,
        completed_at VARCHAR(40), KEY download_status_created(status,created_at),
        CONSTRAINT fk_download_user FOREIGN KEY(requested_by) REFERENCES users(id),
        CONSTRAINT fk_download_dataset FOREIGN KEY(dataset_id) REFERENCES datasets(id)
    """,
}

SCHEMA = tuple(
    f"CREATE TABLE IF NOT EXISTS `{name}` ({_TABLES[name]}) "
    "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin ROW_FORMAT=DYNAMIC"
    for name in TABLES
)
