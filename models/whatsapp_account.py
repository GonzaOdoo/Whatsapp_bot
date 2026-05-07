# -*- coding: utf-8 -*-
from odoo import models, modules, fields, api, _
import logging
import json
import base64
import requests
from markupsafe import Markup, escape
from datetime import timedelta
from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
import mimetypes
_logger = logging.getLogger(__name__)

from odoo.addons.phone_validation.tools import phone_validation
from odoo.addons.whatsapp.tools import phone_validation as wa_phone_validation
from odoo.addons.whatsapp.tools.retryable_codes import WHATSAPP_RETRYABLE_ERROR_CODES
from odoo.addons.whatsapp.tools.bounced_codes import BOUNCED_ERROR_CODES
from odoo.addons.whatsapp.tools.whatsapp_api import WhatsAppApi
from odoo.addons.whatsapp.tools.whatsapp_exception import WhatsAppError
from odoo.exceptions import ValidationError, UserError
from odoo.tools import frozendict, groupby, html2plaintext


class WhatsappAccount(models.Model):
    _inherit = 'whatsapp.account'

    def _process_messages(self, value):
        """
        Interceptamos mensajes entrantes y los guardamos directamente en nuestro modelo.
        NO llamamos a super() para evitar que se creen en discuss.channel.
        """
        sender_name = value.get('contacts', [{}])[0].get('profile', {}).get('name')
        _logger.info("📩 WhatsApp webhook recibido")
        
        # Normalizar estructura del valor
        if 'messages' not in value and value.get('whatsapp_business_api_data', {}).get('messages'):
            value = value['whatsapp_business_api_data']
        
        # Procesar SOLO mensajes de texto para el bot
        if not value.get('messages'):
            _logger.warning("No hay mensajes en el webhook")
            return
        
        for message_data in value.get('messages', []):
            _logger.info(message_data)
            try:
                with self.env.cr.savepoint():
                    # Procesar solo mensajes de texto por ahora
                    if message_data.get('type') == 'text':
                        self._process_bot_conversation(message_data, value)
                    elif message_data.get('type') == 'interactive':
                        interactive = message_data.get('interactive', {})
                        
                        if interactive.get('type') == 'button_reply':
                            reply = interactive.get('button_reply', {})
                            
                            # Simular estructura de mensaje de texto
                            message_data['type'] = 'text'
                            message_data['text'] = {
                                'body': reply.get('title') or reply.get('id')
                            }
                            self._process_bot_conversation(message_data, value)
                        
                        elif interactive.get('type') == 'list_reply':
                            reply = interactive.get('list_reply', {})
                            message_data['type'] = 'text'
                            message_data['text'] = {
                                'body': reply.get('id') or reply.get('title')
                            }
                    elif message_data.get('type') == 'button':
                        button = message_data.get('button', {})
                        
                        # Simular mensaje de texto
                        message_data['type'] = 'text'
                        message_data['text'] = {
                            'body': button.get('text') or button.get('payload')
                        }
                        
                        self._process_bot_conversation(message_data, value)
                    elif message_data.get('type') in ('image', 'audio', 'video', 'document', 'sticker'):
                        self._process_media_message(message_data, value)
                    elif message_data.get('type') == 'location':
                        location = message_data.get('location', {})
                        message_data['text'] = {
                            'body': location.get('address') or 'Ubicación compartida'
                        }
                        # 👇 IMPORTANTE: pasar como location
                        self._process_bot_conversation(message_data, value, message_type='location')
                    else:
                        _logger.info("Tipo de mensaje no soportado aún: %s", message_data.get('type'))
                    
            except Exception as e:
                _logger.error("❌ Error procesando mensaje bot: %s", str(e), exc_info=True)
        
        # IMPORTANTE: NO llamamos a super() para evitar duplicación en discuss.channel
        # Los mensajes solo irán a discuss.channel cuando se transfiera a soporte
        return True

    def _process_bot_conversation(self, message_data, full_value, message_type='text'):
        """Crea/actualiza conversación del bot y guarda el mensaje directamente"""
        self = self.sudo()
        
        # Extraer datos del mensaje
        sender_mobile = message_data['from']
        message_body = message_data.get('text', {}).get('body', '')
        location_data = message_data.get('location') if message_type == 'location' else None
        message_id = message_data['id']
        timestamp = message_data.get('timestamp')
       
        _logger.info("📨 Mensaje entrante de %s: %s", sender_mobile, message_body[:50])
        _logger.info(full_value)
        contacts = full_value.get('contacts', [])
        contact_name = None
        if contacts:
            contact_name = contacts[0].get('profile', {}).get('name')
        # Buscar/crear partner
        partner = self._find_or_create_partner(sender_mobile, contact_name)
        if not partner:
            _logger.warning("No se pudo identificar partner para %s", sender_mobile)
            return
        
        # Formatear número
        formatted_number = self._format_phone_number(sender_mobile)
        body_to_store = message_body
        if message_type == 'location' and location_data:
            body_to_store = f"📍 {location_data.get('address')} ({location_data.get('latitude')}, {location_data.get('longitude')})"
        # Buscar/crear conversación
        conversation = self.env['whatsapp.bot.conversation']._find_or_create_conversation(
            self.id, partner.id, sender_mobile, formatted_number
        )
        if self._is_message_processed(message_id, conversation.id):
            _logger.info("⏭️ Mensaje ya procesado, ignorando: %s", message_id)
            return True  # Respondemos 200 OK pero no duplicamos
    
        
        # Crear mensaje directamente en nuestro modelo
        self.env['whatsapp.bot.conversation.message'].create({
            'conversation_id': conversation.id,
            'message_type': 'inbound',
            'message_uid': message_id,
            'body': body_to_store,
            'raw_data': message_data,
            'timestamp': timestamp,
            'sender': sender_mobile,
            'state': 'sent',
        })
        
        # Actualizar último mensaje de la conversación
        conversation.last_message_date = fields.Datetime.now()
        
        _logger.info("✅ Mensaje guardado en conversación bot %s", conversation.id)
        
        if message_type == 'location' and location_data:
            conversation.process_user_message({
                'type': 'location',
                'lat': location_data.get('latitude'),
                'lng': location_data.get('longitude'),
                'address': location_data.get('address'),
            }, type='location')
        
        else:
            conversation.process_user_message(message_body, type='message')

    def _is_message_processed(self, message_uid, conversation_id):
        """Verifica si este mensaje de WhatsApp ya fue procesado"""
        if not message_uid:
            return False
        return bool(self.env['whatsapp.bot.conversation.message'].search_count([
            ('conversation_id', '=', conversation_id),
            ('message_uid', '=', message_uid),
            ('create_date', '>=', fields.Datetime.now() - timedelta(hours=1))
        ]))

    def _process_media_message(self, message_data, full_value):
        self = self.sudo()
    
        sender_mobile = message_data['from']
        message_type = message_data['type']
        media = message_data.get(message_type, {})
        media_id = media.get('id')
        mime_type = media.get('mime_type')
        caption = media.get('caption')
        message_id = message_data.get('id')
        timestamp = message_data.get('timestamp')

        _logger.info("Imagen recibida")
        _logger.info(full_value)
        
        if not media_id:
            _logger.warning("Media sin ID")
            return
    
        contacts = full_value.get('contacts', [])
        contact_name = contacts[0].get('profile', {}).get('name') if contacts else None
    
        partner = self._find_or_create_partner(sender_mobile, contact_name)
        if not partner:
            return
    
        formatted_number = self._format_phone_number(sender_mobile)
    
        conversation = self.env['whatsapp.bot.conversation']._find_or_create_conversation(
            self.id, partner.id, sender_mobile, formatted_number,'principal'
        )
        if self._is_message_processed(message_id, conversation.id):
            _logger.info("⏭️ Media ya procesado, ignorando: %s", message_id)
            return  # Respondemos 200 OK pero no duplicamos
    
        wa_api = WhatsAppApi(self)
        datas = wa_api._get_whatsapp_document(media_id)
        
        filename = media.get('filename')
        if not filename:
            ext = mimetypes.guess_extension(mime_type) or ''
            filename = f"{message_type}_{message_id}{ext}"
    
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(datas),
            'mimetype': mime_type,
            'res_model': 'whatsapp.bot.conversation',
            'res_id': conversation.id,
            'description': caption or f'[{message_type}]',
        })
    
        self.env['whatsapp.bot.conversation.message'].create({
            'conversation_id': conversation.id,
            'message_type': 'inbound',
            'message_uid': message_id,
            'body': caption or f'[{message_type}]',
            'raw_data': message_data,
            'timestamp': timestamp,
            'sender': sender_mobile,
            'state': 'sent',
            'attachment_id': attachment.id,
        })
    
        conversation.last_message_date = fields.Datetime.now()
        conversation.process_user_message(filename,type='image')
        _logger.info("📎 Media %s guardado correctamente (%s)", message_type, attachment.id)

    def _send_auto_response(self, conversation, user_message):
        """Envía una respuesta automática genérica a la conversación"""
        self = self.sudo()
        
        # Respuesta genérica por ahora (luego se integrará el bot completo)
        response_text = "¡Hola! 👋 Soy un asistente automático. Gracias por tu mensaje. Un agente te contactará pronto."
        
        try:
            # Usar el método de la conversación para enviar la respuesta
            conversation.send_bot_response(response_text)
            _logger.info("🤖 Respuesta automática enviada a conversación %s", conversation.id)
            
        except Exception as e:
            _logger.error("❌ Error enviando respuesta automática: %s", str(e), exc_info=True)

    def _find_or_create_partner(self, mobile_number, contact_name=None):
        partner = self.env['res.partner'].search([
            ('phone', 'ilike', mobile_number)
        ], limit=1)
        
        if not partner:
            partner = self.env['res.partner'].create({
                'name': contact_name or _('Contacto WhatsApp %s') % mobile_number[-4:],
                'phone': mobile_number,
            })
            _logger.info("🆕 Partner creado ID %s para número %s", partner.id, mobile_number)
        
        return partner

    def _format_phone_number(self, number):
        """Formatea número según estándar WhatsApp"""
        try:
            from odoo.addons.whatsapp.tools import phone_validation as wa_phone_validation
            country_code = self.company_id.country_id.code if self.company_id.country_id else None
            return wa_phone_validation.wa_phone_format(number, country=country_code, force_format="WHATSAPP")
        except Exception as e:
            _logger.warning("Error formateando número %s: %s", number, str(e))
            return number


class WhatsappMessage(models.Model):
    _inherit = 'whatsapp.message'

    # Campo para vincular con conversaciones del bot (para cuando enviemos respuestas)
    bot_conversation_id = fields.Many2one('whatsapp.bot.conversation', string='Conversación Bot', index=True)


    def _send_message(self, with_commit=False,buttons=False,attachment = False, location_request=False):
        """ Prepare json data for sending messages, attachments and templates."""
        # init api
        _logger.info("Inheriting send function")
        message_to_api = {}
        for account, messages in groupby(self, lambda msg: msg.wa_account_id):
            if not account:
                messages = self.env['whatsapp.message'].concat(*messages)
                messages.write({
                    'failure_type': 'unknown',
                    'failure_reason': 'Missing whatsapp account for message.',
                    'state': 'error',
                })
                self -= messages
                continue
            wa_api = WhatsAppApi(account)
            for message in messages:
                message_to_api[message] = wa_api

        sent_message_vals = set()
        for whatsapp_message in self:
            wa_api = message_to_api[whatsapp_message]
            # try to make changes with current user (notably due to ACLs), but limit
            # to internal users to avoid crash - rewrite me in master please
            if whatsapp_message.create_uid._is_internal():
                whatsapp_message = whatsapp_message.with_user(whatsapp_message.create_uid)
            if whatsapp_message.state != 'outgoing':
                _logger.info("Message state in %s state so it will not sent.", whatsapp_message.state)
                continue
            is_duplicate = False
            msg_uid = False
            try:
                parent_message_id = False
                # body would always come from plaintext2html hence the url text is already the url and references are redundant
                body = html2plaintext(whatsapp_message.body, include_references=False)
                number = whatsapp_message.mobile_number_formatted
                if not number:
                    raise WhatsAppError(failure_type='phone_invalid')
                blacklist_number = wa_phone_validation.wa_phone_format_for_blacklist(number)
                if self.env['phone.blacklist'].sudo().search_count([('number', 'ilike', blacklist_number), ('active', '=', True)], limit=1):
                    raise WhatsAppError(failure_type='blacklisted')

                # based on template
                if template := whatsapp_message.wa_template_id:
                    message_type = 'template'
                    if whatsapp_message.wa_template_id.status != 'approved' or whatsapp_message.wa_template_id.quality == 'red':
                        raise WhatsAppError(failure_type='template')
                    whatsapp_message.message_type = 'outbound'
                    if whatsapp_message.mail_message_id.model != whatsapp_message.wa_template_id.model:
                        raise WhatsAppError(failure_type='template')

                    RecordModel = self.env[whatsapp_message.mail_message_id.model].with_user(whatsapp_message.env.user)
                    from_record = RecordModel.browse(whatsapp_message.mail_message_id.res_id)

                    # if retrying message then we need to unlink previous attachment
                    # in case of header with report in order to generate it again
                    if whatsapp_message.wa_template_id.report_id and whatsapp_message.wa_template_id.header_type == 'document' and whatsapp_message.mail_message_id.attachment_ids:
                        whatsapp_message.mail_message_id.attachment_ids.unlink()

                    # generate sending values, components and attachments
                    send_vals, attachment = whatsapp_message.wa_template_id._get_send_template_vals(
                        record=from_record,
                        whatsapp_message=whatsapp_message,
                    )
                    # reports are considered always unique
                    if not template.report_id:
                        send_vals_without_attachments = dict(send_vals)
                        # the same attachment re-uploaded will have a different identifier
                        # TODO MASTER avoid having to upload as part of the "get template values" flow
                        if template.header_type in ('image', 'video', 'document'):
                            components = [component_vals for component_vals in send_vals['components'] if component_vals['type'] != 'header']
                            send_vals_without_attachments['components'] = components
                        unique_message_vals = (number, frozendict(send_vals_without_attachments))
                        if unique_message_vals not in sent_message_vals:
                            sent_message_vals.add(unique_message_vals)
                        else:
                            is_duplicate = True
                    if attachment and attachment not in whatsapp_message.mail_message_id.attachment_ids:
                        # Clone the attachment to ensure the template's attachment is not affected by message changes
                        cloned_attachment = attachment.copy({'res_model': whatsapp_message.mail_message_id.model, 'res_id': whatsapp_message.mail_message_id.res_id})
                        whatsapp_message.mail_message_id.attachment_ids = [(4, cloned_attachment.id)]
                # no template
                elif whatsapp_message.mail_message_id.attachment_ids or attachment:
                    attachment_vals = whatsapp_message._prepare_attachment_vals(whatsapp_message.mail_message_id.attachment_ids[0] if whatsapp_message.mail_message_id.attachment_ids else attachment, wa_account_id=whatsapp_message.wa_account_id)
                    message_type = attachment_vals.get('type')
                    send_vals = attachment_vals.get(message_type)
                    if body:
                        send_vals['caption'] = body
                elif location_request:
                    message_type = 'interactive'
                    send_vals = {
                        "type": "location_request_message",
                        "body": {
                            "text": body,
                        },
                        "action": {
                            "name": "send_location"
                        }
                    }
                elif buttons:
                    message_type = 'interactive'
                    first_type = buttons[0].get('type')
                    if first_type == 'reply':
                        send_vals = {
                            "type": "button",
                            "body": {
                                "text": body,
                            },
                            "action": {
                                "buttons": buttons
                            }
                        }
                    elif first_type == 'url':
                        button = buttons[0]
                
                        send_vals = {
                            "type": "cta_url",
                            "body": {
                                "text": body,
                            },
                            "action": {
                                "name": "cta_url",
                                "parameters": button.get('parameters')[0]
                            }
                        }
                    elif first_type == 'phone_number':
                        button = buttons[0]
                
                        send_vals = {
                            "type": "cta_call",
                            "body": {
                                "text": body,
                            },
                            "action": {
                                "name": "cta_call",
                                "parameters": button.get('parameters')[0]
                            }
                        }
                else:
                    message_type = 'text'
                    send_vals = {
                        'preview_url': True,
                        'body': body,
                    }
                # Tagging parent message id if parent message is available
                if whatsapp_message.mail_message_id and whatsapp_message.mail_message_id.parent_id:
                    parent_id = whatsapp_message.mail_message_id.parent_id.wa_message_ids
                    if parent_id:
                        parent_message_id = parent_id[0].msg_uid
                if not is_duplicate:
                    _logger.info("Vals enviados")
                    _logger.info(send_vals)
                    msg_uid = wa_api._send_whatsapp(number=number, message_type=message_type, send_vals=send_vals, parent_message_id=parent_message_id)
            except WhatsAppError as we:
                whatsapp_message._handle_error(whatsapp_error_code=we.error_code, error_message=we.error_message,
                                               failure_type=we.failure_type)
            except (UserError, ValidationError) as e:
                whatsapp_message._handle_error(failure_type='unknown', error_message=str(e))
            else:
                if is_duplicate:
                    whatsapp_message.state = 'cancel'
                elif whatsapp_message.state == 'outgoing':
                    if not msg_uid:
                        whatsapp_message._handle_error(failure_type='unknown')
                    else:
                        # Message already posted for model 'discuss.channel', post message in active channel for other models
                        if message_type == 'template' and whatsapp_message.wa_template_id.model != 'discuss.channel':
                            whatsapp_message._post_message_in_active_channel()
                        whatsapp_message.write({
                            'state': 'sent',
                            'msg_uid': msg_uid
                        })
                if with_commit:
                    self.env.cr.commit()


    