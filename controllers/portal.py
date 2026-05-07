from odoo.addons.portal.controllers.portal import CustomerPortal
from odoo.exceptions import AccessError, MissingError, UserError
from odoo.http import request
from odoo import http, _


class CustomerPortalStageUpdate(CustomerPortal):

    @http.route([
        '/my/ticket/update_stage/<int:ticket_id>',
        '/my/ticket/update_stage/<int:ticket_id>/<access_token>',
    ], type='http', auth="public", methods=['POST'], website=True, csrf=True)
    def ticket_update_stage(self, ticket_id=None, access_token=None, **post):

        # 🔐 Validación de acceso (portal-safe)
        try:
            ticket_sudo = self._document_check_access('helpdesk.ticket', ticket_id, access_token)
        except (AccessError, MissingError):
            return request.redirect('/my')

        user = request.env.user

        # 🔒 Solo el usuario asignado puede cambiar el estado
        if ticket_sudo.user_id.id != user.id:
            raise UserError(_("No estás autorizado a cambiar la etapa de este ticket."))

        stage_id = post.get('stage_id')

        if stage_id:
            stage = request.env['helpdesk.stage'].sudo().browse(int(stage_id))

            # Validar que la etapa pertenece al equipo del ticket
            if stage not in ticket_sudo.team_id.stage_ids:
                raise UserError(_("Etapa inválida."))

            # (Opcional PRO) evitar volver atrás
            # if stage.sequence < ticket_sudo.stage_id.sequence:
            #     raise UserError(_("No podés volver a una etapa anterior."))

            ticket_sudo.sudo().write({
                'stage_id': stage.id
            })

            # Log en el chatter
            ticket_sudo.message_post(
                body=_("Etapa cambiada a %s") % stage.name,
                subtype_xmlid='mail.mt_note'
            )

        # 🔁 Volver a la misma página
        return request.redirect(request.httprequest.referrer or '/my/tickets')


    def _prepare_helpdesk_tickets_domain(self):
        domain = super()._prepare_helpdesk_tickets_domain()

        user = request.env.user

        # Mostrar solo tickets asignados al usuario logueado
        domain += [('user_id', '=', user.id)]

        return domain



    @http.route(['/my/tickets', '/my/tickets/page/<int:page>'], type='http', auth="user", website=True)
    def my_helpdesk_tickets(self, page=1, date_begin=None, date_end=None,
                            sortby=None, filterby=None, search=None,
                            groupby='none', search_in='name', **kw):

        # 👇 Solo aplicar default si NO viene filtro en la URL
        if not filterby:
            filterby = 'open'

        return super().my_helpdesk_tickets(
            page=page,
            date_begin=date_begin,
            date_end=date_end,
            sortby=sortby,
            filterby=filterby,
            search=search,
            groupby=groupby,
            search_in=search_in,
            **kw
        )