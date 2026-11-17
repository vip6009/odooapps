from odoo import _, fields, models
from odoo.exceptions import UserError

from ..tools import normalize_site_url


class OmWoocommerceSetupWizard(models.TransientModel):
    """ Connect a store in three steps: credentials, Odoo settings, first imports. """
    _name = 'om.woocommerce.setup.wizard'
    _description = 'Connect a WooCommerce Store'

    step = fields.Selection([('credentials', 'Credentials'), ('settings', 'Settings'), ('imports', 'First Imports')],
                            default='credentials', required=True)
    store_id = fields.Many2one('om.woocommerce.store')
    site_url = fields.Char(string='Site Address', help="https://shop.example.com")
    consumer_key = fields.Char(help="WooCommerce > Settings > Advanced > REST API > Add key, with Read/Write "
                                    "permission.")
    consumer_secret = fields.Char()
    webhook_base_url = fields.Char(string='Odoo Public URL', help="https address of this Odoo, for the webhooks.")
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company, required=True)
    warehouse_id = fields.Many2one('stock.warehouse', domain="[('company_id', '=', company_id)]")
    journal_id = fields.Many2one('account.journal', string='Payment Journal',
                                 domain="[('type', 'in', ('bank', 'cash', 'credit')), ('company_id', '=', company_id)]",
                                 help="Journal of the payments of the paid orders. Empty: orders are invoiced, not paid.")
    tax_mode = fields.Selection([('woocommerce', 'Taxes of WooCommerce'), ('odoo', 'Taxes of Odoo')],
                                default='woocommerce', required=True)
    import_products = fields.Boolean(string='Import All Products', default=True)
    order_import_from = fields.Datetime(string='Import Orders From', default=lambda self: fields.Datetime.now())
    export_stock = fields.Boolean(string='Send the Stock', default=True)
    site_name = fields.Char(related='store_id.site_name')

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
                'target': 'new', 'name': _("Connect a WooCommerce Store")}

    def action_connect(self):
        self.ensure_one()
        url = normalize_site_url(self.site_url)
        if not url.startswith('https://'):
            raise UserError(_("WooCommerce accepts a consumer key over https only: %(url)s is not an https address.",
                              url=url))
        vals = {
            'site_url': url,
            'consumer_key': self.consumer_key,
            'consumer_secret': self.consumer_secret,
            'webhook_base_url': self.webhook_base_url,
            'company_id': self.company_id.id,
        }
        store = self.store_id
        if store:
            store.write(vals)
        else:
            host = url.split('://', 1)[-1].split('/')[0].removeprefix('www.')
            store = self.env['om.woocommerce.store'].create({'name': host.split('.')[0].replace('-', ' ').title(),
                                                              **vals})
            self.store_id = store
        store.action_woo_connect()
        if store.state != 'connected':
            raise UserError(store.last_error or _("WooCommerce refused the connection."))
        self.write({'step': 'settings', 'warehouse_id': store.warehouse_id.id,
                    'journal_id': store.workflow_id.journal_id.id, 'webhook_base_url': store.webhook_base_url})
        return self._reopen()

    def action_settings(self):
        self.ensure_one()
        store = self.store_id
        store.write({'warehouse_id': self.warehouse_id.id or store.warehouse_id.id, 'tax_mode': self.tax_mode,
                     'name': store.site_name or store.name})
        store.workflow_id.write({'journal_id': self.journal_id.id, 'register_payment': bool(self.journal_id)})
        self.step = 'imports'
        return self._reopen()

    def action_finish(self):
        self.ensure_one()
        store = self.store_id
        store.order_import_from = self.order_import_from
        store.stock_export = self.export_stock
        if self.import_products:
            store.action_woo_import_products()
        store.action_woo_import_orders()
        return {'type': 'ir.actions.act_window', 'res_model': 'om.woocommerce.store', 'res_id': store.id,
                'view_mode': 'form', 'target': 'current'}
