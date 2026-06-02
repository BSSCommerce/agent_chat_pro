-- migrate: skip_if_table_missing plugin_agent_chat_pro_messages
-- migrate: skip_if_column_exists plugin_agent_chat_pro_messages activity_log

ALTER TABLE plugin_agent_chat_pro_messages ADD COLUMN activity_log TEXT;
