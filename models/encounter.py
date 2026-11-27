from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .fhir_utils import clean, fhir_datetime, fhir_ref

# FHIR R4 Encounter.class uses the HL7 v3 ActCode code system
ENCOUNTER_CLASS = [
    ('AMB', 'Outpatient'),
    ('IMP', 'Inpatient'),
    ('EMER', 'Emergency'),
]
CLOSED = ('finished', 'cancelled')
# Fields the system itself may update on a closed visit (billing links, amendment counter)
SYSTEM_FIELDS = {'amendment_count', 'consult_invoice_line_id'}


class HmsEncounter(models.Model):
    """Maps to the HL7 FHIR R4 'Encounter' resource: one visit / admission."""
    _name = 'hms.encounter'
    _description = 'Encounter'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'period_start desc, id desc'
    _check_company_auto = True

    name = fields.Char(readonly=True, copy=False, default='New')
    company_id = fields.Many2one('res.company', required=True, index=True, default=lambda self: self.env.company)
    patient_id = fields.Many2one('hms.patient', required=True, tracking=True, index=True, check_company=True)
    practitioner_id = fields.Many2one('hms.practitioner', required=True, tracking=True, index=True, check_company=True)
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
    # Clinical content: only clinical staff may read it (cashiers open visits for billing, never see this)
    reason = fields.Char(tracking=True, groups='hospital_management.group_hms_nurse')
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
            if vals.get('patient_id') and not vals.get('company_id'):
                vals['company_id'] = self.env['hms.patient'].browse(vals['patient_id']).sudo().company_id.id
        return super().create(vals_list)

    def write(self, vals):
        changed = {f for f in vals if not f.startswith(('message_', 'activity_', 'website_message'))}
        # 1. Finished / cancelled visits are locked (only an admin amendment reopens them)
        if not self.env.context.get('hms_amend') and changed - SYSTEM_FIELDS:
            closed = self.filtered(lambda r: r.status in CLOSED)
            if closed:
                raise UserError(self.env._(
                    '%(visits)s is %(status)s and locked. Ask a hospital administrator to reopen it for amendment.',
                    visits=', '.join(closed.mapped('name')),
                    status=dict(self._fields['status'].selection)[closed[0].status].lower()))
        # 2. Moving a visit to another doctor or patient: administrators only, and always logged
        moved = {'practitioner_id', 'patient_id'} & changed
        if moved and not self.env.su:
            if not self.env.user.has_group('hospital_management.group_hms_admin'):
                raise AccessError(self.env._(
                    'Only a hospital administrator can move a visit to a different doctor or patient.'))
            for rec in self:
                details = []
                if 'practitioner_id' in vals and rec.practitioner_id.id != vals['practitioner_id']:
                    new = self.env['hms.practitioner'].browse(vals['practitioner_id'])
                    details.append(f'doctor {rec.practitioner_id.name} → {new.name}')
                if 'patient_id' in vals and rec.patient_id.id != vals['patient_id']:
                    new = self.env['hms.patient'].browse(vals['patient_id'])
                    details.append(f'patient {rec.patient_id.mrn} → {new.mrn}')
                if details:
                    self.env['hms.access.log']._log(rec, 'reassign', patient_field='patient_id',
                                                    detail='; '.join(details))
        return super().write(vals)

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

    def _check_can_cancel(self):
        """Hook: billing adds 'not invoiced' on top of this."""
        for rec in self:
            if rec.status not in ('planned', 'in-progress'):
                raise UserError(self.env._(
                    '%s is already finished or cancelled. A finished visit cannot be cancelled; '
                    'an administrator can reopen it for amendment instead.', rec.name))

    def action_cancel(self):
        self._check_can_cancel()
        self.write({'status': 'cancelled', 'period_end': fields.Datetime.now()})
        for rec in self:
            rec.message_post(body=self.env._('Visit cancelled by %s.', self.env.user.name))
        self.env['hms.access.log']._log(self, 'cancel', patient_field='patient_id')

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
            'serviceProvider': {'display': self.company_id.name},
        })
