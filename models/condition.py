from odoo import api, fields, models

from .fhir_utils import clean, fhir_datetime, fhir_ref
from .observation import _check_encounter_open


class HmsIcd10(models.Model):
    """ICD-10 diagnosis codes (WHO). Admins can add more under Configuration."""
    _name = 'hms.icd10'
    _description = 'ICD-10 Code'
    _order = 'code'
    _rec_names_search = ['code', 'name']

    code = fields.Char(required=True)
    name = fields.Char(string='Description', required=True)
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint('UNIQUE(code)', 'This ICD-10 code already exists.')

    @api.depends('code', 'name')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.code} {rec.name}'


class HmsCondition(models.Model):
    """Maps to the HL7 FHIR R4 'Condition' resource (encounter diagnosis)."""
    _name = 'hms.condition'
    _description = 'Diagnosis'
    _order = 'recorded_date desc, id desc'

    encounter_id = fields.Many2one('hms.encounter', required=True, ondelete='cascade', index=True)
    patient_id = fields.Many2one(related='encounter_id.patient_id', store=True, index=True)
    company_id = fields.Many2one(related='encounter_id.company_id', store=True, index=True)
    icd10_id = fields.Many2one('hms.icd10', string='Diagnosis (ICD-10)', required=True)
    # Values are FHIR's own codes
    verification_status = fields.Selection([
        ('provisional', 'Provisional'),
        ('confirmed', 'Confirmed'),
    ], string='Certainty', default='provisional', required=True)
    clinical_status = fields.Selection([
        ('active', 'Active'),
        ('resolved', 'Resolved'),
    ], string='Status', default='active', required=True)
    note = fields.Char()
    recorded_date = fields.Datetime(default=fields.Datetime.now, readonly=True)

    @api.depends('icd10_id')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.icd10_id.display_name or ''

    @api.model_create_multi
    def create(self, vals_list):
        _check_encounter_open(self.env, self.env['hms.encounter'].browse([v['encounter_id'] for v in vals_list if v.get('encounter_id')]))
        return super().create(vals_list)

    def write(self, vals):
        _check_encounter_open(self.env, self.encounter_id)
        return super().write(vals)

    def unlink(self):
        _check_encounter_open(self.env, self.encounter_id)
        return super().unlink()

    def to_fhir(self):
        self.ensure_one()
        return clean({
            'resourceType': 'Condition',
            'id': str(self.id),
            'clinicalStatus': {'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-clinical',
                'code': self.clinical_status}]},
            'verificationStatus': {'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-ver-status',
                'code': self.verification_status}]},
            'category': [{'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/condition-category',
                'code': 'encounter-diagnosis', 'display': 'Encounter Diagnosis'}]}],
            'code': {
                'coding': [{'system': 'http://hl7.org/fhir/sid/icd-10',
                            'code': self.icd10_id.code, 'display': self.icd10_id.name}],
                'text': self.icd10_id.name,
            },
            'subject': fhir_ref('Patient', self.patient_id),
            'encounter': fhir_ref('Encounter', self.encounter_id),
            'recordedDate': fhir_datetime(self.recorded_date),
            'recorder': fhir_ref('Practitioner', self.encounter_id.practitioner_id),
            'note': [{'text': self.note}] if self.note else None,
        })
