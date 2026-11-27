from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .fhir_utils import clean, fhir_datetime, fhir_ref


class HmsAppointment(models.Model):
    """Maps to the HL7 FHIR R4 'Appointment' resource."""
    _name = 'hms.appointment'
    _description = 'Appointment'
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'start desc'
    _check_company_auto = True

    name = fields.Char(readonly=True, copy=False, default='New')
    company_id = fields.Many2one('res.company', required=True, index=True, default=lambda self: self.env.company)
    patient_id = fields.Many2one('hms.patient', required=True, tracking=True, index=True, check_company=True)
    practitioner_id = fields.Many2one('hms.practitioner', required=True, tracking=True, index=True, check_company=True)
    start = fields.Datetime(required=True, default=fields.Datetime.now, tracking=True)
    end = fields.Datetime(tracking=True)
    reason = fields.Char(tracking=True)
    # Values are FHIR's AppointmentStatus codes, so they export unchanged
    status = fields.Selection([
        ('booked', 'Booked'),
        ('arrived', 'Arrived'),
        ('fulfilled', 'Fulfilled'),
        ('cancelled', 'Cancelled'),
        ('noshow', 'No Show'),
    ], default='booked', required=True, tracking=True)
    encounter_id = fields.Many2one('hms.encounter', readonly=True, copy=False)

    @api.constrains('start', 'end')
    def _check_dates(self):
        for rec in self:
            if rec.end and rec.end <= rec.start:
                raise ValidationError(self.env._('The appointment must end after it starts.'))

    DEFAULT_LENGTH = timedelta(minutes=30)  # used when no end time is given

    def _effective_end(self):
        return self.end or self.start + self.DEFAULT_LENGTH

    @api.constrains('practitioner_id', 'start', 'end', 'status')
    def _check_double_booking(self):
        """A practitioner cannot have two active appointments that overlap."""
        for rec in self.filtered(lambda r: r.status in ('booked', 'arrived')):
            end = rec._effective_end()
            candidates = self.sudo().search([
                ('id', '!=', rec.id),
                ('practitioner_id', '=', rec.practitioner_id.id),
                ('status', 'in', ('booked', 'arrived')),
                ('start', '<', end),
                ('start', '>', rec.start - timedelta(days=1)),
            ])
            clash = candidates.filtered(lambda o: o._effective_end() > rec.start)[:1]
            if clash:
                raise ValidationError(self.env._(
                    '%(doctor)s is already booked from %(start)s to %(end)s (%(ref)s). Choose another time.',
                    doctor=rec.practitioner_id.name,
                    start=fields.Datetime.context_timestamp(rec, clash.start).strftime('%d %b %H:%M'),
                    end=fields.Datetime.context_timestamp(rec, clash._effective_end()).strftime('%H:%M'),
                    ref=clash.name))

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('hms.appointment') or 'New'
            if vals.get('patient_id') and not vals.get('company_id'):
                vals['company_id'] = self.env['hms.patient'].browse(vals['patient_id']).sudo().company_id.id
        return super().create(vals_list)

    def action_arrive(self):
        """Patient has checked in: mark arrived and open a planned encounter for the visit."""
        for rec in self:
            if rec.status != 'booked':
                raise UserError(self.env._('Only booked appointments can be checked in.'))
            encounter = self.env['hms.encounter'].sudo().create({
                'patient_id': rec.patient_id.id,
                'practitioner_id': rec.practitioner_id.id,
                'appointment_id': rec.id,
                'company_id': rec.company_id.id,
                'reason': rec.reason,
            })
            rec.write({'status': 'arrived', 'encounter_id': encounter.id})

    def action_cancel(self):
        self.write({'status': 'cancelled'})

    def action_noshow(self):
        self.write({'status': 'noshow'})

    def to_fhir(self):
        self.ensure_one()
        participants = [
            {'actor': fhir_ref('Patient', self.patient_id), 'status': 'accepted'},
            {'actor': fhir_ref('Practitioner', self.practitioner_id), 'status': 'accepted'},
        ]
        return clean({
            'resourceType': 'Appointment',
            'id': str(self.id),
            'identifier': [{'value': self.name}],
            'status': self.status,
            'description': self.reason,
            'start': fhir_datetime(self.start),
            'end': fhir_datetime(self.end),
            'participant': participants,
        })
