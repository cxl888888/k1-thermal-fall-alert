"""Frozen SQLite initial schema, revision 0001_initial. No runtime data."""
from alembic import op
import sqlalchemy as sa

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

_STATEMENTS = ['CREATE TABLE changes (\n'
 '\tid INTEGER NOT NULL, \n'
 '\tkind VARCHAR(40) NOT NULL, \n'
 '\tincident_id VARCHAR(32), \n'
 '\tcreated_at DATETIME NOT NULL, \n'
 '\tPRIMARY KEY (id)\n'
 ')',
 'CREATE TABLE clients (\n'
 '\tid VARCHAR(32) NOT NULL, \n'
 '\tlabel VARCHAR(80) NOT NULL, \n'
 '\trevoked BOOLEAN NOT NULL, \n'
 '\tcreated_at DATETIME NOT NULL, \n'
 '\tPRIMARY KEY (id)\n'
 ')',
 'CREATE TABLE devices (\n'
 '\tid VARCHAR(32) NOT NULL, \n'
 '\tdevice_id VARCHAR(100) NOT NULL, \n'
 '\torigin VARCHAR(32) NOT NULL, \n'
 '\tboot_id VARCHAR(100) NOT NULL, \n'
 '\treceived_at DATETIME NOT NULL, \n'
 '\tsensor_status JSON NOT NULL, \n'
 '\tinteraction_state VARCHAR(80) NOT NULL, \n'
 '\tsource_gap BOOLEAN NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (device_id, origin)\n'
 ')',
 'CREATE TABLE incidents (\n'
 '\tid VARCHAR(32) NOT NULL, \n'
 '\tdevice_id VARCHAR(100) NOT NULL, \n'
 '\tsource_incident_id VARCHAR(100) NOT NULL, \n'
 '\torigin VARCHAR(32) NOT NULL, \n'
 '\tboot_id VARCHAR(100) NOT NULL, \n'
 '\tversion INTEGER NOT NULL, \n'
 '\ttitle VARCHAR(200) NOT NULL, \n'
 '\troute VARCHAR(40) NOT NULL, \n'
 '\treason TEXT NOT NULL, \n'
 '\ttranscript TEXT, \n'
 '\tsource VARCHAR(80) NOT NULL, \n'
 '\tred_latched BOOLEAN NOT NULL, \n'
 '\tarchived BOOLEAN NOT NULL, \n'
 '\toccurred_at VARCHAR(48), \n'
 '\ttime_quality VARCHAR(40) NOT NULL, \n'
 '\treceived_at DATETIME NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (device_id, origin, source_incident_id)\n'
 ')',
 'CREATE TABLE pairings (\n'
 '\tdigest VARCHAR(64) NOT NULL, \n'
 '\tlabel VARCHAR(80) NOT NULL, \n'
 '\texpires_at DATETIME NOT NULL, \n'
 '\tconsumed BOOLEAN NOT NULL, \n'
 '\tPRIMARY KEY (digest)\n'
 ')',
 'CREATE TABLE source_cursors (\n'
 '\tid VARCHAR(32) NOT NULL, \n'
 '\tdevice_id VARCHAR(100) NOT NULL, \n'
 '\torigin VARCHAR(32) NOT NULL, \n'
 '\tboot_id VARCHAR(100) NOT NULL, \n'
 '\tsequence INTEGER NOT NULL, \n'
 '\tstate_version INTEGER NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (device_id, origin, boot_id)\n'
 ')',
 'CREATE TABLE credentials (\n'
 '\tdigest VARCHAR(64) NOT NULL, \n'
 '\tclient_id VARCHAR(32), \n'
 '\trole VARCHAR(16) NOT NULL, \n'
 '\trevoked BOOLEAN NOT NULL, \n'
 '\texpires_at DATETIME, \n'
 '\tPRIMARY KEY (digest), \n'
 '\tFOREIGN KEY(client_id) REFERENCES clients (id)\n'
 ')',
 'CREATE TABLE operations (\n'
 '\tid INTEGER NOT NULL, \n'
 '\tclient_id VARCHAR(32) NOT NULL, \n'
 '\trequest_id VARCHAR(100) NOT NULL, \n'
 '\tincident_id VARCHAR(32) NOT NULL, \n'
 '\tstate_version INTEGER NOT NULL, \n'
 '\tkind VARCHAR(32) NOT NULL, \n'
 '\tdigest VARCHAR(64) NOT NULL, \n'
 '\tpayload JSON NOT NULL, \n'
 '\tresult JSON NOT NULL, \n'
 '\tcreated_at DATETIME NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (client_id, request_id), \n'
 '\tFOREIGN KEY(client_id) REFERENCES clients (id), \n'
 '\tFOREIGN KEY(incident_id) REFERENCES incidents (id)\n'
 ')',
 'CREATE TABLE source_events (\n'
 '\tid INTEGER NOT NULL, \n'
 '\tdevice_id VARCHAR(100) NOT NULL, \n'
 '\torigin VARCHAR(32) NOT NULL, \n'
 '\tboot_id VARCHAR(100) NOT NULL, \n'
 '\tsequence INTEGER NOT NULL, \n'
 '\tsource_event_id VARCHAR(100) NOT NULL, \n'
 '\tdigest VARCHAR(64) NOT NULL, \n'
 '\tpayload JSON NOT NULL, \n'
 '\tincident_id VARCHAR(32), \n'
 '\treceived_at DATETIME NOT NULL, \n'
 '\tPRIMARY KEY (id), \n'
 '\tUNIQUE (device_id, origin, boot_id, sequence), \n'
 '\tUNIQUE (device_id, origin, boot_id, source_event_id), \n'
 '\tFOREIGN KEY(incident_id) REFERENCES incidents (id)\n'
 ')',
 'CREATE INDEX ix_operations_state_version ON operations (state_version)']

def upgrade():
    for statement in _STATEMENTS:
        op.execute(sa.text(statement))

def downgrade():
    for name in ['source_events', 'operations', 'credentials', 'source_cursors', 'pairings', 'incidents', 'devices', 'clients', 'changes']:
        op.drop_table(name)
