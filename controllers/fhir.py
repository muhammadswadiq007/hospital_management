import json
from urllib.parse import urlencode

from odoo import http
from odoo.exceptions import AccessError
from odoo.http import request

from ..models.observation import VITALS

DEFAULT_COUNT = 50   # results per page when the client doesn't ask
MAX_COUNT = 500

FHIR_JSON = [('Content-Type', 'application/fhir+json; charset=utf-8')]

# FHIR resource type -> Odoo model, plus the FHIR search parameters we support
# and how each maps to an Odoo domain.
RESOURCES = {
    'Patient': {
        'model': 'hms.patient',
        'search': {
            'family': ('string', lambda v: [('family_name', 'ilike', v)]),
            'given': ('string', lambda v: [('given_name', 'ilike', v)]),
            'identifier': ('token', lambda v: [('mrn', '=', v)]),
            'gender': ('token', lambda v: [('gender', '=', v)]),
        },
    },
    'Practitioner': {
        'model': 'hms.practitioner',
        'search': {
            'name': ('string', lambda v: [('name', 'ilike', v)]),
            'identifier': ('token', lambda v: [('license_number', '=', v)]),
        },
    },
    'Appointment': {
        'model': 'hms.appointment',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'practitioner': ('reference', lambda v: [('practitioner_id', '=', _ref_id(v))]),
            'status': ('token', lambda v: [('status', '=', v)]),
        },
    },
    'Encounter': {
        'model': 'hms.encounter',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'practitioner': ('reference', lambda v: [('practitioner_id', '=', _ref_id(v))]),
            'status': ('token', lambda v: [('status', '=', v)]),
        },
    },
    'Observation': {
        'model': 'hms.observation',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'encounter': ('reference', lambda v: [('encounter_id', '=', _ref_id(v))]),
            'code': ('token', lambda v: [('code', 'in', _vital_keys_for_loinc(v))]),
            'category': ('token', lambda v: [] if _code_part(v) == 'vital-signs' else [('id', '=', 0)]),
        },
    },
    'Condition': {
        'model': 'hms.condition',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'encounter': ('reference', lambda v: [('encounter_id', '=', _ref_id(v))]),
            'code': ('token', lambda v: [('icd10_id.code', '=', _code_part(v))]),
            'clinical-status': ('token', lambda v: [('clinical_status', '=', _code_part(v))]),
        },
    },
    'MedicationRequest': {
        'model': 'hms.medication.request',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'encounter': ('reference', lambda v: [('encounter_id', '=', _ref_id(v))]),
            'status': ('token', lambda v: [('status', '=', _code_part(v))]),
            'code': ('token', lambda v: [('medication_id.atc_code', '=', _code_part(v))]),
        },
    },
    'ServiceRequest': {
        'model': 'hms.lab.request',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'encounter': ('reference', lambda v: [('encounter_id', '=', _ref_id(v))]),
            'status': ('token', lambda v: [('status', '=', _code_part(v))]),
            'code': ('token', lambda v: [('test_id.loinc_code', '=', _code_part(v))]),
        },
    },
    'DiagnosticReport': {
        'model': 'hms.lab.request',
        'method': 'to_fhir_report',
        'domain': [('status', '=', 'completed')],  # only released results are reports
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'encounter': ('reference', lambda v: [('encounter_id', '=', _ref_id(v))]),
            'code': ('token', lambda v: [('test_id.loinc_code', '=', _code_part(v))]),
        },
    },
    'AuditEvent': {
        'model': 'hms.access.log',
        'search': {
            'patient': ('reference', lambda v: [('patient_id', '=', _ref_id(v))]),
            'agent': ('string', lambda v: [('user_login', '=', v)]),
        },
    },
}

# Everything that belongs to one patient, for Patient/<id>/$everything
EVERYTHING = [
    ('Encounter', 'hms.encounter', 'to_fhir'),
    ('Appointment', 'hms.appointment', 'to_fhir'),
    ('Observation', 'hms.observation', 'to_fhir'),
    ('Condition', 'hms.condition', 'to_fhir'),
    ('MedicationRequest', 'hms.medication.request', 'to_fhir'),
    ('ServiceRequest', 'hms.lab.request', 'to_fhir'),
    ('DiagnosticReport', 'hms.lab.request', 'to_fhir_report'),
]


def _code_part(token):
    """FHIR tokens may be 'system|code' or just 'code'."""
    return token.rsplit('|', 1)[-1]


def _vital_keys_for_loinc(token):
    code = _code_part(token)
    return [k for k, v in VITALS.items() if v['loinc'] == code]


def _ref_id(value):
    """Accept '7' or 'Patient/7'; return 7 (or 0, which matches nothing)."""
    tail = value.rsplit('/', 1)[-1]
    return int(tail) if tail.isdigit() else 0


def _audit(records, action, detail):
    """Log access to patient data (one line per patient touched)."""
    if records._name == 'hms.access.log':
        return
    if records._name in ('hms.patient', 'hms.encounter'):
        targets = records
        field = None if records._name == 'hms.patient' else 'patient_id'
    else:
        targets = records.sudo().mapped('patient_id')  # vitals, labs etc. -> log against the patient
        field = None
    request.env['hms.access.log']._log(targets, action, 'api', patient_field=field, detail=detail)


def _fhir_response(body, status=200):
    return request.make_response(json.dumps(body, indent=2), headers=FHIR_JSON, status=status)


def _operation_outcome(code, text, status):
    """FHIR's standard error format."""
    return _fhir_response({
        'resourceType': 'OperationOutcome',
        'issue': [{'severity': 'error', 'code': code, 'diagnostics': text}],
    }, status=status)


class FhirController(http.Controller):
    # auth='bearer': send "Authorization: Bearer <API key>" (a key with scope 'FHIR API'),
    # or open the URL in a browser where you're logged in. Either way Odoo's normal
    # access rights apply, so a receptionist calling /Encounter gets a 403 just like in the UI.

    @http.route('/fhir/R4/metadata', type='http', auth='bearer', bearer_scope='fhir', methods=['GET'])
    def capability_statement(self, **kw):
        return _fhir_response({
            'resourceType': 'CapabilityStatement',
            'status': 'active',
            'date': '2026-11-26',
            'kind': 'instance',
            'fhirVersion': '4.0.1',
            'format': ['application/fhir+json'],
            'rest': [{
                'mode': 'server',
                'security': {
                    'description': 'Send an Odoo API key with scope "FHIR API" as "Authorization: Bearer <key>". '
                                   'Results are limited by the user\'s hospital role, and every access is logged '
                                   '(see AuditEvent).',
                },
                'operation': [{'name': 'everything',
                               'definition': 'http://hl7.org/fhir/OperationDefinition/Patient-everything'}],
                'resource': [{
                    'type': rtype,
                    'interaction': [{'code': 'read'}, {'code': 'search-type'}],
                    'searchParam': [{'name': '_id', 'type': 'token'}] + [
                        {'name': name, 'type': ptype} for name, (ptype, _fn) in conf['search'].items()],
                } for rtype, conf in RESOURCES.items()],
            }],
        })

    @http.route('/fhir/R4/Patient/<int:rid>/$everything', type='http', auth='bearer', bearer_scope='fhir', methods=['GET'])
    def patient_everything(self, rid, **kw):
        """The patient's complete record in one Bundle (e.g. a data-access request or a transfer)."""
        try:
            patient = request.env['hms.patient'].browse(rid).exists()
            if not patient:
                return _operation_outcome('not-found', f'Patient/{rid} not found', 404)
            base = request.httprequest.host_url
            entries = [{'fullUrl': f'{base}fhir/R4/Patient/{rid}', 'resource': patient.to_fhir()}]
            practitioners = request.env['hms.practitioner']
            for rtype, model, method in EVERYTHING:
                domain = [('patient_id', '=', rid)]
                if rtype == 'DiagnosticReport':
                    domain.append(('status', '=', 'completed'))
                for rec in request.env[model].search(domain):
                    entries.append({'fullUrl': f'{base}fhir/R4/{rtype}/{rec.id}', 'resource': getattr(rec, method)()})
                    if 'practitioner_id' in rec._fields:
                        practitioners |= rec.practitioner_id
            for prac in practitioners:
                entries.append({'fullUrl': f'{base}fhir/R4/Practitioner/{prac.id}', 'resource': prac.to_fhir()})
            request.env['hms.access.log']._log(patient, 'api_export', 'api', detail=f'$everything ({len(entries)} resources)')
            return _fhir_response({'resourceType': 'Bundle', 'type': 'searchset', 'total': len(entries), 'entry': entries})
        except AccessError:
            return _operation_outcome('forbidden', f'You are not allowed to export Patient/{rid}', 403)

    @http.route('/fhir/R4/<string:rtype>/<int:rid>', type='http', auth='bearer', bearer_scope='fhir', methods=['GET'])
    def read(self, rtype, rid, **kw):
        if rtype not in RESOURCES:
            return _operation_outcome('not-supported', f'Resource type {rtype} is not supported', 404)
        try:
            conf = RESOURCES[rtype]
            record = request.env[conf['model']].search([('id', '=', rid)] + conf.get('domain', []))
            if not record:
                return _operation_outcome('not-found', f'{rtype}/{rid} not found', 404)
            body = getattr(record, conf.get('method', 'to_fhir'))()
            _audit(record, 'api_read', f'GET {rtype}/{rid}')
            return _fhir_response(body)
        except AccessError:
            return _operation_outcome('forbidden', f'You are not allowed to read {rtype}/{rid}', 403)

    @http.route('/fhir/R4/<string:rtype>', type='http', auth='bearer', bearer_scope='fhir', methods=['GET'])
    def search(self, rtype, **params):
        if rtype not in RESOURCES:
            return _operation_outcome('not-supported', f'Resource type {rtype} is not supported', 404)
        conf = RESOURCES[rtype]
        strict = 'handling=strict' in (request.httprequest.headers.get('Prefer') or '').replace(' ', '')

        # Result parameters (FHIR: _count, _id; plus _offset for the paging links we generate)
        try:
            count = int(params.pop('_count', DEFAULT_COUNT))
            offset = int(params.pop('_offset', 0))
        except ValueError:
            return _operation_outcome('invalid', '_count and _offset must be whole numbers', 400)
        count = max(0, min(count, MAX_COUNT))
        offset = max(0, offset)
        id_param = params.pop('_id', None)

        domain = list(conf.get('domain', []))
        if id_param:
            ids = [int(i) for i in id_param.split(',') if i.strip().isdigit()]
            domain.append(('id', 'in', ids))
        ignored = []
        for name, value in params.items():
            if name in conf['search']:
                domain += conf['search'][name][1](value)
            else:
                ignored.append(name)
        if ignored and strict:
            return _operation_outcome('not-supported', f'Unsupported search parameter(s): {", ".join(sorted(ignored))}', 400)

        try:
            Model = request.env[conf['model']]
            total = Model.search_count(domain)
            records = Model.search(domain, limit=count, offset=offset, order='id') if count else Model.browse()
            if records:
                _audit(records, 'api_search', f'GET {rtype}?{request.httprequest.query_string.decode()[:200]}')
            base = f'{request.httprequest.host_url}fhir/R4/{rtype}'

            def page_url(page_offset):
                query = {k: v for k, v in params.items() if k not in ignored}
                if id_param:
                    query['_id'] = id_param
                query.update({'_count': count, '_offset': page_offset})
                return f'{base}?{urlencode(query)}'

            links = [{'relation': 'self', 'url': page_url(offset)}]
            if count and offset + count < total:
                links.append({'relation': 'next', 'url': page_url(offset + count)})
            if offset > 0:
                links.append({'relation': 'previous', 'url': page_url(max(0, offset - count))})
            entries = [{
                'fullUrl': f'{base}/{r.id}',
                'resource': getattr(r, conf.get('method', 'to_fhir'))(),
                'search': {'mode': 'match'},
            } for r in records]
            if ignored:
                entries.append({'resource': {
                    'resourceType': 'OperationOutcome',
                    'issue': [{'severity': 'warning', 'code': 'not-supported',
                               'diagnostics': f'Ignored unsupported search parameter(s): {", ".join(sorted(ignored))}'}],
                }, 'search': {'mode': 'outcome'}})
            return _fhir_response({
                'resourceType': 'Bundle',
                'type': 'searchset',
                'total': total,
                'link': links,
                'entry': entries,
            })
        except AccessError:
            return _operation_outcome('forbidden', f'You are not allowed to search {rtype}', 403)
