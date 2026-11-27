from odoo import api, fields, models
from odoo.exceptions import UserError

from .fhir_utils import clean, fhir_datetime, fhir_ref

# FHIR R4 Encounter.class uses the HL7 v3 ActCode code system
ENCOUNTER_CLASS = [
    ('AMB', 'Outpatient'),
    ('IMP', 'Inpatient'),
    ('EMER', 'Emergency'),
]


class HmsEncounter(models.Model):
    """Maps to the HL7 FHIR R4 'Encounter' resource: one visit / admission."""
    _name = 'hms.encounter'
    _description = 'Encounter'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period_start desc, id desc'

    name = fields.Char(readonly=True, copy=False, default='New')
    patient_id = fields.Many2one('hms.patient', required=True, tracking=True, index=True)
    practitioner_id = fields.Many2one('hms.practitioner', required=True, tracking=True, index=True)
    appointment_id = fields.Many2one('hms.appointment', readonly=True)
    encounter_class = fields.Selection(ENCOUNTER_CLASS, string='Class', default='AMB', required=True, tracking=True)
    status = fields.Selection([
        ('planned', 'Planned'),
        ('in-progress', 'In Progress'),
        ('finished', 'Finished'),
        ('cancelled', 'Cancelled'),
    ], default='planned', required=True, tracking=True)
    period_start = fields.Datetime(string='Started', readonly=True)
    period_end = fields.Datetime(string='Ended', readonly=True)
    reason = fields.Char(tracking=True)
    # Only clinical staff may read notes (cashiers can open visits for billing, but never see this)
    clinical_notes = fields.Html(groups='hospital_management.group_hms_nurse')
    observation_ids = fields.One2many('hms.observation', 'encounter_id', string='Vital Signs')
    condition_ids = fields.One2many('hms.condition', 'encounter_id', string='Diagnoses')
    medication_request_ids = fields.One2many('hms.medication.request', 'encounter_id', string='Prescriptions')
    lab_request_ids = fields.One2many('hms.lab.request', 'encounter_id', string='Lab Tests')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('hms.encounter') or 'New'
        return super().create(vals_list)

    def action_start(self):
        for rec in self:
            if rec.status != 'planned':
                raise UserError(self.env._('Only planned encounters can be started.'))
        self.write({'status': 'in-progress', 'period_start': fields.Datetime.now()})

    def action_finish(self):
        for rec in self:
            if rec.status != 'in-progress':
                raise UserError(self.env._('Only encounters in progress can be finished.'))
        self.write({'status': 'finished', 'period_end': fields.Datetime.now()})
        self.appointment_id.sudo().write({'status': 'fulfilled'})

    def action_cancel(self):
        self.write({'status': 'cancelled'})

    def to_fhir(self):
        self.ensure_one()
        return clean({
            'resourceType': 'Encounter',
            'id': str(self.id),
            'identifier': [{'value': self.name}],
            'status': self.status,
            'class': {
                'system': 'http://terminology.hl7.org/CodeSystem/v3-ActCode',
                'code': self.encounter_class,
                'display': dict(ENCOUNTER_CLASS)[self.encounter_class],
            },
            'subject': fhir_ref('Patient', self.patient_id),
            'participant': [{'individual': fhir_ref('Practitioner', self.practitioner_id)}],
            'appointment': [fhir_ref('Appointment', self.appointment_id)] if self.appointment_id else None,
            'period': clean({'start': fhir_datetime(self.period_start), 'end': fhir_datetime(self.period_end)}),
            'reasonCode': [{'text': self.reason}] if self.reason else None,
            'diagnosis': [{'condition': fhir_ref('Condition', c), 'rank': i}
                          for i, c in enumerate(self.condition_ids.sorted('id'), start=1)],
        })
