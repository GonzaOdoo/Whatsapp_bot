# models/whatsapp_bot.py
from odoo import models, fields, api, _
import re

class WhatsappMessage(models.Model):
    _inherit = 'whatsapp.message'

    def _process_incoming_message_hook(self, body, partner):
        """Hook para procesar mensajes entrantes y responder automáticamente"""
        body_lower = body.strip().lower()
        
        # === RESPUESTAS PREDEFINIDAS SIMPLES ===
        responses = {
            'hola': '¡Hola! 👋 ¿En qué puedo ayudarte hoy?\n\n1️⃣ Consultar estado de pedido\n2️⃣ Horarios de atención\n3️⃣ Contactar con un agente',
            '1': 'Para consultar tu pedido, por favor envía tu número de orden (ej: PED-12345)',
            '2': '🕒 Horarios de atención:\nLunes a Viernes: 8:00 - 18:00\nSábados: 9:00 - 13:00',
            '3': '✅ Un agente se comunicará contigo en breve. ¡Gracias por tu paciencia!',
            'gracias': '¡De nada! 😊 ¿Necesitas algo más?',
        }
        
        # Buscar coincidencia exacta o parcial
        for keyword, reply in responses.items():
            if keyword in body_lower or body_lower.startswith(keyword):
                self._send_auto_reply(partner, reply)
                return True
        
        # === RESPUESTA POR DEFECTO ===
        if not partner.whatsapp_last_interaction:
            self._send_auto_reply(partner, '🤖 No entendí tu mensaje. Por favor elige una opción:\n\n1️⃣ Consultar pedido\n2️⃣ Horarios\n3️⃣ Hablar con agente')
            partner.whatsapp_last_interaction = 'default_flow'
        
        return False

    def _send_auto_reply(self, partner, message):
        """Enviar respuesta automática usando la misma cuenta de WhatsApp"""
        wa_account = self.env['whatsapp.account'].search([], limit=1)
        if wa_account and partner.mobile:
            wa_account._send_message(
                mobile=partner.mobile,
                body=message,
                partner_ids=partner.ids
            )
            _logger.info("🤖 Bot reply sent to %s: %s", partner.name, message[:50])

    @api.model_create_multi
    def create(self, vals_list):
        """Interceptar mensajes entrantes para procesar con el bot"""
        messages = super().create(vals_list)
        
        for message in messages.filtered(lambda m: m.message_type == 'inbound' and m.state == 'received'):
            if message.mail_message_id and message.mail_message_id.partner_ids:
                partner = message.mail_message_id.partner_ids[0]
                body = message.body and re.sub('<[^<]+?>', '', message.body) or ''
                
                # Intentar procesar con el bot
                if body and not message.parent_id:  # No responder a replies
                    try:
                        handled = self._process_incoming_message_hook(body, partner)
                        if handled:
                            message.write({'state': 'replied'})  # Marcar como procesado por bot
                    except Exception as e:
                        _logger.warning("Error en bot WhatsApp: %s", str(e))
        
        return messages