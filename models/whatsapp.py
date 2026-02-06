# models/whatsapp_bot.py
from odoo import models, api, _
import re
import logging
from odoo.addons.whatsapp.tools import phone_validation as wa_phone_validation
from odoo.tools import plaintext2html

_logger = logging.getLogger(__name__)


class WhatsappMessage(models.Model):
    _inherit = 'whatsapp.message'

    @api.model_create_multi
    def create(self, vals_list):
        """Interceptar mensajes entrantes y programar respuesta automática"""
        messages = super().create(vals_list)
        
        # Filtrar solo mensajes entrantes nuevos con partner
        inbound_messages = messages.filtered(
            lambda m: m.message_type == 'inbound' 
            and m.state == 'received'
            and not m.parent_id  # Ignorar respuestas
        )
        
        for message in inbound_messages:
            try:
                # ✅ PASAR SOLO EL ID para evitar problemas en postcommit
                message_id = message.id
                self.env.cr.postcommit.add(
                    lambda mid=message_id: self.env['whatsapp.message']._process_bot_reply_by_id(mid)
                )
                _logger.info("🤖 Programada respuesta para mensaje %s", message_id)
            except Exception as e:
                _logger.warning("Error programando respuesta bot: %s", str(e))
        
        return messages

    @api.model
    def _process_bot_reply_by_id(self, message_id):
        """Procesar respuesta usando ID (ejecutado después del commit)"""
        message = self.browse(message_id)
        if not message.exists() or message.state != 'received':
            return
        try:
            partner = self.env['res.partner'].browse(53)
            body = message._extract_plain_text(message.body)
            if not body:
                return
            
            body_lower = body.strip().lower()
            
            # === RESPUESTAS PREDEFINIDAS ===
            responses = {
                'hola': '¡Hola! 👋 ¿En qué puedo ayudarte?\n\n1️⃣ Consultar pedido\n2️⃣ Horarios\n3️⃣ Contactar agente',
                '1': '📦 Envía tu número de pedido (ej: PED-12345)',
                '2': '🕒 Horarios:\nLun-Vie: 8:00-18:00\nSáb: 9:00-13:00',
                '3': '✅ Un agente te contactará pronto. ¡Gracias!',
                'gracias': '¡De nada! 😊 ¿Algo más?',
            }
            
            # Buscar coincidencia
            reply = None
            for keyword, msg in responses.items():
                if keyword in body_lower or body_lower.startswith(keyword):
                    reply = msg
                    break
            
            # Respuesta por defecto
            if not reply:
                reply = '🤖 No entendí. Elige:\n1️⃣ Pedido\n2️⃣ Horarios\n3️⃣ Agente'
            
            # ✅ ENVIAR CREANDO UN NUEVO whatsapp.message (correcto)
            message._send_auto_reply(partner, reply)
            _logger.info("✅ Bot respondió a %s: %s", partner.name, reply[:30])
            
        except Exception as e:
            _logger.error("❌ Error procesando respuesta bot para mensaje %s: %s", message_id, str(e))

    def _extract_plain_text(self, html_content):
        """Extraer texto plano de HTML"""
        if not html_content:
            return ''
        text = re.sub(r'<[^>]+?>', '', html_content)
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    def _send_auto_reply(self, partner, message_text):
        """Enviar respuesta y mostrarla en el chatter"""
        self.ensure_one()
        body_plain = message_text
        # ✅ Obtener cuenta WhatsApp (hardcodeada para pruebas)
        wa_account = self.env['whatsapp.account'].search([], limit=1)
        if not wa_account:
            _logger.error("❌ No hay cuenta WhatsApp")
            return False
        
        # ✅ Números hardcodeados para pruebas
        mobile = "5493876475467"
        formatted_number = "5493876475467"
        _logger.info(message_text)
        try:
            from markupsafe import Markup
            body_html = Markup('<p>%s</p>') % message_text.replace('\n', '<br/>')
            
            # ✅ PASO 1: Encontrar/el canal de WhatsApp activo para este número
            channel = wa_account._find_active_channel(formatted_number)
            if not channel:
                # Crear canal si no existe
                channel = self.env['discuss.channel'].create({
                    'name': f'WhatsApp - {partner.name}',
                    'channel_type': 'whatsapp',
                    'whatsapp_partner_id': partner.id,
                    'whatsapp_number': formatted_number,
                    'whatsapp_channel_valid_until': fields.Datetime.now() + timedelta(days=15),
                })
                _logger.info("🆕 Canal WhatsApp creado (ID %s)", channel.id)
            else:
                _logger.info("💬 Canal WhatsApp encontrado (ID %s)", channel.id)
            
            # ✅ PASO 2: Crear mail.message VINCULADO AL CANAL (clave para que aparezca en chatter)
            mail_message = self.env['mail.message'].create({
                'model': 'discuss.channel',      # ← ¡CRÍTICO!
                'res_id': channel.id,            # ← ¡CRÍTICO!
                'body': body_plain,
                'message_type': 'comment',
                'subtype_id': self.env.ref('mail.mt_comment').id,
                'author_id': self.env.user.partner_id.id,  # Autor = usuario actual (el "bot")
            })
            _logger.info("📧 mail.message creado en canal (ID %s)", mail_message.id)
            
            # ✅ PASO 3: Crear whatsapp.message
            new_wa_message = self.env['whatsapp.message'].create({
                'wa_account_id': wa_account.id,
                'mobile_number': mobile,
                'mobile_number_formatted': formatted_number,
                'body': body_plain,
                'mail_message_id': mail_message.id,
                'message_type': 'outbound',
                'state': 'outgoing',
                'parent_id': self.id,
            })
            _logger.info("📤 whatsapp.message creado (ID %s)", new_wa_message.id)
            
            # ✅ PASO 4: Enviar INMEDIATAMENTE
            new_wa_message._send()
            _logger.info("⚡ Mensaje enviado y visible en chatter")
            
            return True
            
        except Exception as e:
            _logger.error("❌ Error enviando mensaje: %s", str(e), exc_info=True)
            return False