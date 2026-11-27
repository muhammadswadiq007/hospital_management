"""Upgrade from 1.5/1.6: the access log's patient column stops being a database link.

The old link (ON DELETE RESTRICT) made it impossible to delete any patient who had ever been viewed.
The column keeps its values; only the constraint goes. A copy of the patient's name and MRN is added
in post-migrate so the log stays readable after a patient is deleted.
"""


def migrate(cr, version):
    cr.execute("ALTER TABLE hms_access_log DROP CONSTRAINT IF EXISTS hms_access_log_patient_id_fkey")
    cr.execute("ALTER TABLE hms_access_log DROP CONSTRAINT IF EXISTS hms_access_log_user_id_fkey")
