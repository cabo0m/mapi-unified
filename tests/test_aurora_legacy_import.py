from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.db_migrations import apply_all_migrations, apply_migrations_through, applied_migration_versions
from mapi.aurora_import import apply_aurora_import, preview_aurora_import


def _legacy_aurora_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL);
            INSERT INTO schema_migrations VALUES ('0001_public_memory_core','2026-01-01T00:00:00Z');
            INSERT INTO schema_migrations VALUES ('0022_remote_auth_dynamic_clients','2026-01-02T00:00:00Z');
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT, content TEXT NOT NULL, summary_short TEXT, title TEXT,
                memory_type TEXT NOT NULL DEFAULT 'project_note', entry_type TEXT, truth_kind TEXT, project_key TEXT,
                scope_code TEXT, state_code TEXT NOT NULL DEFAULT 'validated', memory_v2_status TEXT NOT NULL DEFAULT 'active',
                importance_score REAL NOT NULL DEFAULT 0.5, confidence_score REAL NOT NULL DEFAULT 0.5, tags TEXT,
                source TEXT, source_context TEXT, source_event_ref TEXT, conversation_key TEXT,
                sensitivity_class TEXT NOT NULL DEFAULT 'internal', input_fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT, archived_at TEXT,
                version INTEGER NOT NULL DEFAULT 1, supersedes_memory_id INTEGER, superseded_by_memory_id INTEGER,
                layer_code TEXT NOT NULL DEFAULT 'buffer', importance_level TEXT, priority TEXT NOT NULL DEFAULT 'normal',
                review_due_at TEXT, revalidation_due_at TEXT, expired_due_at TEXT, last_validated_at TEXT,
                validation_source TEXT, agent_key TEXT
            );
            CREATE TABLE memory_events(id INTEGER PRIMARY KEY AUTOINCREMENT,memory_id INTEGER NOT NULL,event_type TEXT NOT NULL,payload_json TEXT,created_at TEXT NOT NULL);
            CREATE TABLE memory_links(
                id INTEGER PRIMARY KEY AUTOINCREMENT,from_memory_id INTEGER NOT NULL,to_memory_id INTEGER NOT NULL,
                relation_type TEXT NOT NULL,weight REAL NOT NULL DEFAULT 1.0,evidence_kind TEXT,evidence_ref TEXT,reason TEXT,
                origin TEXT,applied_by TEXT,preview_hash TEXT,created_at TEXT NOT NULL,archived_at TEXT
            );
            CREATE TABLE conversation_archives(
                id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL UNIQUE,title TEXT,source TEXT NOT NULL DEFAULT 'manual',
                content TEXT NOT NULL,project_key TEXT NOT NULL,tags TEXT,word_count INTEGER NOT NULL DEFAULT 0,
                sensitivity_class TEXT NOT NULL DEFAULT 'internal',created_at TEXT NOT NULL,archived_at TEXT NOT NULL
            );
            CREATE TABLE timeline_events(
                id INTEGER PRIMARY KEY AUTOINCREMENT,event_time TEXT NOT NULL,event_type TEXT NOT NULL,memory_id INTEGER,
                related_memory_id INTEGER,operation_id TEXT,timeline_scope TEXT NOT NULL DEFAULT 'system',semantic_kind TEXT NOT NULL DEFAULT 'runtime_event',
                title TEXT,project_key TEXT,valid_at TEXT,source_table TEXT,source_row_id INTEGER,origin TEXT,
                reconstructed INTEGER NOT NULL DEFAULT 0,payload_json TEXT,created_at TEXT NOT NULL
            );
            CREATE TABLE aurora_onboarding(
                id INTEGER PRIMARY KEY CHECK(id=1),schema_version INTEGER NOT NULL,status TEXT NOT NULL,current_step TEXT,
                answers_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,completed_at TEXT,skipped_at TEXT,skip_reason TEXT
            );
            CREATE TABLE memory_proposals(id INTEGER PRIMARY KEY,content TEXT,project_key TEXT,status TEXT);
            CREATE TABLE file_operations(id INTEGER PRIMARY KEY,operation_key TEXT,backup_path TEXT,status TEXT);
            CREATE TABLE memory_embeddings(memory_id INTEGER PRIMARY KEY,embedding_blob BLOB);
            CREATE TABLE remote_auth_tokens(token_hash TEXT PRIMARY KEY,token_kind TEXT);
            """
        )
        conn.executemany(
            """
            INSERT INTO memories(
                id,content,summary_short,title,memory_type,entry_type,truth_kind,project_key,scope_code,state_code,memory_v2_status,
                importance_score,confidence_score,tags,source,source_context,source_event_ref,conversation_key,sensitivity_class,
                input_fingerprint,created_at,updated_at,version,supersedes_memory_id,superseded_by_memory_id,layer_code,priority,agent_key
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (10,'first durable memory','first','First','project_note','project','fact','alpha','project','validated','active',0.8,0.9,'alpha','user',None,'aurora:test:10',None,'internal','fp10','2026-01-01','2026-01-01',1,None,20,'projects','high',None),
                (20,'newer durable memory','newer','Newer','project_note','decision','decision','alpha','project','validated','active',0.9,0.95,'alpha','user',None,'aurora:test:20',None,'personal','fp20','2026-01-02','2026-01-02',1,10,None,'projects','high',None),
                (30,'medical context','medical','Medical','project_note','raw_note','fact','health','project','validated','active',0.7,0.9,'health,medical','user',None,'aurora:test:30',None,'health_sensitive','fp30','2026-01-03','2026-01-03',1,None,None,'projects','normal',None),
                (40,'credential material for migration test','secret','Secret','project_note','raw_note','fact','secret','project','validated','active',0.5,0.9,'private','user',None,'aurora:test:40',None,'credential_secret','fp40','2026-01-04','2026-01-04',1,None,None,'buffer','normal',None),
                (50,'Astra is the assistant name chosen by the user.','Assistant name: Astra','Astra','identity','user_profile','fact','agent-self','project','validated','active',1.0,1.0,'identity,onboarding','aurora-onboarding',None,'aurora-onboarding:v2:agent_name',None,'internal','fp50','2026-01-05','2026-01-05',1,None,None,'identity','high',None),
            ],
        )
        conn.execute("INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES (20,'created','{\"legacy_id\":20}','2026-01-02')")
        conn.execute("INSERT INTO memory_events(memory_id,event_type,payload_json,created_at) VALUES (30,'created','{}','2026-01-03')")
        conn.execute("INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight,evidence_kind,evidence_ref,reason,origin,created_at) VALUES (20,10,'supersedes',1.0,'memory','10','newer','aurora','2026-01-02')")
        conn.execute("INSERT INTO memory_links(from_memory_id,to_memory_id,relation_type,weight,origin,created_at) VALUES (20,30,'related_to',0.5,'aurora','2026-01-03')")
        conn.execute("INSERT INTO conversation_archives(conversation_id,title,source,content,project_key,tags,word_count,sensitivity_class,created_at,archived_at) VALUES ('conv-1','Chat','manual','hello world','alpha','chat',2,'internal','2026-01-01','2026-01-01')")
        conn.execute("INSERT INTO timeline_events(event_time,event_type,memory_id,related_memory_id,title,project_key,created_at) VALUES ('2026-01-02','decision',20,10,'Decision','alpha','2026-01-02')")
        conn.execute("INSERT INTO aurora_onboarding VALUES (1,2,'completed',NULL,?, '2026-01-01','2026-01-05','2026-01-05',NULL,NULL)", (json.dumps({'agent_name':'Astra','user_name':'Michał','autonomy_level':'collaborative','memory_policy':'automatic_important'}),))
        conn.execute("INSERT INTO memory_proposals VALUES (1,'legacy proposal','alpha','pending')")
        conn.execute("INSERT INTO file_operations VALUES (1,'op-1','legacy/backup','applied')")
        conn.execute("INSERT INTO memory_embeddings VALUES (10,X'0102')")
        conn.execute("INSERT INTO remote_auth_tokens VALUES ('hashed-secret','access')")
        conn.commit()
    finally:
        conn.close()


def _fresh_unified_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        apply_all_migrations(conn)
        conn.execute(
            "INSERT INTO memories(content,summary_short,memory_type,source,tags,project_key,scope_code,source_event_ref) VALUES (?,?,?,?,?,?,?,?)",
            ('Agent is the configured agent identity for this MAPI instance.','Agent identity: Agent','identity','mapi-init','bootstrap,agent-self','agent-self','project','mapi-init:agent:identity'),
        )
        conn.execute(
            "INSERT INTO memories(content,summary_short,memory_type,source,tags,project_key,scope_code,source_event_ref) VALUES (?,?,?,?,?,?,?,?)",
            ('Self evidence stays separate.','Self namespace guardrail','guardrail','mapi-init','bootstrap,guardrail','agent-self','project','mapi-init:agent:namespace-guardrail'),
        )
        conn.commit()
    finally:
        conn.close()


def test_preview_apply_remap_quarantine_and_idempotency(tmp_path: Path) -> None:
    source = tmp_path / 'aurora.db'
    target = tmp_path / 'unified.db'
    _legacy_aurora_db(source)
    _fresh_unified_db(target)

    preview = preview_aurora_import(source_db=source, target_db=target)
    assert preview['status'] == 'preview_ready'
    assert preview['counts']['memories_total'] == 5
    assert preview['counts']['memories_active_import'] == 3
    assert preview['counts']['memories_quarantine_or_omit'] == 2
    assert 'remote_auth_credentials_must_be_reissued' in preview['warnings']

    stale = apply_aurora_import(source_db=source, target_db=target, expected_preview_hash='wrong')
    assert stale['status'] == 'stale_preview'

    applied = apply_aurora_import(source_db=source, target_db=target, expected_preview_hash=preview['preview_hash'])
    assert applied['status'] == 'completed'
    assert Path(applied['backup_path']).is_file()
    result = applied['result']
    assert result['memories_imported'] == 3
    assert result['memories_quarantined'] == 1
    assert result['memories_secret_omitted'] == 1
    assert result['memory_events_imported'] == 1
    assert result['memory_events_archived'] == 1
    assert result['memory_links_imported'] == 1
    assert result['memory_links_archived'] == 1
    assert result['onboarding'] == 'translated'
    assert result['bootstrap_identity_archived'] == 1

    conn = sqlite3.connect(target)
    conn.row_factory = sqlite3.Row
    try:
        active = conn.execute("SELECT id,content,supersedes_memory_id,superseded_by_memory_id FROM memories WHERE source <> 'mapi-init' ORDER BY id").fetchall()
        assert [row['content'] for row in active] == [
            'first durable memory', 'newer durable memory', 'Astra is the assistant name chosen by the user.'
        ]
        by_content = {row['content']: row for row in active}
        first = by_content['first durable memory']
        newer = by_content['newer durable memory']
        assert newer['supersedes_memory_id'] == first['id']
        assert first['superseded_by_memory_id'] == newer['id']
        seed = conn.execute("SELECT archived_at,state_code FROM memories WHERE source_event_ref='mapi-init:agent:identity'").fetchone()
        assert seed['archived_at'] is not None
        assert seed['state_code'] == 'archived'
        onboarding = conn.execute("SELECT status,answers_json FROM polaris_onboarding WHERE id=1").fetchone()
        assert onboarding['status'] == 'completed'
        assert json.loads(onboarding['answers_json'])['agent_name'] == 'Astra'
        assert conn.execute("SELECT COUNT(*) FROM remote_auth_tokens").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM file_operations").fetchone()[0] == 0
        archive_rows = conn.execute("SELECT source_table,sensitivity_class,redacted,payload_json FROM legacy_aurora_import_archive").fetchall()
        assert any(row['source_table'] == 'memory_proposals' for row in archive_rows)
        assert any(row['source_table'] == 'file_operations' for row in archive_rows)
        health = next(row for row in archive_rows if row['sensitivity_class'] == 'health_sensitive')
        assert 'medical context' in health['payload_json']
        secret = next(row for row in archive_rows if row['sensitivity_class'] == 'credential_secret')
        assert secret['redacted'] == 1
        assert 'credential material for migration test' not in secret['payload_json']
        assert 'content_sha256' in secret['payload_json']
    finally:
        conn.close()

    repeated = apply_aurora_import(source_db=source, target_db=target, expected_preview_hash=preview['preview_hash'])
    assert repeated['status'] == 'already_imported'


def test_preview_blocks_nonfresh_target(tmp_path: Path) -> None:
    source = tmp_path / 'aurora.db'
    target = tmp_path / 'unified.db'
    _legacy_aurora_db(source)
    _fresh_unified_db(target)
    conn = sqlite3.connect(target)
    try:
        conn.execute("INSERT INTO memories(content,memory_type,source) VALUES ('user data','project_note','user')")
        conn.commit()
    finally:
        conn.close()
    preview = preview_aurora_import(source_db=source, target_db=target)
    assert preview['status'] == 'blocked'
    assert 'target_not_fresh' in preview['errors']
    assert preview['mutations_performed'] == 0


def test_polaris_database_upgrades_in_place_without_losing_memory(tmp_path: Path) -> None:
    db = tmp_path / 'polaris.db'
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        apply_migrations_through(conn, '0036_memory_self_healing')
        conn.execute("INSERT INTO memories(content,memory_type,source) VALUES ('keep me','project_note','polaris-test')")
        conn.commit()
        before = conn.execute("SELECT id,content FROM memories WHERE source='polaris-test'").fetchone()
        apply_all_migrations(conn)
        conn.commit()
        after = conn.execute("SELECT id,content FROM memories WHERE source='polaris-test'").fetchone()
        assert tuple(before) == tuple(after)
        assert '0042_legacy_aurora_import' in applied_migration_versions(conn)
        assert conn.execute("SELECT COUNT(*) FROM legacy_aurora_import_runs").fetchone()[0] == 0
    finally:
        conn.close()
