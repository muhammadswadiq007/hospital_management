from odoo import api, fields, models
from odoo.exceptions import ValidationError

from .fhir_utils import clean, fhir_datetime, fhir_ref
from .observation import _check_encounter_open

ATC_SYSTEM = 'http://www.whocc.no/atc'  # WHO ATC drug classification
UCUM = 'http://unitsofmeasure.org'

DOSE_FORMS = [
    ('tablet', 'Tablet'), ('capsule', 'Capsule'), ('syrup', 'Syrup'), ('suspension', 'Suspension'),
    ('injection', 'Injection'), ('cream', 'Cream / Ointment'), ('inhaler', 'Inhaler'),
    ('sachet', 'Sachet'), ('drops', 'Drops'),
]
DOSE_UNITS = [
    ('tablet', 'tablet(s)'), ('capsule', 'capsule(s)'), ('ml', 'ml'), ('mg', 'mg'),
    ('g', 'g'), ('puff', 'puff(s)'), ('sachet', 'sachet(s)'), ('drop', 'drop(s)'), ('application', 'application(s)'),
]
UCUM_UNIT = {'ml': 'mL', 'mg': 'mg', 'g': 'g'}  # countable units use UCUM's {annotation} form
COUNTABLE = {'tablet', 'capsule', 'puff', 'sachet', 'drop', 'application'}

# Routes with their SNOMED CT codes (what FHIR's route field expects)
ROUTES = {
    'oral': ('Oral', '26643006'),
    'iv': ('Intravenous', '47625008'),
    'im': ('Intramuscular', '78421000'),
    'sc': ('Subcutaneous', '34206005'),
    'topical': ('Topical', '6064005'),
    'inhaled': ('Inhalation', '447694001'),
    'rectal': ('Rectal', '37161004'),
}
# Clinical frequency abbreviations -> times per day (None = not a regular schedule)
FREQUENCIES = {
    'od': ('Once daily (OD)', 1),
    'bd': ('Twice daily (BD)', 2),
    'tds': ('Three times daily (TDS)', 3),
    'qid': ('Four times daily (QID)', 4),
    'stat': ('Once only (STAT)', None),
    'prn': ('When required (PRN)', None),
}


class HmsMedication(models.Model):
    """Drug formulary. Maps to FHIR's medicationCodeableConcept (coded with WHO ATC)."""
    _name = 'hms.medication'
    _description = 'Medication'
    _order = 'name, strength'
    _rec_names_search = ['name', 'atc_code']

    name = fields.Char(string='Generic Name', required=True)
    strength = fields.Char(help='e.g. 500 mg, 125 mg/5 ml')
    form = fields.Selection(DOSE_FORMS, required=True, default='tablet')
    atc_code = fields.Char(string='ATC Code', help='WHO Anatomical Therapeutic Chemical code')
    active = fields.Boolean(default=True)

    @api.depends('name', 'strength', 'form')
    def _compute_display_name(self):
        forms = dict(DOSE_FORMS)
        for rec in self:
            rec.display_name = ' '.join(p for p in [rec.name, rec.strength, forms.get(rec.form, '').lower()] if p)


class HmsMedicationRequest(models.Model):
    """Maps to the HL7 FHIR R4 'MedicationRequest' resource (one prescription line)."""
    _name = 'hms.medication.request'
    _description = 'Prescription'
    _order = 'id'

    encounter_id = fields.Many2one('hms.encounter', required=True, ondelete='cascade', index=True)
    patient_id = fields.Many2one(related='encounter_id.patient_id', store=True, index=True)
    company_id = fields.Many2one(related='encounter_id.company_id', store=True, index=True)
    medication_id = fields.Many2one('hms.medication', required=True)
    dose = fields.Float(required=True, default=1, digits=(6, 1))
    dose_unit = fields.Selection(DOSE_UNITS, required=True, default='tablet', string='Unit')
    route = fields.Selection([(k, v[0]) for k, v in ROUTES.items()], required=True, default='oral')
    frequency = fields.Selection([(k, v[0]) for k, v in FREQUENCIES.items()], required=True, default='tds')
    duration_days = fields.Integer(string='Days', default=5)
    quantity = fields.Float(string='Qty to Dispense', digits=(6, 0), compute='_compute_quantity',
                            store=True, readonly=False,
                            help='Calculated for tablets/capsules etc. You can overwrite it.')
    instructions = fields.Char(help='e.g. after meals')
    status = fields.Selection([
        ('active', 'Active'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
    ], default='active', required=True)
    authored_on = fields.Datetime(default=fields.Datetime.now, readonly=True)
    sig = fields.Char(string='Directions', compute='_compute_sig')

    @api.onchange('medication_id')
    def _onchange_medication(self):
        """Pick sensible defaults from the drug's form."""
        form = self.medication_id.form
        unit = {'tablet': 'tablet', 'capsule': 'capsule', 'syrup': 'ml', 'suspension': 'ml',
                'inhaler': 'puff', 'sachet': 'sachet', 'drops': 'drop', 'cream': 'application',
                'injection': 'mg'}.get(form)
        route = {'injection': 'im', 'cream': 'topical', 'inhaler': 'inhaled'}.get(form, 'oral')
        if unit:
            self.dose_unit = unit
        self.route = route

    @api.depends('dose', 'dose_unit', 'frequency', 'duration_days')
    def _compute_quantity(self):
        for rec in self:
            per_day = FREQUENCIES[rec.frequency][1] if rec.frequency else None
            if rec.dose_unit not in COUNTABLE:
                rec.quantity = 0
            elif rec.frequency == 'stat':
                rec.quantity = rec.dose
            elif per_day:
                rec.quantity = rec.dose * per_day * max(rec.duration_days, 0)
            # PRN ("when required"): no schedule to calculate from, so the prescriber enters the quantity

    @api.depends('dose', 'dose_unit', 'route', 'frequency', 'duration_days', 'instructions')
    def _compute_sig(self):
        units = dict(DOSE_UNITS)
        for rec in self:
            if not (rec.dose_unit and rec.frequency and rec.route):
                rec.sig = False
                continue
            parts = [f'{rec.dose:g} {units[rec.dose_unit]}', ROUTES[rec.route][0].lower(),
                     FREQUENCIES[rec.frequency][0].split(' (')[0].lower()]
            if rec.frequency not in ('stat',) and rec.duration_days:
                parts.append(f'for {rec.duration_days} days')
            if rec.instructions:
                parts.append(f'({rec.instructions})')
            rec.sig = ' '.join(parts)

    @api.depends('medication_id', 'sig')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = f'{rec.medication_id.display_name} — {rec.sig or ""}'

    @api.constrains('dose', 'duration_days', 'frequency', 'quantity', 'dose_unit')
    def _check_values(self):
        for rec in self:
            if rec.dose <= 0:
                raise ValidationError(self.env._('The dose must be greater than zero.'))
            if rec.frequency not in ('stat', 'prn') and rec.duration_days <= 0:
                raise ValidationError(self.env._('Enter how many days the patient should take %s.',
                                                 rec.medication_id.display_name))
            if rec.frequency == 'prn' and rec.dose_unit in COUNTABLE and rec.quantity <= 0:
                raise ValidationError(self.env._(
                    '%s is "when required", so enter the quantity to dispense (e.g. 10 tablets).',
                    rec.medication_id.display_name))

    @api.model_create_multi
    def create(self, vals_list):
        _check_encounter_open(self.env, self.env['hms.encounter'].browse(
            [v['encounter_id'] for v in vals_list if v.get('encounter_id')]))
        return super().create(vals_list)

    def write(self, vals):
        if set(vals) - {'invoice_line_id'}:  # billing may link a line after the visit closed
            _check_encounter_open(self.env, self.encounter_id)
        return super().write(vals)

    def unlink(self):
        _check_encounter_open(self.env, self.encounter_id)
        return super().unlink()

    def _fhir_dose_quantity(self, value):
        unit_label = dict(DOSE_UNITS)[self.dose_unit]
        code = UCUM_UNIT.get(self.dose_unit, '{%s}' % self.dose_unit)
        return {'value': value, 'unit': unit_label, 'system': UCUM, 'code': code}

    def to_fhir(self):
        self.ensure_one()
        med = self.medication_id
        per_day = FREQUENCIES[self.frequency][1]
        if self.frequency == 'stat':
            timing = {'repeat': {'count': 1}}
        elif per_day:
            timing = {'repeat': clean({
                'frequency': per_day, 'period': 1, 'periodUnit': 'd',
                'boundsDuration': {'value': self.duration_days, 'unit': 'days', 'system': UCUM, 'code': 'd'}
                if self.duration_days else None,
            })}
        else:
            timing = None
        route_label, route_code = ROUTES[self.route]
        dosage = clean({
            'text': self.sig,
            'patientInstruction': self.instructions,
            'timing': timing,
            'asNeededBoolean': True if self.frequency == 'prn' else None,
            'route': {'coding': [{'system': 'http://snomed.info/sct', 'code': route_code, 'display': route_label}]},
            'doseAndRate': [{'doseQuantity': self._fhir_dose_quantity(self.dose)}],
        })
        dispense = None
        if self.quantity:
            dispense = clean({
                'quantity': self._fhir_dose_quantity(self.quantity),
                'expectedSupplyDuration': {'value': self.duration_days, 'unit': 'days', 'system': UCUM, 'code': 'd'}
                if self.duration_days else None,
            })
        return clean({
            'resourceType': 'MedicationRequest',
            'id': str(self.id),
            'status': self.status,
            'intent': 'order',
            'medicationCodeableConcept': clean({
                'coding': [{'system': ATC_SYSTEM, 'code': med.atc_code, 'display': med.name}] if med.atc_code else None,
                'text': med.display_name,
            }),
            'subject': fhir_ref('Patient', self.patient_id),
            'encounter': fhir_ref('Encounter', self.encounter_id),
            'authoredOn': fhir_datetime(self.authored_on),
            'requester': fhir_ref('Practitioner', self.encounter_id.practitioner_id),
            'reasonReference': [fhir_ref('Condition', c) for c in self.encounter_id.condition_ids],
            'dosageInstruction': [dosage],
            'dispenseRequest': dispense,
        })
