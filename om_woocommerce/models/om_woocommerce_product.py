import logging
import math

import requests

from odoo import _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools.binary import BinaryBytes

_logger = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMPORTED_TYPES = ('simple', 'variable')


class OmWoocommerceProductTemplate(models.Model):
    """ A WooCommerce product, linked to an Odoo product template. """
    _name = 'om.woocommerce.product.template'
    _description = 'WooCommerce Product'
    _inherit = ['om.connector.binding.mixin']
    _order = 'store_id, name'
    _connector_backend_field = 'store_id'
    _connector_record_field = 'product_tmpl_id'

    name = fields.Char(readonly=True)
    store_id = fields.Many2one('om.woocommerce.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True)
    product_tmpl_id = fields.Many2one('product.template', string='Product', required=True, ondelete='cascade',
                                      index=True)
    woo_type = fields.Selection([('simple', 'Simple'), ('variable', 'Variable')], string='Type', readonly=True)
    woo_status = fields.Selection([
        ('publish', 'Published'), ('draft', 'Draft'), ('pending', 'Pending Review'), ('private', 'Private'),
        ('future', 'Scheduled'), ('trash', 'Trash'), ('deleted', 'Deleted'),
    ], string='Status in WooCommerce', readonly=True)
    slug = fields.Char(readonly=True)
    variant_ids = fields.One2many('om.woocommerce.product', 'template_binding_id', string='Variants')
    active = fields.Boolean(default=True)

    _store_external_unique = models.Constraint('unique (store_id, external_id)',
                                               "A WooCommerce product is linked once per store.")

    def action_open_in_woocommerce(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_url',
            'url': f'{self.store_id.site_url}/wp-admin/post.php?post={self.external_id}&action=edit',
            'target': 'new',
        }

    def action_woo_import_again(self):
        for binding in self:
            binding.store_id._connector_enqueue(
                '_connector_job_woo_import_product', {'id': int(binding.external_id)},
                model_name=self._name, name=_("Product %(name)s", name=binding.name),
                dedupe_key=f'product:{binding.external_id}', priority=20)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'message': _("Import queued."), 'type': 'info'}}

    # ------------------------------------------------------------------
    # Jobs
    # ------------------------------------------------------------------

    @api.model
    def _connector_job_woo_import_product(self, job):
        return self._woo_import_product(job.backend, job.payload['id'])

    @api.model
    def _connector_job_woo_product_webhook(self, job):
        payload = job.payload or {}
        if payload.get('parent_id'):
            # a variation: the product it belongs to
            return self._woo_import_product(job.backend, payload['parent_id'], from_webhook=True)
        return self._woo_import_product(job.backend, payload['id'], from_webhook=True)

    @api.model
    def _connector_job_woo_product_deleted(self, job):
        return self._woo_unlink(job.backend, job.payload['id'])

    @api.model
    def _woo_unlink(self, store, woo_id, status='deleted'):
        """ The product was deleted or trashed in WooCommerce: its links are archived. """
        bindings = self.with_context(active_test=False)._connector_find(store, woo_id)
        bindings.write({'woo_status': status, 'active': False})
        bindings.variant_ids.write({'active': False})
        return _("%(count)s product(s) unlinked", count=len(bindings))

    # ------------------------------------------------------------------
    # Import
    # ------------------------------------------------------------------

    @api.model
    def _woo_variants(self, store, data):
        """ The variants of the WooCommerce product ``data``, in one format: the product itself when it
        is simple, its variations when it is variable. """
        if data.get('type') != 'variable':
            return [self._woo_variant_values(data, data, parent=False)]
        return [self._woo_variant_values(variation, data, parent=True)
                for variation in store._woo_paginate(f"products/{data['id']}/variations")]

    @api.model
    def _woo_variant_values(self, item, product, parent):
        image = item.get('image') if parent else ((item.get('images') or [None])[0])
        return {
            'id': str(item['id']),
            'parent_id': str(product['id']) if parent else False,
            'sku': (item.get('sku') or '').strip(),
            'barcode': (item.get('global_unique_id') or '').strip(),
            'price': item.get('regular_price') or item.get('price') or '0',
            'weight': item.get('weight'),
            'manage_stock': item.get('manage_stock') is True,
            'image': (image or {}).get('src'),
            'options': [(a.get('name'), a.get('option')) for a in item.get('attributes') or [] if a.get('option')]
            if parent else [],
            'name': item.get('name') or product.get('name'),
        }

    @api.model
    def _woo_import_product(self, store, woo_id, from_webhook=False):
        """ Import or update the WooCommerce product ``woo_id``: find or create the Odoo product of each
        variant, and link them. Gives the linked template, or a text when skipped. """
        data = store._woo_get(f'products/{woo_id}')
        if not data:
            return _("Product %(id)s does not exist anymore", id=woo_id)
        if data.get('type') not in IMPORTED_TYPES:
            return _("%(name)s is a %(type)s product: not imported", name=data.get('name'), type=data.get('type'))
        if data.get('status') == 'trash':
            return self._woo_unlink(store, data['id'], status='trash')
        variants = self._woo_variants(store, data)
        if not variants:
            return _("%(name)s has no variation yet: not imported", name=data.get('name'))
        binding = self.with_context(active_test=False)._connector_find(store, data['id'])
        variant_bindings = {b.external_id: b for b in binding.with_context(active_test=False).variant_ids}
        Variant = self.env['om.woocommerce.product']
        products = {}
        for variant in variants:
            product = variant_bindings[variant['id']].product_id if variant['id'] in variant_bindings else None
            products[variant['id']] = product or Variant._woo_match(store, variant)
        missing = [v for v in variants if not products[v['id']]]
        if missing:
            if not store.product_auto_create:
                if from_webhook and not binding:
                    return _("No Odoo product for %(name)s: not imported", name=data.get('name'))
                raise UserError(_(
                    "No Odoo product has the SKU or GTIN of these variants of %(name)s: %(variants)s. Create them, or "
                    "allow the store to create the products.", name=data.get('name'),
                    variants=', '.join(v['sku'] or v['barcode'] or v['id'] for v in missing)))
            template = next((p.product_tmpl_id for p in products.values() if p), None) or binding.product_tmpl_id
            products.update(self._woo_create_products(store, data, missing, template or None))
        templates = self.env['product.template'].union(p.product_tmpl_id for p in products.values())
        template = binding.product_tmpl_id if binding.product_tmpl_id in templates else templates[:1]
        binding = self._connector_bind(store, str(data['id']), template, {
            'name': data.get('name'),
            'woo_type': data.get('type'),
            'woo_status': data.get('status') if data.get('status') in dict(self._fields['woo_status'].selection)
            else False,
            'slug': data.get('slug'),
            'active': True,
        })
        for variant in variants:
            Variant._woo_bind_variant(store, binding, variant, products[variant['id']])
        stale = binding.with_context(active_test=False).variant_ids.filtered(
            lambda b: b.external_id not in {v['id'] for v in variants})
        stale.write({'active': False})
        if store.product_import_images:
            self._woo_import_images(template, data, variants, products)
        return binding

    @api.model
    def _woo_create_products(self, store, data, variants, template=None):
        """ Create the Odoo products of ``variants`` of the WooCommerce product ``data``; in ``template``
        when given. Gives ``{variant id: product.product}``. """
        first = variants[0]
        new_template = template is None
        # no tracking: the attribute lines replace the variants the tracking of the template looks at
        Template = self.env['product.template'].with_context(tracking_disable=True)
        if new_template:
            simple = data.get('type') != 'variable'
            template = Template.create({
                'name': data.get('name') or first['name'],
                'type': 'consu',
                'is_storable': data.get('manage_stock') is True or any(v['manage_stock'] for v in variants),
                'list_price': float(first['price'] or 0.0),
                'company_id': False,
                'default_code': first['sku'] or False if simple else False,
                'barcode': self.env['om.woocommerce.product']._woo_free_barcode(first['barcode']) if simple else False,
            })
        template = template.with_context(tracking_disable=True)
        if data.get('type') != 'variable':
            product = template.product_variant_ids[:1]
            self.env['om.woocommerce.product']._woo_update_variant(store, product, first)
            return {first['id']: product}
        variants_before = template.product_variant_ids if not new_template else self.env['product.product']
        # one attribute per attribute of variations; a new attribute only makes the variants asked for
        Attribute = self.env['product.attribute']
        attribute_values = {}
        for attribute_data in data.get('attributes') or []:
            if not attribute_data.get('variation') or not attribute_data.get('name'):
                continue
            name = attribute_data['name']
            attribute = Attribute.search([('name', '=ilike', name), ('create_variant', '!=', 'no_variant')],
                                         limit=1) or Attribute.create({'name': name, 'create_variant': 'dynamic'})
            values = attribute_values.setdefault(name, {'attribute': attribute, 'values': {}})
            for value_name in attribute_data.get('options') or []:
                value = attribute.value_ids.filtered(lambda v: v.name.lower() == value_name.lower())[:1] or \
                    self.env['product.attribute.value'].create({'attribute_id': attribute.id, 'name': value_name})
                values['values'][value_name.lower()] = value
        for info in attribute_values.values():
            line = template.attribute_line_ids.filtered(lambda l: l.attribute_id == info['attribute'])
            wanted = self.env['product.attribute.value'].union(info['values'].values())
            if line:
                line.value_ids = [fields.Command.link(v.id) for v in wanted]
            else:
                template.attribute_line_ids = [fields.Command.create({
                    'attribute_id': info['attribute'].id,
                    'value_ids': [fields.Command.set(wanted.ids)],
                })]
        result = {}
        for variant in variants:
            ptavs = self.env['product.template.attribute.value']
            for name, option in variant['options']:
                info = attribute_values.get(name)
                value = info and info['values'].get((option or '').lower())
                if value:
                    ptavs |= template.attribute_line_ids.product_template_value_ids.filtered(
                        lambda v, value=value: v.product_attribute_value_id == value)
            product = template._create_product_variant(ptavs) if ptavs else template.product_variant_ids[:1]
            if not product:
                raise UserError(_("The variation %(variant)s of %(product)s cannot be created in Odoo (a variation "
                                  "for any value of an attribute is not supported).",
                                  variant=variant['sku'] or variant['id'], product=data.get('name')))
            self.env['om.woocommerce.product']._woo_update_variant(store, product, variant)
            result[variant['id']] = product
        # attributes that make every combination create variants WooCommerce does not have: removed,
        # as long as they were just made here
        made = self.env['product.product'].union(result.values())
        bound = self.env['om.woocommerce.product'].with_context(active_test=False).search(
            [('product_id', 'in', template.with_context(active_test=False).product_variant_ids.ids)]).product_id
        (template.product_variant_ids - variants_before - made - bound).unlink()
        return result

    @api.model
    def _woo_download(self, url):
        if not url:
            return None
        try:
            response = requests.get(url, timeout=15)
        except requests.RequestException as e:
            _logger.info("Image %s not downloaded: %s", url, e)
            return None
        if response.ok and response.content and len(response.content) <= MAX_IMAGE_BYTES:
            return BinaryBytes(response.content)
        return None

    @api.model
    def _woo_import_images(self, template, data, variants, products):
        """ The main image, the gallery (in the eCommerce gallery when it is installed) and the image of
        each variation, for the Odoo products without one. """
        urls = [image.get('src') for image in data.get('images') or [] if image.get('src')]
        main_url = urls[0] if urls else None
        if not template.image_1920 and (image := self._woo_download(main_url)):
            template.image_1920 = image
        if 'product.image' in self.env and 'product_tmpl_id' in self.env['product.image']._fields \
                and not template.product_template_image_ids:
            for url in urls[1:]:
                if image := self._woo_download(url):
                    self.env['product.image'].create({'product_tmpl_id': template.id, 'name': template.name,
                                                      'image_1920': image})
        if len(products) > 1:
            for variant in variants:
                product = products.get(variant['id'])
                url = variant['image']
                if product and url and url != main_url and not product.image_variant_1920 \
                        and (image := self._woo_download(url)):
                    product.image_variant_1920 = image


class OmWoocommerceProduct(models.Model):
    """ A WooCommerce simple product or variation, linked to an Odoo product. """
    _name = 'om.woocommerce.product'
    _description = 'WooCommerce Variant'
    _inherit = ['om.connector.binding.mixin']
    _order = 'store_id, template_binding_id, id'
    _connector_backend_field = 'store_id'
    _connector_record_field = 'product_id'

    name = fields.Char(readonly=True)
    store_id = fields.Many2one('om.woocommerce.store', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='store_id.company_id', store=True)
    template_binding_id = fields.Many2one('om.woocommerce.product.template', string='WooCommerce Product',
                                          ondelete='cascade', index=True)
    parent_woo_id = fields.Char(string='Parent Product', readonly=True,
                                help="For a variation: the ID of its variable product in WooCommerce.")
    product_id = fields.Many2one('product.product', string='Product', required=True, ondelete='cascade', index=True)
    sku = fields.Char(string='SKU', readonly=True)
    barcode = fields.Char(string='GTIN', readonly=True)
    manage_stock = fields.Boolean(string='Stock Managed in WooCommerce', readonly=True)
    active = fields.Boolean(default=True)
    stock_rule = fields.Selection([
        ('actual', 'Actual Stock'),
        ('max', 'At Most'),
        ('percent', 'Percentage'),
        ('buffer', 'Keep Back'),
        ('fixed', 'Fixed Quantity'),
    ], string='Stock Sent', default='actual', required=True,
        help="Actual Stock: the stock of the warehouse.\nAt Most: the stock, at most the value.\n"
             "Percentage: this percent of the stock.\nKeep Back: the stock minus the value.\n"
             "Fixed Quantity: always the value, whatever the stock.")
    stock_value = fields.Float(string='Value')

    _store_external_unique = models.Constraint('unique (store_id, external_id)',
                                               "A WooCommerce variant is linked once per store.")

    def _woo_stock_quantity(self, quantity):
        self.ensure_one()
        quantity = max(quantity, 0.0)
        if self.stock_rule == 'max':
            quantity = min(quantity, self.stock_value)
        elif self.stock_rule == 'percent':
            quantity = math.floor(quantity * self.stock_value / 100.0)
        elif self.stock_rule == 'buffer':
            quantity = quantity - self.stock_value
        elif self.stock_rule == 'fixed':
            quantity = self.stock_value
        return max(int(quantity), 0)

    @api.model
    def _woo_match(self, store, variant):
        """ The Odoo product with the SKU or the GTIN of ``variant``, as the store matches them. """
        Product = self.env['product.product'].with_context(active_test=False)
        domain_company = [('company_id', 'in', [False, store.company_id.id])]
        sku, barcode = variant.get('sku') or '', variant.get('barcode') or ''
        if sku and store.product_match in ('sku', 'sku_barcode'):
            products = Product.search([('default_code', '=', sku)] + domain_company, limit=2)
            if len(products) == 1:
                return products
        if barcode and store.product_match in ('barcode', 'sku_barcode'):
            products = Product.search([('barcode', '=', barcode)] + domain_company, limit=2)
            if len(products) == 1:
                return products
        return None

    @api.model
    def _woo_free_barcode(self, barcode):
        barcode = (barcode or '').strip()
        if barcode and not self.env['product.product'].with_context(active_test=False).search_count(
                [('barcode', '=', barcode)], limit=1):
            return barcode
        return False

    @api.model
    def _woo_update_variant(self, store, product, variant):
        """ Fill the reference, barcode and weight of a product made from ``variant``. """
        vals = {}
        if variant.get('sku') and not product.default_code:
            vals['default_code'] = variant['sku']
        if not product.barcode and (barcode := self._woo_free_barcode(variant.get('barcode'))):
            vals['barcode'] = barcode
        if weight := store._woo_weight_kg(variant.get('weight')):
            vals['weight'] = weight
        if vals:
            product.write(vals)

    @api.model
    def _woo_bind_variant(self, store, template_binding, variant, product):
        return self._connector_bind(store, variant['id'], product, {
            'template_binding_id': template_binding.id,
            'parent_woo_id': variant.get('parent_id') or False,
            'name': ' / '.join(option for _name, option in variant['options']) or template_binding.name,
            'sku': variant.get('sku') or False,
            'barcode': variant.get('barcode') or False,
            'manage_stock': variant.get('manage_stock'),
            'active': True,
        })
