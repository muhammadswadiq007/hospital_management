from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .fhir_utils import clean, fhir_datetime, fhir_ref

# Each vital sign with its official LOINC code and UCUM unit (what FHIR's
# vital-signs profile expects). 'range' is a plausibility check to catch typos.
VITALS = {
    'temp':   {'label': 'Temperature',       'loinc': '8310-5',  'display': 'Body temperature',   'unit': '°C',   'ucum': 'Cel',     'range': (30, 45)},
    'bp':     {'label': 'Blood Pressure',    'loinc': '85354-9', 'display': 'Blood pressure panel', 'unit': 'mmHg', 'ucum': 'mm[Hg]', 'range': (50, 260)},
    'pulse':  {'label': 'Pulse',             'loinc': '8867-4',  'display': 'Heart rate',          'unit': 'bpm',  'ucum': '/min',    'range': (20, 250)},
    'resp':   {'label': 'Respiratory Rate',  'loinc': '9279-1',  'display': 'Respiratory rate',    'unit': 'breaths/min', 'ucum': '/min', 'range': (4, 80)},
    'spo2':   {'label': 'SpO₂',              'loinc': '2708-6',  'display': 'Oxygen saturation in Arterial blood', 'unit': '%', 'ucum': '%', 'range': (50, 100)},
    'weight': {'label': 'Weight',            'loinc': '29463-7', 'display': 'Body weight',         'unit': 'kg',   'ucum': 'kg',      'range': (0.3, 400)},
    'height': {'label': 'Height',            'loinc': '8302-2',  'display': 'Body height',         'unit': 'cm',   'ucum': 'cm',      'range': (20, 250)},
}
BP_COMPONENTS = {
    'systolic':  ('8480-6', 'Systolic blood pressure'),
    'diastolic': ('8462-4', 'Diastolic blood pressure'),
}
DIASTOLIC_RANGE = (30, 160)


def _check_encounter_open(env, encounters):
    if any(e.status in ('finished', 'cancelled') for e in encounters):
        raise UserError(env._('This encounter is closed. Clinical data can no longer be changed.'))


class HmsObservation(models.Model):
    """Maps to the HL7 FHIR R4 'Observation' resource (vital-signs category)."""
    _name = 'hms.observation'
    _description = 'Vital Sign'
    _order = 'effective desc, id desc'

    encounter_id = fields.Many2one('hms.encounter', required=True, ondelete='cascade', index=True)
    patient_id = fields.Many2one(related='encounter_id.patient_id', store=True, index=True)
    company_id = fields.Many2one(related='encounter_id.company_id', store=True, index=True)
    code = fields.Selection([(k, v['label']) for k, v in VITALS.items()], string='Vital Sign', required=True)
    value = fields.Float(digits=(6, 1), required=True, help='For blood pressure: the systolic value')
    value_diastolic = fields.Float(string='Diastolic', digits=(6, 0))
    unit = fields.Char(compute='_compute_unit')
    value_display = fields.Char(string='Reading', compute='_compute_value_display')
    effective = fields.Datetime(string='Taken At', required=True, default=fields.Datetime.now)

    @api.depends('code')
    def _compute_unit(self):
        for rec in self:
            rec.unit = VITALS[rec.code]['unit'] if rec.code else False

    @api.depends('code', 'value', 'value_diastolic')
    def _compute_value_display(self):
        for rec in self:
            if not rec.code:
                rec.value_display = False
            elif rec.code == 'bp':
                rec.value_display = f'{rec.value:g}/{rec.value_diastolic:g} mmHg'
            else:
                rec.value_display = f'{rec.value:g} {VITALS[rec.code]["unit"]}'

    @api.depends('code', 'value_display')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{VITALS[rec.code]["label"]} {rec.value_display}' if rec.code else ''

    @api.constrains('code', 'value', 'value_diastolic')
    def _check_ranges(self):
        for rec in self:
            low, high = VITALS[rec.code]['range']
            if not low <= rec.value <= high:
                raise ValidationError(self.env._(
                    '%(vital)s of %(value)s looks wrong (expected %(low)s–%(high)s %(unit)s).',
                    vital=VITALS[rec.code]['label'], value=rec.value, low=low, high=high, unit=VITALS[rec.code]['unit']))
            if rec.code == 'bp':
                if not DIASTOLIC_RANGE[0] <= rec.value_diastolic <= DIASTOLIC_RANGE[1]:
                    raise ValidationError(self.env._('Enter the diastolic value (30–160 mmHg) for blood pressure.'))
                if rec.value_diastolic >= rec.value:
                    raise ValidationError(self.env._('Diastolic must be lower than systolic.'))

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
        v = VITALS[self.code]

        def quantity(value, ucum=v['ucum'], unit=v['unit']):
            return {'value': value, 'unit': unit, 'system': 'http://unitsofmeasure.org', 'code': ucum}

        resource = {
            'resourceType': 'Observation',
            'id': str(self.id),
            'status': 'final',
            'category': [{'coding': [{
                'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                'code': 'vital-signs', 'display': 'Vital Signs'}]}],
            'code': {'coding': [{'system': 'http://loinc.org', 'code': v['loinc'], 'display': v['display']}],
                     'text': v['label']},
            'subject': fhir_ref('Patient', self.patient_id),
            'encounter': fhir_ref('Encounter', self.encounter_id),
            'effectiveDateTime': fhir_datetime(self.effective),
        }
        if self.code == 'bp':
            # FHIR puts blood pressure in two components, not one value
            resource['component'] = [{
                'code': {'coding': [{'system': 'http://loinc.org', 'code': BP_COMPONENTS[part][0],
                                     'display': BP_COMPONENTS[part][1]}]},
                'valueQuantity': quantity(val),
            } for part, val in (('systolic', self.value), ('diastolic', self.value_diastolic))]
        else:
            resource['valueQuantity'] = quantity(self.value)
        return clean(resource)
