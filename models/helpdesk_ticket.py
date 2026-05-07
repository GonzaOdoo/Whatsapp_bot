from odoo import models, fields, api, _
import logging
import json
import requests
from odoo.exceptions import UserError
import random
from odoo.exceptions import ValidationError

_logger = logging.getLogger(__name__)
class WhatsappAccount(models.Model):
    _inherit = 'helpdesk.ticket'

    tipo_reclamo = fields.Char(string='Tipo de reclamo')
    subtipo_reclamo = fields.Char('Subtipo reclamo')
    reclamo_id = fields.Many2one(
        'reclamo.reclamo',
        string='Tipo de Reclamo',
        domain=[('parent_id', '=', False)]
    )
    reclamo_subtipo_id = fields.Many2one(
        'reclamo.reclamo',
        string='Subtipo de Reclamo',
        domain="[('parent_id', '=', reclamo_id)]"
    )
    
    conversation_ids = fields.One2many(
            'whatsapp.bot.conversation',
            'ticket_id',
            string='Conversaciones',
        )
    conversation_count = fields.Integer(
        compute='_compute_conversation_count',
        string='Conversaciones',
    )
    calle_formateada = fields.Char(string='Calle formateada')
    calle_declarada = fields.Char(string="Calle declarada")
    longitud = fields.Float(string='Longitud')
    latitud = fields.Float(string='Latitud')
    barrio = fields.Char(string='Barrio')
    canal_origen = fields.Selection([('app','Aplicación'),('whatsapp','Whatsapp')],string="Canal", default="app")
    calificacion_bot = fields.Integer('Calificación bot')
    calificacion_solucion = fields.Integer('Calificación solución')

    allowed_user_ids = fields.Many2many(
        'res.users',
        compute='_compute_allowed_user_ids',
        store=False
    )
    map_url = fields.Char(
        string="Ubicación en mapa",
        compute="_compute_map_url"
    )
    calificacion_bot_texto = fields.Char(
        string='Calificación bot (texto)',
        compute='_compute_calificaciones_texto',
        store=True
    )
    
    calificacion_solucion_texto = fields.Char(
        string='Calificación solución (texto)',
        compute='_compute_calificaciones_texto',
        store=True
    )
    barrio_id = fields.Many2one(
        'geo.barrio',
        string='Barrio',
        tracking=True
    )
    @api.depends('calificacion_bot', 'calificacion_solucion')
    def _compute_calificaciones_texto(self):
        mapping = {
            0: 'Sin calificación',
            1: 'Mala',
            3: 'Normal',
            5: 'Excelente',
        }
        for rec in self:
            rec.calificacion_bot_texto = mapping.get(rec.calificacion_bot, False)
            rec.calificacion_solucion_texto = mapping.get(rec.calificacion_solucion, False)
    @api.depends('longitud','latitud')
    def _compute_map_url(self):
        for rec in self:
            if rec.latitud and rec.longitud:
                rec.map_url = f"https://www.google.com/maps?q={rec.latitud},{rec.longitud}"
            else:
                rec.map_url = False

    @api.depends('team_id')
    def _compute_allowed_user_ids(self):
        for rec in self:
            rec.allowed_user_ids = rec.team_id.message_partner_ids.mapped('user_ids')

    @api.onchange('reclamo_id', 'reclamo_subtipo_id')
    def _onchange_reclamo(self):
        self._apply_reclamo_team()

    def _apply_reclamo_team(self):
        for rec in self:
            team = False
            user = False
    
            # 1. Prioridad: subtipo
            if rec.reclamo_subtipo_id:
                if rec.reclamo_subtipo_id.team_id:
                    team = rec.reclamo_subtipo_id.team_id
                if rec.reclamo_subtipo_id.user_id:
                    user = rec.reclamo_subtipo_id.user_id
    
            # 2. Fallback: tipo
            if not team and rec.reclamo_id and rec.reclamo_id.team_id:
                team = rec.reclamo_id.team_id
    
            if not user and rec.reclamo_id and rec.reclamo_id.user_id:
                user = rec.reclamo_id.user_id
    
            # 3. Aplicar team
            if team:
                rec.team_id = team
    
            # 4. Resolver usuario
            if user:
                rec.user_id = user
            elif team:
                # lógica tipo helpdesk.team
                if team.auto_assignment and team.member_ids:
                    if team.assign_method == 'randomly':
                        user = random.choice(team.member_ids)
                    else:  # balanced
                        users = team.member_ids
                        counts = {
                            u.id: self.env['helpdesk.ticket'].search_count([
                                ('team_id', '=', team.id),
                                ('user_id', '=', u.id),
                            ])
                            for u in users
                        }
                        user = min(users, key=lambda u: counts[u.id])
                    rec.user_id = user
            
                else:
                    rec.user_id = False
            else:
                rec.user_id = False
            if rec.user_id and rec.team_id:
                assigned_stage = self.env['helpdesk.stage'].search([
                    ('assigned_stage', '=', True)
                ], limit=1)
            
                if (
                    assigned_stage
                    and rec.stage_id
                    and rec.stage_id.sequence < assigned_stage.sequence
                ):
                    rec.stage_id = assigned_stage
    def write(self, vals):
        res = super().write(vals)

        if 'stage_id' in vals:
            for ticket in self:
                template = ticket.stage_id.send_whatsapp_template_id
                script = ticket.stage_id.whatsapp_bot_script_id
                if template:
                    ticket.stage_id = ticket.stage_id.notified_step
                    ticket._send_whatsapp_stage_template(template,script)
                    

        return res

    def _send_whatsapp_stage_template(self, template,script):
        self.ensure_one()
    
        if not self.partner_id or not self.partner_id.phone:
            return
    
        wa_account = template.wa_account_id
    
        conversation = self.env['whatsapp.bot.conversation']._find_or_create_conversation(
            wa_account.id,
            self.partner_id.id,
            self.partner_id.phone,
            self.partner_id.phone,
            'survey',
        )
        conversation.ticket_id = self.id
        if script:
            conversation.script_id = script.id
            conversation.current_step_id = script.step_ids.sorted('sequence')[:1].id
            conversation.conversation_memory = {}
    
        composer = self.env['whatsapp.composer'].with_context(
            active_model='helpdesk.ticket',
            active_ids=[self.id],
        ).create({
            'res_model': 'helpdesk.ticket',
            'res_ids': str([self.id]),
            'wa_template_id': template.id,
            'phone': self.partner_id.phone,
        })
    
        messages = composer._send_whatsapp_template()
        for msg in messages:
            msg.bot_conversation_id = conversation.id
        
            self.env['whatsapp.bot.conversation.message'].create({
                'conversation_id': conversation.id,
                'message_type': 'outbound',
                'body': template.body or template.name,
                'state': 'sent',
                'message_uid': msg.msg_uid,
            })
    
    def _compute_conversation_count(self):
        for ticket in self:
            ticket.conversation_count = len(ticket.conversation_ids)

    def action_open_conversations(self):
        self.ensure_one()
        conversations = self.conversation_ids
    
        # Si hay solo una conversación → abrir directo el form
        if len(conversations) == 1:
            return {
                'type': 'ir.actions.act_window',
                'name': 'Conversación WhatsApp',
                'res_model': 'whatsapp.bot.conversation',
                'view_mode': 'form',
                'res_id': conversations.id,
                'context': {
                    'default_ticket_id': self.id,
                }
            }
    
        # Si hay varias → lista + form
        return {
            'type': 'ir.actions.act_window',
            'name': 'Conversaciones WhatsApp',
            'res_model': 'whatsapp.bot.conversation',
            'view_mode': 'list,form',
            'domain': [('ticket_id', '=', self.id)],
            'context': {
                'default_ticket_id': self.id,
            }
        }


    def action_validar_direccion_google(self):
        self.ensure_one()

        if not self.calle_declarada:
            raise UserError(_("Debe ingresar una dirección primero."))

        api_key = "fake"

        url = f"https://addressvalidation.googleapis.com/v1:validateAddress?key={api_key}"

        payload = {
            "address": {
                "regionCode": "AR",
                "administrativeArea": "Santa Fe",
                "locality": "Alvear",
                "addressLines": [self.calle_declarada.strip()]
            }
        }

        try:
            response = requests.post(url, json=payload, timeout=5)
            response.raise_for_status()
            data = response.json()

            _logger.info("📍 Google response: %s", data)

            result = data.get("result", {})
            verdict = result.get("verdict", {})

            if not verdict.get("addressComplete"):
                raise UserError(_("Google no pudo validar la dirección."))

            address = result.get("address", {})
            geocode = result.get("geocode", {}).get("location", {})

            formatted = address.get("formattedAddress")
            lat = geocode.get("latitude")
            lng = geocode.get("longitude")

            # Extraer barrio
            barrio = False
            locality = None
            province = None

            for comp in address.get("addressComponents", []):
                ctype = comp.get("componentType")
                text = comp.get("componentName", {}).get("text")
            
                if ctype == "locality":
                    locality = text
                elif ctype == "administrative_area_level_1":
                    province = text
                if "neighborhood" in comp.get("componentType", ""):
                    barrio = comp.get("componentName", {}).get("text")
            if locality != "Alvear" or province != "Santa Fe":
                raise UserError(_("La dirección debe pertenecer a Alvear, Santa Fe."))
            self.write({
                "calle_formateada": formatted,
                "latitud": lat,
                "longitud": lng,
                "barrio": barrio,
            })

        except requests.exceptions.RequestException as e:
            _logger.error("Error Google Maps API: %s", str(e), exc_info=True)
            raise UserError(_("Error consultando Google Maps."))

    def action_open_map(self):
        self.ensure_one()
        
        if not self.latitud or not self.longitud:
            return
        
        url = f"https://www.google.com/maps?q={self.latitud},{self.longitud}"
        
        return {
            'type': 'ir.actions.act_url',
            'url': url,
            'target': 'new',
        }


class Stage(models.Model):
    _inherit = 'helpdesk.stage'

    send_whatsapp_template_id = fields.Many2one(
        'whatsapp.template',
        string="WhatsApp Template"
    )
    whatsapp_bot_script_id = fields.Many2one(
        'whatsapp.bot.script',
        string="Bot Script",
        help="Script que manejará la conversación después de enviar el template"
    )
    notified_step = fields.Many2one('helpdesk.stage',string='Etapa notificado')
    hide_in_stage = fields.Boolean("Esconder en sitio")
    assigned_stage = fields.Boolean("Derivado")


    @api.constrains('assigned_stage', 'team_id')
    def _check_unique_assigned_stage(self):
        for stage in self:
            if stage.assigned_stage:
                exists = self.search([
                    ('id', '!=', stage.id),
                    ('assigned_stage', '=', True)
                ], limit=1)
                if exists:
                    raise ValidationError("Solo puede haber una etapa 'Derivado' por equipo.")