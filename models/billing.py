from markupsafe import escape

from odoo import api, fields, models
from odoo.exceptions import UserError

# Drugs sold per tablet/capsule/sachet are billed by the dispensed quantity;
# everything else (a bottle of syrup, an inhaler, a vial) is billed per pack.
BILLED_PER_UNIT_FORMS = {'tablet', 'capsule', 'sachet'}

CONSULTATION_PRODUCTS = {
    'AMB': 'product_consult_outpatient',
    'EMER': 'product_consult_emergency',
    'IMP': 'product_consult_inpatient',
}

# Example prices, in Kenyan shillings. Only ever loaded into a company whose currency is KES.
EXAMPLE_PRICES_CURRENCY = 'KES'
EXAMPLE_PRICES = {
    'product.product': {
        'product_consult_outpatient': 1000, 'product_consult_emergency': 2000, 'product_consult_inpatient': 3000,
    },
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


def _is_billed(line):
    """An item counts as billed while its invoice line exists on a live (not cancelled / reversed) invoice."""
    return bool(line) and line.move_id.state != 'cancel' and line.move_id.payment_state != 'reversed'


class HmsBillableMixin(models.AbstractModel):
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
        if self.product_id:
            if self.product_id.list_price != self.list_price:
                self.product_id.sudo().list_price = self.list_price
            return self.product_id
        vals = self._hms_product_vals()
        vals.update({'list_price': self.list_price, 'taxes_id': [(5, 0, 0)], 'sale_ok': True, 'purchase_ok': False})
        product = self.env['product.product'].sudo().create(vals)
        self.sudo().product_id = product
        return product


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


class HmsExamplePrices(models.AbstractModel):
    _name = 'hms.example.prices'
    _description = 'Load example prices'

    @api.model
    def _load(self, only_empty=True, raise_if_wrong_currency=False):
        """Fill example KES prices. Skipped (or refused) when the company does not invoice in KES,
        so a USD/EUR database never gets shilling amounts as dollar prices."""
        currency = self.env.company.currency_id.name
        if currency != EXAMPLE_PRICES_CURRENCY:
            if raise_if_wrong_currency:
                raise UserError(self.env._(
                    'The example prices are in Kenyan shillings, but this company invoices in %s. '
                    'Set your own prices in Configuration > Medications / Lab Tests and on the consultation products.',
                    currency))
            return False
        count = 0
        for model, prices in EXAMPLE_PRICES.items():
            for xmlid, price in prices.items():
                rec = self.env.ref(f'hospital_management.{xmlid}', raise_if_not_found=False)
                if rec and (not only_empty or not rec.list_price) and price:
                    rec.sudo().list_price = price
                    count += 1
        return count

    @api.model
    def action_load_example_prices(self):
        count = self._load(only_empty=True, raise_if_wrong_currency=True)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'type': 'success', 'sticky': False,
            'message': self.env._('%s example prices loaded (items that already had a price were left alone).', count),
        }}


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
        if self.partner_id:
            return self.partner_id
        partner = self.env['res.partner'].sudo().create({
            'name': self.name, 'phone': self.phone, 'email': self.email, 'ref': self.mrn,
            'company_id': self.company_id.id,
        })
        self.sudo().partner_id = partner
        return partner


# ---------------------------------------------------------------------------
# Billed items: each remembers the invoice line that charged it
# ---------------------------------------------------------------------------

class HmsMedicationRequestBilling(models.Model):
    _inherit = 'hms.medication.request'

    invoice_line_id = fields.Many2one('account.move.line', readonly=True, copy=False, ondelete='set null')
    is_billed = fields.Boolean(compute='_compute_is_billed')

    BILL_LOCKED = {'medication_id', 'dose', 'dose_unit', 'frequency', 'duration_days', 'quantity', 'status',
                   'encounter_id'}

    @api.depends('invoice_line_id.move_id.state', 'invoice_line_id.move_id.payment_state')
    def _compute_is_billed(self):
        for rec in self:
            rec.is_billed = _is_billed(rec.invoice_line_id)

    def _check_not_billed(self):
        billed = self.filtered(lambda r: _is_billed(r.sudo().invoice_line_id))
        if billed:
            inv = billed[0].sudo().invoice_line_id.move_id
            raise UserError(self.env._(
                '%(item)s was already charged on %(inv)s. To change it, first cancel that invoice or issue a '
                'credit note in Invoicing; the item then becomes billable again.',
                item=billed[0].medication_id.display_name, inv=inv.name or inv.display_name))

    def write(self, vals):
        if self.BILL_LOCKED & set(vals):
            self._check_not_billed()
        return super().write(vals)

    def unlink(self):
        self._check_not_billed()
        return super().unlink()


class HmsLabRequestBilling(models.Model):
    _inherit = 'hms.lab.request'

    invoice_line_id = fields.Many2one('account.move.line', readonly=True, copy=False, ondelete='set null')

    def _check_not_billed(self):
        billed = self.filtered(lambda r: _is_billed(r.sudo().invoice_line_id))
        if billed:
            inv = billed[0].sudo().invoice_line_id.move_id
            raise UserError(self.env._(
                '%(item)s was already charged on %(inv)s. Cancel that invoice or issue a credit note first.',
                item=billed[0].name, inv=inv.name or inv.display_name))

    def write(self, vals):
        if {'test_id', 'encounter_id'} & set(vals) or vals.get('status') == 'revoked':
            self._check_not_billed()
        return super().write(vals)

    def unlink(self):
        self._check_not_billed()
        return super().unlink()


class HmsEncounterBilling(models.Model):
    _inherit = 'hms.encounter'

    consult_invoice_line_id = fields.Many2one('account.move.line', readonly=True, copy=False, ondelete='set null')
    invoice_ids = fields.One2many('account.move', 'hms_encounter_id', string='Invoices', readonly=True)
    invoice_count = fields.Integer(compute='_compute_invoice_count')
    payment_state = fields.Selection([
        ('not_paid', 'Not Paid'), ('partial', 'Partially Paid'), ('paid', 'Paid'),
    ], string='Payment', compute='_compute_payment_state')
    billing_state = fields.Selection([
        ('not_ready', 'Visit Not Finished'),
        ('to_invoice', 'To Invoice'),
        ('invoiced', 'Invoiced'),
    ], compute='_compute_billing_state', store=True, index=True)

    @api.depends('invoice_ids.state')
    def _compute_invoice_count(self):
        for rec in self:
            rec.invoice_count = len(rec.sudo().invoice_ids.filtered(lambda m: m.state != 'cancel'))

    @api.depends('invoice_ids.state', 'invoice_ids.payment_state')
    def _compute_payment_state(self):
        for rec in self:
            invoices = rec.sudo().invoice_ids.filtered(
                lambda m: m.state == 'posted' and m.move_type == 'out_invoice' and m.payment_state != 'reversed')
            states = set(invoices.mapped('payment_state'))
            if not invoices:
                rec.payment_state = False
            elif states <= {'paid', 'in_payment'}:
                rec.payment_state = 'paid'
            elif states & {'paid', 'in_payment', 'partial'}:
                rec.payment_state = 'partial'
            else:
                rec.payment_state = 'not_paid'

    @api.depends('status', 'encounter_class',
                 'consult_invoice_line_id.move_id.state', 'consult_invoice_line_id.move_id.payment_state',
                 'medication_request_ids.status', 'medication_request_ids.invoice_line_id.move_id.state',
                 'medication_request_ids.invoice_line_id.move_id.payment_state',
                 'lab_request_ids.status', 'lab_request_ids.invoice_line_id.move_id.state',
                 'lab_request_ids.invoice_line_id.move_id.payment_state')
    def _compute_billing_state(self):
        for rec in self:
            if rec.status != 'finished':
                rec.billing_state = 'not_ready'
            elif rec.sudo()._unbilled_items():
                rec.billing_state = 'to_invoice'
            else:
                rec.billing_state = 'invoiced'

    def _unbilled_items(self):
        """Everything on this visit that has not been charged yet: [(record, kind)]."""
        self.ensure_one()
        items = []
        if not _is_billed(self.consult_invoice_line_id):
            items.append((self, 'consult'))
        items += [(m, 'medication') for m in self.medication_request_ids
                  if m.status != 'cancelled' and not _is_billed(m.invoice_line_id)]
        items += [(lab, 'lab') for lab in self.lab_request_ids
                  if lab.status != 'revoked' and not _is_billed(lab.invoice_line_id)]
        return items

    def _billing_lines(self):
        """[{product, name, quantity, price_unit, source, link_field}] for items not billed yet.
        Runs as sudo so the cashier never needs access to the clinical records themselves."""
        self.ensure_one()
        enc = self.sudo()
        lines = []
        for rec, kind in enc._unbilled_items():
            if kind == 'consult':
                product = self.env.ref(f'hospital_management.{CONSULTATION_PRODUCTS[enc.encounter_class]}')
                lines.append({'product': product, 'name': f'{product.name} ({enc.practitioner_id.name})',
                              'quantity': 1, 'price_unit': product.lst_price, 'source': rec,
                              'link_field': 'consult_invoice_line_id'})
            elif kind == 'medication':
                drug = rec.medication_id
                qty = (rec.quantity or 1) if drug.form in BILLED_PER_UNIT_FORMS else 1
                lines.append({'product': drug._get_product(), 'name': f'{drug.display_name} — {rec.sig}',
                              'quantity': qty, 'price_unit': drug.list_price, 'source': rec,
                              'link_field': 'invoice_line_id'})
            else:
                lines.append({'product': rec.test_id._get_product(), 'name': f'Lab: {rec.test_id.name} ({rec.name})',
                              'quantity': 1, 'price_unit': rec.test_id.list_price, 'source': rec,
                              'link_field': 'invoice_line_id'})
        return lines

    def _check_can_cancel(self):
        super()._check_can_cancel()
        for rec in self:
            if rec.sudo().invoice_ids.filtered(lambda m: m.state != 'cancel' and m.payment_state != 'reversed'):
                raise UserError(self.env._(
                    '%s has been invoiced. Cancel the invoice or issue a credit note in Invoicing before '
                    'cancelling the visit.', rec.name))

    def write(self, vals):
        if 'encounter_class' in vals:
            billed = self.filtered(lambda r: _is_billed(r.sudo().consult_invoice_line_id)
                                   and r.encounter_class != vals['encounter_class'])
            if billed:
                raise UserError(self.env._(
                    'The consultation for %s was already invoiced at the old rate. Cancel or credit that '
                    'invoice before changing the visit type.', billed[0].name))
        return super().write(vals)

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

    def action_view_invoices(self):
        self.ensure_one()
        invoices = self.invoice_ids.filtered(lambda m: m.state != 'cancel') or self.invoice_ids
        action = {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'name': self.env._('Invoices')}
        if len(invoices) == 1:
            action.update(res_id=invoices.id, view_mode='form')
        else:
            action.update(domain=[('id', 'in', invoices.ids)], view_mode='list,form')
        return action


class AccountMove(models.Model):
    _inherit = 'account.move'

    hms_encounter_id = fields.Many2one('hms.encounter', string='Hospital Visit', readonly=True, copy=False,
                                       index='btree_not_null')
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
    is_supplementary = fields.Boolean(compute='_compute_preview')
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
                for key, value in self._defaults_from_encounter(
                        self.env['hms.encounter'].browse(vals['encounter_id'])).items():
                    vals.setdefault(key, value)
        return super().create(vals_list)

    @api.depends('encounter_id')
    def _compute_preview(self):
        for wiz in self:
            if not wiz.encounter_id:
                wiz.preview, wiz.total, wiz.is_supplementary = False, 0, False
                continue
            lines = wiz.encounter_id._billing_lines()
            wiz.is_supplementary = bool(wiz.encounter_id.sudo().invoice_ids.filtered(lambda m: m.state != 'cancel'))
            rows = ''.join(
                f'<tr><td>{escape(l["name"])}</td><td class="text-end">{l["quantity"]:g}</td>'
                f'<td class="text-end">{l["price_unit"]:,.2f}</td>'
                f'<td class="text-end">{l["quantity"] * l["price_unit"]:,.2f}</td></tr>' for l in lines)
            wiz.total = sum(l['quantity'] * l['price_unit'] for l in lines)
            unpriced = [l['name'] for l in lines if not l['price_unit']]
            warning = (f'<p class="text-warning">No price set for: {escape(", ".join(unpriced))}</p>'
                       if unpriced else '')
            note = ('<p class="text-info">This visit was already invoiced; only items added since then '
                    '(e.g. after an amendment) are listed.</p>' if wiz.is_supplementary else '')
            currency = wiz.encounter_id.company_id.currency_id.name
            wiz.preview = (
                f'{note}<table class="table table-sm"><thead><tr><th>Item</th><th class="text-end">Qty</th>'
                f'<th class="text-end">Price</th><th class="text-end">Amount</th></tr></thead>'
                f'<tbody>{rows}</tbody><tfoot><tr><th colspan="3">Total ({currency})</th>'
                f'<th class="text-end">{wiz.total:,.2f}</th></tr></tfoot></table>{warning}')

    def action_create_invoice(self):
        self.ensure_one()
        enc = self.encounter_id.sudo()
        company = enc.company_id
        if enc.status != 'finished':
            raise UserError(self.env._('Finish the visit before invoicing it.'))
        lines = enc._billing_lines()
        if not lines:
            raise UserError(self.env._('Everything on %s has already been invoiced.', enc.name))
        if self.payer_type == 'insurance' and not (self.insurer_id and self.member_number):
            raise UserError(self.env._('Choose the insurer and enter the member number.'))
        if not self.env['account.journal'].sudo().search_count(
                [('type', '=', 'sale'), ('company_id', '=', company.id)], limit=1):
            raise UserError(self.env._(
                'Accounting is not set up yet for %s. An administrator must go to Invoicing > Configuration > '
                'Settings, choose the Fiscal Localization (e.g. Kenya) and save. Then try again.', company.name))
        patient = enc.patient_id
        bill_to = self.insurer_id if self.payer_type == 'insurance' else patient._get_partner()
        move = self.env['account.move'].with_company(company).create({
            'move_type': 'out_invoice',
            'company_id': company.id,
            'partner_id': bill_to.id,
            'invoice_date': fields.Date.context_today(self),
            'invoice_origin': enc.name,
            'ref': self.member_number if self.payer_type == 'insurance' else patient.mrn,
            'narration': self.env._('%(kind)sPatient: %(name)s (%(mrn)s). Visit %(enc)s on %(date)s.',
                                    kind=self.env._('Supplementary invoice. ') if self.is_supplementary else '',
                                    name=patient.name, mrn=patient.mrn, enc=enc.name,
                                    date=fields.Date.to_string(enc.period_start or enc.create_date)),
            'hms_encounter_id': enc.id,
            'invoice_line_ids': [(0, 0, {
                'product_id': l['product'].id,
                'name': l['name'],
                'quantity': l['quantity'],
                'price_unit': l['price_unit'],
                'tax_ids': [(5, 0, 0)],  # medical services and medicines: no VAT by default
                'sequence': 10 + i,
            }) for i, l in enumerate(lines)],
        })
        # Remember which invoice line charged which item
        for line_data, inv_line in zip(lines, move.invoice_line_ids.sorted(lambda l: (l.sequence, l.id))):
            line_data['source'].sudo().write({line_data['link_field']: inv_line.id})
        enc.message_post(body=self.env._('%(kind)s created by %(user)s for %(payer)s: %(amount)s',
                                         kind=self.env._('Supplementary invoice') if self.is_supplementary
                                         else self.env._('Invoice'),
                                         user=self.env.user.name, payer=bill_to.name,
                                         amount=f'{move.amount_total:,.2f} {move.currency_id.name}'))
        return {'type': 'ir.actions.act_window', 'res_model': 'account.move', 'res_id': move.id, 'view_mode': 'form'}
