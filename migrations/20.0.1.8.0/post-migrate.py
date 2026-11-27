"""Upgrade to 1.8.

1. Consultation products were created without a price, so they got Odoo's default of 1.00 and were
   invoiced at 1.00 without any warning. Reset them to 0 (= "no price set") unless someone has changed
   them. Then fill the example KES prices again (only for a KES company, only where still empty).
2. Access log: rows written by 1.5-1.7 are one-record rows; fill the new patient-search column.
"""
from odoo import SUPERUSER_ID, api

CONSULT_XMLIDS = ['product_consult_outpatient', 'product_consult_emergency', 'product_consult_inpatient']


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for xmlid in CONSULT_XMLIDS:
        product = env.ref(f'hospital_management.{xmlid}', raise_if_not_found=False)
        if product and product.list_price == 1.0:
            product.list_price = 0.0
    env['hms.example.prices']._load()

    cr.execute("""UPDATE hms_access_log
                  SET patient_ids_text = ',' || patient_id || ',', record_count = 1
                  WHERE patient_id IS NOT NULL AND patient_ids_text IS NULL""")
    cr.execute("UPDATE hms_access_log SET record_count = 1 WHERE record_count IS NULL OR record_count = 0")
