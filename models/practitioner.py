from odoo import fields, models

from .fhir_utils import clean


class HmsPractitioner(models.Model):
    """Maps to the HL7 FHIR R4 'Practitioner' resource (doctors, nurses, clinical officers)."""
    _name = 'hms.practitioner'
    _description = 'Practitioner'
    _inherit = ['mail.thread']
    _order = 'name'

    name = fields.Char(required=True, tracking=True)
    user_id = fields.Many2one(
        'res.users', string='Odoo User', tracking=True,
        help='The login this practitioner uses. Doctors can only edit encounters linked to their own user.')
    role = fields.Selection([
        ('doctor', 'Doctor'),
        ('clinical_officer', 'Clinical Officer'),
        ('nurse', 'Nurse'),
        ('other', 'Other'),
    ], default='doctor', required=True, tracking=True)
    specialty = fields.Char(tracking=True)
    license_number = fields.Char(string='Licence / Registration No.', tracking=True)
    phone = fields.Char()
    email = fields.Char()
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', index=True, default=lambda self: self.env.company,
                                 help='Leave empty for a practitioner who works at all branches.')

    _user_unique = models.Constraint('UNIQUE(user_id)', 'This user is already linked to another practitioner.')

    def to_fhir(self):
        self.ensure_one()
        parts = self.name.split()
        # Titles go in FHIR's HumanName.prefix, not in the given names
        prefix = []
        while len(parts) > 1 and parts[0].rstrip('.').lower() in ('dr', 'prof', 'mr', 'mrs', 'ms', 'sr'):
            prefix.append(parts.pop(0))
        telecom = [t for t in [
            {'system': 'phone', 'value': self.phone} if self.phone else None,
            {'system': 'email', 'value': self.email} if self.email else None,
        ] if t]
        return clean({
            'resourceType': 'Practitioner',
            'id': str(self.id),
            'active': self.active,
            'identifier': [{'use': 'official', 'value': self.license_number}] if self.license_number else None,
            'name': [clean({'text': self.name, 'family': parts[-1], 'given': parts[:-1] or None,
                      'prefix': prefix or None})],
            'telecom': telecom,
            'qualification': [{'code': {'text': self.specialty or dict(self._fields['role'].selection)[self.role]}}],
        })
