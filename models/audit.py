from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.http import request

from .fhir_utils import clean, fhir_datetime


class HmsAccessLog(models.Model):
    """Append-only record of who looked at which patient's data (maps to FHIR AuditEvent).

    Rows are only ever created by the system (with sudo); nobody can edit or delete them.
    """
    _name = 'hms.access.log'
    _description = 'Patient Data Access Log'
    _order = 'timestamp desc, id desc'

    timestamp = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    user_id = fields.Many2one('res.users', required=True, readonly=True, index=True, ondelete='restrict')
    patient_id = fields.Many2one('hms.patient', readonly=True, index=True, ondelete='restrict')
    res_model = fields.Char(string='Record Type', readonly=True)
    res_id = fields.Integer(string='Record ID', readonly=True)
    record_name = fields.Char(string='Record', readonly=True)
    action = fields.Selection([
        ('view', 'Viewed'),
        ('api_read', 'API Read'),
        ('api_search', 'API Search'),
        ('export', 'Full Record Export'),
        ('amend', 'Reopened for Amendment'),
    ], required=True, readonly=True)
    channel = fields.Selection([('ui', 'Odoo Screen'), ('api', 'FHIR API')], required=True, readonly=True)
    ip_address = fields.Char(string='IP Address', readonly=True)
    detail = fields.Char(readonly=True)

    def write(self, vals):
        raise UserError(self.env._('The access log cannot be changed.'))

    def unlink(self):
        raise UserError(self.env._('The access log cannot be deleted.'))

    @api.model
    def _log(self, records, action, channel, patient_field=None, detail=None, dedupe_seconds=0):
        """Create one log line per record. Safe to call from anywhere (uses sudo)."""
        if not records:
            return
        ip = request.httprequest.remote_addr if request else None
        log = self.sudo()
        since = fields.Datetime.now() - timedelta(seconds=dedupe_seconds) if dedupe_seconds else None
        vals_list = []
        for rec in records:
            patient = rec if rec._name == 'hms.patient' else (rec[patient_field] if patient_field else False)
            if since and log.search_count([
                ('user_id', '=', self.env.uid), ('res_model', '=', rec._name), ('res_id', '=', rec.id),
                ('action', '=', action), ('timestamp', '>=', since),
            ], limit=1):
                continue  # same person re-opening the same record seconds later (e.g. after saving)
            vals_list.append({
                'user_id': self.env.uid,
                'patient_id': patient.id if patient else False,
                'res_model': rec._name,
                'res_id': rec.id,
                'record_name': rec.sudo().display_name,
                'action': action,
                'channel': channel,
                'ip_address': ip,
                'detail': detail,
            })
        if vals_list:
            log.create(vals_list)

    def to_fhir(self):
        """FHIR R4 AuditEvent."""
        self.ensure_one()
        interaction = {'view': 'read', 'api_read': 'read', 'api_search': 'search-type',
                       'export': 'read', 'amend': 'update'}[self.action]
        fhir_type = 'Patient' if self.res_model == 'hms.patient' else 'Encounter'
        return clean({
            'resourceType': 'AuditEvent',
            'id': str(self.id),
            'type': {'system': 'http://terminology.hl7.org/CodeSystem/audit-event-type',
                     'code': 'rest', 'display': 'RESTful Operation'},
            'subtype': [{'system': 'http://hl7.org/fhir/restful-interaction', 'code': interaction}],
            'action': 'U' if self.action == 'amend' else 'R',
            'recorded': fhir_datetime(self.timestamp),
            'outcome': '0',
            'outcomeDesc': self.detail,
            'agent': [clean({
                'who': {'display': self.user_id.name, 'identifier': {'value': self.user_id.login}},
                'requestor': True,
                'network': {'address': self.ip_address, 'type': '2'} if self.ip_address else None,
            })],
            'source': {'observer': {'display': 'Odoo Hospital Management'},
                       'type': [{'system': 'http://terminology.hl7.org/CodeSystem/security-source-type',
                                 'code': '4', 'display': 'Application Server'}]},
            'entity': [clean({
                'what': {'reference': f'{fhir_type}/{self.res_id}', 'display': self.record_name}
                if self.res_model in ('hms.patient', 'hms.encounter') else {'display': self.record_name},
                'type': {'system': 'http://terminology.hl7.org/CodeSystem/audit-entity-type', 'code': '2',
                         'display': 'System Object'},
            })],
        })


class HmsPatientAudit(models.Model):
    _inherit = 'hms.patient'

    def web_read(self, specification):
        # A single-record read from the web client = someone opened the patient's form
        if len(self) == 1:
            self.env['hms.access.log']._log(self, 'view', 'ui', dedupe_seconds=60)
        return super().web_read(specification)


class HmsEncounterAudit(models.Model):
    _inherit = 'hms.encounter'

    amendment_count = fields.Integer(readonly=True, copy=False)

    def web_read(self, specification):
        if len(self) == 1:
            self.env['hms.access.log']._log(self, 'view', 'ui', patient_field='patient_id', dedupe_seconds=60)
        return super().web_read(specification)

    def action_open_amend_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Reopen for Amendment'),
            'res_model': 'hms.encounter.amend.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_encounter_id': self.id},
        }


class HmsEncounterAmendWizard(models.TransientModel):
    _name = 'hms.encounter.amend.wizard'
    _description = 'Reopen Encounter for Amendment'

    encounter_id = fields.Many2one('hms.encounter', required=True, readonly=True)
    reason = fields.Text(required=True, help='Why does this closed record need to change? This is kept permanently.')

    def action_confirm(self):
        self.ensure_one()
        if not self.env.user.has_group('hospital_management.group_hms_admin'):
            raise UserError(self.env._('Only a hospital administrator can reopen a closed encounter.'))
        enc = self.encounter_id
        if enc.status != 'finished':
            raise UserError(self.env._('Only finished encounters can be reopened.'))
        reason = (self.reason or '').strip()
        if len(reason) < 10:
            raise UserError(self.env._('Please give a proper reason (at least 10 characters).'))
        enc.write({'status': 'in-progress', 'period_end': False, 'amendment_count': enc.amendment_count + 1})
        enc.message_post(body=self.env._(
            'Reopened for amendment #%(n)s by %(user)s. Reason: %(reason)s',
            n=enc.amendment_count, user=self.env.user.name, reason=reason))
        self.env['hms.access.log']._log(enc, 'amend', 'ui', patient_field='patient_id', detail=reason[:250])
        return {'type': 'ir.actions.act_window_close'}


class ResUsersApikeysDescription(models.TransientModel):
    """Adds a dedicated 'FHIR API' scope to Odoo's built-in API keys (least privilege)."""
    _inherit = 'res.users.apikeys.description'

    scope = fields.Selection(selection_add=[('fhir', 'FHIR API')], ondelete={'fhir': 'cascade'})
