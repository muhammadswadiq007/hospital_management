from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .fhir_utils import clean, fhir_datetime, fhir_ref
from .observation import _check_encounter_open

LOINC = 'http://loinc.org'
UCUM = 'http://unitsofmeasure.org'
SNOMED = 'http://snomed.info/sct'
INTERPRETATION_SYSTEM = 'http://terminology.hl7.org/CodeSystem/v3-ObservationInterpretation'

# HL7 interpretation flags
INTERPRETATIONS = [
    ('N', 'Normal'),
    ('L', 'Low'),
    ('H', 'High'),
    ('POS', 'Positive'),
    ('NEG', 'Negative'),
]
QUALITATIVE = {
    'positive': ('Positive', '10828004', 'POS'),   # SNOMED CT code, interpretation flag
    'negative': ('Negative', '260385009', 'NEG'),
}
# Only the lab may write these
RESULT_FIELDS = {'result_value', 'result_qualitative', 'result_text', 'result_entered'}
LAB_MANAGED = RESULT_FIELDS | {'status', 'resulted_on', 'resulted_by_id', 'correction_count'}
# Only the ordering doctor (or an admin) may change these, and only while the test is still ordered
ORDER_FIELDS = {'test_id', 'priority', 'clinical_info', 'encounter_id'}
# Written by the system itself (billing link)
SYSTEM_FIELDS = {'invoice_line_id'}


class HmsLabTest(models.Model):
    """Lab test catalogue, each coded with LOINC."""
    _name = 'hms.lab.test'
    _description = 'Lab Test'
    _order = 'name'
    _rec_names_search = ['name', 'loinc_code']

    name = fields.Char(required=True)
    loinc_code = fields.Char(string='LOINC Code')
    loinc_display = fields.Char(string='LOINC Name')
    specimen = fields.Selection([
        ('blood', 'Blood'), ('serum', 'Serum / Plasma'), ('urine', 'Urine'), ('stool', 'Stool'), ('other', 'Other'),
    ], default='blood', required=True)
    result_type = fields.Selection([
        ('numeric', 'Numeric'),
        ('qualitative', 'Positive / Negative'),
    ], default='numeric', required=True)
    unit = fields.Char(help='Unit shown to users, e.g. g/dL')
    ucum_code = fields.Char(string='UCUM Unit Code', help='Machine-readable unit for FHIR, e.g. g/dL, mmol/L, 10*9/L')
    ref_low = fields.Float(string='Normal From', digits=(8, 2))
    ref_high = fields.Float(string='Normal To', digits=(8, 2))
    active = fields.Boolean(default=True)

    @api.depends('name', 'unit')
    def _compute_display_name(self):
        for rec in self:
            rec.display_name = rec.name


class HmsLabRequest(models.Model):
    """A lab order (FHIR ServiceRequest) and, once resulted, its report (FHIR DiagnosticReport)."""
    _name = 'hms.lab.request'
    _description = 'Lab Request'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(readonly=True, copy=False, default='New')
    encounter_id = fields.Many2one('hms.encounter', required=True, ondelete='cascade', index=True)
    patient_id = fields.Many2one(related='encounter_id.patient_id', store=True, index=True)
    company_id = fields.Many2one(related='encounter_id.company_id', store=True, index=True)
    practitioner_id = fields.Many2one(related='encounter_id.practitioner_id', store=True, string='Requested By')
    test_id = fields.Many2one('hms.lab.test', string='Test', required=True, tracking=True)
    result_type = fields.Selection(related='test_id.result_type')
    unit = fields.Char(related='test_id.unit')
    priority = fields.Selection([
        ('routine', 'Routine'), ('urgent', 'Urgent'), ('stat', 'STAT (immediate)'),
    ], default='routine', required=True, tracking=True)
    clinical_info = fields.Char(string='Clinical Info', help='What the lab should know, e.g. "fever 3 days"')
    status = fields.Selection([
        ('active', 'Ordered'),
        ('completed', 'Resulted'),
        ('revoked', 'Cancelled'),
    ], default='active', required=True, tracking=True)
    authored_on = fields.Datetime(string='Ordered On', default=fields.Datetime.now, readonly=True)

    # Result (lab staff only)
    result_value = fields.Float(string='Result Value', digits=(10, 2), tracking=True)
    result_qualitative = fields.Selection([(k, v[0]) for k, v in QUALITATIVE.items()], string='Result (Pos/Neg)', tracking=True)
    result_text = fields.Char(string='Comment', tracking=True, help='e.g. species seen, parasite density')
    interpretation = fields.Selection(INTERPRETATIONS, compute='_compute_interpretation', store=True)
    result_display = fields.Char(string='Result', compute='_compute_result_display')
    reference_range = fields.Char(compute='_compute_result_display')
    result_entered = fields.Boolean(readonly=True, copy=False,
                                    help='Set when a numeric result has actually been typed in (0 is not "empty")')
    resulted_on = fields.Datetime(readonly=True)
    resulted_by_id = fields.Many2one('res.users', string='Resulted By', readonly=True)
    correction_count = fields.Integer(string='Corrections', readonly=True, copy=False)

    @api.depends('status', 'result_type', 'result_value', 'result_qualitative', 'test_id.ref_low', 'test_id.ref_high')
    def _compute_interpretation(self):
        for rec in self:
            if rec.status != 'completed':
                rec.interpretation = False
            elif rec.result_type == 'qualitative':
                rec.interpretation = QUALITATIVE[rec.result_qualitative][2] if rec.result_qualitative else False
            else:
                low, high = rec.test_id.ref_low, rec.test_id.ref_high
                if (low or high) and rec.result_value < low:
                    rec.interpretation = 'L'
                elif high and rec.result_value > high:
                    rec.interpretation = 'H'
                else:
                    rec.interpretation = 'N'

    @api.depends('status', 'result_type', 'result_value', 'result_qualitative', 'result_text', 'test_id')
    def _compute_result_display(self):
        for rec in self:
            t = rec.test_id
            rec.reference_range = f'{t.ref_low:g}–{t.ref_high:g} {t.unit or ""}'.strip() \
                if t.result_type == 'numeric' and (t.ref_low or t.ref_high) else False
            if rec.status != 'completed':
                rec.result_display = False
            elif rec.result_type == 'qualitative':
                rec.result_display = QUALITATIVE[rec.result_qualitative][0] if rec.result_qualitative else False
            else:
                rec.result_display = f'{rec.result_value:g} {t.unit or ""}'.strip()
            if rec.result_display and rec.result_text:
                rec.result_display += f' ({rec.result_text})'

    def _is_lab(self):
        return self.env.su or self.env.user.has_group('hospital_management.group_hms_lab')

    @api.model_create_multi
    def create(self, vals_list):
        if not self._is_lab() and any(
                any(v.get(f) for f in RESULT_FIELDS | {'resulted_on', 'resulted_by_id'})
                or v.get('status', 'active') != 'active' for v in vals_list):
            raise AccessError(self.env._('Only laboratory staff can enter results.'))
        _check_encounter_open(self.env, self.env['hms.encounter'].browse(
            [v['encounter_id'] for v in vals_list if v.get('encounter_id')]))
        for vals in vals_list:
            if vals.get('name', 'New') == 'New':
                vals['name'] = self.env['ir.sequence'].next_by_code('hms.lab.request') or 'New'
        return super().create(vals_list)

    def _is_orderer(self):
        """Doctors (record-level access already limits them to their own patients) and admins."""
        return self.env.su or self.env.user.has_group('hospital_management.group_hms_doctor')

    def write(self, vals):
        keys = set(vals)
        ctx = self.env.context
        # Status only changes through the buttons (release / cancel / correct), never by direct edit
        if 'status' in keys and not ctx.get('hms_lab_action') and not self.env.su:
            raise AccessError(self.env._(
                'The status of a lab test changes only through Release Result or Cancel Test.'))
        if (LAB_MANAGED - {'status'}) & keys and not self._is_lab():
            raise AccessError(self.env._('Only laboratory staff can enter results.'))
        if RESULT_FIELDS & keys and not ctx.get('hms_lab_correction') \
                and any(r.status != 'active' for r in self):
            raise UserError(self.env._(
                'This result has been released. Use "Correct Result" to amend it with a reason.'))
        if ORDER_FIELDS & keys:
            if not self._is_orderer():
                raise AccessError(self.env._(
                    'Only the ordering doctor can change which test was ordered or its priority.'))
            if any(r.status != 'active' for r in self):
                raise UserError(self.env._('The order can only be changed while the test is still awaiting a result.'))
        if 'result_value' in keys:
            vals = dict(vals, result_entered=True)
        # Lab results arrive after the visit, so only the *order* details lock with the encounter
        if keys - LAB_MANAGED - SYSTEM_FIELDS:
            _check_encounter_open(self.env, self.encounter_id)
        return super().write(vals)

    def unlink(self):
        _check_encounter_open(self.env, self.encounter_id)
        return super().unlink()

    def action_complete(self):
        if not self._is_lab():
            raise AccessError(self.env._('Only laboratory staff can release results.'))
        for rec in self:
            if rec.status != 'active':
                raise UserError(self.env._('%s is not awaiting a result.', rec.name))
            if rec.result_type == 'qualitative' and not rec.result_qualitative:
                raise UserError(self.env._('Select Positive or Negative for %s.', rec.test_id.name))
            if rec.result_type == 'numeric' and not rec.result_entered:
                raise UserError(self.env._('Enter the result value for %s before releasing it.', rec.test_id.name))
        self.with_context(hms_lab_action=True).write(
            {'status': 'completed', 'resulted_on': fields.Datetime.now(), 'resulted_by_id': self.env.uid})
        for rec in self:
            flag = dict(INTERPRETATIONS).get(rec.interpretation, '')
            # Tell the doctor on the visit's chatter
            rec.encounter_id.sudo().message_post(
                body=self.env._('Lab result ready: %(test)s = %(result)s %(flag)s',
                                test=rec.test_id.name, result=rec.result_display,
                                flag=f'[{flag}]' if rec.interpretation not in ('N', 'NEG') else ''),
                partner_ids=rec.practitioner_id.user_id.partner_id.ids,
            )

    def action_cancel(self):
        if not (self._is_lab() or self._is_orderer()):
            raise AccessError(self.env._('Only the lab or the ordering doctor can cancel a test.'))
        self.check_access('write')  # a doctor can only cancel their own patients' tests
        for rec in self:
            if rec.status != 'active':
                raise UserError(self.env._('Only tests that are still ordered can be cancelled.'))
        self.with_context(hms_lab_action=True).write({'status': 'revoked'})
        for rec in self:
            rec.message_post(body=self.env._('Test cancelled by %s.', self.env.user.name))

    def action_open_correction_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': self.env._('Correct Released Result'),
            'res_model': 'hms.lab.correction.wizard', 'view_mode': 'form', 'target': 'new',
            'context': {'default_lab_request_id': self.id},
        }

    # ---------------- FHIR ----------------

    def _fhir_code(self):
        t = self.test_id
        return clean({
            'coding': [{'system': LOINC, 'code': t.loinc_code, 'display': t.loinc_display or t.name}] if t.loinc_code else None,
            'text': t.name,
        })

    def to_fhir(self):
        """FHIR ServiceRequest: the order."""
        self.ensure_one()
        return clean({
            'resourceType': 'ServiceRequest',
            'id': str(self.id),
            'identifier': [{'value': self.name}],
            'status': self.status,
            'intent': 'order',
            'priority': self.priority,
            'category': [{'coding': [{'system': SNOMED, 'code': '108252007', 'display': 'Laboratory procedure'}]}],
            'code': self._fhir_code(),
            'subject': fhir_ref('Patient', self.patient_id),
            'encounter': fhir_ref('Encounter', self.encounter_id),
            'authoredOn': fhir_datetime(self.authored_on),
            'requester': fhir_ref('Practitioner', self.practitioner_id),
            'reasonReference': [fhir_ref('Condition', c) for c in self.encounter_id.condition_ids],
            'note': [{'text': self.clinical_info}] if self.clinical_info else None,
        })

    def to_fhir_report(self):
        """FHIR DiagnosticReport: the released result, with the measured value as a contained Observation."""
        self.ensure_one()
        t = self.test_id
        obs = {
            'resourceType': 'Observation',
            'id': 'result',
            'status': 'corrected' if self.correction_count else 'final',
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/observation-category',
                                      'code': 'laboratory', 'display': 'Laboratory'}]}],
            'code': self._fhir_code(),
            'subject': fhir_ref('Patient', self.patient_id),
            'effectiveDateTime': fhir_datetime(self.resulted_on),
        }
        if self.result_type == 'qualitative':
            label, sct, _flag = QUALITATIVE[self.result_qualitative]
            obs['valueCodeableConcept'] = {'coding': [{'system': SNOMED, 'code': sct, 'display': label}], 'text': label}
        else:
            obs['valueQuantity'] = clean({'value': self.result_value, 'unit': t.unit,
                                          'system': UCUM if t.ucum_code else None, 'code': t.ucum_code})
            if t.ref_low or t.ref_high:
                obs['referenceRange'] = [{
                    'low': clean({'value': t.ref_low, 'unit': t.unit, 'system': UCUM if t.ucum_code else None, 'code': t.ucum_code}),
                    'high': clean({'value': t.ref_high, 'unit': t.unit, 'system': UCUM if t.ucum_code else None, 'code': t.ucum_code}),
                }]
        if self.interpretation:
            obs['interpretation'] = [{'coding': [{'system': INTERPRETATION_SYSTEM, 'code': self.interpretation,
                                                  'display': dict(INTERPRETATIONS)[self.interpretation]}]}]
        if self.result_text:
            obs['note'] = [{'text': self.result_text}]
        return clean({
            'resourceType': 'DiagnosticReport',
            'id': str(self.id),
            'identifier': [{'value': self.name}],
            'contained': [clean(obs)],
            'basedOn': [{'reference': f'ServiceRequest/{self.id}'}],
            'status': 'corrected' if self.correction_count else 'final',
            'category': [{'coding': [{'system': 'http://terminology.hl7.org/CodeSystem/v2-0074', 'code': 'LAB',
                                      'display': 'Laboratory'}]}],
            'code': self._fhir_code(),
            'subject': fhir_ref('Patient', self.patient_id),
            'encounter': fhir_ref('Encounter', self.encounter_id),
            'effectiveDateTime': fhir_datetime(self.resulted_on),
            'issued': self.resulted_on.strftime('%Y-%m-%dT%H:%M:%SZ') if self.resulted_on else None,
            'resultsInterpreter': [{'display': self.resulted_by_id.name}] if self.resulted_by_id else None,
            'result': [{'reference': '#result'}],
        })


class HmsLabCorrectionWizard(models.TransientModel):
    """Correct a released result. The old value stays in the history; the reason is kept permanently."""
    _name = 'hms.lab.correction.wizard'
    _description = 'Correct Lab Result'

    lab_request_id = fields.Many2one('hms.lab.request', required=True, readonly=True)
    result_type = fields.Selection(related='lab_request_id.result_type')
    unit = fields.Char(related='lab_request_id.unit')
    old_result = fields.Char(related='lab_request_id.result_display', string='Released Result')
    result_value = fields.Float(string='Correct Value', digits=(10, 2))
    result_qualitative = fields.Selection([(k, v[0]) for k, v in QUALITATIVE.items()], string='Correct Result')
    result_text = fields.Char(string='Comment')
    reason = fields.Text(required=True)

    def action_confirm(self):
        self.ensure_one()
        req = self.lab_request_id
        if not req._is_lab():
            raise AccessError(self.env._('Only laboratory staff can correct a result.'))
        if req.status != 'completed':
            raise UserError(self.env._('Only released results can be corrected.'))
        reason = (self.reason or '').strip()
        if len(reason) < 10:
            raise UserError(self.env._('Please give a proper reason (at least 10 characters).'))
        if req.result_type == 'qualitative' and not self.result_qualitative:
            raise UserError(self.env._('Select Positive or Negative.'))
        old = req.result_display
        vals = {'result_text': self.result_text or req.result_text,
                'correction_count': req.correction_count + 1,
                'resulted_on': fields.Datetime.now(), 'resulted_by_id': self.env.uid}
        if req.result_type == 'qualitative':
            vals['result_qualitative'] = self.result_qualitative
        else:
            vals['result_value'] = self.result_value
        req.with_context(hms_lab_correction=True, hms_lab_action=True).write(vals)
        msg = self.env._('Result CORRECTED by %(user)s: %(old)s → %(new)s. Reason: %(reason)s',
                         user=self.env.user.name, old=old, new=req.result_display, reason=reason)
        req.message_post(body=msg)
        req.encounter_id.sudo().message_post(body=msg, partner_ids=req.practitioner_id.user_id.partner_id.ids)
        self.env['hms.access.log']._log(req, 'lab_correction', patient_field='patient_id',
                                        detail=f'{old} → {req.result_display}. {reason}'[:250])
        return {'type': 'ir.actions.act_window_close'}
