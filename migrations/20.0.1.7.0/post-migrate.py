"""Upgrade from 1.6: billing moved from 'one invoice per visit' to 'each item remembers its invoice line'.

Links the lines of invoices created by 1.6 (stored in the old hms_encounter.invoice_id column) to the
consultation, prescriptions and lab tests they charged, so those visits stay 'Invoiced'.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

CONSULT_XMLIDS = ['product_consult_outpatient', 'product_consult_emergency', 'product_consult_inpatient']


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {'hms_amend': True})

    # Access log rows from 1.5/1.6: keep a copy of who / which patient, independent of those records
    cr.execute("""UPDATE hms_access_log l SET patient_name = p.name, patient_mrn = p.mrn
                  FROM hms_patient p WHERE p.id = l.patient_id AND l.patient_name IS NULL""")
    cr.execute("""UPDATE hms_access_log l SET user_login = u.login
                  FROM res_users u WHERE u.id = l.user_id AND l.user_login IS NULL""")
    cr.execute("""UPDATE hms_access_log l SET company_id = p.company_id
                  FROM hms_patient p WHERE p.id = l.patient_id AND l.company_id IS NULL""")

    # Numeric results typed in before 1.7 count as entered
    cr.execute("UPDATE hms_lab_request SET result_entered = TRUE WHERE result_value <> 0 AND result_entered IS NOT TRUE")

    cr.execute("""SELECT 1 FROM information_schema.columns
                  WHERE table_name = 'hms_encounter' AND column_name = 'invoice_id'""")
    if not cr.fetchone():
        return
    cr.execute("SELECT id, invoice_id FROM hms_encounter WHERE invoice_id IS NOT NULL")
    rows = cr.fetchall()
    consult_products = env['product.product']
    for xmlid in CONSULT_XMLIDS:
        consult_products |= env.ref(f'hospital_management.{xmlid}', raise_if_not_found=False) or env['product.product']

    for enc_id, move_id in rows:
        enc = env['hms.encounter'].browse(enc_id).exists()
        move = env['account.move'].browse(move_id).exists()
        if not enc or not move:
            continue
        if not move.hms_encounter_id:
            cr.execute("UPDATE account_move SET hms_encounter_id = %s WHERE id = %s", (enc.id, move.id))
        meds = enc.medication_request_ids.filtered(lambda m: not m.invoice_line_id)
        labs = enc.lab_request_ids.filtered(lambda l: not l.invoice_line_id)
        for line in move.invoice_line_ids:
            if line.product_id in consult_products and not enc.consult_invoice_line_id:
                cr.execute("UPDATE hms_encounter SET consult_invoice_line_id = %s WHERE id = %s", (line.id, enc.id))
                continue
            med = meds.filtered(lambda m: m.medication_id.product_id == line.product_id)[:1]
            if med:
                cr.execute("UPDATE hms_medication_request SET invoice_line_id = %s WHERE id = %s", (line.id, med.id))
                meds -= med
                continue
            lab = labs.filtered(lambda l: l.test_id.product_id == line.product_id)[:1]
            if lab:
                cr.execute("UPDATE hms_lab_request SET invoice_line_id = %s WHERE id = %s", (line.id, lab.id))
                labs -= lab
        _logger.info('hospital_management: linked invoice %s to visit %s', move.name, enc.name)

    env.invalidate_all()
    encounters = env['hms.encounter'].search([])
    env.add_to_compute(env['hms.encounter']._fields['billing_state'], encounters)
    encounters.flush_recordset(['billing_state'])
