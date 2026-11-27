import logging
import threading
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.http import request

from .fhir_utils import clean, fhir_datetime

ACTIONS = [
    ('view', 'Viewed (form)'),
    ('list', 'Listed'),
    ('read', 'Read (script / RPC)'),
    ('export', 'Exported'),
    ('api_read', 'API Read'),
    ('api_search', 'API Search'),
    ('api_export', 'Full Record Export (API)'),
    ('amend', 'Reopened for Amendment'),
    ('cancel', 'Visit Cancelled'),
    ('reassign', 'Moved to Another Doctor / Patient'),
    ('lab_correction', 'Lab Result Corrected'),
    ('delete', 'Patient Deleted'),
]
DEDUPE_SECONDS = 60

_logger = logging.getLogger(__name__)


def _channel():
    """Where the access came from: the Odoo screens, the FHIR API, an external script, or the server itself."""
    if not request:
        # Odoo hides the web request during XML-RPC / JSON-RPC calls, but marks the thread with the
        # method being called (and resets that mark at the start of every request).
        return 'rpc' if getattr(threading.current_thread(), 'rpc_model_method', None) else 'system'
    path = request.httprequest.path or ''
    if path.startswith('/fhir/'):
        return 'api'
    if path.startswith('/web/'):
        return 'ui'
    return 'rpc'  # /xmlrpc, /jsonrpc, /json/2 ... = an external program


class HmsAccessLog(models.Model):
    """Append-only record of who touched which patient's data (maps to FHIR AuditEvent).

    Rows are only ever created by the system (with sudo); nobody can edit or delete them.
    Each row keeps its own copy of the patient's name and MRN, so the trail survives even if the
    patient (or user) record is later deleted, and it never blocks such a deletion.
    """
    _name = 'hms.access.log'
    _description = 'Patient Data Access Log'
    _order = 'timestamp desc, id desc'

    timestamp = fields.Datetime(default=fields.Datetime.now, required=True, readonly=True, index=True)
    company_id = fields.Many2one('res.company', readonly=True, index=True, ondelete='set null')
    user_id = fields.Many2one('res.users', readonly=True, index=True, ondelete='set null')
    user_login = fields.Char(string='Login', readonly=True)
    # Plain values, deliberately NOT a database link: deleting a patient never touches (or is blocked by) the log
    patient_id = fields.Integer(string='Patient ID', readonly=True, index=True)
    patient_name = fields.Char(readonly=True)
    patient_mrn = fields.Char(string='MRN', readonly=True, index=True)
    res_model = fields.Char(string='Record Type', readonly=True)
    res_id = fields.Integer(string='Record ID', readonly=True)
    record_name = fields.Char(string='Record', readonly=True)
    action = fields.Selection(ACTIONS, required=True, readonly=True)
    channel = fields.Selection([
        ('ui', 'Odoo Screen'), ('api', 'FHIR API'), ('rpc', 'External Script / RPC'), ('system', 'Server'),
    ], required=True, readonly=True)
    ip_address = fields.Char(string='IP Address', readonly=True)
    detail = fields.Char(readonly=True)

    def write(self, vals):
        raise UserError(self.env._('The access log cannot be changed.'))

    def unlink(self):
        raise UserError(self.env._('The access log cannot be deleted.'))

    @api.model
    def _log(self, records, action, channel=None, patient_field=None, detail=None, dedupe_seconds=0):
        """Create one log line per record. Safe to call from anywhere (uses sudo)."""
        if not records:
            return
        log = self.sudo()
        channel = channel or _channel()
        ip = request.httprequest.remote_addr if request else None
        recent = set()
        if dedupe_seconds:
            since = fields.Datetime.now() - timedelta(seconds=dedupe_seconds)
            recent = set(log.search([
                ('user_id', '=', self.env.uid), ('res_model', '=', records._name), ('res_id', 'in', records.ids),
                ('action', '=', action), ('timestamp', '>=', since),
            ]).mapped('res_id'))
        user = self.env.user
        vals_list = []
        for rec in records.sudo():
            if rec.id in recent:
                continue  # same person re-opening the same record seconds later (e.g. after saving)
            if rec._name == 'hms.patient':
                patient = rec
            elif patient_field:
                patient = rec[patient_field]
            else:
                patient = rec['patient_id'] if 'patient_id' in rec._fields else rec.browse()
            vals_list.append({
                'user_id': user.id,
                'user_login': user.login,
                'company_id': (patient.company_id or self.env.company).id,
                'patient_id': patient.id or False,
                'patient_name': patient.name,
                'patient_mrn': patient.mrn,
                'res_model': rec._name,
                'res_id': rec.id,
                'record_name': rec.display_name,
                'action': action,
                'channel': channel,
                'ip_address': ip,
                'detail': detail,
            })
        if vals_list:
            self._insert(vals_list)

    @api.model
    def _insert(self, vals_list):
        """Write log rows in their own short transaction: this works even inside read-only requests,
        and the access stays recorded even if the user's own action later fails and rolls back.
        Uses the current transaction instead when the user was created in it and is not committed yet."""
        user_ids = {v['user_id'] for v in vals_list if v.get('user_id')}
        try:
            with self.env.registry.cursor() as cr:
                cr.execute("SET LOCAL lock_timeout = '2s'")
                visible = True
                if user_ids:
                    cr.execute("SELECT count(*) FROM res_users WHERE id IN %s", [tuple(user_ids)])
                    visible = cr.fetchone()[0] == len(user_ids)
                if visible:
                    self.env(cr=cr, su=True)['hms.access.log'].create(vals_list)
                    return
        except Exception:  # noqa: BLE001
            _logger.warning('Access log: separate transaction failed, using the current one', exc_info=True)
        self.sudo().create(vals_list)

    def to_fhir(self):
        """FHIR R4 AuditEvent."""
        self.ensure_one()
        interaction = {'view': 'read', 'list': 'search-type', 'read': 'read', 'export': 'read',
                       'api_read': 'read', 'api_search': 'search-type', 'api_export': 'read',
                       'amend': 'update', 'cancel': 'update', 'reassign': 'update', 'lab_correction': 'update',
                       'delete': 'delete'}[self.action]
        fhir_type = {'hms.patient': 'Patient', 'hms.encounter': 'Encounter'}.get(self.res_model)
        return clean({
            'resourceType': 'AuditEvent',
            'id': str(self.id),
            'type': {'system': 'http://terminology.hl7.org/CodeSystem/audit-event-type',
                     'code': 'rest', 'display': 'RESTful Operation'},
            'subtype': [{'system': 'http://hl7.org/fhir/restful-interaction', 'code': interaction}],
            'action': {'update': 'U', 'delete': 'D'}.get(interaction, 'R'),
            'recorded': fhir_datetime(self.timestamp),
            'outcome': '0',
            'outcomeDesc': self.detail,
            'agent': [clean({
                'who': {'display': self.user_id.name or self.user_login, 'identifier': {'value': self.user_login}},
                'requestor': True,
                'network': {'address': self.ip_address, 'type': '2'} if self.ip_address else None,
            })],
            'source': {'observer': {'display': 'Odoo Hospital Management'},
                       'type': [{'system': 'http://terminology.hl7.org/CodeSystem/security-source-type',
                                 'code': '4', 'display': 'Application Server'}]},
            'entity': [clean({
                'what': ({'reference': f'{fhir_type}/{self.res_id}', 'display': self.record_name}
                         if fhir_type and self.action != 'delete' else {'display': self.record_name}),
                'type': {'system': 'http://terminology.hl7.org/CodeSystem/audit-entity-type', 'code': '2',
                         'display': 'System Object'},
                'name': f'{self.patient_name} ({self.patient_mrn})' if self.patient_mrn else None,
            })],
        })


class HmsAuditedMixin(models.AbstractModel):
    """Logs every way a user can read patient data: forms, lists, exports, read()/search_read() from
    external scripts. Internal reads by the system (sudo) are not user access and are not logged.
    One user action is logged once (nested reads inside it are skipped)."""
    _name = 'hms.audited.mixin'
    _description = 'Patient data access logging'

    def _hms_audit(self, action):
        if self and not self.env.su and not self.env.context.get('hms_audit_done'):
            self.env['hms.access.log']._log(self, action, dedupe_seconds=DEDUPE_SECONDS)

    def _hms_quiet(self):
        return self.with_context(hms_audit_done=True)

    def web_read(self, specification):
        self._hms_audit('view' if len(self) == 1 else 'list')
        return super(HmsAuditedMixin, self._hms_quiet()).web_read(specification)

    @api.model
    def web_search_read(self, domain, specification, offset=0, limit=None, order=None, count_limit=None):
        res = super(HmsAuditedMixin, self._hms_quiet()).web_search_read(
            domain, specification, offset=offset, limit=limit, order=order, count_limit=count_limit)
        self.browse([r['id'] for r in res.get('records', [])])._hms_audit('list')
        return res

    def read(self, fields=None, load='_classic_read'):
        self._hms_audit('read')
        return super(HmsAuditedMixin, self._hms_quiet()).read(fields, load)

    @api.model
    def search_read(self, domain=None, fields=None, offset=0, limit=None, order=None, **read_kwargs):
        res = super(HmsAuditedMixin, self._hms_quiet()).search_read(
            domain, fields, offset=offset, limit=limit, order=order, **read_kwargs)
        self.browse([r['id'] for r in res])._hms_audit('read')
        return res

    def export_data(self, fields_to_export):
        self._hms_audit('export')
        return super(HmsAuditedMixin, self._hms_quiet()).export_data(fields_to_export)


# Every model that holds patient data gets access logging
class HmsPatientAudited(models.Model):
    _name = 'hms.patient'
    _inherit = ['hms.patient', 'hms.audited.mixin']


class HmsAppointmentAudited(models.Model):
    _name = 'hms.appointment'
    _inherit = ['hms.appointment', 'hms.audited.mixin']


class HmsEncounterAudited(models.Model):
    _name = 'hms.encounter'
    _inherit = ['hms.encounter', 'hms.audited.mixin']

    amendment_count = fields.Integer(readonly=True, copy=False)

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


class HmsObservationAudited(models.Model):
    _name = 'hms.observation'
    _inherit = ['hms.observation', 'hms.audited.mixin']


class HmsConditionAudited(models.Model):
    _name = 'hms.condition'
    _inherit = ['hms.condition', 'hms.audited.mixin']


class HmsMedicationRequestAudited(models.Model):
    _name = 'hms.medication.request'
    _inherit = ['hms.medication.request', 'hms.audited.mixin']


class HmsLabRequestAudited(models.Model):
    _name = 'hms.lab.request'
    _inherit = ['hms.lab.request', 'hms.audited.mixin']


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
        enc.with_context(hms_amend=True).write(
            {'status': 'in-progress', 'period_end': False, 'amendment_count': enc.amendment_count + 1})
        enc.message_post(body=self.env._(
            'Reopened for amendment #%(n)s by %(user)s. Reason: %(reason)s',
            n=enc.amendment_count, user=self.env.user.name, reason=reason))
        self.env['hms.access.log']._log(enc, 'amend', patient_field='patient_id', detail=reason[:250])
        return {'type': 'ir.actions.act_window_close'}


class ResUsersApikeysDescription(models.TransientModel):
    """Adds a dedicated 'FHIR API' scope to Odoo's built-in API keys (least privilege)."""
    _inherit = 'res.users.apikeys.description'

    scope = fields.Selection(selection_add=[('fhir', 'FHIR API')], ondelete={'fhir': 'cascade'})
