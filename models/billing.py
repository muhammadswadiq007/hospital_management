from markupsafe import escape

from odoo import api, fields, models
from odoo.exceptions import UserError

# Drugs sold per tablet/capsule/sachet are billed by the dispensed quantity;
# everything else (a bottle of syrup, an inhaler, a vial) is billed per pack.
BILLED_PER_UNIT_FORMS = {'tablet', 'capsule', 'sachet'}

CONSULTATION_PRODUCTS = {
    'AMB': 'hospital_management.product_consult_outpatient',
    'EMER': 'hospital_management.product_consult_emergency',
    'IMP': 'hospital_management.product_consult_inpatient',
}

# Example prices (KES) filled in once for the starter catalogue; change them in Configuration.
DEFAULT_PRICES = {
    'hms.medication': {
        'med_paracetamol_500': 5, 'med_paracetamol_syrup': 150, 'med_ibuprofen_400': 10,
        'med_amoxicillin_500': 15, 'med_amoxicillin_susp': 250, 'med_al_20_120': 25,
        'med_ciprofloxacin_500': 20, 'med_metronidazole_400': 5, 'med_cotrimoxazole_960': 5,
        'med_ceftriaxone_1g': 350, 'med_albendazole_400': 50, 'med_ors': 30, 'med_zinc_20': 10,
        'med_omeprazole_20': 15, 'med_metformin_500': 5, 'med_amlodipine_5': 10, 'med_hctz_25': 5,
        'med_salbutamol_inh': 600, 'med_cetirizine_10': 10, 'med_ferrous_200': 5,
    },
    'hms.lab.test': {
        'lab_malaria_rdt': 300, 'lab_malaria_bs': 300, 'lab_hb': 300, 'lab_wbc': 400, 'lab_plt': 400,
        'lab_rbs': 200, 'lab_hba1c': 1500, 'lab_creatinine': 800, 'lab_hiv': 0, 'lab_pdt': 200,
    },
}


class _BillableMixin(models.AbstractModel):
    """Gives a catalogue item (drug / lab test) a price and a linked Odoo product for invoicing."""
    _name = 'hms.billable.mixin'
    _description = 'Billable catalogue item'

    list_price = fields.Float(string='Price', digits='Product Price')
    product_id = fields.Many2one('product.product', readonly=True, copy=False, ondelete='restrict')

    def _hms_product_vals(self):
        raise NotImplementedError

    def _get_product(self):
        """Create the linked product the first time it's billed; keep its price in sync."""
        self.ensure_one()
        vals = self._hms_product_vals()
        vals.update({'list_price': self.list_price, 'taxes_id': [(5, 0, 0)], 'sale_ok': True, 'purchase_ok': False})
        if self.product_id:
            if self.product_id.list_price != self.list_price:
                self.product_id.sudo().list_price = self.list_price
            return self.product_id
        product = self.env['product.product'].sudo().create(vals)
        self.sudo().product_id = product
        return product

    @api.model
    def _hms_fill_default_prices(self):
        """Called from data on every upgrade: only fills prices that are still empty."""
        for xmlid, price in DEFAULT_PRICES.get(self._name, {}).items():
            rec = self.env.ref(f'hospital_management.{xmlid}', raise_if_not_found=False)
            if rec and not rec.list_price and price:
                rec.list_price = price


class HmsMedicationBilling(models.Model):
    _name = 'hms.medication'
    _inherit = ['hms.medication', 'hms.billable.mixin']

    def _hms_product_vals(self):
        return {'name': self.display_name, 'type': 'consu', 'default_code': self.atc_code}


class HmsLabTestBilling(models.Model):
    _name = 'hms.lab.test'
    _inherit = ['hms.lab.test', 'hms.billable.mixin']

    def _hms_product_vals(self):
        return {'name': f'Lab: {self.name}', 'type': 'service', 'default_code': self.loinc_code}


class ResPartner(models.Model):
    _inherit = 'res.partner'

    is_hms_insurer = fields.Boolean(string='Medical Insurer')


class HmsPatientBilling(models.Model):
    _inherit = 'hms.patient'

    partner_id = fields.Many2one('res.partner', string='Billing Contact', readonly=True, copy=False)
    payer_type = fields.Selection([('cash', 'Cash / Self-pay'), ('insurance', 'Insurance')],
                                  string='Usually Pays By', default='cash', required=True)
    insurer_id = fields.Many2one('res.partner', string='Insurer', domain=[('is_hms_insurer', '=', True)])
    member_number = fields.Char(string='Member / Policy No.')

    def _get_partner(self):
        self.ensure_one()
        vals = {'name': self.name, 'phone': self.phone, 'email': self.email, 'ref': self.mrn}
        if self.partner_id:
            return self.partner_id
        partner = self.env['res.partner'].sudo().create(vals)
        self.sudo().partner_id = partner
        return partner


class HmsEncounterBilling(models.Model):
    _inherit = 'hms.encounter'

    invoice_id = fields.Many2one('account.move', string='Invoice', readonly=True, copy=False)
    payment_state = fields.Selection(related='invoice_id.payment_state', string='Payment')
    billing_state = fields.Selection([
        ('not_ready', 'Visit Not Finished'),
        ('to_invoice', 'To Invoice'),
        ('invoiced', 'Invoiced'),
    ], compute='_compute_billing_state', search='_search_billing_state')

    @api.depends('status', 'invoice_id.state')
    def _compute_billing_state(self):
        for rec in self:
            if rec.invoice_id and rec.invoice_id.state != 'cancel':
                rec.billing_state = 'invoiced'
            elif rec.status == 'finished':
                rec.billing_state = 'to_invoice'
            else:
                rec.billing_state = 'not_ready'

    def _search_billing_state(self, operator, value):
        if operator != 'in':
            return NotImplemented
        invoiced = ['&', ('invoice_id', '!=', False), ('invoice_id.state', '!=', 'cancel')]
        not_invoiced = ['|', ('invoice_id', '=', False), ('invoice_id.state', '=', 'cancel')]
        parts = []
        if 'invoiced' in value:
            parts.append(invoiced)
        if 'to_invoice' in value:
            parts.append(['&', ('status', '=', 'finished')] + not_invoiced)
        if 'not_ready' in value:
            parts.append(['&', ('status', '!=', 'finished')] + not_invoiced)
        if not parts:
            return [('id', '=', 0)]
        domain = parts[0]
        for p in parts[1:]:
            domain = ['|'] + domain + p
        return domain

    def _billing_lines(self):
        """What this visit costs: [(product, description, qty, unit price)]. Runs as sudo so the
        cashier never needs access to the clinical records themselves."""
        self.ensure_one()
        enc = self.sudo()
        lines = []
        consult = self.env.ref(CONSULTATION_PRODUCTS[enc.encounter_class])
        lines.append((consult, f'{consult.name} ({enc.practitioner_id.name})', 1, consult.lst_price))
        for med in enc.medication_request_ids.filtered(lambda m: m.status != 'cancelled'):
            drug = med.medication_id
            qty = (med.quantity or 1) if drug.form in BILLED_PER_UNIT_FORMS else 1
            lines.append((drug._get_product(), f'{drug.display_name} — {med.sig}', qty, drug.list_price))
        for lab in enc.lab_request_ids.filtered(lambda l: l.status != 'revoked'):
            lines.append((lab.test_id._get_product(), f'Lab: {lab.test_id.name} ({lab.name})', 1, lab.test_id.list_price))
        return lines

    def action_open_invoice_wizard(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.env._('Create Invoice'),
            'res_model': 'hms.invoice.wizard',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_encounter_id': self.id},
        }

    def action_view_invoice(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'account.move',
            'res_id': self.invoice_id.id,
            'view_mode': 'form',
        }


class AccountMove(models.Model):
    _inherit = 'account.move'

    hms_encounter_id = fields.Many2one('hms.encounter', string='Hospital Visit', readonly=True, copy=False, index='btree_not_null')
    hms_patient_id = fields.Many2one(related='hms_encounter_id.patient_id', string='Patient')


class HmsInvoiceWizard(models.TransientModel):
    _name = 'hms.invoice.wizard'
    _description = 'Create Visit Invoice'

    encounter_id = fields.Many2one('hms.encounter', required=True, readonly=True)
    patient_id = fields.Many2one('hms.patient', readonly=True)
    payer_type = fields.Selection([('cash', 'Cash / Self-pay'), ('insurance', 'Insurance')],
                                  required=True, default='cash')
    insurer_id = fields.Many2one('res.partner', domain=[('is_hms_insurer', '=', True)])
    member_number = fields.Char(string='Member / Policy No.')
    preview = fields.Html(compute='_compute_preview', sanitize=False)
    total = fields.Float(compute='_compute_preview')

    def _defaults_from_encounter(self, encounter):
        patient = encounter.sudo().patient_id
        return {
            'patient_id': patient.id,
            'payer_type': patient.payer_type or 'cash',
            'insurer_id': patient.insurer_id.id,
            'member_number': patient.member_number,
        }

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if res.get('encounter_id'):
            res.update({k: v for k, v in self._defaults_from_encounter(
                self.env['hms.encounter'].browse(res['encounter_id'])).items() if k in fields_list})
        return res

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('encounter_id'):
                for key, value in self._defaults_from_encounter(self.env['hms.encounter'].browse(vals['encounter_id'])).items():
                    vals.setdefault(key, value)
        return super().create(vals_list)

    @api.depends('encounter_id')
    def _compute_preview(self):
        for wiz in self:
            if not wiz.encounter_id:
                wiz.preview, wiz.total = False, 0
                continue
            lines = wiz.encounter_id._billing_lines()
            rows = ''.join(
                f'<tr><td>{escape(desc)}</td><td class="text-end">{qty:g}</td><td class="text-end">{price:,.2f}</td>'
                f'<td class="text-end">{qty * price:,.2f}</td></tr>' for _p, desc, qty, price in lines)
            wiz.total = sum(q * p for _x, _d, q, p in lines)
            wiz.preview = (
                '<table class="table table-sm"><thead><tr><th>Item</th><th class="text-end">Qty</th>'
                '<th class="text-end">Price</th><th class="text-end">Amount</th></tr></thead>'
                f'<tbody>{rows}</tbody><tfoot><tr><th colspan="3">Total</th>'
                f'<th class="text-end">{wiz.total:,.2f}</th></tr></tfoot></table>')

    def action_create_invoice(self):
        self.ensure_one()
        enc = self.encounter_id.sudo()
        if enc.status != 'finished':
            raise UserError(self.env._('Finish the visit before invoicing it.'))
        if enc.invoice_id and enc.invoice_id.state != 'cancel':
            raise UserError(self.env._('This visit has already been invoiced (%s).', enc.invoice_id.name or 'draft'))
        if self.payer_type == 'insurance' and not (self.insurer_id and self.member_number):
            raise UserError(self.env._('Choose the insurer and enter the member number.'))
        if not self.env['account.journal'].sudo().search_count(
                [('type', '=', 'sale'), ('company_id', 'in', self.env.companies.ids)], limit=1):
            raise UserError(self.env._(
                'Accounting is not set up yet. An administrator must go to Invoicing > Configuration > '
                'Settings, choose the Fiscal Localization (e.g. Kenya) and save. Then try again.'))
        patient = enc.patient_id
        bill_to = self.insurer_id if self.payer_type == 'insurance' else patient._get_partner()
        lines = enc._billing_lines()
        move = self.env['account.move'].create({
            'move_type': 'out_invoice',
            'partner_id': bill_to.id,
            'invoice_date': fields.Date.context_today(self),
            'invoice_origin': enc.name,
            'ref': self.member_number if self.payer_type == 'insurance' else patient.mrn,
            'narration': self.env._('Patient: %(name)s (%(mrn)s). Visit %(enc)s on %(date)s.',
                                    name=patient.name, mrn=patient.mrn, enc=enc.name,
                                    date=fields.Date.to_string(enc.period_start or enc.create_date)),
            'hms_encounter_id': enc.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': product.id,
                'name': desc,
                'quantity': qty,
                'price_unit': price,
                'tax_ids': [(5, 0, 0)],  # medical services and medicines: no VAT by default
            }) for product, desc, qty, price in lines],
        })
        enc.invoice_id = move
        enc.message_post(body=self.env._('Invoice created by %(user)s for %(payer)s: %(amount)s',
                                         user=self.env.user.name, payer=bill_to.name,
                                         amount=f'{move.amount_total:,.2f} {move.currency_id.name}'))
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'res_id': move.id, 'view_mode': 'form'}
