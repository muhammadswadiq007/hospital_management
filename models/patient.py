from odoo import api, fields, models


class HmsPatient(models.Model):
    """Maps to the HL7 FHIR R4 'Patient' resource."""
    _name = 'hms.patient'
    _description = 'Patient'
    _inherit = ['mail.thread', 'mail.activity.mixin']  # chatter = change history / audit trail
    _order = 'family_name, given_name'

    # FHIR Patient.identifier (Medical Record Number)
    mrn = fields.Char(string='MRN', readonly=True, copy=False, default='New', tracking=True)
    national_id = fields.Char(string='National ID', tracking=True)

    # FHIR Patient.name (HumanName: given + family)
    given_name = fields.Char(string='First Name', required=True, tracking=True)
    family_name = fields.Char(string='Last Name', required=True, tracking=True)
    name = fields.Char(compute='_compute_name', store=True)

    # FHIR Patient.gender uses the 'administrative-gender' value set exactly:
    gender = fields.Selection([
        ('male', 'Male'),
        ('female', 'Female'),
        ('other', 'Other'),
        ('unknown', 'Unknown'),
    ], required=True, default='unknown', tracking=True)

    birth_date = fields.Date(string='Date of Birth', tracking=True)  # FHIR Patient.birthDate
    phone = fields.Char(tracking=True)   # FHIR Patient.telecom (system=phone)
    email = fields.Char(tracking=True)   # FHIR Patient.telecom (system=email)
    address = fields.Char()              # FHIR Patient.address (text)
    active = fields.Boolean(default=True)  # FHIR Patient.active

    appointment_ids = fields.One2many('hms.appointment', 'patient_id', string='Appointments')
    encounter_ids = fields.One2many('hms.encounter', 'patient_id', string='Encounters')
    observation_ids = fields.One2many('hms.observation', 'patient_id', string='Vital Signs')
    condition_ids = fields.One2many('hms.condition', 'patient_id', string='Diagnoses')
    medication_request_ids = fields.One2many('hms.medication.request', 'patient_id', string='Medications')
    lab_request_ids = fields.One2many('hms.lab.request', 'patient_id', string='Lab Results')

    # Odoo 19/20 style SQL constraint (replaces _sql_constraints)
    _mrn_unique = models.Constraint('UNIQUE(mrn)', 'The MRN must be unique.')

    @api.depends('given_name', 'family_name')
    def _compute_name(self):
        for rec in self:
            rec.name = ' '.join(p for p in [rec.given_name, rec.family_name] if p)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('mrn', 'New') == 'New':
                vals['mrn'] = self.env['ir.sequence'].next_by_code('hms.patient') or 'New'
        return super().create(vals_list)

    def to_fhir(self):
        """Return this patient as a FHIR R4 Patient resource (Python dict -> JSON)."""
        self.ensure_one()
        resource = {
            'resourceType': 'Patient',
            'id': str(self.id),
            'active': self.active,
            'identifier': [{
                'use': 'usual',
                'type': {'coding': [{
                    'system': 'http://terminology.hl7.org/CodeSystem/v2-0203',
                    'code': 'MR',
                    'display': 'Medical record number',
                }]},
                'value': self.mrn,
            }],
            'name': [{
                'use': 'official',
                'family': self.family_name,
                'given': [self.given_name],
            }],
            'gender': self.gender,
        }
        if self.birth_date:
            resource['birthDate'] = self.birth_date.isoformat()
        telecom = []
        if self.phone:
            telecom.append({'system': 'phone', 'value': self.phone})
        if self.email:
            telecom.append({'system': 'email', 'value': self.email})
        if telecom:
            resource['telecom'] = telecom
        if self.address:
            resource['address'] = [{'text': self.address}]
        return resource
