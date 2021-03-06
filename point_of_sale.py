import logging
import time
import math

from openerp import tools
from openerp.osv import fields, osv
from openerp.tools.translate import _

_logger = logging.getLogger(__name__)

class pos_order(osv.osv):
    _inherit = "pos.order"

    _columns= {
        'pedido_original' : fields.many2one('pos.order', 'Pedido original'),
        'numero_factura' : fields.char('Numero de factura a generar'),
        'anulado' : fields.boolean('Anulado'),
        'sale_manual_journal': fields.related('session_id', 'config_id', 'journal_manual_id', relation='account.journal', type='many2one', string='Diario de ventas manual', store=True, readonly=True),
    }

    def _order_fields(self, cr, uid, ui_order, context=None):
        return {
            'name':         ui_order['name'],
            'user_id':      ui_order['user_id'] or False,
            'session_id':   ui_order['pos_session_id'],
            'lines':        ui_order['lines'],
            'pos_reference':ui_order['name'],
            'partner_id':   ui_order['partner_id'] or False,
            'numero_factura': ui_order['numero_factura'] or False,
        }

    def mix_and_match(self, cr, uid, ids, context=None):
        for o in self.browse(cr, uid, ids, context=context):
            if o.pedido_original:
                continue

            productos = []
            cantidad = 0

            for l in o.lines:
                if l.product_id.type != 'service':
                    precio = l.product_id.list_price
                    for i in range(int(l.qty)):
                        productos.append({'linea': l, 'precio': precio, 'precio_original': precio})
                    cantidad += int(l.qty)

            productos.sort(key=lambda l: l['precio'])

            restantes = int(math.floor(cantidad/2.0))
            incremento = int(math.ceil(cantidad/2.0))

            for i in range(restantes):
                descuento = productos[i]['precio']
                total = productos[i]['precio'] + productos[i+incremento]['precio'];
                productos[i]['precio'] = productos[i]['precio'] - ( productos[i]['precio'] / total * descuento );
                productos[i+incremento]['precio'] = productos[i+incremento]['precio'] - ( productos[i+incremento]['precio'] / total * descuento );

            for p in productos:

                # Si este producto no cambio de precio es por que es
                # uno impar, por lo que tiene un 25% de descuento
                precio = p['precio']
                if p['precio'] == p['precio_original']:
                    precio = p['precio'] * 0.75

                n_id = self.pool.get('pos.order.line').copy(cr, uid, p['linea'].id, {'qty': 1, 'price_unit': precio}, context=context)
                _logger.warn(n_id)

            p_ids = set()
            for p in productos:
                p_ids.add(p['linea'].id)
            self.pool.get('pos.order.line').unlink(cr, uid, p_ids, context=context)

            # Si es una devolucion
            if o.pedido_original:
                if len(o.lines) == 2:
                    pareja = o.lines.sorted(key=lambda l: l.price_subtotal_incl)
                    if pareja[0].price_subtotal_incl < 0 and pareja[0].product_id.list_price == pareja[1].product_id.list_price:
                        self.pool.get('pos.order.line').write(cr, uid, pareja[0].id, {'price_unit': pareja[0].product_id.list_price})
                        self.pool.get('pos.order.line').write(cr, uid, pareja[1].id, {'price_unit': pareja[1].product_id.list_price})
        return True

    # def add_payment(self, cr, uid, order_id, data, context=None):
    #     for o in self.browse(cr, uid, order_id, context=context):
    #         total_descuento = 0
    #         lineas_id = []
    #         for l in o.lines:
    #             lineas_id.append(l.id)
    #             if l.qty < 0:
    #                 total_descuento += -1 * l.qty * l.price_unit
    #
    #         if o.pedido_original.amount_total + 0.001 < total_descuento:
    #             raise osv.except_osv(_('Error'), _('No se puede devolver por una cantidad mayor a lo comprado.'))
    #
    #         for l in o.pedido_original.lines:
    #             if l.devuelto:
    #                 raise osv.except_osv(_('Error'), _('Este pedido ya fue devuelto anteriormente, no puede ser devuelto dos veces.'))
    #
    #             self.pool.get('pos.order.line').write(cr, uid, [l.id], {'devuelto': True})
    #
    #     return = super(pos_order, self).add_payment(cr, uid, order_id, data, context=context)

    def action_invoice(self, cr, uid, ids, context=None):
        action = super(pos_order, self).action_invoice(cr, uid, ids, context=context)
        for o in self.browse(cr, uid, ids, context=context):
            if not o.numero_factura and o.sale_manual_journal:
                self.pool.get('account.invoice').write(cr, uid, action['res_id'], {'journal_id': o.sale_manual_journal.id}, context=context)
        return action

    def action_paid(self, cr, uid, ids, context=None):
        result = super(pos_order, self).action_paid(cr, uid, ids, context=context)
        for o in self.browse(cr, uid, ids, context=context):
            if o.amount_total > 0:
                if not o.partner_id:
                    self.write(cr, uid, [o.id], {'partner_id': 320334}, context=context)
                self.action_invoice(cr, uid, [o.id], context)
                self.pool['account.invoice'].signal_workflow(cr, uid, [o.invoice_id.id], 'invoice_open')
        return result

class pos_order_line(osv.osv):
    _inherit = "pos.order.line"

    _columns= {
         'devuelto' : fields.boolean('Devuelto'),
    }

class pos_confirm(osv.osv_memory):
    _inherit = 'pos.confirm'

    def action_confirm(self, cr, uid, ids, context=None):
        order_obj = self.pool.get('pos.order')
        ids = order_obj.search(cr, uid, [('state','=','paid')], context=context)
        for order in order_obj.browse(cr, uid, ids, context=context):
            todo = True
            for line in order.statement_ids:
                if line.statement_id.state != 'confirm':
                    todo = False
                    break
            if todo:
                order.signal_workflow('done')

        # Check if there is orders to reconcile their invoices
        ids = order_obj.search(cr, uid, [('state','=','invoiced'),('invoice_id.state','=','open')], context=context)
        for order in order_obj.browse(cr, uid, ids, context=context):
            invoice = order.invoice_id
            data_lines = [x.id for x in invoice.move_id.line_id if x.account_id.id == invoice.account_id.id]
            for st in order.statement_ids:
                data_lines += [x.id for x in st.journal_entry_id.line_id if x.account_id.id == invoice.account_id.id]
            self.pool.get('account.move.line').reconcile(cr, uid, data_lines, context=context)
        return {}

class pos_config(osv.osv):
    _inherit = "pos.config"

    def _pedidos_pendientes(self, cr, uid, ids, fieldnames, args, context=None):
        result = dict.fromkeys(ids, False)
        for c in self.browse(cr, uid, ids, context=context):
            total = 0
            sesiones = self.pool.get('pos.session').search(cr, uid, [('config_id','=',c.id),('state','=','opened')], context=context)
            for s in self.pool.get('pos.session').browse(cr, uid, sesiones, context=context):
                for o in s.order_ids:
                    if o.state == 'paid' and o.numero_factura:
                        total += 1
            result[c.id] = total
        return result

    _columns= {
        'numero_siguiente' : fields.related('journal_id', 'sequence_id', 'number_next', type="integer", string="Numero siguiente"),
        'prefijo' : fields.related('journal_id', 'sequence_id', 'prefix', type="char", string="Prefijo"),
        'relleno' : fields.related('journal_id', 'sequence_id', 'padding', type="integer", string="Relleno"),
        'pedidos_pendientes' : fields.function(_pedidos_pendientes, type="integer", string="Pedidos de pendientes"),
        'journal_manual_id' : fields.many2one('account.journal', 'Diario de ventas manual', domain=[('type', '=', 'sale')]),
        'devoluciones' : fields.boolean('Devoluciones'),
    }

class pos_details(osv.osv_memory):
    _inherit = 'pos.details'

    _columns = {
        'date_start': fields.datetime('Date Start', required=True),
        'date_end': fields.datetime('Date End', required=True),
    }

class pos_session(osv.osv):
    _inherit = 'pos.session'

    def _confirm_orders(self, cr, uid, ids, context=None):
        for session in self.browse(cr, uid, ids, context=context):
            for order in session.order_ids:
                if order.amount_total > 0:
                    if not order.invoice_id:
                        raise osv.except_osv(
                            _('Error!'),
                            _("No se puede cerrar la sesion por que existen pedidos que no tienen facturas creadas."))
                    if order.invoice_id and order.invoice_id.state not in ('open', 'cancel'):
                        raise osv.except_osv(
                            _('Error!'),
                            _("No se puede cerrar la sesion por que existen pedidos que tienen facturas que no han sido validadas."))

        return super(pos_session, self)._confirm_orders(cr, uid, ids, context=context)
