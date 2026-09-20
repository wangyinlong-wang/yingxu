"""Initial local workspace schema; generated and frozen for V1."""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "\nCREATE TABLE accounts (\n\tplatform VARCHAR(20) NOT NULL, \n\tdisplay_name VARCHAR(200) NOT NULL, \n\taccount_identifier VARCHAR(200) NOT NULL, \n\tdevice_serial VARCHAR(200) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (platform)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE reports (\n\tdate VARCHAR(10) NOT NULL, \n\tbody TEXT NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE schedules (\n\tname VARCHAR(200) NOT NULL, \n\tkind VARCHAR(20) NOT NULL, \n\ttime VARCHAR(5) NOT NULL, \n\ttimezone VARCHAR(100) NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tenabled_at VARCHAR(40), \n\tdaily_limit INTEGER NOT NULL, \n\tlast_run_at VARCHAR(40), \n\tlast_scheduled_date VARCHAR(10), \n\tnext_run_at VARCHAR(40), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        '\nCREATE TABLE settings (\n\t"key" VARCHAR(100) NOT NULL, \n\tvalue JSON NOT NULL, \n\tPRIMARY KEY ("key")\n)\n\n'
    )
    op.execute(
        "\nCREATE TABLE sources (\n\tname VARCHAR(200) NOT NULL, \n\tkind VARCHAR(20) NOT NULL, \n\turl TEXT NOT NULL, \n\tenabled BOOLEAN NOT NULL, \n\tlast_collected_at VARCHAR(40), \n\tlast_error TEXT, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE templates (\n\tname VARCHAR(200) NOT NULL, \n\tdescription TEXT NOT NULL, \n\tprompt TEXT NOT NULL, \n\tstyle VARCHAR(20) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE worker_heartbeats (\n\tqueue VARCHAR(20) NOT NULL, \n\tworker_id VARCHAR(100) NOT NULL, \n\tlast_seen VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (queue)\n)\n\n"
    )
    op.execute(
        "\nCREATE TABLE topics (\n\tsource_id VARCHAR(36) NOT NULL, \n\ttitle VARCHAR(1000) NOT NULL, \n\turl TEXT NOT NULL, \n\tfingerprint VARCHAR(64) NOT NULL, \n\tsummary TEXT NOT NULL, \n\tpublished_at VARCHAR(40), \n\tscore FLOAT NOT NULL, \n\trelevance_score FLOAT NOT NULL, \n\tselection_reason TEXT NOT NULL, \n\tstatus VARCHAR(20) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(source_id) REFERENCES sources (id), \n\tUNIQUE (fingerprint)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_topics_status ON topics (status)")
    op.execute(
        "\nCREATE TABLE contents (\n\ttopic_id VARCHAR(36) NOT NULL, \n\ttemplate_id VARCHAR(36) NOT NULL, \n\ttitle VARCHAR(1000) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tscript JSON NOT NULL, \n\tscenes JSON NOT NULL, \n\tmedia_version INTEGER, \n\tvideo_asset_id VARCHAR(36), \n\tcover_asset_id VARCHAR(36), \n\tsubtitle_asset_id VARCHAR(36), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (topic_id), \n\tFOREIGN KEY(topic_id) REFERENCES topics (id), \n\tFOREIGN KEY(template_id) REFERENCES templates (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_contents_status ON contents (status)")
    op.execute(
        "\nCREATE TABLE approvals (\n\tcontent_id VARCHAR(36) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tdecision VARCHAR(20) NOT NULL, \n\tsnapshot_hash VARCHAR(64) NOT NULL, \n\tnotes TEXT NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(content_id) REFERENCES contents (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_approvals_content_id ON approvals (content_id)")
    op.execute(
        "\nCREATE TABLE assets (\n\tcontent_id VARCHAR(36), \n\tkind VARCHAR(20) NOT NULL, \n\tfilename VARCHAR(255) NOT NULL, \n\trelative_path TEXT NOT NULL, \n\tmime_type VARCHAR(100) NOT NULL, \n\tsize INTEGER NOT NULL, \n\tsha256 VARCHAR(64) NOT NULL, \n\tinput_hash VARCHAR(64), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(content_id) REFERENCES contents (id), \n\tUNIQUE (relative_path)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_assets_content_id ON assets (content_id)")
    op.execute("CREATE INDEX ix_assets_input_hash ON assets (input_hash)")
    op.execute(
        "\nCREATE TABLE content_revisions (\n\tcontent_id VARCHAR(36) NOT NULL, \n\tversion INTEGER NOT NULL, \n\tsnapshot JSON NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (content_id, version), \n\tFOREIGN KEY(content_id) REFERENCES contents (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_content_revisions_content_id ON content_revisions (content_id)")
    op.execute(
        "\nCREATE TABLE jobs (\n\tkind VARCHAR(40) NOT NULL, \n\tqueue VARCHAR(20) NOT NULL, \n\tcontent_id VARCHAR(36), \n\tstatus VARCHAR(30) NOT NULL, \n\tidempotency_key VARCHAR(200) NOT NULL, \n\trequest_hash VARCHAR(64) NOT NULL, \n\tpayload JSON NOT NULL, \n\tresult JSON NOT NULL, \n\tprogress INTEGER NOT NULL, \n\tcurrent_step VARCHAR(200) NOT NULL, \n\terror_code VARCHAR(100), \n\terror_message TEXT, \n\tcancel_requested BOOLEAN NOT NULL, \n\tlease_owner VARCHAR(100), \n\tlease_until VARCHAR(40), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(content_id) REFERENCES contents (id), \n\tUNIQUE (idempotency_key)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_jobs_content_id ON jobs (content_id)")
    op.execute("CREATE INDEX ix_jobs_queue ON jobs (queue)")
    op.execute("CREATE INDEX ix_jobs_status ON jobs (status)")
    op.execute(
        "\nCREATE TABLE job_steps (\n\tjob_id VARCHAR(36) NOT NULL, \n\tname VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tdetail TEXT NOT NULL, \n\tstarted_at VARCHAR(40), \n\tcompleted_at VARCHAR(40), \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tUNIQUE (job_id, name), \n\tFOREIGN KEY(job_id) REFERENCES jobs (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_job_steps_job_id ON job_steps (job_id)")
    op.execute(
        "\nCREATE TABLE model_calls (\n\tjob_id VARCHAR(36) NOT NULL, \n\trequest_key VARCHAR(64) NOT NULL, \n\tkind VARCHAR(20) NOT NULL, \n\tmodel VARCHAR(200) NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tattempts INTEGER NOT NULL, \n\tresponse JSON NOT NULL, \n\tusage JSON NOT NULL, \n\tevents JSON NOT NULL, \n\terror_code VARCHAR(100), \n\tstarted_at VARCHAR(40) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(job_id) REFERENCES jobs (id), \n\tUNIQUE (request_key)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_model_calls_job_id ON model_calls (job_id)")
    op.execute("CREATE INDEX ix_model_calls_started_at ON model_calls (started_at)")
    op.execute(
        "\nCREATE TABLE publications (\n\tcontent_id VARCHAR(36) NOT NULL, \n\tcontent_version INTEGER NOT NULL, \n\tcontent_title VARCHAR(1000) NOT NULL, \n\taccount_id VARCHAR(36) NOT NULL, \n\tplatform VARCHAR(20) NOT NULL, \n\tstatus VARCHAR(30) NOT NULL, \n\tjob_id VARCHAR(36), \n\tsnapshot JSON NOT NULL, \n\treserved_serial VARCHAR(200), \n\twork_url TEXT, \n\twork_id VARCHAR(200), \n\tevidence JSON NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(content_id) REFERENCES contents (id), \n\tFOREIGN KEY(account_id) REFERENCES accounts (id), \n\tFOREIGN KEY(job_id) REFERENCES jobs (id), \n\tUNIQUE (reserved_serial)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_publications_content_id ON publications (content_id)")
    op.execute("CREATE INDEX ix_publications_status ON publications (status)")
    op.execute(
        "\nCREATE TABLE metrics (\n\tpublication_id VARCHAR(36) NOT NULL, \n\tplatform VARCHAR(20) NOT NULL, \n\tcontent_title VARCHAR(1000) NOT NULL, \n\tviews INTEGER, \n\tlikes INTEGER, \n\tcomments INTEGER, \n\tfavorites INTEGER, \n\tshares INTEGER, \n\tsource VARCHAR(20) NOT NULL, \n\traw JSON NOT NULL, \n\tobserved_at VARCHAR(40) NOT NULL, \n\tid VARCHAR(36) NOT NULL, \n\tcreated_at VARCHAR(40) NOT NULL, \n\tupdated_at VARCHAR(40) NOT NULL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(publication_id) REFERENCES publications (id)\n)\n\n"
    )
    op.execute("CREATE INDEX ix_metrics_observed_at ON metrics (observed_at)")
    op.execute("CREATE INDEX ix_metrics_publication_id ON metrics (publication_id)")


def downgrade():
    op.drop_table("metrics")
    op.drop_table("publications")
    op.drop_table("model_calls")
    op.drop_table("job_steps")
    op.drop_table("jobs")
    op.drop_table("content_revisions")
    op.drop_table("assets")
    op.drop_table("approvals")
    op.drop_table("contents")
    op.drop_table("topics")
    op.drop_table("worker_heartbeats")
    op.drop_table("templates")
    op.drop_table("sources")
    op.drop_table("settings")
    op.drop_table("schedules")
    op.drop_table("reports")
    op.drop_table("accounts")
